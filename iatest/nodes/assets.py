"""Frozen task assets only: signed App packages and normalized iOS measurements."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import tempfile
from pathlib import Path

from iatest.engines.store import EngineStore, identifier

MAX_APP_BYTES = 1073741824
CHUNK_BYTES = 1048576


def encoded(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False
    ).encode()


def verify_measurement(value):
    from iatest.mobile.ios_metrics import IOS_METRICS

    body = {k: v for k, v in value.items() if k not in {"id", "source_digest"}}
    expected = hashlib.sha256(encoded(body)).hexdigest()
    if value.get("contract_version") != 1 or value.get("source_digest") != expected:
        raise ValueError("Normalized measurement source digest is invalid")
    if type(value.get("coverage_complete")) is not bool or not isinstance(
        value.get("measurements"), dict
    ):
        raise ValueError("Invalid normalized measurement coverage")
    for metric, summary in value["measurements"].items():
        if (
            metric not in IOS_METRICS
            or summary.get("unit") != IOS_METRICS[metric]
            or type(summary.get("samples")) is not int
            or not 1 <= summary["samples"] <= 10000
        ):
            raise ValueError("Invalid normalized measurement metric/unit/count")
        values = [summary.get(k) for k in ("min", "median", "p95", "max")]
        if any(
            type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in values
        ) or values != sorted(values):
            raise ValueError("Invalid normalized measurement summary")


def freeze_assets(root, project, plan):
    from iatest.mobile.delivery import cached_app
    from iatest.nodes.store import plan_tree

    found = {}
    for child in plan_tree(plan):
        preparation = child.get("preparation", {}).get("app", {})
        for field in ("artifact_id", "base_artifact_id"):
            if preparation.get(field):
                value, path = cached_app(root, project, preparation[field])
                if path.is_symlink() or not 1 <= path.stat().st_size <= MAX_APP_BYTES:
                    raise ValueError("App transfer source exceeds bounds")
                manifest = {
                    "kind": "app",
                    "id": value["id"],
                    "sha256": value["sha256"],
                    "bytes": value["bytes"],
                    "metadata": value,
                }
                found["app:" + value["id"]] = {
                    **manifest,
                    "path": str(path),
                    "content": None,
                }
        if child["kind"] == "client_performance" and "measurement" in child["spec"]:
            binding = child["spec"]["measurement"]
            value = EngineStore(root, project).get_artifact(
                "ios_measurement", binding["artifact_id"]
            )
            data = encoded(value)
            if (
                len(data) > 4194304
                or value["source_digest"] != binding["source_digest"]
            ):
                raise ValueError("Measurement transfer source differs from frozen plan")
            found["ios_measurement:" + value["id"]] = {
                "kind": "ios_measurement",
                "id": value["id"],
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "metadata": {"source_digest": value["source_digest"]},
                "path": "",
                "content": data,
            }
    from iatest.engines.frozen import selected

    for entry in selected(root, project, plan):
        data = encoded(entry["value"])
        if len(data) > 4194304:
            raise ValueError("Declarative task asset exceeds 4 MiB")
        found[entry["kind"] + ":" + entry["id"]] = {
            "kind": entry["kind"],
            "id": entry["id"],
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "metadata": {"source_digest": entry["value"]["sha256"]},
            "path": "",
            "content": data,
        }
    if len(found) > 64 or sum(v["bytes"] for v in found.values()) > 2 * MAX_APP_BYTES:
        raise ValueError("Task asset transfer exceeds its total quota")
    return list(found.values())


def asset_chunk(record, offset):
    size = record["manifest"]["bytes"]
    if type(offset) is not int or not 0 <= offset < size or offset % CHUNK_BYTES:
        raise ValueError("Invalid asset chunk offset")
    if record["path"]:
        path = Path(record["path"])
        if path.is_symlink() or path.stat().st_size != size:
            raise ValueError("Frozen App package changed")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            stream.seek(offset)
            data = stream.read(min(CHUNK_BYTES, size - offset))
    else:
        data = record["content"][offset : offset + CHUNK_BYTES]
    if len(data) != min(CHUNK_BYTES, size - offset):
        raise ValueError("Frozen task asset became incomplete")
    return {
        "offset": offset,
        "data": base64.b64encode(data).decode(),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def receive_assets(worker, job):
    from iatest.mobile.delivery import artifact_suffix, file_hash, inspect_package

    store = EngineStore(worker.root, worker.project)
    store.directory.mkdir(parents=True, exist_ok=True)
    for manifest in job.get("assets", []):
        kind, name = manifest["kind"], identifier(manifest["id"])
        if kind not in {
            "app",
            "ios_measurement",
            "security_library",
            "accessibility_review",
        }:
            raise ValueError("Worker rejects arbitrary task file types")
        maximum = MAX_APP_BYTES if kind == "app" else 4194304
        size = manifest["bytes"]
        if type(size) is not int or not 1 <= size <= maximum:
            raise ValueError("Worker task asset quota exceeded")
        with tempfile.NamedTemporaryFile(
            dir=store.directory, prefix="node-asset-", delete=False
        ) as output:
            path = Path(output.name)
            try:
                checksum = hashlib.sha256()
                for offset in range(0, size, CHUNK_BYTES):
                    if worker.stop.is_set():
                        raise RuntimeError("Worker stopping during asset preparation")
                    part = worker.post(
                        "asset",
                        {
                            "job_id": job["id"],
                            "kind": kind,
                            "asset_id": name,
                            "offset": offset,
                        },
                    )
                    data = base64.b64decode(part["data"], validate=True)
                    if (
                        part["offset"] != offset
                        or len(data) != min(CHUNK_BYTES, size - offset)
                        or hashlib.sha256(data).hexdigest() != part["sha256"]
                    ):
                        raise ValueError("Transferred asset chunk is corrupt")
                    checksum.update(data)
                    output.write(data)
                output.flush()
                if checksum.hexdigest() != manifest["sha256"]:
                    raise ValueError(
                        "Transferred asset content differs from frozen digest"
                    )
                output.close()
                if kind == "app":
                    value = manifest["metadata"]
                    platform = value["platform"]
                    if (
                        platform not in {"android", "ios"}
                        or value["sha256"] != manifest["sha256"]
                        or value["bytes"] != size
                    ):
                        raise ValueError("Invalid App transfer metadata")
                    actual = inspect_package(path, platform)
                    if any(
                        actual.get(k) != value["metadata"].get(k)
                        for k in (
                            "app_id",
                            "certificate_sha256",
                            "version_code",
                            "version",
                        )
                    ):
                        raise ValueError("Worker package identity/signature differs")
                    destination = (
                        store.directory
                        / "app-cache"
                        / (
                            manifest["sha256"]
                            + artifact_suffix(value)
                        )
                    )
                    destination.parent.mkdir(exist_ok=True)
                    if (
                        destination.exists()
                        and file_hash(destination) != manifest["sha256"]
                    ):
                        raise ValueError("Worker App cache identity collision")
                    path.replace(destination)
                elif kind == "ios_measurement":
                    value = json.loads(path.read_bytes())
                    verify_measurement(value)
                    if (
                        value.get("id") != name
                        or value.get("source_digest")
                        != manifest["metadata"]["source_digest"]
                        or value.get("contract_version") != 1
                    ):
                        raise ValueError(
                            "Worker normalized measurement binding differs"
                        )
                else:
                    from iatest.engines.frozen import install, verify

                    value = json.loads(path.read_bytes())
                    verify(kind, value, manifest["metadata"]["source_digest"])
                    install(
                        worker.root,
                        worker.project,
                        [{"kind": kind, "id": name, "value": value}],
                    )
                try:
                    old = store.get_artifact(kind, name)
                except ValueError:
                    store.save_artifact(kind, value, artifact_id=name)
                else:
                    if encoded(old) != encoded(value):
                        raise ValueError(
                            "Worker asset ID already names different source content"
                        )
            finally:
                output.close()
                path.unlink(missing_ok=True)
