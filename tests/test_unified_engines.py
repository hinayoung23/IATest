from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest

from iatest.application.testing_service import execute_test_plan
from iatest.core.config import init_project, load_config, save_config
from iatest.engines.contracts import EngineContext, EngineResult, ExecutionPlan
from iatest.engines.http import HTTPExecutor, NetworkScope
from iatest.engines.registry import create_engine_registry
from iatest.engines.store import EngineStore
from iatest.engines.validation import validate_plan
from iatest.engines.web import BrowserSession, chromium_path
from iatest.engines.workflow import validate_workflow
from iatest.execution.control import RunControl
from iatest.web.i18n import set_current_locale
from iatest.web.server import route_get
from iatest.web.testing import build_plan


@pytest.fixture
def root(tmp_path):
    init_project(tmp_path, "suite")
    return tmp_path


@pytest.fixture
def server():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            path = urlparse(self.path).path
            calls.append((path, self.headers.get("Authorization", "")))
            if path == "/slow":
                time.sleep(0.08)
            status, body, headers = (
                200,
                {"id": "order-1", "url": "http://example.invalid"},
                {},
            )
            if (
                path == "/secure"
                and self.headers.get("Authorization") != "Bearer owner"
            ):
                status, body = 403, {"error": "denied"}
            if (
                path == "/ambiguous"
                and self.headers.get("Authorization") != "Bearer owner"
            ):
                body = {"id": "another-order"}
            if path == "/redirect":
                status, headers = 302, {"Location": "http://localhost:1/blocked"}
            if path == "/bad":
                status = 500
            if path == "/html":
                headers["Content-Type"] = "text/html; charset=utf-8"
                body = (
                    """<!doctype html><html><body>
                <label for="username">Username</label><input id="username" data-testid="user" value="old">
                <button id="login" aria-label="Sign in" onclick="document.querySelector('#status').textContent='Hello '+document.querySelector('#username').value">Login</button>
                <div id="status">Waiting</div><button>Duplicate</button><button>Duplicate</button>
                <table id="orders"><tr><td class="order-id">order-1</td><td><button class="details">Details</button></td></tr>
                <tr><td class="order-id">order-2</td><td><button class="details">Details</button></td></tr></table>
                <button id="covered">Covered</button><div style="position:absolute;inset:0;z-index:9;display:none" id="overlay"></div>
                <input id="password" type="password" value="private"><div id="long">"""
                    + ("x" * 1100)
                    + """</div>
                <div id="big">"""
                    + "".join(f"<span>node-{i}</span>" for i in range(2100))
                    + """</div>
                <div id="tail">tail</div></body></html>"""
                )
            encoded = (
                body.encode() if isinstance(body, str) else json.dumps(body).encode()
            )
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except BrokenPipeError:
                pass

        def do_POST(self):
            body = json.loads(
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
            )
            status = (
                200
                if self.path == "/unsafe-mutation"
                or (isinstance(body.get("name"), str) and set(body) == {"name"})
                else 422
            )
            calls.append((self.path, body))
            data = json.dumps({"id": "order-1"}).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    instance = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{instance.server_port}", calls
    instance.shutdown()
    instance.server_close()
    thread.join(timeout=2)


def plan(kind, spec, **budget):
    return {
        "id": "sample",
        "kind": kind,
        "spec": spec,
        "budget": {"timeout_seconds": 20, **budget},
    }


def test_capabilities_and_storage(root):
    assert set(create_engine_registry(root, "suite").capabilities()) == {
        "api",
        "web",
        "performance",
        "client_performance",
        "remote",
        "security",
        "workflow",
        "android",
        "ios",
        "data",
        "protocol",
        "accessibility",
        "desktop",
    }
    for value in [float("nan"), float("inf")]:
        with pytest.raises(ValueError):
            ExecutionPlan.from_dict(
                plan("api", {"requests": []}, timeout_seconds=value)
            )
    with pytest.raises(ValueError):
        EngineStore(root, "suite").evidence_path("../bad", "a" * 32 + ".png")


def test_http_scope_budget_redirects_and_truncation(root, server):
    base, calls = server
    executor = HTTPExecutor(
        NetworkScope(root, "suite", [base]), RunControl(max_requests=3)
    )
    try:
        with pytest.raises(PermissionError):
            executor.request({"url": base + "/redirect", "follow_redirects": True})
        assert len(calls) == 1
        response = executor.request({"url": base}, max_bytes=4)
        assert response["truncated"] and not response["ok"]
        executor.request({"url": base})
        with pytest.raises(Exception, match="请求预算"):
            executor.request({"url": base})
    finally:
        executor.close()


def test_sandbox_scope_cannot_be_expanded_by_plan(root, server):
    config = load_config(root)
    config["sandbox"]["allowed_origins"] = ["https://example.invalid"]
    save_config(root, config)
    with pytest.raises(PermissionError):
        NetworkScope(root, "suite", [server[0]]).check(server[0])


@pytest.mark.parametrize("mode", ["users", "arrival_rate"])
def test_performance_modes_exact_samples_and_gates(root, server, mode):
    base, _ = server
    spec = {
        "allowed_origins": [base],
        "mode": mode,
        "duration_seconds": 1.5,
        "users": 2,
        # This checks accounting and gates, not the shared CI runner's capacity.
        # Saturation has a separate explicit slow-server test below.
        "rate": 2,
        "think_seconds": 0.005,
        "request": {"url": base},
        "thresholds": {"p95_ms": 500, "max_error_ratio": 0, "max_dropped_ratio": 0},
    }
    result = execute_test_plan(root, "suite", plan("performance", spec))
    assert result["verdict"] == "passed", json.dumps(result, indent=2)
    assert result["metrics"]["completed"] > 0
    assert result["metrics"]["completed"] == result["metrics"]["samples"]
    assert result["metrics"]["requests"] == result["metrics"]["completed"]
    assert result["metrics"]["max_inflight"] <= 2
    assert EngineStore(root, "suite").runs()[0]["run_id"] == result["run_id"]


def test_arrival_rate_reports_saturation_and_bad_status(root, server):
    base, _ = server
    spec = {
        "allowed_origins": [base],
        "mode": "arrival_rate",
        "duration_seconds": 0.12,
        "users": 1,
        "rate": 100,
        "request": {"url": base + "/slow"},
        "thresholds": {"p95_ms": 1000, "max_error_ratio": 0},
    }
    result = execute_test_plan(root, "suite", plan("performance", spec))
    assert result["verdict"] == "inconclusive" and result["metrics"]["dropped"] > 0
    spec["thresholds"]["max_dropped_ratio"] = 0
    assert (
        execute_test_plan(root, "suite", plan("performance", spec))["verdict"]
        == "failed"
    )
    spec.update(request={"url": base + "/bad"}, rate=5)
    result = execute_test_plan(root, "suite", plan("performance", spec))
    assert result["verdict"] == "failed" and result["metrics"]["error_ratio"] == 1


def test_performance_cancellation_and_user_lifecycle(root, server):
    base, calls = server
    spec = {
        "allowed_origins": [base],
        "mode": "users",
        "duration_seconds": 5,
        "users": 2,
        "setup": {"url": base + "/setup?user={{user}}"},
        "teardown": {"url": base + "/cleanup?user={{user}}"},
        "request": {"url": base + "/slow"},
        "thresholds": {"max_error_ratio": 0},
    }
    result = execute_test_plan(
        root, "suite", plan("performance", spec), RunControl(timeout_seconds=0.16)
    )
    assert result["verdict"] == "inconclusive" and result["lifecycle"] in {
        "cancelled",
        "timed_out",
    }
    assert [p for p, _ in calls].count("/cleanup") == 2


@pytest.mark.parametrize(
    ("path", "verdict"),
    [("/secure", "passed"), ("/vulnerable", "failed"), ("/ambiguous", "inconclusive")],
)
def test_security_identity_controls(root, server, monkeypatch, path, verdict):
    base, calls = server
    monkeypatch.setenv("TEST_OWNER_AUTH", "Bearer owner")
    monkeypatch.setenv("TEST_OTHER_AUTH", "Bearer challenger")
    spec = {
        "allowed_origins": [base],
        "identities": {
            "owner": {"Authorization": {"$env": "TEST_OWNER_AUTH"}},
            "other": {"Authorization": {"$env": "TEST_OTHER_AUTH"}},
        },
        "checks": [
            {
                "id": "auth",
                "rule": "authorization",
                "request": {"url": base + path},
                "owner": "owner",
                "challenger": "other",
                "marker": {"path": "$.id", "expected": "order-1"},
            }
        ],
    }
    result = execute_test_plan(root, "suite", plan("security", spec))
    assert result["verdict"] == verdict, result
    assert len(calls) == 2
    assert "Bearer owner" not in json.dumps(EngineStore(root, "suite").runs())
    assert result["metrics"]["not_assessed"] == (1 if verdict == "inconclusive" else 0)


@pytest.mark.parametrize("mutation", ["remove_required", "invalid_type", "extra_field"])
@pytest.mark.parametrize(
    ("path", "verdict"), [("/safe-mutation", "passed"), ("/unsafe-mutation", "failed")]
)
def test_security_contract_mutations(root, server, mutation, path, verdict):
    base, _ = server
    schema = {
        "type": "object",
        "required": ["name"],
        "properties": {"name": {"type": "string"}},
        "additionalProperties": False,
    }
    spec = {
        "allowed_origins": [base],
        "allow_mutation": True,
        "checks": [
            {
                "id": "mutation",
                "rule": "contract_mutation",
                "request": {
                    "url": base + path,
                    "method": "POST",
                    "json": {"name": "good"},
                },
                "json_schema": schema,
                "mutation": mutation,
                "field": "extra" if mutation == "extra_field" else "name",
            }
        ],
    }
    result = execute_test_plan(root, "suite", plan("security", spec))
    assert result["verdict"] == verdict, result
    spec["allow_mutation"] = False
    with pytest.raises(ValueError, match="allow_mutation"):
        validate_plan(ExecutionPlan.from_dict(plan("security", spec)))


def test_security_same_identity_and_header_policy(root, server):
    base, _calls = server
    spec = {
        "allowed_origins": [base],
        "checks": [
            {
                "id": "headers",
                "rule": "response_headers",
                "request": {"url": base},
                "required_headers": {"x-content-type-options": "nosniff"},
            }
        ],
    }
    assert (
        execute_test_plan(root, "suite", plan("security", spec))["verdict"] == "failed"
    )
    spec["checks"][0]["request"]["url"] += "/bad"
    assert (
        execute_test_plan(root, "suite", plan("security", spec))["verdict"]
        == "inconclusive"
    )


def test_workflow_typed_data_and_cleanup(root, server):
    base, calls = server
    prepare = plan(
        "api",
        {
            "allowed_origins": [base],
            "requests": [{"url": base}],
            "exports": {
                "order": {"path": "$.id", "type": "string", "sensitive": False}
            },
        },
    )
    verify = plan(
        "api",
        {
            "allowed_origins": [base],
            "requests": [
                {
                    "url": base,
                    "method": "POST",
                    "json": {
                        "order": {
                            "$from": "prepare.order",
                            "type": "string",
                            "sensitive": False,
                        }
                    },
                    "expected_status": [422],
                }
            ],
        },
    )
    workflow = plan(
        "workflow",
        {
            "nodes": [
                {"id": "prepare", "plan": prepare},
                {"id": "verify", "depends_on": ["prepare"], "plan": verify},
                {
                    "id": "cleanup",
                    "depends_on": ["verify"],
                    "always": True,
                    "resource_owner": "prepare",
                    "plan": plan(
                        "api",
                        {
                            "allowed_origins": [base],
                            "requests": [{"url": base + "/cleanup"}],
                        },
                    ),
                },
            ]
        },
    )
    result = execute_test_plan(root, "suite", workflow)
    assert result["verdict"] == "passed", result
    assert calls[1][1] == {"order": "order-1"} and calls[-1][0] == "/cleanup"
    workflow["spec"]["nodes"][0]["plan"]["spec"]["requests"][0]["url"] += "/bad"
    calls.clear()
    result = execute_test_plan(root, "suite", workflow)
    assert result["verdict"] == "failed" and len(calls) == 2
    assert (
        result["steps"][1]["result"]["verdict"] == "skipped"
        and calls[-1][0] == "/cleanup"
    )


def test_workflow_invalid_dependencies_and_sensitivity(root):
    base = plan("api", {"case_ids": []})
    with pytest.raises(ValueError, match="Cyclic"):
        validate_workflow(
            ExecutionPlan.from_dict(
                plan(
                    "workflow",
                    {"nodes": [{"id": "a", "depends_on": ["b"], "plan": base}]},
                )
            )
        )


def test_workflow_cancellation_still_runs_owned_cleanup(root):
    from iatest.engines.workflow import WorkflowEngine

    control = RunControl(timeout_seconds=10)
    calls = []

    class Registry:
        def execute(self, child, context):
            context.control.check()
            calls.append(child.id)
            if child.id == "prepare":
                control.cancelled.set()
            return EngineResult(child.id, child.kind, verdict="passed")

    nodes = [
        {
            "id": "prepare",
            "plan": {"id": "prepare", "kind": "api", "spec": {"case_ids": []}},
        },
        {
            "id": "verify",
            "depends_on": ["prepare"],
            "plan": {"id": "verify", "kind": "api", "spec": {"case_ids": []}},
        },
        {
            "id": "cleanup",
            "always": True,
            "resource_owner": "prepare",
            "depends_on": ["verify"],
            "plan": {"id": "cleanup", "kind": "api", "spec": {"case_ids": []}},
        },
    ]
    result = WorkflowEngine(root, "suite", Registry()).execute(
        ExecutionPlan.from_dict(plan("workflow", {"nodes": nodes})),
        EngineContext(control),
    )
    assert calls == ["prepare", "cleanup"]
    assert result.lifecycle == "cancelled" and result.verdict == "inconclusive"


def test_typed_secret_aliases_and_model_draft_do_not_leak(root):
    from iatest.ai.provider import ModelResponse, ModelToolCall, TokenUsage
    from iatest.application.testing_service import draft_test_plan, safe_result

    raw = {
        "outputs": {
            "data": {
                "value": "private-object-value",
                "type": "string",
                "sensitive": True,
            }
        },
        "steps": [{"text": "Order private-object-value"}],
    }
    sanitized = safe_result(raw)
    assert "private-object-value" not in json.dumps(sanitized)
    authored = plan(
        "performance",
        {
            "allowed_origins": ["http://localhost:8000"],
            "mode": "users",
            "duration_seconds": 1,
            "users": 1,
            "request": {"url": "http://localhost:8000/health"},
            "thresholds": {"max_error_ratio": 0},
        },
    )

    class Gateway:
        def __init__(self, config):
            self.model_config = config

        def complete(self, **kwargs):
            return ModelResponse(
                tool_calls=[
                    ModelToolCall("submit", "submit_test_plan", {"plan": authored})
                ],
                usage=TokenUsage(100, 40, 140),
            )

    drafted = draft_test_plan(
        root,
        "suite",
        "performance",
        "只生成一个有明确错误率门禁的性能草稿",
        {"allowed_origins": ["http://localhost:8000"]},
        gateway_factory=Gateway,
    )
    assert drafted["plan"]["kind"] == "performance" and drafted["agent_run_id"]
    assert EngineStore(root, "suite").runs() == []


def test_per_node_request_budget_is_enforced(root, server):
    base, calls = server
    child = plan(
        "api",
        {"allowed_origins": [base], "requests": [{"url": base}, {"url": base}]},
        max_requests=1,
    )
    workflow = plan(
        "workflow", {"nodes": [{"id": "bounded", "plan": child}]}, max_requests=10
    )
    result = execute_test_plan(root, "suite", workflow)
    assert len(calls) == 1 and result["verdict"] == "inconclusive"
    assert "请求预算" in result["steps"][0]["result"]["error"]


def test_nested_workflows_validate_but_depth_is_bounded():
    base = plan("api", {"case_ids": []})
    nested = plan("workflow", {"nodes": [{"id": "b", "plan": base}]})
    assert validate_workflow(
        ExecutionPlan.from_dict(
            plan("workflow", {"nodes": [{"id": "a", "plan": nested}]})
        )
    )
    for _ in range(6):
        nested = plan("workflow", {"nodes": [{"id": "a", "plan": nested}]})
    with pytest.raises(ValueError, match="six levels"):
        validate_workflow(ExecutionPlan.from_dict(nested))


def test_nested_workflow_exports_and_shared_cleanup_budget(root, server):
    base, calls = server
    child = plan(
        "api",
        {
            "allowed_origins": [base],
            "requests": [{"url": base}],
            "exports": {
                "order": {"path": "$.id", "type": "string", "sensitive": False}
            },
        },
    )
    inner = plan(
        "workflow",
        {
            "max_parallel": 2,
            "nodes": [
                {"id": "create", "resources": ["same"], "plan": child},
                {
                    "id": "cleanup",
                    "always": True,
                    "plan": plan(
                        "api",
                        {
                            "allowed_origins": [base],
                            "requests": [{"url": base + "/cleanup"}],
                        },
                    ),
                },
            ],
            "exports": {
                "order": {
                    "node": "create",
                    "output": "order",
                    "type": "string",
                    "sensitive": False,
                }
            },
        },
    )
    outer = plan(
        "workflow",
        {
            "cleanup_max_requests": 1,
            "nodes": [
                {"id": "inner", "resources": ["same"], "plan": inner},
                {
                    "id": "verify",
                    "depends_on": ["inner"],
                    "plan": plan(
                        "api",
                        {
                            "allowed_origins": [base],
                            "requests": [
                                {
                                    "url": base,
                                    "method": "POST",
                                    "expected_status": [422],
                                    "json": {
                                        "order": {
                                            "$from": "inner.order",
                                            "type": "string",
                                            "sensitive": False,
                                        }
                                    },
                                }
                            ],
                        },
                    ),
                },
                {
                    "id": "cleanup",
                    "always": True,
                    "plan": plan(
                        "api",
                        {
                            "allowed_origins": [base],
                            "requests": [{"url": base + "/cleanup"}],
                        },
                    ),
                },
            ],
        },
    )
    result = execute_test_plan(root, "suite", outer)
    children = {row["node"]: row["result"] for row in result["steps"]}
    assert children["inner"]["verdict"] == "passed", result
    assert children["verify"]["verdict"] == "passed", result
    assert (
        result["verdict"] == "inconclusive"
        and result["metrics"]["cleanup_requests"] == 1
    ), result


@pytest.mark.parametrize("locale", ["zh", "en", "ja", "fr", "ru"])
def test_typed_console_tabs_and_builder(root, locale):
    set_current_locale(locale)
    try:
        for tab in (
            "web",
            "performance",
            "security",
            "workflow",
            "api",
            "runs",
            "capabilities",
        ):
            result = route_get(root, "suite", urlparse("/testing?tab=" + tab))
            assert "testing-workspace" in result and "{{TESTING_URL}}" not in result
            if locale != "zh":
                assert "多类型测试" not in result
    finally:
        set_current_locale("zh")
    value = build_plan(
        EngineStore(root, "suite"),
        {
            k: [v]
            for k, v in {
                "kind": "web",
                "id": "console-plan",
                "url": "http://localhost:8000",
                "origins": "http://localhost:8000",
                "selector": "#login",
                "expected_selector": "#status",
                "op": "click",
                "expected": "Done",
                "max_requests": "100",
            }.items()
        },
    )
    EngineStore(root, "suite").save_plan(value)
    assert EngineStore(root, "suite").get_plan("console-plan")["kind"] == "web"


@pytest.fixture
def browser(root, server):
    if not chromium_path():
        pytest.skip("Installed Chromium required for native CDP integration")
    session = BrowserSession(
        root,
        "suite",
        {"allowed_origins": [server[0]], "start_url": server[0] + "/html"},
        RunControl(timeout_seconds=60),
    )
    yield session
    session.close()
    assert session.process.poll() is not None
    assert not os.path.exists(session.temp.name)


def test_real_chromium_bounded_targets_actions_and_assertions(browser, monkeypatch):
    assert browser.query({"id": "login"})["unique"]
    accessible = browser.query({"role": "button", "name": "Sign in"})
    assert accessible["unique"], accessible
    assert browser.query({"label": "Username"})["unique"]
    assert browser.query({"test_id": "user"})["unique"]
    ambiguous = browser.query({"role": "button", "name": "Duplicate"})
    assert not ambiguous["unique"] and ambiguous["match_count"] == 2
    with pytest.raises(ValueError, match="ambiguous"):
        browser.act({"op": "click", "target": {"text": "Duplicate"}})
    anchored = browser.query(
        {
            "selector": ".details",
            "scope": "#orders",
            "anchor": {"selector": ".order-id", "text": "order-2"},
        }
    )
    assert anchored["unique"], anchored
    incomplete = browser.query({"text": "tail"})
    assert not incomplete["search_complete"]
    with pytest.raises(ValueError, match="Incomplete"):
        browser.assertion({"assert": "absent", "target": {"text": "never-existed"}})
    browser.act({"op": "fill", "target": {"id": "username"}, "value": "Tester"})
    browser.act({"op": "click", "target": {"id": "login"}})
    assert browser.assertion(
        {"assert": "text", "target": {"id": "status"}, "expected": "Hello Tester"}
    )[0]
    with pytest.raises(ValueError, match="Truncated"):
        browser.assertion(
            {"assert": "text", "target": {"id": "long"}, "expected": "x" * 1100}
        )
    with pytest.raises(ValueError, match="Secure"):
        browser.assertion(
            {"assert": "value", "target": {"id": "password"}, "expected": "private"}
        )
    original_evaluate = browser.evaluate

    def change_anchor_after_geometry(expression, **kwargs):
        result = original_evaluate(expression, **kwargs)
        if "const rect=" in expression:
            original_evaluate(
                "document.querySelectorAll('.order-id')[1].textContent='recycled-order'"
            )
        return result

    with monkeypatch.context() as patch:
        patch.setattr(browser, "evaluate", change_anchor_after_geometry)
        with pytest.raises(ValueError, match="identity or business scope"):
            browser.act(
                {
                    "op": "click",
                    "target": {
                        "selector": ".details",
                        "scope": "#orders",
                        "anchor": {"selector": ".order-id", "text": "order-2"},
                    },
                }
            )
    browser.evaluate("document.querySelector('#overlay').style.display='block'")
    with pytest.raises(ValueError, match="query failed"):
        browser.act({"op": "click", "target": {"id": "covered"}})


def test_real_browser_plan_evidence_and_proxy(root, server):
    if not chromium_path():
        pytest.skip("Installed Chromium required")
    base, _calls = server
    spec = {
        "allowed_origins": [base],
        "start_url": base + "/html",
        "steps": [
            {"op": "fill", "target": {"id": "username"}, "value": "Native"},
            {"op": "click", "target": {"role": "button", "name": "Sign in"}},
            {
                "op": "expect",
                "target": {"id": "status"},
                "assert": "text",
                "expected": "Hello Native",
            },
            {"op": "screenshot", "target": {"id": "status"}},
        ],
    }
    result = execute_test_plan(root, "suite", plan("web", spec))
    assert result["verdict"] == "passed", result
    assert result["metrics"]["model_calls"] == 0 and result["metrics"]["cdp_calls"] > 0
    assert result["evidence"][0]["clip"]["width"] > 0
    assert (
        EngineStore(root, "suite")
        .evidence_path(result["run_id"], result["evidence"][0]["name"])
        .read_bytes()
        .startswith(b"\x89PNG")
    )
    # A second real local origin must receive no redirected or subresource traffic.
    received = []

    class Blocked(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            received.append(self.path)
            self.send_response(200)
            self.end_headers()

    blocked = ThreadingHTTPServer(("127.0.0.1", 0), Blocked)
    threading.Thread(target=blocked.serve_forever, daemon=True).start()
    browser = BrowserSession(root, "suite", spec, RunControl(timeout_seconds=20))
    try:
        response = browser.evaluate(
            f"fetch({json.dumps(base + '/safe-mutation')},{{method:'POST',headers:{{'content-type':'application/json'}},body:JSON.stringify({{name:'proxy-body'}})}}).then(r=>r.json())"
        )
        assert response["id"] == "order-1" and _calls[-1] == (
            "/safe-mutation",
            {"name": "proxy-body"},
        )
        destination = f"http://127.0.0.1:{blocked.server_port}/private"
        browser.evaluate(f"fetch({json.dumps(destination)}).catch(()=>null)")
        browser.evaluate(
            f"new Promise(resolve=>{{const i=new Image();i.onload=i.onerror=()=>resolve(true);i.src={json.dumps(destination)};}})"
        )
        assert not received
        assert browser.proxy.blocked >= 2
    finally:
        browser.close()
        blocked.shutdown()
        blocked.server_close()


def test_security_identical_credentials_with_different_trace_headers_rejected(
    root, server
):
    base, calls = server
    spec = {
        "allowed_origins": [base],
        "identities": {
            "owner": {"Authorization": "Bearer same", "X-Trace": "one"},
            "other": {"Authorization": "Bearer same", "X-Trace": "two"},
        },
        "checks": [
            {
                "id": "auth",
                "rule": "authorization",
                "request": {"url": base},
                "owner": "owner",
                "challenger": "other",
                "marker": {"path": "$.id", "expected": "order-1"},
            }
        ],
    }
    result = execute_test_plan(root, "suite", plan("security", spec))
    assert result["verdict"] == "inconclusive" and not calls
    assert "equivalent" in result["error"]


def test_native_isolation_mode_does_not_silently_downgrade(root, server):
    config = load_config(root)
    config["sandbox"]["mode"] = "process"
    save_config(root, config)
    result = execute_test_plan(
        root,
        "suite",
        plan("api", {"allowed_origins": [server[0]], "requests": [{"url": server[0]}]}),
    )
    assert result["verdict"] == "passed" and server[1], result
    assert result["metrics"]["isolation"]["mode"] == "process"
