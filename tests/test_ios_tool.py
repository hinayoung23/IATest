import hashlib
import os
from pathlib import Path

import pytest

from iatest.execution.control import RunControl
from iatest.mobile import ios_capture, ios_tool
from iatest.mobile.ios_capture import XctraceRecorder


def test_unsupported_recording_host_releases_temporary_resources(monkeypatch):
    monkeypatch.setattr(ios_capture.sys, "platform", "win32")
    recorder = XctraceRecorder(777)
    with pytest.raises(ValueError, match="macOS/Xcode"):
        recorder.start()
    assert recorder.log.closed and not recorder.directory.exists()


@pytest.fixture
def installed(tmp_path):
    contents = tmp_path / "Xcode.app/Contents"
    source = contents / "Developer/usr/bin/xctrace"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"signed-tool-fixture")
    (contents / "Applications/Instruments.app/Contents/Frameworks").mkdir(parents=True)
    (contents / "SharedFrameworks").mkdir()
    blank = contents / "Applications/Instruments.app/Contents/Packages/Base.instrdst/Contents/Templates/Blank.tracetemplate"
    blank.parent.mkdir(parents=True)
    blank.write_bytes(b"installed-empty-template-fixture")
    work = tmp_path / "recorder"
    work.mkdir()
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return str(source) if argv[:2] == ["xcrun", "--find"] else ""

    return source, work, runner, calls


def test_copy_is_unchanged_verified_and_scopes_loader_environment(
    installed, monkeypatch
):
    source, work, runner, calls = installed
    monkeypatch.setenv("DYLD_INSERT_LIBRARIES", "/foreign.dylib")
    monkeypatch.setenv("DYLD_FRAMEWORK_PATH", "/foreign-frameworks")
    argv, environment, metadata = ios_tool.prepare_isolated_xctrace(
        work, RunControl(), runner=runner
    )
    copied = Path(argv[0])
    assert copied.read_bytes() == source.read_bytes()
    if os.name == "posix":
        assert copied.stat().st_mode & 0o777 == 0o500
    else:
        assert not copied.stat().st_mode & 0o200  # Windows has no POSIX execute bit.
    assert metadata["tool_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert metadata["tool_signature_verified"] and metadata["tool_copy_byte_identical"]
    assert "DYLD_INSERT_LIBRARIES" not in environment
    assert "/foreign" not in environment["DYLD_FRAMEWORK_PATH"]
    assert os.environ["DYLD_INSERT_LIBRARIES"] == "/foreign.dylib"
    assert calls[1:] == [
        ["/usr/bin/codesign", "--verify", "--strict", str(source)],
        ["/usr/bin/codesign", "--verify", "--strict", str(copied)],
    ]


@pytest.mark.parametrize("failure", ["signature", "changed", "cancelled"])
def test_invalid_or_interrupted_copy_cannot_execute_and_is_cleaned(installed, failure):
    source, work, original_runner, _calls = installed
    control = RunControl()

    def runner(argv, **kwargs):
        value = original_runner(argv, **kwargs)
        if (
            argv[0] == "/usr/bin/codesign"
            and argv[-1] != str(source)
            and failure == "signature"
        ):
            raise RuntimeError("signature verification rejected")
        if (
            argv[0] == "/usr/bin/codesign"
            and argv[-1] == str(source)
            and failure == "cancelled"
        ):
            control.cancelled.set()
        return value

    original_digest = ios_tool._digest

    def digest(path, task_control):
        value = original_digest(path, task_control)
        return "changed" if failure == "changed" and path.parent == work else value

    from unittest.mock import patch

    with (
        patch.object(ios_tool, "_digest", digest),
        pytest.raises((ValueError, RuntimeError)),
    ):
        ios_tool.prepare_isolated_xctrace(work, control, runner=runner)
    assert not list(work.iterdir())


def test_failed_preparation_cleans_recorder_and_never_starts_process(monkeypatch):
    monkeypatch.setattr(ios_capture.sys, "platform", "darwin")

    def failure(directory, control):
        (directory / "partial-copy").write_bytes(b"incomplete")
        raise ValueError("tool unavailable")

    monkeypatch.setattr(ios_tool, "prepare_isolated_xctrace", failure)
    recorder = XctraceRecorder(
        777,
        tool_launch="isolated_copy",
        process_factory=lambda *a, **k: pytest.fail("Must fail before recording/input"),
    )
    with pytest.raises(ValueError, match="tool unavailable"):
        recorder.start()
    assert not recorder.directory.exists()


def test_standard_launch_never_prepares_a_copy(monkeypatch):
    monkeypatch.setattr(
        ios_tool,
        "prepare_isolated_xctrace",
        lambda *a: pytest.fail("Implicit fallback"),
    )
    recorder = XctraceRecorder(777)
    try:
        assert recorder.argv()[:3] == ["xcrun", "xctrace", "record"]
    finally:
        recorder.close()


@pytest.mark.parametrize("outcome", ["finished", "timeout", "no_ack"])
def test_ios_runner_close_waits_for_xctest_teardown_before_termination(outcome):
    import subprocess

    from iatest.mobile.devices import NativeDevice

    order = []

    class Channel:
        closed = False

        def request(self, *args, **kwargs):
            order.append("close_ack")
            return {"status": "received" if outcome == "no_ack" else "closed"}

        def close(self):
            self.closed = True

    class Process:
        args = ("xcodebuild", "test-without-building")
        returncode = None

        def poll(self):
            return self.returncode

        def wait(self, *, timeout):
            order.append(("wait", timeout))
            if outcome == "timeout" and timeout == 5:
                raise subprocess.TimeoutExpired(self.args, timeout)
            self.returncode = 0
            return 0

        def terminate(self):
            order.append("terminate")
            self.returncode = -15

    device = NativeDevice(
        {
            "id": "fixture",
            "platform": "ios",
            "device_id": "fixture",
            "app_id": "com.example.app",
        },
        "fixture-session",
    )
    device.input = device.semantic = Channel()
    device.processes = [Process()]
    device.close()
    assert order[0] == "close_ack"
    if outcome == "finished":
        assert order == ["close_ack", ("wait", 5)]
    elif outcome == "timeout":
        assert order == ["close_ack", ("wait", 5), "terminate", ("wait", 3)]
    else:
        assert order == ["close_ack", "terminate", ("wait", 3)]


@pytest.mark.parametrize("template", ["activity_monitor", "frame_lifetimes"])
def test_successful_attach_retains_tool_provenance_and_cleans_copy(
    installed, monkeypatch, template
):
    source, _work, runner, _calls = installed
    monkeypatch.setattr(ios_capture.sys, "platform", "darwin")
    monkeypatch.setattr(ios_tool, "command", runner)
    monkeypatch.setattr(
        ios_capture, "read_trace", lambda *a, **k: {
            "schema": "xctrace-frame-lifetimes/1",
            "metadata": {"target_pid": 777, "template": "Blank"}
        }
    )
    children = []

    class Process:
        returncode = None

        def __init__(self, argv, **kwargs):
            children.append(argv)
            assert Path(argv[0]).read_bytes() == source.read_bytes()
            assert argv[-2:] == ["--attach", "777"]
            assert "--launch" not in argv and "--all-processes" not in argv
            if template == "frame_lifetimes":
                assert argv[argv.index("--template") + 1].endswith("Blank.tracetemplate")
                assert [argv[i+1] for i, value in enumerate(argv) if value == "--instrument"] == ["Hitches", "Frame Lifetimes"]
                assert "Time Profiler" not in argv and "Hangs" not in argv
            kwargs["stdout"].write(b"Ctrl-C to stop the recording\n")
            kwargs["stdout"].flush()

        def poll(self):
            return self.returncode

        def send_signal(self, signal):
            self.returncode = 0

        def wait(self, **kwargs):
            return self.returncode

    recorder = XctraceRecorder(
        777, tool_launch="isolated_copy", template=template, process_factory=Process
    )
    try:
        recorder.start()
        parsed = recorder.finish()
        assert parsed["metadata"]["tool_launch"] == "isolated_copy"
        assert parsed["metadata"]["target_pid"] == 777
        assert parsed["metadata"]["tool_copy_byte_identical"] is True
        if template == "frame_lifetimes":
            assert parsed["metadata"]["capture_template"] == "frame_lifetimes"
            assert parsed["metadata"]["frame_capture_instruments"] == ["Hitches", "Frame Lifetimes"]
            assert len(parsed["metadata"]["template_sha256"]) == 64
    finally:
        recorder.close()
    assert len(children) == 1 and not Path(children[0][0]).exists()


@pytest.mark.parametrize("problem", ["missing", "oversized", "wrong_tables"])
def test_frame_template_unavailable_or_wrong_output_cannot_pass(installed, monkeypatch, problem):
    monkeypatch.setattr(ios_capture.sys, "platform", "darwin")
    _source, _work, runner, _calls = installed
    monkeypatch.setattr(ios_tool, "command", runner)
    template, _developer, _metadata = ios_tool.prepare_frame_template(RunControl())
    if problem in {"missing", "oversized"}:
        if problem == "missing":
            template.unlink()
        else:
            template.write_bytes(b"x" * 1048577)
        recorder = XctraceRecorder(
            777, template="frame_lifetimes", process_factory=lambda *a, **k: pytest.fail("Must reject before input")
        )
        with pytest.raises(ValueError, match="frame template"):
            recorder.start()
        assert not recorder.directory.exists()
    else:
        monkeypatch.setattr(ios_capture, "read_trace", lambda *a, **k: {"schema": "sysmon-process", "metadata": {"template": "Blank"}})
        recorder = XctraceRecorder(777, template="frame_lifetimes")
        monkeypatch.setattr(recorder, "stop", lambda: None)
        try:
            with pytest.raises(ios_capture.CaptureFailure, match="frame_tables_unavailable"):
                recorder.finish()
        finally:
            recorder.close()


@pytest.mark.parametrize(
    "outcome", ["natural_end", "cancelled", "premature", "deadline"]
)
def test_fixed_window_completion_requires_full_ui_and_bounds_cancellation(
    monkeypatch, outcome
):
    from iatest.execution.control import RunCancelled

    monkeypatch.setattr(ios_capture.sys, "platform", "darwin")
    monkeypatch.setattr(
        ios_capture, "read_trace", lambda *a, **k: {"metadata": {"target_pid": 777}}
    )
    signals = []

    class Process:
        returncode = None

        def __init__(self, argv, **kwargs):
            kwargs["stdout"].write(b"Ctrl-C to stop the recording\n")
            kwargs["stdout"].flush()

        def poll(self):
            return self.returncode

        def send_signal(self, signal):
            signals.append(signal)
            self.returncode = 0

        def wait(self, **kwargs):
            return self.returncode

    control = RunControl(timeout_seconds=2)
    recorder = XctraceRecorder(
        777, control, stop_strategy="time_limit", process_factory=Process
    )

    def wait(_control, seconds):
        if recorder.finishing:
            if outcome == "cancelled":
                control.cancelled.set()
                control.check()
            recorder.process.returncode = 0

    monkeypatch.setattr(RunControl, "wait", wait)
    try:
        recorder.start()
        if outcome == "premature":
            recorder.process.returncode = 0
        if outcome == "deadline":
            recorder.ready_at -= (
                recorder.duration_seconds + recorder.finalize_timeout_seconds + 1
            )
        if outcome == "natural_end":
            assert recorder.finish()["metadata"]["stop_strategy"] == "time_limit"
            assert signals == []
        elif outcome == "cancelled":
            with pytest.raises(RunCancelled):
                recorder.finish()
            assert signals == [ios_capture.signal.SIGINT]
        elif outcome == "deadline":
            with pytest.raises(
                ios_capture.CaptureFailure, match="finalization_timeout"
            ):
                recorder.finish()
            assert signals == [ios_capture.signal.SIGINT]
        else:
            with pytest.raises(ValueError, match="ended before"):
                recorder.finish()
    finally:
        recorder.close()
    assert not recorder.directory.exists()


@pytest.mark.parametrize("outcome", ["accepted", "unknown", "changed_pid"])
def test_isolated_attach_activates_verified_pid_before_recording(monkeypatch, outcome):
    from types import SimpleNamespace

    profile = {"platform": "ios", "device_id": "fixture", "app_id": "com.example.app"}
    capture = ios_capture.IOSCapture(
        None,
        None,
        profile,
        RunControl(),
        {
            "mode": "attach",
            "tool_launch": "isolated_copy",
        },
    )
    calls = []
    monkeypatch.setattr(ios_capture, "resolve_pid", lambda *a, **k: 777)
    monkeypatch.setattr(capture, "before", lambda: calls.append("record"))

    def verify(*args):
        calls.append(("verify", args[1]))
        if outcome == "changed_pid" and "activate" in calls:
            raise ValueError("PID changed during activation")

    def activate(*args, **kwargs):
        calls.append("activate")
        return {"status": "unknown"} if outcome == "unknown" else {"accepted": True}

    monkeypatch.setattr(ios_capture, "verify_pid", verify)
    session = SimpleNamespace(
        profile=profile,
        device=SimpleNamespace(
            input=SimpleNamespace(
                capabilities={"actions": ["activate_running"]},
                request=activate,
            )
        ),
    )
    if outcome == "accepted":
        capture.begin_ui_session(session)
        assert calls == [("verify", 777), "activate", ("verify", 777), "record"]
    else:
        with pytest.raises(ValueError):
            capture.begin_ui_session(session)
        assert "record" not in calls and calls.count("activate") == 1
