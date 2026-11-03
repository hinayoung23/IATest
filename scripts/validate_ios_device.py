"""Real iOS Runner diagnostics; optional input is restricted to IATest Lab.

Requires a signed framework xctestrun and an installed foreground target App.
There is no synthetic-device fallback. Images remain local and no model is used.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from iatest.application.mobile_service import execute_mobile_plan
from iatest.core.config import init_project
from iatest.core.files import atomic_write_text
from iatest.execution.control import RunControl
from iatest.mobile.runtime import MobileSession
from iatest.mobile.store import MobileStore


def expect(identifier, expected=True, **options):
    return {
        "op": "expect",
        "target": {"identifier": identifier},
        "expected": expected,
        "timeout_ms": 10000,
        **options,
    }


def tap(identifier):
    return {"op": "tap", "target": {"identifier": identifier}}


def lab_plans(profile):
    variable = {"$value": "nickname"}
    return [
        {
            "id": "ios-lab-save-relaunch",
            "kind": "ios",
            "title": "保存随机昵称、验证导航及重启读回",
            "budget": {"timeout_seconds": 180, "max_actions": 16, "max_queries": 100},
            "data_generation": {
                "nickname": {
                    "kind": "string",
                    "prefix": "iatest_",
                    "minLength": 22,
                    "maxLength": 24,
                    "unique": True,
                }
            },
            "spec": {
                "profile": profile,
                "steps": [
                    {"op": "launch"},
                    expect("ready_label", "Ready", attribute="text"),
                    tap("reset_button"),
                    expect("saved_status", "Not saved", attribute="text"),
                    {
                        "op": "text",
                        "target": {"identifier": "nickname_field"},
                        "mode": "keyboard",
                        "value": variable,
                    },
                    expect("nickname_field", variable, attribute="text"),
                    tap("save_button"),
                    expect(
                        "saved_status", variable, attribute="text", operator="contains"
                    ),
                    tap("option_toggle"),
                    expect("option_status", "Option on", attribute="text"),
                    tap("item_1"),
                    expect("detail_label", "Item 1 detail", attribute="text"),
                    {"op": "tap", "target": {"type": "button", "identifier": "BackButton", "label": "Back to lab"}},
                    expect("detail_label", False),
                    {"op": "terminate"},
                    {"op": "launch"},
                    expect("nickname_field", variable, attribute="text"),
                    expect(
                        "saved_status", variable, attribute="text", operator="contains"
                    ),
                ],
            },
        },
        {
            "id": "ios-lab-gesture-feedback",
            "kind": "ios",
            "title": "单指滑动及双指缩放，以 App 手势反馈验收",
            "budget": {"timeout_seconds": 120, "max_actions": 8, "max_queries": 80},
            "spec": {
                "profile": profile,
                "steps": [
                    {"op": "launch"},
                    tap("reset_button"),
                    expect("gesture_pad", "pans=0 pinches=0", attribute="value"),
                    {"op": "swipe", "target": {"identifier": "gesture_pad"}},
                    expect("gesture_pad", "pans=1 pinches=0", attribute="value"),
                    {
                        "op": "pinch",
                        "target": {"identifier": "gesture_pad"},
                        "scale": 1.5,
                        "velocity": 1,
                    },
                    expect("gesture_pad", "pans=1 pinches=1", attribute="value"),
                ],
            },
        },
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True)
    parser.add_argument("--app", required=True)
    parser.add_argument("--xctestrun", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--project", default="ios-device-validation")
    parser.add_argument("--media", action="store_true")
    parser.add_argument("--exercise-lab", action="store_true")
    parser.add_argument("--identifier", help="Use a bounded native identifier query for a foreground App")
    parser.add_argument(
        "--observation-host",
        help="Explicit Mac LAN IPv4 for physical reverse-TLS control and media",
    )
    parser.add_argument("--require-wireless", action="store_true", help="Reject USB and require a connected CoreDevice local-network tunnel")
    args = parser.parse_args()
    if args.require_wireless and not args.observation_host:
        parser.error("--require-wireless needs --observation-host")
    if args.exercise_lab and args.app != "dev.iatest.mobile.lab":
        parser.error("--exercise-lab only operates the framework-owned IATest Lab")
    if not args.xctestrun.is_file() or args.xctestrun.suffix != ".xctestrun":
        parser.error("A built and signed framework .xctestrun is required")
    root = args.root.resolve()
    report = {
        "physical_device": True,
        "app": args.app,
        "checks": {},
        "replays": [],
        "media": [],
        "model_calls": 0,
        "scope": "Native diagnostics; optional lab-only input. No model, clipboard or other App input; reverse TLS may request user-approved Local Network access.",
    }
    profile = {
        "id": "ios-device-validation",
        "platform": "ios",
        "device_id": args.device,
        "app_id": args.app,
        "xctestrun": str(args.xctestrun.resolve()),
    }
    if args.observation_host:
        from iatest.mobile.network_observation import validate_observation

        observation = {"transport": "reverse_tls", "host": args.observation_host}
        validate_observation(observation)
        profile["ios_observation"] = observation
    report["observation_transport"] = "reverse_tls" if args.observation_host else "usbmux"

    def check_wireless():
        from iatest.mobile.ios_capture import device_query
        from iatest.mobile.usbmux import list_devices

        connection = device_query(profile, RunControl(timeout_seconds=20), "details")["connectionProperties"]
        usb = any(d["device_id"] == args.device and d["connection"] == "USB" for d in list_devices())
        return not usb and connection.get("transportType") == "localNetwork" and connection.get("tunnelState") == "connected"

    session, failure = None, None
    try:
        if args.require_wireless:
            report["checks"]["wireless_before"] = check_wireless()
            if not report["checks"]["wireless_before"]:
                raise ValueError("Wireless acceptance requires unplugged USB and a connected local-network tunnel")
        session = MobileSession(profile, control=RunControl(timeout_seconds=180))
        report["checks"]["native_handshake"] = True
        report["native_capabilities"] = session.device.semantic.capabilities
        print(json.dumps({"stage": "native_handshake", "passed": True}), flush=True)
        if args.exercise_lab:
            session.action({"op": "launch"}, None, "lab-diagnostic-launch", 20)
        identifier = args.identifier or ("ready_label" if args.exercise_lab else None)
        target = {"identifier": identifier} if identifier else {"type": "any"}
        observation = session.query(target, cache=False, timeout=5, limit=1)
        if identifier:
            session.unique(observation)
        elif observation.get("complete") is not True or not observation.get(
            "candidates"
        ):
            raise ValueError("No complete foreground-App observation")
        report["checks"]["fresh_native_query"] = True
        png = session.preview()
        assert png["binary"].startswith(b"\x89PNG\r\n\x1a\n")
        report["screenshot"] = {k: png[k] for k in ("width", "height")}
        report["checks"]["native_png"] = True
        print(json.dumps({"stage": "native_query_and_png", "passed": True}), flush=True)
        if args.exercise_lab:
            crop = session.visual_region({"identifier": "gesture_pad"})
            report["local_crop"] = {k: crop[k] for k in ("width", "height", "rect")}
            report["checks"]["local_visual_region"] = True
        if args.media:
            for codec in ("h264-native", "h264"):
                started = time.monotonic()
                frame = session.preview(separate=True, codec=codec)
                assert frame["binary"]
                if codec == "h264":
                    assert frame["binary"].startswith(b"\x89PNG\r\n\x1a\n")
                else:
                    assert frame["codec"] == "h264-annexb" and frame["key_frame"]
                report["media"].append(
                    {
                        "codec": codec,
                        "first_frame_ms": round((time.monotonic() - started) * 1000, 2),
                        "bytes": len(frame["binary"]),
                        **{
                            k: frame[k]
                            for k in ("width", "height", "host_decode")
                            if k in frame
                        },
                    }
                )
                session.stop_preview(session.media_receiver.id)
                print(json.dumps({"stage": codec, "passed": True}), flush=True)
            report["checks"]["h264_first_frames"] = True
    except Exception as error:  # noqa: BLE001 -- Persist failures while always cleaning up.
        failure = error
        report["error"] = {"type": type(error).__name__, "message": str(error)}
    finally:
        if session:
            try:
                session.close()
                report["checks"]["owned_host_processes_stopped"] = all(
                    process.poll() is not None for process in session.device.processes
                )
            except Exception as error:  # noqa: BLE001 -- Teardown cannot be counted as success.
                failure = failure or error
                report["cleanup_error"] = {
                    "type": type(error).__name__,
                    "message": str(error),
                }
    if failure is None and args.exercise_lab:
        try:
            init_project(root, args.project)
            store = MobileStore(root, args.project)
            store.save_profile(profile)
            plans = lab_plans(profile["id"])
            for plan in plans:
                store.save_plan(plan)
            for index, identifier in enumerate(
                [plans[0]["id"], plans[0]["id"], plans[1]["id"]], 1
            ):
                result = execute_mobile_plan(root, args.project, identifier)
                atomic_write_text(
                    args.output.with_name(f"{args.output.stem}-run-{index}.json"),
                    json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                )
                report["replays"].append(
                    {
                        "run_id": result["run_id"],
                        "plan_id": identifier,
                        "verdict": result["verdict"],
                        "metrics": result["metrics"],
                        "steps": len(result["steps"]),
                    }
                )
                print(
                    json.dumps(
                        {
                            "stage": identifier,
                            "run": index,
                            "verdict": result["verdict"],
                        }
                    ),
                    flush=True,
                )
                assert result["verdict"] == "passed", (
                    "Lab replay failed; no automatic input retry"
                )
            names = [
                r["metrics"]["runtime_data"]["variables"]["nickname"]
                for r in report["replays"][:2]
            ]
            assert names[0] != names[1]
            report["checks"]["two_independent_random_data_replays"] = True
            report["checks"]["native_gesture_feedback"] = True
        except Exception as error:  # noqa: BLE001 -- Preserve real executor errors and raw result references.
            failure = error
            report["error"] = {"type": type(error).__name__, "message": str(error)}
    if args.require_wireless and failure is None:
        try:
            report["checks"]["wireless_after"] = check_wireless()
        except Exception as error:  # noqa: BLE001 -- Preserve failure and finish the report.
            failure = error
            report["error"] = {"type": type(error).__name__, "message": str(error)}
    report["passed"] = failure is None and all(report["checks"].values())
    atomic_write_text(
        args.output, json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "report": str(args.output),
                "checks": report["checks"],
            }
        )
    )
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
