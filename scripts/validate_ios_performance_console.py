"""Real Chromium → console → signed iPhone Lab collector calibration and gates."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import threading
import time
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

from iatest.core.config import init_project
from iatest.engines.store import EngineStore
from iatest.execution.control import RunControl
from iatest.mobile.ios_capture import resolve_pid
from iatest.mobile.runtime import MobileSession
from iatest.mobile.store import MobileStore
from iatest.web.server import make_handler
from iatest.web.task_store import list_tasks, request_cancel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--project", required=True)
    parser.add_argument("--profile", default="ios-device-validation")
    parser.add_argument("--configuration-id", default="")
    parser.add_argument(
        "--isolated-attach",
        action="store_true",
        help="Record frames through an unchanged signed tool copy, retaining the App PID",
    )
    parser.add_argument(
        "--record-frames",
        action="store_true",
        help="Run actual UI + raw frame recording instead of collector calibration",
    )
    parser.add_argument(
        "--calibration-id",
        default="",
        help="Reuse a real immutable control from the source project instead of injecting again",
    )
    parser.add_argument("--browser", type=Path, required=True)
    parser.add_argument("--node-modules", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixed-window-seconds", type=int, default=0)
    parser.add_argument("--max-trace-bytes", type=int, default=268435456)
    parser.add_argument("--frame-template", choices=("animation_hitches", "frame_lifetimes"), default="animation_hitches")
    args = parser.parse_args()
    if args.fixed_window_seconds and (
        not args.record_frames or not 10 <= args.fixed_window_seconds <= 120
    ):
        parser.error("Fixed frame recording windows must be 10–120 seconds")
    if not 1024 <= args.max_trace_bytes <= 536870912:
        parser.error("Trace capacity must be 1024–536870912 bytes")
    if (
        args.isolated_attach
        and not args.record_frames
        or args.record_frames
        and args.calibration_id
        or not args.record_frames
        and not args.configuration_id
    ):
        parser.error(
            "Choose frame recording without calibration-id, or supply a calibration configuration-id"
        )
    root = args.root.resolve()
    profile = MobileStore(root, args.project).get("profile", args.profile)
    if (
        profile["platform"] != "ios"
        or profile["app_id"] != "dev.iatest.mobile.lab"
        or profile.get("simulator")
    ):
        parser.error("Only the framework-owned Lab on a physical iPhone is accepted")
    project = "ios-perf-console-" + uuid.uuid4().hex[:12]
    init_project(root, project)
    MobileStore(root, project).save_profile(profile)
    if args.calibration_id:
        calibration = EngineStore(root, args.project).get_artifact(
            "ios_calibration", args.calibration_id
        )
        if calibration["status"] != "usable":
            parser.error("The explicitly reused calibration must be usable")
        EngineStore(root, project).save_artifact(
            "ios_calibration", calibration, artifact_id=calibration["id"]
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    spec = {
        "profile": args.profile,
        "mode": "scroll",
        "target_identifier": "item_list",
        "origin_identifier": "item_1",
        "configuration_id": args.configuration_id,
        "iterations": 2,
        "hitch_collector": "signpost",
        "bindings": {
            "hitch_ratio": {
                "identifier": "com.apple.dt.XCTMetric_OSSignpost-Scroll_DraggingAndDeceleration.animation.hitch.time.ratio",
                "unit": "ms per s",
            }
        },
    }
    if args.record_frames:
        spec = {
            "id": "ios-console-frame-journey",
            "kind": "client_performance",
            "budget": {"timeout_seconds": 240},
            "spec": {
                "platform": "ios",
                "iterations": 1,
                "warmup": 0,
                "ios_capture": {
                    "mode": "launch",
                    "template": args.frame_template,
                    "duration_seconds": 120,
                    "finalize_timeout_seconds": 45,
                    "max_trace_bytes": args.max_trace_bytes,
                },
                "thresholds": {"max_frame_lifetime_ms": 5000},
                "plan": {
                    "id": "ios-console-scroll",
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
        }
        if args.isolated_attach:
            session = MobileSession(
                profile,
                root=root,
                project=project,
                control=RunControl(timeout_seconds=90),
            )
            try:
                session.action(
                    {"op": "launch"},
                    None,
                    operation_id="prepare-console-isolated",
                    timeout=30,
                )
            finally:
                session.close()
            measured_pid = resolve_pid(profile, RunControl(timeout_seconds=20))
            spec["spec"]["ios_capture"].update(
                mode="attach",
                tool_launch="isolated_copy",
                target_pid=measured_pid,
                stop_strategy="time_limit",
                duration_seconds=30,
            )
        if args.fixed_window_seconds:
            spec["spec"]["ios_capture"].update(
                stop_strategy="time_limit", duration_seconds=args.fixed_window_seconds
            )
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(root, project))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    from iatest.mobile import ios_capture

    original_stop = ios_capture.XctraceRecorder.stop

    def diagnostic_stop(recorder):
        before = time.monotonic()
        reason = recorder.control.reason()
        try:
            return original_stop(recorder)
        finally:
            log = recorder.directory / "recorder.log"
            if args.record_frames and log.exists():
                evidence = args.output.parent / (
                    args.output.stem
                    + "-"
                    + recorder.directory.name
                    + "-"
                    + str(time.monotonic_ns())
                )
                with log.open("rb") as stream:
                    evidence.with_suffix(".log").write_bytes(stream.read(65536))
                evidence.with_suffix(".json").write_text(
                    json.dumps(
                        {
                            "stop_seconds": time.monotonic() - before,
                            "control_reason": reason,
                            "returncode": recorder.process.returncode
                            if recorder.process
                            else None,
                            "failure": recorder.failure,
                            "sigint_ignored": signal.getsignal(signal.SIGINT)
                            == signal.SIG_IGN,
                            "sigint_blocked": signal.SIGINT
                            in signal.pthread_sigmask(signal.SIG_BLOCK, []),
                            "tool": recorder.launch_metadata,
                        },
                        indent=2,
                    )
                )

    ios_capture.XctraceRecorder.stop = diagnostic_stop
    try:
        subprocess.run(
            ["node", str(Path(__file__).with_suffix(".cjs"))],
            env={
                **os.environ,
                "NODE_PATH": str(args.node_modules),
                "IATEST_SMOKE_BROWSER": str(args.browser),
                "IATEST_SMOKE_URL": f"http://127.0.0.1:{server.server_port}",
                "IATEST_PERF_PROJECT": project,
                "IATEST_PERF_SPEC": json.dumps(spec),
                "IATEST_PERF_REPORT": str(args.output.resolve()),
                "IATEST_PERF_CALIBRATION": args.calibration_id,
                "IATEST_PERF_FRAMES": "1" if args.record_frames else "0",
                "IATEST_PERF_ISOLATED": "1" if args.isolated_attach else "0",
            },
            check=True,
            timeout=540,
        )
        store = EngineStore(root, project)
        report = json.loads(args.output.read_text())
        if args.record_frames:
            run = next(r for r in store.runs() if r["plan_id"] == spec["id"])
            assert run["verdict"] == "passed" and run["metrics"]["fresh_ui_execution"]
            measurement = store.get_artifact(
                "ios_measurement", run["metrics"]["repetitions"][0]["measurement_id"]
            )
            assert measurement["collector_observation"]["matched_frame_intervals"] > 0
            report.update(measurement=measurement, live_run=run)
            for plan_id, verdict in (
                ("ios-console-frame-gate", "passed"),
                ("ios-console-frame-reject", "failed"),
            ):
                gate = next(r for r in store.runs() if r["plan_id"] == plan_id)
                assert gate["verdict"] == verdict and not gate["metrics"]["fresh_ui_execution"]
                assert gate["metrics"]["measurement_id"] == measurement["id"]
                assert gate["metrics"]["source_digest"] == measurement["source_digest"]
                report[plan_id] = gate
            if args.isolated_attach:
                after_pid = resolve_pid(profile, RunControl(timeout_seconds=20))
                assert measured_pid == after_pid == measurement["source"]["target_pid"]
                assert measurement["source"]["tool_copy_byte_identical"]
                report.update(pid_before=measured_pid, pid_after=after_pid)
        else:
            calibration = store.artifacts("ios_calibration")[0]
            measurement = next(
                a
                for a in store.artifacts("ios_measurement")
                if a["source"].get("calibration_id") == calibration["id"]
            )
            run = next(
                r for r in store.runs() if r["plan_id"] == "ios-console-hitch-gate"
            )
            assert calibration["status"] == "usable" and run["verdict"] == "passed"
            report.update(calibration=calibration, measurement=measurement, gate=run)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        args.output.chmod(0o600)
    except BaseException as exc:
        report = json.loads(args.output.read_text()) if args.output.exists() else {}
        report.update(
            passed=False, validation_error=type(exc).__name__ + ": " + str(exc)
        )
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        raise
    finally:
        deadline = time.monotonic() + 30
        while True:
            active = [
                task
                for task in list_tasks(root, project)
                if task["status"] in {"queued", "running"}
            ]
            for task in active:
                request_cancel(root, project, task["id"])
            if not active or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        server.shutdown()
        server.server_close()
        ios_capture.XctraceRecorder.stop = original_stop
        thread.join(timeout=2)
        if active:
            raise RuntimeError("Console-owned device task cleanup remains unconfirmed")


if __name__ == "__main__":
    main()
