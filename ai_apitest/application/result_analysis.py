from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from ai_apitest.agents.identity import RoleRegistry, default_role_registry
from ai_apitest.ai.model_gateway import ModelGatewayProtocol, ProviderModelGateway
from ai_apitest.ai.result_analysis import case_summary
from ai_apitest.application.agent_service import run_agent_role
from ai_apitest.core.models import CaseResult, Endpoint
from ai_apitest.core.artifact_language import analysis_headings, output_language_instruction
from ai_apitest.core.serde import to_jsonable
from ai_apitest.data.agent_runs import redact_sensitive
from ai_apitest.data.repositories import EndpointRepository
from ai_apitest.tools.builtin import build_case_generation_tool_registry
from ai_apitest.tools.registry import ToolRegistry


GatewayFactory = Callable[[dict[str, Any]], ModelGatewayProtocol]


def analyze_results_with_agent_runtime(
    root: Path,
    project: str,
    results: list[CaseResult],
    llm: dict[str, Any],
    *,
    trend_context: dict[str, Any] | None = None,
    quality_feedback: dict[str, Any] | None = None,
    quality_round: int = 1,
    gateway_factory: GatewayFactory = ProviderModelGateway,
    role_registry: RoleRegistry | None = None,
    tool_registry: ToolRegistry | None = None,
) -> str:
    artifact = generate_result_analysis_artifact_with_agent_runtime(
        root,
        project,
        results,
        llm,
        trend_context=trend_context,
        quality_feedback=quality_feedback,
        quality_round=quality_round,
        gateway_factory=gateway_factory,
        role_registry=role_registry,
        tool_registry=tool_registry,
    )
    return render_analysis_artifact(artifact)


def generate_result_analysis_artifact_with_agent_runtime(
    root: Path,
    project: str,
    results: list[CaseResult],
    llm: dict[str, Any],
    *,
    trend_context: dict[str, Any] | None = None,
    quality_feedback: dict[str, Any] | None = None,
    quality_round: int = 1,
    gateway_factory: GatewayFactory = ProviderModelGateway,
    role_registry: RoleRegistry | None = None,
    tool_registry: ToolRegistry | None = None,
) -> dict[str, Any]:
    roles = role_registry or default_role_registry()
    tools = tool_registry or build_case_generation_tool_registry()
    total = len(results)
    passed = sum(item.status == "passed" for item in results)
    failed_results = [item for item in results if item.status == "failed"]
    skipped_results = [item for item in results if item.status == "skipped"]
    documented_endpoints = documented_endpoints_for_results(root, project, results)
    contract_evidence = documented_contract_evidence(results, documented_endpoints)
    cleanup_by_status: dict[str, list[str]] = {}
    for item in results:
        cleanup_by_status.setdefault(result_cleanup_status(item), []).append(item.case_id)
    payload = {
        "project": project,
        "summary": {
            "total": total,
            "passed": passed,
            "failed": len(failed_results),
            "skipped": sum(item.status == "skipped" for item in results),
            "success_rate": round(passed / total, 4) if total else None,
        },
        "failure_groups": group_failed_results(failed_results),
        "skipped_cases": [skipped_case_summary(item) for item in skipped_results[:100]],
        "cleanup_summary": {
            status: {"count": len(case_ids), "case_ids": case_ids}
            for status, case_ids in sorted(cleanup_by_status.items())
        },
        "slowest_cases": [passed_case_summary(item) for item in sorted(results, key=lambda value: value.elapsed_ms, reverse=True)[:5]],
        "documented_endpoint_contracts": contract_evidence,
        "trend_context": trend_context or {},
    }
    headings = analysis_headings()
    required_headings = "、".join(
        f"### {headings[key]}"
        for key in (
            "one_line_conclusion",
            "result_overview",
            "main_issues",
            "recommended_actions",
            "notes",
        )
    )
    result = run_agent_role(
        root=root,
        project=project,
        endpoints=documented_endpoints,
        role_id="result_analyst",
        task_prompt=(
            output_language_instruction()
            + "\n"
            "分析以下接口自动化执行证据。原始 pass/fail 只能由断言决定，不得被模型覆盖。"
            f"正文使用 Markdown，严格包含：{required_headings}。"
            f"存在失败时还必须包含 ### {headings['failure_review']}，逐类引用代表请求、响应和断言证据。"
            "失败归因必须区分接口疑似问题、测试数据问题、用例预期/断言问题、环境问题和需要人工确认。"
            "归因前必须对照 documented_endpoint_contracts：若断言与可信文档契约一致而实际响应不一致，"
            "应报告为接口实现与文档契约不一致并建议确认实现或文档，不得仅按常见框架格式判定为断言错误。"
            "存在跳过用例时必须使用 skipped_cases 中的明确原因，不得写成原因未知，也不得改写为接口失败。"
            "清理结论必须使用 cleanup_summary；not_configured 表示无需额外清理，不得写成已执行清理并通过。"
            "slowest_cases 中 rate_limit_wait_ms 是框架主动限流等待，不是接口执行耗时；"
            "存在该字段时不得据此判断网络或服务端性能瓶颈。"
            "需要历史事实时调用 search_confirmed_lessons。经验候选只提取项目/接口特有且可复用的事实；"
            "每条经验必须使用 title 和 content 字段，可选 scope、method、path、lesson_type、confidence、tags。"
            "完成后调用 submit_result_analysis，由工具分别接收 markdown、lessons 和 known_issue_updates。\n\n"
            + (
                "上一轮报告或经验质量审计未通过。只修复以下有执行证据的问题，不得改变原始 pass/fail："
                + json.dumps(quality_feedback, ensure_ascii=False, separators=(",", ":"), default=str)
                + "\n\n"
                if quality_feedback
                else ""
            )
            + json.dumps(redact_sensitive(payload), ensure_ascii=False, separators=(",", ":"))
        ),
        model_config=llm,
        roles=roles,
        tools=tools,
        enabled_tools=(
            ("search_confirmed_lessons", "submit_result_analysis")
            if failed_results
            else ("submit_result_analysis",)
        ),
        gateway_factory=gateway_factory,
        metadata={
            "task": "result_analysis",
            "result_count": total,
            "failed_count": len(failed_results),
            "quality_round": quality_round,
        },
    )
    artifact = result.artifact
    if not isinstance(artifact, dict) or not str(artifact.get("markdown") or "").strip():
        raise ValueError("result_analyst 没有提交有效分析")
    return {
        "markdown": str(artifact.get("markdown") or "").strip(),
        "lessons": [item for item in artifact.get("lessons") or [] if isinstance(item, dict)],
        "known_issue_updates": [
            item for item in artifact.get("known_issue_updates") or [] if isinstance(item, dict)
        ],
        "_agent_run_id": result.run_id,
    }


def review_failed_results_with_agent_runtime(
    root: Path,
    project: str,
    results: list[CaseResult],
    llm: dict[str, Any],
    *,
    gateway_factory: GatewayFactory = ProviderModelGateway,
    role_registry: RoleRegistry | None = None,
    tool_registry: ToolRegistry | None = None,
) -> str:
    failures = [item for item in results if item.status == "failed"]
    if not failures:
        return ""
    roles = role_registry or default_role_registry()
    tools = tool_registry or build_case_generation_tool_registry()
    result = run_agent_role(
        root=root,
        project=project,
        endpoints=documented_endpoints_for_results(root, project, failures),
        role_id="result_analyst",
        task_prompt=(
            output_language_instruction()
            + "\n"
            "复核以下失败用例。复核只能辅助归因，不能改变原始失败状态。"
            "输出 Markdown，包含 ### 复核说明、### 复核总览、### 重点说明。"
            "结论和建议必须引用请求、响应或断言证据。完成后调用 submit_result_review。\n\n"
            + json.dumps(
                redact_sensitive([compact_case_summary(item) for item in failures]),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        ),
        model_config=llm,
        roles=roles,
        tools=tools,
        enabled_tools=("search_confirmed_lessons", "submit_result_review"),
        gateway_factory=gateway_factory,
        metadata={"task": "failure_review", "failed_count": len(failures)},
    )
    artifact = result.artifact
    if not isinstance(artifact, dict) or not str(artifact.get("markdown") or "").strip():
        raise ValueError("result_analyst 没有提交有效失败复核")
    return str(artifact["markdown"]).strip()


def render_analysis_artifact(artifact: dict[str, Any]) -> str:
    markdown = str(artifact.get("markdown") or "").strip()
    lessons = [item for item in artifact.get("lessons") or [] if isinstance(item, dict)]
    known_updates = [item for item in artifact.get("known_issue_updates") or [] if isinstance(item, dict)]
    return (
        markdown
        + "\n\n```iatest-lessons-json\n"
        + json.dumps(lessons, ensure_ascii=False, indent=2)
        + "\n```\n\n```iatest-known-issues-json\n"
        + json.dumps(known_updates, ensure_ascii=False, indent=2)
        + "\n```"
    )


def compact_case_summary(result: CaseResult) -> dict[str, Any]:
    return compact_value(case_summary(result), depth=0)


def passed_case_summary(result: CaseResult) -> dict[str, Any]:
    """Pass evidence needs metrics, not full request/response payloads."""
    summary = {
        "case_id": result.case_id,
        "title": result.title,
        "method": result.method,
        "path": result.path,
        "status": result.status,
        "status_code": result.status_code,
        "elapsed_ms": result.elapsed_ms,
        "cleanup_status": result.cleanup.get("status", "not_configured") if isinstance(result.cleanup, dict) else "",
    }
    wait_ms = result_rate_limit_wait_ms(result)
    if wait_ms:
        summary["rate_limit_wait_ms"] = wait_ms
        summary["active_elapsed_ms"] = max(0, result.elapsed_ms - wait_ms)
    return summary


def skipped_case_summary(result: CaseResult) -> dict[str, Any]:
    return {
        "case_id": result.case_id,
        "title": result.title,
        "method": result.method,
        "path": result.path,
        "status": result.status,
        "reason": result.error,
        "case_risk": result.case_risk,
        "design_method": result.design_method,
        "expected_result": result.expected_result,
    }


def result_cleanup_status(result: CaseResult) -> str:
    if not isinstance(result.cleanup, dict) or not result.cleanup:
        return "not_configured"
    return str(result.cleanup.get("status") or "not_configured")


def group_failed_results(results: list[CaseResult], limit: int = 50) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for result in results:
        raw = "|".join([result.method, result.path, result.error, *result.assertion_errors])
        signature = re.sub(r"\b[0-9a-f]{8,}\b|\b\d{4,}\b", "<dynamic>", raw.lower())[:1000]
        item = groups.setdefault(
            signature,
            {
                "signature": signature,
                "count": 0,
                "case_ids": [],
                "representative": compact_case_summary(result),
            },
        )
        item["count"] += 1
        if len(item["case_ids"]) < 20:
            item["case_ids"].append(result.case_id)
    return sorted(groups.values(), key=lambda item: (-int(item["count"]), str(item["signature"])))[:limit]


def compact_value(value: Any, *, depth: int) -> Any:
    if depth >= 5:
        return str(value)[:500]
    if isinstance(value, str):
        return value[:3000]
    if isinstance(value, dict):
        return {str(key): compact_value(item, depth=depth + 1) for key, item in list(value.items())[:100]}
    if isinstance(value, list):
        return [compact_value(item, depth=depth + 1) for item in value[:100]]
    return value


def endpoints_from_results(results: list[CaseResult]) -> list[Endpoint]:
    result: list[Endpoint] = []
    seen: set[tuple[str, str]] = set()
    for item in results:
        key = (item.method.upper(), item.path)
        if item.method == "CHAIN" or key in seen:
            continue
        seen.add(key)
        result.append(Endpoint(method=key[0], path=key[1], summary=item.title))
    return result


def documented_endpoints_for_results(
    root: Path,
    project: str,
    results: list[CaseResult],
) -> list[Endpoint]:
    """Return trusted persisted contracts for the endpoints present in results."""
    result_keys = {
        (item.method.upper(), item.path)
        for item in results
        if item.method.upper() != "CHAIN"
    }
    try:
        documented = EndpointRepository(root, project).all()
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        documented = []
    selected = [
        endpoint
        for endpoint in documented
        if (endpoint.method.upper(), endpoint.path) in result_keys
    ]
    return selected or endpoints_from_results(results)


def documented_contract_evidence(
    results: list[CaseResult],
    endpoints: list[Endpoint],
) -> list[dict[str, Any]]:
    """Keep only failed-response contracts and their matching source rules."""
    failures_by_key: dict[tuple[str, str], list[CaseResult]] = {}
    for result in results:
        if result.status == "failed" and result.method.upper() != "CHAIN":
            failures_by_key.setdefault((result.method.upper(), result.path), []).append(result)
    evidence: list[dict[str, Any]] = []
    for endpoint in endpoints:
        failures = failures_by_key.get((endpoint.method.upper(), endpoint.path), [])
        if not failures:
            continue
        statuses = {str(result.status_code) for result in failures if result.status_code is not None}
        rules = [
            rule
            for rule in endpoint.business_rules
            if isinstance(rule, dict)
            and str(rule.get("title") or rule.get("description") or "").strip()
            and any(
                str(rule.get("title") or rule.get("description") or "").strip() in result.title
                for result in failures
            )
        ]
        evidence.append(
            {
                "method": endpoint.method,
                "path": endpoint.path,
                "responses": {
                    status: endpoint.responses[status]
                    for status in sorted(statuses)
                    if status in endpoint.responses
                },
                "business_rules": to_jsonable(rules),
            }
        )
    return evidence


def result_rate_limit_wait_ms(result: CaseResult) -> int:
    request = result.request if isinstance(result.request, dict) else {}
    steps = request.get("steps")
    request_items = steps if isinstance(steps, list) else [request]
    total = 0
    for item in request_items:
        if not isinstance(item, dict):
            continue
        try:
            total += max(0, int(item.get("rate_limit_wait_ms") or 0))
        except (TypeError, ValueError):
            continue
    return total
