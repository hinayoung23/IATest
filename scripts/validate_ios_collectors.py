"""Recheck original Apple collectors and explicit alternatives on the owned Lab."""

import argparse
import json
from pathlib import Path

from iatest.application.testing_service import execute_test_plan
from iatest.execution.control import RunControl
from iatest.mobile.ios_capture import XctraceRecorder, resolve_pid
from iatest.mobile.ios_xctest import run_measurement
from iatest.mobile.lease import device_scope
from iatest.mobile.runtime import MobileSession
from iatest.mobile.store import MobileStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path.cwd()
    profile = MobileStore(root, args.project).get("profile", args.profile)
    if (
        profile["platform"] != "ios"
        or profile["app_id"] != "dev.iatest.mobile.lab"
        or profile.get("simulator")
    ):
        raise ValueError("Only the framework-owned physical iPhone Lab is accepted")
    report = {"physical_device": True, "profile": args.profile}
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    with device_scope(profile):
        control = RunControl(timeout_seconds=90)
        session = MobileSession(
            profile, root=root, project=args.project, control=control
        )
        try:
            session.action(
                {"op": "launch"}, None, operation_id="collector-lab-start", timeout=30
            )
        finally:
            session.close()
        pid = resolve_pid(profile, control)
        recorder = XctraceRecorder(
            pid, control, device_id=profile["device_id"], duration_seconds=5
        )
        try:
            recorder.start()
            control.wait(1)
            report["original_attach"] = {"recorded": True, "data": recorder.finish()}
        except (OSError, ValueError, RuntimeError) as exc:
            report["original_attach"] = {
                "recorded": False,
                "error": type(exc).__name__ + ": " + str(exc),
            }
        finally:
            recorder.close()
        report["original_attach"].update(
            pid_before=pid, pid_after=resolve_pid(profile, control)
        )
        save()

    base = {
        "platform": "ios",
        "iterations": 1,
        "warmup": 0,
        "plan": {
            "id": "collector-ui",
            "kind": "ios",
            "spec": {
                "profile": args.profile,
                "steps": [
                    {
                        "op": "expect",
                        "target": {"identifier": "ready_label"},
                        "attribute": "text",
                        "expected": "Ready",
                        "timeout_ms": 10000,
                    },
                    {
                        "op": "swipe",
                        "target": {"identifier": "item_list"},
                        "direction": "up",
                    },
                    {
                        "op": "expect",
                        "target": {"identifier": "ready_label"},
                        "attribute": "text",
                        "expected": "Ready",
                        "timeout_ms": 10000,
                    },
                ],
            },
        },
    }
    report["device_processes"] = execute_test_plan(
        root,
        args.project,
        {
            "id": "collector-existing-pid",
            "kind": "client_performance",
            "budget": {"timeout_seconds": 240},
            "spec": {
                **base,
                "ios_capture": {
                    "mode": "device_processes",
                    "duration_seconds": 120,
                    "finalize_timeout_seconds": 45,
                },
                "thresholds": {
                    "max_cpu_percent": 200,
                    "max_memory_footprint_kb": 300000,
                },
            },
        },
    )
    save()
    report["legacy_scroll"] = run_measurement(
        root,
        args.project,
        {
            "profile": args.profile,
            "mode": "scroll",
            "target_identifier": "item_list",
            "origin_identifier": "item_1",
            "configuration_id": "1",
            "iterations": 2,
            "hitch_collector": "signpost",
            "bindings": {
                "hitch_ratio": {
                    "identifier": "com.apple.dt.XCTMetric_OSSignpost-Scroll_DraggingAndDeceleration.animation.hitch.time.ratio",
                    "unit": "ms per s",
                }
            },
        },
        RunControl(timeout_seconds=240),
    )
    save()
    report["frame_intervals"] = execute_test_plan(
        root,
        args.project,
        {
            "id": "collector-frame-intervals",
            "kind": "client_performance",
            "budget": {"timeout_seconds": 240},
            "spec": {
                **base,
                "ios_capture": {
                    "mode": "launch",
                    "template": "animation_hitches",
                    "duration_seconds": 120,
                    "finalize_timeout_seconds": 45,
                },
                "thresholds": {"max_frame_lifetime_ms": 5000},
            },
        },
    )
    save()
    print(
        json.dumps(
            {
                "output": str(args.output),
                "attach_recorded": report["original_attach"]["recorded"],
                "device_processes": report["device_processes"]["verdict"],
                "frame_intervals": report["frame_intervals"]["verdict"],
            }
        )
    )
    return (
        0
        if all(
            report[key]["verdict"] == "passed"
            for key in ("device_processes", "frame_intervals")
        )
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
