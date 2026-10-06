from __future__ import annotations

import copy
import json
from pathlib import Path
import threading

import httpx
import pytest

from ai_apitest.core.config import load_config, save_config
from ai_apitest.core.models import Endpoint, TestCase as Case, Assertion, CaseResult
from ai_apitest.core.paths import project_metadata_file
from ai_apitest.core.serde import write_json, case_from_dict
from ai_apitest.core.schema import schema_errors
from ai_apitest.core.redaction import redact
from ai_apitest.data.generation_cache import IncrementalGenerationCache
from ai_apitest.generator.cases import generate_cases
from ai_apitest.domain.contracts import normalize_business_rules
from ai_apitest.execution.fixture_checks import check_fixtures
from ai_apitest.execution.runner import run_ordered_cases
from ai_apitest.execution.options import RunOptions
from ai_apitest.context.storage import save_execution_trend, trend_overview, assess_trend_comparability
from ai_apitest.report.storage import save_run
from ai_apitest.orchestrator.maintainer.change_detector import endpoint_signature
from ai_apitest.web.task_store import create_task, request_cancel, claim_task, list_tasks
from ai_apitest.web.server import run_task_by_id


def endpoint(path):
    return Endpoint("GET", path, responses={"200": {}})


def test_business_rule_changes_invalidate_endpoint_signature():
    original = endpoint("/a")
    changed = copy.deepcopy(original)
    changed.business_rules = [{"description": "TTL is 1800 seconds"}]
    assert endpoint_signature(original) != endpoint_signature(changed)


def test_project_specific_inference_is_scoped_and_has_no_assumed_ttl():
    text = "同一个合法前缀已经存在时刷新邮箱创建时间"
    generic = normalize_business_rules([{"description": text}], method="GET", path="/accounts/create")
    assert not generic[0]["trigger"]["chain_steps"]
    scoped = normalize_business_rules([{"description": text}], method="GET", path="/api/generate")
    assertions = scoped[0]["trigger"]["chain_steps"][-1]["assertions"]
    assert not any(item.get("expected") == 600 for item in assertions)


def test_cache_invalidates_dependencies_but_reuses_independent_endpoints(tmp_path):
    endpoints = [endpoint("/a"), endpoint("/b"), endpoint("/c")]
    graph = {"edges": [{"from": {"method": "GET", "path": "/a"}, "to": {"method": "GET", "path": "/b"}}]}
    write_json(project_metadata_file(tmp_path, "demo"), {"dependency_graph": graph, "contract_hash": "original"})
    config = load_config(tmp_path)
    cache = IncrementalGenerationCache(tmp_path, "demo", endpoints, config, {})
    cache.save(generate_cases(endpoints))
    changed = copy.deepcopy(endpoints)
    changed[1].description = "changed business semantics"
    write_json(project_metadata_file(tmp_path, "demo"), {"dependency_graph": graph, "contract_hash": "changed"})
    reused, missing = IncrementalGenerationCache(tmp_path, "demo", changed, config, {}).load()
    assert {case.path for case in reused} == {"/c"}
    assert {endpoint.path for endpoint in missing} == {"/a", "/b"}


def test_incremental_workflow_regenerates_only_changed_scope(tmp_path, monkeypatch):
    import ai_apitest.ai.workflows as workflows
    config = load_config(tmp_path)
    config["quality_audit"]["enabled"] = False
    save_config(tmp_path, config)
    monkeypatch.setattr(workflows, "ensure_trusted_contract_for_generation", lambda root, project, endpoints, **kwargs: (endpoints, ""))
    original = workflows._generate_cases_without_cache
    calls = []
    def generate(root, endpoints, *args, **kwargs):
        calls.append([item.path for item in endpoints])
        return original(root, endpoints, *args, **kwargs)
    monkeypatch.setattr(workflows, "_generate_cases_without_cache", generate)
    endpoints = [endpoint("/a"), endpoint("/b")]
    first, _ = workflows.generate_cases_with_config(tmp_path, endpoints, project="demo")
    assert {case.path for case in first} == {"/a", "/b"}
    endpoints[1].description = "changed"
    second, message = workflows.generate_cases_with_config(tmp_path, endpoints, project="demo")
    assert len(second) == len(first) and "复用" in message
    assert calls == [["/a", "/b"], ["/b"]]
    workflows.generate_cases_with_config(tmp_path, endpoints, project="demo", force_refresh=True)
    assert calls[-1] == ["/a", "/b"]


def test_fixture_check_is_read_only_until_probe_or_exercise():
    calls = []
    def handler(request):
        calls.append(request.method)
        return httpx.Response(200)
    config = {"fixtures": {"providers": {"seed": {"type": "http", "enabled": True, "prepare": {"url": "https://test.invalid/prepare"}, "check": {"url": "https://test.invalid/health"}}}}}
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert check_fixtures(config, [], client)[0]["status"] == "configured"
        assert not calls
        assert check_fixtures(config, [], client, probe=True)[0]["status"] == "passed"
        assert calls == ["GET"]
        config["fixtures"]["providers"]["seed"]["check"]["method"] = "POST"
        assert check_fixtures(config, [], client, probe=True)[0]["status"] == "failed"
        assert calls == ["GET"]


def test_fixture_examples_control_time_mail_and_clean_up_over_real_http():
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "examples" / "test_support_server.py"
    spec = importlib.util.spec_from_file_location("iatest_support_demo", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = module.SupportServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        template = (Path(__file__).resolve().parents[1] / "examples" / "fixture-providers.json").read_text()
        config = json.loads(template.replace(":8799", f":{server.server_port}"))
        config["execution"] = {"random_seed": "fixture-regression"}
        cases = [
            Case("time", "time", "GET", "/mailboxes/{{mailbox_id}}", fixture_requirements=["time_control"], assertions=[Assertion("json_path", "$.expired", expected=True)]),
            Case("mail", "mail", "GET", "/mailboxes/{{mailbox_id}}", fixture_requirements=["mail_injection"], assertions=[Assertion("json_path", "$.messages[0].subject", expected="iatest fixture")]),
        ]
        # Templates belong in path_params; path templates are intentionally unsupported.
        for case in cases:
            case.path = "/mailboxes/{mailbox_id}"
            case.path_params = {"mailbox_id": "{{mailbox_id}}"}
        with httpx.Client(trust_env=False) as client:
            results = run_ordered_cases(cases, config["environments"]["local"], config, client)
        assert [result.status for result in results] == ["passed", "passed"], [result.error for result in results]
        assert not server.states
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_queued_and_running_tasks_can_be_cancelled(tmp_path):
    queued = create_task(tmp_path, "demo", "run", "run", resumable=False)
    assert request_cancel(tmp_path, "demo", queued["id"])
    assert claim_task(tmp_path, "demo", queued["id"]) is None
    assert list_tasks(tmp_path, "demo")[0]["status"] == "cancelled"
    running = create_task(tmp_path, "demo", "run", "run", resumable=False)
    def target(progress):
        request_cancel(tmp_path, "demo", running["id"])
        progress("should stop before persistence")
        pytest.fail("cancelled task continued")
    run_task_by_id(tmp_path, "demo", running["id"], target)
    assert next(task for task in list_tasks(tmp_path, "demo") if task["id"] == running["id"])["status"] == "cancelled"


def test_trend_excludes_skipped_latency_and_rejects_changed_environment(tmp_path):
    results = [CaseResult("C1", "C1", "passed", "GET", "/", 200, 50), CaseResult("C2", "C2", "skipped", "GET", "/", None, 0)]
    run_id = save_run(tmp_path, results, "demo", metadata={"environment": "test", "cases_hash": "v1"})
    save_execution_trend(tmp_path, "demo", run_id, results)
    trend = trend_overview(tmp_path, "demo")["runs"][0]
    assert trend["pass_rate"] == 1.0
    assert trend["latency_percentiles"]["p50"] == 50
    assert trend["metrics"]["skipped"] == 1
    changed = copy.deepcopy(trend)
    changed["metadata"]["environment"] = "production"
    assert not assess_trend_comparability(changed, trend)["comparable"]


def test_schema_definition_survives_redaction_and_remote_refs_fail_closed():
    payload = {"schema": {"type": "object", "properties": {"token": {"type": "string"}, "password": {"type": "string"}}}, "response": {"text": '{"token":123456}'}}
    cleaned = redact(payload)
    assert cleaned["schema"]["properties"]["password"] == {"type": "string"}
    assert "123456" not in cleaned["response"]["text"]
    assert schema_errors({}, {"$ref": "https://cannot-fetch.invalid/schema.json"})


def test_new_safety_flags_require_boolean_values(tmp_path):
    from ai_apitest.execution.datasets import expand_cases
    with pytest.raises(ValueError, match="布尔"):
        expand_cases([case_from_dict({"id": "C1", "title": "C1", "method": "GET", "path": "/", "retry_safe": "false"})], tmp_path, {})
    with pytest.raises(ValueError):
        RunOptions(shard_index=0, shard_count=2)


def test_corrupt_incremental_cache_is_treated_as_a_miss(tmp_path):
    cache = IncrementalGenerationCache(tmp_path, "demo", [endpoint("/a")], load_config(tmp_path), {})
    cache.path.parent.mkdir(parents=True, exist_ok=True)
    cache.path.write_text("not json")
    reused, missing = IncrementalGenerationCache(tmp_path, "demo", [endpoint("/a")], load_config(tmp_path), {}).load()
    assert not reused and [item.path for item in missing] == ["/a"]


def test_distinct_identities_and_dataset_inputs_are_not_deduplicated():
    from ai_apitest.domain.case_drafts import case_execution_signature, case_request_signature
    first = Case("C1", "C1", "GET", "/data", query={"id": "{{id}}"}, variables={"id": 1})
    second = copy.deepcopy(first)
    second.variables["id"] = 2
    assert case_execution_signature(first) != case_execution_signature(second)
    second.variables = first.variables
    second.identity = "admin"
    assert case_request_signature(first) != case_request_signature(second)


def test_web_edit_preserves_and_updates_execution_options():
    from ai_apitest.web.server import case_from_form, render_case_edit_dialog
    existing = Case("C1", "C1", "GET", "/data", expected_result="返回200", variables={"id": 1}, identity="reader")
    form = {"title": ["changed"], "expected_result": ["返回200"], "runnable": ["on"], "assertions": [json.dumps([{"type": "status_code", "expected": 200}])], "request_data": [json.dumps({"identity": "admin", "variables": {"id": 2}, "poll": {"timeout_seconds": 3}})]}
    edited = case_from_form(existing, form)
    assert edited.identity == "admin" and edited.variables == {"id": 2}
    assert edited.poll == {"timeout_seconds": 3}
    assert "admin" in render_case_edit_dialog(edited, "edit", "delete", "demo")


def test_execution_credit_is_invalidated_when_cases_or_contracts_change(tmp_path):
    from ai_apitest.report.storage import current_run_results
    from ai_apitest.execution.provenance import run_metadata
    case = Case("C1", "C1", "GET", "/a")
    write_json(project_metadata_file(tmp_path, "demo"), {})
    metadata = run_metadata(tmp_path, "demo", {"base_url": "https://test.invalid"}, [case], RunOptions(), "seed")
    save_run(tmp_path, [CaseResult("C1", "C1", "passed", "GET", "/a", 200, 1)], "demo", metadata=metadata)
    assert current_run_results(tmp_path, "demo", [case])["C1"]["status"] == "passed"
    changed = copy.deepcopy(case)
    changed.query = {"limit": 10}
    assert not current_run_results(tmp_path, "demo", [changed])
    from ai_apitest.core.paths import project_docs_file
    write_json(project_docs_file(tmp_path, "demo"), [{"method": "GET", "path": "/a", "description": "changed"}])
    assert not current_run_results(tmp_path, "demo", [case])


@pytest.mark.parametrize("schema_type", ["string", ["string", "null"]])
def test_schema_sensitive_examples_are_redacted_without_losing_field_types(schema_type):
    secret = "private-example-credential"
    payload = {"schema": {"properties": {"api_key": {"type": schema_type, "example": secret}, "token": {"type": schema_type, "anyOf": [{"enum": [secret]}]}}}, "error": f"actual {secret}", "token_value": secret}
    result = redact(payload)
    assert secret not in json.dumps(result)
    assert result["schema"]["properties"]["api_key"]["type"] == schema_type
