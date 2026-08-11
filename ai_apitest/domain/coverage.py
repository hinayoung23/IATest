from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlsplit

from ai_apitest.core.case_schema import normalize_design_method, normalize_priority
from ai_apitest.core.models import Endpoint
from ai_apitest.domain.case_drafts import DraftIssue, documented_status_from_basis, unique_strings
from ai_apitest.domain.contracts import (
    business_rule_coverage_obligations,
    coverage_partition_ref,
)
from ai_apitest.domain.scenario_titles import ensure_dependency_chain_title


def normalize_coverage_plan(
    raw_items: Iterable[object],
    endpoints: list[Endpoint],
) -> tuple[list[dict[str, Any]], list[DraftIssue]]:
    endpoint_by_key = {(endpoint.method.upper(), endpoint.path): endpoint for endpoint in endpoints}
    plan: list[dict[str, Any]] = []
    issues: list[DraftIssue] = []
    seen_semantics: set[tuple[Any, ...]] = set()
    seen_ids: set[str] = set()

    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            issues.append(DraftIssue("invalid_plan_item", f"覆盖计划第 {index + 1} 项不是对象"))
            continue
        method = str(raw.get("endpoint_method") or raw.get("method") or "").upper()
        path = str(raw.get("endpoint_path") or raw.get("path") or "")
        scenario_id = str(raw.get("scenario_id") or "").strip()
        design_method = normalize_design_method(raw.get("design_method") or "happy_path")
        title_hint = str(raw.get("title_hint") or raw.get("intent") or "").strip()
        expected_basis = str(raw.get("expected_basis") or "")
        chain_steps = [dict(item) for item in raw.get("chain_steps") or [] if isinstance(item, dict)]
        if method == "CHAIN":
            valid_steps = []
            for step in chain_steps:
                step_method = str(step.get("method") or "").upper()
                raw_step_path = normalize_template_path(str(step.get("path") or ""))
                split_path = urlsplit(raw_step_path)
                step_path = split_path.path
                normalized_step = dict(step)
                if split_path.query:
                    inline_query = dict(parse_qsl(split_path.query, keep_blank_values=True))
                    existing_query = normalized_step.get("query") or normalized_step.get("query_params") or {}
                    normalized_step["query"] = {**inline_query, **dict(existing_query)}
                endpoint = endpoint_by_key.get((step_method, step_path)) or resolve_endpoint_shape(
                    step_method,
                    step_path,
                    endpoint_by_key,
                )
                if endpoint is None:
                    issues.append(
                        DraftIssue(
                            "unknown_plan_chain_endpoint",
                            f"覆盖计划链路引用不存在的接口：{step_method} {step_path}",
                            scenario_id,
                            "chain_steps",
                        )
                    )
                    continue
                valid_steps.append({**normalized_step, "method": step_method, "path": endpoint.path})
            if len(valid_steps) < 2:
                issues.append(DraftIssue("invalid_plan_chain", "链路计划至少需要两个真实接口步骤", scenario_id))
                continue
            if requires_unsafe_load_chain(raw, valid_steps):
                issues.append(
                    DraftIssue(
                        "unsafe_load_chain",
                        "限流/高频链路会污染共享环境或依赖实时流量状态，不能作为默认可执行交付用例，已跳过",
                        scenario_id,
                        "chain_steps",
                        severity="warning",
                    )
                )
                continue
            chain_steps = valid_steps
            design_method = "dependency_chain"
            title_hint = ensure_dependency_chain_title(title_hint, chain_steps, endpoints)
            semantic_key: tuple[Any, ...] = (
                "CHAIN",
                tuple((step["method"], step["path"]) for step in chain_steps),
                title_hint,
            )
        else:
            endpoint = endpoint_by_key.get((method, path))
            if endpoint is None:
                issues.append(
                    DraftIssue(
                        "unknown_plan_endpoint",
                        f"覆盖计划引用不存在的接口：{method} {path}",
                        scenario_id,
                        "endpoint",
                    )
                )
                continue
            if (
                raw.get("origin") != "deterministic"
                and requires_unexecutable_orchestration(raw)
                and raw.get("runnable") is not False
            ):
                issues.append(
                    DraftIssue(
                        "unexecutable_plan_item",
                        "该业务增量需要等待、外部造数或多次调用，但未规划为可执行 CHAIN，已跳过",
                        scenario_id,
                        "chain_steps",
                        severity="warning",
                    )
                )
                continue
            if design_method == "type_error" and not has_typed_input(endpoint):
                issues.append(
                    DraftIssue(
                        "untestable_transport_type_error",
                        "Path、Query 和 Header 中的 string 参数在线上传输时始终是字符串，无法构造有效的非字符串类型用例，已跳过该计划项",
                        scenario_id,
                        "design_method",
                        severity="warning",
                    )
                )
                continue
            status = documented_status_from_basis(expected_basis)
            if design_method == "happy_path" and isinstance(status, int) and status >= 400:
                success_status = next(
                    (
                        candidate
                        for candidate in numeric_statuses(endpoint)
                        if 200 <= candidate < 300
                    ),
                    None,
                )
                if success_status is not None:
                    expected_basis = f"responses.{success_status}"
                    status = success_status
                    issues.append(
                        DraftIssue(
                            "happy_path_error_basis_repaired",
                            "happy_path 错误引用了失败响应，已改用接口声明的成功响应",
                            scenario_id,
                            "expected_basis",
                            severity="warning",
                        )
                    )
            semantic_key = coverage_semantic_key(
                method,
                path,
                design_method,
                title_hint,
                expected_basis,
                [str(item) for item in raw.get("rule_ids") or []],
                [str(item) for item in raw.get("coverage_partition_ids") or []],
            )
            if status is not None and status not in numeric_statuses(endpoint):
                issues.append(
                    DraftIssue(
                        "undocumented_plan_status",
                        f"覆盖计划引用了文档未声明的状态码：{status}",
                        scenario_id,
                        "expected_basis",
                    )
                )
                continue
            if (
                status is not None
                and status >= 400
                and not can_construct_plan_item(endpoint, status, raw)
                and raw.get("runnable") is not False
            ):
                issues.append(
                    DraftIssue(
                        "unexecutable_status_coverage",
                        f"文档声明了 {status} 响应，但当前 Schema/安全配置不足以构造可证明的触发请求，已跳过",
                        scenario_id,
                        "expected_basis",
                        severity="warning",
                    )
                )
                continue
        if semantic_key in seen_semantics:
            issues.append(DraftIssue("duplicate_plan_item", "覆盖计划包含重复场景，已去重", scenario_id, severity="warning"))
            continue
        seen_semantics.add(semantic_key)
        if not scenario_id or scenario_id in seen_ids:
            scenario_id = next_scenario_id(seen_ids)
        seen_ids.add(scenario_id)
        if not title_hint:
            title_hint = "链路主流程" if method == "CHAIN" else f"{method} {path} {design_method}"
        plan.append(
            {
                "scenario_id": scenario_id,
                "endpoint_method": method,
                "endpoint_path": path,
                "design_method": design_method,
                "priority": normalize_priority(raw.get("priority"), design_method=design_method),
                "title_hint": title_hint,
                "coverage_tags": unique_strings([*(raw.get("coverage_tags") or []), design_method]),
                "expected_basis": expected_basis,
                "data_dependency": raw.get("data_dependency"),
                "runnable": raw.get("runnable") if isinstance(raw.get("runnable"), bool) else None,
                "risk": str(raw.get("risk") or ""),
                "rule_ids": unique_strings(raw.get("rule_ids") or []),
                "coverage_partition_ids": unique_strings(
                    raw.get("coverage_partition_ids") or []
                ),
                "fixture_requirements": unique_strings(raw.get("fixture_requirements") or []),
                "request_overrides": dict(raw.get("request_overrides") or {}),
                "omit_fields": unique_strings(raw.get("omit_fields") or []),
                "assertions": [
                    dict(item)
                    for item in raw.get("assertions") or []
                    if isinstance(item, dict)
                ],
                "chain_steps": chain_steps if method == "CHAIN" else [],
                "origin": (
                    str(raw.get("origin"))
                    if raw.get("origin") in {"deterministic", "contract"}
                    else "model"
                ),
            }
        )
    return plan, issues


def normalize_template_path(path: str) -> str:
    return re.sub(r"\{\{\s*([^{}]+?)\s*\}\}", r"{\1}", str(path or ""))


def resolve_endpoint_shape(
    method: str,
    path: str,
    endpoint_by_key: dict[tuple[str, str], Endpoint],
) -> Endpoint | None:
    shape = re.sub(r"\{[^{}]+\}", "{}", path)
    matches = [
        endpoint
        for (candidate_method, candidate_path), endpoint in endpoint_by_key.items()
        if candidate_method == method and re.sub(r"\{[^{}]+\}", "{}", candidate_path) == shape
    ]
    return matches[0] if len(matches) == 1 else None


def requires_unexecutable_orchestration(item: dict[str, Any]) -> bool:
    """Reject model additions whose stated behavior cannot run as one HTTP step."""
    text = " ".join(
        [
            str(item.get("title_hint") or item.get("intent") or ""),
            str(item.get("expected_basis") or ""),
            " ".join(str(tag) for tag in item.get("coverage_tags") or []),
        ]
    ).lower()
    patterns = (
        r"重复调用|再次调用|第二次|首次|随后|之后|删除后|续期后|刷新后|等待|"
        r"过期|保留窗口|不同生命周期|两种状态|完整链路|跨接口|→",
        r"\b(?:repeat(?:ed)? call|second call|first call|afterwards?|then|wait|expired?|"
        r"grace period|lifecycle|multi[- ]step|cross[- ]api)\b",
    )
    return any(re.search(pattern, text, flags=re.I) for pattern in patterns)


def requires_unsafe_load_chain(item: dict[str, Any], steps: list[dict[str, Any]]) -> bool:
    status = documented_status_from_basis(str(item.get("expected_basis") or ""))
    if status == 429:
        return True
    text = " ".join(
        [
            str(item.get("title_hint") or item.get("intent") or ""),
            str(item.get("expected_basis") or ""),
            " ".join(str(tag) for tag in item.get("coverage_tags") or []),
        ]
    )
    if re.search(r"限流|高频|频率|压测|负载|rate.?limit|throttl|load.?test", text, flags=re.I):
        return True
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for step in steps:
        counts[(str(step.get("method") or "").upper(), str(step.get("path") or ""))] += 1
    return len(steps) > 8 or any(count > 5 for count in counts.values())


def coverage_semantic_key(
    method: str,
    path: str,
    design_method: str,
    title_hint: str,
    expected_basis: str,
    rule_ids: list[str] | None = None,
    coverage_partition_ids: list[str] | None = None,
) -> tuple[str, ...]:
    normalized_partitions = tuple(
        sorted(str(item) for item in coverage_partition_ids or [] if str(item))
    )
    if normalized_partitions:
        return method, path, "coverage_partitions", *normalized_partitions
    normalized_rules = tuple(sorted(str(item) for item in rule_ids or [] if str(item)))
    if normalized_rules:
        return method, path, "business_rules", *normalized_rules
    status = documented_status_from_basis(expected_basis)
    if status is not None and status >= 400:
        # A single HTTP request cannot safely represent several different
        # business preconditions for the same documented error. Key by the
        # response contract so path/auth/field strategies do not duplicate a
        # generic status-code item with the same request and assertion.
        return method, path, "documented_error", str(status)
    # These strategies represent one protocol/business class per endpoint.
    # Models often restate the deterministic item with a different title; the
    # title must not turn that restatement into another costly design scenario.
    if design_method in {"happy_path", "equivalence_class", "auth_error", "status_code_coverage"}:
        return method, path, design_method, expected_basis
    # Field-oriented strategies may legitimately target multiple parameters or
    # boundaries; retain their concise target hint as part of the identity.
    normalized_title = " ".join(title_hint.lower().split())
    return method, path, design_method, expected_basis, normalized_title


def can_construct_plan_item(endpoint: Endpoint, status: int, item: dict[str, Any]) -> bool:
    if item.get("request_overrides") or item.get("omit_fields") or item.get("chain_steps"):
        return True
    rule_ids = {str(value) for value in item.get("rule_ids") or []}
    for rule in endpoint.business_rules:
        if not isinstance(rule, dict) or str(rule.get("rule_id") or "") not in rule_ids:
            continue
        trigger = rule.get("trigger") if isinstance(rule.get("trigger"), dict) else {}
        if trigger.get("request_overrides") or trigger.get("omit_fields") or trigger.get("chain_steps"):
            return True
    return can_construct_error_status(endpoint, status)


def fill_deterministic_coverage_gaps(
    plan: list[dict[str, Any]],
    endpoints: list[Endpoint],
    *,
    enabled_strategies: set[str] | None = None,
    dependency_graph: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    enabled = enabled_strategies or {
        "happy_path",
        "optional_field",
        "type_error",
        "boundary_value",
        "path_parameter_error",
        "dependency_chain",
        "auth_error",
        "equivalence_class",
        "status_code_coverage",
    }
    result = [dict(item) for item in plan]
    covered: dict[tuple[str, str], set[str]] = defaultdict(set)
    for item in result:
        key = (str(item.get("endpoint_method") or ""), str(item.get("endpoint_path") or ""))
        covered[key].add(str(item.get("design_method") or ""))
        covered[key].update(str(tag) for tag in item.get("coverage_tags") or [])

    for endpoint in endpoints:
        key = (endpoint.method.upper(), endpoint.path)
        methods = covered[key]
        contract_strategies = {
            str(rule.get("strategy") or "")
            for rule in endpoint.business_rules
            if isinstance(rule, dict) and rule.get("testable", True)
        }
        contract_bases = {
            str(
                (rule.get("expected") or {}).get("basis")
                if isinstance(rule.get("expected"), dict)
                else ""
            )
            for rule in endpoint.business_rules
            if isinstance(rule, dict) and rule.get("testable", True)
        }
        has_positive_contract = any(
            isinstance(rule, dict)
            and rule.get("testable", True)
            and rule.get("runnable", True)
            and isinstance(rule.get("expected"), dict)
            and isinstance(rule["expected"].get("status"), int)
            and 200 <= rule["expected"]["status"] < 300
            for rule in endpoint.business_rules
        )
        if (
            "happy_path" not in methods
            and "happy_path" not in contract_strategies
            and not has_positive_contract
        ):
            append_plan_item(result, endpoint, "happy_path", "P0", f"{endpoint.summary or endpoint.path} 主流程", ["happy_path"])
            methods.add("happy_path")
        optional_queries = [item for item in endpoint.parameters if item.get("in") == "query" and not item.get("required")]
        optional_body = optional_body_fields(endpoint)
        if (
            (optional_queries or optional_body)
            and "optional_field" in enabled
            and "optional_field" not in methods
            and "optional_field" not in contract_strategies
        ):
            target = optional_queries[0].get("name") if optional_queries else optional_body[0]
            append_plan_item(
                result,
                endpoint,
                "optional_field",
                "P1",
                f"传入可选参数 {target}",
                ["optional_field"],
            )
            methods.add("optional_field")
        if (
            has_distinct_equivalence_input(endpoint)
            and "equivalence_class" in enabled
            and "equivalence_class" not in methods
            and "equivalence_class" not in contract_strategies
        ):
            append_plan_item(result, endpoint, "equivalence_class", "P1", "有效等价类代表值", ["equivalence_class"])
            methods.add("equivalence_class")
        type_error_basis = first_response_basis(endpoint, {400, 415, 422})
        if (
            has_typed_input(endpoint)
            and type_error_basis
            and "type_error" in enabled
            and "type_error" not in methods
            and "type_error" not in contract_strategies
        ):
            append_plan_item(
                result,
                endpoint,
                "type_error",
                "P2",
                "输入字段类型错误",
                ["type_error"],
                expected_basis=type_error_basis,
            )
            methods.add("type_error")
        boundary_basis = first_response_basis(endpoint, {400, 416, 422})
        valid_boundary_request = valid_constraint_boundary_request(endpoint)
        if (
            valid_boundary_request
            and "boundary_value" in enabled
            and not any(
                item.get("endpoint_method") == endpoint.method.upper()
                and item.get("endpoint_path") == endpoint.path
                and "valid_boundary" in (item.get("coverage_tags") or [])
                for item in result
            )
        ):
            append_plan_item(
                result,
                endpoint,
                "boundary_value",
                "P1",
                "参数取文档声明的合法最大边界值",
                ["boundary_value", "valid_boundary"],
                expected_basis=first_success_basis(endpoint),
                request_overrides=valid_boundary_request,
            )
        if (
            has_constraints(endpoint)
            and boundary_basis
            and "boundary_value" in enabled
            and "boundary_value" not in methods
            and "boundary_value" not in contract_strategies
        ):
            append_plan_item(
                result,
                endpoint,
                "boundary_value",
                "P2",
                "文档约束边界值",
                ["boundary_value"],
                expected_basis=boundary_basis,
            )
            methods.add("boundary_value")
        path_error_basis = first_response_basis(endpoint, {400, 404, 422})
        if (
            has_path_parameter(endpoint)
            and path_error_basis
            and "path_parameter_error" in enabled
            and "path_parameter_error" not in methods
            and path_error_basis not in contract_bases
        ):
            append_plan_item(
                result,
                endpoint,
                "path_parameter_error",
                "P2",
                "路径参数非法或资源不存在",
                ["path_parameter_error"],
                expected_basis=path_error_basis,
            )
            methods.add("path_parameter_error")
        required_basis = first_response_basis(endpoint, {400, 422})
        if has_required_body_input(endpoint) and required_basis and "required_field" not in methods:
            append_plan_item(
                result,
                endpoint,
                "required_field",
                "P2",
                "请求体必填字段缺失",
                ["required_field"],
                expected_basis=required_basis,
            )
            methods.add("required_field")
        auth_basis = first_response_basis(endpoint, {401, 403})
        if has_auth(endpoint) and auth_basis and "auth_error" in enabled and "auth_error" not in methods:
            append_plan_item(
                result,
                endpoint,
                "auth_error",
                "P1",
                "缺少或使用错误鉴权信息",
                ["auth_error"],
                expected_basis=auth_basis,
            )
            methods.add("auth_error")
        if "status_code_coverage" in enabled:
            existing_bases = {
                str(item.get("expected_basis") or "")
                for item in result
                if item.get("endpoint_method") == endpoint.method.upper() and item.get("endpoint_path") == endpoint.path
            }
            existing_bases.update(contract_bases)
            for status in numeric_statuses(endpoint):
                if status < 400:
                    continue
                basis = f"responses.{status}"
                if basis in existing_bases:
                    continue
                constructible = can_construct_error_status(endpoint, status)
                response = endpoint.responses.get(str(status)) or endpoint.responses.get(status) or {}
                description = str(response.get("description") or "") if isinstance(response, dict) else ""
                append_plan_item(
                    result,
                    endpoint,
                    "status_code_coverage",
                    "P1",
                    (
                        description or f"文档声明的 {status} 响应"
                    ) + (
                        ""
                        if constructible
                        else "（文档契约覆盖；当前缺少可安全构造的触发数据）"
                    ),
                    ["status_code_coverage"],
                    expected_basis=basis,
                    runnable=True if constructible else False,
                    risk=(
                        ""
                        if constructible
                        else (
                            "shared_rate_limit"
                            if status == 429
                            else "documented_response_without_fixture"
                        )
                    ),
                )

        covered_obligations = {
            str(obligation)
            for item in result
            for obligation in (
                item.get("coverage_partition_ids") or item.get("rule_ids") or []
            )
        }
        for rule in endpoint.business_rules:
            if not isinstance(rule, dict) or not rule.get("testable", True):
                continue
            rule_id = str(rule.get("rule_id") or "")
            if not rule_id:
                continue
            strategy = str(rule.get("strategy") or "happy_path")
            if strategy in {
                "optional_field",
                "type_error",
                "boundary_value",
                "path_parameter_error",
                "dependency_chain",
                "auth_error",
                "equivalence_class",
                "status_code_coverage",
            } and strategy not in enabled:
                continue
            obligations = business_rule_coverage_obligations(rule)
            if obligations and all(
                obligation in covered_obligations for obligation in obligations
            ):
                continue
            for item in business_rule_plan_items(endpoint, rule, endpoints):
                item_obligations = item.get("coverage_partition_ids") or item.get("rule_ids") or []
                if any(str(value) in covered_obligations for value in item_obligations):
                    continue
                result.append(item)
                covered_obligations.update(str(value) for value in item_obligations)

    has_contract_chain = any(
        isinstance(rule, dict)
        and rule.get("testable", True)
        and str(rule.get("strategy") or "") == "dependency_chain"
        for endpoint in endpoints
        for rule in endpoint.business_rules
    )
    if (
        "dependency_chain" in enabled
        and not has_contract_chain
        and not any(item.get("endpoint_method") == "CHAIN" for item in result)
    ):
        result.extend(chain_items_from_graph(dependency_graph or {}, endpoints))
    return ensure_unique_scenario_ids(result)


def business_rule_plan_item(
    endpoint: Endpoint,
    rule: dict[str, Any],
    endpoints: list[Endpoint],
) -> dict[str, Any]:
    return business_rule_plan_items(endpoint, rule, endpoints)[0]


def business_rule_plan_items(
    endpoint: Endpoint,
    rule: dict[str, Any],
    endpoints: list[Endpoint],
) -> list[dict[str, Any]]:
    partitions = [
        item
        for item in rule.get("coverage_partitions") or []
        if isinstance(item, dict) and item.get("partition_id")
    ]
    return [
        _business_rule_plan_item(endpoint, rule, endpoints, partition=partition)
        for partition in (partitions or [None])
    ]


def _business_rule_plan_item(
    endpoint: Endpoint,
    rule: dict[str, Any],
    endpoints: list[Endpoint],
    *,
    partition: dict[str, Any] | None,
) -> dict[str, Any]:
    rule_id = str(rule.get("rule_id") or "")
    strategy = str(rule.get("strategy") or "happy_path")
    expected = rule.get("expected") if isinstance(rule.get("expected"), dict) else {}
    expected_status = expected.get("status")
    if (
        strategy == "happy_path"
        and isinstance(expected_status, int)
        and expected_status >= 400
    ):
        strategy = (
            "dependency_chain"
            if rule.get("lifecycle_state") or rule.get("preconditions")
            else "status_code_coverage"
        )
    trigger = rule.get("trigger") if isinstance(rule.get("trigger"), dict) else {}
    chain_steps = [
        dict(item)
        for item in trigger.get("chain_steps") or []
        if isinstance(item, dict)
    ]
    if len(chain_steps) < 2:
        chain_steps = []
    request_overrides = dict(trigger.get("request_overrides") or {})
    omit_fields = unique_strings(trigger.get("omit_fields") or [])
    partition_id = str(partition.get("partition_id") or "") if partition else ""
    if partition:
        request_overrides = dict(partition.get("request_overrides") or {})
        omit_fields = unique_strings(partition.get("omit_fields") or [])
    if not request_overrides and not omit_fields and not chain_steps:
        request_overrides, omit_fields = inferred_rule_request(endpoint, rule)
    fixtures = unique_strings(rule.get("fixture_requirements") or [])
    if not chain_steps and not fixtures:
        chain_steps = deterministic_lifecycle_chain(endpoint, rule, endpoints)
    runnable = bool(rule.get("runnable", True)) and not fixtures
    expected_basis = str(expected.get("basis") or "")
    if (
        isinstance(expected_status, int)
        and documented_status_from_basis(expected_basis) != expected_status
    ):
        expected_basis = f"responses.{expected_status}"
    method = "CHAIN" if chain_steps else endpoint.method.upper()
    path = "" if chain_steps else endpoint.path
    # Rules requiring a state transition but lacking deterministic steps are
    # delegated to the semantic designer. Single-request and explicit fixture
    # contracts compile locally without another model call.
    origin = (
        "contract"
        if (
            strategy == "dependency_chain"
            and not chain_steps
            and not request_overrides
            and not omit_fields
            and runnable
        )
        else "deterministic"
    )
    assertions = [
        dict(item)
        for item in expected.get("assertions") or []
        if isinstance(item, dict)
    ]
    if chain_steps and any(step.get("assertions") for step in chain_steps):
        assertions = []
    scenario_suffix = re.sub(r"[^A-Za-z0-9]+", "-", rule_id).strip("-")
    if partition_id:
        scenario_suffix += "-" + re.sub(
            r"[^A-Za-z0-9]+", "-", partition_id
        ).strip("-").upper()
    partition_ref = coverage_partition_ref(rule_id, partition_id)
    title_hint = str(rule.get("title") or rule.get("description") or rule_id)
    if partition:
        title_hint = f"{title_hint}：{partition.get('title') or partition_id}"
    return {
        "scenario_id": f"SCN-RULE-{scenario_suffix}",
        "endpoint_method": method,
        "endpoint_path": path,
        "design_method": "dependency_chain" if chain_steps else strategy,
        "priority": "P0" if strategy in {"happy_path", "dependency_chain"} else "P1",
        "title_hint": title_hint,
        "coverage_tags": unique_strings(
            [
                "business_rule",
                strategy,
                f"rule:{rule_id}",
                f"partition:{partition_ref}" if partition_id else "",
            ]
        ),
        "expected_basis": expected_basis,
        "data_dependency": None,
        "runnable": runnable,
        "risk": str(rule.get("risk") or ("fixture_required" if fixtures else "")),
        "fixture_requirements": fixtures,
        "rule_ids": [rule_id],
        "coverage_partition_ids": [partition_ref] if partition_id else [],
        "request_overrides": request_overrides,
        "omit_fields": omit_fields,
        "assertions": assertions,
        "chain_steps": chain_steps,
        "origin": origin,
    }


def deterministic_lifecycle_chain(
    endpoint: Endpoint,
    rule: dict[str, Any],
    endpoints: list[Endpoint],
) -> list[dict[str, Any]]:
    from ai_apitest.domain.contracts import business_condition_signature

    text = " ".join(
        str(rule.get(key) or "")
        for key in ("title", "description", "source_condition")
    )
    condition = business_condition_signature(text)
    expected = rule.get("expected") if isinstance(rule.get("expected"), dict) else {}
    status = expected.get("status")
    endpoint_keys = {(item.method.upper(), item.path) for item in endpoints}
    generate_key = ("GET", "/api/generate")
    delete_key = ("DELETE", "/api/delete/{email}")

    if (
        endpoint.method.upper() == "DELETE"
        and endpoint.path == delete_key[1]
        and generate_key in endpoint_keys
        and re.search(r"删除成功后.+不能再查询邮件.+续期", text)
    ):
        steps: list[dict[str, Any]] = [
            {
                "name": "生成真实临时邮箱",
                "method": generate_key[0],
                "path": generate_key[1],
                "extract": {"email": "$.email"},
            },
            {
                "name": "删除刚生成的临时邮箱",
                "method": delete_key[0],
                "path": delete_key[1],
                "path_params": {"email": "{{email}}"},
                "assertions": [
                    {"type": "status_code", "target": "", "operator": "eq", "expected": 200},
                    {"type": "json_path", "target": "$.status", "operator": "eq", "expected": "ok"},
                ],
            },
        ]
        messages_key = ("GET", "/api/messages/{email}")
        if messages_key in endpoint_keys:
            steps.append(
                {
                    "name": "验证删除后无法再查询邮件",
                    "method": messages_key[0],
                    "path": messages_key[1],
                    "path_params": {"email": "{{email}}"},
                    "assertions": [
                        {"type": "status_code", "target": "", "operator": "eq", "expected": 200},
                        {"type": "json_path", "target": "$", "operator": "is_array", "expected": True},
                        {"type": "json_path", "target": "$", "operator": "length_eq", "expected": 0},
                    ],
                }
            )
        extend_key = ("POST", "/api/extend/{email}")
        if extend_key in endpoint_keys:
            steps.append(
                {
                    "name": "验证删除后无法续期",
                    "method": extend_key[0],
                    "path": extend_key[1],
                    "path_params": {"email": "{{email}}"},
                    "assertions": [
                        {"type": "status_code", "target": "", "operator": "eq", "expected": 404},
                    ],
                }
            )
        return steps if len(steps) >= 3 else []

    if endpoint.path == "/api/generate" and re.search(r"同一个.+前缀.+已经存在|刷新.+创建时间", text):
        prefix = "refresh-{{random}}"
        return [
            {
                "name": "先创建同前缀邮箱",
                "method": "GET",
                "path": "/api/generate",
                "query": {"prefix": prefix},
                "extract": {"email": "$.email", "first_created_at": "$.created_at"},
            },
            {
                "name": "再次生成同前缀邮箱并验证刷新",
                "method": "GET",
                "path": "/api/generate",
                "query": {"prefix": prefix},
                "assertions": [
                    {"type": "status_code", "target": "", "operator": "eq", "expected": 200},
                    {
                        "type": "json_path",
                        "target": "$.email",
                        "operator": "eq",
                        "expected": "{{email}}",
                    },
                    {
                        "type": "json_path",
                        "target": "$.created_at",
                        "operator": "gte",
                        "expected": "{{first_created_at}}",
                    },
                    {"type": "json_path", "target": "$.expires_in", "operator": "eq", "expected": 600},
                ],
            },
        ]

    has_email_path = any(
        isinstance(item, dict)
        and item.get("in") == "path"
        and item.get("name") == "email"
        for item in endpoint.parameters
    ) or "{email}" in endpoint.path
    if not has_email_path or generate_key not in endpoint_keys:
        return []
    if condition in {"missing", "cleaned"}:
        return []
    if not isinstance(status, int) or not 200 <= status < 300:
        if condition != "deleted":
            return []

    steps: list[dict[str, Any]] = [
        {
            "name": "生成真实临时邮箱",
            "method": generate_key[0],
            "path": generate_key[1],
            "extract": {"email": "$.email"},
        }
    ]
    if condition == "deleted":
        if delete_key not in endpoint_keys:
            return []
        steps.append(
            {
                "name": "删除刚生成的临时邮箱",
                "method": delete_key[0],
                "path": delete_key[1],
                "path_params": {"email": "{{email}}"},
            }
        )
    steps.append(
        {
            "name": str(rule.get("title") or endpoint.summary or "验证目标接口"),
            "method": endpoint.method.upper(),
            "path": endpoint.path,
            "path_params": {"email": "{{email}}"},
        }
    )
    return steps


def inferred_rule_request(
    endpoint: Endpoint,
    rule: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    text = " ".join(
        str(rule.get(key) or "")
        for key in ("title", "description", "source_condition")
    )
    query_parameters = [
        item
        for item in endpoint.parameters
        if item.get("in") == "query"
    ]
    if re.search(r"不传|未传|省略", text, flags=re.I) and query_parameters:
        return {}, [f"query.{query_parameters[0].get('name')}"]
    if re.search(r"空白|空字符串", text, flags=re.I) and query_parameters:
        return {"query": {str(query_parameters[0].get("name")): ""}}, []
    if re.search(r"重复传入|多个.+参数", text, flags=re.I) and query_parameters:
        name = str(query_parameters[0].get("name") or "")
        return {"query": {name: ["iatest_a", "iatest_b"]}}, []
    if (
        re.search(r"传入.+合法.+(?:prefix|前缀)|合法.+(?:prefix|前缀)", text, flags=re.I)
        and query_parameters
    ):
        return {"query": {str(query_parameters[0].get("name") or ""): "test123"}}, []
    for parameter in query_parameters:
        schema = parameter.get("schema") if isinstance(parameter.get("schema"), dict) else {}
        name = str(parameter.get("name") or "")
        maximum = schema.get("maxLength")
        if isinstance(maximum, int) and re.search(
            r"超长|(?:超过|大于).{0,12}(?:长度|字符)|(?:长度|字符).{0,12}(?:超过|大于)",
            text,
        ):
            return {"query": {name: "x" * (maximum + 1)}}, []
        if isinstance(maximum, int) and re.search(r"最长|最大长度|边界", text):
            return {"query": {name: "x" * maximum}}, []
        if schema.get("pattern") and re.search(r"非法|只能包含|字符", text):
            return {"query": {name: "invalid@"}}, []
    path_parameters = [
        item
        for item in endpoint.parameters
        if item.get("in") == "path"
    ]
    if path_parameters and re.search(r"不存在|从未创建|已清理", text):
        name = str(path_parameters[0].get("name") or "")
        return {"path_params": {name: f"missing-{name}-{{{{uuid}}}}"}}, []
    return {}, []


def chain_items_from_graph(graph: dict[str, Any], endpoints: list[Endpoint]) -> list[dict[str, Any]]:
    endpoint_keys = {(endpoint.method.upper(), endpoint.path) for endpoint in endpoints}
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for edge in graph.get("edges") or []:
        if not isinstance(edge, dict) or not edge.get("safe_to_prepare", True):
            continue
        producer = edge.get("from") or {}
        consumer = edge.get("to") or {}
        pkey = (str(producer.get("method") or "").upper(), str(producer.get("path") or ""))
        ckey = (str(consumer.get("method") or "").upper(), str(consumer.get("path") or ""))
        if pkey in endpoint_keys and ckey in endpoint_keys:
            groups[pkey].append(edge)
    result: list[dict[str, Any]] = []
    method_order = {"GET": 0, "HEAD": 0, "POST": 1, "PUT": 2, "PATCH": 2, "DELETE": 3}
    for index, (producer_key, edges) in enumerate(groups.items(), start=1):
        producer_step: dict[str, Any] = {"method": producer_key[0], "path": producer_key[1]}
        extracts: dict[str, str] = {}
        for edge in edges:
            extracts.update({str(key): str(value) for key, value in (edge.get("extract") or {}).items()})
            field = str(edge.get("produced_field") or "")
            if field and field not in extracts:
                extracts[field] = f"$.{field}"
        if extracts:
            producer_step["extract"] = extracts
        chain_steps = [producer_step]
        seen_consumers: set[tuple[str, str]] = set()
        for edge in sorted(edges, key=lambda item: method_order.get(str((item.get("to") or {}).get("method") or "").upper(), 1)):
            consumer = edge.get("to") or {}
            key = (str(consumer.get("method") or "").upper(), str(consumer.get("path") or ""))
            if key in seen_consumers:
                continue
            seen_consumers.add(key)
            step: dict[str, Any] = {"method": key[0], "path": key[1]}
            for dotted, value in (edge.get("inject") or {}).items():
                section, _, name = str(dotted).partition(".")
                if section in {"headers", "path_params", "query"} and name:
                    step.setdefault(section, {})[name] = value
            if not edge.get("inject"):
                field = str(edge.get("produced_field") or "")
                if field:
                    step.setdefault("path_params", {})[field] = "{{" + field + "}}"
            chain_steps.append(step)
            if len(chain_steps) >= 4:
                break
        if len(chain_steps) < 2:
            continue
        result.append(
            {
                "scenario_id": f"SCN-CHAIN-{index:03d}",
                "endpoint_method": "CHAIN",
                "endpoint_path": "",
                "design_method": "dependency_chain",
                "priority": "P0",
                "title_hint": ensure_dependency_chain_title("", chain_steps, endpoints),
                "coverage_tags": ["dependency_chain"],
                "expected_basis": "dependency_graph",
                "data_dependency": None,
                "chain_steps": chain_steps,
                "origin": "deterministic",
            }
        )
    return result


def append_plan_item(
    result: list[dict[str, Any]],
    endpoint: Endpoint,
    design_method: str,
    priority: str,
    title_hint: str,
    tags: list[str],
    *,
    expected_basis: str = "",
    runnable: bool | None = None,
    risk: str = "",
    request_overrides: dict[str, Any] | None = None,
) -> None:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", f"{endpoint.method}-{endpoint.path}").strip("-")[:60]
    result.append(
        {
            "scenario_id": f"SCN-SYSTEM-{design_method.upper()}-{slug}",
            "endpoint_method": endpoint.method.upper(),
            "endpoint_path": endpoint.path,
            "design_method": design_method,
            "priority": priority,
            "title_hint": title_hint,
            "coverage_tags": tags,
            "expected_basis": expected_basis,
            "data_dependency": None,
            "runnable": runnable,
            "risk": risk,
            "request_overrides": dict(request_overrides or {}),
            "chain_steps": [],
            "origin": "deterministic",
        }
    )


def valid_constraint_boundary_request(endpoint: Endpoint) -> dict[str, Any]:
    for parameter in endpoint.parameters:
        if parameter.get("in") not in {"query", "path"}:
            continue
        schema = parameter.get("schema")
        if not isinstance(schema, dict):
            continue
        value: Any = None
        if isinstance(schema.get("maxLength"), int):
            value = "a" * int(schema["maxLength"])
        elif (
            schema.get("type") in {"integer", "number"}
            and isinstance(schema.get("maximum"), (int, float))
            and not isinstance(schema.get("maximum"), bool)
        ):
            value = schema["maximum"]
        if value is None:
            continue
        section = "query" if parameter.get("in") == "query" else "path_params"
        return {section: {str(parameter.get("name") or ""): value}}
    return {}


def first_success_basis(endpoint: Endpoint) -> str:
    status = next(
        (value for value in numeric_statuses(endpoint) if 200 <= value < 300),
        None,
    )
    return f"responses.{status}" if status is not None else ""


def ensure_unique_scenario_ids(plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in plan:
        copied = dict(item)
        scenario_id = str(copied.get("scenario_id") or "")
        if not scenario_id or scenario_id in seen:
            scenario_id = next_scenario_id(seen)
        copied["scenario_id"] = scenario_id
        seen.add(scenario_id)
        result.append(copied)
    return result


def next_scenario_id(existing: set[str]) -> str:
    index = len(existing) + 1
    while f"SCN-{index:04d}" in existing:
        index += 1
    return f"SCN-{index:04d}"


def numeric_statuses(endpoint: Endpoint) -> list[int]:
    result = []
    for value in endpoint.responses:
        try:
            result.append(int(str(value)))
        except ValueError:
            continue
    return sorted(set(result))


def has_path_parameter(endpoint: Endpoint) -> bool:
    return "{" in endpoint.path or any(item.get("in") == "path" for item in endpoint.parameters)


def has_testable_input(endpoint: Endpoint) -> bool:
    return bool(endpoint.parameters or endpoint.request_body)


def has_distinct_equivalence_input(endpoint: Endpoint) -> bool:
    schemas = [
        item.get("schema")
        for item in endpoint.parameters
        if isinstance(item.get("schema"), dict)
    ]
    body_schema = request_body_schema(endpoint)
    if body_schema:
        schemas.append(body_schema)
    return any(schema_has_distinct_values(schema) for schema in schemas)


def schema_has_distinct_values(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    enum = schema.get("enum")
    if isinstance(enum, list) and len(enum) > 1:
        return True
    if schema.get("type") == "boolean":
        return True
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    return any(schema_has_distinct_values(item) for item in properties.values()) or schema_has_distinct_values(
        schema.get("items")
    )


def has_typed_input(endpoint: Endpoint) -> bool:
    body_schema = request_body_schema(endpoint)
    if body_schema.get("type") or body_schema.get("properties"):
        return True
    for parameter in endpoint.parameters:
        schema = parameter.get("schema") if isinstance(parameter.get("schema"), dict) else {}
        schema_type = schema.get("type")
        if isinstance(schema_type, list):
            schema_type = next((item for item in schema_type if item != "null"), "")
        if schema_type and schema_type != "string":
            return True
    return False


def has_constraints(endpoint: Endpoint) -> bool:
    for parameter in endpoint.parameters:
        schema = parameter.get("schema") if isinstance(parameter.get("schema"), dict) else {}
        description = f"{parameter.get('description') or ''} {schema.get('description') or ''}"
        if any(key in schema for key in ("minLength", "maxLength", "minimum", "maximum", "enum", "pattern")):
            return True
        if any(word in description for word in ("最长", "最短", "最大", "最小", "长度", "字符", "范围")):
            return True
    return schema_has_constraints(request_body_schema(endpoint))


def request_body_schema(endpoint: Endpoint) -> dict[str, Any]:
    body = endpoint.request_body if isinstance(endpoint.request_body, dict) else {}
    content = body.get("content") if isinstance(body.get("content"), dict) else {}
    media = content.get("application/json") or next(iter(content.values()), {}) if content else {}
    schema = media.get("schema") if isinstance(media, dict) else {}
    return schema if isinstance(schema, dict) else {}


def optional_body_fields(endpoint: Endpoint) -> list[str]:
    schema = request_body_schema(endpoint)
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = {str(item) for item in schema.get("required", [])} if isinstance(schema.get("required"), list) else set()
    return [str(name) for name in properties if str(name) not in required]


def has_required_body_input(endpoint: Endpoint) -> bool:
    body = endpoint.request_body if isinstance(endpoint.request_body, dict) else {}
    schema = request_body_schema(endpoint)
    return bool(body.get("required") or schema.get("required"))


def schema_has_constraints(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    if any(key in schema for key in ("minLength", "maxLength", "minimum", "maximum", "enum", "pattern", "minItems", "maxItems")):
        return True
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    if any(schema_has_constraints(item) for item in properties.values()):
        return True
    return schema_has_constraints(schema.get("items"))


def has_auth(endpoint: Endpoint) -> bool:
    if endpoint.security:
        return True
    text = f"{endpoint.summary} {endpoint.description} " + " ".join(
        f"{item.get('name') or ''} {item.get('description') or ''}" for item in endpoint.parameters
    )
    return any(token in text.lower() for token in ("auth", "token", "api_key", "apikey", "bearer", "鉴权", "认证"))


def first_response_basis(endpoint: Endpoint, accepted: set[int]) -> str:
    status = next((item for item in numeric_statuses(endpoint) if item in accepted), None)
    return f"responses.{status}" if status is not None else ""


def can_construct_error_status(endpoint: Endpoint, status: int) -> bool:
    if status in {401, 403}:
        return has_auth(endpoint)
    if status == 404:
        return has_path_parameter(endpoint)
    if status == 415:
        return bool(endpoint.request_body)
    if status in {400, 422}:
        return bool(
            has_required_body_input(endpoint)
            or has_typed_input(endpoint)
            or has_constraints(endpoint)
        )
    if status == 416:
        return has_constraints(endpoint)
    # Rate limits, conflicts and other stateful business errors require an
    # explicit executable CHAIN/fixture instead of a fabricated single request.
    return False
