import copy
import json
import signal
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from iatest.application.ios_measurements import import_measurement
from iatest.application.testing_service import execute_test_plan
from iatest.core.config import init_project
from iatest.engines.contracts import EngineContext, EngineResult, ExecutionPlan
from iatest.engines.registry import create_engine_registry
from iatest.engines.store import EngineStore
from iatest.execution.control import RunControl
from iatest.mobile import ios_capture
from iatest.mobile.ios_capture import XctraceRecorder, verify_pid
from iatest.mobile.ios_metrics import (
    parse_measurements,
    parse_process_xml,
    toc_metadata,
)
from iatest.mobile.store import MobileStore

FIXTURE = Path(__file__).parent / "fixtures/ios/process-xcode27.xml"


@pytest.fixture
def root(tmp_path):
    init_project(tmp_path, "suite")
    return tmp_path


def interchange():
    return {
        "contract_version": 1,
        "platform": "ios",
        "app_id": "com.example.app",
        "source": {
            "provider": "xctest",
            "device_id": "fixture-phone",
            "build": "v2",
            "test_id": "testLaunch",
        },
        "measurements": [
            {"metric": "ios_launch_ms", "unit": "ms", "values": [100, 120, 110]},
            {"metric": "memory_footprint_kb", "unit": "KiB", "values": [1024, 2048]},
        ],
    }


def evaluation(artifact, **thresholds):
    return {
        "id": "offline-ios",
        "kind": "client_performance",
        "spec": {
            "platform": "ios",
            "measurement": {"artifact_id": artifact["id"]},
            "thresholds": thresholds or {"max_ios_launch_ms": 200},
        },
    }


def test_actual_host_schema_resolves_ids_and_uses_raw_engineering_units():
    result = parse_process_xml(FIXTURE.read_bytes(), 777)
    assert result["schema"] == "sysmon-process"
    assert len(result["samples"]) == 2
    assert result["samples"][0]["memory_footprint_kb"] == 10093072 / 1024
    assert result["samples"][1]["resident_memory_kb"] == 18415616 / 1024
    # Raw 0.005367029 is a percentage, despite rounded display text 0.0%.
    assert result["samples"][1]["cpu_percent"] == 0.005367029
    assert "cpu_percent" not in result["samples"][0]
    assert parse_process_xml(FIXTURE.read_bytes(), 888)["samples"] == []


@pytest.mark.parametrize(
    "change", ["cycle", "unknown_reference", "wrong_type", "duplicate_id", "doctype"]
)
def test_bad_xml_and_references_fail_closed(change):
    root = ET.fromstring(FIXTURE.read_bytes())
    if change == "doctype":
        content = b'<!DOCTYPE x [<!ENTITY e "payload">]>' + ET.tostring(root)
    else:
        ref = root.find(".//row[2]/process")
        if change == "cycle":
            original = root.find(".//row/process")
            original.set("ref", original.get("id"))
        elif change == "unknown_reference":
            ref.set("ref", "missing")
        elif change == "wrong_type":
            ref.set("ref", "3")
        else:
            ref.set("id", "2")
        content = ET.tostring(root)
    with pytest.raises(ValueError):
        parse_process_xml(content, 777)


def test_unknown_schema_or_cpu_unit_cannot_pass_as_app_cpu():
    root = ET.fromstring(FIXTURE.read_bytes())
    schema = root.find(".//schema")
    schema.set("name", "time-sample")
    assert parse_process_xml(ET.tostring(root), 777)["samples"] == []
    schema.set("name", "sysmon-process")
    for col in schema.findall("col"):
        if col.findtext("mnemonic") == "cpu-percent":
            col.find("engineering-type").text = "weight"
    parsed = parse_process_xml(ET.tostring(root), 777)
    assert "cpu_percent" in parsed["missing"] and parsed["notes"]


def test_toc_never_includes_environment_or_unrelated_tables():
    content = b'<trace-toc><run number="1"><info><target><device platform="iOS" uuid="fixture-phone"/><process pid="777"/><environment><item key="API_KEY" value="sensitive-fixture"/></environment></target></info><data><table schema="sysmon-process"/><table schema="system-cpu"/></data></run></trace-toc>'
    result = toc_metadata(content)
    assert result["target_pid"] == 777 and result["schemas"] == ["sysmon-process"]
    assert "sensitive-fixture" not in json.dumps(result) and "environment" not in result


def test_utf16_cannot_bypass_document_type_rejection():
    malicious = '<!DOCTYPE trace-query-result [<!ENTITY payload "expanded">]><trace-query-result>&payload;</trace-query-result>'.encode(
        "utf-16"
    )
    with pytest.raises(ValueError, match="UTF-8"):
        parse_process_xml(malicious, 777)


@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf"), True, "1"])
def test_explicit_measurements_reject_invalid_values(bad):
    value = interchange()
    value["measurements"][0]["values"] = [bad]
    with pytest.raises(ValueError):
        parse_measurements(value)


def test_explicit_measurements_reject_wrong_units_and_duplicates():
    value = interchange()
    value["measurements"][0]["unit"] = "s"
    with pytest.raises(ValueError, match="unit"):
        parse_measurements(value)
    value = interchange()
    value["measurements"].append(copy.deepcopy(value["measurements"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        parse_measurements(value)


def test_offline_import_and_gates_never_require_or_execute_a_phone(root, monkeypatch):
    source = root / "metrics.json"
    source.write_text(json.dumps(interchange()))
    artifact = import_measurement(
        root, "suite", {"format": "measurements_json", "path": str(source)}
    )
    monkeypatch.setattr(
        MobileStore,
        "get",
        lambda *args: pytest.fail("Offline evaluation must not load device profiles"),
    )
    result = execute_test_plan(root, "suite", evaluation(artifact))
    assert result["verdict"] == "passed", result
    assert result["metrics"]["fresh_ui_execution"] is False
    assert result["steps"][0]["actual"] == 120
    assert (
        execute_test_plan(root, "suite", evaluation(artifact, max_ios_launch_ms=110))[
            "verdict"
        ]
        == "failed"
    )
    missing = execute_test_plan(
        root, "suite", evaluation(artifact, max_cpu_percent=100)
    )
    assert (
        missing["verdict"] == "inconclusive" and missing["steps"][0]["actual"] is None
    )
    with pytest.raises(ValueError, match="Offline"):
        bad = evaluation(artifact)
        bad["spec"]["iterations"] = 2
        execute_test_plan(root, "suite", bad)


def test_host_trace_cannot_satisfy_ios_gate(root):
    artifact = import_measurement(
        root,
        "suite",
        {
            "format": "xctrace_xml",
            "path": str(FIXTURE),
            "platform": "macos",
            "target_pid": 777,
        },
    )
    assert artifact["measurements"]["cpu_percent"]["max"] == 0.005367029
    assert not artifact["raw_source_retained"]
    result = execute_test_plan(root, "suite", evaluation(artifact, max_cpu_percent=100))
    assert result["verdict"] == "inconclusive" and "macOS" in result["error"]


def test_process_identity_requires_exact_app_ownership():
    profile = {"device_id": "fixture-phone", "app_id": "com.example.app"}

    def runner(argv, **kwargs):
        if "processes" in argv:
            return json.dumps(
                {
                    "result": {
                        "runningProcesses": [
                            {
                                "processIdentifier": 777,
                                "executable": "file:///Apps/Target.app/Target",
                            }
                        ]
                    }
                }
            )
        return json.dumps(
            {
                "result": {
                    "apps": [
                        {
                            "bundleIdentifier": "com.example.app",
                            "url": "file:///Apps/Target.app",
                        }
                    ]
                }
            }
        )

    verify_pid(profile, 777, RunControl(timeout_seconds=2), runner=runner)
    with pytest.raises(ValueError):
        verify_pid(profile, 778, RunControl(timeout_seconds=2), runner=runner)

    def wrong(argv, **kwargs):
        return json.dumps(
            {
                "result": {
                    "runningProcesses": [
                        {"pid": 777, "bundleIdentifier": "com.other.app"}
                    ]
                }
            }
        )

    with pytest.raises(ValueError, match="another App"):
        verify_pid(profile, 777, RunControl(timeout_seconds=2), runner=wrong)


@pytest.mark.parametrize("ready", [True, False])
def test_recorder_interrupts_only_recorder_and_cleans_temporary_files(
    monkeypatch, ready
):
    monkeypatch.setattr(ios_capture.sys, "platform", "darwin")
    signals = []

    class Process:
        returncode = None

        def __init__(self, argv, **kwargs):
            assert argv[argv.index("--attach") + 1] == "777"
            kwargs["stdout"].write(
                b"Ctrl-C to stop the recording\n" if ready else b"Not ready\n"
            )
            kwargs["stdout"].flush()

        def poll(self):
            return self.returncode

        def send_signal(self, sig):
            signals.append(sig)
            self.returncode = 0

        def wait(self, **kwargs):
            return self.returncode

    recorder = XctraceRecorder(
        777,
        RunControl(timeout_seconds=2),
        process_factory=Process,
        ready_timeout_seconds=0.01,
    )
    directory = recorder.directory
    if ready:
        recorder.start()
        assert recorder.ready
    else:
        with pytest.raises(ios_capture.CaptureFailure, match="startup_timeout"):
            recorder.start()
    recorder.close()
    assert signals == [signal.SIGINT] and not directory.exists()


@pytest.mark.parametrize(
    "recording_ok,quality_ok", [(True, True), (False, True), (True, False)]
)
def test_live_ios_collection_is_connected_to_client_engine_without_device_actions(
    root, monkeypatch, recording_ok, quality_ok
):
    profile = {
        "id": "phone",
        "platform": "ios",
        "device_id": "fixture-phone",
        "app_id": "com.example.app",
    }
    MobileStore(root, "suite").save_profile(profile)
    monkeypatch.setattr(ios_capture, "verify_pid", lambda *args: None)

    class Recorder:
        closed = False

        def __init__(self, **kwargs):
            assert kwargs["device_id"] == "fixture-phone"

        def start(self):
            pass

        def finish(self):
            if not recording_ok:
                raise ValueError("Controlled measurement failure")
            return {
                **parse_process_xml(FIXTURE.read_bytes(), 777),
                "metric_quality": {"cpu_percent": {"assessed": quality_ok}},
                "metadata": {
                    "platform": "iOS",
                    "device_id": "fixture-phone",
                    "target_pid": 777,
                },
            }

        def close(self):
            self.closed = True

    monkeypatch.setattr(ios_capture, "XctraceRecorder", Recorder)
    registry = create_engine_registry(root, "suite")

    class UI:
        def execute(self, plan, context):
            from types import SimpleNamespace

            from iatest.engines.client_performance import ACTIVE_CLIENT_CAPTURE

            collector = ACTIVE_CLIENT_CAPTURE.get()
            collector.begin_ui_session(SimpleNamespace(profile=profile))
            try:
                return EngineResult(
                    plan.id, "ios", verdict="passed", steps=[{"elapsed_ms": 12}]
                )
            finally:
                try:
                    collector.finish_ui_session()
                except ValueError as exc:
                    collector.measurement_errors.append(str(exc))

    registry._engines["ios"] = UI()
    plan = ExecutionPlan.from_dict(
        {
            "id": "live-ios",
            "kind": "client_performance",
            "spec": {
                "platform": "ios",
                "iterations": 1,
                "ios_capture": {"target_pid": 777},
                "plan": {
                    "id": "journey",
                    "kind": "ios",
                    "spec": {
                        "profile": "phone",
                        "steps": [
                            {
                                "op": "expect",
                                "target": {"text": "Ready"},
                                "attribute": "exists",
                                "expected": True,
                            }
                        ],
                    },
                },
                "thresholds": {"max_cpu_percent": 50, "max_memory_footprint_kb": 50000},
            },
        }
    )
    if not recording_ok:
        plan.spec["thresholds"] = {"max_action_max_ms": 100}
    result = registry.execute(plan, EngineContext(RunControl(timeout_seconds=5)))
    if not recording_ok:
        assert result.verdict == "inconclusive" and result.metrics["capture_errors"]
        assert not EngineStore(root, "suite").artifacts("ios_measurement")
        return
    assert result.verdict == ("passed" if quality_ok else "inconclusive"), result
    artifact_id = result.metrics["repetitions"][0]["measurement_id"]
    artifact = EngineStore(root, "suite").get_artifact("ios_measurement", artifact_id)
    assert artifact["source"]["app_id"] == "com.example.app"
    assert "environment" not in json.dumps(artifact)


def test_launch_recording_is_explicit_and_bundle_argument_is_last():
    recorder = XctraceRecorder(
        control=RunControl(),
        mode="launch",
        device_id="fixture",
        app_id="com.example.app",
    )
    try:
        assert recorder.argv()[-3:] == ["--launch", "--", "com.example.app"]
        assert "--attach" not in recorder.argv()
    finally:
        recorder.close()
    with pytest.raises(ValueError, match="pre-launch PID"):
        XctraceRecorder(
            777,
            RunControl(),
            mode="launch",
            device_id="fixture",
            app_id="com.example.app",
        )


def test_recorder_classifies_missing_target_without_leaking_raw_output(monkeypatch):
    monkeypatch.setattr(ios_capture.sys, "platform", "darwin")
    calls = []

    class Process:
        returncode = 21

        def __init__(self, argv, **kwargs):
            calls.append(argv)
            kwargs["stdout"].write(
                b"Cannot find process for provided pid: 777\nprivate credential=never-report-this\n"
            )
            kwargs["stdout"].flush()

        def poll(self):
            return self.returncode

    recorder = XctraceRecorder(777, RunControl(), process_factory=Process)
    with pytest.raises(ios_capture.CaptureFailure) as error:
        recorder.start()
    assert error.value.code == "target_not_found"
    assert error.value.phase == "target_discovery"
    assert "never-report-this" not in str(error.value)
    assert len(calls) == 1 and "--launch" not in calls[0]
    assert not recorder.directory.exists()
    recorder.close()


def test_launch_cleanup_never_kills_a_different_app_incarnation(monkeypatch):
    profile = {"platform": "ios", "device_id": "fixture", "app_id": "com.example.app"}
    capture = ios_capture.IOSCapture(
        None, None, profile, RunControl(), {"mode": "launch"}
    )
    capture.cleanup_status, capture.launched_pid = "pending", 777
    killed = []
    monkeypatch.setattr(ios_capture, "resolve_pid", lambda *args, **kw: 888)
    monkeypatch.setattr(
        ios_capture, "terminate_app_pid", lambda *args: killed.append(args)
    )
    capture._cleanup_launched_app()
    assert not killed
    assert capture.cleanup_status == "owned_pid_gone_other_process_left"
    capture.cleanup_status = "pending"
    monkeypatch.setattr(ios_capture, "resolve_pid", lambda *args, **kw: 777)
    capture._cleanup_launched_app()
    assert killed[0][1] == 777 and capture.cleanup_status == "stopped"


@pytest.mark.parametrize("response", [{"accepted": True}, {"status": "unknown"}])
def test_launch_activation_is_once_and_requires_acknowledgement(monkeypatch, response):
    from types import SimpleNamespace

    profile = {"platform": "ios", "device_id": "fixture", "app_id": "com.example.app"}
    capture = ios_capture.IOSCapture(
        None, None, profile, RunControl(), {"mode": "launch"}
    )
    capture.launched_pid = 777
    order = []
    monkeypatch.setattr(capture, "before", lambda: order.append("record"))
    monkeypatch.setattr(
        ios_capture, "verify_pid", lambda *args: order.append(("verify", args[1]))
    )

    class Channel:
        def __init__(self):
            self.capabilities = {"actions": ["activate_running"]}

        def request(self, *args, **kwargs):
            order.append(("activate", kwargs["operation_id"]))
            return response

    session = SimpleNamespace(profile=profile, device=SimpleNamespace(input=Channel()))
    if response.get("accepted"):
        capture.begin_ui_session(session)
    else:
        with pytest.raises(ios_capture.CaptureFailure, match="activation_unconfirmed"):
            capture.begin_ui_session(session)
    assert order[0] == "record" and order[1] == ("verify", 777)
    assert order[-1] == ("verify", 777)
    assert (
        len(
            [
                item
                for item in order
                if isinstance(item, tuple) and item[0] == "activate"
            ]
        )
        == 1
    )


def test_old_runner_is_rejected_before_launch_restarts_the_app(monkeypatch):
    from types import SimpleNamespace

    profile = {"platform": "ios", "device_id": "fixture", "app_id": "com.example.app"}
    capture = ios_capture.IOSCapture(
        None, None, profile, RunControl(), {"mode": "launch"}
    )
    monkeypatch.setattr(
        capture,
        "before",
        lambda: pytest.fail("Do not restart before capability preflight"),
    )
    session = SimpleNamespace(
        profile=profile, device=SimpleNamespace(input=SimpleNamespace(capabilities={}))
    )
    with pytest.raises(ios_capture.CaptureFailure, match="runner_upgrade_required"):
        capture.begin_ui_session(session)


def test_device_process_statistics_are_explicit_and_never_launch_the_target():
    recorder = XctraceRecorder(777, mode="device_processes", device_id="fixture")
    try:
        assert "--all-processes" in recorder.argv()
        assert "--launch" not in recorder.argv() and "--attach" not in recorder.argv()
    finally:
        recorder.close()
    with pytest.raises(ValueError):
        XctraceRecorder(777, mode="device_processes", template="animation_hitches")


def test_device_scope_trace_requires_explicit_opt_in_and_retains_selected_pid(
    tmp_path, monkeypatch
):
    trace = tmp_path / "capture.trace"
    trace.mkdir()
    toc = b'<trace-toc><run number="1"><info><target><device platform="iOS" uuid="fixture"/></target><summary><template-name>Activity Monitor</template-name></summary></info><data><table schema="sysmon-process"/></data></run></trace-toc>'
    monkeypatch.setattr(
        ios_capture,
        "export_xml",
        lambda path, output, arguments, control: (
            toc if "--toc" in arguments else FIXTURE.read_bytes()
        ),
    )
    with pytest.raises(ValueError, match="target PID"):
        ios_capture.read_trace(trace, target_pid=777)
    parsed = ios_capture.read_trace(trace, target_pid=777, allow_device_scope=True)
    assert parsed["samples"]
    assert parsed["metadata"]["target_pid"] == 0
    assert parsed["metadata"]["selected_pid"] == 777
    assert not ios_capture.read_trace(trace, target_pid=999, allow_device_scope=True)[
        "samples"
    ]


def test_existing_process_capture_closes_recorder_without_terminating_the_app(
    monkeypatch,
):
    profile = {"platform": "ios", "device_id": "fixture", "app_id": "com.example.app"}
    capture = ios_capture.IOSCapture(
        None, None, profile, RunControl(), {"mode": "device_processes"}
    )
    monkeypatch.setattr(ios_capture, "resolve_pid", lambda *args, **kwargs: 777)
    monkeypatch.setattr(
        ios_capture,
        "terminate_app_pid",
        lambda *args: pytest.fail("Existing App must remain running"),
    )

    class Recorder:
        def __init__(self, **kwargs):
            assert kwargs["mode"] == "device_processes" and kwargs["target_pid"] == 777

        def start(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(ios_capture, "XctraceRecorder", Recorder)
    capture.before()
    capture.close()
    assert capture.cleanup_status == "not_owned"
