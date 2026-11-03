"""Compare original and byte-preserving attach on the framework-owned iPhone App."""

import argparse
import hashlib
import json
from pathlib import Path

from iatest.application.testing_service import execute_test_plan
from iatest.execution.control import RunControl
from iatest.mobile.devices import command
from iatest.mobile.ios_capture import XctraceRecorder, resolve_pid
from iatest.mobile.lease import device_scope
from iatest.mobile.runtime import MobileSession
from iatest.mobile.store import MobileStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixed-window-seconds", type=int, default=0)
    parser.add_argument("--max-trace-bytes", type=int, default=268435456)
    parser.add_argument("--frame-template", choices=("animation_hitches", "frame_lifetimes"), default="animation_hitches")
    args = parser.parse_args()
    if args.fixed_window_seconds and not 10 <= args.fixed_window_seconds <= 120:
        parser.error("Fixed acceptance windows must be 10–120 seconds")
    if not 1024 <= args.max_trace_bytes <= 536870912:
        parser.error("Trace capacity must be 1024–536870912 bytes")
    root = Path.cwd()
    profile = MobileStore(root, args.project).get("profile", args.profile)
    if (
        profile["platform"] != "ios"
        or profile["app_id"] != "dev.iatest.mobile.lab"
        or profile.get("simulator")
    ):
        raise ValueError("Only the framework-owned physical iPhone Lab is accepted")
    control = RunControl(timeout_seconds=600)
    source = Path(command(["xcrun", "--find", "xctrace"]).strip()).resolve()
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    report = {"physical_device": True, "source_sha256_before": digest}
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    def original_probe():
        pid = resolve_pid(profile, control)
        recorder = XctraceRecorder(
            pid,
            control,
            device_id=profile["device_id"],
            duration_seconds=30,
        )
        try:
            recorder.start()
            control.wait(1)
            value = {"recorded": True, "data": recorder.finish()}
        except (OSError, ValueError, RuntimeError) as exc:
            value = {"recorded": False, "error": str(exc)}
        finally:
            recorder.close()
        value.update(pid_before=pid, pid_after=resolve_pid(profile, control))
        return value

    # Preparation is outside all recordings. Subsequent measured journeys cannot
    # launch or terminate the target process.
    with device_scope(profile):
        session = MobileSession(
            profile, root=root, project=args.project, control=control
        )
        try:
            session.action(
                {"op": "launch"}, None, operation_id="prepare-attach-lab", timeout=30
            )
        finally:
            session.close()
        report["measurement_pid"] = resolve_pid(profile, control)
        report["original_before"] = original_probe()
        save()
    templates = ("activity_monitor", args.frame_template)
    for template in templates:
        pid_before = resolve_pid(profile, control)
        result = execute_test_plan(
            root,
            args.project,
            {
                "id": "isolated-attach-" + template,
                "kind": "client_performance",
                "budget": {"timeout_seconds": 240},
                "spec": {
                    "platform": "ios",
                    "iterations": 1,
                    "warmup": 0,
                    "ios_capture": {
                        "mode": "attach",
                        "tool_launch": "isolated_copy",
                        "target_pid": report["measurement_pid"],
                        "template": template,
                        "duration_seconds": args.fixed_window_seconds or 120,
                        "stop_strategy": "time_limit"
                        if args.fixed_window_seconds
                        else "interrupt",
                        "ready_timeout_seconds": 40,
                        "finalize_timeout_seconds": 45,
                        "max_trace_bytes": args.max_trace_bytes,
                    },
                    "thresholds": (
                        {"max_cpu_percent": 200, "max_memory_footprint_kb": 300000}
                        if template == "activity_monitor"
                        else {"max_frame_lifetime_ms": 5000}
                    ),
                    "plan": {
                        "id": "attach-ui",
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
                },
            },
        )
        report[template] = {
            "pid_before": pid_before,
            "pid_after": resolve_pid(profile, control),
            "result": result,
        }
        save()
    with device_scope(profile):
        report["original_after"] = original_probe()
    report["source_sha256_after"] = hashlib.sha256(source.read_bytes()).hexdigest()
    report["source_unchanged"] = report["source_sha256_after"] == digest
    report["passed"] = report["source_unchanged"] and all(
        report[key]["result"]["verdict"] == "passed"
        and report[key]["pid_before"]
        == report[key]["pid_after"]
        == report["measurement_pid"]
        for key in templates
    )
    save()
    print(json.dumps({"output": str(args.output), "passed": report["passed"]}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
