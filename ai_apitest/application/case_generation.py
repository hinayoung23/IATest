from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ai_apitest.agents.identity import RoleRegistry, default_role_registry
from ai_apitest.agents.runtime import AgentTaskResult
from ai_apitest.application.agent_service import run_agent_role
from ai_apitest.ai.llm_config import is_llm_configured, normalize_llm_config, resolve_llm_role
from ai_apitest.ai.model_gateway import ModelGatewayProtocol, ProviderModelGateway
from ai_apitest.context.storage import record_case_generation_lessons
from ai_apitest.core.artifact_language import localize_generated_cases, output_language_instruction
from ai_apitest.core.case_schema import normalize_cases
from ai_apitest.core.config import load_config
from ai_apitest.core.models import Endpoint, TestCase
from ai_apitest.core.observability import record_operational_error
from ai_apitest.core.serde import to_jsonable
from ai_apitest.data.repositories import ProjectDataAccess
from ai_apitest.domain.case_drafts import (
    CompilationResult,
    DraftIssue,
    NEGATIVE_DESIGN_METHODS,
    case_execution_signature,
    case_request_signature,
    compile_scenario_drafts,
    documented_status_from_basis,
    remove_redundant_shape_assertions,
    step_execution_payload,
    validate_compiled_cases,
)
from ai_apitest.domain.contracts import assertion_contradictions
from ai_apitest.domain.coverage import fill_deterministic_coverage_gaps, normalize_coverage_plan
from ai_apitest.generator.cases import (
    boundary_values,
    default_body,
    default_query,
    dependency_producers,
    likely_produced_fields,
    wrong_type_value,
)
from ai_apitest.tools.builtin import build_case_generation_tool_registry
from ai_apitest.tools.registry import ToolRegistry


GatewayFactory = Callable[[dict[str, Any]], ModelGatewayProtocol]

PLANNER_TOOLS = (
    "list_endpoints",
    "get_endpoint_detail",
    "list_business_rules",
    "get_business_rule_detail",
    "get_generation_strategies",
    "search_confirmed_lessons",
    "get_dependency_graph",
    "submit_coverage_plan",
)
DESIGNER_TOOLS = (
    "get_endpoint_detail",
    "get_business_rule_detail",
    "submit_case_drafts",
)
SUPPLEMENT_TOOLS = (
    "list_endpoints",
    "get_endpoint_detail",
    "list_business_rules",
    "get_business_rule_detail",
    "list_existing_cases",
    "get_existing_case_detail",
    "get_generation_strategies",
    "search_confirmed_lessons",
    "get_dependency_graph",
    "submit_case_drafts",
)


@dataclass(slots=True)
class GenerationTelemetry:
    plan_runs: int = 0
    design_runs: int = 0
    repair_runs: int = 0
    local_fallbacks: int = 0
    local_compilations: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    def include(self, result: AgentTaskResult) -> None:
        self.input_tokens += result.usage.input_tokens
        self.output_tokens += result.usage.output_tokens
        self.total_tokens += result.usage.total_tokens

    def summary(self) -> str:
        return (
            f"Agent 运行：规划 {self.plan_runs} 次，设计 {self.design_runs} 次，"
            f"定向修订 {self.repair_runs} 次，本地兜底 {self.local_fallbacks} 个场景，"
            f"本地确定性编译 {self.local_compilations} 个场景，"
            f"Token {self.total_tokens}（输入 {self.input_tokens} / 输出 {self.output_tokens}）"
        )


def generate_cases_with_agent_runtime(
    root: Path,
    endpoints: list[Endpoint],
    *,
    project: str = "default",
    stream_callback: Callable[[str], None] | None = None,
    llm_override: dict[str, Any] | None = None,
    gateway_factory: GatewayFactory = ProviderModelGateway,
    role_registry: RoleRegistry | None = None,
    tool_registry: ToolRegistry | None = None,
    quality_feedback: dict[str, Any] | None = None,
    quality_round: int = 1,
    record_lessons: bool = True,
) -> tuple[list[TestCase], str]:
    if not endpoints:
        return [], "项目没有可生成用例的接口"
    config = load_config(root)
    normalized_llm = normalize_llm_config(config.get("llm", {}))
    roles = role_registry or default_role_registry()
    tools = tool_registry or build_case_generation_tool_registry()
    telemetry = GenerationTelemetry()
    agent_config = config.get("agents", {})
    planner_batch_size = bounded_int(agent_config.get("planner_batch_endpoints"), 25, 1, 100)
    designer_batch_size = bounded_int(agent_config.get("designer_batch_endpoints"), 8, 1, 30)
    designer_scenario_size = bounded_int(agent_config.get("designer_batch_scenarios"), 8, 1, 30)
    repair_attempts = bounded_int(agent_config.get("semantic_repair_attempts"), 2, 0, 2)
    data = ProjectDataAccess.create(root, project, endpoints=endpoints)
    dependency_graph = data.endpoints.dependency_graph()
    enabled_strategies = set(data.config.generation_strategies().get("enabled_default_strategies") or [])
    plan: list[dict[str, Any]] = fill_deterministic_coverage_gaps(
        [],
        endpoints,
        enabled_strategies=enabled_strategies,
        dependency_graph=dependency_graph,
    )
    warnings: list[str] = []

    planner_role = roles.resolve("test_planner")
    planner_model = llm_override or resolve_llm_role(normalized_llm, planner_role.model_role)
    testable_rule_ids = {
        str(rule.get("rule_id") or "")
        for endpoint in endpoints
        for rule in endpoint.business_rules
        if isinstance(rule, dict) and rule.get("testable", True) and rule.get("rule_id")
    }
    planned_rule_ids = {
        str(rule_id)
        for item in plan
        for rule_id in item.get("rule_ids") or []
    }
    needs_semantic_planning = bool(
        quality_feedback
        or not testable_rule_ids
        or testable_rule_ids - planned_rule_ids
        or any(item.get("origin") == "contract" for item in plan)
        or agent_config.get("planner_on_complete_contract", True)
    )
    planner_batches = (
        endpoint_batches(endpoints, planner_batch_size)
        if needs_semantic_planning and is_llm_configured(planner_model)
        else []
    )
    if needs_semantic_planning and not planner_batches:
        warnings.append("用例生成模型未配置，已交付确定性标准覆盖")
    elif not needs_semantic_planning:
        warnings.append("可信契约规则已被确定性计划完整覆盖，无需重复调用模型规划")
    for index, batch in enumerate(planner_batches, start=1):
        emit(stream_callback, f"开始 Agent 覆盖规划 {index}/{len(planner_batches)}：{len(batch)} 个接口。\n")
        task = planner_task_prompt(
            project,
            len(batch),
            batch_index=index,
            batch_count=len(planner_batches),
            allow_chain=len(planner_batches) == 1,
            deterministic_plan=[
                item
                for item in plan
                if item.get("endpoint_method") == "CHAIN"
                or (item.get("endpoint_method"), item.get("endpoint_path"))
                in {(endpoint.method.upper(), endpoint.path) for endpoint in batch}
            ],
            quality_feedback=quality_feedback,
        )
        try:
            result = run_role(
                root=root,
                project=project,
                endpoints=batch,
                role_id="test_planner",
                task_prompt=task,
                model_config=planner_model,
                roles=roles,
                tools=tools,
                enabled_tools=PLANNER_TOOLS,
                gateway_factory=gateway_factory,
                stream_callback=stream_callback,
                metadata={"task": "coverage_plan", "batch": index, "batch_count": len(planner_batches)},
            )
        except Exception as exc:
            record_operational_error(
                root,
                project,
                component="agent_case_generation",
                operation="planner_batch",
                error=exc,
                detail={"batch": index, "endpoint_count": len(batch)},
            )
            warnings.append(f"业务增量规划第 {index} 批失败，已保留确定性覆盖：{str(exc)[:500]}")
            continue
        telemetry.plan_runs += 1
        telemetry.include(result)
        raw_plan = ensure_object_list(result.artifact, "test_planner 没有提交有效覆盖计划", allow_empty=True)
        normalized, plan_issues = normalize_coverage_plan(raw_plan, batch)
        plan.extend(normalized)
        warnings.extend(issue_messages(plan_issues))

    plan, merge_issues = normalize_coverage_plan(plan, endpoints)
    warnings.extend(issue_messages(merge_issues))
    plan = fill_deterministic_coverage_gaps(
        plan,
        endpoints,
        enabled_strategies=enabled_strategies,
        dependency_graph=dependency_graph,
    )
    if not plan:
        raise ValueError("覆盖规划未产出任何有效场景")
    emit(stream_callback, f"覆盖计划完成：{len(plan)} 个场景，开始生成语义草稿。\n")

    deterministic_plan = [item for item in plan if item.get("origin") == "deterministic"]
    semantic_plan = [item for item in plan if item.get("origin") != "deterministic"]
    endpoint_by_key = {(endpoint.method.upper(), endpoint.path): endpoint for endpoint in endpoints}
    local_drafts = [fallback_draft(item, endpoint_by_key) for item in deterministic_plan]
    local_compilation = compile_scenario_drafts(
        local_drafts,
        endpoints,
        expected_scenario_ids=[item["scenario_id"] for item in deterministic_plan],
    )
    if local_compilation.error_issues:
        detail = "；".join(
            f"{issue.scenario_id or '-'} {issue.message}" for issue in local_compilation.error_issues[:10]
        )
        raise ValueError(f"确定性标准场景未通过本地编译门禁：{detail}")
    telemetry.local_compilations = len(deterministic_plan)
    all_cases: list[TestCase] = list(local_compilation.cases)
    accepted_scenario_ids = {case.scenario_id for case in all_cases if case.scenario_id}

    designer_model: dict[str, Any] = {}
    if semantic_plan:
        designer_role = roles.resolve("case_designer")
        designer_model = llm_override or resolve_llm_role(normalized_llm, designer_role.model_role)
        if not is_llm_configured(designer_model):
            raise ValueError("case_designer 对应的用例生成模型未配置")

    batches = plan_batches(semantic_plan, endpoints, designer_batch_size, designer_scenario_size)
    for index, (batch_plan, batch_endpoints) in enumerate(batches, start=1):
        emit(
            stream_callback,
            f"开始 Agent 场景设计 {index}/{len(batches)}：{len(batch_plan)} 个场景。\n",
        )
        try:
            compilation = design_and_compile_batch(
                root=root,
                project=project,
                plan_items=batch_plan,
                endpoints=batch_endpoints,
                model_config=designer_model,
                roles=roles,
                tools=tools,
                gateway_factory=gateway_factory,
                stream_callback=stream_callback,
                repair_attempts=repair_attempts,
                telemetry=telemetry,
                batch_index=index,
                batch_count=len(batches),
                quality_feedback=quality_feedback,
            )
        except Exception as exc:
            record_operational_error(
                root,
                project,
                component="agent_case_generation",
                operation="design_batch",
                error=exc,
                detail={"batch": index, "scenario_count": len(batch_plan)},
            )
            warnings.append(f"业务语义设计第 {index} 批失败，已剔除该批增量：{str(exc)[:500]}")
            continue
        all_cases.extend(compilation.cases)
        accepted_scenario_ids.update(case.scenario_id for case in compilation.cases if case.scenario_id)
        warnings.extend(issue_messages(issue for issue in compilation.issues if issue.severity != "error"))

    covered_contract_obligations = {
        obligation
        for case in all_cases
        for obligation in (
            case.coverage_partition_ids or case.rule_ids
        )
    }
    missing_contract_plan = [
        item
        for item in plan
        if item.get("rule_ids")
        and not set(
            item.get("coverage_partition_ids") or item.get("rule_ids") or []
        ).intersection(covered_contract_obligations)
    ]
    if missing_contract_plan:
        contract_fallbacks = [
            fallback_draft(item, endpoint_by_key)
            for item in missing_contract_plan
        ]
        contract_compilation = compile_scenario_drafts(
            contract_fallbacks,
            endpoints,
            expected_scenario_ids=[
                str(item["scenario_id"])
                for item in missing_contract_plan
            ],
        )
        if contract_compilation.error_issues:
            detail = "；".join(
                f"{item.scenario_id or '-'} {item.message}"
                for item in contract_compilation.error_issues[:10]
            )
            raise ValueError(f"可信接口契约兜底用例未通过本地编译：{detail}")
        all_cases.extend(contract_compilation.cases)
        accepted_scenario_ids.update(
            case.scenario_id
            for case in contract_compilation.cases
            if case.scenario_id
        )
        telemetry.local_fallbacks += len(contract_compilation.cases)
        warnings.append(
            f"{len(contract_compilation.cases)} 个未完成的业务规则已转为明确的本地契约/夹具用例，未静默丢弃"
        )

    deduplicated_cases = dedupe_cases([], all_cases)
    duplicate_count = len(all_cases) - len(deduplicated_cases)
    if duplicate_count:
        warnings.append(f"最终执行语义去重：剔除 {duplicate_count} 个与已交付场景完全相同的模型增量")
    all_cases = localize_generated_cases(
        normalize_cases(deduplicated_cases, source_tag="agent_compiled")
    )
    accepted_scenario_ids = {case.scenario_id for case in all_cases if case.scenario_id}
    final_issues = validate_compiled_cases(
        all_cases,
        endpoints,
        expected_scenario_ids=accepted_scenario_ids,
    )
    errors = [issue for issue in final_issues if issue.severity == "error"]
    if errors:
        detail = "；".join(issue.message for issue in errors[:10])
        raise ValueError(f"Agent 用例生成未通过最终质量门禁：{detail}")

    if record_lessons:
        try:
            record_case_generation_lessons(root, project, all_cases, actor="agent:case_designer")
        except Exception as exc:
            record_operational_error(
                root,
                project,
                component="context",
                operation="record_agent_case_generation_lessons",
                error=exc,
            )
    warnings.append(telemetry.summary())
    dropped_count = len(plan) - len(accepted_scenario_ids)
    warnings.append(
        f"质量门禁通过：交付 {len(accepted_scenario_ids)} 个场景"
        + (f"，剔除 {dropped_count} 个未闭环的模型增量" if dropped_count else "，无不合格场景")
    )
    return all_cases, "；".join(unique_strings(warnings))


def supplement_cases_with_agent_runtime(
    root: Path,
    endpoints: list[Endpoint],
    existing_cases: list[TestCase],
    *,
    project: str,
    scenario_text: str,
    max_cases: int = 5,
    stream_callback: Callable[[str], None] | None = None,
    llm_override: dict[str, Any] | None = None,
    gateway_factory: GatewayFactory = ProviderModelGateway,
    role_registry: RoleRegistry | None = None,
    tool_registry: ToolRegistry | None = None,
    quality_feedback: dict[str, Any] | None = None,
    quality_round: int = 1,
    record_lessons: bool = True,
) -> tuple[list[TestCase], str]:
    request = scenario_text.strip()
    if not request:
        raise ValueError("补充场景说明不能为空")
    if not endpoints:
        return [], "项目没有可补充用例的接口"
    limit = bounded_int(max_cases, 5, 1, 50)
    config = load_config(root)
    roles = role_registry or default_role_registry()
    tools = tool_registry or build_case_generation_tool_registry()
    role = roles.resolve("case_designer")
    llm = llm_override or resolve_llm_role(normalize_llm_config(config.get("llm", {})), role.model_role)
    if not is_llm_configured(llm):
        raise ValueError("case_designer 对应的用例生成模型未配置")
    attempts = bounded_int(config.get("agents", {}).get("semantic_repair_attempts"), 2, 0, 2)
    telemetry = GenerationTelemetry()
    task = f"""{output_language_instruction()}

为项目 {project} 补充不超过 {limit} 个 ScenarioDraft。

用户补充要求：
{request}

先分页调用 list_endpoints 和 list_existing_cases；再按需读取接口详情、已有用例详情、策略、依赖图和已确认经验。
只补充用户要求且尚未被已有用例覆盖的场景。scenario_id 使用 SCN-SUP-001 起的唯一编号。
不得为了凑数生成重复或无关场景。完成后调用 submit_case_drafts；如没有可新增场景，说明原因后仍需明确结束，不能伪造接口。
{(
    "上一轮质量审计未通过。只修复以下有证据问题并保留正确内容："
    + json.dumps(quality_feedback, ensure_ascii=False, separators=(",", ":"), default=str)
    if quality_feedback
    else ""
)}
"""
    result = run_agent_role(
        root=root,
        project=project,
        endpoints=endpoints,
        cases=existing_cases,
        role_id="case_designer",
        task_prompt=task,
        model_config=llm,
        roles=roles,
        tools=tools,
        enabled_tools=SUPPLEMENT_TOOLS,
        gateway_factory=gateway_factory,
        stream_callback=stream_callback,
        metadata={"task": "case_supplement", "max_cases": limit},
    )
    telemetry.design_runs += 1
    telemetry.include(result)
    drafts = ensure_object_list(
        result.artifact,
        "case_designer 没有提交有效补充场景草稿",
        allow_empty=True,
    )[:limit]
    if not drafts:
        return [], f"没有发现符合补充要求且未被已有用例覆盖的新场景；{telemetry.summary()}"
    scenario_ids = unique_scenario_ids(drafts)
    compilation = compile_scenario_drafts(drafts, endpoints, expected_scenario_ids=scenario_ids)
    previous_error_fingerprint = compilation_error_fingerprint(compilation)

    for attempt in range(1, attempts + 1):
        target_ids = error_scenario_ids(compilation, scenario_ids)
        if not target_ids:
            break
        repair = run_agent_role(
            root=root,
            project=project,
            endpoints=endpoints,
            cases=existing_cases,
            role_id="case_designer",
            task_prompt=supplement_repair_prompt(request, drafts, compilation.error_issues, target_ids),
            model_config=llm,
            roles=roles,
            tools=tools,
            enabled_tools=SUPPLEMENT_TOOLS,
            gateway_factory=gateway_factory,
            stream_callback=stream_callback,
            metadata={"task": "case_supplement_repair", "attempt": attempt, "scenario_ids": sorted(target_ids)},
        )
        telemetry.repair_runs += 1
        telemetry.include(repair)
        replacements = ensure_object_list(repair.artifact, "补充场景定向修订没有提交有效草稿")
        drafts = replace_drafts(drafts, replacements, target_ids)
        compilation = compile_scenario_drafts(drafts, endpoints, expected_scenario_ids=scenario_ids)
        fingerprint = compilation_error_fingerprint(compilation)
        if fingerprint and fingerprint == previous_error_fingerprint:
            break
        previous_error_fingerprint = fingerprint

    if compilation.error_issues:
        detail = "；".join(f"{item.scenario_id or '-'} {item.message}" for item in compilation.error_issues[:10])
        raise ValueError(f"补充场景未通过语义编译门禁：{detail}")
    generated = localize_generated_cases(
        dedupe_cases(existing_cases, compilation.cases)[:limit]
    )
    if not generated:
        return [], "补充场景与已有用例重复，未追加"
    if record_lessons:
        try:
            record_case_generation_lessons(root, project, generated, actor="agent:case_designer")
        except Exception as exc:
            record_operational_error(root, project, component="context", operation="record_agent_supplement_lessons", error=exc)
    return generated, f"补充场景语义门禁通过：{len(generated)} 条；{telemetry.summary()}"


def design_and_compile_batch(
    *,
    root: Path,
    project: str,
    plan_items: list[dict[str, Any]],
    endpoints: list[Endpoint],
    model_config: dict[str, Any],
    roles: RoleRegistry,
    tools: ToolRegistry,
    gateway_factory: GatewayFactory,
    stream_callback: Callable[[str], None] | None,
    repair_attempts: int,
    telemetry: GenerationTelemetry,
    batch_index: int,
    batch_count: int,
    quality_feedback: dict[str, Any] | None = None,
) -> CompilationResult:
    task = designer_task_prompt(
        project,
        plan_items,
        batch_index=batch_index,
        batch_count=batch_count,
        quality_feedback=quality_feedback,
    )
    result = run_role(
        root=root,
        project=project,
        endpoints=endpoints,
        role_id="case_designer",
        task_prompt=task,
        model_config=model_config,
        roles=roles,
        tools=tools,
        enabled_tools=designer_tools_for_plan(plan_items),
        gateway_factory=gateway_factory,
        stream_callback=stream_callback,
        metadata={"task": "case_design", "batch": batch_index, "batch_count": batch_count},
    )
    telemetry.design_runs += 1
    telemetry.include(result)
    raw_drafts = ensure_object_list(result.artifact, "case_designer 没有提交有效场景草稿")
    raw_drafts = apply_plan_contract_fields(raw_drafts, plan_items)
    scenario_ids = [item["scenario_id"] for item in plan_items]
    compilation = compile_scenario_drafts(raw_drafts, endpoints, expected_scenario_ids=scenario_ids)

    local_ids = locally_repairable_scenario_ids(compilation, scenario_ids)
    if local_ids:
        plan_by_id = {item["scenario_id"]: item for item in plan_items}
        endpoint_by_key = {(endpoint.method.upper(), endpoint.path): endpoint for endpoint in endpoints}
        fallbacks = [fallback_draft(plan_by_id[scenario_id], endpoint_by_key) for scenario_id in sorted(local_ids)]
        raw_drafts = replace_drafts(raw_drafts, fallbacks, local_ids)
        telemetry.local_fallbacks += len(fallbacks)
        compilation = compile_scenario_drafts(raw_drafts, endpoints, expected_scenario_ids=scenario_ids)

    previous_error_fingerprint = compilation_error_fingerprint(compilation)
    for attempt in range(1, repair_attempts + 1):
        error_ids = error_scenario_ids(compilation, scenario_ids)
        if not error_ids:
            break
        target_plan = [item for item in plan_items if item["scenario_id"] in error_ids]
        previous = [item for item in raw_drafts if str(item.get("scenario_id") or "") in error_ids]
        issues = [issue.to_dict() for issue in compilation.error_issues if not issue.scenario_id or issue.scenario_id in error_ids]
        emit(stream_callback, f"{len(error_ids)} 个场景未通过语义门禁，开始第 {attempt} 次定向修订。\n")
        repair_result = run_role(
            root=root,
            project=project,
            endpoints=endpoints_for_plan(target_plan, endpoints),
            role_id="case_designer",
            task_prompt=repair_task_prompt(project, target_plan, previous, issues),
            model_config=model_config,
            roles=roles,
            tools=tools,
            enabled_tools=designer_tools_for_plan(target_plan),
            gateway_factory=gateway_factory,
            stream_callback=stream_callback,
            metadata={"task": "case_design_repair", "attempt": attempt, "scenario_ids": sorted(error_ids)},
        )
        telemetry.repair_runs += 1
        telemetry.include(repair_result)
        repaired = ensure_object_list(repair_result.artifact, "定向修订没有提交有效场景草稿")
        repaired = apply_plan_contract_fields(repaired, target_plan)
        raw_drafts = replace_drafts(raw_drafts, repaired, error_ids)
        compilation = compile_scenario_drafts(raw_drafts, endpoints, expected_scenario_ids=scenario_ids)
        fingerprint = compilation_error_fingerprint(compilation)
        if fingerprint and fingerprint == previous_error_fingerprint:
            break
        previous_error_fingerprint = fingerprint

    remaining_ids = error_scenario_ids(compilation, scenario_ids)
    if remaining_ids:
        plan_by_id = {item["scenario_id"]: item for item in plan_items}
        endpoint_by_key = {(endpoint.method.upper(), endpoint.path): endpoint for endpoint in endpoints}
        fallbacks = [fallback_draft(plan_by_id[scenario_id], endpoint_by_key) for scenario_id in sorted(remaining_ids)]
        raw_drafts = replace_drafts(raw_drafts, fallbacks, remaining_ids)
        telemetry.local_fallbacks += len(fallbacks)
        compilation = compile_scenario_drafts(raw_drafts, endpoints, expected_scenario_ids=scenario_ids)

    if compilation.error_issues:
        dropped_ids = error_scenario_ids(compilation, scenario_ids)
        retained_ids = [scenario_id for scenario_id in scenario_ids if scenario_id not in dropped_ids]
        retained_raw = [
            item
            for item in raw_drafts
            if str(item.get("scenario_id") or "") in set(retained_ids)
        ]
        retained = compile_scenario_drafts(retained_raw, endpoints, expected_scenario_ids=retained_ids)
        if retained.error_issues:
            detail = "；".join(
                f"{issue.scenario_id or '-'} {issue.message}" for issue in retained.error_issues[:10]
            )
            raise ValueError(f"合格业务增量重新编译失败：{detail}")
        drop_issues = []
        for scenario_id in sorted(dropped_ids):
            reasons = unique_strings(
                issue.message
                for issue in compilation.error_issues
                if not issue.scenario_id or issue.scenario_id == scenario_id
            )
            drop_issues.append(
                DraftIssue(
                    "dropped_unrepairable_semantic_scenario",
                    "模型增量经过定向修订和本地兜底后仍不闭环，已剔除：" + "；".join(reasons[:5]),
                    scenario_id,
                    severity="warning",
                )
            )
        compilation = CompilationResult(
            drafts=retained.drafts,
            cases=retained.cases,
            issues=[*retained.issues, *drop_issues],
        )
    return compilation


def apply_plan_contract_fields(
    drafts: list[dict[str, Any]],
    plan_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    plan_by_id = {
        str(item.get("scenario_id") or ""): item
        for item in plan_items
    }
    result: list[dict[str, Any]] = []
    for draft in drafts:
        copied = dict(draft)
        plan = plan_by_id.get(str(copied.get("scenario_id") or ""), {})
        if plan:
            copied["endpoint_method"] = str(
                plan.get("endpoint_method") or copied.get("endpoint_method") or ""
            ).upper()
            copied["endpoint_path"] = str(
                plan.get("endpoint_path")
                if "endpoint_path" in plan
                else copied.get("endpoint_path") or ""
            )
            if plan.get("design_method"):
                copied["design_method"] = str(plan["design_method"])
            if plan.get("expected_basis"):
                copied["expected_basis"] = str(plan["expected_basis"])
            request_overrides = dict(copied.get("request_overrides") or {})
            merge_nested_mapping(
                request_overrides,
                dict(plan.get("request_overrides") or {}),
            )
            copied["request_overrides"] = request_overrides
            copied["omit_fields"] = unique_strings(
                [
                    *(copied.get("omit_fields") or []),
                    *(plan.get("omit_fields") or []),
                ]
            )
            copied["assertions"] = merge_assertion_payloads(
                copied.get("assertions") or [],
                plan.get("assertions") or [],
            )
            if plan.get("chain_steps"):
                copied["chain_steps"] = [
                    dict(item)
                    for item in plan["chain_steps"]
                    if isinstance(item, dict)
                ]
        for key in ("rule_ids", "coverage_partition_ids", "fixture_requirements"):
            if plan.get(key):
                copied[key] = list(plan[key])
        if isinstance(plan.get("runnable"), bool):
            copied["runnable"] = plan["runnable"]
        if plan.get("risk"):
            copied["risk"] = str(plan["risk"])
        result.append(copied)
    return result


def run_role(
    *,
    root: Path,
    project: str,
    endpoints: list[Endpoint],
    role_id: str,
    task_prompt: str,
    model_config: dict[str, Any],
    roles: RoleRegistry,
    tools: ToolRegistry,
    enabled_tools: tuple[str, ...] | None = None,
    gateway_factory: GatewayFactory,
    stream_callback: Callable[[str], None] | None,
    metadata: dict[str, Any],
) -> AgentTaskResult:
    return run_agent_role(
        root=root,
        project=project,
        endpoints=endpoints,
        role_id=role_id,
        task_prompt=task_prompt,
        model_config=model_config,
        roles=roles,
        tools=tools,
        enabled_tools=enabled_tools,
        gateway_factory=gateway_factory,
        stream_callback=stream_callback,
        metadata=metadata,
    )


def planner_task_prompt(
    project: str,
    endpoint_count: int,
    *,
    batch_index: int,
    batch_count: int,
    allow_chain: bool,
    deterministic_plan: list[dict[str, Any]],
    quality_feedback: dict[str, Any] | None = None,
) -> str:
    chain_instruction = (
        "可以在依赖图有明确证据时规划 CHAIN 场景。"
        if allow_chain
        else "本批不要规划 CHAIN；跨批链路由框架根据已验证依赖图补齐。"
    )
    deterministic_summary = [
        {
            "endpoint_method": item.get("endpoint_method"),
            "endpoint_path": item.get("endpoint_path"),
            "design_method": item.get("design_method"),
            "expected_basis": item.get("expected_basis", ""),
            "rule_ids": item.get("rule_ids", []),
            "coverage_partition_ids": item.get("coverage_partition_ids", []),
            "runnable": item.get("runnable"),
            "fixture_requirements": item.get("fixture_requirements", []),
        }
        for item in deterministic_plan
    ]
    feedback = (
        "\n\n上一轮最终用例质量审计未通过。规划时必须避免以下有证据问题：\n"
        + json.dumps(quality_feedback, ensure_ascii=False, separators=(",", ":"), default=str)
        if quality_feedback
        else ""
    )
    return f"""{output_language_instruction()}

为项目 {project} 补充本地规则无法表达的业务测试覆盖计划。

不要输出分析过程、接口复述或计划说明；读取必要事实后直接调用 submit_coverage_plan。

当前是第 {batch_index}/{batch_count} 批，共 {endpoint_count} 个目标接口。目标范围就是 list_endpoints 返回的全部接口。
框架已经确定性生成以下标准覆盖，不得重复提交：
{json.dumps(deterministic_summary, ensure_ascii=False, separators=(',', ':'))}
先分页调用 list_endpoints 和 list_business_rules，确认每条可信业务规则已经由上述标准覆盖处理；需要完整触发条件时调用 get_business_rule_detail 或 get_endpoint_detail。
接口定义和 business_rules 是接口识别阶段已经审计通过的唯一事实，不得读取接口文档原文或自行补充接口事实。
再按需调用 get_generation_strategies、search_confirmed_lessons 和 get_dependency_graph。
{chain_instruction}

要求：
1. 只提交可信 business_rules、已确认经验或跨接口语义带来的新增场景；没有新增时提交空 items。
2. status_code_coverage 只能引用文档声明的 responses.<status>。
3. 可合并的策略放入 coverage_tags，避免只改标题的重复场景。
4. design_method 只能使用 happy_path、optional_field、required_field、type_error、boundary_value、path_parameter_error、dependency_chain、auth_error、equivalence_class、status_code_coverage。
5. 普通接口场景必须能由单次请求执行；重复调用、前后状态比较和跨接口校验必须使用 CHAIN。
6. 需要时间控制、邮件注入或隔离限流的可信规则不能省略；无法立即执行时提交 runnable=false、risk、fixture_requirements 和对应 rule_ids。
7. 每个业务规则场景必须携带 rule_ids；发现可信接口契约缺少必要事实时明确指出上游缺陷，不能通过猜测补齐。
8. 完成后调用 submit_coverage_plan，不要用普通文本提交结果。
{feedback}"""


def designer_task_prompt(
    project: str,
    plan_items: list[dict[str, Any]],
    *,
    batch_index: int,
    batch_count: int,
    quality_feedback: dict[str, Any] | None = None,
) -> str:
    feedback = (
        "\n\n上一轮最终用例质量审计未通过。只修复下列有证据的问题，不能为提高分数猜测接口行为：\n"
        + json.dumps(quality_feedback, ensure_ascii=False, separators=(",", ":"), default=str)
        if quality_feedback
        else ""
    )
    return f"""{output_language_instruction()}

为项目 {project} 的覆盖计划生成 ScenarioDraft。这是第 {batch_index}/{batch_count} 批。

不要输出分析过程、计划复述或逐场景说明；读取必要事实后直接调用 submit_case_drafts。

覆盖计划：
{json.dumps(plan_items, ensure_ascii=False, separators=(',', ':'))}

先调用 get_endpoint_detail 核对计划涉及的接口，并按计划中的 rule_ids 调用 get_business_rule_detail；只有 CHAIN 批次才读取依赖图。覆盖策略和历史经验已经由规划阶段确定，不要重复检索或重新规划。
接口定义和 business_rules 是上游已审计产物，不读取接口文档原文。每个草稿必须原样保留计划中的 rule_ids、coverage_partition_ids、runnable、risk 和 fixture_requirements。
每个 scenario_id 恰好提交一个草稿。只提交业务语义、请求差量、预期结果和证据，不生成最终 TestCase 的 ID、preconditions、test_steps、setup_steps 或 cleanup_steps。
request_overrides 只写相对接口默认请求的差量；删除字段使用 omit_fields，例如 body.phone 或 query.prefix。
CHAIN 场景把计划中的步骤保留在 chain_steps，并补充必要的 extract 与模板注入。
普通接口草稿不要填写 dependencies；它不是执行步骤，多步骤语义必须用 CHAIN 表达。
CHAIN 预期中的状态码、空数组和字段值必须落到对应步骤断言；前后结果相同或一致必须用 extract 变量进行跨步骤比较。
完成后调用 submit_case_drafts，不要用普通文本提交。
{feedback}"""


def repair_task_prompt(
    project: str,
    plan_items: list[dict[str, Any]],
    previous: list[dict[str, Any]],
    issues: list[dict[str, Any]],
) -> str:
    return f"""{output_language_instruction()}

定向修订项目 {project} 中未通过门禁的 ScenarioDraft，只处理列出的场景。

目标计划：{json.dumps(plan_items, ensure_ascii=False, separators=(',', ':'))}
上次草稿：{json.dumps(previous, ensure_ascii=False, separators=(',', ':'))}
门禁问题：{json.dumps(issues, ensure_ascii=False, separators=(',', ':'))}

调用接口详情工具核对事实，修正问题后通过 submit_case_drafts 提交这些场景。不要提交无关场景。
"""


def fallback_draft(
    plan_item: dict[str, Any],
    endpoint_by_key: dict[tuple[str, str], Endpoint],
) -> dict[str, Any]:
    method = str(plan_item["endpoint_method"]).upper()
    if method == "CHAIN":
        title = str(plan_item.get("title_hint") or "接口依赖主链路")
        basis = str(plan_item.get("expected_basis") or "dependency_graph")
        return {
            "scenario_id": plan_item["scenario_id"],
            "endpoint_method": "CHAIN",
            "endpoint_path": "",
            "intent": title,
            "design_method": "dependency_chain",
            "priority_hint": str(plan_item.get("priority") or "P0"),
            "coverage_tags": plan_item.get("coverage_tags") or ["dependency_chain"],
            "expected_outcome": (
                f"{title}；{basis}"
                if plan_item.get("origin") == "model"
                else f"{title}，各步骤按顺序执行成功，生产者变量能够传递给后续接口。"
            ),
            "expected_basis": basis,
            "chain_steps": plan_item.get("chain_steps") or [],
            "assertions": plan_item.get("assertions") or [],
            "rule_ids": plan_item.get("rule_ids") or [],
            "coverage_partition_ids": plan_item.get("coverage_partition_ids") or [],
            "fixture_requirements": plan_item.get("fixture_requirements") or [],
            "runnable": plan_item.get("runnable") if isinstance(plan_item.get("runnable"), bool) else None,
            "risk": str(plan_item.get("risk") or ""),
        }
    path = str(plan_item["endpoint_path"])
    endpoint = endpoint_by_key[(method, path)]
    design_method = str(plan_item.get("design_method") or "happy_path")
    expected_basis = str(plan_item.get("expected_basis") or "")
    request_overrides: dict[str, Any] = {}
    omit_fields: list[str] = []
    apply_fallback_request_design(endpoint, design_method, expected_basis, request_overrides, omit_fields)
    merge_nested_mapping(request_overrides, dict(plan_item.get("request_overrides") or {}))
    omit_fields = unique_strings([*omit_fields, *(plan_item.get("omit_fields") or [])])
    rule = next(
        (
            item
            for item in endpoint.business_rules
            if isinstance(item, dict)
            and str(item.get("rule_id") or "") in set(plan_item.get("rule_ids") or [])
        ),
        {},
    )
    fixture_requirements = list(plan_item.get("fixture_requirements") or [])
    if "isolated_rate_limit" in fixture_requirements:
        request_overrides = {}
        omit_fields = []
    expected = rule.get("expected") if isinstance(rule.get("expected"), dict) else {}
    fallback_runnable = (
        plan_item.get("runnable")
        if isinstance(plan_item.get("runnable"), bool)
        else None
    )
    fallback_risk = str(plan_item.get("risk") or "")
    if (
        plan_item.get("origin") == "contract"
        and design_method == "dependency_chain"
        and not plan_item.get("chain_steps")
    ):
        fallback_runnable = False
        fallback_risk = fallback_risk or "fixture_required"
    expected_outcome = str(
        expected.get("description")
        or rule.get("description")
        or expected_outcome_from_document(endpoint, expected_basis, design_method)
    )
    assertions, explicit_expectations = fallback_response_assertions(
        endpoint,
        expected_basis,
        design_method=design_method,
    )
    assertions = merge_assertion_payloads(
        assertions,
        [
            *(
                expected.get("assertions")
                if isinstance(expected.get("assertions"), list)
                else []
            ),
            *(plan_item.get("assertions") or []),
        ],
    )
    assertions = merge_assertion_payloads(
        assertions,
        request_response_relation_assertions(
            endpoint,
            request_overrides,
            expected_basis,
        ),
    )
    if explicit_expectations:
        expected_outcome = f"{expected_outcome}；" + "；".join(explicit_expectations)
    return {
        "scenario_id": plan_item["scenario_id"],
        "endpoint_method": method,
        "endpoint_path": path,
        "intent": str(plan_item.get("title_hint") or design_method),
        "design_method": design_method,
        "priority_hint": str(plan_item.get("priority") or "P1"),
        "coverage_tags": plan_item.get("coverage_tags") or [design_method],
        "request_overrides": request_overrides,
        "omit_fields": omit_fields,
        "expected_outcome": expected_outcome,
        "expected_basis": expected_basis,
        "assertions": assertions,
        "runnable": fallback_runnable,
        "risk": fallback_risk,
        "rule_ids": plan_item.get("rule_ids") or [],
        "coverage_partition_ids": plan_item.get("coverage_partition_ids") or [],
        "fixture_requirements": fixture_requirements,
    }


def merge_nested_mapping(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            merge_nested_mapping(target[key], value)
        else:
            target[key] = value


def merge_assertion_payloads(
    first: list[dict[str, Any]],
    second: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in [*first, *second]:
        if not isinstance(item, dict):
            continue
        key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            result.append(dict(item))
    return result


def merge_subsumed_chain_cases(cases: list[TestCase]) -> list[TestCase]:
    removed: set[int] = set()
    for short_index, shorter in enumerate(cases):
        if short_index in removed or shorter.method.upper() != "CHAIN":
            continue
        candidates = [
            longer
            for long_index, longer in enumerate(cases)
            if (
                long_index != short_index
                and long_index not in removed
                and chain_case_is_strict_prefix(shorter, longer)
            )
        ]
        if not candidates:
            continue
        target = min(candidates, key=lambda item: len(item.steps))
        target.rule_ids = unique_strings([*target.rule_ids, *shorter.rule_ids])
        target.coverage_partition_ids = unique_strings(
            [*target.coverage_partition_ids, *shorter.coverage_partition_ids]
        )
        target.tags = unique_strings([*target.tags, *shorter.tags])
        target.fixture_requirements = unique_strings(
            [*target.fixture_requirements, *shorter.fixture_requirements]
        )
        removed.add(short_index)
    return [
        case
        for index, case in enumerate(cases)
        if index not in removed
    ]


def chain_case_is_strict_prefix(shorter: TestCase, longer: TestCase) -> bool:
    if (
        longer.method.upper() != "CHAIN"
        or not 1 < len(shorter.steps) < len(longer.steps)
        or shorter.runnable != longer.runnable
        or shorter.risk != longer.risk
        or sorted(shorter.fixture_requirements) != sorted(longer.fixture_requirements)
        or to_jsonable(shorter.assertions) != to_jsonable(longer.assertions)
    ):
        return False
    return all(
        step_execution_payload(short_step) == step_execution_payload(long_step)
        for short_step, long_step in zip(shorter.steps, longer.steps)
    )


def request_response_relation_assertions(
    endpoint: Endpoint,
    request_overrides: dict[str, Any],
    expected_basis: str,
) -> list[dict[str, Any]]:
    status = documented_status_from_basis(expected_basis)
    if not isinstance(status, int) or not 200 <= status < 300:
        return []
    query = request_overrides.get("query")
    prefix = query.get("prefix") if isinstance(query, dict) else None
    if not isinstance(prefix, str) or not prefix or "{{" in prefix:
        return []
    response = endpoint.responses.get(str(status)) or endpoint.responses.get(status)
    media = response.get("content") if isinstance(response, dict) else None
    json_media = media.get("application/json") if isinstance(media, dict) else None
    schema = json_media.get("schema") if isinstance(json_media, dict) else None
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict) or "email" not in properties:
        return []
    domain = ""
    for rule in endpoint.business_rules:
        if not isinstance(rule, dict):
            continue
        text = " ".join(
            str(rule.get(key) or "")
            for key in ("title", "description", "source_condition", "source_evidence")
        )
        match = re.search(
            r"(?:邮箱域名固定为|生成邮箱地址为\s*\{?prefix\}?@)\s*`?([A-Za-z0-9.-]+)",
            text,
            flags=re.I,
        )
        if match:
            domain = match.group(1).rstrip(".")
            break
    if not domain:
        return []
    return [
        {
            "type": "json_path",
            "target": "$.email",
            "operator": "eq",
            "expected": f"{prefix}@{domain}",
        }
    ]


def apply_fallback_request_design(
    endpoint: Endpoint,
    design_method: str,
    expected_basis: str,
    overrides: dict[str, Any],
    omit_fields: list[str],
) -> None:
    query_parameters = [item for item in endpoint.parameters if item.get("in") == "query"]
    path_parameters = [item for item in endpoint.parameters if item.get("in") == "path"]
    header_parameters = [item for item in endpoint.parameters if item.get("in") == "header"]
    cookie_parameters = [item for item in endpoint.parameters if item.get("in") == "cookie"]
    body = default_body(endpoint)
    status = documented_status_from_basis(expected_basis)
    effective = design_method
    if design_method == "status_code_coverage":
        if status in {401, 403}:
            effective = "auth_error"
        elif status == 404 and path_parameters:
            effective = "path_parameter_error"
        elif status and status >= 400 and status != 429:
            effective = "type_error"
    if effective == "optional_field":
        optional = next((item for item in query_parameters if not item.get("required")), None)
        if optional:
            name = str(optional.get("name") or "")
            overrides["query"] = {name: default_query(endpoint, include_optional=True).get(name)}
        else:
            optional_body = first_optional_body_field(endpoint)
            if optional_body:
                overrides["body"] = default_body(endpoint)
    elif effective == "required_field":
        required_query = next((item for item in query_parameters if item.get("required")), None)
        if required_query:
            omit_fields.append(f"query.{required_query.get('name')}")
        elif required_header := next((item for item in header_parameters if item.get("required")), None):
            omit_fields.append(f"headers.{required_header.get('name')}")
        elif any(item.get("required") for item in cookie_parameters):
            omit_fields.append("headers.Cookie")
        else:
            required_body = first_required_body_field(endpoint)
            if required_body:
                omit_fields.append(f"body.{required_body}")
            elif endpoint.request_body:
                overrides["body"] = {}
    elif effective == "path_parameter_error" and path_parameters:
        name = str(path_parameters[0].get("name") or "")
        overrides["path_params"] = {name: f"invalid-{name}"}
    elif effective in {"type_error", "boundary_value"}:
        candidates = [*query_parameters, *path_parameters]
        if effective == "type_error":
            parameter = next((item for item in candidates if parameter_is_typed(item)), None)
        else:
            parameter = next((item for item in candidates if parameter_has_constraints(item)), None)
        if parameter:
            name = str(parameter.get("name") or "")
            if effective == "type_error":
                value = wrong_type_value(parameter)
            else:
                values = boundary_values(parameter)
                value = values[-1][1] if values else ""
            section = "query" if parameter.get("in") == "query" else "path_params"
            overrides[section] = {name: value}
        elif endpoint.request_body:
            body_override = fallback_body_field_value(endpoint, body, effective)
            overrides["body"] = body_override
    elif effective == "auth_error":
        overrides["headers"] = {"Authorization": ""}


def expected_outcome_from_document(endpoint: Endpoint, basis: str, design_method: str) -> str:
    status = documented_status_from_basis(basis)
    if status is not None:
        response = endpoint.responses.get(str(status)) or endpoint.responses.get(status) or {}
        description = str(response.get("description") or "").strip() if isinstance(response, dict) else ""
        if description:
            return description
        return f"接口返回文档声明的 {status} 响应。"
    successes = []
    for key, response in endpoint.responses.items():
        try:
            if 200 <= int(str(key)) < 300:
                successes.append(response)
        except ValueError:
            continue
    for response in successes:
        if isinstance(response, dict) and str(response.get("description") or "").strip():
            return str(response["description"]).strip()
    if design_method in {"type_error", "boundary_value", "required_field", "path_parameter_error", "auth_error"}:
        return "接口应根据文档规则拒绝无效输入，并返回明确且非服务端异常的结果。"
    return "接口使用符合文档的输入时，应返回文档声明的成功结果。"


def fallback_response_assertions(
    endpoint: Endpoint,
    basis: str,
    *,
    design_method: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    status = documented_status_from_basis(basis)
    # A negative strategy without an explicit documented response must not
    # borrow field assertions from the success schema. The deterministic
    # compiler will still add an error-status assertion appropriate to the
    # strategy, while response fields remain unspecified until a concrete
    # responses.<status> basis is available.
    if status is None and design_method in NEGATIVE_DESIGN_METHODS:
        return [], []
    if status is None:
        status = next(
            (
                int(str(key))
                for key in endpoint.responses
                if str(key).isdigit() and 200 <= int(str(key)) < 300
            ),
            None,
        )
    response = endpoint.responses.get(str(status)) or endpoint.responses.get(status) if status is not None else {}
    if not isinstance(response, dict):
        return [], []
    content = response.get("content") if isinstance(response.get("content"), dict) else {}
    schemas = [response.get("schema"), *(item.get("schema") for item in content.values() if isinstance(item, dict))]
    schema = next((item for item in schemas if isinstance(item, dict)), {})
    if not schema:
        return [], []
    assertions: list[dict[str, Any]] = []
    explicit: list[str] = []
    if schema.get("type") == "array":
        assertions.append({"type": "json_path", "target": "$", "operator": "is_array", "expected": True})
        return assertions, explicit
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = {str(item) for item in schema.get("required", [])} if isinstance(schema.get("required"), list) else set(properties)
    for name, raw_property in properties.items():
        if str(name) not in required or not isinstance(raw_property, dict):
            continue
        target = f"$.{name}"
        expected = explicit_schema_value(raw_property)
        if expected is not None:
            assertions.append({"type": "json_path", "target": target, "operator": "eq", "expected": expected})
            explicit.append(f"{name}={json.dumps(expected, ensure_ascii=False)}")
            continue
        schema_type = raw_property.get("type")
        nullable = isinstance(schema_type, list) and "null" in schema_type
        if isinstance(schema_type, list):
            schema_type = next((item for item in schema_type if item != "null"), "")
        operator = "exists" if nullable else {
            "array": "is_array",
            "string": "is_string",
            "integer": "is_integer",
            "number": "is_number",
            "boolean": "is_boolean",
            "object": "is_object",
        }.get(str(schema_type or ""), "exists")
        assertions.append({"type": "json_path", "target": target, "operator": operator, "expected": True})
    return assertions, explicit


def explicit_schema_value(schema: dict[str, Any]) -> Any:
    for key in ("const", "default"):
        if key in schema:
            return schema[key]
    enum = schema.get("enum")
    if isinstance(enum, list) and len(enum) == 1:
        return enum[0]
    description = str(schema.get("description") or "")
    match = re.search(r"(?:默认(?:返回)?|固定(?:返回)?|恒为)\s*[：:=]?\s*(-?\d+(?:\.\d+)?)", description)
    if match:
        number = float(match.group(1))
        return int(number) if number.is_integer() else number
    return None


def first_required_body_field(endpoint: Endpoint) -> str:
    body = endpoint.request_body or {}
    content = body.get("content") or {}
    media = content.get("application/json") or next(iter(content.values()), {}) if content else {}
    schema = media.get("schema") if isinstance(media, dict) else {}
    required = schema.get("required") if isinstance(schema, dict) else []
    return str(required[0]) if isinstance(required, list) and required else ""


def first_optional_body_field(endpoint: Endpoint) -> str:
    body = endpoint.request_body or {}
    content = body.get("content") or {}
    media = content.get("application/json") or next(iter(content.values()), {}) if content else {}
    schema = media.get("schema") if isinstance(media, dict) else {}
    properties = schema.get("properties") if isinstance(schema, dict) and isinstance(schema.get("properties"), dict) else {}
    required = {str(item) for item in schema.get("required", [])} if isinstance(schema, dict) and isinstance(schema.get("required"), list) else set()
    return next((str(name) for name in properties if str(name) not in required), "")


def parameter_is_typed(parameter: dict[str, Any]) -> bool:
    schema = parameter.get("schema") if isinstance(parameter.get("schema"), dict) else {}
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        schema_type = next((item for item in schema_type if item != "null"), "")
    return bool(schema_type and schema_type != "string")


def parameter_has_constraints(parameter: dict[str, Any]) -> bool:
    schema = parameter.get("schema") if isinstance(parameter.get("schema"), dict) else {}
    return any(key in schema for key in ("minLength", "maxLength", "minimum", "maximum", "enum", "pattern", "minItems", "maxItems"))


def fallback_body_field_value(endpoint: Endpoint, body: Any, design_method: str) -> Any:
    request_body = endpoint.request_body or {}
    content = request_body.get("content") or {}
    media = content.get("application/json") or next(iter(content.values()), {}) if content else {}
    schema = media.get("schema") if isinstance(media, dict) else {}
    properties = schema.get("properties") if isinstance(schema, dict) and isinstance(schema.get("properties"), dict) else {}
    predicate = parameter_is_typed if design_method == "type_error" else parameter_has_constraints
    selected = next(
        ((str(name), value) for name, value in properties.items() if isinstance(value, dict) and predicate({"schema": value})),
        None,
    )
    if selected and isinstance(body, dict):
        name, property_schema = selected
        updated = dict(body)
        parameter = {"name": name, "schema": property_schema}
        if design_method == "type_error":
            updated[name] = wrong_type_value(parameter)
        else:
            values = boundary_values(parameter)
            updated[name] = values[-1][1] if values else ""
        return updated
    return "invalid_body" if design_method == "type_error" else ({} if isinstance(body, dict) else "")


def endpoint_batches(endpoints: list[Endpoint], size: int) -> list[list[Endpoint]]:
    return [endpoints[index : index + size] for index in range(0, len(endpoints), size)]


def plan_batches(
    plan: list[dict[str, Any]],
    endpoints: list[Endpoint],
    endpoint_limit: int,
    scenario_limit: int,
) -> list[tuple[list[dict[str, Any]], list[Endpoint]]]:
    by_endpoint: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    chain_items: list[dict[str, Any]] = []
    for item in plan:
        if item["endpoint_method"] == "CHAIN":
            chain_items.append(item)
        else:
            by_endpoint[(item["endpoint_method"], item["endpoint_path"])].append(item)
    keys = list(by_endpoint)
    result: list[tuple[list[dict[str, Any]], list[Endpoint]]] = []
    for index in range(0, len(keys), endpoint_limit):
        selected = keys[index : index + endpoint_limit]
        selected_plan = [item for key in selected for item in by_endpoint[key]]
        for scenario_index in range(0, len(selected_plan), scenario_limit):
            scenario_batch = selected_plan[scenario_index : scenario_index + scenario_limit]
            result.append((scenario_batch, endpoints_for_plan(scenario_batch, endpoints)))
    if chain_items:
        for scenario_index in range(0, len(chain_items), scenario_limit):
            scenario_batch = chain_items[scenario_index : scenario_index + scenario_limit]
            result.append((scenario_batch, endpoints_for_plan(scenario_batch, endpoints)))
    return result


def endpoints_for_plan(plan_items: list[dict[str, Any]], endpoints: list[Endpoint]) -> list[Endpoint]:
    keys: set[tuple[str, str]] = set()
    for item in plan_items:
        if item.get("endpoint_method") == "CHAIN":
            keys.update(
                (str(step.get("method") or "").upper(), str(step.get("path") or ""))
                for step in item.get("chain_steps") or []
                if isinstance(step, dict)
            )
        else:
            keys.add((str(item.get("endpoint_method") or "").upper(), str(item.get("endpoint_path") or "")))
    targets = [endpoint for endpoint in endpoints if (endpoint.method.upper(), endpoint.path) in keys]
    required_fields = {
        str(parameter.get("name") or "")
        for endpoint in targets
        for parameter in endpoint.parameters
        if parameter.get("in") == "path" and parameter.get("name")
    }
    if required_fields:
        for producer in dependency_producers(endpoints):
            if required_fields.intersection(likely_produced_fields(producer)):
                keys.add((producer.method.upper(), producer.path))
    return [endpoint for endpoint in endpoints if (endpoint.method.upper(), endpoint.path) in keys]


def designer_tools_for_plan(plan_items: list[dict[str, Any]]) -> tuple[str, ...]:
    if any(
        item.get("endpoint_method") == "CHAIN" or item.get("design_method") == "dependency_chain"
        for item in plan_items
    ):
        return (*DESIGNER_TOOLS[:-1], "get_dependency_graph", "submit_case_drafts")
    return DESIGNER_TOOLS


def replace_drafts(
    current: list[dict[str, Any]],
    replacements: list[dict[str, Any]],
    target_ids: set[str],
) -> list[dict[str, Any]]:
    result = [item for item in current if str(item.get("scenario_id") or "") not in target_ids]
    replacement_by_id = {
        str(item.get("scenario_id") or ""): item
        for item in replacements
        if str(item.get("scenario_id") or "") in target_ids
    }
    result.extend(replacement_by_id.values())
    return result


def error_scenario_ids(compilation: CompilationResult, expected_ids: list[str]) -> set[str]:
    result = {issue.scenario_id for issue in compilation.error_issues if issue.scenario_id}
    if any(not issue.scenario_id for issue in compilation.error_issues):
        result.update(expected_ids)
    compiled = {case.scenario_id for case in compilation.cases if case.scenario_id}
    result.update(set(expected_ids) - compiled)
    return result


def ensure_object_list(value: Any, message: str, *, allow_empty: bool = False) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(message)
    result = [dict(item) for item in value if isinstance(item, dict)]
    if not result and not allow_empty:
        raise ValueError(message)
    return result


LOCAL_REPAIRABLE_CODES = {
    "missing_scenario",
    "missing_scenario_id",
    "duplicate_scenario_id",
    "unexpected_scenario",
    "unknown_endpoint",
    "invalid_chain",
    "missing_intent",
    "missing_expected_outcome",
    "invalid_assertion_type",
    "invalid_assertion_operator",
    "missing_assertion_expected",
    "undocumented_status_assertion",
    "assertion_field_not_in_schema",
    "missing_compiled_scenario",
    "schema_placeholder_request_value",
    "unresolved_template_variable",
    "contradictory_optional_parameter",
    "contradictory_optional_body_field",
}


def locally_repairable_scenario_ids(compilation: CompilationResult, expected_ids: list[str]) -> set[str]:
    by_id: dict[str, list[DraftIssue]] = defaultdict(list)
    global_issues: list[DraftIssue] = []
    for issue in compilation.error_issues:
        if issue.scenario_id:
            by_id[issue.scenario_id].append(issue)
        else:
            global_issues.append(issue)
    if global_issues and all(issue.code in LOCAL_REPAIRABLE_CODES for issue in global_issues):
        return set(expected_ids)
    return {
        scenario_id
        for scenario_id, issues in by_id.items()
        if issues and all(issue.code in LOCAL_REPAIRABLE_CODES for issue in issues)
    }


def compilation_error_fingerprint(compilation: CompilationResult) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        sorted((issue.scenario_id, issue.code, issue.message) for issue in compilation.error_issues)
    )


def unique_scenario_ids(drafts: list[dict[str, Any]]) -> list[str]:
    result: list[str] = []
    for item in drafts:
        scenario_id = str(item.get("scenario_id") or "").strip()
        if scenario_id and scenario_id not in result:
            result.append(scenario_id)
    return result


def supplement_repair_prompt(
    request: str,
    drafts: list[dict[str, Any]],
    issues: list[DraftIssue],
    target_ids: set[str],
) -> str:
    previous = [item for item in drafts if str(item.get("scenario_id") or "") in target_ids]
    relevant = [item.to_dict() for item in issues if not item.scenario_id or item.scenario_id in target_ids]
    return f"""{output_language_instruction()}

定向修订未通过门禁的补充 ScenarioDraft，只提交目标 scenario_id。

原补充要求：{request}
目标 scenario_id：{json.dumps(sorted(target_ids), ensure_ascii=False)}
上次草稿：{json.dumps(previous, ensure_ascii=False, separators=(',', ':'))}
门禁问题：{json.dumps(relevant, ensure_ascii=False, separators=(',', ':'))}

使用接口和已有用例工具核对事实，修正后调用 submit_case_drafts。不要新增或删除 scenario_id。
"""


def dedupe_cases(existing: list[TestCase], generated: list[TestCase]) -> list[TestCase]:
    seen = {case_semantic_key(item): item for item in existing}
    request_seen: dict[str, TestCase] = {}
    result: list[TestCase] = []
    for case in generated:
        key = case_semantic_key(case)
        if key in seen:
            target = seen[key]
            target.rule_ids = unique_strings([*target.rule_ids, *case.rule_ids])
            target.coverage_partition_ids = unique_strings(
                [*target.coverage_partition_ids, *case.coverage_partition_ids]
            )
            target.fixture_requirements = unique_strings(
                [*target.fixture_requirements, *case.fixture_requirements]
            )
            target.tags = unique_strings([*target.tags, *case.tags])
            if case_title_rank(case.title) > case_title_rank(target.title):
                target.title = case.title
            if len(case.expected_result) > len(target.expected_result):
                target.expected_result = case.expected_result
            continue
        request_key = case_request_signature(case)
        request_target = request_seen.get(request_key)
        if (
            request_target is not None
            and assertions_are_compatible(request_target, case)
        ):
            request_target.assertions = merge_assertion_objects(
                request_target.assertions,
                case.assertions,
            )
            request_target.rule_ids = unique_strings(
                [*request_target.rule_ids, *case.rule_ids]
            )
            request_target.coverage_partition_ids = unique_strings(
                [
                    *request_target.coverage_partition_ids,
                    *case.coverage_partition_ids,
                ]
            )
            request_target.fixture_requirements = unique_strings(
                [
                    *request_target.fixture_requirements,
                    *case.fixture_requirements,
                ]
            )
            request_target.tags = unique_strings(
                [*request_target.tags, *case.tags]
            )
            if case_title_rank(case.title) > case_title_rank(request_target.title):
                request_target.title = case.title
            if len(case.expected_result) > len(request_target.expected_result):
                request_target.expected_result = case.expected_result
            continue
        seen[key] = case
        request_seen[request_key] = case
        result.append(case)
    return merge_subsumed_chain_cases(result)


def case_title_rank(value: str) -> tuple[int, int]:
    title = str(value or "").strip()
    score = 0
    if re.search(r"不传|省略|空白|非法|超过|重复|已删除|从未创建|固定域名", title):
        score += 20
    if re.search(r"验证|返回|生成\s*\d+\s*位|随机生成", title):
        score += 5
    if re.search(r"(?:^|[-—：:]\s*)用于", title):
        score -= 30
    if len(title) > 90:
        score -= 5
    return score, -len(title)


def case_semantic_key(case: TestCase) -> str:
    return case_execution_signature(case)


def assertions_are_compatible(first: TestCase, second: TestCase) -> bool:
    assertions = [
        *to_jsonable(first.assertions),
        *to_jsonable(second.assertions),
    ]
    return not assertion_contradictions(assertions)


def merge_assertion_objects(
    first: list,
    second: list,
) -> list:
    result = []
    seen: set[str] = set()
    for assertion in [*first, *second]:
        key = json.dumps(
            to_jsonable(assertion),
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if key not in seen:
            seen.add(key)
            result.append(assertion)
    return remove_redundant_shape_assertions(result)


def issue_messages(issues) -> list[str]:
    return [
        f"{issue.severity}: {issue.scenario_id + ' ' if issue.scenario_id else ''}{issue.message}"
        for issue in issues
    ]


def emit(callback: Callable[[str], None] | None, text: str) -> None:
    if callback:
        callback(text)


def bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def unique_strings(values) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result
