from __future__ import annotations

import copy
import json
import re
from typing import Any

import pytest

from ai_apitest.agents.identity import RoleRegistry, RoleSpec, UnknownRoleError, default_role_registry
from ai_apitest.agents.runtime import (
    AgentBudgetExceeded,
    AgentRuntime,
    compact_agent_history,
    uses_kimi_thinking_protocol,
)
from ai_apitest.ai.model_gateway import ProviderModelGateway, parse_tool_envelope
from ai_apitest.ai.provider import (
    ModelResponse,
    ModelToolCall,
    TokenUsage,
    parse_anthropic_tool_calls,
    parse_openai_tool_calls,
    to_anthropic_messages,
)
from ai_apitest.application.case_generation import (
    PLANNER_TOOLS,
    apply_plan_contract_fields,
    dedupe_cases,
    designer_tools_for_plan,
    fallback_draft,
    generate_cases_with_agent_runtime,
    plan_batches,
    supplement_cases_with_agent_runtime,
)
from ai_apitest.application.discovery import (
    discover_endpoints_with_agent_runtime,
    generate_dependency_graph_with_agent_runtime,
)
from ai_apitest.application.case_maintenance import maintain_cases_with_agent_runtime
from ai_apitest.application.quality_audit import (
    QualityAuditService,
    calibrate_review_result,
    review_rubric_violations,
    unique_audit_models,
)
from ai_apitest.application.result_analysis import result_cleanup_status, skipped_case_summary
from ai_apitest.ai.workflows import case_generation_strategy_context
from ai_apitest.core.config import load_config, save_config
from ai_apitest.core.models import Assertion, CaseResult, Endpoint, TestCase as ApiTestCase, TestStep as ApiTestStep
from ai_apitest.core.paths import project_docs_file
from ai_apitest.core.serde import to_jsonable, write_json
from ai_apitest.data.agent_runs import AgentRunRepository, redact_sensitive, redact_text
from ai_apitest.data.repositories import ProjectDataAccess
from ai_apitest.domain.case_drafts import compile_scenario_drafts, normalize_chain_step, validate_compiled_cases
from ai_apitest.domain.coverage import (
    business_rule_plan_item,
    fill_deterministic_coverage_gaps,
    normalize_coverage_plan,
)
from ai_apitest.domain.contracts import (
    assertion_satisfies_obligation,
    contract_catalog_issues,
    coverage_manifest_issues,
    merge_endpoint_contract,
    normalize_business_rules,
    normalize_endpoint_contract,
)
from ai_apitest.discovery.document import parse_api_document
from ai_apitest.execution.assertions import evaluate
from ai_apitest.generator.cases import attach_setup_steps, load_cases_from_markdown, save_cases
from ai_apitest.tools.builtin import build_case_generation_tool_registry
from ai_apitest.tools.registry import (
    ToolExecutionContext,
    ToolExecutionResult,
    ToolInputError,
    ToolPermissionError,
    ToolRegistry,
    ToolSpec,
)
from ai_apitest.web.server import llm_role_from_form, render_config


def test_role_registry_is_versioned_and_fails_closed() -> None:
    registry = default_role_registry()
    tools = build_case_generation_tool_registry()
    planner = registry.resolve("test_planner")
    assert planner.version == "1.7"
    assert "submit_coverage_plan" in planner.allowed_tools
    assert "get_endpoint_detail" in planner.allowed_tools
    assert "read_task_evidence" in planner.allowed_tools
    assert "不执行接口" in planner.identity_text
    with pytest.raises(UnknownRoleError):
        registry.resolve("master_with_all_permissions")
    for role in registry.all():
        assert tools.definitions_for(role.id, role.allowed_tools)
    assert registry.resolve("case_designer").max_tool_rounds == 10
    assert registry.resolve("result_analyst").version == "1.3"
    assert registry.resolve("result_analyst").token_budget == 48000


def test_tool_registry_enforces_role_and_schema(tmp_path) -> None:
    endpoints = [Endpoint(method="GET", path="/health", summary="健康检查", responses={"200": {}})]
    data = ProjectDataAccess.create(tmp_path, "demo", endpoints=endpoints)
    context = ToolExecutionContext(tmp_path, "demo", data)
    tools = build_case_generation_tool_registry()
    planner = default_role_registry().resolve("test_planner")

    result = tools.execute(
        role_id=planner.id,
        allowed_tools=planner.allowed_tools,
        name="list_endpoints",
        arguments={"limit": 1},
        context=context,
    )
    assert json.loads(result.content)["items"][0]["path"] == "/health"

    with pytest.raises(ToolPermissionError):
        tools.execute(
            role_id=planner.id,
            allowed_tools=planner.allowed_tools,
            name="submit_case_drafts",
            arguments={"drafts": []},
            context=context,
        )

    with pytest.raises(ToolInputError):
        tools.execute(
            role_id=planner.id,
            allowed_tools=planner.allowed_tools,
            name="list_endpoints",
            arguments={"limit": 0},
            context=context,
        )


def test_discovery_requires_complete_document_read_before_submission(tmp_path) -> None:
    tools = build_case_generation_tool_registry()
    role = default_role_registry().resolve("api_analyst")
    context = ToolExecutionContext(
        tmp_path,
        "demo",
        ProjectDataAccess.create(tmp_path, "demo", endpoints=[]),
        state={"document_text": "x" * 2000},
    )
    submission = {
        "endpoints": [{"method": "GET", "path": "/health", "summary": "健康检查"}]
    }
    rejected = tools.execute(
        role_id=role.id,
        allowed_tools=role.allowed_tools,
        name="submit_endpoint_drafts",
        arguments=submission,
        context=context,
    )
    assert rejected.is_error
    assert "document_not_fully_read" in rejected.content

    for offset in (0, 1000):
        tools.execute(
            role_id=role.id,
            allowed_tools=role.allowed_tools,
            name="read_document_chunk",
            arguments={"offset": offset, "max_chars": 1000},
            context=context,
        )
    accepted = tools.execute(
        role_id=role.id,
        allowed_tools=role.allowed_tools,
        name="submit_endpoint_drafts",
        arguments=submission,
        context=context,
    )
    assert accepted.terminal and not accepted.is_error


def test_discovery_submission_accumulates_batches_until_inventory_is_complete(tmp_path) -> None:
    tools = build_case_generation_tool_registry()
    role = default_role_registry().resolve("api_analyst")
    context = ToolExecutionContext(
        tmp_path,
        "demo",
        ProjectDataAccess.create(tmp_path, "demo", endpoints=[]),
        state={
            "document_text": "document",
            "expected_endpoint_keys": [
                {"method": "GET", "path": "/one"},
                {"method": "POST", "path": "/two"},
            ],
        },
    )
    tools.execute(
        role_id=role.id,
        allowed_tools=role.allowed_tools,
        name="read_document_chunk",
        arguments={"offset": 0, "max_chars": 1000},
        context=context,
    )
    partial = tools.execute(
        role_id=role.id,
        allowed_tools=role.allowed_tools,
        name="submit_endpoint_drafts",
        arguments={"endpoints": [{"method": "GET", "path": "/one", "summary": "One"}]},
        context=context,
    )
    assert partial.is_error
    assert "POST /two" in partial.content

    completed = tools.execute(
        role_id=role.id,
        allowed_tools=role.allowed_tools,
        name="submit_endpoint_drafts",
        arguments={"endpoints": [{"method": "POST", "path": "/two", "summary": "Two"}]},
        context=context,
    )
    assert completed.terminal and not completed.is_error
    assert [(item["method"], item["path"]) for item in completed.value] == [
        ("GET", "/one"),
        ("POST", "/two"),
    ]


def test_quality_audit_reader_advances_contiguously_and_does_not_repeat_evidence(tmp_path) -> None:
    tools = build_case_generation_tool_registry()
    role = default_role_registry().resolve("quality_reviewer")
    context = ToolExecutionContext(
        tmp_path,
        "demo",
        ProjectDataAccess.create(tmp_path, "demo", endpoints=[]),
        state={"audit_artifact_text": "evidence" * 20_000},
    )
    first = tools.execute(
        role_id=role.id,
        allowed_tools=role.allowed_tools,
        name="read_audit_artifact_chunk",
        arguments={"offset": 99_999, "max_chars": 1_000},
        context=context,
    )
    assert first.value["offset"] == 0
    assert first.value["next_offset"] == 60_000
    second = tools.execute(
        role_id=role.id,
        allowed_tools=role.allowed_tools,
        name="read_audit_artifact_chunk",
        arguments={"offset": 0, "max_chars": 180_000},
        context=context,
    )
    assert second.value["offset"] == 60_000
    assert second.value["has_more"] is False
    assert context.state["forced_terminal_tool"] == "submit_quality_review"
    repeated = tools.execute(
        role_id=role.id,
        allowed_tools=role.allowed_tools,
        name="read_audit_artifact_chunk",
        arguments={"offset": 0, "max_chars": 180_000},
        context=context,
    )
    assert repeated.value["already_fully_read"] is True
    assert repeated.value["content"] == ""


def test_semantic_drafts_compile_and_gate_endpoint_status_and_variables() -> None:
    producer = Endpoint(
        method="POST",
        path="/users",
        summary="创建用户",
        request_body={
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["phone"],
                        "properties": {"phone": {"type": "string"}, "name": {"type": "string"}},
                    }
                }
            }
        },
        responses={"201": {"schema": {"type": "object", "properties": {"id": {"type": "string"}}}}, "422": {}},
    )
    consumer = Endpoint(
        method="GET",
        path="/users/{id}",
        summary="查询用户",
        parameters=[{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
        responses={"200": {}},
    )
    drafts = [
        {
            "scenario_id": "SCN-USER-001",
            "endpoint_method": "POST",
            "endpoint_path": "/users",
            "intent": "缺少必填手机号",
            "design_method": "required_field",
            "omit_fields": ["body.phone"],
            "expected_outcome": "返回参数校验错误",
            "expected_basis": "responses.422",
        },
        {
            "scenario_id": "SCN-CHAIN-001",
            "endpoint_method": "CHAIN",
            "endpoint_path": "",
            "intent": "创建用户后查询",
            "design_method": "dependency_chain",
            "expected_outcome": "创建结果中的 id 可用于查询用户",
            "chain_steps": [
                {"method": "POST", "path": "/users", "extract": {"id": "$.id"}},
                {"method": "GET", "path": "/users/{id}", "path_params": {"id": "{{id}}"}},
            ],
        },
    ]
    result = compile_scenario_drafts(
        drafts,
        [producer, consumer],
        expected_scenario_ids=["SCN-USER-001", "SCN-CHAIN-001"],
    )
    assert result.accepted
    required = next(item for item in result.cases if item.scenario_id == "SCN-USER-001")
    assert "phone" not in required.body
    assert required.assertions[0].expected == 422
    chain = next(item for item in result.cases if item.method == "CHAIN")
    assert chain.steps[1].path_params == {"id": "{{id}}"}

    invalid = compile_scenario_drafts(
        [
            {
                "scenario_id": "SCN-BAD",
                "endpoint_method": "GET",
                "endpoint_path": "/missing",
                "intent": "幻觉接口",
                "design_method": "happy_path",
                "expected_outcome": "成功",
            }
        ],
        [producer, consumer],
        expected_scenario_ids=["SCN-BAD"],
    )
    assert any(issue.code == "unknown_endpoint" for issue in invalid.error_issues)


def test_model_assertion_dialects_are_compiled_to_executable_assertions() -> None:
    endpoint = Endpoint(method="GET", path="/generate", responses={"200": {}})
    compiled = compile_scenario_drafts(
        [
            {
                "scenario_id": "SCN-ASSERT-001",
                "endpoint_method": "GET",
                "endpoint_path": "/generate",
                "intent": "验证响应字段",
                "design_method": "happy_path",
                "expected_outcome": "返回邮箱信息",
                "expected_basis": "responses.200 — 文档成功响应",
                "assertions": [
                    {"type": "status_code", "expected": 200},
                    {"type": "json_field_exists", "path": "email"},
                    {"type": "json_path", "path": "$.email", "assertion": "is_string"},
                    {"type": "json_path", "path": "$.email", "assertion": "matches", "pattern": ".*@example\\.com$"},
                    {"type": "json_path", "path": "$.expires_in", "assertion": "is_integer"},
                ],
            }
        ],
        [endpoint],
        expected_scenario_ids=["SCN-ASSERT-001"],
    )
    assert compiled.accepted
    assertions = compiled.cases[0].assertions
    response = {"status_code": 200, "json": {"email": "demo@example.com", "expires_in": 600}}
    assert all(evaluate(item, response) is None for item in assertions)


def test_success_boundary_and_trusted_read_only_path_case_remain_runnable() -> None:
    boundary_endpoint = Endpoint(
        method="GET",
        path="/generate",
        parameters=[
            {
                "name": "prefix",
                "in": "query",
                "required": False,
                "schema": {"type": "string", "maxLength": 50},
            }
        ],
        responses={"200": {}},
    )
    missing_endpoint = Endpoint(
        method="GET",
        path="/check/{email}",
        parameters=[
            {
                "name": "email",
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
            }
        ],
        responses={"200": {}},
    )
    delete_endpoint = Endpoint(
        method="DELETE",
        path="/delete/{email}",
        parameters=[
            {
                "name": "email",
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
            }
        ],
        responses={"404": {}},
    )
    compiled = compile_scenario_drafts(
        [
            {
                "scenario_id": "SCN-BOUNDARY-SUCCESS",
                "endpoint_method": "GET",
                "endpoint_path": "/generate",
                "intent": "合法前缀边界成功",
                "design_method": "boundary_value",
                "request_overrides": {"query": {"prefix": "x" * 50}},
                "expected_basis": "responses.200",
                "expected_outcome": "返回成功响应",
                "runnable": True,
            },
            {
                "scenario_id": "SCN-MISSING-READ",
                "endpoint_method": "GET",
                "endpoint_path": "/check/{email}",
                "intent": "查询从未创建的邮箱",
                "design_method": "dependency_chain",
                "request_overrides": {
                    "path_params": {"email": "missing-{{uuid}}@invalid.test"}
                },
                "expected_basis": "responses.200",
                "expected_outcome": "返回不存在状态",
                "runnable": True,
            },
            {
                "scenario_id": "SCN-MISSING-DELETE",
                "endpoint_method": "DELETE",
                "endpoint_path": "/delete/{email}",
                "intent": "删除从未创建的邮箱",
                "design_method": "dependency_chain",
                "request_overrides": {
                    "path_params": {"email": "missing-{{uuid}}@invalid.test"}
                },
                "expected_basis": "responses.404",
                "expected_outcome": "不存在邮箱返回 404",
                "runnable": True,
            },
        ],
        [boundary_endpoint, missing_endpoint, delete_endpoint],
        expected_scenario_ids=[
            "SCN-BOUNDARY-SUCCESS",
            "SCN-MISSING-READ",
            "SCN-MISSING-DELETE",
        ],
    )

    assert compiled.accepted
    boundary = next(item for item in compiled.cases if item.scenario_id == "SCN-BOUNDARY-SUCCESS")
    missing = next(item for item in compiled.cases if item.scenario_id == "SCN-MISSING-READ")
    missing_delete = next(
        item for item in compiled.cases if item.scenario_id == "SCN-MISSING-DELETE"
    )
    assert boundary.negative is False
    assert boundary.runnable is True and boundary.risk == "none"
    assert missing.runnable is True and missing.risk == "none"
    assert missing_delete.runnable is True and missing_delete.risk == "none"


def test_chain_deduplication_ignores_display_only_step_names_and_merges_lineage() -> None:
    first = ApiTestCase(
        id="TC-CHAIN-0001",
        title="删除活动邮箱",
        method="CHAIN",
        path="",
        steps=[
            ApiTestStep(
                name="生成邮箱",
                method="GET",
                path="/generate",
                extract={"email": "$.email"},
            ),
            ApiTestStep(
                name="删除活动邮箱",
                method="DELETE",
                path="/delete/{email}",
                path_params={"email": "{{email}}"},
                assertions=[Assertion(type="status_code", expected=200)],
            ),
        ],
        rule_ids=["delete-active"],
    )
    second = copy.deepcopy(first)
    second.id = "TC-CHAIN-0002"
    second.title = "校验删除成功响应"
    second.steps[0].name = "准备真实邮箱"
    second.steps[1].name = "检查删除响应格式"
    second.rule_ids = ["delete-response"]

    result = dedupe_cases([], [first, second])

    assert len(result) == 1
    assert result[0].rule_ids == ["delete-active", "delete-response"]


def test_chain_prefix_is_merged_only_when_all_shared_steps_match() -> None:
    short = ApiTestCase(
        id="TC-CHAIN-0001",
        title="生成并续期",
        method="CHAIN",
        path="",
        steps=[
            ApiTestStep(
                name="生成",
                method="GET",
                path="/generate",
                assertions=[Assertion(type="status_code", expected=200)],
                extract={"email": "$.email"},
            ),
            ApiTestStep(
                name="续期",
                method="POST",
                path="/extend/{email}",
                path_params={"email": "{{email}}"},
                assertions=[Assertion(type="status_code", expected=200)],
            ),
        ],
        rule_ids=["extend-active"],
    )
    long = copy.deepcopy(short)
    long.id = "TC-CHAIN-0002"
    long.title = "生成、续期并检查状态"
    long.rule_ids = ["extend-postcondition"]
    long.steps.append(
        ApiTestStep(
            name="检查",
            method="GET",
            path="/check/{email}",
            path_params={"email": "{{email}}"},
            assertions=[Assertion(type="status_code", expected=200)],
        )
    )

    merged = dedupe_cases([], [short, long])

    assert len(merged) == 1
    assert merged[0].rule_ids == ["extend-postcondition", "extend-active"]

    different = copy.deepcopy(long)
    different.steps[1].assertions = [Assertion(type="status_code", expected=404)]
    not_merged = dedupe_cases([], [short, different])

    assert len(not_merged) == 2


def test_same_request_cases_merge_compatible_assertions_and_rule_lineage() -> None:
    first = ApiTestCase(
        id="TC-GEN-0001",
        title="生成邮箱并验证域名",
        method="GET",
        path="/generate",
        assertions=[
            Assertion(type="status_code", expected=200),
            Assertion(
                type="json_path",
                target="$.email",
                operator="ends_with",
                expected="@example.test",
            ),
        ],
        rule_ids=["fixed-domain"],
    )
    second = copy.deepcopy(first)
    second.id = "TC-GEN-0002"
    second.title = "生成随机八位邮箱前缀"
    second.assertions = [
        Assertion(type="status_code", expected=200),
        Assertion(
            type="json_path",
            target="$.email",
            operator="matches",
            expected=r"^[A-Za-z0-9]{8}@example\.test$",
        ),
    ]
    second.rule_ids = ["random-prefix"]

    result = dedupe_cases([], [first, second])

    assert len(result) == 1
    assert result[0].rule_ids == ["fixed-domain", "random-prefix"]
    assert len(result[0].assertions) == 3


def test_negative_case_normalization_adds_negative_tag() -> None:
    case = ApiTestCase(
        id="TC-API-0001",
        title="不存在资源返回 404",
        method="GET",
        path="/missing",
        negative=True,
        tags=["status_code_coverage"],
    )

    from ai_apitest.core.case_schema import normalize_test_case

    normalized = normalize_test_case(case)

    assert "negative" in normalized.tags


def test_same_request_merge_prefers_scenario_title_over_purpose_description() -> None:
    purpose = ApiTestCase(
        id="TC-GEN-0001",
        title="生成临时邮箱 - 用于创建一个新的临时邮箱，邮箱域名固定为 mail.tempbox.cn。",
        method="GET",
        path="/generate",
        assertions=[Assertion(type="status_code", expected=200)],
    )
    scenario = copy.deepcopy(purpose)
    scenario.id = "TC-GEN-0002"
    scenario.title = "生成临时邮箱 - 不传 prefix 时随机生成 8 位邮箱并验证固定域名"

    result = dedupe_cases([], [purpose, scenario])

    assert len(result) == 1
    assert result[0].title == scenario.title


def test_same_request_merge_removes_implied_string_assertions() -> None:
    first = ApiTestCase(
        id="TC-GEN-0001",
        title="生成邮箱并验证域名",
        method="GET",
        path="/generate",
        assertions=[
            Assertion(type="status_code", expected=200),
            Assertion(
                type="json_path",
                target="$.email",
                operator="is_string",
                expected=True,
            ),
            Assertion(
                type="json_path",
                target="$.email",
                operator="matches",
                expected=r"^[^@]+@example\.test$",
            ),
        ],
    )
    second = copy.deepcopy(first)
    second.id = "TC-GEN-0002"
    second.assertions = [
        Assertion(type="status_code", expected=200),
        Assertion(
            type="json_path",
            target="$.email",
            operator="matches",
            expected=r"^[A-Za-z0-9]{8}@example\.test$",
        ),
    ]

    result = dedupe_cases([], [first, second])

    assert len(result) == 1
    assert [
        (item.type, item.target, item.operator)
        for item in result[0].assertions
    ] == [
        ("status_code", "", "eq"),
        ("json_path", "$.email", "matches"),
    ]
    assert assertion_satisfies_obligation(
        result[0].assertions[1],
        {
            "type": "json_path",
            "target": "$.email",
            "operator": "matches",
            "expected": r"^[^@]+@example\.test$",
        },
    )
    assert assertion_satisfies_obligation(
        Assertion(
            type="json_path",
            target="$.email",
            operator="eq",
            expected="abc12345@example.test",
        ),
        {
            "type": "json_path",
            "target": "$.email",
            "operator": "matches",
            "expected": r"^[^@]+@example\.test$",
        },
    )
    assert not assertion_satisfies_obligation(
        Assertion(
            type="json_path",
            target="$.email",
            operator="matches",
            expected=r"^.*@example\.test$",
        ),
        {
            "type": "json_path",
            "target": "$.email",
            "operator": "matches",
            "expected": r"^[^@]+@example\.test$",
        },
    )


def test_fixture_cases_with_different_rule_states_never_merge() -> None:
    first = ApiTestCase(
        id="TC-CHECK-0001",
        title="过期但仍在保留窗口",
        method="GET",
        path="/check/{email}",
        path_params={"email": "fixture@example.test"},
        assertions=[Assertion(type="status_code", expected=200)],
        runnable=False,
        risk="fixture_required",
        rule_ids=["expired-in-grace"],
        fixture_requirements=["time_control"],
    )
    second = copy.deepcopy(first)
    second.id = "TC-CHECK-0002"
    second.title = "超过保留窗口已清理"
    second.rule_ids = ["cleaned"]

    result = dedupe_cases([], [first, second])

    assert len(result) == 2


def test_business_rule_request_inference_distinguishes_length_from_lifecycle() -> None:
    endpoint = Endpoint(
        method="GET",
        path="/generate/{email}",
        parameters=[
            {
                "name": "prefix",
                "in": "query",
                "required": False,
                "schema": {"type": "string", "maxLength": 50},
            }
        ],
        responses={"200": {}},
    )
    legal = {
        "rule_id": "legal-prefix",
        "title": "传入合法 prefix 时生成对应邮箱",
        "strategy": "happy_path",
        "expected": {"status": 200, "basis": "responses.200"},
        "runnable": True,
    }
    lifecycle = {
        "rule_id": "expired-cleanup",
        "title": "超过有效期且超过可续期保留窗口的旧邮箱会被清理",
        "strategy": "dependency_chain",
        "expected": {"status": 200, "basis": "responses.200"},
        "runnable": False,
        "fixture_requirements": ["time_control"],
    }

    legal_plan = business_rule_plan_item(endpoint, legal, [endpoint])
    lifecycle_plan = business_rule_plan_item(endpoint, lifecycle, [endpoint])

    assert legal_plan["request_overrides"] == {"query": {"prefix": "test123"}}
    assert lifecycle_plan["request_overrides"] == {}


def test_auto_setup_does_not_make_fixture_dependent_case_runnable() -> None:
    producer = Endpoint(
        method="GET",
        path="/generate",
        responses={
            "200": {
                "schema": {
                    "type": "object",
                    "properties": {"email": {"type": "string"}},
                }
            }
        },
    )
    consumer = Endpoint(
        method="GET",
        path="/messages/{email}",
        parameters=[
            {
                "name": "email",
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
            },
            {
                "name": "email",
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
            },
        ],
        responses={"200": {}},
    )
    case = ApiTestCase(
        id="TC-MSG-0001",
        title="查询过期邮箱的历史邮件",
        method="GET",
        path="/messages/{email}",
        path_params={"email": "fixture-email"},
        runnable=False,
        risk="fixture_required",
        fixture_requirements=["time_control", "mail_injection"],
    )

    attach_setup_steps([case], [producer, consumer])

    assert case.setup_steps == []
    assert case.runnable is False
    assert case.risk == "fixture_required"


def test_fixture_case_explains_required_injection_and_scope() -> None:
    endpoint = Endpoint(
        method="GET",
        path="/generate/{email}",
        parameters=[
            {
                "name": "prefix",
                "in": "query",
                "required": False,
                "schema": {"type": "string"},
            },
            {
                "name": "email",
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
            },
        ],
        responses={"422": {}},
    )

    result = compile_scenario_drafts(
        [
            {
                "scenario_id": "SCN-RESERVED",
                "endpoint_method": "GET",
                "endpoint_path": "/generate/{email}",
                "intent": "服务保留前缀被拒绝",
                "design_method": "status_code_coverage",
                "expected_outcome": "返回文档声明的 422 响应。",
                "expected_basis": "responses.422",
                "assertions": [
                    {"type": "status_code", "operator": "eq", "expected": 422}
                ],
                "runnable": False,
                "risk": "fixture_required",
                "fixture_requirements": ["reserved_mailbox_prefix"],
                "request_overrides": {
                    "path_params": {"email": "fixture@mail.test"}
                },
            }
        ],
        [endpoint],
        expected_scenario_ids=["SCN-RESERVED"],
    )

    assert result.accepted
    case = result.cases[0]
    assert case.runnable is False
    assert "requires external test capability from the project" in case.title
    assert "reserved_mailbox_prefix" not in case.title
    assert "夹具" not in case.title
    assert any("IATest does not provide them" in item for item in case.preconditions)
    assert any("inject it into query.prefix" in item for item in case.test_steps)
    assert any("project must provide a real reserved-prefix test value" in item for item in case.test_steps)
    assert any("replace the corresponding path parameter" in item for item in case.test_steps)


def test_legacy_fixture_wording_is_replaced_when_cases_are_loaded(tmp_path) -> None:
    case = ApiTestCase(
        id="TC-MESSAGES-0001",
        title="查询历史邮件（需 mail_injection 夹具验证）",
        method="GET",
        path="/messages/{email}",
        path_params={"email": "fixture@mail.test"},
        runnable=False,
        risk="fixture_required",
        preconditions=["执行前必须启用以下测试夹具并建立标题所述状态：mail_injection。"],
        test_steps=["由 mail_injection 夹具向目标邮箱投递受控邮件数据，再执行接口校验。"],
        fixture_requirements=["mail_injection"],
    )
    save_cases(tmp_path, [case], "demo")

    loaded = load_cases_from_markdown(tmp_path, "demo")[0]
    visible_text = "\n".join([loaded.title, *loaded.preconditions, *loaded.test_steps])

    assert "mail_injection" not in visible_text
    assert "夹具" not in visible_text
    assert "当前框架未内置该能力" in visible_text
    assert "被测项目方投递受控邮件数据" in visible_text


def test_schema_boundary_coverage_includes_valid_maximum_and_overflow() -> None:
    endpoint = Endpoint(
        method="GET",
        path="/generate",
        parameters=[
            {
                "name": "prefix",
                "in": "query",
                "required": False,
                "schema": {"type": "string", "maxLength": 50},
            }
        ],
        responses={
            "200": {
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {"email": {"type": "string"}},
                        }
                    }
                }
            },
            "422": {},
        },
        business_rules=[
            {
                "rule_id": "fixed-domain",
                "title": "邮箱域名固定为 mail.tempbox.cn",
                "testable": True,
                "expected": {
                    "status": 200,
                    "basis": "responses.200",
                    "assertions": [
                        {"type": "status_code", "operator": "eq", "expected": 200}
                    ],
                },
                "runnable": True,
            },
            {
                "rule_id": "over-max",
                "title": "自定义前缀超过长度限制",
                "strategy": "boundary_value",
                "testable": True,
                "trigger": {
                    "request_overrides": {"query": {"prefix": "x" * 51}},
                    "omit_fields": [],
                    "chain_steps": [],
                },
                "expected": {
                    "status": 422,
                    "basis": "responses.422",
                    "assertions": [
                        {"type": "status_code", "operator": "eq", "expected": 422}
                    ],
                },
                "runnable": True,
            }
        ],
    )

    plan = fill_deterministic_coverage_gaps([], [endpoint])
    valid = next(
        item
        for item in plan
        if "valid_boundary" in (item.get("coverage_tags") or [])
    )
    overflow = next(item for item in plan if item.get("rule_ids") == ["over-max"])

    assert valid["request_overrides"] == {"query": {"prefix": "a" * 50}}
    assert valid["expected_basis"] == "responses.200"
    assert overflow["request_overrides"] == {"query": {"prefix": "x" * 51}}
    draft = fallback_draft(valid, {("GET", "/generate"): endpoint})
    assert {
        "type": "json_path",
        "target": "$.email",
        "operator": "eq",
        "expected": f"{'a' * 50}@mail.tempbox.cn",
    } in draft["assertions"]
    compiled = compile_scenario_drafts([draft], [endpoint], expected_scenario_ids=[draft["scenario_id"]])
    assert compiled.accepted
    email_assertions = [
        item
        for item in compiled.cases[0].assertions
        if item.target == "$.email"
    ]
    assert [(item.operator, item.expected) for item in email_assertions] == [
        ("eq", f"{'a' * 50}@mail.tempbox.cn")
    ]


def test_rule_top_level_status_does_not_mix_downstream_chain_statuses() -> None:
    endpoint = normalize_endpoint_contract(
        Endpoint(
            method="DELETE",
            path="/mail/{email}",
            parameters=[
                {
                    "name": "email",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string"},
                }
            ],
            responses={
                "200": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"status": {"type": "string"}},
                            }
                        }
                    }
                },
                "404": {},
            },
            business_rules=[
                {
                    "rule_id": "delete-postcondition",
                    "title": "删除成功后下游续期返回 404",
                    "expected": {
                        "status": 200,
                        "assertions": [
                            {"type": "status_code", "operator": "eq", "expected": 200},
                            {"type": "status_code", "operator": "equals", "expected": 200},
                            {"type": "status_code", "operator": "eq", "expected": 404},
                        ],
                    },
                    "trigger": {
                        "chain_steps": [
                            {"method": "DELETE", "path": "/mail/{email}"},
                            {"method": "POST", "path": "/renew/{email}"},
                        ]
                    },
                }
            ],
        )
    )

    assertions = endpoint.business_rules[0]["expected"]["assertions"]
    assert [
        item["expected"]
        for item in assertions
        if item.get("type") == "status_code"
    ] == [200]


def test_url_encoding_instruction_remains_parameter_metadata_not_business_rule() -> None:
    endpoint = normalize_endpoint_contract(
        Endpoint(
            method="DELETE",
            path="/mail/{email}",
            parameters=[
                {
                    "name": "email",
                    "in": "path",
                    "required": True,
                    "description": "URL encode 后传入",
                    "schema": {"type": "string"},
                }
            ],
            responses={"200": {}},
            business_rules=[
                {
                    "rule_id": "transport-only",
                    "title": "邮箱地址需要 URL encode 后作为路径参数传入",
                    "source_condition": "邮箱地址需要 URL encode 后作为路径参数传入",
                    "expected": {"assertions": []},
                }
            ],
        )
    )

    assert endpoint.business_rules == []

    schema_only = normalize_endpoint_contract(
        Endpoint(
            method="GET",
            path="/generate",
            responses={"200": {}},
            business_rules=[
                {
                    "rule_id": "single-parameter",
                    "title": "prefix 必须作为单个参数传入",
                    "expected": {"status": 200, "assertions": []},
                },
                {
                    "rule_id": "config-note",
                    "title": "当前默认保留窗口是 1440 分钟（24 小时），由 TEMP_RENEW_MINUTES 控制",
                    "testable": True,
                    "expected": {"status": 200, "assertions": []},
                },
            ],
        )
    )
    assert [item["rule_id"] for item in schema_only.business_rules] == ["config-note"]
    assert schema_only.business_rules[0]["testable"] is False


def test_never_created_resource_rule_remains_testable() -> None:
    endpoint = normalize_endpoint_contract(
        Endpoint(
            method="POST",
            path="/mail/{email}/renew",
            responses={"404": {}},
            business_rules=[
                {
                    "rule_id": "never-created",
                    "title": "邮箱从未创建过。",
                    "source_condition": "邮箱从未创建过。",
                    "expected": {
                        "status": 404,
                        "assertions": [
                            {
                                "type": "status_code",
                                "operator": "eq",
                                "expected": 404,
                            }
                        ],
                    },
                }
            ],
        )
    )

    assert endpoint.business_rules[0]["testable"] is True
    assert endpoint.business_rules[0]["runnable"] is True
    assert endpoint.business_rules[0]["trigger"]["request_overrides"] == {
        "path_params": {"email": "missing-{{uuid}}@example.invalid"}
    }


def test_restricted_prefix_rule_is_testable_with_fixture() -> None:
    endpoint = normalize_endpoint_contract(
        Endpoint(
            method="GET",
            path="/generate",
            responses={"422": {}},
            business_rules=[
                {
                    "rule_id": "restricted",
                    "title": "自定义前缀包含受限内容。",
                    "expected": {
                        "status": 422,
                        "assertions": [
                            {"type": "status_code", "operator": "eq", "expected": 422}
                        ],
                    },
                }
            ],
        )
    )

    rule = endpoint.business_rules[0]
    assert rule["testable"] is True
    assert rule["runnable"] is False
    assert rule["fixture_requirements"] == ["restricted_prefix"]


def test_contract_normalization_is_idempotent_when_status_is_inferred() -> None:
    endpoint = Endpoint(
        method="GET",
        path="/messages/{email}",
        responses={"200": {}},
        business_rules=[
            {
                "rule_id": "framework-error",
                "title": "仅框架级错误才返回非 200",
                "expected": {
                    "basis": "文档错误响应说明",
                    "description": "返回 Web 框架通用错误",
                    "assertions": [],
                },
            }
        ],
    )

    first = normalize_endpoint_contract(endpoint)
    first_json = json.dumps(
        first.business_rules,
        ensure_ascii=False,
        sort_keys=True,
    )
    second = normalize_endpoint_contract(copy.deepcopy(first))
    second_json = json.dumps(
        second.business_rules,
        ensure_ascii=False,
        sort_keys=True,
    )

    assert first.business_rules[0]["expected"]["status"] == 200
    assert first.business_rules[0]["expected"]["basis"] == "responses.200"
    assert second_json == first_json


def test_source_equivalent_rules_are_deduplicated_and_deleted_state_is_inferred() -> None:
    endpoint = normalize_endpoint_contract(
        Endpoint(
            method="GET",
            path="/generate",
            responses={"200": {}, "429": {}},
            business_rules=[
                {
                    "rule_id": "refresh-a",
                    "title": "如果同一个自定义前缀对应的邮箱已经存在，本接口会刷新该邮箱的创建时间，相当于重新开始10分钟有效期",
                    "expected": {
                        "status": 200,
                        "assertions": [
                            {"type": "status_code", "operator": "eq", "expected": 200}
                        ],
                    },
                },
                {
                    "rule_id": "refresh-b",
                    "title": "如果同一个自定义前缀对应的邮箱已经存在，本接口会刷新该邮箱的创建时间，相当于重新开始 10 分钟有效期。",
                    "expected": {
                        "status": 200,
                        "assertions": [
                            {"type": "status_code", "operator": "eq", "expected": 200}
                        ],
                    },
                },
                {
                    "rule_id": "rate-a",
                    "title": "同一IP在60秒内创建邮箱超过10次返回429",
                    "expected": {
                        "status": 429,
                        "assertions": [
                            {"type": "status_code", "operator": "eq", "expected": 429}
                        ],
                    },
                },
                {
                    "rule_id": "rate-b",
                    "title": "同一 IP 在 60 秒内创建邮箱超过 10 次，返回 429",
                    "expected": {
                        "status": 429,
                        "assertions": [
                            {"type": "status_code", "operator": "eq", "expected": 429}
                        ],
                    },
                },
            ],
        )
    )

    assert len(endpoint.business_rules) == 2

    deleted = normalize_endpoint_contract(
        Endpoint(
            method="DELETE",
            path="/mail/{email}",
            responses={"404": {}},
            business_rules=[
                {
                    "rule_id": "deleted",
                    "title": "邮箱已被用户删除。",
                    "expected": {
                        "status": 404,
                        "assertions": [
                            {"type": "status_code", "operator": "eq", "expected": 404}
                        ],
                    },
                }
            ],
        )
    )

    assert deleted.business_rules[0]["lifecycle_state"] == "deleted"


def test_reused_rule_id_never_merges_different_business_facts() -> None:
    endpoint = normalize_endpoint_contract(
        Endpoint(
            method="GET",
            path="/generate",
            responses={"200": {}, "429": {}},
            business_rules=[
                {
                    "rule_id": "GEN-007",
                    "title": "不传 prefix 时随机生成邮箱",
                    "expected": {
                        "status": 200,
                        "assertions": [
                            {"type": "status_code", "operator": "eq", "expected": 200}
                        ],
                    },
                },
                {
                    "rule_id": "GEN-007",
                    "title": "同一 IP 在 60 秒内创建邮箱超过 10 次，返回 429",
                    "expected": {
                        "status": 429,
                        "basis": "文档明确说明频率限制",
                        "assertions": [
                            {"type": "status_code", "operator": "eq", "expected": 429}
                        ],
                    },
                },
            ],
        )
    )

    assert len(endpoint.business_rules) == 2
    assert len({item["rule_id"] for item in endpoint.business_rules}) == 2
    assert {
        item["expected"]["status"] for item in endpoint.business_rules
    } == {200, 429}
    rate_limit = next(
        item for item in endpoint.business_rules
        if item["expected"]["status"] == 429
    )
    assert rate_limit["expected"]["basis"] == "responses.429"


def test_semantic_rule_deduplication_repairs_lifecycle_and_history_assertions() -> None:
    messages = normalize_endpoint_contract(
        Endpoint(
            method="GET",
            path="/messages/{email}",
            responses={
                "200": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "array",
                                "items": {"type": "object"},
                            }
                        }
                    }
                }
            },
            business_rules=[
                {
                    "rule_id": "sort-a",
                    "title": "邮件按 received_at 倒序返回，最新邮件在前",
                    "expected": {
                        "status": 200,
                        "assertions": [
                            {
                                "type": "json_path",
                                "target": "$",
                                "operator": "sorted_desc_by",
                                "expected": "received_at",
                            }
                        ],
                    },
                },
                {
                    "rule_id": "sort-b",
                    "title": "邮件列表必须按 received_at 倒序排列，最新邮件在前",
                    "expected": {"status": 200, "assertions": []},
                },
                {
                    "rule_id": "history-a",
                    "title": "邮箱已过期但在保留窗口内仍可查询历史邮件",
                    "lifecycle_state": "cleaned",
                    "expected": {
                        "status": 200,
                        "assertions": [
                            {
                                "type": "json_path",
                                "target": "$",
                                "operator": "is_array",
                                "expected": True,
                            },
                            {
                                "type": "json_path",
                                "target": "$",
                                "operator": "length_eq",
                                "expected": 0,
                            },
                        ],
                    },
                },
                {
                    "rule_id": "history-b",
                    "title": "如果邮箱到期但仍在保留窗口内，接口可返回历史邮件",
                    "expected": {"status": 200, "assertions": []},
                },
            ],
        )
    )

    assert len(messages.business_rules) == 2
    history = next(
        item for item in messages.business_rules if "历史邮件" in item["title"]
    )
    assert history["lifecycle_state"] == "expired_in_grace"
    assert not any(
        item.get("operator") == "length_eq"
        for item in history["expected"]["assertions"]
    )

    extend = normalize_endpoint_contract(
        Endpoint(
            method="POST",
            path="/extend/{email}",
            responses={"200": {}},
            business_rules=[
                {
                    "rule_id": "active-or-grace",
                    "title": "邮箱仍存在且未超过可续期保留窗口",
                    "lifecycle_state": "cleaned",
                    "expected": {"status": 200, "assertions": []},
                },
                {
                    "rule_id": "active-or-grace-alias",
                    "title": "只有仍存在且未超过可续期保留窗口的邮箱可以续期",
                    "expected": {"status": 200, "assertions": []},
                },
                {
                    "rule_id": "refresh-a",
                    "title": "续期成功后创建/刷新时间更新并返回新的剩余有效期",
                    "expected": {"status": 200, "assertions": []},
                },
                {
                    "rule_id": "refresh-b",
                    "title": "续期成功后邮箱刷新时间更新为当前时间且返回 expires_in",
                    "expected": {"status": 200, "assertions": []},
                },
            ],
        )
    )
    assert len(extend.business_rules) == 2
    renewable = next(
        item for item in extend.business_rules if "保留窗口" in item["title"]
    )
    assert renewable["lifecycle_state"] == "active_or_grace"


def test_contract_gate_accepts_assertions_on_explicit_chain_steps() -> None:
    endpoint = Endpoint(
        method="DELETE",
        path="/mail/{email}",
        business_rules=[
            {
                "rule_id": "delete-postcondition",
                "title": "删除后不可续期",
                "testable": True,
                "source_evidence": "删除后不可续期",
                "expected": {"assertions": []},
                "trigger": {
                    "chain_steps": [
                        {"method": "DELETE", "path": "/mail/{email}"},
                        {
                            "method": "POST",
                            "path": "/renew/{email}",
                            "assertions": [
                                {
                                    "type": "status_code",
                                    "operator": "eq",
                                    "expected": 404,
                                }
                            ],
                        },
                    ]
                },
            }
        ],
    )

    assert contract_catalog_issues([endpoint], [endpoint]) == []


def test_audit_calibration_rejects_fixture_and_chain_rubric_violations() -> None:
    artifact_text = json.dumps(
        {
            "artifact": [
                {
                    "rule_id": "fixture-rule",
                    "runnable": False,
                    "testable": True,
                    "fixture_requirements": ["time_control"],
                    "trigger": {"chain_steps": []},
                },
                {
                    "rule_id": "chain-rule",
                    "trigger": {
                        "chain_steps": [
                            {"method": "DELETE", "path": "/mail"},
                            {
                                "method": "POST",
                                "path": "/renew",
                                "assertions": [
                                    {
                                        "type": "status_code",
                                        "operator": "eq",
                                        "expected": 404,
                                    }
                                ],
                            },
                        ]
                    },
                },
                {
                    "id": "case-edge",
                    "query": {"prefix": "a" * 51},
                    "steps": [
                        {"query": {"prefix": "refresh-{{random}}"}},
                        {"query": {"prefix": "refresh-{{random}}"}},
                    ],
                },
            ]
        },
        ensure_ascii=False,
    )
    review = {
        "issues": [
            {
                "location": "fixture-rule",
                "reason": "testable=true 但 runnable=false 存在矛盾",
                "suggested_fix": "增加 time_control 等待和 chain_steps",
            },
            {
                "location": "chain-rule",
                "reason": "expected 仅有删除响应，缺少后置验证",
                "suggested_fix": "将后置验证链移至 chain_steps",
            },
            {
                "dimension": "non_redundancy",
                "location": "case-a 与 case-b",
                "reason": "两条链路只共享必要前置步骤，不构成完全重复",
                "suggested_fix": "可考虑合并，但两者验证目标不同",
            },
            {
                "dimension": "reliability",
                "location": "case-sort assertions",
                "reason": "sorted_desc_by 是自定义断言，缺乏明确执行器支持",
                "suggested_fix": "改为脚本校验排序",
            },
            {
                "dimension": "reliability",
                "location": "case-edge steps",
                "reason": "random 可能不会持久化为相同值，导致两步使用不同前缀",
                "suggested_fix": "额外提取 random",
            },
            {
                "dimension": "completeness",
                "location": "case-edge query",
                "reason": "缺少 51 字符的明确越界用例",
                "suggested_fix": "补充 51 字符 prefix",
            },
            {
                "dimension": "reasonableness",
                "location": "delete-002 rule_id",
                "reason": "rule_id 使用小写前缀，与其他规则命名风格不一致",
                "suggested_fix": "改为 DELETE-002",
            },
        ]
    }

    violations = review_rubric_violations(review, artifact_text)

    assert len(violations) == 7


def test_audit_calibration_rejects_incorrect_artifact_counts() -> None:
    artifact_text = json.dumps(
        {
            "artifact": [
                {
                    "method": "GET",
                    "path": "/one",
                    "business_rules": [{"rule_id": "one"}],
                },
                {
                    "method": "POST",
                    "path": "/two",
                    "business_rules": [{"rule_id": "two"}, {"rule_id": "three"}],
                },
            ]
        },
        ensure_ascii=False,
    )

    violations = review_rubric_violations(
        {
            "summary": "共 2 个接口、42 条 business_rules。",
            "issues": [],
        },
        artifact_text,
    )

    assert violations == ["审计摘要声称有 42 条业务规则，实际为 3 条"]


def test_audit_calibration_accepts_uncovered_non_testable_rule() -> None:
    violations = review_rubric_violations(
        {
            "issues": [
                {
                    "dimension": "completeness",
                    "location": "coverage_manifest MSG-005",
                    "reason": "MSG-005 标注 testable=false 且无对应 case_ids",
                    "suggested_fix": "增加 coverage_gap disposition 或生成用例",
                }
            ]
        },
        json.dumps({"artifact": []}, ensure_ascii=False),
    )

    assert violations == [
        "coverage_manifest msg-005：testable=false 规则不得要求生成伪造用例或 case_id"
    ]


def test_audit_calibration_knows_valid_max_boundary_and_numeric_timestamp() -> None:
    artifact_text = json.dumps(
        {
            "artifact": [
                {"id": "TC-API-0001", "query": {"prefix": "a" * 50}},
                {"id": "TC-API-0008", "query": {"prefix": "x" * 51}},
                {
                    "id": "TC-API-0005",
                    "steps": [{"query": {"prefix": "iatest_{{timestamp}}"}}],
                },
            ]
        },
        ensure_ascii=False,
    )
    violations = review_rubric_violations(
        {
            "issues": [
                {
                    "location": "TC-API-0001",
                    "reason": "50 字符只是合法内点且与 happy_path 重复，应改为 51 字符边界",
                    "suggested_fix": "删除 50 字符用例并合并到 51 字符用例",
                },
                {
                    "location": "TC-API-0005",
                    "reason": "timestamp 可能包含点或冒号等非法字符，导致不匹配 prefix pattern",
                    "suggested_fix": "改用纯数字变量",
                },
            ]
        },
        artifact_text,
    )

    assert len(violations) == 2
    assert any("50 字符是合法上边界" in item for item in violations)
    assert any("timestamp 是纯十进制" in item for item in violations)


def test_audit_calibration_removes_positive_non_issue() -> None:
    violations = review_rubric_violations(
        {
            "issues": [
                {
                    "location": "TC-API-0005",
                    "reason": "gte 与 expires_in 组合断言符合审计规则，设计合理",
                    "suggested_fix": "无需修复",
                }
            ]
        },
        json.dumps({"artifact": []}, ensure_ascii=False),
    )

    assert violations == [
        "tc-api-0005：正向确认且无需修复的内容不能作为质量问题"
    ]


def test_audit_calibration_rejects_false_case_duplicate_and_missing_assertions() -> None:
    base = {
        "id": "TC-API-0005",
        "title": "刷新邮箱",
        "method": "CHAIN",
        "path": "",
        "steps": [
            {
                "name": "生成",
                "method": "GET",
                "path": "/generate",
                "query": {"prefix": "refresh-{{random}}"},
                "assertions": [{"type": "status_code", "operator": "eq", "expected": 200}],
                "extract": {"email": "$.email", "created": "$.created_at"},
            },
            {
                "name": "刷新",
                "method": "GET",
                "path": "/generate",
                "query": {"prefix": "refresh-{{random}}"},
                "assertions": [
                    {"type": "json_path", "target": "$.email", "operator": "eq", "expected": "{{email}}"},
                    {"type": "json_path", "target": "$.expires_in", "operator": "eq", "expected": 600},
                ],
            },
        ],
    }
    different = copy.deepcopy(base)
    different["id"] = "TC-API-0030"
    different["steps"][-1] = {
        "name": "重复删除",
        "method": "DELETE",
        "path": "/delete/{email}",
        "path_params": {"email": "{{email}}"},
        "assertions": [{"type": "status_code", "operator": "eq", "expected": 404}],
    }
    artifact_text = json.dumps({"artifact": [base, different]}, ensure_ascii=False)

    violations = review_rubric_violations(
        {
            "issues": [
                {
                    "dimension": "non_redundancy",
                    "location": "TC-API-0005 / TC-API-0030",
                    "reason": "HTTP步骤、断言与变量流完全一致，执行语义重复",
                    "suggested_fix": "删除 TC-API-0030 并合并 rule_ids",
                },
                {
                    "dimension": "completeness",
                    "location": "TC-API-0005",
                    "reason": "未断言 email 相同且 expires_in=600",
                    "suggested_fix": "增加 email 与 expires_in 断言",
                },
            ]
        },
        artifact_text,
    )

    assert len(violations) == 2
    assert any("HTTP 步骤或最终状态不同" in item for item in violations)
    assert any("已包含 email 与 expires_in" in item for item in violations)

    dimensions, retained, summary = calibrate_review_result(
        {
            "summary": "存在若干问题",
            "issues": [
                {
                    "severity": "warning",
                    "dimension": "non_redundancy",
                    "location": "TC-API-0005 / TC-API-0030",
                    "reason": "HTTP步骤、断言与变量流完全一致，执行语义重复",
                    "suggested_fix": "删除 TC-API-0030 并合并 rule_ids",
                },
                {
                    "severity": "high",
                    "dimension": "completeness",
                    "location": "TC-API-0005",
                    "reason": "未断言 email 相同且 expires_in=600",
                    "suggested_fix": "增加 email 与 expires_in 断言",
                },
                {
                    "severity": "warning",
                    "dimension": "reliability",
                    "location": "TC-API-0005",
                    "reason": "这是不与固定规则冲突的有效问题",
                    "suggested_fix": "核对服务行为",
                },
            ],
        },
        artifact_text,
        {
            "reliability": 82.0,
            "completeness": 70.0,
            "reasonableness": 91.0,
            "non_redundancy": 75.0,
        },
    )

    assert dimensions == {
        "reliability": 82.0,
        "completeness": 100.0,
        "reasonableness": 91.0,
        "non_redundancy": 100.0,
    }
    assert len(retained) == 1
    assert "剔除 2 条" in summary and "保留 1 条" in summary


def test_real_model_assertion_dialects_do_not_require_semantic_repair() -> None:
    endpoint = Endpoint(
        method="GET",
        path="/items",
        responses={"200": {}, "422": {}},
    )
    compiled = compile_scenario_drafts(
        [
            {
                "scenario_id": "SCN-DIALECT-001",
                "endpoint_method": "GET",
                "endpoint_path": "/items",
                "intent": "验证模型方言",
                "design_method": "status_code_coverage",
                "expected_outcome": "返回文档化校验错误",
                "expected_basis": "非法输入会返回校验错误",
                "assertions": [
                    {"type": "status_code", "value": 422},
                    {"type": "response_json", "field": "detail", "contains": "invalid"},
                    {"type": "json_field", "field": "valid", "expected": False},
                    {"type": "json_field_compare", "field": "count", "operator": "gt", "expected": 0},
                    {"check": "response_body.code", "expected": "INVALID"},
                ],
            }
        ],
        [endpoint],
        expected_scenario_ids=["SCN-DIALECT-001"],
    )
    assert compiled.accepted, [issue.to_dict() for issue in compiled.issues]
    response = {
        "status_code": 422,
        "json": {"detail": "invalid input", "valid": False, "count": 1, "code": "INVALID"},
        "text": "",
        "headers": {},
    }
    assert all(evaluate(item, response) is None for item in compiled.cases[0].assertions)
    assert compiled.drafts[0].expected_basis.startswith("responses.422")


def test_chain_step_dialects_are_normalized_and_step_assertions_execute() -> None:
    producer = Endpoint(
        method="GET",
        path="/generate",
        responses={"200": {"schema": {"type": "object", "properties": {"email": {"type": "string"}}}}},
    )
    consumer = Endpoint(
        method="GET",
        path="/messages/{email}",
        parameters=[{"name": "email", "in": "path", "required": True, "schema": {"type": "string"}}],
        responses={"200": {}},
    )
    compiled = compile_scenario_drafts(
        [
            {
                "scenario_id": "SCN-CHAIN-DIALECT",
                "endpoint_method": "CHAIN",
                "endpoint_path": "",
                "intent": "生成后查询",
                "design_method": "dependency_chain",
                "expected_outcome": "链路执行成功",
                "chain_steps": [
                    {
                        "method": "GET",
                        "path": "/generate",
                        "extract": {"field": "email", "from": "response_body"},
                    },
                    {
                        "method": "GET",
                        "path": "/messages/{{email}}",
                        "inject": {"variable": "email", "to": "path_parameter", "target_field": "email"},
                    },
                ],
                "assertions": [
                    {"step": 1, "type": "response_status", "expected": 200},
                    {"step": 1, "type": "response_body", "field": "email", "condition": "exists"},
                    {"step": 2, "type": "json_type", "path": "$", "expected": "array"},
                    {"step": 2, "type": "json_length", "path": "$", "expected": 0},
                ],
            }
        ],
        [producer, consumer],
        expected_scenario_ids=["SCN-CHAIN-DIALECT"],
    )
    assert compiled.accepted, [issue.to_dict() for issue in compiled.issues]
    chain = compiled.cases[0]
    assert chain.steps[0].extract == {"email": "$.email"}
    assert chain.steps[1].path == "/messages/{email}"
    assert chain.steps[1].path_params == {"email": "{{email}}"}
    assert all(evaluate(item, {"status_code": 200, "json": {"email": "a@example.com"}, "text": "", "headers": {}}) is None for item in chain.steps[0].assertions)
    assert all(evaluate(item, {"status_code": 200, "json": [], "text": "[]", "headers": {}}) is None for item in chain.steps[1].assertions)


def test_chain_expect_status_dialect_compiles_to_status_assertion() -> None:
    step = normalize_chain_step(
        {
            "method": "GET",
            "path": "/messages/{email}",
            "path_params": {"email": "missing@example.com"},
            "expect_status": 404,
        }
    )

    assert step["assertions"][0] == {"type": "status_code", "operator": "eq", "expected": 404}


def test_endpoint_dependency_chain_is_promoted_and_extract_action_is_folded() -> None:
    producer = Endpoint(
        method="GET",
        path="/generate",
        responses={"200": {"schema": {"type": "object", "properties": {"email": {"type": "string"}}}}},
    )
    consumer = Endpoint(
        method="GET",
        path="/messages/{email}",
        parameters=[{"name": "email", "in": "path", "required": True, "schema": {"type": "string"}}],
        responses={"200": {}},
    )
    compiled = compile_scenario_drafts(
        [
            {
                "scenario_id": "SCN-PROMOTE-CHAIN",
                # A common model dialect keeps the consumer method here even though
                # the semantic product is a multi-step chain.
                "endpoint_method": "GET",
                "endpoint_path": "/messages/{email}",
                "intent": "先生成邮箱再查询",
                "design_method": "dependency_chain",
                "expected_outcome": "链路执行成功",
                "chain_steps": [
                    {"action": "call", "method": "GET", "path": "/generate"},
                    {"action": "extract", "field": "email", "as": "GENERATED_EMAIL"},
                    {
                        "action": "call",
                        "method": "GET",
                        "path": "/messages/{email}",
                        "path_params": {"email": "${GENERATED_EMAIL}"},
                    },
                ],
            }
        ],
        [producer, consumer],
        expected_scenario_ids=["SCN-PROMOTE-CHAIN"],
    )
    assert compiled.accepted, [issue.to_dict() for issue in compiled.issues]
    case = compiled.cases[0]
    assert case.method == "CHAIN"
    assert case.steps[0].extract == {"GENERATED_EMAIL": "$.email"}
    assert case.steps[1].path_params == {"email": "{{GENERATED_EMAIL}}"}


def test_current_model_status_json_and_schema_assertion_dialects_are_executable() -> None:
    endpoint = Endpoint(method="GET", path="/status", responses={"200": {}})
    compiled = compile_scenario_drafts(
        [
            {
                "scenario_id": "SCN-CURRENT-DIALECT",
                "endpoint_method": "GET",
                "endpoint_path": "/status",
                "intent": "验证当前模型断言方言",
                "design_method": "happy_path",
                "expected_outcome": "返回状态",
                "assertions": [
                    {"type": "status", "operator": "equals", "expected": 200},
                    {"type": "json", "path": "$.valid", "operator": "equals", "expected": True},
                    {"type": "json_field", "field": "count", "expected": "integer"},
                    {"type": "json_schema", "schema": {"type": "object", "required": ["valid", "count"]}},
                ],
            }
        ],
        [endpoint],
        expected_scenario_ids=["SCN-CURRENT-DIALECT"],
    )
    assert compiled.accepted, [issue.to_dict() for issue in compiled.issues]
    response = {"status_code": 200, "json": {"valid": True, "count": 1}, "text": "", "headers": {}}
    assert all(evaluate(item, response) is None for item in compiled.cases[0].assertions)


def test_request_override_path_alias_is_applied() -> None:
    endpoint = Endpoint(
        method="GET",
        path="/users/{email}",
        parameters=[{"name": "email", "in": "path", "required": True, "schema": {"type": "string"}}],
        responses={"200": {}},
    )
    compiled = compile_scenario_drafts(
        [
            {
                "scenario_id": "SCN-PATH-ALIAS",
                "endpoint_method": "GET",
                "endpoint_path": "/users/{email}",
                "intent": "使用指定邮箱查询",
                "design_method": "path_parameter_error",
                "expected_outcome": "返回文档结果",
                "request_overrides": {"path.email": "not-an-email"},
            }
        ],
        [endpoint],
        expected_scenario_ids=["SCN-PATH-ALIAS"],
    )
    assert compiled.accepted
    assert compiled.cases[0].path_params == {"email": "not-an-email"}


def test_negated_status_mention_does_not_require_that_status_assertion() -> None:
    endpoint = Endpoint(
        method="GET",
        path="/messages/{email}",
        responses={"200": {"schema": {"type": "array", "items": {"type": "object"}}}},
    )
    compiled = compile_scenario_drafts(
        [
            {
                "scenario_id": "SCN-NOT-404",
                "endpoint_method": "GET",
                "endpoint_path": "/messages/{email}",
                "intent": "不存在时返回空数组而不返回404",
                "design_method": "equivalence_class",
                "expected_outcome": "状态码200且返回空数组，不返回404",
                "assertions": [
                    {"type": "status_code", "expected": 200},
                    {"type": "json_path", "target": "$", "operator": "is_array", "expected": True},
                ],
            }
        ],
        [endpoint],
        expected_scenario_ids=["SCN-NOT-404"],
    )
    assert compiled.accepted, [issue.to_dict() for issue in compiled.issues]


def test_model_plan_requires_executable_chain_and_canonicalizes_template_path() -> None:
    endpoints = [
        Endpoint(method="GET", path="/generate", responses={"200": {}}),
        Endpoint(method="GET", path="/check/{email}", responses={"200": {}}),
    ]
    plan, issues = normalize_coverage_plan(
        [
            {
                "scenario_id": "wait-case",
                "endpoint_method": "GET",
                "endpoint_path": "/check/{email}",
                "design_method": "equivalence_class",
                "title_hint": "等待邮箱过期后再次调用",
            },
            {
                "scenario_id": "chain-case",
                "endpoint_method": "CHAIN",
                "endpoint_path": "",
                "design_method": "dependency_chain",
                "title_hint": "生成后检查",
                "chain_steps": [
                    {"method": "GET", "path": "/generate?prefix=fixed"},
                    {"method": "GET", "path": "/check/{{email}}"},
                ],
            },
        ],
        endpoints,
    )
    assert [item["scenario_id"] for item in plan] == ["chain-case"]
    assert plan[0]["chain_steps"][0]["path"] == "/generate"
    assert plan[0]["chain_steps"][0]["query"] == {"prefix": "fixed"}
    assert plan[0]["chain_steps"][1]["path"] == "/check/{email}"
    assert any(issue.code == "unexecutable_plan_item" and issue.severity == "warning" for issue in issues)


def test_model_plan_rejects_unproven_error_trigger_and_shared_rate_limit_chain() -> None:
    endpoint = Endpoint(
        method="GET",
        path="/generate",
        parameters=[{"name": "prefix", "in": "query", "required": False, "schema": {"type": "string"}}],
        responses={"200": {}, "422": {}, "429": {}},
    )
    plan, issues = normalize_coverage_plan(
        [
            {
                "scenario_id": "invalid-prefix",
                "endpoint_method": "GET",
                "endpoint_path": "/generate",
                "design_method": "equivalence_class",
                "title_hint": "非法前缀返回422",
                "expected_basis": "responses.422",
            },
            {
                "scenario_id": "rate-limit",
                "endpoint_method": "CHAIN",
                "design_method": "dependency_chain",
                "title_hint": "高频创建触发限流",
                "expected_basis": "responses.429",
                "chain_steps": [
                    {"method": "GET", "path": "/generate"},
                    {"method": "GET", "path": "/generate"},
                ],
            },
        ],
        [endpoint],
    )
    assert plan == []
    assert {item.code for item in issues} == {
        "unexecutable_status_coverage",
        "unsafe_load_chain",
    }


def test_non_chain_dependencies_are_rejected_as_non_executable() -> None:
    endpoint = Endpoint(method="GET", path="/health", responses={"200": {}})
    compiled = compile_scenario_drafts(
        [
            {
                "scenario_id": "SCN-DEPENDENCY-TEXT",
                "endpoint_method": "GET",
                "endpoint_path": "/health",
                "intent": "依赖前置文本",
                "design_method": "happy_path",
                "expected_outcome": "成功",
                "dependencies": [{"step": "先创建数据"}],
            }
        ],
        [endpoint],
        expected_scenario_ids=["SCN-DEPENDENCY-TEXT"],
    )
    assert any(issue.code == "non_executable_dependencies" for issue in compiled.error_issues)


def test_chain_assertion_dialects_and_cross_step_claims_compile_to_executable_checks() -> None:
    endpoints = [
        Endpoint(
            method="GET",
            path="/generate",
            responses={"200": {"schema": {"type": "object", "properties": {"email": {"type": "string"}}}}},
        ),
        Endpoint(
            method="DELETE",
            path="/delete/{email}",
            parameters=[{"name": "email", "in": "path", "required": True, "schema": {"type": "string"}}],
            responses={"200": {}},
        ),
        Endpoint(
            method="POST",
            path="/extend/{email}",
            parameters=[{"name": "email", "in": "path", "required": True, "schema": {"type": "string"}}],
            responses={"200": {}, "404": {"schema": {"type": "object", "properties": {"detail": {"type": "string"}}}}},
        ),
    ]
    compiled = compile_scenario_drafts(
        [
            {
                "scenario_id": "SCN-CHAIN-DIALECT",
                "endpoint_method": "CHAIN",
                "endpoint_path": "",
                "intent": "删除后续期",
                "design_method": "dependency_chain",
                "expected_outcome": "删除成功后续期返回404",
                "chain_steps": [
                    {"method": "GET", "path": "/generate", "extract": {"json_path": "$.email", "variable": "email"}},
                    {
                        "method": "DELETE",
                        "path": "/delete/{{email}}",
                        "assertions": [{"operator": "status_code", "value": 200}],
                    },
                    {
                        "method": "POST",
                        "path": "/extend/{{email}}",
                        "assertions": [{"operator": "status_code", "value": 404}],
                    },
                ],
            }
        ],
        endpoints,
        expected_scenario_ids=["SCN-CHAIN-DIALECT"],
    )
    assert compiled.accepted, [issue.to_dict() for issue in compiled.issues]
    assert compiled.cases[0].steps[2].assertions[0].expected == 404
    assert all(any(assertion.type == "status_code" for assertion in step.assertions) for step in compiled.cases[0].steps)

    same_value = compile_scenario_drafts(
        [
            {
                "scenario_id": "SCN-CHAIN-SAME",
                "endpoint_method": "CHAIN",
                "endpoint_path": "",
                "intent": "两次生成返回相同邮箱",
                "design_method": "dependency_chain",
                "expected_outcome": "两次响应中的 email 相同",
                "chain_steps": [
                    {"method": "GET", "path": "/generate", "extract": {"json_path": "$.email", "variable": "first_email"}},
                    {
                        "method": "GET",
                        "path": "/generate",
                        "assertions": [{"json_path": "$.email", "operator": "equals", "value": "{{first_email}}"}],
                    },
                ],
            }
        ],
        endpoints,
        expected_scenario_ids=["SCN-CHAIN-SAME"],
    )
    assert same_value.accepted, [issue.to_dict() for issue in same_value.issues]


def test_chain_chinese_response_field_claims_require_matching_assertions() -> None:
    endpoint = Endpoint(
        method="GET",
        path="/generate",
        responses={
            "200": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "email": {"type": "string"},
                        "created_at": {"type": "integer"},
                        "expires_in": {"type": "integer"},
                    },
                }
            }
        },
    )
    compiled = compile_scenario_drafts(
        [
            {
                "scenario_id": "SCN-REFRESH-CLAIM",
                "endpoint_method": "CHAIN",
                "endpoint_path": "",
                "intent": "重复生成刷新邮箱",
                "design_method": "dependency_chain",
                "expected_outcome": "第二次返回相同邮箱地址，创建时间被刷新且剩余有效期重置",
                "chain_steps": [
                    {"method": "GET", "path": "/generate", "extract": {"email": "$.email"}},
                    {
                        "method": "GET",
                        "path": "/generate",
                        "assertions": [{"type": "json_path", "path": "$.email", "expected": "{{email}}"}],
                    },
                ],
            }
        ],
        [endpoint],
        expected_scenario_ids=["SCN-REFRESH-CLAIM"],
    )
    issue_fields = {issue.message for issue in compiled.error_issues}
    assert any("created_at" in message for message in issue_fields)
    assert any("expires_in" in message for message in issue_fields)


def test_optional_query_and_response_assertions_match_real_tempbox_draft_semantics() -> None:
    endpoint = Endpoint(
        method="GET",
        path="/api/generate",
        parameters=[
            {
                "name": "prefix",
                "in": "query",
                "required": False,
                "schema": {"type": "string"},
            }
        ],
        responses={
            "200": {
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {
                                "email": {"type": "string"},
                                "created_at": {"type": "integer"},
                                "expires_in": {"type": "integer"},
                            },
                        }
                    }
                }
            }
        },
    )
    compiled = compile_scenario_drafts(
        [
            {
                "scenario_id": "generate-happy-path",
                "endpoint_method": "GET",
                "endpoint_path": "/api/generate",
                "intent": "不传prefix时服务端随机生成8位邮箱前缀",
                "design_method": "happy_path",
                "expected_outcome": "返回email、created_at时间戳和expires_in=600",
                "expected_basis": "不传 prefix 时返回200",
                # Even a contradictory model default must not override explicit omission semantics.
                "request_overrides": {"query": {"prefix": "should-not-be-sent"}},
                "assertions": [
                    {"type": "status_code", "expected": 200},
                    {
                        "type": "response_body",
                        "field": "email",
                        "pattern": r"^[a-zA-Z0-9_\-]{8}@mail\.tempbox\.cn$",
                    },
                    {"type": "response_body", "field": "created_at", "type_check": "integer"},
                ],
            }
        ],
        [endpoint],
        expected_scenario_ids=["generate-happy-path"],
    )
    assert compiled.accepted, [issue.to_dict() for issue in compiled.issues]
    case = compiled.cases[0]
    assert case.query == {}
    by_target = {(item.type, item.target): item for item in case.assertions}
    assert by_target[("json_path", "$.email")].operator == "matches"
    assert by_target[("json_path", "$.created_at")].operator == "is_integer"
    assert by_target[("json_path", "$.expires_in")].operator == "eq"
    assert by_target[("json_path", "$.expires_in")].expected == 600
    response = {
        "status_code": 200,
        "json": {"email": "abcdefgh@mail.tempbox.cn", "created_at": 1784204885, "expires_in": 600},
        "text": "",
        "headers": {},
    }
    assert all(evaluate(item, response) is None for item in case.assertions)


def test_assertion_without_expected_value_is_rejected_instead_of_becoming_eq_null() -> None:
    endpoint = Endpoint(method="GET", path="/health", responses={"200": {}})
    compiled = compile_scenario_drafts(
        [
            {
                "scenario_id": "SCN-BAD-NULL",
                "endpoint_method": "GET",
                "endpoint_path": "/health",
                "intent": "检查响应字段",
                "design_method": "happy_path",
                "expected_outcome": "返回健康状态",
                "assertions": [{"type": "response_body", "field": "status"}],
            }
        ],
        [endpoint],
        expected_scenario_ids=["SCN-BAD-NULL"],
    )
    assert any(issue.code == "missing_assertion_expected" for issue in compiled.error_issues)


def test_nested_chain_data_flow_dialect_survives_save_and_reload(tmp_path) -> None:
    producer = Endpoint(
        method="GET",
        path="/generate",
        responses={"200": {"schema": {"type": "object", "properties": {"email": {"type": "string"}}}}},
    )
    consumers = [
        Endpoint(
            method="GET",
            path="/messages/{email}",
            parameters=[{"name": "email", "in": "path", "required": True, "schema": {"type": "string"}}],
            responses={"200": {"schema": {"type": "array", "items": {"type": "object"}}}},
        ),
        Endpoint(
            method="GET",
            path="/check/{email}",
            parameters=[{"name": "email", "in": "path", "required": True, "schema": {"type": "string"}}],
            responses={"200": {"schema": {"type": "object", "properties": {"valid": {"type": "boolean"}}}}},
        ),
    ]
    compiled = compile_scenario_drafts(
        [
            {
                "scenario_id": "SCN-NESTED-CHAIN",
                "endpoint_method": "CHAIN",
                "endpoint_path": "",
                "intent": "生成邮箱后查询并检查",
                "design_method": "dependency_chain",
                "expected_outcome": "步骤全部返回 200",
                "chain_steps": [
                    {
                        "step": 1,
                        "method": "GET",
                        "path": "/generate",
                        "extract": {"email": {"from": "response_body", "field": "email", "jsonpath": "$.email"}},
                    },
                    {
                        "step": 2,
                        "method": "GET",
                        "path": "/messages/{email}",
                        "inject": {"path": {"email": "{{step1.email}}"}},
                    },
                    {
                        "step": 3,
                        "method": "GET",
                        "path": "/check/{email}",
                        "inject": {"path": {"email": "{{step1.email}}"}},
                    },
                ],
                "assertions": [
                    {"step": 1, "type": "status_code", "expected": 200},
                    {"step": 2, "type": "status_code", "expected": 200},
                    {"step": 3, "type": "status_code", "expected": 200},
                ],
            }
        ],
        [producer, *consumers],
        expected_scenario_ids=["SCN-NESTED-CHAIN"],
    )
    assert compiled.accepted, [item.to_dict() for item in compiled.issues]
    case = compiled.cases[0]
    assert case.steps[0].extract["step1_email"] == "$.email"
    assert case.steps[1].path_params == {"email": "{{step1_email}}"}
    assert case.steps[2].path_params == {"email": "{{step1_email}}"}

    save_cases(tmp_path, [case], project="demo")
    reloaded = load_cases_from_markdown(tmp_path, "demo")
    assert not validate_compiled_cases(reloaded, [producer, *consumers])
    assert reloaded[0].steps[2].path_params == {"email": "{{step1_email}}"}


def test_generic_dependency_chain_title_is_replaced_with_step_scenario() -> None:
    endpoints = [
        Endpoint(method="GET", path="/api/generate", summary="生成临时邮箱", responses={"200": {}}),
        Endpoint(method="GET", path="/api/messages/{email}", summary="查询邮箱邮件", responses={"200": {}}),
        Endpoint(method="GET", path="/api/check/{email}", summary="检查邮箱状态", responses={"200": {}}),
        Endpoint(method="POST", path="/api/extend/{email}", summary="续期邮箱", responses={"200": {}}),
    ]
    raw_steps = [
        {"method": "GET", "path": "/api/generate", "extract": {"email": "$.email"}},
        {"method": "GET", "path": "/api/messages/{email}", "path_params": {"email": "{{email}}"}},
        {"method": "GET", "path": "/api/check/{email}", "path_params": {"email": "{{email}}"}},
        {"method": "POST", "path": "/api/extend/{email}", "path_params": {"email": "{{email}}"}},
    ]
    plan, issues = normalize_coverage_plan(
        [
            {
                "scenario_id": "SCN-CHAIN-TITLE",
                "endpoint_method": "CHAIN",
                "design_method": "dependency_chain",
                "title_hint": "接口数据准备与消费主链路",
                "chain_steps": raw_steps,
            }
        ],
        endpoints,
    )

    assert not [item for item in issues if item.severity == "error"]
    assert plan[0]["title_hint"] == "生成邮箱、查询邮件、检查邮箱状态后续期邮箱"

    compiled = compile_scenario_drafts(
        [
            {
                "scenario_id": "SCN-CHAIN-TITLE",
                "endpoint_method": "CHAIN",
                "intent": "接口数据准备与消费主链路",
                "design_method": "dependency_chain",
                "expected_outcome": "链路执行成功",
                "chain_steps": raw_steps,
            }
        ],
        endpoints,
        expected_scenario_ids=["SCN-CHAIN-TITLE"],
    )
    assert compiled.cases[0].title == "生成邮箱、查询邮件、检查邮箱状态后续期邮箱"


def test_vague_chain_title_is_rebuilt_from_actions_and_final_status() -> None:
    endpoints = [
        Endpoint(method="GET", path="/generate", summary="生成邮箱", responses={"200": {}}),
        Endpoint(method="DELETE", path="/delete/{email}", summary="删除邮箱", responses={"200": {}, "404": {}}),
        Endpoint(method="POST", path="/extend/{email}", summary="续期邮箱", responses={"200": {}, "404": {}}),
    ]
    compiled = compile_scenario_drafts(
        [
            {
                "scenario_id": "SCN-DELETED-EXTEND",
                "endpoint_method": "CHAIN",
                "intent": "邮箱已被用户删除。",
                "design_method": "dependency_chain",
                "expected_outcome": "删除后续期返回 404",
                "expected_basis": "responses.404",
                "chain_steps": [
                    {
                        "method": "GET",
                        "path": "/generate",
                        "extract": {"email": "$.email"},
                    },
                    {
                        "method": "DELETE",
                        "path": "/delete/{email}",
                        "path_params": {"email": "{{email}}"},
                    },
                    {
                        "method": "POST",
                        "path": "/extend/{email}",
                        "path_params": {"email": "{{email}}"},
                        "assertions": [
                            {"type": "status_code", "operator": "eq", "expected": 404}
                        ],
                    },
                ],
            }
        ],
        endpoints,
        expected_scenario_ids=["SCN-DELETED-EXTEND"],
    )

    assert compiled.accepted
    assert compiled.cases[0].title == "生成并删除邮箱后续期邮箱返回404"

    refresh = compile_scenario_drafts(
        [
            {
                "scenario_id": "SCN-REFRESH",
                "endpoint_method": "CHAIN",
                "intent": "生成邮箱后再次生成邮箱",
                "design_method": "dependency_chain",
                "expected_outcome": "同一邮箱刷新有效期",
                "chain_steps": [
                    {
                        "method": "GET",
                        "path": "/generate",
                        "query": {"prefix": "refresh-{{random}}"},
                        "extract": {"email": "$.email"},
                    },
                    {
                        "method": "GET",
                        "path": "/generate",
                        "query": {"prefix": "refresh-{{random}}"},
                        "assertions": [
                            {
                                "type": "json_path",
                                "target": "$.email",
                                "operator": "eq",
                                "expected": "{{email}}",
                            },
                            {
                                "type": "json_path",
                                "target": "$.expires_in",
                                "operator": "eq",
                                "expected": 600,
                            },
                        ],
                    },
                ],
            }
        ],
        endpoints,
        expected_scenario_ids=["SCN-REFRESH"],
    )

    assert refresh.accepted
    assert refresh.cases[0].title == "生成邮箱后再次生成邮箱并刷新有效期"


def test_string_transport_type_error_plan_is_skipped_instead_of_generating_false_case() -> None:
    endpoint = Endpoint(
        method="GET",
        path="/generate",
        parameters=[{"name": "prefix", "in": "query", "required": False, "schema": {"type": "string"}}],
        responses={"200": {}, "422": {}},
    )
    plan, issues = normalize_coverage_plan(
        [
            {
                "scenario_id": "SCN-FAKE-TYPE",
                "endpoint_method": "GET",
                "endpoint_path": "/generate",
                "design_method": "type_error",
                "title_hint": "传整数而不是字符串",
            }
        ],
        [endpoint],
    )
    assert plan == []
    assert any(item.code == "untestable_transport_type_error" and item.severity == "warning" for item in issues)
    completed = fill_deterministic_coverage_gaps(plan, [endpoint])
    assert all(item["design_method"] != "type_error" for item in completed)


def test_deterministic_coverage_adds_distinct_executable_and_safe_contract_scenarios() -> None:
    generate = Endpoint(
        method="GET",
        path="/generate",
        summary="生成临时邮箱",
        parameters=[{"name": "prefix", "in": "query", "required": False, "schema": {"type": "string"}}],
        responses={"200": {}, "422": {}, "429": {}},
    )
    messages = Endpoint(
        method="GET",
        path="/messages/{email}",
        parameters=[{"name": "email", "in": "path", "required": True, "schema": {"type": "string"}}],
        responses={"200": {}},
    )
    extend = Endpoint(
        method="POST",
        path="/extend/{email}",
        parameters=[{"name": "email", "in": "path", "required": True, "schema": {"type": "string"}}],
        responses={"200": {}, "404": {"description": "邮箱不存在"}},
    )
    plan = fill_deterministic_coverage_gaps([], [generate, messages, extend], dependency_graph={})
    by_endpoint = {
        endpoint.path: [
            item for item in plan
            if item["endpoint_method"] == endpoint.method and item["endpoint_path"] == endpoint.path
        ]
        for endpoint in (generate, messages, extend)
    }
    assert [item["design_method"] for item in by_endpoint["/generate"]] == [
        "happy_path",
        "optional_field",
        "status_code_coverage",
        "status_code_coverage",
    ]
    assert [
        (item["expected_basis"], item["runnable"], item["risk"])
        for item in by_endpoint["/generate"][2:]
    ] == [
        ("responses.422", False, "documented_response_without_fixture"),
        ("responses.429", False, "shared_rate_limit"),
    ]
    assert [item["design_method"] for item in by_endpoint["/messages/{email}"]] == ["happy_path"]
    assert [item["design_method"] for item in by_endpoint["/extend/{email}"]] == [
        "happy_path",
        "path_parameter_error",
    ]
    endpoint_by_key = {(item.method, item.path): item for item in (generate, messages, extend)}
    optional = fallback_draft(by_endpoint["/generate"][1], endpoint_by_key)
    assert optional["request_overrides"]["query"]["prefix"]
    path_error = fallback_draft(by_endpoint["/extend/{email}"][1], endpoint_by_key)
    assert path_error["expected_basis"] == "responses.404"
    assert "成功" not in path_error["expected_outcome"]


def test_markdown_contract_preserves_distinct_business_rules_and_negated_status() -> None:
    source = """
## 生成临时邮箱
```http
GET /api/generate
GET /api/generate?prefix={prefix}
```
- 不传 `prefix` 或传入空白 `prefix` 时，服务端随机生成 8 位邮箱前缀。
| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `prefix` | string | 否 | 最长 50 个字符，只能包含字母、数字、下划线和中划线；必须作为单个 `prefix` 参数传入。 |
以下情况会返回 `422`：
- 自定义前缀包含非法字符。
- 同一请求重复传入多个 `prefix` 参数。

## 查询邮箱邮件
```http
GET /api/messages/{email}
```
- 如果邮箱不存在，或已超过保留窗口被清理，返回空数组 `[]`，不会返回 404。

## 删除邮箱
```http
DELETE /api/delete/{email}
```
以下情况会返回 `404`：邮箱不存在、已被删除，或已超过保留窗口被清理。
"""
    parsed, _ = parse_api_document(source)
    endpoints = [merge_endpoint_contract(endpoint, None) for endpoint in parsed]
    generate = next(item for item in endpoints if item.path == "/api/generate")
    prefix = next(item for item in generate.parameters if item.get("name") == "prefix")
    assert prefix["schema"]["maxLength"] == 50
    assert prefix["schema"]["pattern"] == r"^[A-Za-z0-9_-]*$"
    assert prefix["x-iatest-allow-duplicate"] is False
    assert any("不传" in rule["title"] for rule in generate.business_rules)
    assert any("空白" in rule["title"] for rule in generate.business_rules)

    messages = next(item for item in endpoints if item.path == "/api/messages/{email}")
    assert all(rule["expected"].get("status") != 404 for rule in messages.business_rules)
    message_rules = [rule for rule in messages.business_rules if "空数组" in rule["title"]]
    assert len(message_rules) == 2
    assert len({rule["rule_id"] for rule in message_rules}) == 2
    assert len({rule["source_condition"] for rule in message_rules}) == 2
    assert {rule["source_evidence"] for rule in message_rules} == {
        "如果邮箱不存在，或已超过保留窗口被清理，返回空数组 `[]`，不会返回 404。"
    }

    delete = next(item for item in endpoints if item.path == "/api/delete/{email}")
    status_404 = [rule for rule in delete.business_rules if rule["expected"].get("status") == 404]
    assert len(status_404) == 3
    assert len({rule["rule_id"] for rule in status_404}) == 3
    assert len({rule["source_condition"] for rule in status_404}) == 3
    assert {rule["source_evidence"] for rule in status_404} == {
        "以下情况会返回 `404`：邮箱不存在、已被删除，或已超过保留窗口被清理。"
    }
    plan = fill_deterministic_coverage_gaps([], endpoints)
    assert len(
        [
            item
            for item in plan
            if item.get("expected_basis") == "responses.404"
            and item.get("rule_ids")
            and any(
                rule_id in {rule["rule_id"] for rule in status_404}
                for rule_id in item.get("rule_ids") or []
            )
        ]
    ) == 3


def test_markdown_recognition_deduplicates_route_variants_and_builds_response_contracts() -> None:
    source = """
## 生成邮箱
```http
GET /api/generate
GET /api/generate?prefix={prefix}
```
- 不传 `prefix` 或传入空白 `prefix` 时，随机生成 8 位前缀。
| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `prefix` | string | 否 | 最长 50 个字符，只能包含字母、数字、下划线和中划线。 |
### 成功响应
```json
{"email":"abc@mail.tempbox.cn","created_at":123,"expires_in":600}
```
### 错误响应
以下情况会返回 `422`：
- 自定义前缀超过长度限制。
```json
{"detail":"invalid prefix"}
```
"""
    parsed, _ = parse_api_document(source)
    assert len(parsed) == 1
    endpoint = merge_endpoint_contract(parsed[0], None)
    assert set(endpoint.responses) == {"200", "422"}
    success_schema = endpoint.responses["200"]["content"]["application/json"]["schema"]
    assert success_schema["properties"]["created_at"]["type"] == "integer"
    assert endpoint.responses["422"]["content"]["application/json"]["example"]["detail"] == "invalid prefix"

    omitted = next(rule for rule in endpoint.business_rules if rule["title"].startswith("不传"))
    assert omitted["trigger"]["omit_fields"] == ["query.prefix"]
    assert omitted["trigger"]["request_overrides"] == {}
    overlength = next(rule for rule in endpoint.business_rules if "超过长度限制" in rule["title"])
    assert len(overlength["trigger"]["request_overrides"]["query"]["prefix"]) == 51
    assert {
        (item["target"], item["operator"], item["expected"])
        for item in overlength["expected"]["assertions"]
    } >= {
        ("", "eq", 422),
        ("$.detail", "eq", "invalid prefix"),
    }


def test_contract_merge_removes_nested_model_rule_from_trigger() -> None:
    deterministic = Endpoint(
        method="GET",
        path="/api/generate",
        parameters=[
            {
                "name": "prefix",
                "in": "query",
                "required": False,
                "schema": {"type": "string", "maxLength": 50},
            }
        ],
        responses={"422": {}},
        business_rules=[
            {
                "description": "自定义前缀超过长度限制，返回 422",
                "source_evidence": "自定义前缀超过长度限制，返回 422",
            }
        ],
    )
    semantic = Endpoint(
        method="GET",
        path="/api/generate",
        business_rules=[
            {
                "rule_id": "GEN-006",
                "description": "自定义前缀长度限制校验",
                "source_evidence": "以下情况会返回 422：自定义前缀超过长度限制。",
                "trigger": {
                    "request_overrides": {"prefix": "a"},
                    "nested_rule": {
                        "rule_id": "GEN-007",
                        "title": "重复传入多个 prefix 参数",
                    },
                },
                "expected": {
                    "status": 422,
                    "assertions": [
                        {"type": "json_path", "target": "$.unknown", "operator": "eq", "expected": "bad"}
                    ],
                },
            }
        ],
    )

    rule = merge_endpoint_contract(deterministic, semantic).business_rules[0]
    assert set(rule["trigger"]) == {"request_overrides", "omit_fields", "chain_steps"}
    assert len(rule["trigger"]["request_overrides"]["query"]["prefix"]) == 51
    assert all(item.get("target") != "$.unknown" for item in rule["expected"]["assertions"])


def test_contract_replaces_invalid_null_assertions_and_keeps_parser_runnable_policy() -> None:
    deterministic = Endpoint(
        method="GET",
        path="/api/generate",
        responses={
            "200": {
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {
                                "email": {"type": "string"},
                                "created_at": {"type": "integer"},
                                "expires_in": {"type": "integer"},
                            },
                        },
                        "example": {
                            "email": "abc12345@mail.tempbox.cn",
                            "created_at": 1710000000,
                            "expires_in": 600,
                        },
                    }
                }
            }
        },
        business_rules=[
            {
                "title": "不传 prefix 时随机生成 8 位邮箱前缀",
                "description": "不传 prefix 时随机生成 8 位邮箱前缀",
                "source_evidence": "不传 prefix 时随机生成 8 位邮箱前缀",
                "expected_status": 200,
            }
        ],
    )
    semantic = Endpoint(
        method="GET",
        path="/api/generate",
        business_rules=[
            {
                "title": "不传 prefix 时随机生成 8 位邮箱前缀",
                "description": "不传 prefix 时随机生成 8 位邮箱前缀",
                "source_evidence": "不传 prefix 时随机生成 8 位邮箱前缀",
                "runnable": False,
                "expected": {
                    "status": 200,
                    "description": "返回 email，created_at 为时间戳，expires_in 为 600",
                    "assertions": [
                        {
                            "type": "json_path",
                            "target": "$.created_at",
                            "operator": "eq",
                            "expected": None,
                        },
                        {
                            "type": "json_path",
                            "target": "$.expires_in",
                            "operator": "eq",
                            "expected": None,
                        },
                    ],
                },
            }
        ],
    )

    rule = merge_endpoint_contract(deterministic, semantic).business_rules[0]
    assertions = {
        (item["target"], item["operator"], item["expected"])
        for item in rule["expected"]["assertions"]
    }
    assert rule["runnable"] is True
    assert ("$.created_at", "eq", None) not in assertions
    assert ("$.expires_in", "eq", None) not in assertions
    assert ("$.created_at", "is_integer", True) in assertions
    assert ("$.expires_in", "eq", 600) in assertions


def test_contract_required_response_fields_and_rule_strategy_are_deterministic() -> None:
    endpoint = normalize_endpoint_contract(
        Endpoint(
            method="GET",
            path="/api/generate",
            responses={
                "200": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "email": {"type": "string"},
                                    "created_at": {"type": "integer"},
                                    "expires_in": {"type": "integer", "default": 600},
                                },
                                "required": ["email", "created_at", "expires_in"],
                            }
                        }
                    }
                }
            },
            business_rules=[
                {
                    "title": "不传 prefix 时随机生成 8 位邮箱前缀",
                    "description": "不传 prefix 时随机生成 8 位邮箱前缀",
                    "source_evidence": "不传 prefix 时随机生成 8 位邮箱前缀",
                    "expected_status": 200,
                    "strategy": "positive",
                }
            ],
        )
    )
    rule = endpoint.business_rules[0]
    assertions = {
        (item["target"], item["operator"], item["expected"])
        for item in rule["expected"]["assertions"]
    }
    assert rule["strategy"] == "optional_field"
    assert ("$.email", "is_string", True) in assertions
    assert ("$.created_at", "is_integer", True) in assertions
    assert ("$.expires_in", "eq", 600) in assertions

    overlength = normalize_business_rules(
        [
            {
                "title": "自定义前缀超过 50 个字符时返回 422",
                "description": "自定义前缀超过 50 个字符时返回 422",
                "source_evidence": "自定义前缀超过长度限制。",
                "strategy": "negative",
                "expected_status": "422",
                "assertions": [
                    {
                        "type": "status_code",
                        "target": "",
                        "operator": "eq",
                        "expected": "422",
                    }
                ],
            }
        ],
        method="GET",
        path="/api/generate",
    )[0]
    assert overlength["testable"] is True
    assert overlength["strategy"] == "boundary_value"
    assert overlength["expected"]["status"] == 422
    assert overlength["expected"]["assertions"][0]["expected"] == 422


def test_deleted_resource_rule_builds_real_generate_delete_consume_chain() -> None:
    generate = Endpoint(
        method="GET",
        path="/api/generate",
        responses={"200": {"schema": {"type": "object", "properties": {"email": {"type": "string"}}}}},
    )
    delete = Endpoint(
        method="DELETE",
        path="/api/delete/{email}",
        parameters=[{"name": "email", "in": "path", "required": True, "schema": {"type": "string"}}],
        responses={"200": {}, "404": {}},
    )
    extend = normalize_endpoint_contract(
        Endpoint(
            method="POST",
            path="/api/extend/{email}",
            parameters=[{"name": "email", "in": "path", "required": True, "schema": {"type": "string"}}],
            responses={"200": {}, "404": {}},
            business_rules=[
                {
                    "rule_id": "BR-DELETED",
                    "description": "邮箱已被用户删除时调用续期邮箱，返回 404",
                    "source_evidence": "邮箱已被用户删除，不能续期并返回 404",
                    "expected": {"status": 404},
                }
            ],
        )
    )

    plan = fill_deterministic_coverage_gaps([], [generate, delete, extend])
    item = next(entry for entry in plan if entry.get("rule_ids") == ["BR-DELETED"])
    assert item["endpoint_method"] == "CHAIN"
    assert [(step["method"], step["path"]) for step in item["chain_steps"]] == [
        ("GET", "/api/generate"),
        ("DELETE", "/api/delete/{email}"),
        ("POST", "/api/extend/{email}"),
    ]
    assert item["chain_steps"][0]["extract"] == {"email": "$.email"}
    assert item["runnable"] is True


def test_delete_postcondition_builds_query_and_renew_verification_chain() -> None:
    endpoints = [
        Endpoint(method="GET", path="/api/generate", responses={"200": {}}),
        Endpoint(
            method="GET",
            path="/api/messages/{email}",
            parameters=[{"name": "email", "in": "path", "required": True}],
            responses={"200": {}},
        ),
        Endpoint(
            method="POST",
            path="/api/extend/{email}",
            parameters=[{"name": "email", "in": "path", "required": True}],
            responses={"200": {}, "404": {}},
        ),
        normalize_endpoint_contract(
            Endpoint(
                method="DELETE",
                path="/api/delete/{email}",
                parameters=[{"name": "email", "in": "path", "required": True}],
                responses={"200": {}, "404": {}},
                business_rules=[
                    {
                        "rule_id": "BR-DELETE-POST",
                        "title": "删除成功后不能再查询邮件、接收邮件或续期",
                        "description": "删除成功后不能再查询邮件、接收邮件或续期",
                        "source_evidence": "删除成功后不能再查询邮件、接收邮件或续期。",
                        "expected_status": 200,
                    }
                ],
            )
        ),
    ]

    plan = fill_deterministic_coverage_gaps([], endpoints)
    contract_rule = endpoints[-1].business_rules[0]
    assert [(step["method"], step["path"]) for step in contract_rule["trigger"]["chain_steps"]] == [
        ("GET", "/api/generate"),
        ("DELETE", "/api/delete/{email}"),
        ("GET", "/api/messages/{email}"),
        ("POST", "/api/extend/{email}"),
    ]
    item = next(entry for entry in plan if entry.get("rule_ids") == ["BR-DELETE-POST"])
    assert item["endpoint_method"] == "CHAIN"
    assert [(step["method"], step["path"]) for step in item["chain_steps"]] == [
        ("GET", "/api/generate"),
        ("DELETE", "/api/delete/{email}"),
        ("GET", "/api/messages/{email}"),
        ("POST", "/api/extend/{email}"),
    ]
    assert item["assertions"] == []
    assert item["chain_steps"][2]["assertions"][-1] == {
        "type": "json_path",
        "target": "$",
        "operator": "length_eq",
        "expected": 0,
    }
    assert item["chain_steps"][3]["assertions"][0]["expected"] == 404


def test_coverage_manifest_rejects_rule_lineage_without_obligatory_assertion() -> None:
    endpoint = normalize_endpoint_contract(
        Endpoint(
            method="GET",
            path="/check",
            business_rules=[
                {
                    "rule_id": "BR-CHECK",
                    "description": "邮箱不存在时返回 valid: false",
                    "testable": True,
                    "expected": {
                        "status": 200,
                        "assertions": [
                            {
                                "type": "json_path",
                                "target": "$.valid",
                                "operator": "eq",
                                "expected": False,
                            }
                        ],
                    },
                }
            ],
        )
    )
    case = ApiTestCase(
        id="TC-API-0001",
        title="检查不存在邮箱",
        method="GET",
        path="/check",
        assertions=[Assertion("status_code", expected=200)],
        rule_ids=["BR-CHECK"],
    )
    issues = coverage_manifest_issues([endpoint], [case])
    assert any("没有实现契约要求的响应校验" in item["reason"] for item in issues)
    case.assertions.append(Assertion("json_path", "$.valid", "eq", False))
    assert coverage_manifest_issues([endpoint], [case]) == []


def test_coverage_manifest_accepts_stronger_assertion_for_schema_type_obligation() -> None:
    endpoint = normalize_endpoint_contract(
        Endpoint(
            method="GET",
            path="/generate",
            responses={
                "200": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"email": {"type": "string"}},
                                "required": ["email"],
                            }
                        }
                    }
                }
            },
            business_rules=[
                {
                    "rule_id": "BR-EMAIL",
                    "title": "返回固定域名邮箱",
                    "source_evidence": "邮箱域名固定为 mail.tempbox.cn",
                    "expected_status": 200,
                    "assertions": [
                        {
                            "type": "json_path",
                            "target": "$.email",
                            "operator": "matches",
                            "expected": r"^[^@]+@mail\.tempbox\.cn$",
                        }
                    ],
                }
            ],
        )
    )
    case = ApiTestCase(
        id="TC-API-0001",
        title="固定域名邮箱",
        method="GET",
        path="/generate",
        assertions=[
            Assertion("status_code", expected=200),
            Assertion("json_path", "$.email", "matches", r"^[^@]+@mail\.tempbox\.cn$"),
        ],
        rule_ids=["BR-EMAIL"],
    )
    assert coverage_manifest_issues([endpoint], [case]) == []


def test_explicit_invalid_character_examples_compile_to_independent_coverage_partitions() -> None:
    endpoint = normalize_endpoint_contract(
        Endpoint(
            method="GET",
            path="/api/generate",
            summary="生成临时邮箱",
            parameters=[
                {
                    "name": "prefix",
                    "in": "query",
                    "required": False,
                    "schema": {
                        "type": "string",
                        "pattern": r"^[A-Za-z0-9_-]*$",
                    },
                }
            ],
            responses={"422": {}},
            business_rules=[
                {
                    "rule_id": "GEN-004",
                    "title": "自定义前缀包含非法字符，例如 `@`、空格、中文等",
                    "description": "自定义前缀包含非法字符，例如 `@`、空格、中文等",
                    "source_evidence": "自定义前缀包含非法字符，例如 `@`、空格、中文等。",
                    "expected": {"status": 422},
                }
            ],
        )
    )
    rule = endpoint.business_rules[0]
    assert [item["partition_id"] for item in rule["coverage_partitions"]] == [
        "invalid_symbol",
        "embedded_space",
        "unicode_chinese",
    ]

    plan = [
        item
        for item in fill_deterministic_coverage_gaps([], [endpoint])
        if item.get("rule_ids") == ["GEN-004"]
    ]
    assert [item["request_overrides"]["query"]["prefix"] for item in plan] == [
        "invalid@",
        "invalid prefix",
        "中文",
    ]
    assert [item["coverage_partition_ids"][0] for item in plan] == [
        "GEN-004#invalid_symbol",
        "GEN-004#embedded_space",
        "GEN-004#unicode_chinese",
    ]

    endpoint_by_key = {(endpoint.method, endpoint.path): endpoint}
    drafts = [fallback_draft(item, endpoint_by_key) for item in plan]
    compilation = compile_scenario_drafts(
        drafts,
        [endpoint],
        expected_scenario_ids=[item["scenario_id"] for item in plan],
    )
    assert compilation.accepted, [item.to_dict() for item in compilation.issues]
    assert len(compilation.cases) == 3
    assert coverage_manifest_issues([endpoint], compilation.cases) == []

    dishonest_single_case = copy.deepcopy(compilation.cases[0])
    dishonest_single_case.coverage_partition_ids = [
        item["coverage_partition_ids"][0]
        for item in plan
    ]
    missing = coverage_manifest_issues([endpoint], [dishonest_single_case])
    assert {
        item["location"]
        for item in missing
        if item["reason"] == "最终用例集没有用例声明并执行该独立异常触发条件。"
    } == {
        "GEN-004#embedded_space",
        "GEN-004#unicode_chinese",
    }


def test_generic_invalid_character_rule_keeps_single_representative_case() -> None:
    endpoint = normalize_endpoint_contract(
        Endpoint(
            method="GET",
            path="/api/generate",
            parameters=[
                {
                    "name": "prefix",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string"},
                }
            ],
            responses={"422": {}},
            business_rules=[
                {
                    "rule_id": "GEN-INVALID",
                    "title": "自定义前缀包含非法字符时返回 422",
                    "source_evidence": "自定义前缀包含非法字符时返回 422。",
                    "expected_status": 422,
                }
            ],
        )
    )

    assert endpoint.business_rules[0]["coverage_partitions"] == []
    plan = [
        item
        for item in fill_deterministic_coverage_gaps([], [endpoint])
        if item.get("rule_ids") == ["GEN-INVALID"]
    ]
    assert len(plan) == 1
    assert plan[0]["request_overrides"] == {"query": {"prefix": "invalid@"}}
    assert plan[0]["coverage_partition_ids"] == []


def test_plan_contract_fields_override_model_request_and_assertion_drift() -> None:
    plan = [
        {
            "scenario_id": "SCN-RULE-OMIT",
            "endpoint_method": "GET",
            "endpoint_path": "/generate",
            "design_method": "optional_field",
            "expected_basis": "responses.200",
            "request_overrides": {},
            "omit_fields": ["query.prefix"],
            "assertions": [
                {
                    "type": "json_path",
                    "target": "$.expires_in",
                    "operator": "is_integer",
                    "expected": True,
                }
            ],
            "rule_ids": ["BR-OMIT"],
        }
    ]
    model_drafts = [
        {
            "scenario_id": "SCN-RULE-OMIT",
            "endpoint_method": "POST",
            "endpoint_path": "/wrong",
            "intent": "不传 prefix 时随机生成邮箱",
            "design_method": "happy_path",
            "request_overrides": {"query": {"prefix": "model-drift"}},
            "omit_fields": [],
            "expected_outcome": "成功",
            "assertions": [],
        }
    ]
    repaired = apply_plan_contract_fields(model_drafts, plan)[0]
    assert repaired["endpoint_method"] == "GET"
    assert repaired["endpoint_path"] == "/generate"
    assert repaired["design_method"] == "optional_field"
    assert repaired["omit_fields"] == ["query.prefix"]
    assert repaired["assertions"][0]["target"] == "$.expires_in"
    assert repaired["rule_ids"] == ["BR-OMIT"]


def test_strategy_context_distinguishes_global_enablement_from_schema_applicability(tmp_path) -> None:
    endpoint = Endpoint(
        method="GET",
        path="/generate",
        parameters=[{"name": "prefix", "in": "query", "required": False, "schema": {"type": "string"}}],
        responses={"200": {}, "422": {}, "429": {}},
    )
    context = case_generation_strategy_context(tmp_path, [endpoint])
    applicability = context["strategy_applicability"]
    assert applicability["optional_field"]["applicable"] is True
    assert applicability["status_code_coverage"]["matching_count"] == 2
    assert applicability["type_error"]["applicable"] is False
    assert "string" in applicability["type_error"]["unavailable_reason"]
    assert applicability["boundary_value"]["applicable"] is False


def test_compiler_rejects_duplicate_execution_and_error_status_success_claim() -> None:
    health = Endpoint(method="GET", path="/health", responses={"200": {}})
    duplicate = compile_scenario_drafts(
        [
            {
                "scenario_id": "SCN-HAPPY",
                "endpoint_method": "GET",
                "endpoint_path": "/health",
                "intent": "主流程",
                "design_method": "happy_path",
                "expected_outcome": "成功",
            },
            {
                "scenario_id": "SCN-EQUIVALENT",
                "endpoint_method": "GET",
                "endpoint_path": "/health",
                "intent": "有效等价类",
                "design_method": "equivalence_class",
                "expected_outcome": "成功",
            },
        ],
        [health],
        expected_scenario_ids=["SCN-HAPPY", "SCN-EQUIVALENT"],
    )
    assert any(item.code == "duplicate_compiled_behavior" for item in duplicate.error_issues)

    delete = Endpoint(
        method="DELETE",
        path="/users/{id}",
        parameters=[{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
        responses={"404": {}},
    )
    conflict = compile_scenario_drafts(
        [
            {
                "scenario_id": "SCN-CONFLICT",
                "endpoint_method": "DELETE",
                "endpoint_path": "/users/{id}",
                "intent": "删除不存在用户",
                "design_method": "status_code_coverage",
                "request_overrides": {"path_params": {"id": "missing-user"}},
                "expected_outcome": "删除成功",
                "expected_basis": "responses.404",
            }
        ],
        [delete],
        expected_scenario_ids=["SCN-CONFLICT"],
    )
    assert any(
        item.code == "error_status_success_expectation_conflict"
        for item in conflict.error_issues
    )


def test_chain_compiler_adds_response_contract_assertions_and_negative_semantics() -> None:
    generate = Endpoint(
        method="GET",
        path="/generate",
        responses={
            "200": {
                "schema": {
                    "type": "object",
                    "properties": {"email": {"type": "string"}},
                }
            }
        },
    )
    delete = Endpoint(
        method="DELETE",
        path="/delete/{email}",
        parameters=[{"name": "email", "in": "path", "required": True, "schema": {"type": "string"}}],
        responses={"200": {}, "404": {}},
    )
    compiled = compile_scenario_drafts(
        [
            {
                "scenario_id": "SCN-DOUBLE-DELETE",
                "endpoint_method": "CHAIN",
                "intent": "重复删除同一邮箱第二次返回404",
                "design_method": "dependency_chain",
                "expected_outcome": "首次删除返回200，第二次删除返回404",
                "expected_basis": "responses.404",
                "chain_steps": [
                    {"method": "GET", "path": "/generate", "extract": {"email": "$.email"}},
                    {
                        "method": "DELETE",
                        "path": "/delete/{email}",
                        "path_params": {"email": "{{email}}"},
                        "assertions": [{"type": "status_code", "expected": 200}],
                    },
                    {
                        "method": "DELETE",
                        "path": "/delete/{email}",
                        "path_params": {"email": "{{email}}"},
                        "assertions": [{"type": "status_code", "expected": 404}],
                    },
                ],
            }
        ],
        [generate, delete],
        expected_scenario_ids=["SCN-DOUBLE-DELETE"],
    )
    assert compiled.accepted, [item.to_dict() for item in compiled.issues]
    case = compiled.cases[0]
    assert case.negative is True
    assert "negative" in case.tags
    assert any(
        item.target == "$.email" and item.operator == "is_string"
        for item in case.steps[0].assertions
    )


def test_path_underscore_override_alias_is_applied() -> None:
    endpoint = Endpoint(
        method="DELETE",
        path="/users/{id}",
        parameters=[{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
        responses={"404": {}},
    )
    compiled = compile_scenario_drafts(
        [
            {
                "scenario_id": "SCN-PATH-UNDERSCORE",
                "endpoint_method": "DELETE",
                "endpoint_path": "/users/{id}",
                "intent": "删除不存在用户",
                "design_method": "status_code_coverage",
                "expected_outcome": "返回 404",
                "expected_basis": "responses.404",
                "request_overrides": {"path_id": "missing-user"},
            }
        ],
        [endpoint],
        expected_scenario_ids=["SCN-PATH-UNDERSCORE"],
    )
    assert compiled.accepted, [item.to_dict() for item in compiled.issues]
    assert compiled.cases[0].path_params == {"id": "missing-user"}


def test_response_shape_order_and_explicit_value_are_completed_from_semantics() -> None:
    messages = Endpoint(
        method="GET",
        path="/messages",
        responses={
            "200": {
                "schema": {
                    "type": "array",
                    "items": {"type": "object", "properties": {"received_at": {"type": "integer"}}},
                }
            }
        },
    )
    compiled = compile_scenario_drafts(
        [
            {
                "scenario_id": "SCN-SORT",
                "endpoint_method": "GET",
                "endpoint_path": "/messages",
                "intent": "查询邮件",
                "design_method": "happy_path",
                "expected_outcome": "返回邮件数组，按 received_at 倒序排列",
                "assertions": [{"type": "status_code", "expected": 200}],
            }
        ],
        [messages],
        expected_scenario_ids=["SCN-SORT"],
    )
    assert compiled.accepted, [item.to_dict() for item in compiled.issues]
    assertions = compiled.cases[0].assertions
    assert any(item.operator == "is_array" for item in assertions)
    order = next(item for item in assertions if item.operator == "sorted_desc_by")
    assert evaluate(order, {"json": [{"received_at": 2}, {"received_at": 1}]}) is None
    assert evaluate(order, {"json": [{"received_at": 1}, {"received_at": 2}]}) is not None

    extend = Endpoint(
        method="POST",
        path="/extend",
        responses={"200": {"schema": {"type": "object", "properties": {"expires_in": {"type": "integer"}}}}},
    )
    value_case = compile_scenario_drafts(
        [
            {
                "scenario_id": "SCN-VALUE",
                "endpoint_method": "POST",
                "endpoint_path": "/extend",
                "intent": "续期",
                "design_method": "happy_path",
                "expected_outcome": "返回 expires_in 整数（600）",
                "assertions": [
                    {"type": "status_code", "expected": 200},
                    {"type": "json_field", "field": "expires_in", "expected": "integer"},
                ],
            }
        ],
        [extend],
        expected_scenario_ids=["SCN-VALUE"],
    )
    assert value_case.accepted, [item.to_dict() for item in value_case.issues]
    assert any(item.target == "$.expires_in" and item.operator == "eq" and item.expected == 600 for item in value_case.cases[0].assertions)


def test_failed_resource_creation_does_not_attach_impossible_cleanup() -> None:
    generate = Endpoint(
        method="GET",
        path="/generate",
        parameters=[{"name": "prefix", "in": "query", "required": False, "schema": {"type": "string"}}],
        responses={
            "200": {"schema": {"type": "object", "properties": {"email": {"type": "string"}}}},
            "422": {"schema": {"type": "object", "properties": {"detail": {"type": "string"}}}},
        },
    )
    delete = Endpoint(
        method="DELETE",
        path="/delete/{email}",
        parameters=[{"name": "email", "in": "path", "required": True, "schema": {"type": "string"}}],
        responses={"200": {}, "404": {}},
    )
    compiled = compile_scenario_drafts(
        [
            {
                "scenario_id": "SCN-REJECTED-CREATE",
                "endpoint_method": "GET",
                "endpoint_path": "/generate",
                "intent": "非法前缀",
                "design_method": "equivalence_class",
                "expected_outcome": "返回 422，detail 为错误信息",
                "request_overrides": {"query": {"prefix": "invalid@"}},
                "assertions": [{"type": "status_code", "expected": 422}],
            }
        ],
        [generate, delete],
        expected_scenario_ids=["SCN-REJECTED-CREATE"],
    )
    assert compiled.accepted, [item.to_dict() for item in compiled.issues]
    assert compiled.cases[0].negative
    assert compiled.cases[0].cleanup_steps == []


def test_provider_tool_protocol_normalization() -> None:
    openai_calls = parse_openai_tool_calls(
        [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "get_endpoint_detail", "arguments": '{"method":"GET","path":"/health"}'},
            }
        ]
    )
    assert openai_calls[0].arguments == {"method": "GET", "path": "/health"}

    anthropic_calls = parse_anthropic_tool_calls(
        [{"type": "tool_use", "id": "tool-1", "name": "list_endpoints", "input": {"limit": 10}}]
    )
    assert anthropic_calls[0].name == "list_endpoints"
    assert anthropic_calls[0].arguments == {"limit": 10}

    system, messages = to_anthropic_messages(
        [
            {"role": "system", "content": "identity"},
            {"role": "user", "content": "task"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [openai_calls[0].to_openai_dict()],
            },
            {"role": "tool", "tool_call_id": "call-1", "name": "get_endpoint_detail", "content": "{}"},
        ]
    )
    assert system == "identity"
    assert messages[-2]["content"][0]["type"] == "tool_use"
    assert messages[-1]["content"][0]["type"] == "tool_result"

    envelope = parse_tool_envelope('```json\n{"tool":"list_endpoints","arguments":{"limit":5}}\n```')
    assert envelope is not None
    assert envelope.arguments == {"limit": 5}


def test_provider_repairs_malformed_tool_arguments_before_execution() -> None:
    calls = parse_openai_tool_calls(
        [
            {
                "id": "repair-1",
                "function": {
                    "name": "submit_endpoint_drafts",
                    "arguments": (
                        '{"endpoints":[{"method":"GET" "path":"/health",'
                        '"summary":"健康检查","business_rules":[]}]}'
                    ),
                },
            }
        ]
    )

    assert calls[0].parse_error == ""
    assert calls[0].repair_warning
    assert calls[0].arguments["endpoints"][0]["path"] == "/health"
    assert json.loads(calls[0].raw_arguments) == calls[0].arguments


def test_provider_gateway_forwards_forced_terminal_tool_choice(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return ModelResponse(text="", tool_calls=[], usage=TokenUsage(), finish_reason="stop")

    monkeypatch.setattr("ai_apitest.ai.model_gateway.model_completion", fake_completion)
    gateway = ProviderModelGateway(
        {
            "provider": "openai-compatible",
            "base_url": "https://unused.invalid/v1",
            "model": "kimi-k2.6",
            "api_key_env": "UNUSED",
            "tool_choice": {
                "type": "function",
                "function": {"name": "submit_quality_review"},
            },
            "thinking": {"type": "disabled"},
        }
    )
    gateway.complete(messages=[], tools=[], timeout=10, max_tokens=100)

    assert captured["tool_choice"]["function"]["name"] == "submit_quality_review"
    assert captured["thinking"] == {"type": "disabled"}
    assert uses_kimi_thinking_protocol(gateway.model_config)
    assert not uses_kimi_thinking_protocol(
        {
            "provider": "openai-compatible",
            "base_url": "https://api.example.test/v1",
            "model": "other-model",
        }
    )


def test_agent_audit_redacts_credentials_without_losing_schema() -> None:
    assert redact_sensitive({"headers": {"Authorization": "Bearer secret"}, "path": "/users"}) == {
        "headers": {"Authorization": "[REDACTED]"},
        "path": "/users",
    }
    redacted = redact_text('{"password":"plain","schema":{"properties":{"password":{"type":"string"}}}}')
    assert '"password":"[REDACTED]"' in redacted
    assert '"password":{"type":"string"}' in redacted


class QueueGateway:
    def __init__(self, responses: list[ModelResponse], *, usage: int = 20) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.model_config = {
            "provider": "fake",
            "model": "fake-model",
            "default_output_tokens": 1000,
            "supports_tools": True,
        }
        self.usage = usage

    def complete(self, **kwargs) -> ModelResponse:
        self.requests.append(copy.deepcopy(kwargs))
        if not self.responses:
            raise AssertionError("unexpected model turn")
        return self.responses.pop(0)


def tool_response(name: str, arguments: dict[str, Any], *, call_id: str = "call-1", total_tokens: int = 20) -> ModelResponse:
    return ModelResponse(
        tool_calls=[
            ModelToolCall(
                id=call_id,
                name=name,
                arguments=arguments,
                raw_arguments=json.dumps(arguments, ensure_ascii=False),
            )
        ],
        usage=TokenUsage(total_tokens // 2, total_tokens - total_tokens // 2, total_tokens),
        finish_reason="tool_calls",
    )


def test_discovery_agent_normalizes_html_and_recovers_partial_terminal_submission(tmp_path) -> None:
    markdown = """# API
## One
```http
GET /one
```
## Two
```http
POST /two
```
"""
    document = f'<html><style>{"x" * 5000}</style><script id="markdown-source">{markdown}</script></html>'
    expected = [
        Endpoint(method="GET", path="/one", summary="One", responses={"200": {}}),
        Endpoint(method="POST", path="/two", summary="Two", responses={"200": {}}),
    ]
    gateway = QueueGateway(
        [
            tool_response("get_document_info", {}, call_id="info"),
            tool_response("read_document_chunk", {"offset": 0, "max_chars": 12000}, call_id="read"),
            tool_response(
                "submit_endpoint_drafts",
                {"endpoints": [{"method": "GET", "path": "/one", "summary": "One"}]},
                call_id="partial",
            ),
            tool_response(
                "submit_endpoint_drafts",
                {"endpoints": [{"method": "POST", "path": "/two", "summary": "Two"}]},
                call_id="complete",
            ),
        ]
    )

    endpoints = discover_endpoints_with_agent_runtime(
        tmp_path,
        "demo",
        document,
        gateway.model_config,
        expected_endpoints=expected,
        gateway_factory=lambda model_config: gateway,
    )

    assert [(item.method, item.path) for item in endpoints] == [("GET", "/one"), ("POST", "/two")]
    checkpoint_messages = gateway.requests[3]["messages"]
    assert [message["role"] for message in checkpoint_messages] == ["system", "user"]
    assert "接口识别分段检查点" in checkpoint_messages[1]["content"]
    assert 'missing=[\"POST /two\"]' in checkpoint_messages[1]["content"]
    run = AgentRunRepository(tmp_path, "demo").list_recent(limit=1)[0]
    metadata = json.loads(run["metadata"])
    assert metadata["document_chars"] > metadata["normalized_document_chars"]
    assert metadata["expected_endpoint_count"] == 2


def test_agent_runtime_executes_bounded_loop_and_persists_audit(tmp_path) -> None:
    endpoint = Endpoint(method="GET", path="/health", summary="健康检查", responses={"200": {}})
    data = ProjectDataAccess.create(tmp_path, "demo", endpoints=[endpoint])
    context = ToolExecutionContext(tmp_path, "demo", data)
    gateway = QueueGateway(
        [
            tool_response("list_endpoints", {"limit": 10}, call_id="list"),
            tool_response(
                "submit_coverage_plan",
                {
                    "items": [
                        {
                            "scenario_id": "SCN-001",
                            "endpoint_method": "GET",
                            "endpoint_path": "/health",
                            "design_method": "happy_path",
                            "priority": "P0",
                            "title_hint": "健康检查",
                            "coverage_tags": ["happy_path"],
                        }
                    ]
                },
                call_id="submit",
            ),
        ]
    )
    audit = AgentRunRepository(tmp_path, "demo")
    runtime = AgentRuntime(
        roles=default_role_registry(),
        tools=build_case_generation_tool_registry(),
        gateway=gateway,
        audit=audit,
    )
    result = runtime.run(role_id="test_planner", task_prompt="规划健康检查", tool_context=context)
    assert result.artifact[0]["scenario_id"] == "SCN-001"
    run = audit.get(result.run_id)
    assert run is not None
    assert run["status"] == "completed"
    assert run["turn_count"] == 2
    assert run["tool_call_count"] == 2
    assert run["total_tokens"] == 40


def test_agent_runtime_rebases_context_and_rehydrates_persisted_evidence(tmp_path) -> None:
    role = RoleSpec(
        id="memory_role",
        version="1",
        mission="验证自动上下文恢复",
        system_prompt="只按任务和恢复检查点调用工具。",
        model_role="case_generator",
        input_contract="Task",
        output_contract="Result",
        allowed_tools=("long_read", "read_task_evidence", "submit_done"),
        max_tool_rounds=5,
        max_tool_calls=5,
        token_budget=50000,
    )
    tools = ToolRegistry()
    tools.register(
        ToolSpec(
            name="long_read",
            description="返回长证据",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=lambda arguments, context: {"payload": "evidence-marker-" + "x" * 16000},
            allowed_roles=("memory_role",),
            result_limit_chars=20000,
        )
    )
    tools.register(
        ToolSpec(
            name="read_task_evidence",
            description="分页读取上下文恢复证据",
            input_schema={
                "type": "object",
                "properties": {
                    "evidence_id": {"type": "string"},
                    "offset": {"type": "integer"},
                    "max_chars": {"type": "integer"},
                },
                "required": ["evidence_id"],
                "additionalProperties": False,
            },
            handler=lambda arguments, context: context.evidence_reader(
                str(arguments["evidence_id"]),
                int(arguments.get("offset") or 0),
                int(arguments.get("max_chars") or 1000),
            ),
            allowed_roles=("memory_role",),
            result_limit_chars=4000,
        )
    )
    tools.register(
        ToolSpec(
            name="submit_done",
            description="提交完成结果",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=lambda arguments, context: ToolExecutionResult(
                content="done",
                value={"ok": True},
                terminal=True,
            ),
            allowed_roles=("memory_role",),
            side_effect="transform",
            terminal=True,
        )
    )

    class MemoryGateway:
        model_config = {
            "provider": "fake",
            "model": "memory-model",
            "context_window": 8000,
            "default_output_tokens": 1000,
            "supports_tools": True,
        }

        def __init__(self) -> None:
            self.turn = 0
            self.messages: list[list[dict[str, Any]]] = []
            self.evidence_id = ""

        def complete(self, *, messages, **kwargs) -> ModelResponse:
            self.turn += 1
            self.messages.append(messages)
            if self.turn == 1:
                return tool_response("long_read", {}, call_id="long", total_tokens=100)
            if self.turn == 2:
                assert len(messages) == 2
                recovery = str(messages[1]["content"])
                assert "自动上下文恢复检查点" in recovery
                evidence_ids = re.findall(r"evidence-[0-9a-f]{20}", recovery)
                assert evidence_ids
                self.evidence_id = evidence_ids[-1]
                return tool_response(
                    "read_task_evidence",
                    {"evidence_id": self.evidence_id, "offset": 0, "max_chars": 1000},
                    call_id="rehydrate",
                    total_tokens=100,
                )
            tool_messages = [item for item in messages if item.get("role") == "tool"]
            assert tool_messages
            assert "evidence-marker-" in str(tool_messages[-1]["content"])
            return tool_response("submit_done", {}, call_id="done", total_tokens=100)

    config = load_config(tmp_path)
    config["agents"]["context_recovery"].update(
        {
            "soft_ratio": 0.40,
            "rebase_ratio": 0.55,
            "circuit_breaker_ratio": 0.90,
            "target_ratio": 0.25,
            "safety_margin_tokens": 500,
        }
    )
    save_config(tmp_path, config)
    gateway = MemoryGateway()
    audit = AgentRunRepository(tmp_path, "demo")
    runtime = AgentRuntime(
        roles=RoleRegistry([role]),
        tools=tools,
        gateway=gateway,
        audit=audit,
    )
    context = ToolExecutionContext(
        tmp_path,
        "demo",
        ProjectDataAccess.create(tmp_path, "demo", endpoints=[]),
    )
    stream: list[str] = []

    result = runtime.run(
        role_id="memory_role",
        task_prompt="读取长证据后完成任务",
        tool_context=context,
        stream_callback=stream.append,
    )

    assert result.artifact == {"ok": True}
    assert result.usage.total_tokens == 300
    checkpoints = audit.list_checkpoints(result.run_id)
    assert len(checkpoints) == 1
    assert checkpoints[0]["input_tokens_after"] < checkpoints[0]["input_tokens_before"]
    assert checkpoints[0]["ratio_after"] < checkpoints[0]["ratio_before"]
    evidence = audit.list_evidence(result.run_id)
    assert any(item["tool_name"] == "long_read" and item["content_chars"] > 15000 for item in evidence)
    assert audit.read_evidence(result.run_id, gateway.evidence_id, max_chars=100)["content"].startswith('{"payload":"evidence-marker-')
    assert any("已自动保存检查点并重建" in item for item in stream)


def test_history_compaction_exposes_evidence_id_instead_of_content_hash() -> None:
    compacted = compact_agent_history(
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "task"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call-1", "type": "function"}],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "name": "get_existing_case_detail",
                "content": "x" * 5000,
            },
            {"role": "assistant", "content": "继续" * 500},
            {"role": "user", "content": "请完成" * 500},
            {"role": "assistant", "content": "处理中" * 500},
            {"role": "user", "content": "继续" * 500},
            {"role": "assistant", "content": "处理中" * 500},
            {"role": "user", "content": "继续" * 500},
            {"role": "assistant", "content": "处理中" * 500},
            {"role": "user", "content": "继续" * 500},
        ],
        9000,
        evidence_by_call_id={"call-1": "evidence-0123456789abcdefabcd"},
    )

    tool_message = next(item for item in compacted if item.get("role") == "tool")
    payload = json.loads(tool_message["content"])
    assert payload["evidence_id"] == "evidence-0123456789abcdefabcd"
    assert "不要把 sha256 当作 evidence_id" in payload["next_action"]


def test_agent_runtime_forces_terminal_submission_on_last_round(tmp_path) -> None:
    role = RoleSpec(
        id="bounded_role",
        version="1",
        mission="在有限轮次内交付",
        system_prompt="读取后提交。",
        model_role="case_generator",
        input_contract="Task",
        output_contract="Result",
        allowed_tools=("read_once", "submit_done"),
        max_tool_rounds=2,
        max_tool_calls=2,
        token_budget=10000,
    )
    tools = ToolRegistry()
    tools.register(
        ToolSpec(
            name="read_once",
            description="读取一次",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=lambda arguments, context: {"ready": True},
            allowed_roles=("bounded_role",),
        )
    )
    tools.register(
        ToolSpec(
            name="submit_done",
            description="提交",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=lambda arguments, context: ToolExecutionResult(
                content="done",
                value={"ok": True},
                terminal=True,
            ),
            allowed_roles=("bounded_role",),
            side_effect="transform",
            terminal=True,
        )
    )

    class BoundedGateway:
        model_config = {
            "provider": "fake",
            "model": "bounded",
            "context_window": 16000,
            "default_output_tokens": 1000,
        }

        def __init__(self) -> None:
            self.turn = 0

        def complete(self, *, tools, **kwargs) -> ModelResponse:
            self.turn += 1
            if self.turn == 1:
                return tool_response("read_once", {}, call_id="read", total_tokens=10)
            assert [item["function"]["name"] for item in tools] == ["submit_done"]
            assert self.model_config["tool_choice"]["function"]["name"] == "submit_done"
            return tool_response("submit_done", {}, call_id="submit", total_tokens=10)

    runtime = AgentRuntime(
        roles=RoleRegistry([role]),
        tools=tools,
        gateway=BoundedGateway(),
        audit=AgentRunRepository(tmp_path, "demo"),
    )
    result = runtime.run(
        role_id="bounded_role",
        task_prompt="完成",
        tool_context=ToolExecutionContext(
            tmp_path,
            "demo",
            ProjectDataAccess.create(tmp_path, "demo", endpoints=[]),
        ),
    )

    assert result.artifact == {"ok": True}


def test_agent_runtime_deduplicates_replayed_idempotent_effect_tool(tmp_path) -> None:
    role = RoleSpec(
        id="idempotent_role",
        version="1",
        mission="验证恢复后的副作用去重",
        system_prompt="执行工具并结束。",
        model_role="master",
        input_contract="Task",
        output_contract="Text",
        allowed_tools=("write_once",),
        max_tool_rounds=3,
        max_tool_calls=3,
        token_budget=10000,
    )
    calls = [0]

    def write_once(arguments, context):
        calls[0] += 1
        return {"saved": arguments["id"]}

    tools = ToolRegistry()
    tools.register(
        ToolSpec(
            name="write_once",
            description="幂等写入",
            input_schema={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
                "additionalProperties": False,
            },
            handler=write_once,
            allowed_roles=("idempotent_role",),
            side_effect="write",
            idempotent=True,
        )
    )
    gateway = QueueGateway(
        [
            tool_response("write_once", {"id": "same"}, call_id="write-1"),
            tool_response("write_once", {"id": "same"}, call_id="write-2"),
            ModelResponse(text="done", usage=TokenUsage(10, 10, 20)),
        ]
    )
    runtime = AgentRuntime(
        roles=RoleRegistry([role]),
        tools=tools,
        gateway=gateway,
        audit=AgentRunRepository(tmp_path, "demo"),
    )
    context = ToolExecutionContext(
        tmp_path,
        "demo",
        ProjectDataAccess.create(tmp_path, "demo", endpoints=[]),
    )

    result = runtime.run(role_id="idempotent_role", task_prompt="保存一次", tool_context=context)

    assert result.text == "done"
    assert calls[0] == 1
    with runtime.audit.connection() as conn:
        statuses = [
            str(row[0])
            for row in conn.execute(
                "select status from agent_tool_calls where run_id = ? order by id",
                (result.run_id,),
            ).fetchall()
        ]
    assert statuses == ["completed", "deduplicated"]


def test_quality_audit_deduplicates_models_and_uses_independent_equal_votes(tmp_path) -> None:
    config = load_config(tmp_path)
    config["llm"] = {
        "master": {
            "enabled": True,
            "provider": "openai-compatible",
            "base_url": "https://judge-a.invalid/v1",
            "model": "judge-a",
            "api_key_env": "A",
        },
        "case_generator": {
            "enabled": True,
            "provider": "anthropic",
            "base_url": "https://judge-b.invalid/v1",
            "model": "judge-b",
            "api_key_env": "B",
        },
        "analyzer": {
            "enabled": True,
            "provider": "anthropic",
            "base_url": "https://judge-b.invalid/v1/",
            "model": "judge-b",
            "api_key_env": "C",
        },
    }
    save_config(tmp_path, config)
    assert len(unique_audit_models(config["llm"])) == 2

    class JudgeGateway:
        def __init__(self, model_config: dict[str, Any]) -> None:
            self.model_config = {
                **model_config,
                "context_window": 32000,
                "default_output_tokens": 4000,
                "supports_tools": True,
            }
            self.turn = 0

        def complete(self, *, messages, **kwargs) -> ModelResponse:
            self.turn += 1
            if self.turn == 1:
                return tool_response("get_audit_artifact_info", {}, call_id="info", total_tokens=20)
            if self.turn == 2:
                return tool_response(
                    "read_audit_artifact_chunk",
                    {"offset": 0, "max_chars": 16000},
                    call_id="read",
                    total_tokens=20,
                )
            is_a = self.model_config["model"] == "judge-a"
            dimensions = {
                "reliability": 92 if is_a else 90,
                "completeness": 90 if is_a else 86,
                "reasonableness": 90 if is_a else 88,
                "non_redundancy": 90 if is_a else 88,
            }
            return tool_response(
                "submit_quality_review",
                {
                    "verdict": "approved",
                    "confidence": "high",
                    "dimension_scores": dimensions,
                    "summary": "证据与产物一致",
                    "issues": [],
                    "suggestions": [],
                    "coverage_gaps": [],
                },
                call_id="submit",
                total_tokens=20,
            )

    service = QualityAuditService(
        tmp_path,
        "demo",
        "endpoint_catalog",
        endpoints=[Endpoint(method="GET", path="/health", responses={"200": {}})],
        gateway_factory=JudgeGateway,
    )
    result = service.evaluate(
        [{"method": "GET", "path": "/health", "responses": {"200": {}}}],
        evidence={"deterministic_inventory": [{"method": "GET", "path": "/health"}]},
        round_index=1,
    )
    service.finalize(result, decision="accepted", reason="质量审计通过")

    assert result.passed
    assert len(result.scores) == 2
    assert {score.model for score in result.scores} == {"judge-a", "judge-b"}
    detail = service.repository.session_detail(service.session_id)
    assert detail is not None
    assert detail["unique_model_count"] == 2
    assert len(detail["scores"]) == 2


def test_quality_audit_enforces_strict_gates_and_retries_judge_independently(
    tmp_path,
    monkeypatch,
) -> None:
    config = load_config(tmp_path)
    config["llm"]["master"].update(
        {
            "enabled": True,
            "provider": "openai-compatible",
            "base_url": "https://judge.invalid/v1",
            "model": "judge",
            "api_key_env": "JUDGE_KEY",
        }
    )
    config["quality_audit"]["judge_max_attempts"] = 2
    save_config(tmp_path, config)

    exact_85 = {
        "dimension_scores": {
            "reliability": 90,
            "completeness": 85,
            "reasonableness": 80,
            "non_redundancy": 75,
        },
        "confidence": "high",
        "summary": "恰好达到 85 分",
        "issues": [],
    }
    monkeypatch.setattr(
        "ai_apitest.application.quality_audit.review_quality_with_agent_runtime",
        lambda *args, **kwargs: exact_85,
    )
    strict_service = QualityAuditService(tmp_path, "demo", "endpoint_catalog")
    strict_result = strict_service.evaluate(
        [{"method": "GET", "path": "/strict"}],
        evidence={"source": "strict"},
        round_index=1,
    )
    strict_service.finalize(
        strict_result,
        decision="highest_safe_candidate",
        reason="严格阈值测试",
    )
    assert strict_result.average_score == 85
    assert strict_result.passed is False
    assert any("未严格高于" in item for item in strict_result.decision_reasons)

    critical_review = {
        "dimension_scores": {
            "reliability": 100,
            "completeness": 100,
            "reasonableness": 100,
            "non_redundancy": 100,
        },
        "confidence": "high",
        "summary": "存在高分但致命问题",
        "issues": [
            {
                "severity": "critical",
                "dimension": "reliability",
                "location": "GET /critical",
                "evidence": "接口文档为 POST，候选却写为 GET。",
                "reason": "method 与事实证据冲突。",
                "suggested_fix": "改为 POST。",
            }
        ],
    }
    monkeypatch.setattr(
        "ai_apitest.application.quality_audit.review_quality_with_agent_runtime",
        lambda *args, **kwargs: critical_review,
    )
    critical_service = QualityAuditService(tmp_path, "critical", "endpoint_catalog")
    critical_result = critical_service.evaluate(
        [{"method": "GET", "path": "/critical"}],
        evidence={"source": "critical"},
        round_index=1,
    )
    assert critical_result.average_score == 100
    assert critical_result.passed is False
    assert critical_result.safe_for_fallback is False

    attempts = 0

    def flaky_review(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary transport failure")
        return {
            "dimension_scores": {
                "reliability": 96,
                "completeness": 94,
                "reasonableness": 92,
                "non_redundancy": 90,
            },
            "confidence": "high",
            "summary": "第二次独立审计成功",
            "issues": [],
        }

    monkeypatch.setattr(
        "ai_apitest.application.quality_audit.review_quality_with_agent_runtime",
        flaky_review,
    )
    retry_service = QualityAuditService(tmp_path, "retry", "endpoint_catalog")
    retry_result = retry_service.evaluate(
        [{"method": "GET", "path": "/retry"}],
        evidence={"source": "retry"},
        round_index=1,
    )
    assert attempts == 2
    assert retry_result.passed is True
    assert retry_result.audit_complete is True


def test_quality_audit_detail_page_shows_per_model_reasons(tmp_path, monkeypatch) -> None:
    from ai_apitest.web.server import render_quality_audit_detail

    config = load_config(tmp_path)
    config["llm"]["master"].update(
        {
            "enabled": True,
            "provider": "openai-compatible",
            "base_url": "https://judge.invalid/v1",
            "model": "judge",
            "api_key_env": "JUDGE_KEY",
        }
    )
    save_config(tmp_path, config)
    monkeypatch.setattr(
        "ai_apitest.application.quality_audit.review_quality_with_agent_runtime",
        lambda *args, **kwargs: {
            "dimension_scores": {
                "reliability": 96,
                "completeness": 94,
                "reasonableness": 92,
                "non_redundancy": 90,
            },
            "confidence": "high",
            "summary": "评分原因可见",
            "issues": [
                {
                    "severity": "warning",
                    "dimension": "non_redundancy",
                    "location": "GET /health",
                    "evidence": "存在相近描述。",
                    "reason": "描述略有重复。",
                    "suggested_fix": "合并描述。",
                }
            ],
        },
    )
    service = QualityAuditService(tmp_path, "demo", "endpoint_catalog")
    result = service.evaluate(
        [{"method": "GET", "path": "/health"}],
        evidence={"source": "test"},
        round_index=1,
    )
    service.finalize(result, decision="accepted", reason="质量审计通过")

    html = render_quality_audit_detail(tmp_path, "demo", service.session_id)
    assert html is not None
    assert "可靠性" in html
    assert "非冗余" in html
    assert "评分原因可见" in html
    assert "描述略有重复" in html


def test_dependency_graph_agent_serializes_submitted_artifact(tmp_path) -> None:
    endpoint = Endpoint(
        method="GET",
        path="/health",
        responses={"200": {}},
    )
    gateway = QueueGateway(
        [
            tool_response("list_endpoints", {}, call_id="list"),
            tool_response(
                "submit_dependency_graph",
                {"edges": []},
                call_id="submit",
            ),
        ]
    )

    graph = generate_dependency_graph_with_agent_runtime(
        tmp_path,
        "demo",
        [endpoint],
        {
            "enabled": True,
            "provider": "openai-compatible",
            "base_url": "https://unused.invalid/v1",
            "model": "fake",
            "api_key_env": "UNUSED",
            "context_window": 32000,
            "default_output_tokens": 4000,
        },
        gateway_factory=lambda config: gateway,
    )

    assert graph["edges"] == []
    assert "safe_to_prepare" in graph["rule"]


def test_result_analysis_explicit_tool_scope_and_lesson_contract(tmp_path) -> None:
    tools = build_case_generation_tool_registry()
    role = default_role_registry().resolve("result_analyst")
    context = ToolExecutionContext(
        tmp_path,
        "demo",
        ProjectDataAccess.create(tmp_path, "demo", endpoints=[]),
    )

    class ScopedGateway:
        model_config = {
            "provider": "fake",
            "model": "fake",
            "context_window": 32000,
            "default_output_tokens": 1000,
        }

        def __init__(self) -> None:
            self.tool_names: list[str] = []

        def complete(self, *, tools, **kwargs) -> ModelResponse:
            self.tool_names = [str(item["function"]["name"]) for item in tools]
            return tool_response(
                "submit_result_analysis",
                {
                    "markdown": "### 一句话结论\n通过",
                    "lessons": [],
                    "known_issue_updates": [],
                },
            )

    gateway = ScopedGateway()
    result = AgentRuntime(
        roles=default_role_registry(),
        tools=tools,
        gateway=gateway,
        audit=AgentRunRepository(tmp_path, "demo"),
    ).run(
        role_id=role.id,
        task_prompt="汇总结果",
        tool_context=context,
        enabled_tools=("submit_result_analysis",),
    )
    assert result.artifact["lessons"] == []
    assert gateway.tool_names == ["submit_result_analysis"]

    with pytest.raises(ToolInputError):
        tools.execute(
            role_id=role.id,
            allowed_tools=role.allowed_tools,
            name="submit_result_analysis",
            arguments={
                "markdown": "### 一句话结论\n通过",
                "lessons": [{"lesson": "错误字段"}],
                "known_issue_updates": [],
            },
            context=context,
        )

    skipped = skipped_case_summary(
        CaseResult(
            case_id="TC-SKIP",
            title="共享限流契约",
            status="skipped",
            method="GET",
            path="/limited",
            status_code=None,
            elapsed_ms=0,
            error="限流场景依赖共享流量状态，默认计划不会污染测试环境。",
            case_risk="shared_rate_limit",
            design_method="status_code_coverage",
            expected_result="记录契约而不执行",
        )
    )
    assert skipped["reason"] == "限流场景依赖共享流量状态，默认计划不会污染测试环境。"
    assert skipped["case_risk"] == "shared_rate_limit"
    assert result_cleanup_status(
        CaseResult(
            case_id="TC-NO-CLEANUP",
            title="无需清理",
            status="passed",
            method="GET",
            path="/health",
            status_code=200,
            elapsed_ms=1,
        )
    ) == "not_configured"


def test_agent_runtime_rejects_unauthorized_tool_and_token_overrun(tmp_path) -> None:
    endpoint = Endpoint(method="GET", path="/health")
    context = ToolExecutionContext(
        tmp_path,
        "demo",
        ProjectDataAccess.create(tmp_path, "demo", endpoints=[endpoint]),
    )
    unauthorized = AgentRuntime(
        roles=default_role_registry(),
        tools=build_case_generation_tool_registry(),
        gateway=QueueGateway([tool_response("submit_case_drafts", {"drafts": []})]),
        audit=AgentRunRepository(tmp_path, "demo"),
    )
    with pytest.raises(ToolPermissionError):
        unauthorized.run(role_id="test_planner", task_prompt="越权", tool_context=context)

    task_scoped = AgentRuntime(
        roles=default_role_registry(),
        tools=build_case_generation_tool_registry(),
        gateway=QueueGateway([tool_response("submit_coverage_plan", {"items": []})]),
        audit=AgentRunRepository(tmp_path, "demo"),
    )
    with pytest.raises(ToolPermissionError):
        task_scoped.run(
            role_id="test_planner",
            task_prompt="任务级越权",
            tool_context=context,
            enabled_tools=("list_endpoints",),
        )

    role = RoleSpec(
        id="budget_role",
        version="1",
        mission="budget test",
        system_prompt="budget test",
        model_role="case_generator",
        input_contract="Task",
        output_contract="Text",
        token_budget=10,
        max_tool_rounds=1,
        max_tool_calls=1,
    )
    budget_runtime = AgentRuntime(
        roles=RoleRegistry([role]),
        tools=build_case_generation_tool_registry(),
        gateway=QueueGateway([ModelResponse(text="done", usage=TokenUsage(10, 10, 20))]),
        audit=AgentRunRepository(tmp_path, "demo"),
    )
    with pytest.raises(AgentBudgetExceeded):
        budget_runtime.run(role_id="budget_role", task_prompt="budget", tool_context=context)


class ReleaseFixtureGateway:
    """Deterministic model substitute that exercises real tool loops."""

    def __init__(self) -> None:
        self.model_config = {
            "provider": "fake",
            "model": "release-fixture-scripted",
            "default_output_tokens": 8000,
            "supports_tools": True,
        }
        self.sequence = 0

    def complete(self, *, messages, tools, timeout, max_tokens, stream_callback=None) -> ModelResponse:
        self.sequence += 1
        system = str(messages[0]["content"])
        if "test_planner" in system:
            tool_messages = [item for item in messages if item.get("role") == "tool"]
            if not tool_messages:
                return tool_response("list_endpoints", {"limit": 100}, call_id=f"call-{self.sequence}")
            endpoint_page = json.loads(tool_messages[-1]["content"])
            items = []
            for index, endpoint in enumerate(endpoint_page["items"], start=1):
                statuses = endpoint.get("response_statuses") or []
                items.append(
                    {
                        "scenario_id": f"SCN-MODEL-{index:03d}",
                        "endpoint_method": endpoint["method"],
                        "endpoint_path": endpoint["path"],
                        "design_method": "happy_path",
                        "priority": "P0",
                        "title_hint": f"{endpoint['summary']}主流程",
                        "coverage_tags": ["happy_path"],
                        "expected_basis": f"responses.{statuses[0]}" if statuses else "",
                    }
                )
            return tool_response(
                "submit_coverage_plan",
                {"items": items},
                call_id=f"call-{self.sequence}",
            )

        task = next(str(item["content"]) for item in messages if item.get("role") == "user")
        if task.startswith("定向修订"):
            plan = json.loads(task.split("目标计划：", 1)[1].split("\n上次草稿：", 1)[0])
        else:
            plan = json.loads(task.split("覆盖计划：\n", 1)[1].split("\n\n先调用", 1)[0])
        required_keys = endpoint_keys_for_plan(plan)
        queried = {
            (str(json.loads(item["content"])["method"]).upper(), str(json.loads(item["content"])["path"]))
            for item in messages
            if item.get("role") == "tool"
            and item.get("name") == "get_endpoint_detail"
            and isinstance(json.loads(item["content"]), dict)
            and "method" in json.loads(item["content"])
        }
        pending = [key for key in required_keys if key not in queried]
        if pending:
            method, path = pending[0]
            return tool_response(
                "get_endpoint_detail",
                {"method": method, "path": path},
                call_id=f"call-{self.sequence}",
            )
        drafts = [
            {
                "scenario_id": item["scenario_id"],
                "endpoint_method": item["endpoint_method"],
                "endpoint_path": item["endpoint_path"],
                "intent": item["title_hint"],
                "design_method": item["design_method"],
                "priority_hint": item["priority"],
                "coverage_tags": item["coverage_tags"],
                "expected_outcome": "实际响应符合接口文档声明的业务结果。",
                "expected_basis": item.get("expected_basis") or "",
                "chain_steps": item.get("chain_steps") or [],
            }
            for item in plan
        ]
        return tool_response("submit_case_drafts", {"drafts": drafts}, call_id=f"call-{self.sequence}")


def endpoint_keys_for_plan(plan: list[dict[str, Any]]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for item in plan:
        if item["endpoint_method"] == "CHAIN":
            values = [(str(step["method"]).upper(), str(step["path"])) for step in item.get("chain_steps") or []]
        else:
            values = [(str(item["endpoint_method"]).upper(), str(item["endpoint_path"]))]
        for value in values:
            if value not in result:
                result.append(value)
    return result


def test_release_fixture_agent_generation_is_delivery_ready(tmp_path) -> None:
    project = "release-fixture"
    email_parameter = {
        "name": "email",
        "in": "path",
        "required": True,
        "schema": {"type": "string", "format": "email"},
    }
    generated_email_response = {
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["email"],
                    "properties": {"email": {"type": "string", "format": "email"}},
                }
            }
        }
    }
    endpoints = [
        Endpoint(
            method="GET",
            path="/api/generate",
            summary="Create test resource",
            responses={"200": generated_email_response},
        ),
        Endpoint(
            method="GET",
            path="/api/check/{email}",
            summary="Check test resource",
            parameters=[email_parameter],
            responses={"200": {"content": {"application/json": {"schema": {"type": "object"}}}}},
        ),
        Endpoint(
            method="POST",
            path="/api/extend/{email}",
            summary="Extend test resource",
            parameters=[email_parameter],
            responses={"200": {"content": {"application/json": {"schema": {"type": "object"}}}}},
        ),
        Endpoint(
            method="DELETE",
            path="/api/delete/{email}",
            summary="Delete test resource",
            parameters=[email_parameter],
            responses={"200": {"content": {"application/json": {"schema": {"type": "object"}}}}},
        ),
    ]
    write_json(project_docs_file(tmp_path, project), to_jsonable(endpoints))
    config = load_config(tmp_path)
    config["llm"]["case_generator"].update(
        {
            "enabled": True,
            "provider": "openai-compatible",
            "base_url": "https://unused.invalid/v1",
            "model": "release-fixture-scripted",
            "api_key_env": "UNUSED_KEY",
        }
    )
    save_config(tmp_path, config)
    gateway = ReleaseFixtureGateway()

    cases, warning = generate_cases_with_agent_runtime(
        tmp_path,
        endpoints,
        project=project,
        gateway_factory=lambda model_config: gateway,
    )

    assert len(cases) >= len(endpoints)
    assert len({case.scenario_id for case in cases}) == len(cases)
    assert all(case.expected_result for case in cases)
    prepared = [case for case in cases if case.path in {"/api/check/{email}", "/api/extend/{email}"} and case.design_method == "happy_path"]
    assert prepared
    assert all(case.runnable and case.setup_steps for case in prepared)
    assert "质量门禁通过" in warning
    runs = AgentRunRepository(tmp_path, project).list_recent(limit=20)
    assert runs
    assert all(item["status"] == "completed" for item in runs)
    assert sum(int(item["tool_call_count"]) for item in runs) > 0


def test_standard_coverage_compiles_locally_without_designer_model_call(tmp_path) -> None:
    endpoint = Endpoint(
        method="GET",
        path="/health",
        responses={
            "200": {
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {"status": {"type": "string", "const": "ok"}},
                        }
                    }
                }
            }
        },
    )
    config = load_config(tmp_path)
    config["llm"]["case_generator"].update(
        {"enabled": True, "base_url": "https://unused.invalid/v1", "model": "fake", "api_key_env": "UNUSED"}
    )
    save_config(tmp_path, config)
    gateway = QueueGateway(
        [
            tool_response("list_endpoints", {"limit": 100}, call_id="local-list"),
            tool_response("submit_coverage_plan", {"items": []}, call_id="local-plan"),
        ]
    )

    cases, warning = generate_cases_with_agent_runtime(
        tmp_path,
        [endpoint],
        project="demo",
        gateway_factory=lambda model_config: gateway,
    )

    assert len(gateway.responses) == 0
    assert len(cases) == 1
    assert [(item.target, item.operator, item.expected) for item in cases[0].assertions] == [
        ("", "eq", 200),
        ("$.status", "eq", "ok"),
    ]
    assert "本地确定性编译 1 个场景" in warning
    runs = AgentRunRepository(tmp_path, "demo").list_recent(limit=10)
    assert len(runs) == 1
    assert runs[0]["role_id"] == "test_planner"


def test_designer_failure_drops_optional_model_batch_but_delivers_deterministic_cases(tmp_path) -> None:
    endpoint = Endpoint(method="GET", path="/health", summary="健康检查", responses={"200": {}})
    config = load_config(tmp_path)
    config["llm"]["case_generator"].update(
        {
            "enabled": True,
            "base_url": "https://unused.invalid/v1",
            "model": "planner-then-fail",
            "api_key_env": "UNUSED_KEY",
        }
    )
    save_config(tmp_path, config)

    class PlannerThenFailGateway:
        model_config = {
            "provider": "fake",
            "model": "planner-then-fail",
            "default_output_tokens": 8000,
            "supports_tools": True,
        }

        def complete(self, *, messages, tools, timeout, max_tokens, stream_callback=None) -> ModelResponse:
            system = str(messages[0]["content"])
            if "test_planner" not in system:
                raise RuntimeError("designer unavailable")
            if not any(item.get("role") == "tool" for item in messages):
                return tool_response("list_endpoints", {"limit": 100}, call_id="list")
            return tool_response(
                "submit_coverage_plan",
                {
                    "items": [
                        {
                            "scenario_id": "SCN-BUSINESS-OPTIONAL",
                            "endpoint_method": "GET",
                            "endpoint_path": "/health",
                            "design_method": "equivalence_class",
                            "priority": "P1",
                            "title_hint": "业务健康状态语义",
                            "coverage_tags": ["business_rule"],
                            "expected_basis": "confirmed business rule",
                        }
                    ]
                },
                call_id="plan",
            )

    gateway = PlannerThenFailGateway()
    cases, warning = generate_cases_with_agent_runtime(
        tmp_path,
        [endpoint],
        project="demo",
        gateway_factory=lambda model_config: gateway,
    )
    assert cases
    assert all(case.scenario_id != "SCN-BUSINESS-OPTIONAL" for case in cases)
    assert "已剔除该批增量" in warning
    assert "质量门禁通过" in warning


class SupplementGateway:
    def __init__(self) -> None:
        self.model_config = {
            "provider": "fake",
            "model": "supplement-scripted",
            "default_output_tokens": 2000,
            "supports_tools": True,
        }
        self.sequence = 0

    def complete(self, *, messages, **kwargs) -> ModelResponse:
        self.sequence += 1
        tool_names = [item.get("name") for item in messages if item.get("role") == "tool"]
        if "list_existing_cases" not in tool_names:
            return tool_response("list_existing_cases", {"limit": 20}, call_id=f"sup-{self.sequence}")
        if "get_endpoint_detail" not in tool_names:
            return tool_response(
                "get_endpoint_detail",
                {"method": "GET", "path": "/users/{id}"},
                call_id=f"sup-{self.sequence}",
            )
        return tool_response(
            "submit_case_drafts",
            {
                "drafts": [
                    {
                        "scenario_id": "SCN-SUP-001",
                        "endpoint_method": "GET",
                        "endpoint_path": "/users/{id}",
                        "intent": "查询不存在用户",
                        "design_method": "path_parameter_error",
                        "request_overrides": {"path_params": {"id": "missing-user"}},
                        "expected_outcome": "返回文档声明的不存在响应",
                        "expected_basis": "responses.404",
                    }
                ]
            },
            call_id=f"sup-{self.sequence}",
        )


def test_supplement_agent_reads_existing_cases_and_compiles_draft(tmp_path) -> None:
    endpoint = Endpoint(
        method="GET",
        path="/users/{id}",
        summary="查询用户",
        parameters=[{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
        responses={"200": {}, "404": {}},
    )
    existing = ApiTestCase(
        id="TC-USERS-0001",
        title="查询用户主流程",
        method="GET",
        path="/users/{id}",
        design_method="happy_path",
        assertions=[Assertion(type="status_code", expected=200)],
    )
    config = load_config(tmp_path)
    config["llm"]["case_generator"].update(
        {"enabled": True, "base_url": "https://unused.invalid/v1", "model": "fake", "api_key_env": "UNUSED"}
    )
    save_config(tmp_path, config)
    gateway = SupplementGateway()

    cases, warning = supplement_cases_with_agent_runtime(
        tmp_path,
        [endpoint],
        [existing],
        project="demo",
        scenario_text="补充不存在用户场景",
        gateway_factory=lambda model_config: gateway,
    )

    assert len(cases) == 1
    assert cases[0].scenario_id == "SCN-SUP-001"
    assert cases[0].path_params == {"id": "missing-user"}
    assert cases[0].assertions[0].expected == 404
    assert "语义门禁通过" in warning
    assert AgentRunRepository(tmp_path, "demo").list_recent(limit=1)[0]["tool_call_count"] == 3


def test_designer_batches_limit_scenarios_and_tools_to_current_task() -> None:
    endpoint = Endpoint(method="GET", path="/health", responses={"200": {}})
    plan = [
        {
            "scenario_id": f"SCN-{index:03d}",
            "endpoint_method": "GET",
            "endpoint_path": "/health",
            "design_method": "happy_path",
        }
        for index in range(9)
    ]
    batches = plan_batches(plan, [endpoint], endpoint_limit=8, scenario_limit=8)
    assert [len(items) for items, _endpoints in batches] == [8, 1]
    assert "get_generation_strategies" not in designer_tools_for_plan(plan)
    assert "get_endpoint_detail" in PLANNER_TOOLS
    assert "get_dependency_graph" not in designer_tools_for_plan(plan)
    assert "get_dependency_graph" in designer_tools_for_plan(
        [{"endpoint_method": "CHAIN", "design_method": "dependency_chain"}]
    )


class MaintenanceGateway:
    def __init__(self) -> None:
        self.model_config = {
            "provider": "fake",
            "model": "maintenance-scripted",
            "default_output_tokens": 2000,
            "supports_tools": True,
        }
        self.sequence = 0

    def complete(self, *, messages, **kwargs) -> ModelResponse:
        self.sequence += 1
        tool_names = [item.get("name") for item in messages if item.get("role") == "tool"]
        if "get_existing_case_detail" not in tool_names:
            return tool_response(
                "get_existing_case_detail",
                {"case_id": "TC-USERS-0001"},
                call_id=f"maint-{self.sequence}",
            )
        if "get_endpoint_detail" not in tool_names:
            return tool_response(
                "get_endpoint_detail",
                {"method": "GET", "path": "/users/{id}"},
                call_id=f"maint-{self.sequence}",
            )
        return tool_response(
            "submit_case_maintenance",
            {
                "decisions": [
                    {
                        "target_case_id": "TC-USERS-0001",
                        "action": "update",
                        "reason": "接口新增文档化 404 响应",
                        "draft": {
                            "scenario_id": "SCN-MAINT-001",
                            "endpoint_method": "GET",
                            "endpoint_path": "/users/{id}",
                            "intent": "查询不存在用户",
                            "design_method": "status_code_coverage",
                            "request_overrides": {"path_params": {"id": "missing-user"}},
                            "expected_outcome": "返回文档声明的不存在响应",
                            "expected_basis": "responses.404",
                        },
                    }
                ]
            },
            call_id=f"maint-{self.sequence}",
        )


def test_case_maintenance_agent_preserves_identity_and_passes_compiler(tmp_path) -> None:
    endpoint = Endpoint(
        method="GET",
        path="/users/{id}",
        summary="查询用户",
        parameters=[{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
        responses={"200": {}, "404": {}},
    )
    original = ApiTestCase(
        id="TC-USERS-0001",
        title="查询用户主流程",
        method="GET",
        path="/users/{id}",
        design_method="happy_path",
        assertions=[Assertion(type="status_code", expected=200)],
        scenario_id="SCN-ORIGINAL-001",
    )
    gateway = MaintenanceGateway()
    result = maintain_cases_with_agent_runtime(
        tmp_path,
        "demo",
        impacted=[original],
        endpoints=[endpoint],
        change_context={"quality_review_issues": [{"scenario_id": "SCN-ORIGINAL-001", "detail": "补 404"}]},
        llm={"provider": "fake", "model": "fake", "default_output_tokens": 2000},
        allow_retire=False,
        gateway_factory=lambda model_config: gateway,
    )

    assert result.retired_case_ids == set()
    assert result.updated[0].id == original.id
    assert result.updated[0].scenario_id == original.scenario_id
    assert result.updated[0].assertions[0].expected == 404
    assert "维护门禁通过" in result.warning


def test_web_and_llm_config_expose_agent_runtime_without_losing_capabilities(tmp_path) -> None:
    existing = {
        "context_window": 64000,
        "default_output_tokens": 4096,
        "supports_stream": True,
        "supports_tools": True,
    }
    updated = llm_role_from_form(
        tmp_path,
        "case_generator",
        {
            "provider": ["openai-compatible"],
            "base_url": ["https://example.invalid/v1"],
            "model": ["model"],
            "api_key_env": ["ENV_KEY"],
            "supports_tools": ["on"],
        },
        existing,
    )
    assert updated["context_window"] == 64000
    assert updated["supports_tools"] is True
    page = render_config(tmp_path, "demo", type("Parsed", (), {"query": ""})())
    assert "Agent Runtime" in page
    assert "Agent 模式" in page
    assert "Legacy 模式" in page
    assert "按任务分配受限角色" in page
    assert "绕过 Agent Runtime" in page
    assert "当前全部使用 Agent Runtime" in page
    assert "原生工具协议" in page
    assert "高级参数改用 JSON 配置" in page
    assert "项目根目录/iatest.json" in page
    assert str(tmp_path.resolve()) not in page
    assert "最近 Agent 运行审计" in page
    assert 'action="/actions/save-agent-runtime"' not in page
    assert 'name="case_generation_mode"' not in page
    assert 'name="planner_batch_endpoints"' not in page
    assert 'name="context_recovery_enabled"' not in page
