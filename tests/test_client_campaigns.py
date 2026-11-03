import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from iatest.application.campaigns import create_campaign, run_campaign
from iatest.application.testing_service import execute_test_plan
from iatest.core.config import init_project
from iatest.engines.client_performance import android_metrics
from iatest.engines.contracts import ExecutionPlan
from iatest.engines.store import EngineStore
from iatest.engines.validation import validate_plan
from iatest.mobile.lease import DeviceLease


@pytest.fixture
def lab(tmp_path):
    init_project(tmp_path, "suite")
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            calls.append(self.path)
            body = b"<html><body><h1>Client journey</h1><button>Continue</button></body></html>"
            self.send_response(503 if self.path == "/fail" else 200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield tmp_path, f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()


def api(base, name, path="/"):
    return {
        "id": name,
        "kind": "api",
        "spec": {"allowed_origins": [base], "requests": [{"url": base + path}]},
    }


def test_campaign_freezes_plans_and_keeps_cleanup_after_dependency_failure(lab):
    root, base, calls = lab
    store = EngineStore(root, "suite")
    for name, path in (
        ("check", "/fail"),
        ("dependent", "/blocked"),
        ("cleanup", "/cleanup"),
    ):
        store.save_plan(api(base, name, path))
    manifest = {
        "id": "release",
        "build": {"service_version": "v2"},
        "nodes": [
            {"id": "check", "plan_id": "check"},
            {"id": "dependent", "plan_id": "dependent", "depends_on": ["check"]},
            {
                "id": "cleanup",
                "plan_id": "cleanup",
                "depends_on": ["check"],
                "always": True,
            },
        ],
    }
    snapshot = create_campaign(root, "suite", manifest)
    store.save_plan(api(base, "check", "/now-passes"))
    result = run_campaign(root, "suite", "release")
    assert result["verdict"] == "failed", result
    assert (
        "/fail" in calls
        and "/cleanup" in calls
        and "/blocked" not in calls
        and "/now-passes" not in calls
    )
    assert [gate["category"] for gate in result["gates"]] == [
        "business_assertion",
        "dependency_blocked",
        "passed",
    ]
    assert result["manifest_digest"] == snapshot["digest"]
    assert all(store.get_result(gate["run_id"]) for gate in result["gates"])
    with pytest.raises(Exception, match="UNIQUE"):
        create_campaign(root, "suite", manifest)


def test_campaign_resource_conflict_is_environment_failure_without_requests(lab):
    root, base, calls = lab
    EngineStore(root, "suite").save_plan(api(base, "smoke"))
    create_campaign(
        root,
        "suite",
        {
            "id": "locked",
            "build": {"version": "v1"},
            "resources": ["account:shared"],
            "nodes": [{"id": "smoke", "plan_id": "smoke"}],
        },
    )
    lease = DeviceLease("resource", "account:shared")
    lease.acquire()
    try:
        result = run_campaign(root, "suite", "locked")
        assert (
            result["state"] == "environment_error"
            and result["verdict"] == "inconclusive"
        )
        assert calls == []
    finally:
        lease.close()
    assert run_campaign(root, "suite", "locked")["verdict"] == "passed"


def test_android_missing_frames_are_not_reported_as_zero_jank():
    assert android_metrics(
        "TOTAL PSS: 1234", "Total frames rendered: 0\nJanky frames: 0"
    ) == {"memory_pss_kb": 1234}
    assert (
        android_metrics(
            "TOTAL PSS: 1234", "Total frames rendered: 10\nJanky frames: 2"
        )["jank_ratio"]
        == 0.2
    )


@pytest.mark.parametrize("output,total,wait", [("Status: ok\nWaitTime: 9\n", None, 9), ("Status: ok\nTotalTime: 11\nWaitTime: 13\n", 11, 13)])
def test_android_launch_wait_and_total_times_are_distinct(output, total, wait):
    from iatest.engines.client_performance import AndroidCapture
    from iatest.execution.control import RunControl

    capture = AndroidCapture({"id": "phone", "platform": "android", "device_id": "fixture", "app_id": "dev.example.app"}, RunControl(timeout_seconds=5), "warm")
    capture.device.adb = lambda *args: "dev.example.app/.Main" if "resolve-activity" in args else "100" if "getconf" in args else output
    capture.cpu_snapshot = lambda: (1, 2, 3)
    capture.before()
    assert capture.values.get("android_start_total_ms") == total
    assert capture.values["android_start_wait_ms"] == wait


def test_client_preparation_cannot_install_on_a_different_device():
    plan = {
        "id": "client-device",
        "kind": "client_performance",
        "preparation": {
            "app": {"profile": "other-phone", "artifact_id": "build", "mode": "fresh"}
        },
        "spec": {
            "platform": "android",
            "plan": {
                "id": "journey",
                "kind": "android",
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
            "thresholds": {"max_memory_pss_kb": 100000},
        },
    }
    with pytest.raises(ValueError, match="same device"):
        validate_plan(ExecutionPlan.from_dict(plan))


@pytest.mark.skipif(
    not os.environ.get("IATEST_CHROMIUM_PATH"), reason="Native Chromium not configured"
)
def test_real_browser_client_metrics_and_unavailable_metric_gate(lab):
    root, base, _ = lab
    child = {
        "id": "web-client",
        "kind": "web",
        "spec": {
            "start_url": base,
            "allowed_origins": [base],
            "steps": [
                {"op": "expect", "target": {"selector": "h1"}, "assert": "exists"},
                {"op": "screenshot"},
            ],
        },
    }
    plan = {
        "id": "client",
        "kind": "client_performance",
        "spec": {
            "platform": "web",
            "plan": child,
            "iterations": 2,
            "warmup": 1,
            "thresholds": {"max_navigation_ms": 10000, "max_action_max_ms": 10000},
        },
    }
    result = execute_test_plan(root, "suite", plan)
    assert result["verdict"] == "passed", json.dumps(result, indent=2)
    assert result["metrics"]["measurements"]["navigation_ms"]["samples"] == 2
    assert len(result["evidence"]) == 3
    assert all(
        EngineStore(root, "suite").get_result(row["run_id"])
        for row in result["metrics"]["repetitions"]
    )
    plan["spec"].update(iterations=1, warmup=0, thresholds={"max_cpu_percent": 100000})
    result = execute_test_plan(root, "suite", plan)
    if "cpu_percent" in result["metrics"]["measurements"]:
        assert result["verdict"] == "passed", result
        assert result["metrics"]["measurements"]["cpu_percent"]["max"] >= 0
    else:
        # A renderer/utility process may exit during navigation. Its final CPU
        # counter is unknown; a generous threshold cannot turn that into pass.
        assert result["verdict"] == "inconclusive", result
        assert result["metrics"]["not_assessed"] == ["cpu_percent"]
        assert any("CPU" in note and ("changed" in note or "unavailable" in note)
                   for note in result["limitations"])
