"""Read-only hints from explicitly selected development App builds; never oracles."""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from iatest.core.redaction import sensitive_key
from iatest.engines.http import validate
from iatest.execution.control import RunControl
from iatest.mobile.delivery import cached_app, inspect_package
from iatest.mobile.devices import NativeDevice, command
from iatest.mobile.lease import HELD_DEVICES, device_scope
from iatest.mobile.store import MobileStore
from iatest.mobile.telemetry import METAL_SCHEMA
from iatest.mobile.telemetry import SCHEMA as TELEMETRY_SCHEMA

BRIDGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "contract_version",
        "test_build",
        "app_id",
        "build",
        "process_id",
        "sequence",
        "timestamp_ms",
        "ready",
        "identifiers",
    ],
    "properties": {
        "performance": TELEMETRY_SCHEMA,
        "metal_gpu": METAL_SCHEMA,
        "contract_version": {"const": 1},
        "test_build": {"const": True},
        "app_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "build": {"type": "string", "minLength": 1, "maxLength": 80},
        "process_id": {"type": "integer", "minimum": 1},
        "sequence": {"type": "integer", "minimum": 1},
        "timestamp_ms": {"type": "integer", "minimum": 1},
        "ready": {
            "type": "object",
            "maxProperties": 20,
            "additionalProperties": {"type": "boolean"},
        },
        "identifiers": {
            "type": "object",
            "maxProperties": 20,
            "additionalProperties": {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
            },
        },
        "input_state": {
            "type": "object",
            "maxProperties": 20,
            "additionalProperties": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "marked",
                    "marked_length",
                    "selected_length",
                    "first_responder",
                ],
                "properties": {
                    "marked": {"type": "boolean"},
                    "first_responder": {"type": "boolean"},
                    "marked_length": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 16384,
                    },
                    "selected_length": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 16384,
                    },
                },
            },
        },
    },
}


def parse_bridge(content, app_id, build, *, now=None, max_age_seconds=30):
    if not 1 <= max_age_seconds <= 300 or not 0 < len(content) <= 16384:
        raise ValueError("Bridge observation exceeds age/byte bounds")
    value = json.loads(content)
    validate(value, BRIDGE_SCHEMA)
    if value["app_id"] != app_id or value["build"] != build:
        raise ValueError("Bridge App/build binding differs")
    age = (time.time() if now is None else now) - value["timestamp_ms"] / 1000
    if age < -5 or age > max_age_seconds:
        raise ValueError("Bridge observation is stale or device clock differs")
    if any(
        sensitive_key(k) for section in ("ready", "identifiers") for k in value[section]
    ):
        raise ValueError("Bridge cannot publish credential or secret fields")
    return {
        **value,
        "trust": "App-declared orientation/readiness hints; native query and trusted business assertions remain mandatory",
    }


def read_bridge(root, project, profile_id, artifact_id, *, control=None, session=None):
    control = control or RunControl(timeout_seconds=15)
    profile = MobileStore(root, project).get("profile", profile_id)
    asset, path = cached_app(root, project, artifact_id)
    metadata = inspect_package(path, asset["platform"])
    if any(
        metadata.get(k) != asset["metadata"].get(k)
        for k in ("app_id", "certificate_sha256", "version_code")
    ):
        raise ValueError("Bridge artifact identity/signature changed")
    if (
        asset["platform"] != profile["platform"]
        or metadata["app_id"] != profile["app_id"]
        or not metadata.get(
            "debuggable"
            if profile["platform"] == "android"
            else "development_provisioning"
        )
    ):
        raise ValueError("Bridge requires a matching verified development App artifact")
    if session is not None and (
        session.closed
        or session.profile != profile
        or (
            session.lease.handle is None and session.lease.key not in HELD_DEVICES.get()
        )
    ):
        raise ValueError("Bridge session does not own the selected device")
    with session.lock if session is not None else device_scope(profile):
        if profile["platform"] == "android":
            device = NativeDevice(profile, "bridge-read")
            device.control = control
            content = device.adb(
                "shell",
                "run-as",
                profile["app_id"],
                "head",
                "-c",
                "16385",
                "files/iatest-bridge.json",
            ).encode()
        elif profile.get("simulator"):
            container = Path(
                command(
                    [
                        "xcrun",
                        "simctl",
                        "get_app_container",
                        profile["device_id"],
                        profile["app_id"],
                        "data",
                    ],
                    control=control,
                    timeout=5,
                ).strip()
            ).resolve()
            path = container / "Library/Caches/iatest-bridge.json"
            if (
                not path.resolve().is_relative_to(container)
                or path.is_symlink()
                or not path.is_file()
            ):
                raise ValueError("Simulator bridge escaped the selected App container")
            with path.open("rb") as stream:
                content = stream.read(16385)
        else:
            with tempfile.TemporaryDirectory(prefix="iatest-bridge-") as directory:
                path = Path(directory) / "observation.json"
                command(
                    [
                        "xcrun",
                        "devicectl",
                        "device",
                        "copy",
                        "from",
                        "--device",
                        profile["device_id"],
                        "--domain-type",
                        "appDataContainer",
                        "--domain-identifier",
                        profile["app_id"],
                        "--source",
                        "Library/Caches/iatest-bridge.json",
                        "--destination",
                        str(path),
                        "--timeout",
                        "5",
                    ],
                    control=control,
                    timeout=7,
                )
                if (
                    not path.is_file()
                    or path.is_symlink()
                    or path.stat().st_size > 16384
                ):
                    raise ValueError("Bridge App-container file exceeds bounds")
                content = path.read_bytes()
        return parse_bridge(content, profile["app_id"], metadata["version_code"])
