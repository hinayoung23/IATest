from __future__ import annotations

import copy
import json
import sqlite3
from dataclasses import replace

import pytest

from ai_apitest.agents.identity import default_role_registry
from ai_apitest.agents.runtime import AgentRuntime
from ai_apitest.agents.token_optimization import canonical, pack_evidence, unpack_evidence
from ai_apitest.ai.provider import MODEL_HTTP_ATTEMPTS, ModelResponse, ModelToolCall, TokenUsage
from ai_apitest.application.incremental_audit import incremental_bundle
from ai_apitest.application.quality_audit import DIMENSIONS, RUBRIC_VERSION, QualityAuditService
from ai_apitest.application.case_generation import plan_batches
from ai_apitest.core.config import load_config, save_config
from ai_apitest.core.models import Assertion, Endpoint, TestCase as Case
from ai_apitest.core.serde import to_jsonable
from ai_apitest.data.agent_runs import AgentRunRepository
from ai_apitest.data.repositories import ProjectDataAccess
from ai_apitest.data.token_usage import token_statistics
from ai_apitest.tools.builtin import build_case_generation_tool_registry
from ai_apitest.tools.registry import ToolExecutionContext, ToolRegistry


def settings(root, *, enabled=True):
    config = load_config(root)
    for role in ("master", "case_generator", "analyzer"):
        config["llm"][role]["enabled"] = role == "master"
    config["llm"]["master"].update(model="judge", base_url="https://judge.invalid/v1", api_key_env="TEST_JUDGE")
    config["agents"]["token_optimization"]["enabled"] = enabled
    save_config(root, config)
    return config


def cases_and_evidence(count=20):
    schema = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean", "description": "可信契约说明。" * 100}}}
    endpoints = [Endpoint(method="GET", path=f"/items-{i}", parameters=[{"in": "query", "name": "page", "schema": {"type": "integer", "minimum": 1}}], responses={"200": {"content": {"application/json": {"schema": schema}}}}) for i in range(count)]
    cases = [Case(id=f"c-{i}", title=f"接口 {i} 正常响应", method="GET", path=endpoint.path, query={"page": 1}, assertions=[Assertion(type="status_code", expected=200), Assertion(type="json_schema", expected=schema)]) for i, endpoint in enumerate(endpoints)]
    return to_jsonable(cases), {"endpoints": to_jsonable(endpoints), "trusted_contract": {"contract_hash": "same-contract", "contract_version": "1", "contract_status": "trusted"}, "coverage_manifest": {"total": count}, "local_gate_issues": []}


def bundle(cases, evidence):
    return {"artifact_type": "test_cases", "artifact": cases, "evidence": evidence, "rubric_version": RUBRIC_VERSION}


def baseline(cases, evidence, *, depth=0):
    return {"candidate_id": "approved-baseline", "judge_key": "same-judge", "dimensions": dict.fromkeys(DIMENSIONS, 95), "bundle": bundle(cases, evidence), "depth": depth}


class JudgeGateway:
    calls = []

    def __init__(self, config):
        self.model_config = {**config, "supports_tools": True}
        self.turn = 0

    def complete(self, *, messages, tools, **kwargs):
        self.turn += 1
        type(self).calls.append((copy.deepcopy(messages), copy.deepcopy(tools)))
        prefetched = any("[框架已读取的完整事实]" in str(message.get("content")) for message in messages)
        if not prefetched and self.turn <= 2:
            name = "get_audit_artifact_info" if self.turn == 1 else "read_audit_artifact_chunk"
            args = {} if self.turn == 1 else {"offset": 0, "max_chars": 180000}
        else:
            name, args = "submit_quality_review", {"verdict": "approved", "confidence": "high", "dimension_scores": dict.fromkeys(DIMENSIONS, 96), "summary": "契约一致，覆盖完整", "issues": [], "suggestions": [], "coverage_gaps": []}
        MODEL_HTTP_ATTEMPTS.set(MODEL_HTTP_ATTEMPTS.get() + 1)
        return ModelResponse(tool_calls=[ModelToolCall(str(self.turn), name, args)], usage=TokenUsage(100, 20, 120, 50, 0, 10))


def test_usage_subsets_do_not_double_count():
    raw = {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130, "prompt_tokens_details": {"cached_tokens": 70}, "completion_tokens_details": {"reasoning_tokens": 20}}
    usage = TokenUsage.from_openai(raw)
    assert (usage.total_tokens, usage.cached_input_tokens, usage.reasoning_tokens) == (130, 70, 20)
    assert usage.raw_usage == raw
    anthropic = TokenUsage.from_anthropic({"input_tokens": 10, "cache_read_input_tokens": 70, "cache_creation_input_tokens": 20, "output_tokens": 30})
    assert (anthropic.input_tokens, anthropic.total_tokens) == (100, 130)
    usage.add(anthropic)
    assert (usage.total_tokens, usage.cached_input_tokens, usage.cache_write_tokens) == (260, 140, 20)


@pytest.mark.parametrize("raw", [None, [], {"prompt_tokens": "oops", "prompt_tokens_details": []}, {"input_tokens": -5, "completion_tokens_details": "bad"}])
def test_malformed_usage_is_safe(raw):
    assert TokenUsage.from_openai(raw).total_tokens == 0
    assert TokenUsage.from_anthropic(raw).total_tokens == 0


def test_fact_dictionary_is_lossless_and_smaller():
    cases, evidence = cases_and_evidence()
    original = bundle(cases, evidence)
    packed, metrics = pack_evidence(original)
    assert unpack_evidence(packed) == original
    assert metrics["fact_count"] > 0
    assert metrics["wire_chars"] < metrics["original_chars"] * 0.6


@pytest.mark.parametrize("value", [{"$iatest_fact": "user-content"}, {"unique": "small"}, {"a": {"$iatest_fact": "x"}, "b": "large" * 500}])
def test_packing_preserves_reserved_keys_and_small_inputs(value):
    packed, metrics = pack_evidence(value)
    assert packed == value
    assert metrics["format"] == "json"


def test_incremental_closure_includes_shared_fixtures_and_removed_cases():
    cases, evidence = cases_and_evidence()
    cases[0]["fixture_requirements"] = ["shared-fixture"]
    cases[1]["fixture_requirements"] = ["shared-fixture"]
    changed = copy.deepcopy(cases)
    changed[0]["query"]["page"] = 2
    delta, scope = incremental_bundle(bundle(changed, evidence), baseline(cases, evidence))
    assert scope["affected_ids"] == ["c-0", "c-1"]
    assert scope["reused_cases"] == 18
    assert len(delta["global_case_index"]) == 20
    removed = cases[1:]
    delta, scope = incremental_bundle(bundle(removed, evidence), baseline(cases, evidence))
    assert scope["deleted_ids"] == ["c-0"]
    assert scope["affected_ids"] == ["c-0", "c-1"]


@pytest.mark.parametrize("change", ["contract", "rubric", "large", "duplicate_ids", "duplicate_execution", "untrusted", "depth", "unknown", "unknown_field", "connected"])
def test_incremental_unsafe_scopes_fall_back_to_full(change):
    cases, evidence = cases_and_evidence()
    prior = baseline(cases, evidence)
    candidate = copy.deepcopy(bundle(cases, evidence))
    candidate["artifact"][0]["query"]["page"] = 2
    if change == "contract":
        candidate["evidence"]["trusted_contract"]["contract_hash"] = "changed"
    elif change == "rubric":
        candidate["rubric_version"] = "new-rubric"
    elif change == "large":
        for case in candidate["artifact"][:6]:
            case["title"] = "changed"
    elif change == "duplicate_ids":
        candidate["artifact"][1]["id"] = "c-0"
    elif change == "duplicate_execution":
        candidate["artifact"][1] = {**copy.deepcopy(candidate["artifact"][0]), "id": "c-1"}
    elif change == "untrusted":
        candidate["evidence"]["trusted_contract"]["contract_status"] = "fallback"
    elif change == "depth":
        prior["depth"] = 3
    elif change == "unknown":
        candidate["artifact"][0]["steps"] = ["unknown-step"]
    elif change == "unknown_field":
        candidate["artifact"][0]["future_dependency"] = "c-1"
    else:
        for case in candidate["artifact"]:
            case["resources"] = ["global-resource"]
        prior["bundle"]["artifact"] = copy.deepcopy(candidate["artifact"])
        candidate["artifact"][0]["title"] = "changed"
    assert incremental_bundle(candidate, prior) is None


def test_prefetch_saves_model_turns_and_records_provenance(tmp_path):
    settings(tmp_path)
    JudgeGateway.calls = []
    service = QualityAuditService(tmp_path, "demo", "test_cases", gateway_factory=JudgeGateway)
    cases, evidence = cases_and_evidence()
    result = service.evaluate(cases, evidence=evidence, round_index=1)
    assert result.passed
    assert len(JudgeGateway.calls) == 1
    with AgentRunRepository(tmp_path, "demo").connection() as conn:
        names = [row[0] for row in conn.execute("select name from agent_tool_calls where run_id=? and status='prefetched' order by id", (result.scores[0].agent_run_id,))]
    assert names == ["get_audit_artifact_info", "read_audit_artifact_chunk"]
    stats = token_statistics(tmp_path, "demo")
    role = stats["roles"][0]
    assert (role["model_calls"], role["prefetched_tool_calls"], role["http_attempts"]) == (1, 2, 1)
    assert role["cached_input_tokens"] == 50 and role["reasoning_tokens"] == 10
    assert stats["unknown_cost_calls"] == 0


def test_approved_baseline_reused_and_scores_remain_conservative(tmp_path):
    settings(tmp_path)
    cases, evidence = cases_and_evidence()
    class ChangingScore(JudgeGateway):
        score = 95
        def complete(self, **kwargs):
            response = super().complete(**kwargs)
            if response.tool_calls[0].name == "submit_quality_review":
                response.tool_calls[0].arguments["dimension_scores"] = dict.fromkeys(DIMENSIONS, self.score)
            return response
    service = QualityAuditService(tmp_path, "demo", "test_cases", gateway_factory=ChangingScore)
    first = service.evaluate(cases, evidence=evidence, round_index=1)
    assert first.passed
    ChangingScore.score = 99
    cases[0]["query"]["page"] = 2
    second = service.evaluate(cases, evidence=evidence, round_index=2)
    assert second.passed and second.scores[0].audit_scope["mode"] == "incremental"
    assert second.scores[0].audit_scope["reused_cases"] == 19
    assert second.scores[0].dimensions == first.scores[0].dimensions
    config = load_config(tmp_path)
    config["quality_audit"]["threshold"] = 97
    save_config(tmp_path, config)
    replacement = QualityAuditService(tmp_path, "demo", "test_cases", gateway_factory=JudgeGateway)
    assert replacement.models[0].key != service.models[0].key
    third = replacement.evaluate(cases, evidence=evidence, round_index=1)
    assert not third.scores[0].cached and third.scores[0].audit_scope["mode"] == "full"


def test_exact_cache_keeps_scope_and_role_changes_invalidate_it(tmp_path):
    from ai_apitest.agents.identity import RoleRegistry
    settings(tmp_path)
    cases, evidence = cases_and_evidence()
    JudgeGateway.calls = []
    service = QualityAuditService(tmp_path, "demo", "test_cases", gateway_factory=JudgeGateway)
    first = service.evaluate(cases, evidence=evidence, round_index=1)
    again = service.evaluate(cases, evidence=evidence, round_index=2)
    assert first.passed and again.passed and again.scores[0].cached
    assert len(JudgeGateway.calls) == 1
    assert again.scores[0].audit_scope == first.scores[0].audit_scope
    roles = default_role_registry()
    changed = RoleRegistry([replace(roles.resolve("quality_reviewer"), version="new-version")])
    replacement = QualityAuditService(tmp_path, "demo", "test_cases", gateway_factory=JudgeGateway, role_registry=changed)
    assert replacement.models[0].key != service.models[0].key


@pytest.mark.parametrize("confidence,issues", [("low", []), ("high", [{"severity": "low", "dimension": "reasonableness", "location": "c-0", "evidence": "示例", "reason": "建议", "suggested_fix": "复核"}])])
def test_low_confidence_or_issues_cannot_create_incremental_baseline(tmp_path, confidence, issues):
    settings(tmp_path)
    class Ineligible(JudgeGateway):
        def complete(self, **kwargs):
            response = super().complete(**kwargs)
            if response.tool_calls[0].name == "submit_quality_review":
                response.tool_calls[0].arguments.update(confidence=confidence, issues=issues)
            return response
    service = QualityAuditService(tmp_path, "demo", "test_cases", gateway_factory=Ineligible)
    cases, evidence = cases_and_evidence()
    assert service.evaluate(cases, evidence=evidence, round_index=1).passed
    assert service.repository.get_baseline(service.models[0].key, "test_cases") is None


def test_nonapproved_verdict_never_creates_baseline(tmp_path):
    settings(tmp_path)
    class Nonapproved(JudgeGateway):
        def complete(self, **kwargs):
            response = super().complete(**kwargs)
            if response.tool_calls[0].name == "submit_quality_review":
                response.tool_calls[0].arguments["verdict"] = "needs_revision"
            return response
    service = QualityAuditService(tmp_path, "demo", "test_cases", gateway_factory=Nonapproved)
    cases, evidence = cases_and_evidence()
    service.evaluate(cases, evidence=evidence, round_index=1)
    assert service.repository.get_baseline(service.models[0].key, "test_cases") is None


def test_changed_local_gate_warnings_invalidate_exact_audit_cache(tmp_path):
    settings(tmp_path)
    service = QualityAuditService(tmp_path, "demo", "test_cases", gateway_factory=JudgeGateway)
    cases, evidence = cases_and_evidence()
    service.evaluate(cases, evidence=evidence, round_index=1)
    second = service.evaluate(cases, evidence=evidence, round_index=2, hard_gate_issues=[{"severity": "low", "reason": "新的诊断证据", "location": "c-0"}])
    assert not second.scores[0].cached


def test_redaction_does_not_certify_unchanged_secrets_or_share_exact_cache(tmp_path):
    settings(tmp_path)
    cases, evidence = cases_and_evidence()
    service = QualityAuditService(tmp_path, "demo", "test_cases", gateway_factory=JudgeGateway)
    cases[0]["headers"] = {"Authorization": "Bearer synthetic-first-value"}
    first = service.evaluate(cases, evidence=evidence, round_index=1)
    assert first.passed
    assert service.repository.get_baseline(service.models[0].key, "test_cases") is None
    cases[0]["headers"]["Authorization"] = "Bearer synthetic-second-value"
    second = service.evaluate(cases, evidence=evidence, round_index=2)
    assert not second.scores[0].cached and second.scores[0].audit_scope["mode"] == "full"
    with service.repository.connection() as conn:
        persisted = "\n".join(row[0] for row in conn.execute("select artifact_json from quality_artifact_candidates"))
    assert "synthetic-first-value" not in persisted and "synthetic-second-value" not in persisted


def test_hard_gate_failure_creates_no_baseline_or_model_call(tmp_path):
    settings(tmp_path)
    JudgeGateway.calls = []
    service = QualityAuditService(tmp_path, "demo", "test_cases", gateway_factory=JudgeGateway)
    cases, evidence = cases_and_evidence()
    result = service.evaluate(cases, evidence=evidence, round_index=1, hard_gate_issues=[{"severity": "critical", "reason": "状态断言与契约冲突"}])
    assert not result.passed and not JudgeGateway.calls
    assert service.repository.get_baseline(service.models[0].key, "test_cases") is None


def test_prefetch_disable_and_capacity_fallback(tmp_path):
    config = settings(tmp_path, enabled=False)
    JudgeGateway.calls = []
    service = QualityAuditService(tmp_path, "demo", "test_cases", gateway_factory=JudgeGateway)
    cases, evidence = cases_and_evidence()
    assert service.evaluate(cases, evidence=evidence, round_index=1).passed
    assert len(JudgeGateway.calls) == 3
    config["agents"]["token_optimization"].update(enabled=True, prefetch_max_tokens=1024)
    save_config(tmp_path, config)
    JudgeGateway.calls = []
    service = QualityAuditService(tmp_path, "demo", "test_cases", gateway_factory=JudgeGateway)
    assert service.evaluate(cases, evidence=evidence, round_index=1).passed
    assert len(JudgeGateway.calls) == 3


def test_prefetch_never_executes_read_tool_registered_as_write(tmp_path):
    settings(tmp_path)
    registry = ToolRegistry()
    builtins = build_case_generation_tool_registry()
    for spec in builtins.all():
        registry.register(replace(spec, side_effect="write") if spec.name == "read_audit_artifact_chunk" else spec)
    JudgeGateway.calls = []
    service = QualityAuditService(tmp_path, "demo", "test_cases", gateway_factory=JudgeGateway, tool_registry=registry)
    cases, evidence = cases_and_evidence()
    assert service.evaluate(cases, evidence=evidence, round_index=1).passed
    assert len(JudgeGateway.calls) == 3


def test_failed_model_call_is_unknown_cost_and_usage_estimates_are_marked(tmp_path):
    settings(tmp_path)
    class FailingGateway:
        model_config = {"model": "failed", "default_output_tokens": 3000}
        def complete(self, **kwargs):
            MODEL_HTTP_ATTEMPTS.set(2)
            raise RuntimeError("simulated network failure")
    context = ToolExecutionContext(tmp_path, "demo", ProjectDataAccess.create(tmp_path, "demo", endpoints=[]), {"audit_artifact_text": "{}"})
    runtime = AgentRuntime(roles=default_role_registry(), tools=build_case_generation_tool_registry(), gateway=FailingGateway(), audit=AgentRunRepository(tmp_path, "demo"))
    with pytest.raises(RuntimeError, match="network"):
        runtime.run(role_id="quality_reviewer", task_prompt="审计", tool_context=context)
    stats = token_statistics(tmp_path, "demo")
    assert (stats["failed_model_calls"], stats["unknown_cost_calls"], stats["model_http_attempts"]) == (1, 1, 2)
    assert stats["roles"][0]["http_retries"] == 1


def test_statistics_does_not_create_or_migrate_db(tmp_path):
    assert token_statistics(tmp_path, "absent")["runs"] == 0
    from ai_apitest.core.paths import project_agent_db
    db = project_agent_db(tmp_path, "old")
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as conn:
        conn.execute("create table agent_runs(id text, project text, role_id text, provider text, model text, started_at text, input_tokens integer, output_tokens integer, total_tokens integer, turn_count integer)")
        conn.execute("insert into agent_runs values('r', 'old', 'quality_reviewer', 'old', 'old', '2026', 10, 2, 12, 3)")
    original = db.read_bytes()
    stats = token_statistics(tmp_path, "old")
    assert stats["roles"][0]["legacy_runs"] == 1
    assert stats["roles"][0]["model_calls"] == 3
    assert original == db.read_bytes()


def test_batching_uses_serialized_contract_size_and_preserves_scenarios():
    endpoints = [Endpoint(method="GET", path=f"/{i}", description="文档" * 2000) for i in range(4)]
    plan = [{"scenario_id": str(i), "endpoint_method": "GET", "endpoint_path": f"/{i}"} for i in range(4)]
    fixed = plan_batches(plan, endpoints, 8, 8)
    adaptive = plan_batches(plan, endpoints, 8, 8, input_token_limit=5000)
    assert len(fixed) == 1 and len(adaptive) > 1
    assert [item["scenario_id"] for group, _ in adaptive for item in group] == [str(i) for i in range(4)]
    assert all({item["endpoint_path"] for item in group} <= {endpoint.path for endpoint in selected} for group, selected in adaptive)


def test_complete_contract_without_business_increment_skips_optional_planner(tmp_path):
    from ai_apitest.application.case_generation import generate_cases_with_agent_runtime
    config = settings(tmp_path)
    config["agents"]["planner_on_complete_contract"] = False
    save_config(tmp_path, config)
    calls = []
    def gateway(config):
        calls.append(config)
        raise AssertionError("完整确定性场景不应调用模型")
    cases, _ = generate_cases_with_agent_runtime(tmp_path, [Endpoint(method="GET", path="/health", responses={"200": {}})], gateway_factory=gateway, record_lessons=False)
    assert cases and not calls


@pytest.mark.parametrize("required,field_schema,expected_text,valid", [
    (True, {"type": "string", "const": "inventory"}, 'service="inventory"', True),
    (True, {"type": "string", "enum": ["inventory"]}, 'service="inventory"', True),
    (True, {"type": "string"}, "返回 service 字段", True),
    (False, {"type": "string", "const": "inventory"}, 'service="inventory"', False),
    (True, {"type": "string"}, 'service="inventory"', False),
    (True, {"type": "string", "enum": ["inventory", "orders"]}, 'service="inventory"', False),
    (True, {"type": "string", "const": "orders"}, 'service="inventory"', False),
    (True, {"type": "boolean", "const": True}, "service=1", False),
])
def test_required_schema_field_is_a_real_assertion(required, field_schema, expected_text, valid):
    from ai_apitest.domain.case_drafts import validate_final_assertions
    schema = {"type": "object", "properties": {"service": field_schema}, "required": ["service"] if required else []}
    endpoint = Endpoint(method="GET", path="/health", responses={"200": {"content": {"application/json": {"schema": schema}}}})
    assertions = [Assertion(type="status_code", expected=200), Assertion(type="json_schema", expected=schema)]
    errors = validate_final_assertions("s", endpoint, assertions, semantic_text=expected_text)
    assert not any(issue.code.startswith("expected_field") for issue in errors) if valid else any(issue.code.startswith("expected_field") for issue in errors)


def test_live_fixture_passes_the_actual_local_gate():
    import runpy
    from pathlib import Path
    fixture = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/validate_token_optimization.py"))["fixture"]
    from ai_apitest.core.serde import case_from_dict
    from ai_apitest.domain.case_drafts import validate_compiled_cases
    endpoints, cases, _ = fixture()
    assert not [issue for issue in validate_compiled_cases([case_from_dict(case) for case in cases], endpoints) if issue.severity == "error"]


def test_prefetch_resume_retains_facts_and_does_not_repeat_reads(tmp_path):
    settings(tmp_path)
    class Gateway(JudgeGateway):
        fail = True
        def complete(self, **kwargs):
            if self.fail:
                raise RuntimeError("interrupted after prefetch")
            return super().complete(**kwargs)
    tools = build_case_generation_tool_registry()
    data = ProjectDataAccess.create(tmp_path, "demo", endpoints=[])
    audit = AgentRunRepository(tmp_path, "demo")
    def execute(resume=False):
        gateway = Gateway({"model": "judge", "default_output_tokens": 6000})
        runtime = AgentRuntime(roles=default_role_registry(), tools=tools, gateway=gateway, audit=audit)
        context = ToolExecutionContext(tmp_path, "demo", data, {"audit_artifact_text": '{"fact":"stable"}'})
        return runtime.run(role_id="quality_reviewer", task_prompt="审计", tool_context=context, session_id="prefetch-resume", resume=resume)
    with pytest.raises(RuntimeError, match="interrupted"):
        execute()
    Gateway.fail = False
    result = execute(resume=True)
    assert result.artifact["verdict"] == "approved"
    with audit.connection() as conn:
        assert conn.execute("select count(*) from agent_tool_calls where status='prefetched'").fetchone()[0] == 2
    assert any("stable" in message.get("content", "") for message in result.messages)


def test_prefetch_is_charged_to_task_tool_budget(tmp_path):
    from ai_apitest.agents.policy import TaskBudget, TaskBudgetExceeded, task_budget
    settings(tmp_path)
    JudgeGateway.calls = []
    context = ToolExecutionContext(tmp_path, "demo", ProjectDataAccess.create(tmp_path, "demo", endpoints=[]), {"audit_artifact_text": "{}"})
    runtime = AgentRuntime(roles=default_role_registry(), tools=build_case_generation_tool_registry(), gateway=JudgeGateway({"model": "judge"}), audit=AgentRunRepository(tmp_path, "demo"))
    with task_budget(tmp_path, budget=TaskBudget(max_tool_calls=1)):
        with pytest.raises(TaskBudgetExceeded, match="工具调用预算"):
            runtime.run(role_id="quality_reviewer", task_prompt="审计", tool_context=context)
    assert not JudgeGateway.calls
    assert token_statistics(tmp_path, "demo")["roles"][0]["prefetched_tool_calls"] == 1
