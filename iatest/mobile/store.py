from __future__ import annotations

import json
import re
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from iatest.core.paths import (
    project_assets_dir,
    project_reports_root,
    project_runtime_dir,
)
from iatest.core.redaction import redact
from iatest.data.project_storage import ProjectAssetIndex
from iatest.engines.contracts import EngineResult, ExecutionPlan
from iatest.mobile.contracts import validate_mobile_plan


def asset_id(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", value) or value in {".", ".."}:
        raise ValueError("Invalid mobile asset identifier")
    return value


def validate_profile(value: dict) -> dict:
    allowed = {
        "id",
        "platform",
        "device_id",
        "app_id",
        "artifact_dir",
        "xctestrun",
        "runner_port",
        "simulator",
        "simulator_orientation",
        "allow_private_ios_gestures",
        "allow_ime_setup",
        "allowed_apps",
        "allowed_permissions",
        "allow_clipboard",
        "allow_webview",
        "display_id",
        "video_capture",
        "media_limits",
        "app_media_port",
        "app_media_token_env",
        "ios_input_backend",
        "simulator_hid_driver",
        "isolation", "require_os_sandbox",
        "managed_emulator",
        "usb_hid",
        "bluetooth_hid",
        "ios_observation",
    }
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("Unknown device profile fields")
    for key in ("id", "platform", "device_id", "app_id"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise ValueError(f"Device profile requires {key}")
    asset_id(value["id"])
    for key in ("artifact_dir", "xctestrun"):
        if key in value and (not isinstance(value[key], str) or "\x00" in value[key]):
            raise ValueError(f"{key} must be a valid path string")
    if value["platform"] not in {"android", "ios"}:
        raise ValueError("Unsupported device platform")
    if "require_os_sandbox" in value:
        if type(value["require_os_sandbox"]) is not bool:
            raise ValueError("require_os_sandbox must be boolean")
        if value["require_os_sandbox"] and not value.get("managed_emulator"):
            raise ValueError("Full physical-device OS isolation is unavailable; never substitute host process isolation")
    if "managed_emulator" in value:
        from iatest.engines.http import validate
        from iatest.mobile.emulator import SCHEMA

        validate(value["managed_emulator"], SCHEMA)
        if value["platform"] != "android":
            raise ValueError("Managed VM isolation requires Android")
    if "isolation" in value:
        import ipaddress

        from iatest.engines.http import validate
        from iatest.mobile.isolation import SCHEMA

        validate(value["isolation"], SCHEMA)
        if value["platform"] != "android":
            raise ValueError("Device UID network isolation currently requires Android root")
        for endpoint in value["isolation"]["allowed_endpoints"]:
            ipaddress.ip_address(endpoint["ip"])
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value["device_id"]):
        raise ValueError("Invalid device identifier")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_-]+)+", value["app_id"]):
        raise ValueError("Invalid target app identifier")
    if "runner_port" in value and (
        type(value["runner_port"]) is not int
        or not 1024 <= value["runner_port"] <= 65534
    ):
        raise ValueError(
            "Runner port must be between 1024 and 65534; the next port is reserved for media"
        )
    if "simulator" in value and type(value["simulator"]) is not bool:
        raise ValueError("simulator must be boolean")
    if "simulator_orientation" in value and (
        value["platform"] != "ios" or value.get("simulator") is not True
        or value["simulator_orientation"] != "portrait"
    ):
        raise ValueError("Explicit portrait setup requires an iOS Simulator")
    if (
        "allow_private_ios_gestures" in value
        and type(value["allow_private_ios_gestures"]) is not bool
    ):
        raise ValueError("allow_private_ios_gestures must be boolean")
    if "allow_ime_setup" in value and (
        type(value["allow_ime_setup"]) is not bool or value["platform"] != "android"
    ):
        raise ValueError("allow_ime_setup is an Android boolean option")
    for key in ("allow_clipboard", "allow_webview"):
        if key in value and type(value[key]) is not bool:
            raise ValueError(f"{key} must be boolean")
    for key in ("allowed_apps", "allowed_permissions"):
        if key in value and (
            not isinstance(value[key], list)
            or len(value[key]) > 20
            or any(
                not isinstance(x, str)
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_-]+)+", x)
                for x in value[key]
            )
        ):
            raise ValueError(f"{key} requires a bounded identifier allowlist")
    if "display_id" in value and (
        type(value["display_id"]) is not int
        or not 0 <= value["display_id"] <= 1000
        or value["platform"] != "android"
    ):
        raise ValueError("display_id is an Android display integer")
    if "video_capture" in value and value["video_capture"] not in {
        "screenshot",
        "mirror",
        "app_stream",
    }:
        raise ValueError("Unknown explicit video capture source")
    if "media_limits" in value:
        limits = value["media_limits"]
        if not isinstance(limits, dict) or set(limits) - {"duration_seconds", "byte_limit"}:
            raise ValueError("Unknown media budget fields")
        for key, maximum in (("duration_seconds", 7200), ("byte_limit", 2147483648)):
            if key in limits and (type(limits[key]) is not int or not 1 <= limits[key] <= maximum):
                raise ValueError("Media budgets must be bounded positive integers")
        if value.get("video_capture") == "app_stream":
            raise ValueError("App stream does not negotiate extended media budgets")
    if value.get("video_capture") == "mirror" and value["platform"] != "android":
        raise ValueError("Display mirror capture is Android only")
    if value.get("video_capture") == "app_stream" and (
        value["platform"] != "ios"
        or type(value.get("app_media_port")) is not int
        or not 1024 <= value["app_media_port"] <= 65534
        or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", value.get("app_media_token_env", "")
        )
    ):
        raise ValueError(
            "iOS App stream requires a port and existing token env reference"
        )
    if value.get("ios_input_backend", "xctest") not in {"xctest", "simulator_hid", "usb_hid", "android_bluetooth_hid"}:
        raise ValueError("Unknown iOS input backend")
    if "ios_observation" in value:
        from iatest.mobile.network_observation import validate_observation

        if value["platform"] != "ios" or value.get("video_capture") == "app_stream":
            raise ValueError("Network observation requires iOS Runner capture, not App stream")
        validate_observation(value["ios_observation"], simulator=value.get("simulator", False))
    if "usb_hid" in value:
        from iatest.mobile.usb_hid import validate_config

        validate_config(value["usb_hid"])
    if "bluetooth_hid" in value:
        from iatest.mobile.bluetooth_hid import validate_config

        validate_config(value["bluetooth_hid"])
    if value.get("ios_input_backend") == "android_bluetooth_hid" and (
        value["platform"] != "ios" or value.get("simulator") or not value.get("bluetooth_hid")
        or not value.get("ios_observation")
    ):
        raise ValueError("Bluetooth HID requires a physical iPhone, paired Android sender and independent TLS observation")
    if value.get("ios_input_backend") == "usb_hid" and (
        value["platform"] != "ios" or value.get("simulator") or not value.get("usb_hid")
        or not value.get("ios_observation")
    ):
        raise ValueError("USB HID requires a physical iPhone, external bridge and independent TLS observation")
    if value.get("ios_input_backend") == "simulator_hid" and (
        value["platform"] != "ios"
        or value.get("simulator") is not True
        or not isinstance(value.get("simulator_hid_driver"), str)
        or not value["simulator_hid_driver"]
        or "\x00" in value["simulator_hid_driver"]
    ):
        raise ValueError(
            "Simulator HID requires an explicit iOS Simulator profile and driver path"
        )
    return dict(value)


class MobileStore:
    def __init__(self, root: Path, project: str):
        self.root, self.project = root, project
        self.directory = project_runtime_dir(root, project) / "mobile"
        self.index = ProjectAssetIndex(root, project, "mobile")
        self.path = self.index.path

    @contextmanager
    def connection(self):
        with self.index.connection() as conn:
            yield conn

    def has_assets(self) -> bool:
        return (
            self.path.is_file()
            or self.index.legacy.is_file()
            or any(
                next(
                    (
                        project_assets_dir(self.root, self.project) / kind / "profiles"
                    ).glob("*.json"),
                    None,
                )
                is not None
                for kind in ("android", "ios")
            )
        )

    def save_profile(self, value: dict) -> None:
        self._save("profile", value["id"], validate_profile(value))

    def save_plan(self, value: dict) -> None:
        plan = ExecutionPlan.from_dict(value)
        validate_mobile_plan(plan)
        self._save("plan", plan.id, asdict(plan))

    def _save(self, kind: str, identifier: str, value: dict) -> None:
        asset_id(identifier)
        with self.connection() as conn:
            conn.execute(
                "insert or replace into assets values(?,?,?)",
                (
                    kind,
                    identifier,
                    json.dumps(value, ensure_ascii=False, allow_nan=False),
                ),
            )

    def get(self, kind: str, identifier: str) -> dict:
        asset_id(identifier)
        with self.connection() as conn:
            row = conn.execute(
                "select content from assets where kind=? and id=?", (kind, identifier)
            ).fetchone()
        if row is None:
            raise ValueError(f"Mobile {kind} not found: {identifier}")
        value = json.loads(row["content"])
        if kind == "profile":
            from iatest.mobile.emulator import bound_profile

            return bound_profile(self.root, self.project, value)
        return value

    def list(self, kind: str) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "select content from assets where kind=? order by id", (kind,)
            ).fetchall()
        return [json.loads(r["content"]) for r in rows]

    def delete(self, kind: str, identifier: str) -> None:
        asset_id(identifier)
        with self.connection() as conn:
            conn.execute("delete from assets where kind=? and id=?", (kind, identifier))

    def save_result(self, result: EngineResult) -> str:
        result.run_id = result.run_id or "mobile-" + uuid.uuid4().hex[:20]
        with self.connection() as conn:
            conn.execute(
                "insert or replace into runs values(?,?,?,?)",
                (
                    result.run_id,
                    result.plan_id,
                    datetime.now(UTC).isoformat(),
                    json.dumps(redact(asdict(result)), ensure_ascii=False),
                ),
            )
        return result.run_id

    def runs(self, limit: int = 50) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "select content,created_at from runs order by created_at desc limit ?",
                (max(1, min(200, limit)),),
            ).fetchall()
        return [
            {**json.loads(r["content"]), "created_at": r["created_at"]} for r in rows
        ]

    @property
    def evidence_roots(self):
        return (
            project_reports_root(self.root, self.project),
            self.directory / "evidence",
        )

    def evidence_path(self, run_id: str, name: str, *, kind=None) -> Path:
        from iatest.data.project_storage import evidence_path

        if not name.endswith(".png"):
            raise ValueError("Invalid mobile evidence name")
        return evidence_path(self.root, self.project, "mobile", run_id, name, kind=kind)
