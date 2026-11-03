from types import SimpleNamespace

import pytest

from iatest.engines.contracts import EngineContext, ExecutionPlan
from iatest.engines.registry import create_engine_registry
from iatest.execution.control import RunCancelled, RunControl
from iatest.mobile import ios_capture, runtime
from iatest.mobile.store import MobileStore


@pytest.mark.parametrize("budget", [1, 30])
@pytest.mark.parametrize(
    "stage", ["before_export", "during_export", "after_export", "deadline"]
)
def test_export_obeys_parent_cancellation_and_remaining_budget(
    monkeypatch, stage, budget
):
    control = RunControl(timeout_seconds=budget)
    recorder = ios_capture.XctraceRecorder(777, control)
    exported = []

    class Process:
        returncode = None

        def poll(self):
            return self.returncode

        def send_signal(self, _signal):
            self.returncode = 0
            if stage == "before_export":
                control.cancelled.set()
            elif stage == "deadline":
                control.started -= 31

        def wait(self, **_kwargs):
            return self.returncode

    def export(_path, *, control: RunControl, **_kwargs):
        exported.append(True)
        assert control.timeout_seconds <= min(20, parent.timeout_seconds)
        parent.cancelled.set()
        if stage != "after_export":
            control.check()
        return {"metadata": {}}

    parent = control
    recorder.process = Process()
    monkeypatch.setattr(ios_capture, "read_trace", export)
    try:
        with pytest.raises(RunCancelled):
            recorder.finish()
        assert bool(exported) == (stage in {"during_export", "after_export"})
    finally:
        recorder.close()
    assert not recorder.directory.exists()


@pytest.mark.parametrize("exit_at", ["interrupt", "terminate", "kill"])
def test_recorder_exit_between_poll_and_signal_still_reaps_and_cleans(exit_at):
    import subprocess

    recorder = ios_capture.XctraceRecorder(777)

    class Process:
        returncode = None
        waited = False

        def poll(self):
            return self.returncode

        def send_signal(self, _signal):
            if exit_at == "interrupt":
                self.returncode = 0
                raise ProcessLookupError("Recorder exited concurrently")

        def terminate(self):
            if exit_at == "terminate":
                self.returncode = 0
                raise ProcessLookupError("Recorder exited concurrently")

        def kill(self):
            self.returncode = 0
            raise ProcessLookupError("Recorder exited concurrently")

        def wait(self, **_kwargs):
            self.waited = True
            if self.returncode is None:
                raise subprocess.TimeoutExpired("owned-recorder", 1)
            return self.returncode

    recorder.process = process = Process()
    try:
        recorder.close()
        assert process.waited
        assert recorder.log.closed and not recorder.directory.exists()
    finally:
        recorder.process = None
        recorder.close()


def test_trace_budget_failure_survives_watchdog_cancellation(tmp_path):
    trace = tmp_path / "capture.trace"
    trace.mkdir()
    (trace / "samples").write_bytes(b"oversized")
    with pytest.raises(ios_capture.CaptureFailure) as failure:
        ios_capture.tree_bytes(trace, 1)
    assert failure.value.code == "trace_budget_exceeded"
    control = RunControl()
    control.cancelled.set()
    capture = ios_capture.IOSCapture(None, None, {}, control, {})
    capture.ui_session_started = True
    capture.recorder = SimpleNamespace(
        failure=str(failure.value), capture_failure=failure.value
    )
    with pytest.raises(ios_capture.CaptureFailure, match="trace_budget_exceeded"):
        capture.finish_ui_session()
    assert not capture.ui_session_started


@pytest.mark.parametrize("mode", ["launch", "attach"])
def test_recording_may_finish_its_owned_launch_but_must_preserve_attached_pid(
    tmp_path, monkeypatch, mode
):
    from pathlib import Path

    from iatest.engines.store import EngineStore
    from iatest.mobile.ios_metrics import parse_process_xml

    profile = {
        "platform": "ios",
        "device_id": "fixture-phone",
        "app_id": "com.example.app",
    }
    capture = ios_capture.IOSCapture(
        tmp_path, "suite", profile, RunControl(), {"mode": mode}
    )
    capture.spec["target_pid"] = 777
    capture.launched_pid = 777
    capture.cleanup_status = "pending" if mode == "launch" else "not_owned"
    finished = []

    def verify(*_args, **_kwargs):
        if finished:
            raise ValueError("Measured PID is unavailable or ambiguous")
        return 777

    def finish():
        finished.append(True)
        parsed = parse_process_xml(
            (Path(__file__).parent / "fixtures/ios/process-xcode27.xml").read_bytes(),
            777,
        )
        return {
            **parsed,
            "metadata": {
                "platform": "iOS",
                "device_id": "fixture-phone",
                "target_pid": 777,
            },
        }

    capture.recorder = SimpleNamespace(finish=finish)
    monkeypatch.setattr(ios_capture, "verify_pid", verify)
    monkeypatch.setattr(ios_capture, "resolve_pid", lambda *_args, **_kwargs: None)
    if mode == "launch":
        capture.after()
        assert capture.cleanup_status == "stopped"
        assert capture.values["cpu_percent"] >= 0
        assert len(EngineStore(tmp_path, "suite").artifacts("ios_measurement")) == 1
    else:
        with pytest.raises(ValueError, match="unavailable"):
            capture.after()
        assert not EngineStore(tmp_path, "suite").artifacts("ios_measurement")


@pytest.mark.parametrize("failure", ["finalization", "cancelled", "deadline"])
def test_mobile_finalization_failure_reaches_parent_without_a_measurement(
    tmp_path, monkeypatch, failure
):
    profile = {
        "id": "phone",
        "platform": "ios",
        "device_id": "fixture-lifecycle-phone",
        "app_id": "com.example.app",
    }
    MobileStore(tmp_path, "suite").save_profile(profile)
    closed = []

    class Session:
        query_timeout = 5
        native_snapshot_attempts = 0

        def __init__(self, value, **_kwargs):
            self.profile = value
            channel = SimpleNamespace(capabilities={"actions": ["query"]})
            self.device = SimpleNamespace(input=channel, semantic=channel)

        def query(self, *_args, **_kwargs):
            return {
                "complete": True,
                "match_count": 1,
                "candidates": [{"text": "Ready"}],
            }

        @staticmethod
        def unique(observation):
            return observation["candidates"][0]

        def close(self):
            closed.append("session")

    class Recorder:
        def __init__(self, *, control, **_kwargs):
            self.control = control

        def start(self):
            pass

        def finish(self):
            if failure == "finalization":
                raise ios_capture.CaptureFailure(
                    "finalization_timeout",
                    "finalization",
                    "Owned recorder did not finalize",
                )
            if failure == "cancelled":
                self.control.cancelled.set()
            else:
                self.control.started -= 31
            self.control.check()

        def close(self):
            closed.append("recorder")

    monkeypatch.setattr(runtime, "MobileSession", Session)
    monkeypatch.setattr(ios_capture, "XctraceRecorder", Recorder)
    monkeypatch.setattr(ios_capture, "verify_pid", lambda *_args, **_kwargs: 777)
    plan = ExecutionPlan.from_dict(
        {
            "id": "capture-lifecycle",
            "kind": "client_performance",
            "spec": {
                "platform": "ios",
                "iterations": 1,
                "ios_capture": {"target_pid": 777},
                "thresholds": {"max_cpu_percent": 100},
                "plan": {
                    "id": "ready",
                    "kind": "ios",
                    "spec": {
                        "profile": "phone",
                        "steps": [
                            {
                                "op": "expect",
                                "target": {"identifier": "ready"},
                                "attribute": "text",
                                "expected": "Ready",
                            }
                        ],
                    },
                },
            },
        }
    )
    result = create_engine_registry(tmp_path, "suite").execute(
        plan, EngineContext(RunControl(timeout_seconds=30))
    )
    assert result.verdict == "inconclusive"
    assert result.metrics["not_assessed"] == ["cpu_percent"]
    assert "session" in closed and "recorder" in closed
    from iatest.engines.store import EngineStore

    assert not EngineStore(tmp_path, "suite").artifacts("ios_measurement")
    if failure == "finalization":
        assert result.metrics["capture_diagnostics"] == {
            "code": "finalization_timeout",
            "phase": "finalization",
            "iteration": 0,
        }
    else:
        assert result.lifecycle == (
            "cancelled" if failure == "cancelled" else "timed_out"
        )
