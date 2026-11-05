"""Minimal native workers with per-request host grants and bounded evidence."""

from __future__ import annotations
from iatest.core.env import environment_values

import copy
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from iatest.core.config import load_config
from iatest.core.sandbox import (
    ACTIVE_POLICY,
    SandboxPolicy,
    policy_scope,
    positive,
    read_file,
)
from iatest.engines.contracts import EngineResult
from iatest.execution.control import RunCancelled, RunControl
from iatest.execution.isolation import (
    container_command,
    stop_worker,
    worker_environment,
)


def stage_uploads(value, policy, directory, worker_directory, maximum):
    """Copy only explicitly declared uploads, with policy and a total byte cap."""
    total = 0
    for step in value.get("spec", {}).get("steps", []):
        if step["op"] != "upload":
            continue
        paths = []
        for item in step["files"]:
            source = policy.check_path(item)
            if ACTIVE_POLICY.get() is not None:
                ACTIVE_POLICY.get().check_path(source)
            with policy_scope(policy):
                content = read_file(
                    source, min(maximum, step.get("max_bytes", 8388608))
                )
            total += len(content)
            if total > maximum:
                raise ValueError("Native worker input quota exceeded")
            folder = directory / str(uuid.uuid4().hex)
            folder.mkdir()
            target = folder / source.name
            target.write_bytes(content)
            target.chmod(0o600)
            paths.append(str(worker_directory / folder.name / source.name))
        step["files"] = paths
    if value.get("kind") == "client_performance":
        total += stage_uploads(
            value["spec"]["plan"], policy, directory, worker_directory, maximum - total
        )
    return total


def referenced_env(value):
    if isinstance(value, dict):
        yield from value.get("input_env", {}).values()
        for key in ("username_env", "password_env", "path_env", "tls_ca_env"):
            if key in value:
                yield value[key]
        if set(value) == {"$env"}:
            yield value["$env"]
        for child in value.values():
            yield from referenced_env(child)
    elif isinstance(value, list):
        for child in value:
            yield from referenced_env(child)


def stage_databases(value, policy, directory, worker_directory, maximum, control):
    """SQLite backup includes committed WAL content; workers never open the original."""
    import sqlite3

    overrides = {}
    source = value.get("spec", {}).get("database")
    if value.get("kind") == "data" and source and source["driver"] != "sqlite":
        if source.get("tls_ca_env"):
            env_name = source["tls_ca_env"]
            path = policy.check_path(environment_values().get(env_name, ""))
            if ACTIVE_POLICY.get() is not None:
                ACTIVE_POLICY.get().check_path(path)
            content = read_file(path, min(maximum, 1048576))
            target = directory / ("database-ca-" + uuid.uuid4().hex + ".pem")
            target.write_bytes(content)
            target.chmod(0o600)
            overrides[env_name] = str(worker_directory / target.name)
            return len(content), overrides
        return 0, overrides
    if value.get("kind") == "data" and source:
        env_name = source["path_env"]
        path = policy.check_path(environment_values().get(env_name, ""))
        if ACTIVE_POLICY.get() is not None:
            ACTIVE_POLICY.get().check_path(path)
        if not path.is_file() or path.stat().st_size > maximum:
            raise ValueError("Database input exceeds worker quota")
        name = "database-" + uuid.uuid4().hex + ".sqlite"
        target = directory / name
        reader = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.1)
        writer = sqlite3.connect(target)
        try:
            def progress(status, remaining, total):
                control.check()
                if total * reader.execute("PRAGMA page_size").fetchone()[0] > maximum:
                    raise ValueError("Database snapshot exceeds worker input quota")
            reader.backup(writer, pages=64, progress=progress, sleep=.01)
            # A WAL source also copies its journal mode. Seal the owned snapshot
            # as one database file so a read-only container mount needs no SHM/WAL.
            if writer.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise ValueError("Cannot seal the database snapshot for read-only input")
        finally:
            writer.close()
            reader.close()
        if target.stat().st_size > maximum:
            raise ValueError("Database snapshot exceeds worker input quota")
        overrides[env_name] = str(worker_directory / name)
        return target.stat().st_size, overrides
    return 0, overrides


def execute_native_isolated(engine, plan, context):
    supported = (
        plan.kind in {"web", "security", "data", "protocol", "accessibility"}
        or plan.kind == "api"
        and "requests" in plan.spec
        or plan.kind == "performance"
        and not plan.spec.get("distribution")
        or plan.kind == "client_performance"
        and plan.spec["platform"] == "web"
    )
    if not supported:
        return None
    config = load_config(engine.root)
    settings = config["sandbox"]
    mode = settings.get("mode", "policy")
    if mode not in {"process", "container"}:
        return None
    from iatest.engines.store import EngineStore
    from iatest.nodes.worker import import_evidence

    timeout = min(
        positive(settings, "hard_timeout_seconds", 300),
        context.control.remaining() or 3600,
    )
    grace = min(positive(settings, "termination_grace_seconds", 5), 30)
    maximum = int(positive(settings, "max_output_bytes", 64000000))
    max_input = int(positive(settings, "max_input_bytes", 64000000))
    policy = SandboxPolicy.from_config(engine.root, config, project=engine.project)
    container_name = "iatest-native-" + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="iatest-native-") as temporary:
        work = Path(temporary)
        inputs, outputs = work / "input", work / "output"
        inputs.mkdir(mode=0o700)
        outputs.mkdir(mode=0o700)
        files = inputs / "files"
        files.mkdir()
        raw = copy.deepcopy(asdict(plan))
        worker_files = Path("/input/files") if mode == "container" else files
        used = stage_uploads(raw, policy, files, worker_files, max_input)
        worker_root = Path("/tmp/project") if mode == "container" else work / "project"
        narrowed = {
            **settings,
            "mode": "policy",
            "read_paths": [str(worker_files)],
            "denied_read_paths": [],
        }
        env = worker_environment({**settings, "env_allowlist": []}, work)
        # Explicit business references only. No dotenv/config/model secrets copied.
        for key in referenced_env(raw):
            if not isinstance(key, str) or key not in environment_values():
                raise ValueError("Referenced worker credential is missing")
            env[key] = environment_values()[key]
        db_bytes, db_env = stage_databases(raw, policy, files, worker_files, max_input - used, context.control)
        used += db_bytes
        env.update(db_env)
        from iatest.engines.web import chromium_path

        executable = chromium_path()
        if executable and mode == "process":
            env["IATEST_CHROMIUM_PATH"] = executable
        payload = {
            "frozen_assets": __import__(
                "iatest.engines.frozen", fromlist=["selected"]
            ).selected(engine.root, engine.project, raw),
            "root": str(worker_root),
            "global_leases": __import__(
                "iatest.nodes.coordinator", fromlist=["held_descriptors"]
            ).held_descriptors()
            or __import__(
                "iatest.nodes.coordinator", fromlist=["DELEGATED"]
            ).DELEGATED.get(),
            "plan": raw,
            "config": {"sandbox": narrowed},
            "settings": settings,
            "timeout": timeout,
            "max_requests": plan.budget.get("max_requests", 10000),
            "max_output": maximum,
            "parent_policy": ACTIVE_POLICY.get().to_dict()
            if ACTIVE_POLICY.get()
            else None,
        }
        encoded = (
            json.dumps(payload, ensure_ascii=False, allow_nan=False).encode() + b"\n"
        )
        if used + len(encoded) > max_input:
            raise ValueError("Native worker input quota exceeded")
        if mode == "container":
            if not shutil.which("docker"):
                raise ValueError(
                    "Container mode requires Docker; no isolation downgrade"
                )
            network = settings.get("container_network", "none")
            if network != "none":
                probe = subprocess.run(
                    [
                        "docker",
                        "network",
                        "inspect",
                        "--format",
                        "{{.Internal}}",
                        network,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if probe.returncode or probe.stdout.strip() != "true":
                    raise ValueError("Container mode requires an internal test network")
            # Feed env over stdin as part of the payload, never an env-file or argv.
            payload["environment"] = {
                k: v for k, v in env.items() if k in set(referenced_env(raw))
            }
            encoded = json.dumps(payload, ensure_ascii=False).encode() + b"\n"
            if used + len(encoded) > max_input:
                raise ValueError("Native worker input quota exceeded")
            (inputs / "worker.env").write_text(
                "HOME=/tmp\nTMPDIR=/tmp\nPYTHONDONTWRITEBYTECODE=1\n", encoding="utf-8"
            )
            args = container_command(settings, inputs, outputs, container_name)
            args.insert(2, "-i")
            args = args[: args.index("iatest.execution.worker")] + [
                "iatest.execution.native_worker"
            ]
            process_env = None
        else:
            source = str(Path(__file__).resolve().parents[2])
            bootstrap = f"import sys;sys.path.insert(0,{source!r});from iatest.execution.native_worker import main;main()"
            args = [sys.executable, "-I", "-B", "-c", bootstrap]
            process_env = env
        process = subprocess.Popen(
            args,
            cwd=work,
            env=process_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name == "posix",
        )
        job = None
        messages = queue.Queue(maxsize=4)
        reader_done = threading.Event()
        send_lock = threading.Lock()

        def send(data):
            encoded_line = json.dumps(data).encode() + b"\n"
            with send_lock:
                process.stdin.write(encoded_line)
                process.stdin.flush()

        def read():
            consumed = 0
            try:
                while line := process.stdout.readline(maximum + 1):
                    consumed += len(line)
                    if len(line) > maximum or consumed > maximum * 2:
                        raise ValueError("Worker protocol output quota exceeded")
                    messages.put(json.loads(line))
            except Exception as exc:  # noqa: BLE001 -- return a bounded protocol error
                messages.put({"type": "protocol_error", "error": type(exc).__name__})
            finally:
                reader_done.set()

        thread = threading.Thread(target=read, daemon=True, name="iatest-native-output")
        started, cancelled_at, response, reason = time.monotonic(), None, None, ""
        cleanup = None
        try:
            if os.name == "nt":
                from iatest.execution.windows_job import attach_job

                job = attach_job(process, settings)
            process.stdin.write(encoded)
            process.stdin.flush()
            thread.start()
            while True:
                if not reason:
                    reason = context.control.reason() or (
                        "Native worker hard timeout"
                        if time.monotonic() - started >= timeout
                        else ""
                    )
                if reason and cancelled_at is None:
                    cancelled_at = time.monotonic()
                    send({"type": "cancel"})
                if (
                    cancelled_at is not None
                    and time.monotonic() - cancelled_at >= grace
                ):
                    break
                try:
                    value = messages.get(timeout=0.05)
                except queue.Empty:
                    if reader_done.is_set() and process.poll() is not None:
                        break
                    continue
                if value.get("type") == "request":
                    if type(value.get("id")) is not int or value["id"] <= 0:
                        raise ValueError("Invalid worker request grant")
                    if value.get("phase") == "normal":
                        grant_control = context.control
                    elif value.get("phase") == "cleanup":
                        if cleanup is None:
                            cleanup = RunControl(timeout_seconds=15, max_requests=100)
                        grant_control = cleanup
                    else:
                        raise ValueError("Invalid worker request phase")
                    allowed = False
                    try:
                        if reason and value["phase"] == "normal":
                            raise RunCancelled(reason)
                        grant_control.request()
                        allowed = True
                    except RunCancelled:
                        pass
                    send({"type": "grant", "id": value["id"], "allowed": allowed})
                elif value.get("type") == "result":
                    response = value
                    break
                else:
                    raise ValueError("Invalid native worker protocol")
            if response is None:
                result = EngineResult(
                    plan.id,
                    plan.kind,
                    lifecycle="cancelled" if context.control.reason() else "error",
                    verdict="inconclusive",
                    error=reason or "Native worker exited without a complete result",
                )
            else:
                result = EngineResult(**response["result"])
                if result.plan_id != plan.id or result.kind != plan.kind:
                    raise ValueError("Native worker returned a different plan")
                import_evidence(engine.root, engine.project, response["evidence"])
                for child in response.get("children", []):
                    EngineStore(engine.root, engine.project).save_result(
                        EngineResult(**child)
                    )
                samples = response.get("samples", [])
                if not isinstance(samples, list) or len(samples) > 100000:
                    raise ValueError("Invalid isolated load samples")
                if getattr(engine, "sample_sink", None):
                    for sample in samples:
                        engine.sample_sink(sample)
                if reason:
                    result.lifecycle, result.verdict, result.error = (
                        "cancelled",
                        "inconclusive",
                        reason,
                    )
            result.metrics["isolation"] = {
                "mode": mode,
                "worker_pid": response.get("worker_pid") if response else process.pid,
                "controller_pid": os.getpid(),
                "normal_requests_granted": context.control.request_count(),
                "cleanup_requests_granted": cleanup.request_count() if cleanup else 0,
                "hard_timeout_seconds": timeout,
            }
            return result
        finally:
            if mode == "container":
                try:
                    subprocess.run(
                        ["docker", "rm", "-f", container_name],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
            # Also reap descendants if the worker itself exited after an error.
            # Docker owns the container's processes; rm -f above reaps them.
            # An exited CLI's process group may no longer be ours to signal.
            stop_worker(process, min(grace, 0.5), process_group=mode != "container")
            if job:
                job()
            for stream in (process.stdin, process.stdout):
                stream.close()
            if thread.ident:
                thread.join(1)
