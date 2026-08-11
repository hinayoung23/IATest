from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from ai_apitest.agents.identity import RoleRegistry, default_role_registry
from ai_apitest.ai.model_gateway import ModelGatewayProtocol, ProviderModelGateway
from ai_apitest.application.agent_service import run_agent_role
from ai_apitest.application.result_analysis import compact_value
from ai_apitest.core.models import Endpoint
from ai_apitest.tools.builtin import build_case_generation_tool_registry
from ai_apitest.tools.registry import ToolRegistry


GatewayFactory = Callable[[dict[str, Any]], ModelGatewayProtocol]


def review_quality_with_agent_runtime(
    root: Path,
    project: str,
    *,
    review_type: str,
    evidence: dict[str, Any],
    endpoints: list[Endpoint],
    llm: dict[str, Any],
    rubric: str = "",
    calibration_feedback: str = "",
    state: dict[str, Any] | None = None,
    extra_tools: tuple[str, ...] = (),
    gateway_factory: GatewayFactory = ProviderModelGateway,
    role_registry: RoleRegistry | None = None,
    tool_registry: ToolRegistry | None = None,
) -> dict[str, Any]:
    roles = role_registry or default_role_registry()
    tools = tool_registry or build_case_generation_tool_registry()
    result = run_agent_role(
        root=root,
        project=project,
        endpoints=endpoints,
        role_id="quality_reviewer",
        task_prompt=(
            f"执行 {review_type} 独立质量审计。先调用 get_audit_artifact_info，再连续调用 "
            "read_audit_artifact_chunk，直到 has_more=false。不要推测产出模型身份。"
            "证据包已包含完成审计所需的全部事实，读完后不要尝试其它证据工具，必须直接提交裁决。"
            "从 reliability、completeness、reasonableness、non_redundancy 四个维度分别给出 0-100 分。"
            "issues 中每个扣分项必须包含 severity、dimension、location、evidence、reason、suggested_fix。"
            "相同根因只保留一个代表问题并列出受影响位置，issues 最多 8 条；"
            "每个文本字段不超过 300 个汉字，证据和建议应简洁，不要逐字段复述整份产物。"
            "没有问题时 verdict=approved；存在可修复问题时 needs_revision；存在无法安全交付的问题时 rejected。"
            + (
                "\n上一份裁决因违反审计规则被拒绝，必须纠正后重新评分："
                + calibration_feedback[:4000]
                if calibration_feedback
                else ""
            )
            + "完成后调用 submit_quality_review。\n\n"
            + f"审计规则：{rubric or '严格依据证据评价产物，不评价表达风格。'}\n\n"
            + "证据包摘要："
            + json.dumps(compact_value(evidence, depth=0), ensure_ascii=False, separators=(",", ":"))
        ),
        model_config=llm,
        roles=roles,
        tools=tools,
        enabled_tools=(
            "get_audit_artifact_info",
            "read_audit_artifact_chunk",
            "submit_quality_review",
            *extra_tools,
        ),
        gateway_factory=gateway_factory,
        metadata={"task": "quality_review", "review_type": review_type},
        state=state,
    )
    artifact = result.artifact
    dimensions = artifact.get("dimension_scores") if isinstance(artifact, dict) else None
    if (
        not isinstance(artifact, dict)
        or artifact.get("verdict") not in {"approved", "needs_revision", "rejected"}
        or not isinstance(dimensions, dict)
        or any(name not in dimensions for name in (
            "reliability",
            "completeness",
            "reasonableness",
            "non_redundancy",
        ))
    ):
        raise ValueError("quality_reviewer 没有提交有效裁决")
    return {**artifact, "_agent_run_id": result.run_id}


def govern_context_candidates_with_agent_runtime(
    root: Path,
    project: str,
    *,
    candidates: list[dict[str, Any]],
    existing: list[dict[str, Any]],
    llm: dict[str, Any],
    gateway_factory: GatewayFactory = ProviderModelGateway,
    role_registry: RoleRegistry | None = None,
    tool_registry: ToolRegistry | None = None,
) -> list[dict[str, Any]]:
    if not candidates:
        return []
    roles = role_registry or default_role_registry()
    tools = tool_registry or build_case_generation_tool_registry()
    result = run_agent_role(
        root=root,
        project=project,
        endpoints=[],
        role_id="context_curator",
        task_prompt=(
            "治理以下经验候选。逐项判断 create、merge、reject、needs_review 或 conflict。"
            "只有语义相同且目标真实存在时才能 merge；证据不足或低置信度时 needs_review。"
            "可调用 search_confirmed_lessons 检索更多已确认经验。完成后调用 submit_context_decisions。\n\n"
            + json.dumps(
                {"candidates": candidates, "existing_related": existing},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        ),
        model_config=llm,
        roles=roles,
        tools=tools,
        enabled_tools=("search_confirmed_lessons", "submit_context_decisions"),
        gateway_factory=gateway_factory,
        metadata={"task": "context_governance", "candidate_count": len(candidates)},
    )
    artifact = result.artifact
    decisions = artifact.get("decisions") if isinstance(artifact, dict) else None
    if not isinstance(decisions, list):
        raise ValueError("context_curator 没有提交有效治理决策")
    candidate_ids = {str(item.get("id") or "") for item in candidates}
    existing_ids = {str(item.get("id") or "") for item in existing}
    normalized = []
    for item in decisions:
        if not isinstance(item, dict) or str(item.get("candidate_id") or "") not in candidate_ids:
            continue
        decision = str(item.get("decision") or "needs_review")
        target = str(item.get("target_lesson_id") or "")
        if decision in {"merge", "conflict"} and target not in existing_ids:
            decision = "needs_review"
            target = ""
        normalized.append({**item, "decision": decision, "target_lesson_id": target or None})
    return normalized


def review_conflicts_with_agent_runtime(
    root: Path,
    project: str,
    *,
    conflicts: list[dict[str, Any]],
    llm: dict[str, Any],
    gateway_factory: GatewayFactory = ProviderModelGateway,
    role_registry: RoleRegistry | None = None,
    tool_registry: ToolRegistry | None = None,
) -> list[dict[str, Any]]:
    if not conflicts:
        return []
    roles = role_registry or default_role_registry()
    tools = tool_registry or build_case_generation_tool_registry()
    result = run_agent_role(
        root=root,
        project=project,
        endpoints=[],
        role_id="context_curator",
        task_prompt=(
            "复核以下规则引擎识别的疑似经验冲突。语义一致只是表达不同则 not_conflict；"
            "状态码、布尔结论、存在性或断言预期互斥才是 conflict；证据不足为 uncertain。"
            "完成后调用 submit_conflict_reviews。\n\n"
            + json.dumps(conflicts, ensure_ascii=False, separators=(",", ":"))
        ),
        model_config=llm,
        roles=roles,
        tools=tools,
        enabled_tools=("submit_conflict_reviews",),
        gateway_factory=gateway_factory,
        metadata={"task": "conflict_review", "conflict_count": len(conflicts)},
    )
    reviews = result.artifact
    if not isinstance(reviews, list):
        raise ValueError("context_curator 没有提交有效冲突复核")
    relation_ids = {int(item["relation_id"]) for item in conflicts if item.get("relation_id") is not None}
    return [
        {
            "relation_id": int(item["relation_id"]),
            "decision": str(item.get("decision") or "uncertain"),
            "reason": str(item.get("reason") or ""),
        }
        for item in reviews
        if isinstance(item, dict)
        and str(item.get("relation_id") or "").isdigit()
        and int(item["relation_id"]) in relation_ids
    ]
