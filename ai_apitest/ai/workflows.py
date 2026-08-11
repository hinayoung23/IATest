from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
import hashlib
import json

from ai_apitest.ai.api_discovery import discover_endpoints_with_llm
from ai_apitest.ai.case_generation import generate_cases_with_llm, generate_supplemental_cases_with_llm, generate_coverage_plan, generate_cases_from_plan_batch
from ai_apitest.ai.conflict_review import review_conflicts_with_llm
from ai_apitest.ai.llm_config import is_llm_configured, normalize_llm_config, resolve_llm_role
from ai_apitest.ai.provider import DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS
from ai_apitest.ai.result_analysis import analyze_results_with_llm, review_failed_results_with_llm
from ai_apitest.context.extraction import process_report_analysis
from ai_apitest.context.injection import build_context_bundle
from ai_apitest.context.storage import apply_conflict_review, audit, conflict_pairs, context_connection, merge_lesson_into, now_iso, record_case_generation_lessons
from ai_apitest.core.artifact_language import (
    analysis_headings,
    configured_artifact_language,
    detect_analysis_language,
    localize_generated_cases,
    output_language_instruction,
    set_current_artifact_language,
)
from ai_apitest.core.paths import project_docs_dir, safe_project_name
from ai_apitest.core.config import load_config
from ai_apitest.core.observability import record_operational_error
from ai_apitest.core.models import CaseResult, Endpoint, TestCase
from ai_apitest.generator.cases import attach_cleanup_steps, attach_setup_steps, generate_cases


GENERATION_PIPELINE_VERSION = "2026-08-11.1"


DEFAULT_STRATEGY_DESCRIPTIONS = {
    "happy_path": "合法参数、正常返回的主流程用例；所有接口至少生成 1 条。",
    "optional_field": "覆盖可选参数缺省、只传必填参数、可选参数不同组合。",
    "type_error": "覆盖 query/path/body 字段类型错误、格式错误。",
    "boundary_value": "覆盖字符串长度、数值范围、数组长度、空字符串等边界值。",
    "path_parameter_error": "覆盖路径参数为空、非法格式、不存在资源等异常。",
    "dependency_chain": "识别接口依赖关系，生成先创建/生成数据再查询、更新、删除的链路用例。",
    "auth_error": "覆盖缺少鉴权、错误 Token、过期 Token 等鉴权异常。",
    "equivalence_class": "按有效/无效等价类选择代表数据，减少重复但覆盖关键输入类别。",
    "status_code_coverage": "只覆盖接口文档明确声明的响应状态码；没有声明的 4xx/5xx 不要猜测补充。",
}

def configured_llm(root: Path, role: str) -> dict:
    return resolve_llm_role(normalize_llm_config(load_config(root).get("llm", {})), role)


def case_generation_strategy_context(root: Path, endpoints: list[Endpoint] | None = None) -> dict:
    generation = load_config(root).get("case_generation", {})
    default_flags = generation.get("default_strategies", {})
    enabled_defaults = []
    for name, description in DEFAULT_STRATEGY_DESCRIPTIONS.items():
        if default_flags.get(name, True):
            enabled_defaults.append({"name": name, "description": description})
    custom = []
    for strategy in generation.get("custom_strategies", []):
        if not strategy.get("enabled", True):
            continue
        name = str(strategy.get("name", "")).strip()
        if name:
            custom.append({"name": name, "description": str(strategy.get("description", "")).strip()})
    return {
        "enabled_default_strategies": enabled_defaults,
        "custom_strategies": custom,
        "strategy_applicability": strategy_applicability(endpoints or []),
        "generation_requirements": [
            "按启用策略尽可能全面生成用例，不要只生成 happy_path。",
            "启用表示允许在适用接口上生成，并不表示每个接口都必须机械生成；以 strategy_applicability 的 Schema 判定为准。",
            "无法安全执行的破坏性、依赖外部状态或明显缺少前置数据的负向用例，runnable 应设为 false，并说明 risk。",
            "存在接口依赖时优先使用 steps 生成链路用例，例如先生成邮箱再查询、续期或删除。",
        ],
    }


def strategy_applicability(endpoints: list[Endpoint]) -> dict[str, dict[str, object]]:
    if not endpoints:
        return {}
    from ai_apitest.domain.coverage import (
        first_response_basis,
        has_auth,
        has_constraints,
        has_distinct_equivalence_input,
        has_path_parameter,
        has_typed_input,
        numeric_statuses,
        optional_body_fields,
    )

    def matching(predicate) -> list[str]:
        return [
            f"{endpoint.method.upper()} {endpoint.path}"
            for endpoint in endpoints
            if predicate(endpoint)
        ]

    def has_rule_strategy(endpoint: Endpoint, *strategies: str) -> bool:
        accepted = set(strategies)
        return any(
            isinstance(rule, dict)
            and rule.get("testable", True)
            and str(rule.get("strategy") or "") in accepted
            for rule in endpoint.business_rules
        )

    optional = matching(
        lambda endpoint: has_rule_strategy(endpoint, "optional_field") or any(
            item.get("in") == "query" and not item.get("required")
            for item in endpoint.parameters
        ) or bool(optional_body_fields(endpoint))
    )
    typed = matching(
        lambda endpoint: (
            has_rule_strategy(endpoint, "type_error")
            or has_typed_input(endpoint)
        )
        and bool(first_response_basis(endpoint, {400, 415, 422}))
    )
    boundary = matching(
        lambda endpoint: (
            has_rule_strategy(endpoint, "boundary_value")
            or has_constraints(endpoint)
        )
        and bool(first_response_basis(endpoint, {400, 416, 422}))
    )
    path_error = matching(
        lambda endpoint: has_path_parameter(endpoint)
        and bool(first_response_basis(endpoint, {400, 404, 422}))
    )
    auth = matching(
        lambda endpoint: has_auth(endpoint)
        and bool(first_response_basis(endpoint, {401, 403}))
    )
    equivalence = matching(
        lambda endpoint: has_rule_strategy(endpoint, "equivalence_class")
        or has_distinct_equivalence_input(endpoint)
    )
    status_contracts = [
        f"{endpoint.method.upper()} {endpoint.path} responses.{status}"
        for endpoint in endpoints
        for status in numeric_statuses(endpoint)
        if status >= 400
    ]

    def item(matches: list[str], unavailable_reason: str) -> dict[str, object]:
        return {
            "applicable": bool(matches),
            "matching_count": len(matches),
            "matches": matches,
            "unavailable_reason": "" if matches else unavailable_reason,
        }

    return {
        "happy_path": item(
            [f"{endpoint.method.upper()} {endpoint.path}" for endpoint in endpoints],
            "",
        ),
        "optional_field": item(optional, "接口集没有可选 Query 或请求体字段。"),
        "type_error": item(
            typed,
            "HTTP Path/Query/Header 的 string 在线上传输仍是字符串，且请求体没有可验证的非字符串类型错误或对应文档错误响应。",
        ),
        "boundary_value": item(boundary, "接口 Schema 未声明长度、范围、枚举或数组边界及对应错误响应。"),
        "path_parameter_error": item(path_error, "路径参数接口未声明可匹配的 400/404/422 响应。"),
        "auth_error": item(auth, "接口未声明鉴权要求及 401/403 响应。"),
        "equivalence_class": item(equivalence, "接口 Schema 没有多值 enum 或 boolean 等可区分等价类。"),
        "status_code_coverage": item(status_contracts, "接口文档没有声明 4xx/5xx 响应。"),
        "dependency_chain": {
            "applicable": bool(
                chain_matches := matching(
                    lambda endpoint: has_rule_strategy(endpoint, "dependency_chain")
                )
            ) or "由项目依赖图判定",
            "matching_count": len(chain_matches),
            "matches": chain_matches,
            "unavailable_reason": "",
        },
        "business_rule_coverage": {
            "applicable": any(endpoint.business_rules for endpoint in endpoints),
            "matching_count": sum(
                1
                for endpoint in endpoints
                for rule in endpoint.business_rules
                if isinstance(rule, dict) and rule.get("testable", True)
            ),
            "matches": [
                str(rule.get("rule_id"))
                for endpoint in endpoints
                for rule in endpoint.business_rules
                if isinstance(rule, dict) and rule.get("testable", True) and rule.get("rule_id")
            ],
            "unavailable_reason": "",
        },
    }


def apply_ai_discovery_if_configured(
    root: Path,
    text: str,
    endpoints: list[Endpoint],
    metadata: dict,
    stream_callback: Callable[[str], None] | None = None,
    project: str = "default",
) -> tuple[list[Endpoint], dict]:
    llm = configured_llm(root, "master")
    if not is_llm_configured(llm):
        return endpoints, metadata
    config = load_config(root)
    discovery_mode = str(config.get("agents", {}).get("api_discovery_mode") or "agent").strip().lower()
    from ai_apitest.application.quality_audit import QualityAuditService
    from ai_apitest.core.serde import endpoint_from_dict, to_jsonable

    deterministic_endpoints = list(endpoints)
    deterministic_keys = {(item.method.upper(), item.path) for item in deterministic_endpoints}
    audit_service = QualityAuditService(
        root,
        project,
        "endpoint_catalog",
        endpoints=deterministic_endpoints,
        metadata={"source_type": metadata.get("source_type", ""), "document_chars": len(text)},
    )
    quality_feedback: dict | None = (
        metadata.pop("upstream_repair_feedback", None)
        if isinstance(metadata.get("upstream_repair_feedback"), dict)
        else None
    )
    audit_retry_candidate: list[Endpoint] | None = None
    selected_round = None
    round_summaries: list[dict] = []
    for round_index in range(1, audit_service.policy.max_rounds + 1):
        audit_retry_only = audit_retry_candidate is not None
        if audit_retry_candidate is not None:
            candidate = audit_retry_candidate
        else:
            ai_endpoints: list[Endpoint] = []
            try:
                if metadata.get("source_type") == "openapi" and deterministic_endpoints and round_index == 1:
                    ai_endpoints = deterministic_endpoints
                    metadata["ai_discovery_skipped"] = (
                        "OpenAPI 首轮由确定性解析器完整解析；低于审计阈值时再由接口识别模型修订"
                    )
                elif discovery_mode == "agent":
                    from ai_apitest.application.discovery import discover_endpoints_with_agent_runtime

                    ai_endpoints = discover_endpoints_with_agent_runtime(
                        root,
                        project,
                        text,
                        llm,
                        expected_endpoints=deterministic_endpoints,
                        quality_feedback=quality_feedback,
                        quality_round=round_index,
                        stream_callback=stream_callback,
                    )
                elif discovery_mode == "legacy":
                    ai_endpoints = discover_endpoints_with_llm(
                        text,
                        llm,
                        stream_callback=stream_callback,
                        quality_feedback=quality_feedback,
                    )
                else:
                    raise ValueError(f"不支持的接口识别模式：{discovery_mode}")
            except Exception as exc:
                metadata["ai_discovery_error"] = str(exc)
                record_operational_error(
                    root,
                    project,
                    component="ai_discovery",
                    operation="discover_endpoints",
                    error=exc,
                    detail={"quality_round": round_index},
                )
            candidate = merge_endpoint_candidates(deterministic_endpoints, ai_endpoints)
        gate_issues = endpoint_catalog_gate_issues(candidate, deterministic_endpoints)
        audit_round = audit_service.evaluate(
            to_jsonable(candidate),
            evidence={
                "document_text": text,
                "deterministic_gate_summary": {
                    "endpoint_keys": [
                        {"method": item.method.upper(), "path": item.path}
                        for item in deterministic_endpoints
                    ],
                    "hard_gate_issue_count": len(gate_issues),
                    "note": "确定性解析结果已由本地硬门禁逐字段核对，不在审计包中重复发送整份接口产物。",
                },
                "source_metadata": {
                    "source_type": metadata.get("source_type", ""),
                    "source": metadata.get("source", ""),
                    "base_url": metadata.get("base_url", ""),
                },
            },
            round_index=round_index,
            hard_gate_issues=gate_issues,
            producer_model_key=model_config_key(llm),
            stream_callback=stream_callback,
        )
        round_summaries.append(
            {
                "round": round_index,
                "score": audit_round.average_score,
                "passed": audit_round.passed,
                "candidate_id": audit_round.candidate_id,
                "audit_retry_only": audit_retry_only,
            }
        )
        if audit_round.passed:
            selected_round = audit_round
            break
        if audit_round.hard_gate_pass and not audit_round.audit_complete:
            audit_retry_candidate = candidate
            quality_feedback = None
        else:
            audit_retry_candidate = None
            quality_feedback = audit_round.repair_feedback()
    if selected_round is None:
        selected_round = audit_service.best_safe_round()
    if selected_round is None:
        merged_endpoints = deterministic_endpoints
        audit_service.finalize(
            None,
            decision="deterministic_fallback",
            reason="三轮审计后没有安全候选，交付确定性接口解析基线。",
        )
        metadata["quality_audit_fallback"] = "deterministic_parser"
    else:
        merged_endpoints = [
            endpoint_from_dict(item)
            for item in selected_round.artifact
            if isinstance(item, dict)
        ]
        decision = "accepted" if selected_round.passed else "highest_safe_candidate"
        audit_service.finalize(
            selected_round,
            decision=decision,
            reason=(
                "接口信息质量审计通过。"
                if selected_round.passed
                else "三轮未达阈值，交付通过硬门禁且无 critical 问题的最高分候选。"
            ),
        )
    ai_keys = {(item.method.upper(), item.path) for item in merged_endpoints}
    missing_ai_keys = sorted(deterministic_keys - ai_keys)
    metadata["quality_audit_session_id"] = audit_service.session_id
    metadata["quality_audit_rounds"] = round_summaries
    metadata["quality_audit_score"] = selected_round.average_score if selected_round else None
    metadata["source_type"] = f"{metadata.get('source_type', 'document')}+ai"
    metadata["ai_discovery"] = True
    metadata["deterministic_endpoint_count"] = len(deterministic_keys)
    metadata["ai_endpoint_count"] = len(ai_keys)
    from ai_apitest.application.discovery import normalize_document_for_agent
    from ai_apitest.domain.contracts import TRUSTED_CONTRACT_VERSION, contract_hash

    normalized_source = normalize_document_for_agent(text)
    metadata["contract_version"] = TRUSTED_CONTRACT_VERSION
    metadata["contract_hash"] = contract_hash(merged_endpoints)
    metadata["contract_status"] = (
        "trusted"
        if selected_round is not None and selected_round.passed
        else "fallback"
    )
    metadata["source_snapshot_sha256"] = hashlib.sha256(
        normalized_source.encode("utf-8")
    ).hexdigest()
    metadata["business_rule_count"] = sum(
        len(endpoint.business_rules)
        for endpoint in merged_endpoints
    )
    if missing_ai_keys:
        metadata["ai_missing_endpoints_recovered"] = [
            {"method": method, "path": path} for method, path in missing_ai_keys
        ]
    # Build a deterministic graph first. AI may add semantic edges, but cannot
    # remove schema-proven producer/consumer relationships.
    from ai_apitest.context.dependencies import dependency_edge_identity, infer_dependency_edges

    local_graph = {"edges": infer_dependency_edges(merged_endpoints), "source": "local_rule"}
    metadata["dependency_graph"] = local_graph
    # Generate dependency graph via master
    try:
        if discovery_mode == "agent":
            from ai_apitest.application.discovery import generate_dependency_graph_with_agent_runtime

            dep_graph = generate_dependency_graph_with_agent_runtime(
                root,
                project,
                merged_endpoints,
                llm,
                existing_graph=local_graph,
            )
        else:
            from ai_apitest.ai.api_discovery import generate_dependency_graph

            dep_graph = generate_dependency_graph(merged_endpoints, llm)
        if dep_graph and dep_graph.get("edges"):
            combined = {dependency_edge_identity(edge): edge for edge in local_graph["edges"]}
            for edge in dep_graph["edges"]:
                combined.setdefault(dependency_edge_identity(edge), edge)
            metadata["dependency_graph"] = {
                "edges": list(combined.values()),
                "source": "local_rule+ai_review",
                "rule": dep_graph.get("rule", ""),
            }
    except Exception as exc:
        record_operational_error(root, project, component="ai_discovery", operation="dependency_graph", error=exc)
    return merged_endpoints, metadata


def merge_endpoint_candidates(
    deterministic: list[Endpoint],
    ai_endpoints: list[Endpoint],
) -> list[Endpoint]:
    ai_by_key = {(item.method.upper(), item.path): item for item in ai_endpoints}
    merged = [
        merge_discovered_endpoint(item, ai_by_key.get((item.method.upper(), item.path)))
        for item in deterministic
    ]
    merged_keys = {(item.method.upper(), item.path) for item in merged}
    merged.extend(
        item for item in ai_endpoints if (item.method.upper(), item.path) not in merged_keys
    )
    return merged


def endpoint_catalog_gate_issues(
    candidate: list[Endpoint],
    deterministic: list[Endpoint],
) -> list[dict]:
    issues: list[dict] = []
    candidate_keys = [(item.method.upper(), item.path) for item in candidate]
    missing = sorted({(item.method.upper(), item.path) for item in deterministic} - set(candidate_keys))
    for method, path in missing:
        issues.append(
            {
                "severity": "critical",
                "dimension": "completeness",
                "location": f"{method} {path}",
                "evidence": "确定性解析器从同一接口文档中识别出该接口。",
                "reason": "候选接口目录遗漏确定性清单中的接口。",
                "suggested_fix": "重新读取对应文档片段并补齐接口。",
            }
        )
    duplicates = sorted({key for key in candidate_keys if candidate_keys.count(key) > 1})
    for method, path in duplicates:
        issues.append(
            {
                "severity": "high",
                "dimension": "non_redundancy",
                "location": f"{method} {path}",
                "evidence": "候选目录中存在相同 method/path 的重复记录。",
                "reason": "接口目录包含重复接口。",
                "suggested_fix": "合并重复记录并保留有文档依据的字段。",
            }
        )
    from ai_apitest.domain.contracts import contract_catalog_issues

    issues.extend(contract_catalog_issues(candidate, deterministic))
    return issues


def model_config_key(value: dict) -> str:
    provider = str(value.get("provider") or "openai-compatible").strip().lower()
    base_url = str(value.get("base_url") or "").strip().rstrip("/").lower()
    model = str(value.get("model") or "").strip().lower()
    return f"{provider}|{base_url}|{model}"


def case_quality_gate_issues(
    cases: list[TestCase],
    endpoints: list[Endpoint],
) -> list[dict]:
    if not cases:
        return [
            {
                "severity": "critical",
                "dimension": "completeness",
                "location": "用例集",
                "evidence": f"当前接口目录包含 {len(endpoints)} 个接口。",
                "reason": "用例产物为空。",
                "suggested_fix": "重新生成覆盖真实接口的可执行用例。",
            }
        ]
    from ai_apitest.domain.case_drafts import validate_compiled_cases

    issues: list[dict] = []
    for item in validate_compiled_cases(cases, endpoints):
        raw = item.to_dict()
        severity = "critical" if raw.get("severity") == "error" else "warning"
        issues.append(
            {
                "severity": severity,
                "dimension": case_issue_dimension(str(raw.get("code") or "")),
                "location": str(raw.get("scenario_id") or "用例集"),
                "evidence": f"本地统一用例门禁：{raw.get('code') or 'validation'}",
                "reason": str(raw.get("message") or "用例未通过本地门禁"),
                "suggested_fix": "根据接口定义和用例语义定向修订该场景。",
            }
        )
    from ai_apitest.domain.contracts import coverage_manifest_issues

    issues.extend(coverage_manifest_issues(endpoints, cases))
    return issues


def case_issue_dimension(code: str) -> str:
    lowered = code.lower()
    if any(marker in lowered for marker in ("missing", "coverage", "assertion")):
        return "completeness"
    if any(marker in lowered for marker in ("duplicate", "redund")):
        return "non_redundancy"
    if any(marker in lowered for marker in ("title", "intent", "runnable", "chain")):
        return "reasonableness"
    return "reliability"


def merge_discovered_endpoint(deterministic: Endpoint, ai: Endpoint | None) -> Endpoint:
    from ai_apitest.domain.contracts import merge_endpoint_contract

    return merge_endpoint_contract(deterministic, ai)


def generate_cases_with_config(
    root: Path,
    endpoints: list[Endpoint],
    project: str = "default",
    stream_callback: Callable[[str], None] | None = None,
    llm_override: dict | None = None,
    force_refresh: bool = False,
    artifact_language: str | None = None,
) -> tuple[list[TestCase], str]:
    config = load_config(root)
    language = configured_artifact_language(config, artifact_language)
    set_current_artifact_language(language)
    config.setdefault("artifacts", {})["language"] = language
    endpoints, contract_warning = ensure_trusted_contract_for_generation(
        root,
        project,
        endpoints,
        stream_callback=stream_callback,
    )
    llm = llm_override or configured_llm(root, "case_generator")
    fingerprint = case_generation_fingerprint(root, project, endpoints, config, llm)
    if not force_refresh and bool(config.get("agents", {}).get("reuse_generation_cache", True)):
        cached = load_generation_cache(root, project, fingerprint, endpoints)
        if cached is not None:
            return cached, "；".join(
                item
                for item in (
                    contract_warning,
                    "输入、策略、经验和模型配置均未变化，已复用通过质量门禁的生成缓存",
                )
                if item
            )
    from ai_apitest.application.quality_audit import QualityAuditService
    from ai_apitest.core.serde import read_json, to_jsonable
    from ai_apitest.domain.case_drafts import validate_compiled_cases
    from ai_apitest.domain.contracts import build_coverage_manifest

    audit_service = QualityAuditService(
        root,
        project,
        "test_cases",
        endpoints=endpoints,
        metadata={"operation": "full_generation", "endpoint_count": len(endpoints)},
    )
    feedback: dict | None = None
    audit_retry_cases: list[TestCase] | None = None
    audit_retry_diagnostics = ""
    audit_retry_reason = ""
    warnings: list[str] = [contract_warning] if contract_warning else []
    selected_round = None
    contract_metadata = read_json(
        project_docs_dir(root, project) / "metadata.json",
        {},
    )
    for round_index in range(1, audit_service.policy.max_rounds + 1):
        if audit_retry_cases is not None:
            cases = audit_retry_cases
            round_diagnostics = audit_retry_diagnostics
            warnings.append(
                audit_retry_reason
                or f"第 {round_index} 轮复用上一轮候选，未重复调用完整产出模型"
            )
        else:
            round_diagnostics = ""
            try:
                cases, warning = _generate_cases_without_cache(
                    root,
                    endpoints,
                    project=project,
                    stream_callback=stream_callback,
                    llm_override=llm_override,
                    quality_feedback=feedback,
                    quality_round=round_index,
                )
                round_diagnostics = warning
                if warning:
                    warnings.append(warning)
            except Exception as exc:
                cases = []
                round_diagnostics = f"用例产出失败：{exc}"
                warnings.append(f"第 {round_index} 轮用例产出失败：{exc}")
                record_operational_error(
                    root,
                    project,
                    component="quality_audit",
                    operation="produce_test_cases",
                    error=exc,
                    detail={"round": round_index},
                )
        cases = localize_generated_cases(cases, language)
        gate_issues = case_quality_gate_issues(cases, endpoints)
        audit_round = audit_service.evaluate(
            to_jsonable(cases),
            evidence={
                "endpoints": to_jsonable(endpoints),
                "trusted_contract": {
                    "contract_version": contract_metadata.get("contract_version", ""),
                    "contract_hash": contract_metadata.get("contract_hash", ""),
                    "contract_status": contract_metadata.get("contract_status", ""),
                },
                "dependency_graph": read_json(
                    project_docs_dir(root, project) / "metadata.json",
                    {},
                ).get("dependency_graph", {}),
                "generation_strategies": case_generation_strategy_context(root, endpoints),
                "generation_diagnostics": round_diagnostics,
                "coverage_manifest": build_coverage_manifest(endpoints, cases),
                "local_gate_issues": [
                    item.to_dict()
                    for item in validate_compiled_cases(cases, endpoints)
                ] if cases else [],
            },
            round_index=round_index,
            hard_gate_issues=gate_issues,
            producer_model_key=model_config_key(llm),
            stream_callback=stream_callback,
        )
        if audit_round.passed:
            selected_round = audit_round
            break
        if audit_round.hard_gate_pass and not audit_round.audit_complete:
            audit_retry_cases = cases
            audit_retry_diagnostics = round_diagnostics
            audit_retry_reason = (
                f"第 {round_index + 1} 轮仅重试未完成的审计模型，"
                "复用上一轮候选，未重复调用产出模型"
            )
            feedback = None
        else:
            repair_feedback = audit_round.repair_feedback()
            repaired, repair_message = repair_cases_targeted_from_audit(
                root,
                project,
                cases,
                endpoints,
                repair_feedback,
                llm,
            )
            if repaired is not None:
                audit_retry_cases = repaired
                audit_retry_diagnostics = "；".join(
                    item for item in (round_diagnostics, repair_message) if item
                )
                audit_retry_reason = (
                    f"第 {round_index + 1} 轮复用上一轮未受影响用例，"
                    f"{repair_message}"
                )
                feedback = None
            else:
                audit_retry_cases = None
                audit_retry_diagnostics = ""
                audit_retry_reason = ""
                feedback = repair_feedback
    if selected_round is None:
        selected_round = audit_service.best_safe_round()
    if selected_round is not None:
        from ai_apitest.core.serde import case_from_dict

        cases = [
            case_from_dict(item)
            for item in selected_round.artifact
            if isinstance(item, dict)
        ]
        accepted = selected_round.passed
        audit_service.finalize(
            selected_round,
            decision="accepted" if accepted else "highest_safe_candidate",
            reason=(
                "最终用例质量审计通过。"
                if accepted
                else "三轮未达阈值，交付通过硬门禁且无 critical 问题的最高分候选。"
            ),
        )
        score = f"{selected_round.average_score:.2f}" if selected_round.average_score is not None else "-"
        warnings.append(
            f"质量审计{'通过' if accepted else '安全兜底'}："
            f"第 {selected_round.round_index} 轮，平均分 {score}，审计会话 {audit_service.session_id}"
        )
    else:
        cases = localize_generated_cases(generate_cases(endpoints), language)
        fallback_issues = case_quality_gate_issues(cases, endpoints)
        if fallback_issues:
            cases = []
        audit_service.finalize(
            None,
            decision="deterministic_fallback" if cases else "no_safe_artifact",
            reason=(
                "三轮后没有安全模型候选，交付确定性标准用例。"
                if cases
                else "三轮后没有安全模型候选，确定性用例也未通过硬门禁。"
            ),
        )
        warnings.append(
            "三轮质量审计后没有安全模型候选，"
            + ("已交付确定性标准用例" if cases else "未覆盖现有用例文件")
        )
    if cases:
        try:
            record_case_generation_lessons(root, project, cases, actor="quality:selected_case_artifact")
        except Exception as exc:
            record_operational_error(
                root,
                project,
                component="context",
                operation="record_selected_case_lessons",
                error=exc,
            )
        save_generation_cache(root, project, fingerprint, cases)
    return cases, "；".join(dict.fromkeys(item for item in warnings if item))


def repair_cases_targeted_from_audit(
    root: Path,
    project: str,
    cases: list[TestCase],
    endpoints: list[Endpoint],
    feedback: dict[str, Any],
    llm: dict[str, Any],
) -> tuple[list[TestCase] | None, str]:
    """Repair only cases named by evidence-backed audit issues.

    Missing coverage without an existing case and artifact-wide findings still
    require a new planning pass. They intentionally return ``None`` so the
    caller regenerates with audit feedback.
    """
    if not cases or not is_llm_configured(llm):
        return None, ""
    issues = feedback.get("issues") if isinstance(feedback, dict) else None
    if not isinstance(issues, list):
        return None, ""
    impacted_ids: set[str] = set()
    for issue in issues:
        if not isinstance(issue, dict) or str(issue.get("severity") or "") == "info":
            continue
        location = str(
            issue.get("location")
            or issue.get("scenario_id")
            or issue.get("case_id")
            or ""
        ).strip()
        if not location or location == "产物整体":
            continue
        for case in cases:
            identities = [case.id, case.scenario_id, *case.rule_ids]
            if any(identity and identity in location for identity in identities):
                impacted_ids.add(case.id)
    if not impacted_ids or len(impacted_ids) == len(cases):
        return None, ""
    impacted = [case for case in cases if case.id in impacted_ids]
    try:
        from ai_apitest.application.case_maintenance import maintain_cases_with_agent_runtime

        result = maintain_cases_with_agent_runtime(
            root,
            project,
            impacted=impacted,
            endpoints=endpoints,
            change_context={
                "source": "quality_audit",
                "instruction": "只修复审计指出的问题，严格保留未受影响语义和业务规则血缘。",
                "feedback": feedback,
            },
            llm=llm,
            allow_retire=False,
            repair_attempts=2,
        )
    except Exception as exc:
        record_operational_error(
            root,
            project,
            component="quality_audit",
            operation="targeted_case_repair",
            error=exc,
            detail={"impacted_case_ids": sorted(impacted_ids)},
        )
        return None, ""
    updated_by_id = {case.id: case for case in result.updated}
    if set(updated_by_id) != impacted_ids:
        return None, ""
    repaired = [updated_by_id.get(case.id, case) for case in cases]
    return repaired, f"仅定向修订 {len(impacted_ids)} 条审计命中的用例，保留其余 {len(cases) - len(impacted_ids)} 条"


def ensure_trusted_contract_for_generation(
    root: Path,
    project: str,
    endpoints: list[Endpoint],
    *,
    stream_callback: Callable[[str], None] | None = None,
) -> tuple[list[Endpoint], str]:
    """Route an incomplete downstream input back through interface recognition.

    The case-generation roles never receive the source snapshot. Only this
    orchestration boundary invokes the interface-recognition pipeline, then
    replaces the downstream input with its newly audited artifact.
    """
    from ai_apitest.application.discovery import normalize_document_for_agent
    from ai_apitest.context.dependencies import synchronize_project_dependencies
    from ai_apitest.core.paths import (
        project_docs_file,
        project_metadata_file,
        project_source_snapshot_file,
    )
    from ai_apitest.core.serde import read_json, to_jsonable, write_json
    from ai_apitest.discovery.document import parse_api_document
    from ai_apitest.domain.contracts import (
        TRUSTED_CONTRACT_VERSION,
        contract_hash,
        normalize_endpoint_contract,
    )

    normalized = [normalize_endpoint_contract(item) for item in endpoints]
    metadata = read_json(project_metadata_file(root, project), {})
    snapshot_path = project_source_snapshot_file(root, project)
    snapshot = snapshot_path.read_text(encoding="utf-8") if snapshot_path.exists() else ""
    issues: list[dict] = []
    if snapshot and str(metadata.get("contract_status") or "") != "trusted_manual":
        baseline, _source_metadata = parse_api_document(snapshot, source=str(metadata.get("source") or ""))
        issues.extend(endpoint_catalog_gate_issues(normalized, baseline))
    stored_hash = str(metadata.get("contract_hash") or "")
    actual_hash = contract_hash(normalized)
    if (
        str(metadata.get("contract_version") or "") == TRUSTED_CONTRACT_VERSION
        and stored_hash
        and stored_hash != actual_hash
    ):
        issues.append(
            {
                "severity": "critical",
                "location": "trusted_contract",
                "reason": "接口契约内容哈希与已审计版本不一致。",
            }
        )
    if not issues:
        if (
            str(metadata.get("contract_version") or "") != TRUSTED_CONTRACT_VERSION
            or stored_hash != actual_hash
        ):
            metadata.update(
                {
                    "contract_version": TRUSTED_CONTRACT_VERSION,
                    "contract_hash": actual_hash,
                    "contract_status": metadata.get("contract_status") or "trusted_legacy",
                    "business_rule_count": sum(len(item.business_rules) for item in normalized),
                }
            )
            write_json(project_docs_file(root, project), to_jsonable(normalized))
            write_json(project_metadata_file(root, project), metadata)
        return normalized, ""

    if not snapshot:
        detail = "；".join(str(item.get("reason") or item) for item in issues[:10])
        raise ValueError(
            "接口识别产物不完整且没有可用于上游定向修订的接口文档快照；"
            f"请先重新识别接口文档。{detail}"
        )
    if stream_callback:
        stream_callback(
            "用例生成预检发现上游接口识别产物缺陷，正在退回接口识别阶段定向修订并重新审计。\n"
        )
    baseline, source_metadata = parse_api_document(
        snapshot,
        source=str(metadata.get("source") or ""),
    )
    feedback = {
        "instruction": "修复接口识别产物缺失，不生成测试用例。",
        "issues": issues,
    }
    # apply_ai_discovery_if_configured owns source-document access and endpoint
    # auditing. Passing feedback through metadata keeps the stage boundary
    # explicit without exposing the source to downstream roles.
    source_metadata["upstream_repair_feedback"] = feedback
    repaired, repaired_metadata = apply_ai_discovery_if_configured(
        root,
        normalize_document_for_agent(snapshot),
        baseline,
        source_metadata,
        stream_callback=stream_callback,
        project=project,
    )
    if not repaired:
        raise ValueError("接口识别阶段定向修订后仍没有可交付的可信接口契约")
    repaired = [normalize_endpoint_contract(item) for item in repaired]
    repaired_metadata.update(
        {
            "source": metadata.get("source", ""),
            "source_name": metadata.get("source_name", ""),
            "source_kind": metadata.get("source_kind", ""),
            "upstream_repaired_for_case_generation": True,
            "contract_version": TRUSTED_CONTRACT_VERSION,
            "contract_hash": contract_hash(repaired),
            "contract_status": repaired_metadata.get("contract_status") or "fallback",
            "business_rule_count": sum(len(item.business_rules) for item in repaired),
        }
    )
    write_json(project_docs_file(root, project), to_jsonable(repaired))
    write_json(project_metadata_file(root, project), repaired_metadata)
    synchronize_project_dependencies(
        root,
        project,
        normalized,
        repaired,
        actor="orchestrator:upstream-contract-repair",
    )
    return repaired, "检测到接口识别产物缺陷，已在接口识别阶段定向修订并重新审计后继续生成"


def _generate_cases_without_cache(
    root: Path,
    endpoints: list[Endpoint],
    project: str = "default",
    stream_callback: Callable[[str], None] | None = None,
    llm_override: dict | None = None,
    quality_feedback: dict | None = None,
    quality_round: int = 1,
) -> tuple[list[TestCase], str]:
    llm = llm_override or configured_llm(root, "case_generator")
    if not is_llm_configured(llm):
        return generate_cases(endpoints), ""
    config = load_config(root)
    generation_mode = str(config.get("agents", {}).get("case_generation_mode") or "agent").strip().lower()
    if generation_mode == "agent":
        from ai_apitest.application.case_generation import generate_cases_with_agent_runtime

        return generate_cases_with_agent_runtime(
            root,
            endpoints,
            project=project,
            stream_callback=stream_callback,
            llm_override=llm_override,
            quality_feedback=quality_feedback,
            quality_round=quality_round,
            record_lessons=False,
        )
    if generation_mode != "legacy":
        raise ValueError(f"不支持的用例生成模式：{generation_mode}；可选值为 agent 或 legacy")
    strategy_context = case_generation_strategy_context(root, endpoints)
    context_block = ""
    try:
        context_block = build_context_bundle(
            root, project, task_type="case_generation", model_config=llm,
            context_config=config.get("context", {}),
            system_prompt="", user_prompt_base="",
            endpoints=endpoints, environment=str(config.get("default_environment", "")),
        ).text
    except Exception as exc:
        record_operational_error(root, project, component="context", operation="build_case_generation_bundle", error=exc)
        context_block = ""
    if quality_feedback:
        context_block += (
            "\n\n## 上一轮质量审计修订要求\n"
            + json.dumps(quality_feedback, ensure_ascii=False, separators=(",", ":"), default=str)
        )
    base_kwargs = dict(base_url=llm.get("base_url",""), model=llm.get("model",""),
                       api_key_env=llm.get("api_key_env",""), provider=llm.get("provider","openai-compatible"),
                       timeout=DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS,
                       max_tokens=int(llm.get("default_output_tokens", 8000)))

    # Read dependency graph from project metadata (master-generated during API discovery)
    from ai_apitest.core.serde import read_json as _read_json
    dep_graph = _read_json(project_docs_dir(root, project) / "metadata.json", {}).get("dependency_graph")
    if dep_graph:
        dep_issues = validate_dependency_graph(dep_graph, endpoints)
        if dep_issues:
            dep_graph = None  # invalidate if structural issues found

    # ══ Phase 1: Coverage plan ══
    try:
        if stream_callback:
            stream_callback("开始生成覆盖计划...\n")
        plan, plan_warning = generate_coverage_plan(
            endpoints=endpoints, strategy_context=strategy_context,
            context_block=context_block, dep_graph=dep_graph,
            stream_callback=stream_callback, **base_kwargs,
        )
    except Exception as exc:
        record_operational_error(root, project, component="case_generation", operation="coverage_plan", error=exc)
        return [], f"覆盖计划生成失败（模型 {llm.get('model','?')}）：{exc}。可检查 API 连通性或稍后重试。"
    if not plan:
        return [], "大模型未生成覆盖计划。请检查接口定义和生成策略配置。"
    if stream_callback:
        stream_callback(f"\n覆盖计划生成完成：{len(plan)} 个场景。\n")

    # ── System hard gap fill ──
    plan = _fill_hard_gaps(plan, endpoints, dep_graph=dep_graph)
    plan = _fill_doc_based_gaps(plan, endpoints)

    # ── Plan design_method consistency check ──
    plan_dm_issues = _detect_plan_design_method_mismatch(plan)
    diag_parts: list[str] = []
    if plan_dm_issues:
        sample = ", ".join(i.split(":")[0] for i in plan_dm_issues[:3])
        diag_parts.append(f"覆盖计划 {len(plan_dm_issues)} 个场景设计方法与标题语义不一致：{sample}")

    # ── Master plan review (lightweight, high-ROI) ──
    _review_coverage_plan(root, project, plan, endpoints)

    # ══ Phase 2: Batch generation from plan ══
    from collections import defaultdict
    batches: list[list[dict[str, Any]]] = []
    chain_items = [p for p in plan if p["endpoint_method"] == "CHAIN"]
    ep_items = [p for p in plan if p["endpoint_method"] != "CHAIN"]
    by_endpoint: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for p in ep_items:
        by_endpoint[(p["endpoint_method"], p["endpoint_path"])].append(p)
    ep_keys = list(by_endpoint.keys())
    for i in range(0, len(ep_keys), 4):
        batch = []
        for key in ep_keys[i:i + 4]:
            batch.extend(by_endpoint[key])
        batches.append(batch)
    if chain_items:
        batches.append(chain_items)

    all_cases: list[TestCase] = []
    diag_parts.append(f"plan: {len(plan)} 场景, {len(batches)} 批次")
    unresolved_scenario_ids: set[str] = set()
    counters: dict[str, int] = {}

    for bi, batch in enumerate(batches):
        batch_sids = {p["scenario_id"] for p in batch}
        try:
            if stream_callback:
                stream_callback(f"\n开始生成第 {bi + 1}/{len(batches)} 批用例，覆盖 {len(batch)} 个场景...\n")
            batch_cases, bw = generate_cases_from_plan_batch(
                plan_items=batch, endpoints=endpoints,
                context_block=context_block, existing_cases=all_cases,
                dep_graph=dep_graph, stream_callback=stream_callback, **base_kwargs,
            )
        except Exception as exc:
            record_operational_error(
                root,
                project,
                component="case_generation",
                operation="generate_batch",
                error=exc,
                detail={"batch": bi + 1, "scenario_count": len(batch)},
            )
            batch_cases, bw = [], ""
        # Only report scenario_id issues if they persist through retries
        if bw and "缺失" in bw:
            pass  # track in unresolved, report at end
        elif bw:
            diag_parts.append(bw)

        if batch_cases:
            batch_cases = attach_cleanup_steps(attach_setup_steps(batch_cases, endpoints), endpoints)
            accepted = dedupe_supplemental_cases(all_cases, batch_cases)
            all_cases = all_cases + accepted
            # Coverage based on actually-accepted cases (post-dedupe)
            accepted_sids = {getattr(c, "scenario_id", "") for c in accepted}
            missing_items = [p for p in batch if p["scenario_id"] not in accepted_sids]
        else:
            missing_items = list(batch)

        # Retry missing scenarios
        retry_count = 0
        while missing_items and retry_count < 2:
            retry_count += 1
            try:
                if stream_callback:
                    stream_callback(f"\n第 {bi + 1} 批仍缺 {len(missing_items)} 个场景，正在第 {retry_count} 次补生成...\n")
                retry_cases, rw = generate_cases_from_plan_batch(
                    plan_items=missing_items, endpoints=endpoints,
                    context_block=context_block, existing_cases=all_cases,
                    dep_graph=dep_graph, stream_callback=stream_callback, **base_kwargs,
                )
                if rw and "缺失" not in rw:
                    diag_parts.append(f"补生成: {rw}")
            except Exception as exc:
                record_operational_error(
                    root,
                    project,
                    component="case_generation",
                    operation="retry_missing_scenarios",
                    error=exc,
                    detail={"batch": bi + 1, "attempt": retry_count, "missing": len(missing_items)},
                )
                retry_cases = []
            if retry_cases:
                retry_cases = attach_cleanup_steps(attach_setup_steps(retry_cases, endpoints), endpoints)
                accepted_retry = dedupe_supplemental_cases(all_cases, retry_cases)
                all_cases = all_cases + accepted_retry
                retry_sids = {getattr(c, "scenario_id", "") for c in accepted_retry}
                missing_items = [p for p in missing_items if p["scenario_id"] not in retry_sids]
        unresolved_scenario_ids.update(p["scenario_id"] for p in missing_items)

    # Pre-governance coverage check
    missing_pre = _verify_plan_scenario_coverage(plan, all_cases)
    if missing_pre:
        unresolved_scenario_ids.update(missing_pre)

    if not all_cases:
        return [], "两阶段生成未产出有效用例。" + "; ".join(diag_parts)

    # The common quality-audit wrapper records lessons only for the selected
    # immutable candidate. Legacy governance remains a rollback-only option
    # when the new artifact audit is explicitly disabled.
    if _governance_enabled(root) and not _quality_audit_artifact_enabled(root, "test_cases"):
        revised = _govern_cases(root, project, all_cases, endpoints)
        if revised:
            all_cases = revised
            # Re-check after governance
            missing_post = _verify_plan_scenario_coverage(plan, all_cases)
            if missing_post:
                unresolved_scenario_ids.update(missing_post)
                diag_parts.append(f"治理后发现 {len(missing_post)} 个计划场景无对应用例")

    # ══ Final unified status (after governance) ══
    # Pre-check silently, then repair, then report final status
    final_gaps = _detect_coverage_gaps(all_cases, endpoints)
    final_missing = _verify_plan_scenario_coverage(plan, all_cases)
    dm_mismatches = _detect_design_method_mismatch(plan, all_cases)
    if final_missing:
        unresolved_scenario_ids.update(final_missing)

    # ── One-shot targeted fix (uses case_generator, not governance-gated) ──
    fix_attempted = False
    case_gen = configured_llm(root, "case_generator")
    if (dm_mismatches or final_gaps) and is_llm_configured(case_gen):
        affected_sids: set[str] = set()
        for m in dm_mismatches:
            sid = m.split("(")[1].split(")")[0] if "(" in m else ""
            if sid:
                affected_sids.add(sid)
        for gap in final_gaps:
            gap_method = gap.get("method", "")
            gap_path = gap.get("path", "")
            gap_strategy = gap.get("missing_strategy", "")
            for p in plan:
                if p["endpoint_method"] == gap_method and p["endpoint_path"] == gap_path:
                    if gap_strategy == "dependency_chain" or p["design_method"] == gap_strategy or gap_strategy == "happy_path":
                        affected_sids.add(p["scenario_id"])
        affected_items = [p for p in plan if p["scenario_id"] in affected_sids]
        if affected_items:
            try:
                fix_cases, fw = generate_cases_from_plan_batch(
                    plan_items=affected_items, endpoints=endpoints,
                    context_block=context_block, existing_cases=all_cases,
                    dep_graph=dep_graph, **base_kwargs,
                )
                if fix_cases:
                    fix_cases = attach_cleanup_steps(attach_setup_steps(fix_cases, endpoints), endpoints)
                    fix_sids = {getattr(c, "scenario_id", "") for c in fix_cases if getattr(c, "scenario_id", "")}
                    all_cases = [c for c in all_cases if getattr(c, "scenario_id", "") not in fix_sids]
                    all_cases = all_cases + fix_cases
                    fix_attempted = True
                    diag_parts.append(f"验收未通过后定向修复 {len(fix_cases)} 条")
                    # Re-evaluate after successful fix
                    final_gaps = _detect_coverage_gaps(all_cases, endpoints)
                    final_missing = _verify_plan_scenario_coverage(plan, all_cases)
                    dm_mismatches = _detect_design_method_mismatch(plan, all_cases)
            except Exception as exc:
                record_operational_error(root, project, component="case_generation", operation="targeted_acceptance_repair", error=exc)

    # Report diagnostics AFTER repair attempt (no stale warnings)
    if dm_mismatches:
        diag_parts.append(f"{len(dm_mismatches)} 条用例 design_method 与计划不一致")
    if final_gaps:
        gap_details = ", ".join(f"{g['method']} {g['path']}/{g['missing_strategy']}" for g in final_gaps[:5])
        diag_parts.append(f"仍有 {len(final_gaps)} 项缺口：{gap_details}")

    if final_gaps or final_missing or dm_mismatches or plan_dm_issues:
        diag_parts.append("覆盖验收未通过")
    else:
        diag_parts.append("覆盖验收通过")

    # Clean up: remove scenario_ids that are now covered (e.g. by final repair)
    final_sids = {getattr(c, "scenario_id", "") for c in all_cases if getattr(c, "scenario_id", "")}
    unresolved_scenario_ids = {sid for sid in unresolved_scenario_ids if sid not in final_sids}

    warning = (plan_warning + "；" if plan_warning else "") + "; ".join(diag_parts)
    if unresolved_scenario_ids:
        warning += f"。{len(unresolved_scenario_ids)} 个场景未产出用例，可尝试重新生成或调整策略配置后重试"

    # ── Local validation: runnable misclassification + variable flow ──
    misclassifications = detect_runnable_misclassification(all_cases, dep_graph)
    if misclassifications:
        warning += f"。发现 {len(misclassifications)} 条用例可执行性误判"
    setup_issues = validate_setup_variable_flow(all_cases)
    if setup_issues:
        warning += f"。{len(setup_issues)} 条用例 setup_steps 变量未被引用"

    return all_cases, warning


def case_generation_fingerprint(
    root: Path,
    project: str,
    endpoints: list[Endpoint],
    config: dict,
    llm: dict,
) -> str:
    from ai_apitest.application.quality_audit import unique_audit_models
    from ai_apitest.core.paths import project_metadata_file
    from ai_apitest.core.serde import read_json, to_jsonable

    lesson_version: dict[str, object] = {"count": 0, "revision": 0, "updated_at": ""}
    try:
        with context_connection(root, project) as conn:
            row = conn.execute(
                """
                select count(*) as count, coalesce(sum(revision), 0) as revision,
                       coalesce(max(updated_at), '') as updated_at
                from lessons where project = ? and lifecycle_status in ('active', 'reopened')
                  and review_status = 'confirmed'
                """,
                (safe_project_name(project),),
            ).fetchone()
            if row:
                lesson_version = dict(row)
    except Exception:
        pass
    role_dir = Path(__file__).resolve().parents[1] / "agents" / "identities"
    role_versions = {
        name: hashlib.sha256((role_dir / name).read_bytes()).hexdigest()
        for name in ("test_planner.yaml", "case_designer.yaml")
    }
    project_metadata = read_json(project_metadata_file(root, project), {})
    graph = project_metadata.get("dependency_graph", {})
    graph_edges = graph.get("edges", []) if isinstance(graph, dict) else []
    semantic_graph = {
        "edges": sorted(
            [item for item in graph_edges if isinstance(item, dict)],
            key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, default=str),
        ),
        "rule": graph.get("rule", "") if isinstance(graph, dict) else "",
    }
    generation_agent_keys = {
        "case_generation_mode",
        "planner_batch_endpoints",
        "designer_batch_endpoints",
        "designer_batch_scenarios",
        "semantic_repair_attempts",
        "history_max_chars",
    }
    payload = {
        "pipeline_version": GENERATION_PIPELINE_VERSION,
        "trusted_contract": {
            "version": project_metadata.get("contract_version", ""),
            "hash": project_metadata.get("contract_hash", ""),
            "status": project_metadata.get("contract_status", ""),
            "source_snapshot_sha256": project_metadata.get("source_snapshot_sha256", ""),
        },
        "endpoints": to_jsonable(endpoints),
        "dependency_graph": semantic_graph,
        "case_generation": config.get("case_generation", {}),
        "agents": {key: value for key, value in config.get("agents", {}).items() if key in generation_agent_keys},
        "quality_audit": config.get("quality_audit", {}),
        "artifact_language": configured_artifact_language(config),
        "audit_models": [
            model.key
            for model in unique_audit_models(config.get("llm", {}))
        ],
        "llm": {key: value for key, value in llm.items() if "key" not in key.lower()},
        "lessons": lesson_version,
        "roles": role_versions,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_generation_cache(
    root: Path,
    project: str,
    fingerprint: str,
    endpoints: list[Endpoint],
) -> list[TestCase] | None:
    from ai_apitest.core.paths import project_generation_cache
    from ai_apitest.core.serde import case_from_dict, read_json

    payload = read_json(project_generation_cache(root, project), {})
    if not isinstance(payload, dict) or payload.get("fingerprint") != fingerprint:
        return None
    try:
        cases = [case_from_dict(item) for item in payload.get("cases", []) if isinstance(item, dict)]
    except Exception:
        return None
    if not cases or case_quality_gate_issues(cases, endpoints):
        return None
    return cases


def save_generation_cache(root: Path, project: str, fingerprint: str, cases: list[TestCase]) -> None:
    if not cases:
        return
    from datetime import datetime, timezone
    from ai_apitest.core.paths import project_generation_cache
    from ai_apitest.core.serde import to_jsonable, write_json

    write_json(
        project_generation_cache(root, project),
        {
            "fingerprint": fingerprint,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "cases": to_jsonable(cases),
        },
    )


def detect_runnable_misclassification(
    cases: list[TestCase],
    dep_graph: dict[str, Any] | None,
) -> list[dict[str, str]]:
    """Detect cases marked runnable=false that could execute via setup_steps."""
    issues: list[dict[str, str]] = []
    if not dep_graph or not dep_graph.get("edges"):
        return issues
    safe_edges = [e for e in dep_graph["edges"] if e.get("safe_to_prepare")]
    if not safe_edges:
        return issues
    for case in cases:
        if case.runnable:
            continue
        if case.design_method not in ("happy_path", "status_code_coverage", "equivalence_class"):
            continue
        for edge in safe_edges:
            to = edge.get("to", {})
            if to.get("method") == case.method and to.get("path") == case.path:
                producer = edge.get("from", {})
                issues.append({
                    "case_id": case.id, "scenario_id": getattr(case, "scenario_id", ""),
                    "severity": "error", "type": "runnable_misclassified",
                    "detail": f"runnable=false 但 {producer.get('method')} {producer.get('path')} 可准备数据",
                    "producer": f"{producer.get('method')} {producer.get('path')}",
                })
                break
    return issues


def validate_dependency_graph(dep_graph: dict[str, Any] | None, endpoints: list[Endpoint]) -> list[dict[str, str]]:
    """Validate dependency graph edges reference real endpoints."""
    issues: list[dict[str, str]] = []
    if not dep_graph or not dep_graph.get("edges"):
        return issues
    ep_keys = {(ep.method, ep.path) for ep in endpoints}
    for i, edge in enumerate(dep_graph["edges"]):
        frm = edge.get("from", {})
        to = edge.get("to", {})
        f_key = (str(frm.get("method","")).upper(), str(frm.get("path","")))
        t_key = (str(to.get("method","")).upper(), str(to.get("path","")))
        if f_key not in ep_keys:
            issues.append({"severity": "error", "type": "invalid_from",
                           "detail": f"edge[{i}] from {f_key} not in endpoints"})
        if t_key not in ep_keys:
            issues.append({"severity": "error", "type": "invalid_to",
                           "detail": f"edge[{i}] to {t_key} not in endpoints"})
        if edge.get("confidence") not in ("high", "medium", "low"):
            issues.append({"severity": "warn", "type": "invalid_confidence",
                           "detail": f"edge[{i}] confidence={edge.get('confidence')}"})
    return issues


def validate_setup_variable_flow(cases: list[TestCase]) -> list[dict[str, str]]:
    """Detect cases with setup_steps where extract variables aren't referenced in path_params/query/body."""
    issues: list[dict[str, str]] = []
    for case in cases:
        if not case.setup_steps or not case.runnable:
            continue
        for step in case.setup_steps:
            if not step.extract:
                continue
            for var_name in step.extract:
                ref = "{{" + var_name + "}}"
                params_str = str(case.path_params) + str(case.query) + str(case.body or "")
                if ref not in params_str:
                    issues.append({
                        "case_id": case.id, "scenario_id": getattr(case, "scenario_id", ""),
                        "severity": "warn", "type": "extract_not_referenced",
                        "detail": f"setup_steps 提取了 {var_name} 但主请求未引用 {ref}",
                    })
    return issues


def _detect_plan_design_method_mismatch(plan: list[dict[str, Any]]) -> list[str]:
    """Detect plan items whose title_hint suggests a different design_method.

    Uses lightweight keyword rules on title_hint (structured, short field).
    """
    RULES = [
        (["路径参数", "非法格式", "不存在资源", "空值路径"], "path_parameter_error"),
        (["鉴权", "认证", "未授权", "Token"], "auth_error"),
        (["边界", "超长", "最小值", "最大值", "最小长度", "最大长度", "空字符串"], "boundary_value"),
        (["类型错误", "格式非法", "非法类型", "JSON非法"], "type_error"),
    ]
    issues = []
    for item in plan:
        hint = str(item.get("title_hint", "")).lower()
        dm = str(item.get("design_method", ""))
        for keywords, expected_dm in RULES:
            if any(kw.lower() in hint for kw in keywords) and dm != expected_dm and dm == "happy_path":
                issues.append(f"{item.get('scenario_id','')}: title_hint='{item.get('title_hint','')}' 应为 {expected_dm} 非 {dm}")
                break
    return issues


def _verify_plan_scenario_coverage(plan: list[dict[str, Any]], cases: list[TestCase]) -> set[str]:
    """Return scenario_ids from plan that have no matching case."""
    planned_sids = {p["scenario_id"] for p in plan}
    final_sids = {getattr(c, "scenario_id", "") for c in cases if getattr(c, "scenario_id", "")}
    return planned_sids - final_sids


def _detect_design_method_mismatch(plan: list[dict[str, Any]], cases: list[TestCase]) -> list[str]:
    """Detect cases whose design_method doesn't match the plan or its own tags.

    Two checks:
    A. case.design_method vs plan[scenario_id].design_method (syntactic)
    B. case.design_method vs case.tags — if tags contain a known design_method
       that differs from case.design_method, flag it (semantic via structured data).
    """
    plan_by_sid = {p["scenario_id"]: p.get("design_method", "") for p in plan}
    known_methods = {"happy_path", "path_parameter_error", "boundary_value", "type_error",
                     "auth_error", "optional_field", "equivalence_class", "status_code_coverage",
                     "dependency_chain", "required_field"}
    issues = []
    for c in cases:
        sid = getattr(c, "scenario_id", "")
        expected = plan_by_sid.get(sid)
        # A: plan mismatch
        if expected and c.design_method != expected:
            issues.append(f"{c.id} ({sid}): design_method={c.design_method} 计划为 {expected}")
        # B: tags mismatch
        tag_methods = {t for t in (c.tags or []) if t in known_methods}
        if tag_methods and c.design_method not in tag_methods:
            issues.append(f"{c.id} ({sid}): design_method={c.design_method} 但 tags 含 {tag_methods}")
    return issues


def _fill_doc_based_gaps(plan: list[dict[str, Any]], endpoints: list[Endpoint]) -> list[dict[str, Any]]:
    """Add plan items for document-declared constraints not yet covered.

    Unlike a mechanical strategy×endpoint matrix, this only triggers when the API
    doc explicitly declares: error response codes, parameter constraints, or auth
    requirements. LLM-merged coverage (via coverage_tags) is respected.
    """
    from ai_apitest.domain.coverage import can_construct_error_status, first_response_basis

    result = list(plan)
    covered_methods: dict[tuple[str, str], set[str]] = {}
    for p in result:
        key = (p["endpoint_method"], p["endpoint_path"])
        covered_methods.setdefault(key, set()).update(p.get("coverage_tags", []))

    for ep in endpoints:
        key = (ep.method, ep.path)
        covered = covered_methods.get(key, set())

        # Documented error responses → need negative coverage
        error_codes = [
            int(k)
            for k in (ep.responses or {})
            if isinstance(k, str) and k.isdigit() and int(k) >= 400 and can_construct_error_status(ep, int(k))
        ]
        if error_codes and not (covered & {"status_code_coverage", "type_error", "path_parameter_error", "auth_error", "boundary_value"}):
            result.append(
                _make_plan_item(
                    ep,
                    "status_code_coverage",
                    "P1",
                    f"文档声明错误响应 {error_codes[0]}",
                    ["status_code_coverage"],
                    expected_basis=f"responses.{error_codes[0]}",
                )
            )

        # Parameter constraints → need boundary or equivalence
        for param in ep.parameters or []:
            schema = param.get("schema", {}) if isinstance(param.get("schema"), dict) else {}
            desc = str(param.get("description", "")) + " " + str(schema.get("description", ""))
            has_constraint = (
                any(k in schema for k in ("maxLength", "minLength", "maximum", "minimum", "enum"))
                or any(kw in desc for kw in ("最长", "最短", "最大", "最小", "不超过", "至少", "字符", "长度"))
            )
            boundary_basis = first_response_basis(ep, {400, 416, 422})
            if has_constraint and boundary_basis and not (covered & {"boundary_value", "equivalence_class", "type_error"}):
                result.append(
                    _make_plan_item(
                        ep,
                        "boundary_value",
                        "P1",
                        f"参数 {param.get('name','')} 有文档约束",
                        ["boundary_value"],
                        expected_basis=boundary_basis,
                    )
                )
                break

        # Auth declared → need auth_error (only if doc/params mention it)
        auth_hints = ("auth", "token", "api_key", "apikey", "bearer", "鉴权", "认证")
        ep_text = f"{ep.summary or ''} {ep.description or ''} {' '.join(p.get('name','')+p.get('description','') for p in (ep.parameters or []))}".lower()
        auth_basis = first_response_basis(ep, {401, 403})
        if auth_basis and (ep.security or any(h in ep_text for h in auth_hints)) and "auth_error" not in covered:
            result.append(
                _make_plan_item(
                    ep,
                    "auth_error",
                    "P1",
                    "文档声明鉴权要求",
                    ["auth_error"],
                    expected_basis=auth_basis,
                )
            )

    return result


def _make_plan_item(
    ep: Endpoint,
    dm: str,
    priority: str,
    title_hint: str,
    tags: list[str],
    *,
    expected_basis: str | None = None,
) -> dict[str, Any]:
    sid_base = f"{ep.method}-{ep.path}".replace("/", "-").replace("{", "").replace("}", "").strip("-")[:50]
    return {
        "scenario_id": f"SCN-DOC-{dm.upper()}-{sid_base}",
        "endpoint_method": ep.method, "endpoint_path": ep.path,
        "design_method": dm, "priority": priority, "title_hint": title_hint,
        "coverage_tags": tags, "expected_basis": expected_basis, "data_dependency": None,
        "chain_steps": None, "setup_required": False, "dependency_edge_ids": [],
        "expected_setup": [],
    }


def _fill_hard_gaps(plan: list[dict[str, Any]], endpoints: list[Endpoint], *, dep_graph: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """System hard gap fill: append missing critical scenarios without calling LLM.

    Prefers master-generated dep_graph when available (from API discovery metadata),
    falls back to local _build_dependency_graph heuristic.
    """
    from ai_apitest.domain.coverage import first_response_basis

    result = list(plan)
    covered = {(p["endpoint_method"], p["endpoint_path"], p["design_method"]) for p in result}
    has_chain = any(p["endpoint_method"] == "CHAIN" and p.get("chain_steps") for p in result)

    # Per-endpoint happy_path
    for ep in endpoints:
        if (ep.method, ep.path, "happy_path") not in covered:
            result.append({"scenario_id": f"SCN-HARD-HP-{ep.method}-{ep.path.replace('/','-').strip('-')}",
                           "endpoint_method": ep.method, "endpoint_path": ep.path,
                           "design_method": "happy_path", "priority": "P0",
                           "title_hint": f"{ep.summary or ep.path} 主流程",
                           "coverage_tags": ["happy_path"], "expected_basis": None, "data_dependency": None, "chain_steps": None})

    # CHAIN gap — prefer master dep_graph, fallback to local heuristic
    if not has_chain:
        if not (dep_graph and dep_graph.get("edges")):
            local = _build_dependency_graph(endpoints)
            if local:
                # Normalize local format to edges format
                edges = []
                for prod_key, consumer_keys in local.items():
                    pm, pp = prod_key.split(" ", 1)
                    for ck in consumer_keys:
                        cm, cp = ck.split(" ", 1)
                        edges.append({"from": {"method": pm, "path": pp}, "to": {"method": cm, "path": cp}})
                dep_graph = {"edges": edges}
        if dep_graph and dep_graph.get("edges"):
            chain_steps = []
            edges = dep_graph["edges"]
            prod = edges[0]["from"]
            chain_steps.append({"method": prod.get("method","GET"), "path": prod.get("path","/")})
            # Sort consumer steps: GET/HEAD first, PUT/PATCH middle, DELETE last
            consumers = [e.get("to", {}) for e in edges[:5]]
            METHOD_ORDER = {"GET": 0, "HEAD": 0, "POST": 1, "PUT": 2, "PATCH": 2, "DELETE": 3}
            consumers.sort(key=lambda x: METHOD_ORDER.get(str(x.get("method","GET")).upper(), 1))
            for to in consumers[:3]:
                chain_steps.append({"method": to.get("method","GET"), "path": to.get("path","/")})
            if chain_steps:
                result.append({"scenario_id": "SCN-HARD-CHAIN-001", "endpoint_method": "CHAIN", "endpoint_path": "",
                               "design_method": "dependency_chain", "priority": "P0",
                               "title_hint": "链路用例", "coverage_tags": ["dependency_chain"],
                               "expected_basis": None, "data_dependency": None, "chain_steps": chain_steps})

    # Path parameter error
    for ep in endpoints:
        path_error_basis = first_response_basis(ep, {400, 404, 422})
        if path_error_basis and "{" in ep.path and "}" in ep.path:
            if (ep.method, ep.path, "path_parameter_error") not in covered:
                result.append({"scenario_id": f"SCN-HARD-PPE-{ep.method}-{ep.path.replace('/','-').strip('-')}",
                               "endpoint_method": ep.method, "endpoint_path": ep.path,
                               "design_method": "path_parameter_error", "priority": "P1",
                               "title_hint": f"{ep.path} 路径参数异常", "coverage_tags": ["path_parameter_error"],
                               "expected_basis": path_error_basis, "data_dependency": None, "chain_steps": None})
    return result


def supplement_cases_with_config(
    root: Path,
    endpoints: list[Endpoint],
    existing_cases: list[TestCase],
    *,
    project: str = "default",
    scenario_text: str,
    max_cases: int = 5,
    stream_callback: Callable[[str], None] | None = None,
    artifact_language: str | None = None,
) -> tuple[list[TestCase], str]:
    from ai_apitest.application.quality_audit import QualityAuditService
    from ai_apitest.core.serde import case_from_dict, to_jsonable

    config = load_config(root)
    language = configured_artifact_language(config, artifact_language)
    set_current_artifact_language(language)
    scenario_text = scenario_text.strip()
    if not scenario_text:
        raise ValueError("补充场景说明不能为空")
    llm = configured_llm(root, "case_generator")
    if not is_llm_configured(llm):
        return [], "用例生成模型未配置，无法补充生成用例"
    audit_service = QualityAuditService(
        root,
        project,
        "test_cases",
        endpoints=endpoints,
        metadata={"operation": "supplement", "requested_max_cases": max_cases},
    )
    feedback: dict | None = None
    audit_retry_cases: list[TestCase] | None = None
    warnings: list[str] = []
    selected_round = None
    for round_index in range(1, audit_service.policy.max_rounds + 1):
        if audit_retry_cases is not None:
            generated = audit_retry_cases
            warnings.append(f"第 {round_index} 轮仅重试未完成的审计模型，复用上一轮补充候选")
        else:
            generated, warning = _supplement_cases_once(
                root,
                endpoints,
                existing_cases,
                project=project,
                scenario_text=scenario_text,
                max_cases=max_cases,
                stream_callback=stream_callback,
                quality_feedback=feedback,
                quality_round=round_index,
            )
            if warning:
                warnings.append(warning)
        generated = localize_generated_cases(generated, language)
        audit_round = audit_service.evaluate(
            to_jsonable(generated),
            evidence={
                "user_request": scenario_text,
                "endpoints": to_jsonable(endpoints),
                "existing_cases": to_jsonable(existing_cases),
            },
            round_index=round_index,
            hard_gate_issues=case_quality_gate_issues(generated, endpoints),
            producer_model_key=model_config_key(llm),
            stream_callback=stream_callback,
        )
        if audit_round.passed:
            selected_round = audit_round
            break
        if audit_round.hard_gate_pass and not audit_round.audit_complete:
            audit_retry_cases = generated
            feedback = None
        else:
            audit_retry_cases = None
            feedback = audit_round.repair_feedback()
    if selected_round is None:
        selected_round = audit_service.best_safe_round()
    if selected_round is None:
        audit_service.finalize(
            None,
            decision="preserve_existing_cases",
            reason="三轮后没有安全补充用例，保留现有用例集。",
        )
        return [], "；".join([*warnings, "三轮质量审计后没有安全补充用例，现有用例未被修改"])
    generated = [
        case_from_dict(item)
        for item in selected_round.artifact
        if isinstance(item, dict)
    ][:max_cases]
    audit_service.finalize(
        selected_round,
        decision="accepted" if selected_round.passed else "highest_safe_candidate",
        reason=(
            "补充用例质量审计通过。"
            if selected_round.passed
            else "三轮未达阈值，交付最高分安全补充用例。"
        ),
    )
    try:
        record_case_generation_lessons(
            root,
            project,
            generated,
            actor="quality:selected_supplement_artifact",
        )
    except Exception as exc:
        record_operational_error(
            root,
            project,
            component="context",
            operation="record_selected_supplement_lessons",
            error=exc,
        )
    score = f"{selected_round.average_score:.2f}" if selected_round.average_score is not None else "-"
    warnings.append(
        f"补充用例质量审计{'通过' if selected_round.passed else '安全兜底'}："
        f"第 {selected_round.round_index} 轮，平均分 {score}"
    )
    return generated, "；".join(dict.fromkeys(item for item in warnings if item))


def _supplement_cases_once(
    root: Path,
    endpoints: list[Endpoint],
    existing_cases: list[TestCase],
    *,
    project: str = "default",
    scenario_text: str,
    max_cases: int = 5,
    stream_callback: Callable[[str], None] | None = None,
    quality_feedback: dict | None = None,
    quality_round: int = 1,
) -> tuple[list[TestCase], str]:
    scenario_text = scenario_text.strip()
    if not scenario_text:
        raise ValueError("补充场景说明不能为空")
    llm = configured_llm(root, "case_generator")
    if not is_llm_configured(llm):
        return [], "用例生成模型未配置，无法补充生成用例"
    config = load_config(root)
    generation_mode = str(config.get("agents", {}).get("case_generation_mode") or "agent").strip().lower()
    if generation_mode == "agent":
        from ai_apitest.application.case_generation import supplement_cases_with_agent_runtime

        return supplement_cases_with_agent_runtime(
            root,
            endpoints,
            existing_cases,
            project=project,
            scenario_text=scenario_text,
            max_cases=max_cases,
            stream_callback=stream_callback,
            quality_feedback=quality_feedback,
            quality_round=quality_round,
            record_lessons=False,
        )
    if generation_mode != "legacy":
        raise ValueError(f"不支持的用例生成模式：{generation_mode}；可选值为 agent 或 legacy")
    strategy_context = case_generation_strategy_context(root, endpoints)
    context_block = ""
    try:
        context_block = build_context_bundle(
            root,
            project,
            task_type="case_generation",
            model_config=llm,
            context_config=config.get("context", {}),
            system_prompt=case_generation_system_prompt(),
            user_prompt_base=case_supplement_user_prompt_base(endpoints, existing_cases, strategy_context, scenario_text, max_cases),
            endpoints=endpoints,
            environment=str(config.get("default_environment", "")),
        ).text
    except Exception as exc:
        record_operational_error(root, project, component="context", operation="build_supplement_bundle", error=exc)
        context_block = ""
    if quality_feedback:
        context_block += (
            "\n\n## 上一轮质量审计修订要求\n"
            + json.dumps(quality_feedback, ensure_ascii=False, separators=(",", ":"), default=str)
        )
    try:
        generated = generate_supplemental_cases_with_llm(  # supplemental raises on failure, no repair needed here
            endpoints=endpoints,
            existing_cases=existing_cases,
            scenario_text=scenario_text,
            max_cases=max_cases,
            base_url=llm.get("base_url", ""),
            model=llm.get("model", ""),
            api_key_env=llm.get("api_key_env", ""),
            strategy_context=strategy_context,
            provider=llm.get("provider", "openai-compatible"),
            timeout=DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS,
            stream_callback=stream_callback,
            context_block=context_block,
        )
    except Exception as exc:
        return [], f"大模型补充用例未完成：{exc}"
    if not generated:
        return [], "大模型未生成有效补充用例"
    generated = dedupe_supplemental_cases(existing_cases, generated)
    generated = attach_cleanup_steps(attach_setup_steps(generated, endpoints), endpoints)
    if not generated:
        return [], "大模型生成的补充用例与现有用例重复，未追加"
    if _governance_enabled(root) and not _quality_audit_artifact_enabled(root, "test_cases"):
        _govern_cases(root, project, generated, endpoints)
    return generated[:max_cases], ""


def _detect_coverage_gaps(cases: list[TestCase], endpoints: list[Endpoint]) -> list[dict[str, str]]:
    """Detect critical coverage gaps. Returns structured gap dicts with method/path/missing_strategy."""
    gaps: list[dict[str, str]] = []

    # Per-endpoint happy_path check
    covered_endpoints = {(c.method, c.path) for c in cases if c.method != "CHAIN"}
    for ep in endpoints:
        if (ep.method, ep.path) not in covered_endpoints:
            gaps.append({"method": ep.method, "path": ep.path, "missing_strategy": "happy_path"})

    # Dependency chain check
    all_endpoint_keys = {(ep.method, ep.path) for ep in endpoints}

    def _step_key(s) -> tuple[str, str] | None:
        if isinstance(s, dict):
            m, p = str(s.get("method", "")).upper(), str(s.get("path", ""))
        else:
            m, p = str(getattr(s, "method", "")).upper(), str(getattr(s, "path", ""))
        return (m, p) if m and p else None

    def _is_valid_chain(case) -> bool:
        if case.method != "CHAIN":
            return False
        steps = case.steps or []
        if len(steps) < 2:
            return False
        return all(
            (key := _step_key(s)) is not None and key in all_endpoint_keys
            for s in steps
        )

    has_chain = any(_is_valid_chain(c) for c in cases)
    if not has_chain:
        dep_graph = _build_dependency_graph(endpoints)
        if dep_graph:
            gaps.append({"method": "CHAIN", "path": "", "missing_strategy": "dependency_chain"})

    # Path parameter coverage check
    for ep in endpoints:
        if "{" in ep.path and "}" in ep.path:
            path_cases = [c for c in cases if c.method == ep.method and c.path == ep.path
                          and c.design_method in ("path_parameter_error",)]
            if not path_cases:
                gaps.append({"method": ep.method, "path": ep.path, "missing_strategy": "path_parameter_error"})

    # Error response coverage check
    for ep in endpoints:
        responses = ep.responses or {}
        error_codes = [int(k) for k in responses if isinstance(k, str) and k.isdigit() and int(k) >= 400]
        if error_codes:
            error_cases = [c for c in cases if c.method == ep.method and c.path == ep.path
                           and c.design_method in ("type_error", "path_parameter_error", "auth_error", "status_code_coverage")]
            if not error_cases:
                gaps.append({"method": ep.method, "path": ep.path, "missing_strategy": "type_error"})

    return gaps


def _valid_chain_case(case: TestCase, endpoints: list[Endpoint]) -> bool:
    if case.method != "CHAIN":
        return False
    endpoint_keys = {(ep.method, ep.path) for ep in endpoints}
    steps = case.steps or []
    if len(steps) < 2:
        return False
    return all((step.method.upper(), step.path) in endpoint_keys for step in steps)


def _rule_fill_coverage_gaps(cases: list[TestCase], endpoints: list[Endpoint]) -> list[TestCase]:
    """Use local rules only for critical gaps that AI failed to fill."""
    rule_cases = generate_cases(endpoints)
    selected: list[TestCase] = []

    covered_endpoints = {(case.method, case.path) for case in cases if case.method != "CHAIN"}
    for endpoint in endpoints:
        if (endpoint.method, endpoint.path) in covered_endpoints:
            continue
        match = next(
            (
                case
                for case in rule_cases
                if case.method == endpoint.method
                and case.path == endpoint.path
                and case.design_method == "happy_path"
            ),
            None,
        )
        if match:
            selected.append(match)

    if not any(_valid_chain_case(case, endpoints) for case in cases):
        match = next((case for case in rule_cases if _valid_chain_case(case, endpoints)), None)
        if match:
            selected.append(match)

    for endpoint in endpoints:
        if "{" not in endpoint.path or "}" not in endpoint.path:
            continue
        has_path_error = any(
            case.method == endpoint.method
            and case.path == endpoint.path
            and case.design_method == "path_parameter_error"
            for case in cases
        )
        if has_path_error:
            continue
        selected.extend(
            case
            for case in rule_cases
            if case.method == endpoint.method
            and case.path == endpoint.path
            and case.design_method == "path_parameter_error"
        )

    for endpoint in endpoints:
        error_codes = [
            int(key)
            for key in endpoint.responses or {}
            if isinstance(key, str) and key.isdigit() and int(key) >= 400
        ]
        if not error_codes:
            continue
        has_error_case = any(
            case.method == endpoint.method
            and case.path == endpoint.path
            and case.design_method in {"type_error", "path_parameter_error", "auth_error", "status_code_coverage"}
            for case in cases
        )
        if has_error_case:
            continue
        match = next(
            (
                case
                for case in rule_cases
                if case.method == endpoint.method
                and case.path == endpoint.path
                and case.design_method in {"type_error", "path_parameter_error", "auth_error", "status_code_coverage"}
            ),
            None,
        )
        if match:
            selected.append(match)

    return dedupe_supplemental_cases(cases, selected)


def _build_dependency_graph(endpoints: list[Endpoint]) -> dict[str, set[str]]:
    """Detect data dependency chains by finding shared path parameters.

    When multiple endpoints share the same path param (e.g. '{email}' appears in
    /api/check/{email}, /api/messages/{email}, /api/delete/{email}), there is
    likely a data dependency chain.
    """
    import re as _re

    # Pass 1: collect param groups and potential producers
    param_groups: dict[str, list[str]] = {}
    paramless: list[tuple[str, str]] = []  # [(key, hint), ...]

    for ep in endpoints:
        key = f"{ep.method} {ep.path}"
        path_params = _re.findall(r'\{(\w+)\}', ep.path)
        if not path_params and ep.parameters:
            path_params = [p.get("name", "") for p in ep.parameters if p.get("in") == "path"]

        if path_params:
            for pn in path_params:
                param_groups.setdefault(pn.lower(), []).append(key)
        else:
            hint = ((ep.summary or "") + " " + ep.path.rsplit("/", 1)[-1]).lower()
            paramless.append((key, hint))

    # Pass 2: match producers to param groups
    graph: dict[str, set[str]] = {}
    for pn_lower, consumer_keys in param_groups.items():
        if len(consumer_keys) < 2:
            continue
        # Find best producer: paramless endpoint whose hint contains the param name
        prod_key = ""
        for key, hint in paramless:
            if pn_lower in hint:
                prod_key = key
                break
        if not prod_key and paramless:
            prod_key = paramless[0][0]  # fallback: first paramless endpoint
        if prod_key:
            graph.setdefault(prod_key, set()).update(consumer_keys)

    return graph


def _find_dependency_chains(graph: dict[str, set[str]]) -> list[list[str]]:
    """Find linear chains from producers to terminal consumers."""
    chains = []
    visited = set()

    def walk(node, path):
        if node in path:
            chains.append(path + [node])
            return
        new_path = path + [node]
        if node not in graph or not graph[node]:
            if len(new_path) >= 2:
                chains.append(new_path)
            return
        for next_node in graph[node]:
            walk(next_node, new_path)

    for start in graph:
        if start not in visited:
            walk(start, [])
            visited.add(start)
    return sorted(chains, key=len, reverse=True)


def _fill_coverage_gaps(
    root: Path, project: str,
    endpoints: list[Endpoint],
    existing_cases: list[TestCase],
    gaps: list[str],
    llm: dict,
    strategy_context: dict,
) -> tuple[list[TestCase], str]:
    """Targeted supplementary generation. Returns (cases, diagnostics)."""
    import json
    from ai_apitest.ai.case_generation import generate_supplemental_cases_with_llm

    endpoint_keys = {(ep.method, ep.path) for ep in endpoints}
    gap_text = "\n".join(f"- {g}" for g in gaps)
    existing_summary = [
        {"id": c.id, "title": c.title, "method": c.method, "path": c.path,
         "design_method": c.design_method, "tags": c.tags}
        for c in existing_cases
    ]

    scenario = f"""系统检测到以下覆盖缺口，请只补充缺口对应的用例，不要重新生成已有覆盖的用例。

## 检测到的缺口
{gap_text}

## 已生成用例摘要（共 {len(existing_cases)} 条）
{json.dumps(existing_summary, ensure_ascii=False, indent=2)[:3000]}

## 补充要求
- 只补充缺口列表中列出的缺失场景。
- 不要重复生成已覆盖的接口和策略。
- 特别是：如果缺口提到缺少链路用例，必须生成 method=CHAIN 的用例，steps 中每个 step 的 (method, path) 必须来自当前接口列表中的真实接口。
- 如果缺口提到缺少 path_parameter_error，必须为对应接口生成路径参数异常用例。
- 每条补充用例的 tags 中只包含实际覆盖的策略名。"""

    try:
        raw_generated = generate_supplemental_cases_with_llm(
            endpoints=endpoints,
            existing_cases=existing_cases,
            scenario_text=scenario,
            max_cases=len(gaps) * 3,
            base_url=llm.get("base_url", ""),
            model=llm.get("model", ""),
            api_key_env=llm.get("api_key_env", ""),
            strategy_context=strategy_context,
            provider=llm.get("provider", "openai-compatible"),
            timeout=DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        raise ValueError(f"补生成模型调用失败：{exc}") from exc

    # ── Structured diagnostics ──
    chain_candidates = [c for c in raw_generated if c.method == "CHAIN"]
    diag_parts = [f"补生成校验后保留 {len(raw_generated)} 条"]
    if chain_candidates:
        chain_diag = []
        for c in chain_candidates:
            steps = c.steps or []
            if not steps:
                chain_diag.append(f"{c.id} 无 steps")
            elif len(steps) < 2:
                chain_diag.append(f"{c.id} 仅 {len(steps)} 个 step（需 ≥2）")
            else:
                invalid = 0
                for s in steps:
                    if isinstance(s, dict):
                        m, p = str(s.get("method", "")).upper(), str(s.get("path", ""))
                    else:
                        m, p = str(getattr(s, "method", "")).upper(), str(getattr(s, "path", ""))
                    if not m or not p or (m, p) not in endpoint_keys:
                        invalid += 1
                if invalid:
                    chain_diag.append(f"{c.id} 有 {invalid}/{len(steps)} 个 step 不在接口列表")
                else:
                    chain_diag.append(f"{c.id} {len(steps)} steps 全部有效")
        diag_parts.append(f"其中 CHAIN {len(chain_candidates)} 条：{'；'.join(chain_diag)}")
    diag_parts.append(f"validate_cases 后保留 {len(raw_generated)} 条待去重")

    return raw_generated, "；".join(diag_parts)


def case_generation_system_prompt() -> str:
    from ai_apitest.ai.case_generation import system_prompt

    return system_prompt()


def case_generation_user_prompt_base(endpoints: list[Endpoint], strategy_context: dict) -> str:
    from ai_apitest.ai.case_generation import user_prompt

    return user_prompt(endpoints, strategy_context, context_block="")


def case_supplement_user_prompt_base(
    endpoints: list[Endpoint],
    existing_cases: list[TestCase],
    strategy_context: dict,
    scenario_text: str,
    max_cases: int,
) -> str:
    from ai_apitest.ai.case_generation import supplement_user_prompt

    return supplement_user_prompt(
        endpoints=endpoints,
        existing_cases=existing_cases,
        scenario_text=scenario_text,
        max_cases=max_cases,
        strategy_context=strategy_context,
        context_block="",
    )


def dedupe_supplemental_cases(existing_cases: list[TestCase], generated_cases: list[TestCase]) -> list[TestCase]:
    seen = {case_similarity_key(case) for case in existing_cases}
    result: list[TestCase] = []
    for case in generated_cases:
        key = case_similarity_key(case)
        if key in seen:
            continue
        seen.add(key)
        result.append(case)
    return result


def case_similarity_key(case: TestCase) -> tuple:
    import json
    from ai_apitest.core.serde import to_jsonable

    return (
        case.method.upper(),
        case.path,
        case.design_method,
        normalize_text(case.title),
        json.dumps(
            {
                "path_params": case.path_params,
                "query": case.query,
                "body": case.body,
                "assertions": to_jsonable(case.assertions),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ),
    )


def normalize_text(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def analysis_artifact_from_text(value: str) -> dict[str, object]:
    import re

    from ai_apitest.context.extraction import strip_context_blocks

    def block(name: str) -> list[dict]:
        match = re.search(
            rf"```{re.escape(name)}\s*(.*?)\s*```",
            str(value or ""),
            flags=re.S,
        )
        if not match:
            return []
        try:
            parsed = json.loads(match.group(1))
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []

    return {
        "markdown": strip_context_blocks(str(value or "")).strip(),
        "lessons": block("iatest-lessons-json"),
        "known_issue_updates": block("iatest-known-issues-json"),
    }


def report_quality_gate_issues(
    markdown: str,
    results: list[CaseResult],
    documented_endpoints: list[Endpoint] | None = None,
    artifact_language: str | None = None,
) -> list[dict]:
    headings = analysis_headings(
        artifact_language
        or detect_analysis_language(markdown)
    )
    required = tuple(
        f"### {headings[key]}"
        for key in (
            "one_line_conclusion",
            "result_overview",
            "main_issues",
            "recommended_actions",
            "notes",
        )
    )
    issues: list[dict] = []
    for heading in required:
        if heading not in markdown:
            issues.append(
                {
                    "severity": "high",
                    "dimension": "completeness",
                    "location": heading,
                    "evidence": "报告输出契约要求包含该固定章节。",
                    "reason": "报告缺少必需章节。",
                    "suggested_fix": f"补充 {heading}，内容必须引用执行事实。",
                }
            )
    failure_review_heading = f"### {headings['failure_review']}"
    if any(item.status == "failed" for item in results) and failure_review_heading not in markdown:
        issues.append(
            {
                "severity": "critical",
                "dimension": "completeness",
                "location": failure_review_heading,
                "evidence": "本次执行存在 failed 用例。",
                "reason": "存在失败时报告必须给出基于证据的失败复核。",
                "suggested_fix": "按失败类别补充请求、响应和断言证据。",
            }
        )
    from ai_apitest.application.result_analysis import result_rate_limit_wait_ms

    report_lines = markdown.splitlines()
    for result in results:
        wait_ms = result_rate_limit_wait_ms(result)
        if wait_ms < 1000 or wait_ms * 2 < result.elapsed_ms:
            continue
        misleading_lines = [
            line
            for line in report_lines
            if result.case_id in line
            and any(term in line for term in ("性能最慢", "性能瓶颈", "性能风险", "监控性能"))
            and not any(term in line for term in ("限流", "框架等待", "主动等待"))
        ]
        if misleading_lines:
            issues.append(
                {
                    "severity": "high",
                    "dimension": "reliability",
                    "location": result.case_id,
                    "evidence": (
                        f"该用例总耗时 {result.elapsed_ms}ms，其中框架主动限流等待 {wait_ms}ms；"
                        f"报告写为：{misleading_lines[0][:300]}"
                    ),
                    "reason": "把框架主动限流等待误写成接口性能风险。",
                    "suggested_fix": "显式拆分限流等待与实际执行耗时，并移除无证据的服务端性能结论。",
                }
            )
    endpoint_map = {
        (endpoint.method.upper(), endpoint.path): endpoint
        for endpoint in documented_endpoints or []
    }
    for result in results:
        if result.status != "failed":
            continue
        endpoint = endpoint_map.get((result.method.upper(), result.path))
        basis = documented_assertion_basis(result, endpoint)
        if not basis:
            continue
        misleading_lines = [
            line
            for line in report_lines
            if result.case_id in line
            and any(term in line for term in ("调整断言", "更新断言", "断言问题", "用例预期问题"))
            and not any(term in line for term in ("不是断言问题", "非断言问题", "不得调整断言"))
        ]
        if misleading_lines:
            issues.append(
                {
                    "severity": "high",
                    "dimension": "reliability",
                    "location": result.case_id,
                    "evidence": basis,
                    "reason": "失败断言有可信文档契约依据，不能直接归因为用例断言错误。",
                    "suggested_fix": "归因为接口实现与文档契约不一致；建议先确认应修实现还是修文档。",
                }
            )
    return issues


def documented_assertion_basis(result: CaseResult, endpoint: Endpoint | None) -> str:
    if endpoint is None or not result.assertion_errors:
        return ""
    failure_text = "\n".join(result.assertion_errors)
    for rule in endpoint.business_rules:
        if not isinstance(rule, dict):
            continue
        rule_text = str(rule.get("title") or rule.get("description") or "").strip()
        if rule_text and rule_text not in result.title:
            continue
        expected = rule.get("expected")
        assertions = expected.get("assertions") if isinstance(expected, dict) else None
        for assertion in assertions if isinstance(assertions, list) else []:
            if not isinstance(assertion, dict):
                continue
            expected_value = assertion.get("expected")
            if isinstance(expected_value, str) and expected_value and expected_value in failure_text:
                source = str(rule.get("source_evidence") or rule_text).strip()
                return (
                    f"文档规则 {rule.get('rule_id') or '<unknown>'} 明确预期 {expected_value!r}"
                    + (f"；原文证据：{source}" if source else "")
                )
    return ""


def experience_quality_gate_issues(value: dict[str, object]) -> list[dict]:
    lessons = value.get("lessons")
    updates = value.get("known_issue_updates")
    issues: list[dict] = []
    if not isinstance(lessons, list) or not isinstance(updates, list):
        return [
            {
                "severity": "critical",
                "dimension": "reliability",
                "location": "经验候选结构",
                "evidence": "经验输出契约要求 lessons 和 known_issue_updates 均为数组。",
                "reason": "经验候选结构无效。",
                "suggested_fix": "重新提交结构化经验数组。",
            }
        ]
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(lessons):
        if not isinstance(item, dict) or not str(item.get("title") or "").strip() or not str(item.get("content") or "").strip():
            issues.append(
                {
                    "severity": "high",
                    "dimension": "completeness",
                    "location": f"lessons[{index}]",
                    "evidence": "经验候选必须同时包含 title 和 content。",
                    "reason": "经验候选缺少必要内容。",
                    "suggested_fix": "补齐可追溯标题和事实内容，无法补齐则删除该候选。",
                }
            )
            continue
        key = (
            str(item.get("title") or "").strip(),
            str(item.get("content") or "").strip(),
        )
        if key in seen:
            issues.append(
                {
                    "severity": "high",
                    "dimension": "non_redundancy",
                    "location": f"lessons[{index}]",
                    "evidence": "同一批经验存在完全相同的标题和内容。",
                    "reason": "经验候选重复。",
                    "suggested_fix": "删除重复候选。",
                }
            )
        seen.add(key)
    return issues


def build_ai_report_analysis(
    root: Path,
    project: str,
    results: list[CaseResult],
    *,
    artifact_language: str | None = None,
) -> str:
    llm = configured_llm(root, "analyzer")
    if not is_llm_configured(llm):
        return ""
    config = load_config(root)
    language = configured_artifact_language(config, artifact_language)
    set_current_artifact_language(language)
    trend_ctx = _build_trend_context(root, project)
    analysis_mode = str(config.get("agents", {}).get("report_analysis_mode") or "agent").strip().lower()
    from ai_apitest.application.quality_audit import QualityAuditService
    from ai_apitest.application.result_analysis import (
        documented_contract_evidence,
        documented_endpoints_for_results,
        render_analysis_artifact,
    )
    from ai_apitest.core.serde import to_jsonable
    from ai_apitest.report.markdown import render_report_markdown

    documented_endpoints = documented_endpoints_for_results(root, project, results)
    contract_evidence = documented_contract_evidence(results, documented_endpoints)
    report_audit = QualityAuditService(
        root,
        project,
        "test_report",
        endpoints=documented_endpoints,
        metadata={"result_count": len(results)},
    )
    experience_audit = QualityAuditService(
        root,
        project,
        "experiences",
        endpoints=documented_endpoints,
        metadata={"source": "result_analysis", "result_count": len(results)},
    )
    report_selected = None
    experience_selected = None
    quality_feedback: dict | None = None
    audit_retry_analysis: str | None = None
    analysis_error: Exception | None = None
    evidence = {
        "execution_results": to_jsonable(results),
        "documented_endpoint_contracts": contract_evidence,
        "trend_context": trend_ctx,
        "immutable_summary": {
            "total": len(results),
            "passed": sum(item.status == "passed" for item in results),
            "failed": sum(item.status == "failed" for item in results),
            "skipped": sum(item.status == "skipped" for item in results),
        },
    }
    try:
        for round_index in range(1, report_audit.policy.max_rounds + 1):
            try:
                if audit_retry_analysis is not None:
                    analysis = audit_retry_analysis
                elif analysis_mode == "agent":
                    from ai_apitest.application.result_analysis import analyze_results_with_agent_runtime

                    analysis = analyze_results_with_agent_runtime(
                        root,
                        project,
                        results,
                        llm,
                        trend_context=trend_ctx,
                        quality_feedback=quality_feedback,
                        quality_round=round_index,
                    )
                elif analysis_mode == "legacy":
                    context_block = ""
                    try:
                        context_block = build_context_bundle(
                            root,
                            project,
                            task_type="report_analysis",
                            model_config=llm,
                            context_config=config.get("context", {}),
                            system_prompt="",
                            user_prompt_base=report_analysis_user_prompt_base(root, project, results),
                            endpoints=documented_endpoints,
                            environment=str(config.get("default_environment", "")),
                        ).text
                    except Exception as exc:
                        record_operational_error(
                            root,
                            project,
                            component="context",
                            operation="build_report_bundle",
                            error=exc,
                        )
                    if quality_feedback:
                        context_block += (
                            "\n\n## 上一轮质量审计修订要求\n"
                            + json.dumps(
                                quality_feedback,
                                ensure_ascii=False,
                                separators=(",", ":"),
                                default=str,
                            )
                        )
                    analysis = analyze_results_with_llm(
                        results,
                        project,
                        llm,
                        context_block=context_block,
                        trend_context=trend_ctx,
                    )
                else:
                    raise ValueError(f"不支持的报告分析模式：{analysis_mode}")
            except Exception as exc:
                analysis_error = exc
                record_operational_error(
                    root,
                    project,
                    component="report_analysis",
                    operation="produce_quality_round",
                    error=exc,
                    detail={"quality_round": round_index},
                )
                break
            artifact = analysis_artifact_from_text(analysis)
            full_report = render_report_markdown(
                results,
                project=project,
                analysis=str(artifact["markdown"]),
                artifact_language=language,
            )
            if report_selected is None:
                report_round = report_audit.evaluate(
                    {
                        "analysis_markdown": artifact["markdown"],
                        "full_report_markdown": full_report,
                    },
                    evidence=evidence,
                    round_index=round_index,
                    hard_gate_issues=report_quality_gate_issues(
                        artifact["markdown"],
                        results,
                        documented_endpoints,
                        language,
                    ),
                    producer_model_key=model_config_key(llm),
                )
                if report_round.passed:
                    report_selected = report_round
            else:
                report_round = report_selected
            experience_artifact = {
                "lessons": artifact["lessons"],
                "known_issue_updates": artifact["known_issue_updates"],
            }
            if experience_selected is None:
                selected_report_artifact = (
                    report_selected.artifact.get("analysis_markdown", "")
                    if report_selected is not None and isinstance(report_selected.artifact, dict)
                    else artifact["markdown"]
                )
                experience_round = experience_audit.evaluate(
                    experience_artifact,
                    evidence={
                        **evidence,
                        "selected_report_candidate": selected_report_artifact,
                    },
                    round_index=round_index,
                    hard_gate_issues=experience_quality_gate_issues(experience_artifact),
                    producer_model_key=model_config_key(llm),
                )
                if experience_round.passed:
                    experience_selected = experience_round
            else:
                experience_round = experience_selected
            if report_selected is not None and experience_selected is not None:
                break
            audit_incomplete = (
                report_round.hard_gate_pass
                and not report_round.audit_complete
            ) or (
                experience_round.hard_gate_pass
                and not experience_round.audit_complete
            )
            if audit_incomplete:
                audit_retry_analysis = analysis
                quality_feedback = None
            else:
                audit_retry_analysis = None
                quality_feedback = {}
                if report_selected is None:
                    quality_feedback["report"] = report_round.repair_feedback()
                if experience_selected is None:
                    quality_feedback["experiences"] = experience_round.repair_feedback()
                quality_feedback = quality_feedback or None
        if report_selected is None:
            report_selected = report_audit.best_safe_round()
        if experience_selected is None:
            experience_selected = experience_audit.best_safe_round()
        report_audit.finalize(
            report_selected,
            decision=(
                "accepted"
                if report_selected and report_selected.passed
                else ("highest_safe_candidate" if report_selected else "factual_report_fallback")
            ),
            reason=(
                "报告质量审计通过。"
                if report_selected and report_selected.passed
                else (
                    (
                        f"分析修订提前结束（{analysis_error}），选择最高分安全报告分析。"
                        if analysis_error
                        else "三轮未达阈值，选择最高分安全报告分析。"
                    )
                    if report_selected
                    else "没有安全 AI 报告分析，仅交付框架事实报告。"
                )
            ),
        )
        experience_audit.finalize(
            experience_selected,
            decision=(
                "accepted"
                if experience_selected and experience_selected.passed
                else ("pending_highest_safe" if experience_selected else "no_experience_persisted")
            ),
            reason=(
                "经验候选质量审计通过。"
                if experience_selected and experience_selected.passed
                else (
                    (
                        f"分析修订提前结束（{analysis_error}），仅保留最高分安全经验为待人工确认。"
                        if analysis_error
                        else "三轮未达阈值，仅保留最高分安全经验为待人工确认。"
                    )
                    if experience_selected
                    else "没有安全经验候选，不写入经验库。"
                )
            ),
        )
        if report_selected is None:
            return ""
        selected_experiences = (
            experience_selected.artifact
            if experience_selected is not None
            else {"lessons": [], "known_issue_updates": []}
        )
        selected_analysis = render_analysis_artifact(
            {
                "markdown": str(
                    report_selected.artifact.get("analysis_markdown", "")
                    if isinstance(report_selected.artifact, dict)
                    else report_selected.artifact
                ),
                "lessons": selected_experiences.get("lessons", []),
                "known_issue_updates": selected_experiences.get("known_issue_updates", []),
            }
        )
        visible, _summary = process_report_analysis(
            root,
            project,
            selected_analysis,
            source=f"report_analysis:{project}",
            environment=str(config.get("default_environment", "")),
            llm=llm,
        )
        # Governance is deliberately decoupled from the synchronous report
        # path. One execution produces one analyzer call; conflict/candidate
        # governance can be run explicitly, or opted back into inline mode.
        if bool(config.get("governance", {}).get("inline_after_report", False)):
            review_context_conflicts(root, project)
            if (
                _governance_enabled(root)
                and not _quality_audit_artifact_enabled(root, "test_report")
                and should_review_report(results, visible, language)
            ):
                _govern_report(root, project, results, analysis, visible)
            if _governance_enabled(root):
                _govern_experience_candidates(root, project)
        return visible
    except Exception as exc:
        try:
            if (report_audit.repository.get_session(report_audit.session_id) or {}).get("status") == "running":
                report_audit.finalize(None, decision="failed", reason=str(exc))
            if (experience_audit.repository.get_session(experience_audit.session_id) or {}).get("status") == "running":
                experience_audit.finalize(None, decision="failed", reason=str(exc))
        except Exception:
            pass
        record_operational_error(root, project, component="report_analysis", operation="analyze_results", error=exc)
        return f"大模型分析失败：{exc}"


def build_ai_report_bundle(
    root: Path,
    project: str,
    results: list[CaseResult],
    *,
    artifact_language: str | None = None,
) -> tuple[str, str]:
    """One analyzer run supplies both failure attribution and the full report."""
    if not results:
        return "", ""
    return "", build_ai_report_analysis(
        root,
        project,
        results,
        artifact_language=artifact_language,
    )


def review_context_conflicts(root: Path, project: str) -> None:
    """Master model reviews and resolves context conflicts (governance: context_governance)."""
    if not _governance_feature_enabled(root, "review_experience_candidates"):
        return
    master = configured_llm(root, "master")
    if not is_llm_configured(master):
        return
    try:
        conflicts = conflict_pairs(root, project, limit=20)
        if not conflicts:
            return
        governance_mode = str(load_config(root).get("agents", {}).get("governance_mode") or "agent").strip().lower()
        if governance_mode == "agent":
            from ai_apitest.application.governance import review_conflicts_with_agent_runtime

            reviews = review_conflicts_with_agent_runtime(
                root,
                project,
                conflicts=conflicts,
                llm=master,
            )
        elif governance_mode == "legacy":
            reviews = review_conflicts_with_llm(conflicts, master)
        else:
            raise ValueError(f"不支持的治理模式：{governance_mode}")
        apply_conflict_review(root, project, reviews, actor="ai:master")
        _audit_governance(root, project, "conflict_reviewed", {
            "conflict_count": len(conflicts), "decisions": [r.get("decision", "") for r in reviews],
        }, task="context_governance")
    except Exception as exc:
        record_operational_error(root, project, component="governance", operation="review_context_conflicts", error=exc)
        return


def run_pending_context_governance(root: Path, project: str) -> None:
    """Explicitly process pending conflict and experience governance work."""
    review_context_conflicts(root, project)
    if _governance_enabled(root):
        _govern_experience_candidates(root, project)


def endpoints_from_results(results: list[CaseResult]) -> list[Endpoint]:
    seen: set[tuple[str, str]] = set()
    endpoints: list[Endpoint] = []
    for result in results:
        key = (result.method, result.path)
        if key in seen or result.method == "CHAIN":
            continue
        seen.add(key)
        endpoints.append(Endpoint(method=result.method, path=result.path))
    return endpoints


def _build_trend_context(root, project: str) -> dict:
    """Build structured trend context for AI analysis. Shared by web rendering and LLM prompts."""
    try:
        from ai_apitest.context.storage import trend_overview
        trend_data = trend_overview(root, project, limit=3)
        runs = trend_data.get("runs", [])
        if len(runs) < 2:
            return {}
        latest = runs[0]
        previous = runs[1]
        latest_eps = {f"{e.get('method','')} {e.get('path','')}": e for e in latest.get("endpoint_metrics", [])}
        prev_eps = {f"{e.get('method','')} {e.get('path','')}": e for e in previous.get("endpoint_metrics", [])}
        endpoint_deltas = []
        for key, ep in latest_eps.items():
            prev_ep = prev_eps.get(key, {})
            ep_failed = int(ep.get("failed", 0))
            prev_failed = int(prev_ep.get("failed", 0)) if prev_ep else 0
            if ep_failed != prev_failed:
                change = "new_failure" if prev_failed == 0 and ep_failed > 0 else ("recovered" if prev_failed > 0 and ep_failed == 0 else "persistent")
                endpoint_deltas.append({"endpoint": key, "current_failed": ep_failed, "previous_failed": prev_failed, "change": change})
        from ai_apitest.context.storage import assess_trend_comparability
        comp = assess_trend_comparability(latest, previous)
        return {
            "previous_run": {"total_cases": previous.get("total_cases"), "passed_cases": previous.get("passed_cases"), "failed_cases": previous.get("failed_cases"), "pass_rate": previous.get("pass_rate")},
            "run_scope": latest.get("run_scope", "") or previous.get("run_scope", ""),
            "case_overlap": comp.get("jaccard"),
            "total_cases_latest": latest.get("total_cases"),
            "total_cases_previous": previous.get("total_cases"),
            "comparable": comp["comparable"],
            "confidence": comp["confidence"],
            "endpoint_deltas": endpoint_deltas[:10],
            "note": comp["note"],
        }
    except Exception as exc:
        record_operational_error(root, project, component="trend", operation="build_analysis_context", error=exc)
        return {}


def report_analysis_user_prompt_base(root, project: str, results: list[CaseResult]) -> str:
    import json
    from ai_apitest.ai.prompting import render_prompt
    from ai_apitest.ai.result_analysis import case_summary
    from ai_apitest.core.serde import to_jsonable

    total = len(results)
    passed = sum(1 for item in results if item.status == "passed")
    failed = sum(1 for item in results if item.status == "failed")

    # Build trend context for richer AI analysis
    trend_context = _build_trend_context(root, project)

    payload = {
        "project": project,
        "summary": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "success_rate": f"{(passed / total * 100):.1f}%" if total else "-",
        },
        "failed_cases": [case_summary(item) for item in results if item.status == "failed"],
        "slowest_cases": [case_summary(item) for item in sorted(results, key=lambda item: item.elapsed_ms, reverse=True)[:5]],
        "results": [to_jsonable(item) for item in results],
        "trend_context": trend_context,
    }
    from ai_apitest.core.artifact_language import output_language_instruction

    headings = analysis_headings()
    return render_prompt(
        "result_analysis.md",
        {
            "CONTEXT_BLOCK": "",
            "RESULTS_JSON": json.dumps(payload, ensure_ascii=False, indent=2),
            "OUTPUT_LANGUAGE_INSTRUCTION": output_language_instruction(),
            "ONE_LINE_CONCLUSION": headings["one_line_conclusion"],
            "RESULT_OVERVIEW": headings["result_overview"],
            "MAIN_ISSUES": headings["main_issues"],
            "RECOMMENDED_ACTIONS": headings["recommended_actions"],
            "NOTES": headings["notes"],
            "FAILURE_REVIEW": headings["failure_review"],
        },
    )


def build_ai_failure_review(root: Path, project: str, results: list[CaseResult]) -> str:
    if not any(item.status == "failed" for item in results):
        return ""
    llm = configured_llm(root, "analyzer")
    if not is_llm_configured(llm):
        return ""
    try:
        mode = str(load_config(root).get("agents", {}).get("report_analysis_mode") or "agent").strip().lower()
        if mode == "agent":
            from ai_apitest.application.result_analysis import review_failed_results_with_agent_runtime

            return review_failed_results_with_agent_runtime(root, project, results, llm)
        if mode == "legacy":
            return review_failed_results_with_llm(results, project, llm)
        raise ValueError(f"不支持的报告分析模式：{mode}")
    except Exception as exc:
        return f"AI 复核失败：{exc}"


def should_review_report(
    results: list[CaseResult],
    visible_report: str,
    artifact_language: str | None = None,
) -> bool:
    headings = analysis_headings(
        artifact_language
        or detect_analysis_language(visible_report)
    )
    required_sections = tuple(
        f"### {headings[key]}"
        for key in (
            "one_line_conclusion",
            "result_overview",
            "main_issues",
            "recommended_actions",
            "notes",
        )
    )
    if any(section not in visible_report for section in required_sections):
        return True
    if (
        any(item.status == "failed" for item in results)
        and f"### {headings['failure_review']}" not in visible_report
    ):
        return True
    return False


# ═══════════════════════════════════════════════════════════════
# Master governance layer
# ═══════════════════════════════════════════════════════════════

def _governance_enabled(root: Path) -> bool:
    config = load_config(root)
    governance = config.get("governance", {})
    return bool(governance.get("enabled", True))


def _quality_audit_artifact_enabled(root: Path, artifact_type: str) -> bool:
    config = load_config(root).get("quality_audit", {})
    return bool(config.get("enabled", True)) and bool(
        config.get("artifacts", {}).get(artifact_type, True)
    )


def _governance_feature_enabled(root: Path, feature: str) -> bool:
    if not _governance_enabled(root):
        return False
    governance = load_config(root).get("governance", {})
    return bool(governance.get(feature, True))


def _govern_report(root: Path, project: str, results: list[CaseResult], analysis_text: str, visible_report: str) -> None:
    """Master reviews analyzer's report for consistency, specificity, and misattribution (governance: report_review)."""
    if not _governance_feature_enabled(root, "review_reports"):
        return
    master = configured_llm(root, "master")
    if not is_llm_configured(master):
        return
    import json as _json

    total = len(results)
    passed = sum(1 for r in results if r.status == "passed")
    failed = sum(1 for r in results if r.status == "failed")
    rate_text = f"{(passed/total*100):.1f}%" if total else "N/A"
    failed_brief = [{"case_id": r.case_id, "title": r.title, "error": (r.error or "")[:200]} for r in results if r.status == "failed"][:10]

    prompt = f"""你是测试策略审查者，不是用例生成者。请审查下面这份由 analyzer 模型生成的测试报告，输出严格的 JSON。

## 审查标准
1. 报告结论是否和实际执行结果一致
2. 建议是否具体、可操作
3. 是否误把断言问题说成接口缺陷
4. 是否结合了趋势上下文
5. 提取的经验候选是否可信

## 执行摘要
- 总计 {total} 条，通过 {passed}，失败 {failed}
- 通过率 {rate_text}

## 失败用例（最多10条）
{_json.dumps(failed_brief, ensure_ascii=False, indent=2)}

## 报告正文（analyzer 输出）
{analysis_text[:3000]}

## 严格 JSON 输出格式
```json
{{
  "verdict": "approved|needs_revision|rejected",
  "confidence": "high|medium|low",
  "issues": [
    {{"severity": "error|warn", "location": "summary|suggestion|attribution", "detail": "..."}}
  ],
  "suggestions": ["具体改进建议..."]
}}
```

只输出 JSON，不要其他文字。"""
    try:
        governance_mode = str(load_config(root).get("agents", {}).get("governance_mode") or "agent").strip().lower()
        if governance_mode == "agent":
            from ai_apitest.application.governance import review_quality_with_agent_runtime

            parsed = review_quality_with_agent_runtime(
                root,
                project,
                review_type="report_review",
                evidence={
                    "summary": {"total": total, "passed": passed, "failed": failed, "pass_rate": rate_text},
                    "failed_cases": failed_brief,
                    "analysis": analysis_text[:5000],
                    "visible_report": visible_report[:5000],
                },
                endpoints=endpoints_from_results(results),
                llm=master,
            )
        elif governance_mode == "legacy":
            from ai_apitest.ai.provider import chat_completion

            response = chat_completion(
                base_url=master.get("base_url", ""), model=master.get("model", ""),
                api_key_env=master.get("api_key_env", ""), provider=master.get("provider", "openai-compatible"),
                messages=[{"role": "user", "content": prompt}],
            ).strip()
            parsed = _governance_parse_json(response, response_type="report_review", base_url=master.get("base_url",""), model=master.get("model",""), api_key_env=master.get("api_key_env",""), provider=master.get("provider","openai-compatible"))
        else:
            raise ValueError(f"不支持的治理模式：{governance_mode}")
        if parsed:
            _audit_governance(root, project, "report_reviewed", {
                "verdict": parsed.get("verdict"), "confidence": parsed.get("confidence"),
                "issue_count": len(parsed.get("issues", [])),
            }, task="report_review")
    except Exception as exc:
        record_operational_error(root, project, component="governance", operation="review_report", error=exc)


def _govern_experience_candidates(root: Path, project: str) -> None:
    """Master reviews pending experience candidates for semantic quality (governance: context_governance)."""
    if not _governance_feature_enabled(root, "review_experience_candidates"):
        return
    master = configured_llm(root, "master")
    if not is_llm_configured(master):
        return
    import json as _json
    from ai_apitest.context.storage import list_lessons

    # Query recent pending candidates
    governance = load_config(root).get("governance", {})
    max_candidates = max(1, min(100, int(governance.get("max_candidates_per_batch", 10))))
    candidates_raw = list_lessons(root, project, lifecycle_status="pending", limit=max_candidates)
    if not candidates_raw:
        return
    candidates = [{k: v for k, v in c.items() if k in ("id", "title", "content", "lesson_type", "confidence", "scope")}
                  for c in candidates_raw]

    # Get potentially similar existing lessons (semantic_key match)
    config = load_config(root)
    existing = list_lessons(root, project, limit=10, lesson_type="", scope="")
    existing_brief = [{"id": e.get("id"), "title": e.get("title"), "content": (e.get("content") or "")[:200],
                       "lesson_type": e.get("lesson_type"), "lifecycle": e.get("lifecycle_status")}
                      for e in existing if str(e.get("id")) not in {c.get("id") for c in candidates}][:10]

    prompt = f"""你是上下文治理审查者。请审查以下经验候选，判断每条候选是否应当入库。

## 审查标准
1. 是否和已有经验重复（表达不同但含义相同 → merge）
2. 是否和已有经验冲突
3. 内容是否足够具体、可复用
4. 置信度评估

## 候选经验
{_json.dumps(candidates, ensure_ascii=False, indent=2)}

## 已有相关经验（语义相近）
{_json.dumps(existing_brief, ensure_ascii=False, indent=2) if existing_brief else "无"}

## 严格 JSON 输出格式
```json
{{
  "decisions": [
    {{
      "candidate_id": "les-xxx",
      "decision": "create|merge|reject|needs_review|conflict",
      "target_lesson_id": null,
      "confidence": "high|medium|low",
      "reason": "..."
    }}
  ]
}}
```

只输出 JSON，不要其他文字。"""
    try:
        governance_mode = str(config.get("agents", {}).get("governance_mode") or "agent").strip().lower()
        if governance_mode == "agent":
            from ai_apitest.application.governance import govern_context_candidates_with_agent_runtime

            decisions = govern_context_candidates_with_agent_runtime(
                root,
                project,
                candidates=candidates,
                existing=existing_brief,
                llm=master,
            )
        elif governance_mode == "legacy":
            from ai_apitest.ai.provider import chat_completion

            response = chat_completion(
                base_url=master.get("base_url", ""), model=master.get("model", ""),
                api_key_env=master.get("api_key_env", ""), provider=master.get("provider", "openai-compatible"),
                messages=[{"role": "user", "content": prompt}],
            ).strip()
            parsed = _governance_parse_json(response, response_type="experience_governance", base_url=master.get("base_url",""), model=master.get("model",""), api_key_env=master.get("api_key_env",""), provider=master.get("provider","openai-compatible"))
            decisions = parsed.get("decisions", []) if parsed else []
        else:
            raise ValueError(f"不支持的治理模式：{governance_mode}")
        if decisions:
            for decision in decisions:
                d_confidence = str(decision.get("confidence", "low"))
                if d_confidence == "low" and bool(governance.get("low_confidence_requires_manual", True)):
                    decision["decision"] = "needs_review"
                d_type = decision.get("decision")
                cid = decision.get("candidate_id", "")
                tid = decision.get("target_lesson_id", "")
                if d_type == "merge" and tid:
                    _apply_merge(root, project, cid, tid)
                elif d_type == "reject":
                    _apply_reject(root, project, cid)
                elif d_type == "create":
                    _apply_confirm(root, project, cid)
                elif d_type == "needs_review":
                    _apply_needs_review(root, project, cid)
                elif d_type == "conflict" and tid:
                    _apply_conflict(root, project, cid, tid)
            _audit_governance(root, project, "experience_governed", {
                "candidates_in": len(candidates), "decisions": decisions,
            }, task="context_governance")
    except Exception as exc:
        record_operational_error(root, project, component="governance", operation="review_experience_candidates", error=exc)


def _review_coverage_plan(root: Path, project: str, plan: list[dict[str, Any]], endpoints: list[Endpoint]) -> None:
    """Master reviews the coverage plan (lightweight, high-ROI)."""
    if not _governance_feature_enabled(root, "review_cases"):
        return
    master = configured_llm(root, "master")
    if not is_llm_configured(master):
        return
    import json as _json

    endpoint_briefs = [{"method": e.method, "path": e.path, "summary": e.summary or "",
                         "responses": {str(k): str(v) for k, v in (e.responses or {}).items()},
                         "has_request_body": bool(e.request_body)}
                        for e in endpoints]
    prompt = f"""你是测试策略审查者。请审查以下覆盖计划，只输出结构化 JSON，不要解释。

## 审查标准
1. 是否漏掉核心业务场景（如 clear 数据依赖链未生成 CHAIN）
2. 是否有明显重复场景
3. status_code_coverage 是否可和 happy_path 合并
4. path_parameter_error 是否机械生成（空路径有路由行为时不生成空值）
5. CHAIN chain_steps 是否合理
6. 是否有不符合接口文档的预期

## 接口定义
{_json.dumps(endpoint_briefs, ensure_ascii=False, indent=2)}

## 覆盖计划（{len(plan)} 条）
{_json.dumps(plan[:30], ensure_ascii=False, indent=2)}

## 严格 JSON 输出格式
```json
{{
  "verdict": "approved|needs_revision",
  "confidence": "high|medium|low",
  "issues": [{{"scenario_id": "...", "severity": "error|warn", "detail": "..."}}],
  "suggestions": ["..."]
}}
```
只输出 JSON。"""
    try:
        governance_mode = str(load_config(root).get("agents", {}).get("governance_mode") or "agent").strip().lower()
        if governance_mode == "agent":
            from ai_apitest.application.governance import review_quality_with_agent_runtime

            parsed = review_quality_with_agent_runtime(
                root,
                project,
                review_type="coverage_plan_review",
                evidence={"endpoints": endpoint_briefs, "plan": plan[:100]},
                endpoints=endpoints,
                llm=master,
            )
        elif governance_mode == "legacy":
            from ai_apitest.ai.provider import chat_completion

            response = chat_completion(
                base_url=master.get("base_url",""), model=master.get("model",""),
                api_key_env=master.get("api_key_env",""), provider=master.get("provider","openai-compatible"),
                messages=[{"role": "user", "content": prompt}],
            ).strip()
            parsed = _governance_parse_json(response, response_type="case_review",
                                             base_url=master.get("base_url",""), model=master.get("model",""),
                                             api_key_env=master.get("api_key_env",""), provider=master.get("provider","openai-compatible"))
        else:
            raise ValueError(f"不支持的治理模式：{governance_mode}")
        if parsed:
            _audit_governance(root, project, "plan_reviewed", {"verdict": parsed.get("verdict"),
                "issue_count": len(parsed.get("issues", [])), "plan_size": len(plan)}, task="plan_review")
    except Exception as exc:
        record_operational_error(root, project, component="governance", operation="review_coverage_plan", error=exc)


def _govern_cases(root: Path, project: str, cases: list[TestCase], endpoints: list[Endpoint]) -> list[TestCase] | None:
    """Master tiered review of final TestCases. Returns revised cases if changes made, else None."""
    if not _governance_feature_enabled(root, "review_cases"):
        return None
    master = configured_llm(root, "master")
    if not is_llm_configured(master):
        return None
    import json as _json

    # Build lookup dict for case replacement; process reviews on mutable dict
    # Use scenario_id as primary key to avoid cross-batch ID collisions
    case_by_key: dict[str, TestCase] = {}
    for c in cases:
        sid = getattr(c, "scenario_id", "")
        key = sid if sid else c.id
        case_by_key[key] = c
    from ai_apitest.core.serde import read_json as _rj
    dep_graph = _rj(project_docs_dir(root, project) / "metadata.json", {}).get("dependency_graph")
    changed = False

    # Tiered selection: mandatory all-in, P2/P3 sampled
    mandatory = [c for c in cases if c.priority in ("P0", "P1") or c.method == "CHAIN"
                 or c.risk in ("high", "destructive") or getattr(c, "scenario_id", "").startswith("SCN-HARD-")]
    sampled = [c for c in cases if c not in mandatory and c.priority in ("P2", "P3")]
    import random as _random
    sampled = _random.sample(sampled, min(max(5, len(mandatory) // 4), len(sampled))) if len(sampled) > 0 else []

    if not mandatory and not sampled:
        return

    endpoint_briefs = [{"method": e.method, "path": e.path, "summary": e.summary or ""} for e in endpoints]

    def _brief(c):
        return {"id": c.id, "scenario_id": getattr(c, "scenario_id", ""), "title": c.title,
                "method": c.method, "path": c.path, "priority": c.priority,
                "design_method": c.design_method, "runnable": c.runnable, "risk": c.risk,
                "assertions": [{"type": a.type, "target": a.target, "operator": a.operator} for a in c.assertions],
                "source": "system_gap_filled" if getattr(c, "scenario_id", "").startswith("SCN-HARD-") else "llm"}

    def _review_batch(batch_cases, label):
        if not batch_cases:
            return
        briefs = [_brief(c) for c in batch_cases]
        # Pre-review: run local validation to flag issues for master attention
        local_issues = detect_runnable_misclassification(batch_cases, dep_graph) + validate_setup_variable_flow(batch_cases)

        prompt = f"""你是测试策略审查者。请审查以下测试用例，输出结构化 JSON。

## 审查标准
1. 断言是否合理（不过度猜测，不遗漏关键字段）
2. CHAIN steps 是否完整、变量传递正确
3. 是否存在明显重复
4. 依赖链路是否合理
5. system_gap_filled 用例（系统硬性补漏追加）质量是否可接受

## 本地校验发现的问题（供参考）
{_json.dumps(local_issues[:10], ensure_ascii=False, indent=2) if local_issues else "无"}

## 接口列表
{_json.dumps(endpoint_briefs, ensure_ascii=False, indent=2)}

## 待审查用例（{label}，{len(briefs)} 条）
{_json.dumps(briefs, ensure_ascii=False, indent=2)}

## 严格 JSON 输出格式
```json
{{
  "verdict": "approved|needs_revision|rejected",
  "confidence": "high|medium|low",
  "issues": [{{"scenario_id": "...", "severity": "error|warn", "detail": "..."}}],
  "coverage_gaps": [{{"endpoint": "POST /x", "missing_strategy": "path_parameter_error"}}],
  "suggestions": ["..."]
}}
```
只输出 JSON。"""
        try:
            governance_mode = str(load_config(root).get("agents", {}).get("governance_mode") or "agent").strip().lower()
            if governance_mode == "agent":
                from ai_apitest.application.governance import review_quality_with_agent_runtime

                parsed = review_quality_with_agent_runtime(
                    root,
                    project,
                    review_type="case_review",
                    evidence={
                        "local_issues": local_issues[:20],
                        "endpoints": endpoint_briefs,
                        "cases": briefs,
                        "batch": label,
                    },
                    endpoints=endpoints,
                    llm=master,
                )
            elif governance_mode == "legacy":
                from ai_apitest.ai.provider import chat_completion

                response = chat_completion(
                    base_url=master.get("base_url", ""), model=master.get("model", ""),
                    api_key_env=master.get("api_key_env", ""), provider=master.get("provider", "openai-compatible"),
                    messages=[{"role": "user", "content": prompt}],
                ).strip()
                parsed = _governance_parse_json(response, response_type="case_review",
                                                 base_url=master.get("base_url",""), model=master.get("model",""),
                                                 api_key_env=master.get("api_key_env",""), provider=master.get("provider","openai-compatible"))
            else:
                raise ValueError(f"不支持的治理模式：{governance_mode}")
            if parsed:
                _audit_governance(root, project, "cases_reviewed", {
                    "verdict": parsed.get("verdict"), "confidence": parsed.get("confidence"),
                    "issue_count": len(parsed.get("issues", [])), "batch": label,
                }, task="case_review")
                if parsed.get("verdict") == "needs_revision" and parsed.get("issues"):
                    case_gen = configured_llm(root, "case_generator")
                    if is_llm_configured(case_gen):
                        issues = parsed.get("issues", [])
                        if governance_mode == "agent":
                            from ai_apitest.application.case_maintenance import maintain_cases_with_agent_runtime

                            issue_ids = {
                                str(item.get("case_id") or item.get("scenario_id") or "")
                                for item in issues
                                if isinstance(item, dict)
                            }
                            targets = [
                                case
                                for case in batch_cases
                                if case.id in issue_ids or case.scenario_id in issue_ids
                            ][:5]
                            maintenance = maintain_cases_with_agent_runtime(
                                root,
                                project,
                                impacted=targets,
                                endpoints=endpoints,
                                change_context={"quality_review_issues": issues},
                                llm=case_gen,
                                allow_retire=False,
                            )
                            revised = maintenance.updated
                        else:
                            revised = _revise_cases(root, project, batch_cases, issues, endpoints, case_gen)
                        if revised:
                            nonlocal changed
                            changed = True
                            for r in revised:
                                # Match by scenario_id first (stable across revision),
                                # fall back to id if scenario_id missing
                                sid = getattr(r, "scenario_id", "")
                                if sid:
                                    existing = next((c for c in case_by_key.values()
                                                     if getattr(c, "scenario_id", "") == sid), None)
                                    if existing:
                                        case_by_key[sid] = r
                                        continue
                                r_key = getattr(r, "scenario_id", "") or r.id
                                case_by_key[r_key] = r
        except Exception as exc:
            record_operational_error(
                root,
                project,
                component="governance",
                operation="review_case_batch",
                error=exc,
                detail={"batch": label, "case_count": len(batch_cases)},
            )

    # Batch mandatory cases (10 per batch) — all reviewed, no cap
    BATCH_SIZE = 10
    for i in range(0, len(mandatory), BATCH_SIZE):
        _review_batch(mandatory[i:i + BATCH_SIZE], f"必审-第{i//BATCH_SIZE + 1}批")
    # One batch for sampled P2/P3
    _review_batch(sampled, "P2/P3抽样")
    return list(case_by_key.values()) if changed else None


def _apply_merge(root: Path, project: str, candidate_id: str, target_id: str) -> None:
    """Merge candidate into target: validate, merge occurrence/content, write relations, archive candidate."""
    try:
        from ai_apitest.context.storage import lessons_by_ids, merge_lesson_into, now_iso
        safe = safe_project_name(project)
        both = lessons_by_ids(root, safe, [candidate_id, target_id])
        if candidate_id not in both or target_id not in both:
            return
        target = both[target_id]
        if target.get("lifecycle_status") not in ("active", "pending"):
            return
        with context_connection(root, safe) as conn:
            merge_lesson_into(conn, source_id=candidate_id, target_id=target_id, timestamp=now_iso(), actor="ai:master")
            audit(conn, "governance_merged", "lesson", candidate_id, safe, "ai:master", {"merged_into": target_id})
    except Exception as exc:
        record_operational_error(root, project, component="governance", operation="merge_candidate", error=exc)


def _apply_reject(root: Path, project: str, candidate_id: str) -> None:
    """Reject candidate: validate existence, mark as ignored with audit trail."""
    try:
        from ai_apitest.context.storage import lessons_by_ids
        safe = safe_project_name(project)
        candidates = lessons_by_ids(root, safe, [candidate_id])
        if candidate_id not in (candidates or {}):
            return
        with context_connection(root, safe) as conn:
            conn.execute(
                "update lessons set lifecycle_status='ignored', review_status='rejected' where id=? and project=?",
                (candidate_id, safe),
            )
            audit(conn, "governance_rejected", "lesson", candidate_id, safe, "ai:master", {"reason": "governance review"})
    except Exception as exc:
        record_operational_error(root, project, component="governance", operation="reject_candidate", error=exc)


def _apply_confirm(root: Path, project: str, candidate_id: str) -> None:
    """Confirm candidate: activate pending lesson (governance approved)."""
    try:
        from ai_apitest.context.storage import lessons_by_ids
        safe = safe_project_name(project)
        candidates = lessons_by_ids(root, safe, [candidate_id])
        if candidate_id not in (candidates or {}):
            return
        with context_connection(root, safe) as conn:
            conn.execute(
                "update lessons set lifecycle_status='active', review_status='confirmed' where id=? and project=? and lifecycle_status='pending'",
                (candidate_id, safe),
            )
            audit(conn, "governance_confirmed", "lesson", candidate_id, safe, "ai:master", {"reason": "governance approved"})
    except Exception as exc:
        record_operational_error(root, project, component="governance", operation="confirm_candidate", error=exc)


def _apply_needs_review(root: Path, project: str, candidate_id: str) -> None:
    """Mark candidate for manual review (low confidence or flagged)."""
    try:
        from ai_apitest.context.storage import lessons_by_ids
        safe = safe_project_name(project)
        candidates = lessons_by_ids(root, safe, [candidate_id])
        if candidate_id not in (candidates or {}):
            return
        with context_connection(root, safe) as conn:
            conn.execute(
                "update lessons set review_status='needs_review' where id=? and project=?",
                (candidate_id, safe),
            )
            audit(conn, "governance_needs_review", "lesson", candidate_id, safe, "ai:master", {"reason": "low confidence or flagged by governance"})
    except Exception as exc:
        record_operational_error(root, project, component="governance", operation="mark_candidate_for_review", error=exc)


def _apply_conflict(root: Path, project: str, candidate_id: str, target_id: str) -> None:
    """Mark candidate as conflicting with target — both kept, manual resolution needed."""
    try:
        from ai_apitest.context.storage import lessons_by_ids, now_iso
        safe = safe_project_name(project)
        both = lessons_by_ids(root, safe, [candidate_id, target_id])
        if candidate_id not in both or target_id not in both:
            return
        with context_connection(root, safe) as conn:
            conn.execute(
                "update lessons set review_status='needs_review' where id in (?, ?) and project=?",
                (candidate_id, target_id, safe),
            )
            # Insert a conflict relation (matches actual schema: id, lesson_id, related_lesson_id, relation_type, notes, created_at)
            conn.execute(
                """insert into lesson_relations(lesson_id, related_lesson_id, relation_type, notes, created_at)
                   values (?, ?, 'conflict', ?, ?)""",
                (candidate_id, target_id, "governance detected conflict", now_iso()),
            )
            audit(conn, "governance_conflict_flagged", "lesson_relation", candidate_id, safe, "ai:master", {"conflict_with": target_id})
    except Exception as exc:
        record_operational_error(root, project, component="governance", operation="flag_candidate_conflict", error=exc)


def _revise_cases(root: Path, project: str, cases: list[TestCase], issues: list[dict[str, Any]], endpoints: list[Endpoint], llm: dict) -> list[TestCase]:
    """Ask case_generator to revise specific problematic cases. Returns revised cases."""
    import json as _json
    from ai_apitest.ai.provider import chat_completion
    from ai_apitest.ai.case_generation import validate_cases
    from ai_apitest.ai.provider import extract_json_array

    case_ids = list({i.get("case_id", i.get("scenario_id", "")) for i in issues if i.get("case_id") or i.get("scenario_id")})
    if not case_ids:
        return []
    target_cases = [c for c in cases if c.id in case_ids or getattr(c, "scenario_id", "") in case_ids][:5]
    if not target_cases:
        return []

    briefs = [{"id": c.id, "scenario_id": getattr(c, "scenario_id", ""), "title": c.title,
               "method": c.method, "path": c.path, "runnable": c.runnable, "risk": c.risk,
               "design_method": c.design_method, "tags": c.tags, "priority": c.priority,
               "assertions": [{"type": a.type, "target": a.target, "operator": a.operator, "expected": a.expected} for a in c.assertions],
               "query": c.query, "body": c.body, "expected_result": c.expected_result,
               "setup_steps": [{"method": s.method, "path": s.path, "extract": s.extract} for s in (c.setup_steps or [])],
               "path_params": c.path_params}
              for c in target_cases]
    issues_brief = [{"case_id": i.get("case_id"), "scenario_id": i.get("scenario_id"),
                     "severity": i.get("severity"), "detail": i.get("detail")}
                    for i in issues[:5]]

    prompt = f"""{output_language_instruction()}

以下测试用例经 master 审查发现问题，请根据 issue 修复这些问题并重新输出完整 JSON 数组。

## 问题列表
{_json.dumps(issues_brief, ensure_ascii=False, indent=2)}

## 需修复的用例
{_json.dumps(briefs, ensure_ascii=False, indent=2)}

## 修复要求
- 只修复 issue 中列出的问题，不要改动其他用例内容。
- id、scenario_id、design_method、method、path 必须和原始用例完全一致，不可修改。
- 只输出修复后的 JSON 数组，不要解释。"""

    try:
        response = chat_completion(
            base_url=llm.get("base_url", ""), model=llm.get("model", ""),
            api_key_env=llm.get("api_key_env", ""), provider=llm.get("provider", "openai-compatible"),
            messages=[{"role": "user", "content": prompt}],
        ).strip()
        revised_raw = extract_json_array(response)
        revised = validate_cases(revised_raw, endpoints)
        # Force-overwrite immutable fields from originals (unless issue targets them)
        IMMUTABLE = ["id", "scenario_id", "design_method", "method", "path"]
        issue_targets = {i.get("type", "") for i in issues}
        orig_by_sid = {getattr(c, "scenario_id", ""): c for c in target_cases if getattr(c, "scenario_id", "")}
        valid_revised = []
        for r in revised:
            sid = getattr(r, "scenario_id", "")
            orig = orig_by_sid.get(sid)
            if not orig:
                continue
            for field in IMMUTABLE:
                if field not in issue_targets:
                    setattr(r, field, getattr(orig, field))
            valid_revised.append(r)
        revised = valid_revised
        # Stable identity: re-map revised ids back to originals
        orig_sid_to_id = {sid: c.id for sid, c in orig_by_sid.items()}
        for r in revised:
            sid = getattr(r, "scenario_id", "")
            if sid in orig_sid_to_id:
                r.id = orig_sid_to_id[sid]
        if revised:
            _audit_governance(root, project, "cases_revised", {
                "revised_count": len(revised), "issue_count": len(issues_brief),
            }, task="case_review")
        return revised
    except Exception as exc:
        record_operational_error(root, project, component="governance", operation="revise_cases", error=exc)
        return []


def _audit_governance(root: Path, project: str, action: str, detail: dict, *, task: str = "") -> None:
    try:
        safe = safe_project_name(project)
        master = configured_llm(root, "master")
        detail["model"] = master.get("model", "unknown") if master else "unknown"
        detail["task"] = task
        detail["timestamp"] = now_iso()
        with context_connection(root, safe) as conn:
            audit(conn, f"governance_{action}", "governance", "", safe, "ai:master", detail)
    except Exception as exc:
        record_operational_error(root, project, component="governance", operation="write_audit", error=exc)


def _governance_parse_json(
    content: str,
    *,
    response_type: str,
    base_url: str = "",
    model: str = "",
    api_key_env: str = "",
    provider: str = "openai-compatible",
) -> dict | None:
    """Parse governance JSON with self-repair. Returns validated dict or None."""
    from ai_apitest.ai.provider import retry_with_json_repair

    def _parse(text: str) -> dict:
        parsed = _safe_json(text)
        if parsed is None:
            raise ValueError("JSON 对象解析失败")
        validated = _validate_governance_response(parsed, response_type)
        if validated is None:
            raise ValueError("治理响应 schema 校验失败")
        return validated

    if base_url and model and api_key_env:
        try:
            result, _warning = retry_with_json_repair(
                content, parse_json=_parse,
                base_url=base_url, model=model, api_key_env=api_key_env, provider=provider,
            )
            return result
        except ValueError:
            return None
    # No model configured — bare parse without repair
    try:
        return _parse(content)
    except ValueError:
        return None


def _safe_json(text: str) -> dict | None:
    import json as _json, re
    text = text.strip()
    m = re.search(r'\{[\s\S]*\}', text)
    if not m:
        return None
    try:
        return _json.loads(m.group())
    except _json.JSONDecodeError:
        return None


def _validate_governance_response(parsed: dict, response_type: str) -> dict | None:
    """Validate master governance JSON output against expected schema."""
    VALID_CONFIDENCE = {"high", "medium", "low"}
    if response_type == "report_review":
        if parsed.get("verdict") not in ("approved", "needs_revision", "rejected"):
            return None
        if parsed.get("confidence") not in VALID_CONFIDENCE:
            return None
        for issue in parsed.get("issues", []):
            if not isinstance(issue, dict) or "severity" not in issue or "detail" not in issue:
                return None
    elif response_type == "case_review":
        if parsed.get("verdict") not in ("approved", "needs_revision", "rejected"):
            return None
        if parsed.get("confidence") not in VALID_CONFIDENCE:
            return None
    elif response_type == "experience_governance":
        VALID_DECISIONS = {"create", "merge", "reject", "needs_review", "conflict"}
        for d in parsed.get("decisions", []):
            if not isinstance(d, dict):
                return None
            if d.get("decision") not in VALID_DECISIONS:
                return None
            if d.get("confidence") not in VALID_CONFIDENCE:
                return None
            if d.get("decision") in ("merge", "conflict") and not d.get("target_lesson_id"):
                return None
    return parsed
