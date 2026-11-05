from __future__ import annotations
from iatest.core.env import environment_values

import base64
import hashlib
import os
import platform
import ssl
import sys
import threading
import time
import uuid
from dataclasses import asdict
from urllib.parse import urlsplit

import httpx

from iatest.application.engine_analysis import digest
from iatest.application.testing_service import safe_result
from iatest.core.files import atomic_write_bytes
from iatest.core.redaction import redact, secret_scope
from iatest.core.sandbox import origin
from iatest.engines.contracts import EngineContext, ExecutionPlan
from iatest.engines.data import declared_secrets
from iatest.engines.registry import create_engine_registry
from iatest.engines.store import EngineStore, identifier
from iatest.engines.validation import validate_plan
from iatest.execution.control import RunControl
from iatest.nodes.store import (
    PROTOCOL,
    engine_fingerprint,
    environment_fingerprint,
    input_proof,
    plan_tree,
)


def nested_origins(value):
    if isinstance(value, dict):
        if "allowed_origins" in value:
            yield from value["allowed_origins"]
        for child in value.values():
            yield from nested_origins(child)
    elif isinstance(value, list):
        for child in value:
            yield from nested_origins(child)


def export_evidence(root, project, result):
    files, total = [], 0
    store = EngineStore(root, project)
    for item in result.evidence:
        if not item.get("name"):
            continue
        run_id = item.get("run_id") or result.run_id
        path = store.evidence_path(run_id, item["name"])
        if not path.is_file():
            from iatest.mobile.store import MobileStore

            path = MobileStore(root, project).evidence_path(run_id, item["name"])
        data = path.read_bytes()
        total += len(data)
        if total > 8388608:
            raise ValueError("Remote evidence exceeds 8 MiB")
        files.append(
            {
                "run_id": run_id,
                "name": item["name"],
                "data": base64.b64encode(data).decode(),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return files


def import_evidence(root, project, files):
    total = 0
    for item in files:
        data = base64.b64decode(item["data"], validate=True)
        total += len(data)
        if (
            total > 8388608
            or (
                item["name"].endswith(".png")
                and not data.startswith(b"\x89PNG\r\n\x1a\n")
            )
            or hashlib.sha256(data).hexdigest() != item["sha256"]
        ):
            raise ValueError("Invalid or oversized remote evidence")
        path = EngineStore(root, project).evidence_path(item["run_id"], item["name"])
        path.parent.mkdir(parents=True, exist_ok=True)
        if (
            path.exists()
            and hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]
        ):
            raise ValueError("Evidence identity collision")
        atomic_write_bytes(path, data)


class NodeWorker:
    def __init__(
        self,
        root,
        project,
        controller,
        node_id,
        token_env,
        allowed_origins,
        *,
        allowed_kinds=("performance",),
        lease_seconds=5,
        ca_file="",
    ):
        parsed = urlsplit(controller)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Controller must be a configured HTTP(S) origin")
        if parsed.scheme != "https" and parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("Remote node transport requires HTTPS")
        token = environment_values().get(token_env, "")
        if len(token) < 16:
            raise ValueError(
                "Node token environment variable must already contain at least 16 characters"
            )
        self.root, self.project, self.url, self.node_id = (
            root,
            project,
            controller.rstrip("/"),
            identifier(node_id),
        )
        self.allowed_origins = {origin(x) for x in allowed_origins}
        self.allowed_kinds, self.lease_seconds = set(allowed_kinds), lease_seconds
        if not 1 <= lease_seconds <= 30:
            raise ValueError("Node lease must be 1–30 seconds")
        self.instance, self.stop = "agent-" + uuid.uuid4().hex[:20], threading.Event()
        self.client = httpx.Client(
            trust_env=False,
            timeout=2,
            verify=ssl.create_default_context(cafile=ca_file) if ca_file else True,
            headers={"Authorization": "Bearer " + token, "X-IATest-Node": node_id},
        )
        self.fingerprint = engine_fingerprint()

    def post(self, path, value):
        response = self.client.post(
            self.url + "/v1/" + path, json={**value, "instance": self.instance}
        )
        if response.status_code != 200:
            raise RuntimeError(
                "Controller rejected node operation: " + response.text[:500]
            )
        return response.json()

    def register(self):
        response = self.post(
            "register",
            {
                "capabilities": {
                    "host_platform": platform.system(),
                    "python_version": sys.version.split()[0],
                    "host_id": hashlib.sha256(platform.node().encode()).hexdigest()[
                        :16
                    ],
                    "protocol": PROTOCOL,
                    "engine_fingerprint": self.fingerprint,
                    "environment_fingerprint": environment_fingerprint(self.root),
                    "kinds": sorted(self.allowed_kinds),
                    "allowed_origins": sorted(self.allowed_origins),
                    "max_users": 128,
                    "max_requests": 100000,
                }
            },
        )
        self.allowed_origins &= {origin(x) for x in response["allowed_origins"]}
        self.allowed_kinds &= set(response["allowed_kinds"])

    def prepare(self, job):
        plan = ExecutionPlan.from_dict(job["plan"])
        validate_plan(plan)
        if (
            not {p["kind"] for p in plan_tree(job["plan"])} <= self.allowed_kinds
            or not {origin(x) for x in nested_origins(job["plan"])}
            <= self.allowed_origins
        ):
            raise ValueError("Task exceeds worker-local kind/network authorization")
        if any(
            p["kind"] == "remote"
            or (p["kind"] == "performance" and p["spec"].get("distribution"))
            for p in plan_tree(job["plan"])
        ):
            raise ValueError("A worker cannot recursively distribute load")
        proof = input_proof(job["plan"])
        from iatest.nodes.assets import receive_assets

        receive_assets(self, job)
        from iatest.mobile.delivery import cached_app
        from iatest.mobile.store import MobileStore

        for child in plan_tree(job["plan"]):
            if child["kind"] == "client_performance" and "measurement" in child["spec"]:
                binding = child["spec"]["measurement"]
                asset = EngineStore(self.root, self.project).get_artifact(
                    "ios_measurement", binding["artifact_id"]
                )
                if binding.get("source_digest") != asset["source_digest"]:
                    raise ValueError(
                        "Worker measurement differs from the frozen controller source"
                    )
            if child["kind"] == "api" and "requests" not in child["spec"]:
                raise ValueError(
                    "Remote API tasks require explicit typed requests and allowed_origins"
                )
            preparation = child.get("preparation", {}).get("app", {})
            profile_id = preparation.get("profile") or (
                child["spec"].get("profile")
                if child["kind"] in {"android", "ios"}
                else None
            )
            if profile_id:
                profile = MobileStore(self.root, self.project).get(
                    "profile", profile_id
                )
                if job.get("global_leases"):
                    resource = (
                        "device:" + profile["platform"] + ":" + profile["device_id"]
                    )
                    if resource not in {
                        r for d in job["global_leases"] for r in d["fences"]
                    }:
                        raise ValueError(
                            "Worker device is not covered by the controller's global lease"
                        )
            for key in ("artifact_id", "base_artifact_id"):
                if preparation.get(key):
                    cached_app(self.root, self.project, preparation[key])
        # A previous running attempt is never silently re-executed after transport loss/restart.
        try:
            previous = EngineStore(self.root, self.project).get_artifact(
                "node_attempt", job["id"]
            )
        except ValueError:
            previous = {}
        if previous.get("state") in {"running", "executed", "completed"}:
            raise ValueError(
                "Task has a prior dispatched attempt; reconcile it instead of replaying"
            )
        self.post("ready", {"job_id": job["id"], "proof": proof})

    def execute(self, job, server_time):
        try:
            previous = EngineStore(self.root, self.project).get_artifact(
                "node_attempt", job["id"]
            )
        except ValueError:
            previous = {}
        if previous.get("state") in {"running", "executed", "completed"}:
            raise ValueError("A dispatched node task is not automatically replayed")
        plan = ExecutionPlan.from_dict(job["plan"])
        control = RunControl(
            timeout_seconds=plan.budget.get("timeout_seconds", 120),
            max_requests=plan.budget.get("max_requests", 10000),
            cancellation_check=self.stop.is_set,
        )
        stopped = threading.Event()
        last_ack = [time.monotonic()]

        def heartbeat():
            while not stopped.wait(min(0.5, self.lease_seconds / 3)):
                try:
                    response = self.post("heartbeat", {"job_id": job["id"]})
                    last_ack[0] = time.monotonic()
                    if response["cancel"]:
                        control.cancelled.set()
                except (httpx.HTTPError, RuntimeError, ValueError):
                    if time.monotonic() - last_ack[0] >= self.lease_seconds:
                        control.cancelled.set()

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        samples = []
        store = EngineStore(self.root, self.project)
        store.save_artifact(
            "node_attempt",
            {"state": "running", "plan_digest": digest(job["plan"])},
            artifact_id=job["id"],
            replace=True,
        )
        try:
            delay = max(0, job["start"] - server_time)
            control.wait(delay)
            registry = create_engine_registry(self.root, self.project)
            from iatest.engines.performance import PerformanceEngine

            registry._engines["performance"] = PerformanceEngine(
                self.root, self.project, sample_sink=samples.append
            )
            began = time.monotonic()
            with secret_scope(declared_secrets(job["plan"])):
                from iatest.nodes.coordinator import DELEGATED

                delegated = DELEGATED.set(job.get("global_leases", []))
                try:
                    result = registry.execute(plan, EngineContext(control))
                finally:
                    DELEGATED.reset(delegated)
                result.metrics["node"] = self.node_id
                result.metrics["node_runtime_seconds"] = time.monotonic() - began
                # Record sample count independently from the declared metrics.
                result.metrics["transport_sample_count"] = len(samples)
                payload = redact(safe_result(asdict(result)))
                payload["outputs"] = {
                    k: v
                    for k, v in payload.get("outputs", {}).items()
                    if not v.get("sensitive")
                }
                files = export_evidence(self.root, self.project, result)
            store.save_artifact(
                "node_attempt",
                {
                    "state": "executed",
                    "plan_digest": digest(job["plan"]),
                    "result": payload,
                    "samples_digest": digest(samples),
                    "sample_count": len(samples),
                    "evidence": [
                        {k: v for k, v in item.items() if k != "data"} for item in files
                    ],
                },
                artifact_id=job["id"],
                replace=True,
            )
            for sequence in range(0, len(samples), 1000):
                self.post(
                    "samples",
                    {
                        "job_id": job["id"],
                        "sequence": sequence // 1000,
                        "rows": samples[sequence : sequence + 1000],
                    },
                )
            self.post(
                "complete",
                {
                    "job_id": job["id"],
                    "result": payload,
                    "samples_digest": digest(samples),
                    "evidence_files": files,
                },
            )
            store.save_artifact(
                "node_attempt",
                {"state": "completed", "result": payload},
                artifact_id=job["id"],
                replace=True,
            )
        finally:
            stopped.set()
            thread.join(timeout=3)

    def run(self):
        self.register()
        prepared = set()
        while not self.stop.is_set():
            try:
                response = self.post("poll", {})
                job = response["job"]
                if job:
                    try:
                        if (
                            job["state"] in {"preparing", "ready"}
                            and job["id"] not in prepared
                        ):
                            self.prepare(job)
                            prepared.add(job["id"])
                        elif job["state"] == "running":
                            if job["id"] not in prepared:
                                raise ValueError(
                                    "Node did not prepare this task instance"
                                )
                            self.execute(job, response["server_time"])
                            prepared.discard(job["id"])
                    except Exception as exc:  # noqa: BLE001 -- report a failed attempt, never run it again
                        self.post("error", {"job_id": job["id"], "error": str(exc)})
                self.stop.wait(0.1)
            except (httpx.HTTPError, RuntimeError, ValueError):
                self.stop.wait(0.5)

    def close(self):
        self.stop.set()
        self.client.close()
