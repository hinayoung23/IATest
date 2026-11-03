"""Real logical-secondary-display pixels/input in a newly owned Android VM.

This changes only the disposable VM's overlay setting, never a physical phone.
It proves Android multi-display integration, not external-monitor hardware.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path

from iatest.application.testing_service import execute_test_plan
from iatest.core.config import init_project
from iatest.engines.contracts import ExecutionPlan
from iatest.execution.control import RunControl
from iatest.mobile.delivery import fetch_app
from iatest.mobile.devices import NativeDevice
from iatest.mobile.emulator import plan_vm_scope
from iatest.mobile.runtime import MobileSession
from iatest.mobile.store import MobileStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--apk", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--java-home", type=Path)
    parser.add_argument("--image", default="android-36;google_apis;arm64-v8a")
    parser.add_argument("--project", default="secondary-display-acceptance")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.java_home:
        os.environ["JAVA_HOME"] = str(args.java_home.resolve(strict=True))
    root = Path.cwd()
    init_project(root, args.project)
    artifact = fetch_app(root, args.project, {"id": "display-lab", "platform": "android", "app_id": "dev.iatest.mobile.lab", "path": str(args.apk.resolve())})
    original = {
        "id": "display-vm", "platform": "android", "device_id": "managed",
        "app_id": artifact["metadata"]["app_id"], "artifact_dir": str(args.artifacts.resolve()),
        "require_os_sandbox": True, "video_capture": "mirror",
        "managed_emulator": {"sdk_root": str(args.sdk.resolve()), "system_image": args.image, "artifact_id": artifact["id"]},
    }
    store = MobileStore(root, args.project)
    store.save_profile(original)
    target = lambda name: {"identifier": original["app_id"] + ":id/" + name}
    text = "display-" + uuid.uuid4().hex[:12]
    plan = {
        "id": "selected-display-ui", "kind": "android", "budget": {"timeout_seconds": 120},
        "spec": {"profile": original["id"], "steps": [
            {"op": "launch"},
            {"op": "wait", "target": target("nickname_field"), "attribute": "enabled", "expected": True, "timeout_ms": 10000},
            {"op": "text", "target": target("nickname_field"), "value": text, "mode": "semantic"},
            {"op": "expect", "target": target("nickname_field"), "attribute": "value", "expected": text},
            {"op": "tap", "target": target("save_button")},
            {"op": "expect", "target": target("saved_status"), "attribute": "text", "expected": "Saved: " + text},
        ]},
    }
    control = RunControl(timeout_seconds=480)
    report = {"passed": False, "emulator": True, "physical_external_display": False, "checks": {}}
    try:
        with plan_vm_scope(root, args.project, ExecutionPlan.from_dict(plan), control) as evidence:
            report["os_sandbox"] = evidence
            device = NativeDevice(store.get("profile", original["id"]), "display-environment")
            device.control = control

            def display_ids():
                return set(map(int, re.findall(r"Display id (\d+):", device.adb("shell", "cmd", "display", "get-displays"))))

            baseline = display_ids()
            device.adb("shell", "settings", "put", "global", "overlay_display_devices", "640x480/160")
            deadline = time.monotonic() + 10
            while not (extra := display_ids() - baseline):
                if time.monotonic() >= deadline:
                    raise TimeoutError("Owned VM secondary display did not appear")
                control.wait(0.1)
            if len(extra) != 1:
                raise ValueError("Secondary display identity is ambiguous")
            selected, = extra
            report["display_id"] = selected
            store.save_profile({**original, "display_id": selected})
            result = execute_test_plan(root, args.project, plan, control=control)
            report["ui"] = result
            if result["verdict"] != "passed":
                raise RuntimeError("Selected-display native UI journey did not pass")
            session = MobileSession(store.get("profile", original["id"]), root=root, project=args.project, control=control)
            try:
                session.action({"op": "launch"}, None, "display-launch-" + uuid.uuid4().hex, 15)
                screenshot = session.preview()
                pixels = screenshot.pop("binary")
                report["png"] = {**screenshot, "bytes": len(pixels), "sha256": hashlib.sha256(pixels).hexdigest()}
                report["checks"]["secondary_png_geometry"] = (screenshot["coordinate_width"], screenshot["coordinate_height"], screenshot["display_id"]) == (640, 480, selected)
                region = session.visual_region(target("reset_button"))
                anchor = {**{k: region[k] for k in ("sha256", "width", "height", "viewport")}, "x": region["width"] / 2, "y": region["height"] / 2}
                report["visual_dispatch"] = session.visual_click({"target": target("reset_button"), "visual_anchor": anchor}, "display-visual-" + uuid.uuid4().hex)
                deadline = time.monotonic() + 10
                while True:
                    observed = session.query(target("saved_status"), cache=False)
                    if observed.get("complete") and observed.get("match_count") == 1 and observed["candidates"][0].get("text") == "Not saved":
                        report["checks"]["visual_business_postcondition"] = True
                        break
                    if time.monotonic() >= deadline:
                        raise AssertionError("Secondary-display visual click did not reset the saved value")
                    control.wait(0.05)
                preview = session.preview(separate=True)
                data = preview.pop("binary")
                report["png_preview"] = {**preview, "bytes": len(data)}
                report["checks"]["secondary_png_preview"] = preview.get("display_id") == selected and preview["width"] == 640 and preview["height"] == 480
                session.stop_preview(preview["receiver_id"])
                packet = session.preview(separate=True, codec="h264-native", request_keyframe=True)
                data = packet.pop("binary", b"")
                report["encoded"] = {**packet, "bytes": len(data)}
                report["checks"]["secondary_h264_mirror"] = bool(data) and packet.get("key_frame") is True and packet.get("display_id") == selected and packet.get("coordinate_width") == 640 and packet.get("coordinate_height") == 480
            finally:
                session.close()
        report["checks"]["vm_cleaned"] = bool(evidence.get("process_stopped") and evidence.get("userdata_removed"))
        report["passed"] = all(report["checks"].values())
    except Exception as exc:  # noqa: BLE001 -- Persist the acceptance failure after owned-VM cleanup.
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        store.save_profile(original)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.output.exists():
            args.output.with_name(args.output.stem + "-attempt-" + str(time.time_ns()) + args.output.suffix).write_bytes(args.output.read_bytes())
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"passed": report["passed"], "checks": report["checks"], "error": report.get("error"), "output": str(args.output)}))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
