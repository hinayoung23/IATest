import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from iatest.application.testing_service import execute_test_plan
from iatest.core.config import init_project, load_config, save_config
from iatest.execution.control import RunControl
from iatest.nodes.coordinator import (
    CoordinatorClient,
    LeaseGuard,
    LeaseStore,
    make_coordinator,
)


def test_expired_resources_are_quarantined_and_old_fences_never_resume(tmp_path):
    now = [1000]
    store = LeaseStore(tmp_path / "authority.sqlite", clock=lambda: now[0])
    first = store.operate(
        "a", "acquire", {"id": "one", "resources": ["phone"], "ttl": 2}, ["phone"]
    )
    assert (
        store.operate(
            "a", "acquire", {"id": "one", "resources": ["phone"], "ttl": 2}, ["phone"]
        )["id"]
        == first["id"]
    )
    now[0] += 3
    with pytest.raises(ValueError, match="occupied"):
        store.operate("b", "acquire", {"id": "two", "resources": ["phone"]}, ["phone"])
    claims = store.operate("a", "list", {}, ["phone"])
    assert claims["leases"][0]["state"] == "uncertain"
    for owner, stopped in [("b", True), ("a", False)]:
        with pytest.raises(ValueError):
            store.operate(
                owner,
                "reconcile",
                {
                    "id": "one",
                    "stopped": stopped,
                    "note": "Confirmed all old input processes stopped",
                },
                ["phone"],
            )
    store.operate(
        "a",
        "reconcile",
        {
            "id": "one",
            "stopped": True,
            "note": "Confirmed all old input processes stopped",
        },
        ["phone"],
    )
    second = store.operate(
        "b", "acquire", {"id": "two", "resources": ["phone"]}, ["phone"]
    )
    assert json.loads(second["fences"])["phone"] == 2
    with pytest.raises(ValueError, match="fencing"):
        store.operate("b", "renew", {"id": "two", "fences": {"phone": 1}}, ["phone"])


@pytest.fixture
def authority(tmp_path, monkeypatch):
    monkeypatch.setenv("IATEST_RESOURCE_TEST_A", "synthetic-resource-authority-a")
    monkeypatch.setenv("IATEST_RESOURCE_TEST_B", "synthetic-resource-authority-b")
    identities = [
        {
            "id": name,
            "token_env": "IATEST_RESOURCE_TEST_" + name.upper(),
            "resources": ["lab:shared"],
        }
        for name in ("a", "b")
    ]
    server = make_coordinator(tmp_path / "authority.sqlite", identities, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    roots = {}
    for name in ("a", "b"):
        root = tmp_path / name
        init_project(root, "suite")
        config = load_config(root)
        config["resource_coordinator"] = {
            "url": f"http://127.0.0.1:{server.server_port}",
            "id": name,
            "token_env": "IATEST_RESOURCE_TEST_" + name.upper(),
            "ttl": 2,
        }
        save_config(root, config)
        roots[name] = root
    try:
        yield server, roots
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_independent_controller_processes_exclude_before_http_dispatch(authority):
    _, roots = authority
    entered, release = threading.Event(), threading.Event()
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            calls.append(self.path)
            entered.set()
            release.wait(10)
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    plan = {
        "id": "shared-api",
        "kind": "api",
        "resources": ["lab:shared"],
        "spec": {"allowed_origins": [base], "requests": [{"url": base}]},
    }
    source = str(Path(__file__).resolve().parents[1])
    code = (
        "import sys,json;from pathlib import Path;sys.path.insert(0,"
        + repr(source)
        + ");from iatest.application.testing_service import execute_test_plan;print(json.dumps(execute_test_plan(Path("
        + repr(str(roots["a"]))
        + "),'suite',"
        + repr(plan)
        + ")))"
    )
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert entered.wait(5)
        blocked = execute_test_plan(roots["b"], "suite", plan)
        assert blocked["verdict"] == "inconclusive" and len(calls) == 1, blocked
        release.set()
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        passed = json.loads(stdout)
        assert passed["verdict"] == "passed", passed
        assert passed["metrics"]["global_resources"]["state"] == "released"
        config = load_config(roots["b"])
        config["sandbox"]["mode"] = "process"
        save_config(roots["b"], config)
        second = execute_test_plan(roots["b"], "suite", plan)
        assert second["verdict"] == "passed" and len(calls) == 2, second
        assert (
            second["metrics"]["global_resources"]["lease"]["fences"]["lab:shared"] == 2
        )
    finally:
        release.set()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_authority_loss_stops_control_and_preserves_uncertain_claim(authority):
    server, roots = authority
    client = CoordinatorClient.configured(roots["a"])
    guard = LeaseGuard(client, ["lab:shared"])
    guard.safe = True
    control = RunControl(cancellation_check=lambda: not guard.alive())
    server.shutdown()
    server.server_close()
    deadline = time.monotonic() + 4
    while not control.reason() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert control.reason()
    assert guard.close() == "unconfirmed"


def test_resource_acl_and_tls_are_mandatory(authority):
    _, roots = authority
    client = CoordinatorClient.configured(roots["a"])
    try:
        with pytest.raises(ValueError, match="authorize"):
            client.call("acquire", {"id": "bad", "resources": ["not-authorized"]})
    finally:
        client.close()
    with pytest.raises(ValueError, match="TLS"):
        CoordinatorClient(
            {
                "url": "http://192.0.2.5:8769",
                "id": "a",
                "token_env": "IATEST_RESOURCE_TEST_A",
            }
        )


def test_resource_inference_does_not_read_business_records(tmp_path):
    from iatest.engines.contracts import ExecutionPlan
    from iatest.nodes.coordinator import declared_resources

    init_project(tmp_path, "suite")
    value = {
        "id": "data",
        "kind": "data",
        "resources": ["real"],
        "spec": {
            "records": [
                {
                    "kind": "android",
                    "spec": {"profile": "not-a-profile"},
                    "resources": ["business-field"],
                    "plan": {"resources": ["also-data"]},
                }
            ]
        },
    }
    assert declared_resources(tmp_path, "suite", ExecutionPlan.from_dict(value)) == [
        "real"
    ]
