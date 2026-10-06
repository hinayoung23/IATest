from __future__ import annotations

from dataclasses import asdict
import json
import threading
import time
from xml.etree import ElementTree

import httpx
import pytest
from typer.testing import CliRunner

from ai_apitest.cli import app
from ai_apitest.core.config import load_config, save_config
from ai_apitest.core.models import Assertion, TestCase as Case, TestStep as Step, CaseResult
from ai_apitest.core.redaction import redact
from ai_apitest.core.sampling import sample_from_schema
from ai_apitest.core.schema import schema_errors
from ai_apitest.core.serde import write_json
from ai_apitest.core.paths import project_metadata_file, project_docs_file
from ai_apitest.discovery.openapi import parse_openapi
from ai_apitest.execution.assertions import evaluate
from ai_apitest.execution.auth import AuthManager
from ai_apitest.execution.control import ExecutionRuntime, RunControl, activate
from ai_apitest.execution.datasets import expand_cases, seeded_variables
from ai_apitest.execution.fixtures import FixtureManager
from ai_apitest.execution.options import RunOptions
from ai_apitest.execution.plugins import PluginRegistry
from ai_apitest.execution.runner import execute_step, run_ordered_cases, run_cases
from ai_apitest.generator.cases import save_cases
from ai_apitest.report.export import execution_metrics, write_json_results, write_junit
from ai_apitest.report.markdown import render_report_markdown
from ai_apitest.report.storage import latest_run_metadata, latest_case_results


ENV = {"name": "test", "base_url": "https://test.invalid", "headers": {}}


def case(identifier="C1", **kwargs):
    return Case(id=identifier, title=identifier, method="GET", path="/data", **kwargs)


@pytest.mark.parametrize("schema", [
    {"type": "integer", "minimum": 10, "maximum": 20, "multipleOf": 3},
    {"type": "number", "exclusiveMinimum": 10, "exclusiveMaximum": 11},
    {"type": "integer", "minimum": -10, "maximum": -1},
    {"type": "string", "pattern": "^[A-Z]{3}[0-9]{2}$"},
    {"type": "string", "minLength": 20, "maxLength": 20},
    {"type": "string", "format": "email"},
    {"type": "string", "format": "uuid"},
    {"type": "array", "minItems": 3, "uniqueItems": True, "items": {"type": "integer"}},
    {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer", "minimum": 20}, "secret": {"type": "string", "readOnly": True}}},
    {"oneOf": [{"type": "integer", "minimum": 10}, {"type": "null"}]},
    {"allOf": [{"type": "integer", "minimum": 10}, {"maximum": 15}]},
    {"type": "string", "nullable": True, "enum": [None, "ok"]},
])
def test_generated_request_respects_constraints(schema):
    value = sample_from_schema(schema)
    assert not schema_errors(value, schema)


def test_impossible_request_schema_fails_with_actionable_error():
    with pytest.raises(ValueError, match="example|数据集"):
        sample_from_schema({"type": "integer", "minimum": 20, "maximum": 10})


def test_schema_dialects_and_recursive_openapi_contract():
    draft4 = {"$schema": "http://json-schema.org/draft-04/schema#", "type": "integer", "minimum": 10, "exclusiveMinimum": True}
    assert schema_errors(10, draft4)
    assert not schema_errors(11, draft4)
    document = {"openapi": "3.1.0", "paths": {"/nodes": {"get": {"responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Node"}}}}}}}}, "components": {"schemas": {"Node": {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}, "child": {"$ref": "#/components/schemas/Node"}}}}}}
    schema = parse_openapi(document)[0].responses["200"]["content"]["application/json"]["schema"]
    assert not schema_errors({"id": 1, "child": {"id": 2}}, schema)
    assert schema_errors({"id": 1, "child": {"id": "bad"}}, schema)
    assert not schema_errors(sample_from_schema(schema), schema)


def test_complete_schema_assertions_and_missing_headers():
    schema = {"type": "object", "required": ["value"], "additionalProperties": False, "properties": {"value": {"type": "integer", "minimum": 10}}}
    assertion = Assertion("json_schema", expected=schema)
    assert evaluate(assertion, {"json": {"value": 20}}) is None
    for body in ({}, {"value": 1}, {"value": 20, "extra": True}):
        assert evaluate(assertion, {"json": body})
    assert evaluate(Assertion("header", "X-Missing", "exists"), {"headers": {}})
    assert evaluate(Assertion("header", "X-Trace", "eq", "ok"), {"headers": {"x-trace": "ok"}}) is None
    assert evaluate(Assertion("response_time", operator="lte", expected=50), {"elapsed_ms": 10}) is None


def test_secrets_removed_from_structured_raw_and_failure_evidence(tmp_path):
    secret = "sample-sensitive-value"
    result = CaseResult("C", "C", "failed", "GET", "/", 200, 1,
        error=f"unexpected {secret}", assertion_errors=[f"actual {secret}"],
        request={"headers": {"Authorization": f"Bearer {secret}"}},
        response={"json": {"token": secret}, "text": json.dumps({"token": secret})})
    assert secret not in json.dumps(redact(asdict(result)))
    assert secret not in render_report_markdown([result])
    write_json_results(tmp_path / "results.json", [result], {"seed": "abc"})
    write_junit(tmp_path / "results.xml", [result], metadata={"seed": "abc"})
    assert secret not in (tmp_path / "results.json").read_text()
    assert secret not in (tmp_path / "results.xml").read_text()
    assert ElementTree.parse(tmp_path / "results.xml").getroot().get("failures") == "1"
    assert redact({"token": secret, "input_tokens": 15})["input_tokens"] == 15


def test_dataset_expansion_and_shards_are_stable_and_isolated(tmp_path):
    (tmp_path / "data.csv").write_text("_id,name\na,Alice\nb,Bob\n")
    base = case(dataset="users", variables={"tenant": "common"}, query={"name": "{{name}}"})
    expanded = expand_cases([base], tmp_path, {"datasets": {"users": {"source": "data.csv"}}})
    assert [item.id for item in expanded] == ["C1[a]", "C1[b]"]
    assert expanded[0].variables == {"tenant": "common", "name": "Alice"}
    expanded[0].variables["tenant"] = "changed"
    assert expanded[1].variables["tenant"] == base.variables["tenant"] == "common"
    cases = [case(f"C{i}") for i in range(50)]
    shards = [RunOptions(shard_index=i, shard_count=4).select(cases) for i in range(1, 5)]
    assert sorted(item.id for shard in shards for item in shard) == sorted(item.id for item in cases)
    assert RunOptions().select(expanded, {"C1"}) == expanded
    assert seeded_variables("seed", "C1") == seeded_variables("seed", "C1")
    assert seeded_variables("seed", "C1") != seeded_variables("seed", "C2")


def test_cookie_and_variable_isolation_keeps_chain_session():
    observed = []
    def handler(request):
        observed.append((request.url.path, request.headers.get("cookie", "")))
        return httpx.Response(200, json={"ok": True}, headers={"Set-Cookie": "session=chain; Path=/"})
    chain = case("chain", steps=[Step("create", "GET", "/create"), Step("read", "GET", "/read")])
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = run_ordered_cases([chain, case("other")], ENV, {"execution": {"parallel": 1}}, client)
        assert not client.cookies
    assert [item.status for item in results] == ["passed", "passed"]
    assert observed == [("/create", ""), ("/read", "session=chain"), ("/data", "")]


def test_polling_stops_when_assertion_succeeds_and_blocks_mutations():
    count = 0
    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(200, json={"ready": count >= 3})
    step = Step("wait", "GET", "/", assertions=[Assertion("json_path", "$.ready", expected=True)], poll={"interval_seconds": 0.001, "timeout_seconds": 1})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = execute_step(step, ENV, {}, {}, client)
        assert result["status"] == "passed"
        assert result["request"]["poll_attempts"] == 3
        blocked = execute_step(Step("write", "POST", "/", poll=step.poll), ENV, {}, {}, client)
        assert blocked["request"]["blocked"]
        assert count == 3


def test_total_deadline_still_runs_cleanup_and_stops_pending_cases():
    paths = []
    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/data":
            time.sleep(0.02)
        return httpx.Response(200, json={"ready": False})
    slow = case(poll={"timeout_seconds": 1, "interval_seconds": 0.01}, assertions=[Assertion("json_path", "$.ready", expected=True)], cleanup_steps=[Step("clean", "DELETE", "/cleanup")])
    with httpx.Client(transport=httpx.MockTransport(handler)) as client, activate(ExecutionRuntime(control=RunControl(timeout_seconds=0.01))):
        results = run_ordered_cases([slow, case("pending")], ENV, {}, client)
    assert [item.status for item in results] == ["cancelled", "cancelled"]
    assert paths == ["/data", "/cleanup"]
    assert results[0].cleanup["status"] == "passed"


def test_retries_keep_first_failure_and_do_not_retry_writes():
    counts = {}
    def handler(request):
        name = request.headers["X-Case"]
        counts[name] = counts.get(name, 0) + 1
        return httpx.Response(500 if counts[name] == 1 else 200)
    read = case("read", headers={"X-Case": "read"}, assertions=[Assertion("status_code", expected=200)])
    write = Case("write", "write", "POST", "/data", headers={"X-Case": "write"}, assertions=[Assertion("status_code", expected=200)])
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = run_ordered_cases([read, write], ENV, {"execution": {"failure_retries": 1}}, client)
    assert results[0].flaky and results[0].status == "passed"
    assert results[0].attempt_history[0]["status_code"] == 500
    assert "500" in render_report_markdown(results)
    assert results[1].status == "failed" and counts["write"] == 1


def test_resource_exclusion_with_parallel_execution():
    lock = threading.Lock()
    active = maximum = 0
    def handler(request):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.005)
        with lock:
            active -= 1
        return httpx.Response(200)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = run_ordered_cases([case(f"C{i}", resources=["account"]) for i in range(8)], ENV, {"execution": {"parallel": 4}}, client)
    assert maximum == 1
    assert all(item.status == "passed" for item in results)


def test_oauth_cache_refresh_identity_and_explicit_authorization():
    calls = []
    tokens = 0
    def handler(request):
        nonlocal tokens
        calls.append((request.url.path, request.headers.get("authorization", "")))
        if request.url.path == "/token":
            tokens += 1
            return httpx.Response(200, json={"access_token": f"token-{tokens}", "expires_in": 300, "refresh_token": "refresh"})
        return httpx.Response(401 if request.headers.get("authorization") == "Bearer token-1" else 200)
    env = {**ENV, "auth": {"default_identity": "user", "identities": {"user": {"type": "oauth2", "token_url": "/token"}, "admin": {"token": "admin"}}}}
    with httpx.Client(transport=httpx.MockTransport(handler)) as client, activate(ExecutionRuntime(control=RunControl(), auth=AuthManager(env))):
        results = run_ordered_cases([case(), case("second"), case("admin", identity="admin"), case("negative", headers={"authorization": "invalid"})], env, {}, client)
    assert tokens == 2
    assert all(item.status == "passed" for item in results)
    assert ("/data", "Bearer admin") in calls and ("/data", "invalid") in calls


def test_partial_fixture_failure_runs_cleanup_for_created_resource():
    paths = []
    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={"resource": "created"})
    config = {"fixtures": {"providers": {"prepare": {"enabled": True, "type": "http", "prepare": {"url": "https://test.invalid/create", "extract": {"resource": "$.resource", "missing": "$.absent"}}, "cleanup": {"url": "https://test.invalid/cleanup/{{resource}}", "method": "DELETE"}}}}}
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        manager = FixtureManager(config, client)
        context = {}
        setup, session = manager.prepare(case(fixture_requirements=["prepare"]), context)
        cleanup = manager.cleanup(session, context)
    assert setup["status"] == "failed" and cleanup["status"] == "passed"
    assert paths == ["/create", "/cleanup/created"]


def test_plugin_hooks_assertion_and_fixture_lifecycle():
    events = []
    class Fixture:
        def prepare(self, config, context, client):
            events.append("prepare")
            return {"values": {"id": 7}, "cleanup_state": 7}
        def cleanup(self, state, context, client):
            assert state == 7
            events.append("cleanup")
    plugins = PluginRegistry()
    plugins.register_fixture("example", Fixture())
    plugins.register_assertion("matches", lambda assertion, response, context: None if context["id"] == response["json"]["id"] else "id mismatch")
    plugins.before_request.append(lambda step, headers, kwargs, context: events.append("before"))
    plugins.after_response.append(lambda step, response, context: events.append("after"))
    config = {"fixtures": {"providers": {"seed": {"type": "plugin", "enabled": True, "adapter": "example"}}}}
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"id": 7}))) as client, activate(ExecutionRuntime(control=RunControl(), plugins=plugins)):
        results = run_ordered_cases([case(fixture_requirements=["seed"], assertions=[Assertion("custom", "matches")])], ENV, config, client)
    assert results[0].status == "passed"
    assert events == ["prepare", "before", "after", "cleanup"]


def test_selected_environment_provenance_and_ci_exit_codes(tmp_path, monkeypatch):
    import ai_apitest.execution.runner as runner
    original_client = httpx.Client
    urls = []
    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(500, json={"token": "private-token"})
    def client_factory(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(handler))
        return original_client(**kwargs)
    monkeypatch.setattr(runner.httpx, "Client", client_factory)
    config = load_config(tmp_path)
    config["environments"] = {"test": {"base_url": "https://chosen.invalid", "headers": {}}}
    config["default_environment"] = "test"
    save_config(tmp_path, config)
    write_json(project_metadata_file(tmp_path, "demo"), {"base_url": "https://imported.invalid"})
    write_json(project_docs_file(tmp_path, "demo"), [])
    save_cases(tmp_path, [case(assertions=[Assertion("status_code", expected=200)])], project="demo")
    results = run_cases(tmp_path, project="demo", options=RunOptions(seed="fixed"))
    assert results[0].status == "failed" and urls == ["https://chosen.invalid/data"]
    metadata = latest_run_metadata(tmp_path, "demo")
    assert metadata["seed"] == "fixed" and metadata["cases_hash"]
    assert latest_case_results(tmp_path, "demo")[results[0].case_id]["status"] == "failed"
    monkeypatch.chdir(tmp_path)
    output = CliRunner().invoke(app, ["run", "--project", "demo", "--no-ai", "--junit", "results.xml", "--json-output", "results.json"])
    assert output.exit_code == 1, output.output
    assert (tmp_path / "results.xml").exists()
    assert "private-token" not in (tmp_path / "results.json").read_text(encoding="utf-8")
    empty = CliRunner().invoke(app, ["run", "--project", "empty", "--no-ai"])
    assert empty.exit_code == 5, empty.output
    allowed = CliRunner().invoke(app, ["run", "--project", "empty", "--no-ai", "--allow-empty"])
    assert allowed.exit_code == 0, allowed.output


def test_execution_metrics_do_not_count_skips_as_failures():
    results = [CaseResult("C1", "C1", "passed", "GET", "/", 200, 10), CaseResult("C2", "C2", "skipped", "GET", "/", None, 0)]
    metrics = execution_metrics(results)
    assert metrics["execution_pass_rate"] == 1.0
    assert metrics["skip_rate"] == 0.5


def test_anonymous_case_does_not_inherit_default_auth_and_named_identity_wins():
    headers = []
    def handler(request):
        headers.append(request.headers.get("authorization"))
        return httpx.Response(200)
    env = {**ENV, "headers": {"Authorization": "Bearer legacy"}, "auth": {"default_identity": "user", "identities": {"user": {"token": "user"}, "admin": {"token": "admin"}}}}
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = run_ordered_cases([case("anonymous", headers={"Authorization": None}), case("admin", identity="admin"), case("legacy")], env, {}, client)
    assert all(item.status == "passed" for item in results)
    assert headers == [None, "Bearer admin", "Bearer legacy"]


@pytest.mark.parametrize("body,valid", [(b"null", True), (b"not json", False), (b"", False)])
def test_schema_assertions_distinguish_json_null_from_invalid_response(body, valid):
    from ai_apitest.execution.runner import parse_response
    result = evaluate(Assertion("json_schema", expected={"type": "null"}), parse_response(200, {}, body))
    assert (result is None) is valid


@pytest.mark.parametrize("flags,exit_code", [([], 5), (["--allow-all-skipped"], 0)])
def test_ci_all_skipped_policy(tmp_path, monkeypatch, flags, exit_code):
    save_config(tmp_path, load_config(tmp_path))
    save_cases(tmp_path, [case(runnable=False, risk="fixture_required", fixture_requirements=["not_configured"])], project="demo")
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["run", "--project", "demo", "--no-ai", *flags])
    assert result.exit_code == exit_code, result.output


def test_junit_removes_invalid_xml_control_characters(tmp_path):
    result = CaseResult("C\x00", "title\x01", "failed", "GET", "/", 500, 1, error="error\x08")
    write_junit(tmp_path / "junit.xml", [result])
    root = ElementTree.parse(tmp_path / "junit.xml").getroot()
    assert root.get("failures") == "1"


def test_redaction_removes_credentials_from_url_embedded_in_errors():
    assert "password" not in redact({"error": "Request failed at https://user:password@example.invalid/data"})["error"]
