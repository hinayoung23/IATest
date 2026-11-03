"""Create an owned Simulator, verify DTUHID tap/swipe by App UI, then delete it.

Requires an installed runtime, Simulator Lab build, Runner xctestrun and helper.
Never boots, shuts down or installs into a pre-existing Simulator or iPhone.
"""

from __future__ import annotations

import argparse
import json
import plistlib
import subprocess
import time
import uuid
from pathlib import Path

from iatest.application.mobile_service import execute_mobile_plan
from iatest.core.config import init_project
from iatest.execution.control import RunControl
from iatest.mobile.runtime import MobileSession
from iatest.mobile.store import MobileStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True)
    parser.add_argument(
        "--device-type", default="com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro"
    )
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--xctestrun", type=Path, required=True)
    parser.add_argument("--driver", type=Path)
    parser.add_argument("--input-backend", choices=("simulator_hid", "xctest"), default="simulator_hid")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--observation-host", help="Explicit reverse TLS controller address; 127.0.0.1 for Simulator")
    parser.add_argument("--media", action="store_true", help="Verify separate H264 transport, decoding and restart")
    args = parser.parse_args()
    app = args.app.resolve(strict=True)
    if (
        plistlib.loads((app / "Info.plist").read_bytes()).get("CFBundleIdentifier")
        != "dev.iatest.mobile.lab"
    ):
        parser.error("Only the owned IATest Lab is accepted")
    xctestrun = args.xctestrun.resolve(strict=True)
    if args.input_backend == "simulator_hid" and args.driver is None:
        parser.error("Simulator HID requires an explicit driver")
    driver = args.driver.resolve(strict=True) if args.driver else None
    report = {"passed": False, "physical_device": False, "checks": {}}
    device = None

    def simctl(*values, timeout=30, check=True):
        result = subprocess.run(
            ["xcrun", "simctl", *values],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        if check and result.returncode:
            raise RuntimeError("simctl failed: " + result.stderr[-1500:])
        return result

    try:
        runtimes = json.loads(simctl("list", "runtimes", "-j").stdout)["runtimes"]
        selected = [
            r
            for r in runtimes
            if r["identifier"] == args.runtime and r.get("isAvailable")
        ]
        if len(selected) != 1:
            raise ValueError("The explicitly selected runtime is not available")
        report["runtime"] = selected[0]
        device = simctl(
            "create", "iatest-hid-" + uuid.uuid4().hex, args.device_type, args.runtime
        ).stdout.strip()
        uuid.UUID(device)
        report["owned_device"] = device
        simctl("boot", device)
        simctl("bootstatus", device, "-b", timeout=300)
        simctl("install", device, str(app), timeout=60)
        simctl("ui", device, "appearance", "light")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        root = args.output.parent / "simulator-project"
        init_project(root, "simulator")
        profile = {
            "id": "owned-simulator",
            "platform": "ios",
            "device_id": device,
            "app_id": "dev.iatest.mobile.lab",
            "simulator": True,
            "simulator_orientation": "portrait",
            "xctestrun": str(xctestrun),
            "ios_input_backend": args.input_backend,
        }
        if driver:
            profile["simulator_hid_driver"] = str(driver)
        report["input_backend"] = args.input_backend
        if args.observation_host:
            profile["ios_observation"] = {"transport": "reverse_tls", "host": args.observation_host}
            report["observation_transport"] = "reverse_tls"
        MobileStore(root, "simulator").save_profile(profile)

        def expect(identifier, value, attribute="text"):
            return {
                "op": "expect",
                "target": {"identifier": identifier},
                "expected": value,
                "attribute": attribute,
                "timeout_ms": 10000,
            }

        plan = {
            "id": "simulator-hid-business",
            "kind": "ios",
            "budget": {"timeout_seconds": 180, "max_actions": 10, "max_queries": 60},
            "spec": {
                "profile": profile["id"],
                "steps": [
                    {"op": "launch"},
                    expect("ready_label", "Ready"),
                    expect("option_status", "Option off"),
                    {"op": "tap", "target": {"identifier": "option_toggle"}},
                    expect("option_status", "Option on"),
                    expect("gesture_pad", "pans=0 pinches=0", "value"),
                    {
                        "op": "swipe",
                        "target": {"identifier": "gesture_pad"},
                        "direction": "up",
                    },
                    expect("gesture_pad", "pans=1 pinches=0", "value"),
                ],
            },
        }
        MobileStore(root, "simulator").save_plan(plan)
        result = execute_mobile_plan(root, "simulator", plan["id"])
        report["result"] = result
        report["checks"]["tap_and_swipe_business_postconditions"] = (
            result["verdict"] == "passed"
        )
        if result["verdict"] == "passed" and (args.observation_host or args.media):
            session = None
            try:
                session = MobileSession(profile, control=RunControl(timeout_seconds=180))
                session.action({"op": "launch"}, None, "owned-media-launch", 20)
                session.unique(session.query({"identifier": "ready_label"}, cache=False, timeout=5))
                report["media"] = []
                for codec in ("h264-native", "h264"):
                    frame = session.preview(separate=True, codec=codec)
                    assert frame["binary"]
                    if codec == "h264":
                        assert frame["binary"].startswith(b"\x89PNG\r\n\x1a\n")
                    else:
                        assert frame["codec"] == "h264-annexb" and frame["key_frame"]
                    report["media"].append({"codec": codec, "bytes": len(frame["binary"]),
                                            "width": frame["width"], "height": frame["height"]})
                    session.stop_preview(session.media_receiver.id)
                report["checks"]["separate_video_and_restart"] = True
            finally:
                if session:
                    session.close()
                    report["checks"]["owned_runner_processes_stopped"] = all(p.poll() is not None for p in session.device.processes)
                    report["checks"]["tls_listener_closed"] = session.device.observation_listener is None
        if result["verdict"] != "passed":
            snapshot = args.output.with_suffix(".png").resolve()
            capture = simctl("io", device, "screenshot", str(snapshot), check=False)
            if capture.returncode == 0:
                report["failure_screenshot"] = snapshot.name
            report["display_diagnostics"] = simctl(
                "io", device, "enumerate", check=False
            ).stdout[:16000]
        report["passed"] = all(report["checks"].values())
    except Exception as exc:  # noqa: BLE001 -- Preserve runtime/ABI or actual input failure.
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if device:
            simctl("shutdown", device, check=False)
            simctl("delete", device, check=False)
            devices = json.loads(simctl("list", "devices", "-j").stdout)["devices"]
            report["checks"]["owned_simulator_deleted"] = not any(
                d["udid"] == device for group in devices.values() for d in group
            )
        report["passed"] = report["passed"] and all(report["checks"].values())
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.output.exists():
            args.output.with_name(
                args.output.stem
                + "-attempt-"
                + str(time.time_ns())
                + args.output.suffix
            ).write_bytes(args.output.read_bytes())
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(
            json.dumps(
                {k: v for k, v in report.items() if k not in {"result", "runtime"}}
            )
        )
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
