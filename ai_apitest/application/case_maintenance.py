from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ai_apitest.agents.identity import RoleRegistry, default_role_registry
from ai_apitest.ai.model_gateway import ModelGatewayProtocol, ProviderModelGateway
from ai_apitest.application.agent_service import run_agent_role
from ai_apitest.core.models import Endpoint, TestCase
from ai_apitest.core.artifact_language import output_language_instruction
from ai_apitest.domain.case_drafts import CompilationResult, DraftIssue, compile_scenario_drafts
from ai_apitest.tools.builtin import build_case_generation_tool_registry
from ai_apitest.tools.registry import ToolRegistry


GatewayFactory = Callable[[dict[str, Any]], ModelGatewayProtocol]


@dataclass(slots=True)
class MaintenanceResult:
    updated: list[TestCase]
    retired_case_ids: set[str]
    decisions: list[dict[str, Any]]
    warning: str = ""


def maintain_cases_with_agent_runtime(
    root: Path,
    project: str,
    *,
    impacted: list[TestCase],
    endpoints: list[Endpoint],
    change_context: dict[str, Any],
    llm: dict[str, Any],
    allow_retire: bool = True,
    repair_attempts: int = 2,
    gateway_factory: GatewayFactory = ProviderModelGateway,
    role_registry: RoleRegistry | None = None,
    tool_registry: ToolRegistry | None = None,
) -> MaintenanceResult:
    if not impacted:
        return MaintenanceResult([], set(), [])
    roles = role_registry or default_role_registry()
    tools = tool_registry or build_case_generation_tool_registry()
    targets = maintenance_targets(impacted)
    task = maintenance_prompt(targets, change_context, allow_retire)
    result = run_agent_role(
        root=root,
        project=project,
        endpoints=endpoints,
        cases=impacted,
        role_id="case_maintainer",
        task_prompt=task,
        model_config=llm,
        roles=roles,
        tools=tools,
        gateway_factory=gateway_factory,
        metadata={"task": "case_maintenance", "target_count": len(targets), "allow_retire": allow_retire},
    )
    decisions = object_list(result.artifact, "case_maintainer 没有提交有效维护决策")
    validation = validate_maintenance_decisions(decisions, targets, endpoints, allow_retire=allow_retire)
    previous_issues = tuple(validation.issues)

    for attempt in range(1, max(0, min(2, int(repair_attempts))) + 1):
        if not validation.issues:
            break
        repair = run_agent_role(
            root=root,
            project=project,
            endpoints=endpoints,
            cases=impacted,
            role_id="case_maintainer",
            task_prompt=maintenance_repair_prompt(targets, change_context, decisions, validation.issues, allow_retire),
            model_config=llm,
            roles=roles,
            tools=tools,
            gateway_factory=gateway_factory,
            metadata={"task": "case_maintenance_repair", "attempt": attempt, "target_count": len(targets)},
        )
        decisions = object_list(repair.artifact, "case_maintainer 定向修订没有提交有效维护决策")
        validation = validate_maintenance_decisions(decisions, targets, endpoints, allow_retire=allow_retire)
        current_issues = tuple(validation.issues)
        if current_issues and current_issues == previous_issues:
            break
        previous_issues = current_issues

    if validation.issues:
        detail = "；".join(validation.issues[:10])
        raise ValueError(f"用例维护决策未通过门禁：{detail}")

    target_by_scenario = {item["maintenance_scenario_id"]: item for item in targets}
    updated: list[TestCase] = []
    for case in validation.compilation.cases:
        target = target_by_scenario[case.scenario_id]
        original = target["case"]
        case.id = original.id
        case.scenario_id = original.scenario_id or case.scenario_id
        case.rule_ids = unique_strings([*original.rule_ids, *case.rule_ids])
        case.coverage_partition_ids = unique_strings(
            [*original.coverage_partition_ids, *case.coverage_partition_ids]
        )
        case.fixture_requirements = unique_strings(
            [*original.fixture_requirements, *case.fixture_requirements]
        )
        case.tags = unique_strings([*original.tags, *case.tags])
        if case.fixture_requirements and not case.runnable:
            case.risk = case.risk or original.risk or "fixture_required"
        updated.append(case)
    retired = {
        str(item["target_case_id"])
        for item in decisions
        if str(item.get("action") or "") == "retire"
    }
    return MaintenanceResult(
        updated=updated,
        retired_case_ids=retired,
        decisions=decisions,
        warning=f"维护门禁通过：更新 {len(updated)} 条，退役 {len(retired)} 条",
    )


@dataclass(slots=True)
class MaintenanceValidation:
    compilation: CompilationResult
    issues: list[str]


def validate_maintenance_decisions(
    decisions: list[dict[str, Any]],
    targets: list[dict[str, Any]],
    endpoints: list[Endpoint],
    *,
    allow_retire: bool,
) -> MaintenanceValidation:
    target_by_id = {item["case_id"]: item for item in targets}
    endpoint_keys = {(item.method.upper(), item.path) for item in endpoints}
    seen: set[str] = set()
    drafts: list[dict[str, Any]] = []
    expected_scenarios: list[str] = []
    issues: list[str] = []
    for decision in decisions:
        case_id = str(decision.get("target_case_id") or "").strip()
        target = target_by_id.get(case_id)
        if target is None:
            issues.append(f"维护决策引用任务外用例：{case_id or '-'}")
            continue
        if case_id in seen:
            issues.append(f"用例存在重复维护决策：{case_id}")
            continue
        seen.add(case_id)
        action = str(decision.get("action") or "")
        if action == "retire":
            original = target["case"]
            if not allow_retire:
                issues.append(f"当前任务不允许退役用例：{case_id}")
            elif (original.method.upper(), original.path) in endpoint_keys:
                issues.append(f"接口仍存在，不能退役用例：{case_id}")
            continue
        if action != "update":
            issues.append(f"用例维护动作无效：{case_id} {action or '-'}")
            continue
        draft = decision.get("draft")
        if not isinstance(draft, dict):
            issues.append(f"update 决策缺少 draft：{case_id}")
            continue
        expected = target["maintenance_scenario_id"]
        if str(draft.get("scenario_id") or "") != expected:
            issues.append(f"维护草稿 scenario_id 不匹配：{case_id} 应为 {expected}")
            continue
        drafts.append(dict(draft))
        expected_scenarios.append(expected)
    for case_id in sorted(set(target_by_id) - seen):
        issues.append(f"目标用例缺少维护决策：{case_id}")

    compilation = compile_scenario_drafts(drafts, endpoints, expected_scenario_ids=expected_scenarios)
    issues.extend(format_draft_issue(item) for item in compilation.error_issues)
    return MaintenanceValidation(compilation, unique_strings(issues))


def maintenance_targets(cases: list[TestCase]) -> list[dict[str, Any]]:
    return [
        {
            "case_id": case.id,
            "maintenance_scenario_id": f"SCN-MAINT-{index:03d}",
            "method": case.method,
            "path": case.path,
            "title": case.title,
            "design_method": case.design_method,
            "rule_ids": list(case.rule_ids),
            "coverage_partition_ids": list(case.coverage_partition_ids),
            "fixture_requirements": list(case.fixture_requirements),
            "case": case,
        }
        for index, case in enumerate(cases, start=1)
    ]


def maintenance_prompt(targets: list[dict[str, Any]], change_context: dict[str, Any], allow_retire: bool) -> str:
    public_targets = [{key: value for key, value in item.items() if key != "case"} for item in targets]
    retire_rule = (
        "只有原接口已从当前接口目录删除时才允许 retire。"
        if allow_retire
        else "本任务不允许 retire，所有目标都必须提交 update。"
    )
    return f"""{output_language_instruction()}

维护项目中的指定测试用例。

目标映射：{json.dumps(public_targets, ensure_ascii=False, separators=(',', ':'))}
变更或质量问题：{json.dumps(change_context, ensure_ascii=False, separators=(',', ':'), default=str)}

逐项调用 get_existing_case_detail 读取目标原始内容，并调用 get_endpoint_detail 核对当前接口。
{retire_rule}
每个目标 case_id 恰好提交一个决策；update.draft 的 scenario_id 必须使用目标映射中的 maintenance_scenario_id。
目标携带 rule_ids 时，逐条调用 get_business_rule_detail 核对触发条件和断言义务，draft 必须保留这些 rule_ids。
只修改受变更或问题影响的语义，完成后调用 submit_case_maintenance。
"""


def maintenance_repair_prompt(
    targets: list[dict[str, Any]],
    change_context: dict[str, Any],
    previous: list[dict[str, Any]],
    issues: list[str],
    allow_retire: bool,
) -> str:
    public_targets = [{key: value for key, value in item.items() if key != "case"} for item in targets]
    return f"""{output_language_instruction()}

定向修订用例维护决策。必须重新提交所有目标的完整决策列表。

目标：{json.dumps(public_targets, ensure_ascii=False, separators=(',', ':'))}
任务上下文：{json.dumps(change_context, ensure_ascii=False, separators=(',', ':'), default=str)}
上次决策：{json.dumps(previous, ensure_ascii=False, separators=(',', ':'), default=str)}
门禁问题：{json.dumps(issues, ensure_ascii=False)}
允许退役：{str(allow_retire).lower()}

读取必要的用例和接口详情，修正后调用 submit_case_maintenance。
"""


def object_list(value: Any, message: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(message)
    result = [dict(item) for item in value if isinstance(item, dict)]
    if not result:
        raise ValueError(message)
    return result


def format_draft_issue(issue: DraftIssue) -> str:
    return f"{issue.scenario_id or '-'} {issue.message}"


def unique_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result
