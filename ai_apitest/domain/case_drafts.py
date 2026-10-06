from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from ai_apitest.core.case_schema import normalize_design_method, normalize_priority, normalize_test_case
from ai_apitest.core.models import Assertion, Endpoint, TestCase, TestStep
from ai_apitest.core.schema import check_schema
from ai_apitest.domain.scenario_titles import concise_case_intent, ensure_dependency_chain_title
from ai_apitest.core.serde import assertion_from_dict, to_jsonable
from ai_apitest.domain.contracts import (
    business_rule_coverage_obligations,
    regex_pattern_implies,
)
from ai_apitest.generator.cases import (
    attach_cleanup_steps,
    attach_setup_steps,
    build_case,
    case_requires_absent_resource,
    default_body,
    default_headers,
    default_path_params,
    default_query,
    documented_status_codes,
    module_name,
    parameter_serialization,
    success_assertions,
)


NEGATIVE_DESIGN_METHODS = {
    "required_field",
    "type_error",
    "boundary_value",
    "path_parameter_error",
    "auth_error",
}
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
ALLOWED_ASSERTION_TYPES = {"status_code", "json_path", "header", "body_contains", "json_schema", "response_time", "custom"}
ALLOWED_ASSERTION_OPERATORS = {
    "eq", "equals", "equal", "==", "ne", "not_equals", "!=", "in",
    "gt", "gte", "lt", "lte", "contains", "not_empty", "exists", "not_exists",
    "is_array", "is_string", "is_integer", "is_positive_integer", "is_number",
    "is_boolean", "is_object", "is_null", "is_2xx", "length_eq",
    "sorted_desc_by", "sorted_asc_by", "starts_with", "ends_with", "matches", "regex",
    "gte_path", "lte_path", "gt_path", "lt_path", "approx_eq",
}
BUILTIN_TEMPLATE_VARIABLES = {"timestamp", "uuid", "random", "base_url"}


@dataclass(slots=True)
class ScenarioDraft:
    """Semantic case intent produced by a model before deterministic compilation."""

    scenario_id: str
    endpoint_method: str
    endpoint_path: str
    intent: str
    design_method: str
    priority_hint: str = ""
    coverage_tags: list[str] = field(default_factory=list)
    request_overrides: dict[str, Any] = field(default_factory=dict)
    omit_fields: list[str] = field(default_factory=list)
    expected_outcome: str = ""
    expected_basis: str = ""
    assertions: list[dict[str, Any]] = field(default_factory=list)
    dependencies: list[dict[str, Any]] = field(default_factory=list)
    chain_steps: list[dict[str, Any]] = field(default_factory=list)
    runnable_hint: bool | None = None
    risk_hint: str = ""
    rule_ids: list[str] = field(default_factory=list)
    coverage_partition_ids: list[str] = field(default_factory=list)
    fixture_requirements: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScenarioDraft:
        if not isinstance(data, dict):
            raise ValueError("场景草稿必须是对象")
        method = str(data.get("endpoint_method") or data.get("method") or "").strip().upper()
        path = str(data.get("endpoint_path") or data.get("path") or "").strip()
        endpoint_text = str(data.get("endpoint") or "").strip()
        if endpoint_text and (not method or not path):
            parts = endpoint_text.split(maxsplit=1)
            if len(parts) == 2:
                method = method or parts[0].upper()
                path = path or parts[1]
        request_overrides = data.get("request_overrides")
        if request_overrides is None:
            request_overrides = data.get("request") or {}
        if not isinstance(request_overrides, dict):
            raise ValueError("request_overrides 必须是对象")
        design_method = normalize_design_method(data.get("design_method") or "happy_path")
        assertions = dict_items(data.get("assertions"))
        chain_steps = normalize_chain_step_sequence(dict_items(data.get("chain_steps") or data.get("steps")))
        if design_method == "dependency_chain" and len(chain_steps) >= 2:
            method = "CHAIN"
            path = ""
        expected_basis = str(data.get("expected_basis") or "").strip()
        if design_method == "status_code_coverage" and documented_status_from_basis(expected_basis) is None:
            asserted_status = asserted_status_from_raw(assertions)
            if asserted_status is not None:
                expected_basis = f"responses.{asserted_status}" + (f" — {expected_basis}" if expected_basis else "")
        return cls(
            scenario_id=str(data.get("scenario_id") or "").strip(),
            endpoint_method=method,
            endpoint_path=path,
            intent=str(data.get("intent") or data.get("title_hint") or data.get("title") or "").strip(),
            design_method=design_method,
            priority_hint=str(data.get("priority_hint") or data.get("priority") or "").strip(),
            coverage_tags=string_items(data.get("coverage_tags") or data.get("tags")),
            request_overrides=normalize_request_overrides(request_overrides),
            omit_fields=string_items(data.get("omit_fields")),
            expected_outcome=str(data.get("expected_outcome") or data.get("expected_result") or "").strip(),
            expected_basis=expected_basis,
            assertions=assertions,
            dependencies=dict_items(data.get("dependencies")),
            chain_steps=chain_steps,
            runnable_hint=data.get("runnable") if isinstance(data.get("runnable"), bool) else None,
            risk_hint=str(data.get("risk") or "").strip(),
            rule_ids=string_items(data.get("rule_ids")),
            coverage_partition_ids=string_items(data.get("coverage_partition_ids")),
            fixture_requirements=string_items(data.get("fixture_requirements")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "endpoint_method": self.endpoint_method,
            "endpoint_path": self.endpoint_path,
            "intent": self.intent,
            "design_method": self.design_method,
            "priority_hint": self.priority_hint,
            "coverage_tags": list(self.coverage_tags),
            "request_overrides": copy.deepcopy(self.request_overrides),
            "omit_fields": list(self.omit_fields),
            "expected_outcome": self.expected_outcome,
            "expected_basis": self.expected_basis,
            "assertions": copy.deepcopy(self.assertions),
            "dependencies": copy.deepcopy(self.dependencies),
            "chain_steps": copy.deepcopy(self.chain_steps),
            "runnable": self.runnable_hint,
            "risk": self.risk_hint,
            "rule_ids": list(self.rule_ids),
            "coverage_partition_ids": list(self.coverage_partition_ids),
            "fixture_requirements": list(self.fixture_requirements),
        }


@dataclass(slots=True)
class DraftIssue:
    code: str
    message: str
    scenario_id: str = ""
    field: str = ""
    severity: str = "error"

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "message": self.message,
            "scenario_id": self.scenario_id,
            "field": self.field,
            "severity": self.severity,
        }


@dataclass(slots=True)
class CompilationResult:
    drafts: list[ScenarioDraft]
    cases: list[TestCase]
    issues: list[DraftIssue]

    @property
    def accepted(self) -> bool:
        return bool(self.cases) and not any(issue.severity == "error" for issue in self.issues)

    @property
    def error_issues(self) -> list[DraftIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]


def parse_scenario_drafts(raw_drafts: Iterable[object]) -> tuple[list[ScenarioDraft], list[DraftIssue]]:
    drafts: list[ScenarioDraft] = []
    issues: list[DraftIssue] = []
    for index, raw in enumerate(raw_drafts):
        if not isinstance(raw, dict):
            issues.append(DraftIssue("invalid_draft", f"第 {index + 1} 个草稿不是对象"))
            continue
        try:
            drafts.append(ScenarioDraft.from_dict(raw))
        except (TypeError, ValueError) as exc:
            issues.append(
                DraftIssue(
                    "invalid_draft",
                    f"第 {index + 1} 个草稿无法解析：{exc}",
                    str(raw.get("scenario_id") or ""),
                )
            )
    return drafts, issues


def compile_scenario_drafts(
    raw_drafts: Iterable[object],
    endpoints: list[Endpoint],
    *,
    expected_scenario_ids: Iterable[str] | None = None,
) -> CompilationResult:
    drafts, issues = parse_scenario_drafts(raw_drafts)
    endpoint_by_key = {(endpoint.method.upper(), endpoint.path): endpoint for endpoint in endpoints}
    expected_ids = {str(item) for item in (expected_scenario_ids or []) if str(item)}
    seen_ids: set[str] = set()
    cases: list[TestCase] = []
    counters: dict[str, int] = {}

    for draft in drafts:
        issues.extend(validate_draft(draft, endpoint_by_key, expected_ids, seen_ids))
        if any(issue.severity == "error" and issue.scenario_id == draft.scenario_id for issue in issues):
            continue
        seen_ids.add(draft.scenario_id)
        if draft.endpoint_method == "CHAIN":
            case, chain_issues = compile_chain_draft(draft, endpoint_by_key, counters)
            issues.extend(chain_issues)
        else:
            case, case_issues = compile_endpoint_draft(draft, endpoint_by_key, counters)
            issues.extend(case_issues)
        if case is not None and not any(
            issue.severity == "error" and issue.scenario_id == draft.scenario_id
            for issue in issues
        ):
            cases.append(case)

    if expected_ids:
        for scenario_id in sorted(expected_ids - seen_ids):
            issues.append(
                DraftIssue(
                    "missing_scenario",
                    "覆盖计划中的场景没有对应草稿",
                    scenario_id,
                    "scenario_id",
                )
            )

    cases = attach_cleanup_steps(attach_setup_steps(cases, endpoints), endpoints)
    issues.extend(validate_compiled_cases(cases, endpoints, expected_scenario_ids=expected_ids))
    return CompilationResult(drafts=drafts, cases=cases, issues=dedupe_issues(issues))


def validate_draft(
    draft: ScenarioDraft,
    endpoint_by_key: dict[tuple[str, str], Endpoint],
    expected_ids: set[str],
    seen_ids: set[str],
) -> list[DraftIssue]:
    issues: list[DraftIssue] = []
    sid = draft.scenario_id
    if not sid:
        issues.append(DraftIssue("missing_scenario_id", "scenario_id 不能为空", sid, "scenario_id"))
    elif sid in seen_ids:
        issues.append(DraftIssue("duplicate_scenario_id", "scenario_id 重复", sid, "scenario_id"))
    elif expected_ids and sid not in expected_ids:
        issues.append(DraftIssue("unexpected_scenario", "草稿不属于当前覆盖计划", sid, "scenario_id"))
    if draft.endpoint_method != "CHAIN" and (draft.endpoint_method, draft.endpoint_path) not in endpoint_by_key:
        issues.append(
            DraftIssue(
                "unknown_endpoint",
                f"接口不存在：{draft.endpoint_method} {draft.endpoint_path}",
                sid,
                "endpoint",
            )
        )
    known_rule_ids = {
        str(rule.get("rule_id") or "")
        for endpoint in (
            endpoint_by_key.values()
            if draft.endpoint_method == "CHAIN"
            else [endpoint_by_key.get((draft.endpoint_method, draft.endpoint_path))]
        )
        if endpoint is not None
        for rule in endpoint.business_rules
        if isinstance(rule, dict) and rule.get("rule_id")
    }
    for rule_id in draft.rule_ids:
        if rule_id not in known_rule_ids:
            issues.append(
                DraftIssue(
                    "unknown_business_rule",
                    f"草稿引用的业务规则不属于目标接口契约：{rule_id}",
                    sid,
                    "rule_ids",
                )
            )
    known_partition_ids = {
        obligation
        for endpoint in (
            endpoint_by_key.values()
            if draft.endpoint_method == "CHAIN"
            else [endpoint_by_key.get((draft.endpoint_method, draft.endpoint_path))]
        )
        if endpoint is not None
        for rule in endpoint.business_rules
        if isinstance(rule, dict)
        for obligation in business_rule_coverage_obligations(rule)
        if "#" in obligation
    }
    for partition_id in draft.coverage_partition_ids:
        if partition_id not in known_partition_ids:
            issues.append(
                DraftIssue(
                    "unknown_coverage_partition",
                    f"草稿引用的覆盖分区不属于目标接口契约：{partition_id}",
                    sid,
                    "coverage_partition_ids",
                )
            )
    if draft.endpoint_method == "CHAIN" and len(draft.chain_steps) < 2:
        issues.append(DraftIssue("invalid_chain", "CHAIN 场景至少需要两个 chain_steps", sid, "chain_steps"))
    if draft.endpoint_method != "CHAIN" and draft.dependencies:
        issues.append(
            DraftIssue(
                "non_executable_dependencies",
                "普通接口场景中的 dependencies 不会被执行；多步骤准备与校验必须改为 CHAIN 并提供 chain_steps",
                sid,
                "dependencies",
            )
        )
    if not draft.intent:
        issues.append(DraftIssue("missing_intent", "场景意图不能为空", sid, "intent"))
    if not draft.expected_outcome:
        issues.append(DraftIssue("missing_expected_outcome", "expected_outcome 不能为空", sid, "expected_outcome"))
    if draft.design_method == "status_code_coverage" and not documented_status_from_basis(draft.expected_basis):
        issues.append(
            DraftIssue(
                "missing_expected_basis",
                "status_code_coverage 必须提供 responses.<status> 依据",
                sid,
                "expected_basis",
            )
        )
    return issues


def compile_endpoint_draft(
    draft: ScenarioDraft,
    endpoint_by_key: dict[tuple[str, str], Endpoint],
    counters: dict[str, int],
) -> tuple[TestCase | None, list[DraftIssue]]:
    endpoint = endpoint_by_key[(draft.endpoint_method, draft.endpoint_path)]
    issues = validate_expected_basis(draft, endpoint)
    if any(issue.severity == "error" for issue in issues):
        return None, issues

    headers = default_headers(endpoint)
    path_params = default_path_params(endpoint)
    # ScenarioDraft overrides express optional inputs explicitly. A minimal valid
    # request must not silently inject every optional query parameter.
    query = default_query(endpoint, include_optional=False)
    body = minimal_valid_body(endpoint, default_body(endpoint))
    request = {"headers": headers, "path_params": path_params, "query": query, "body": body}
    apply_request_overrides(request, draft.request_overrides)
    apply_semantic_request_omissions(request, draft, endpoint)
    for field_path in draft.omit_fields:
        remove_dotted_value(request, field_path)

    basis_status = documented_status_from_basis(draft.expected_basis)
    asserted_status = asserted_status_from_raw(draft.assertions)
    negative_strategy = draft.design_method in NEGATIVE_DESIGN_METHODS
    # boundary_value 同时包含“合法边界”和“越界”两类场景。已有明确 2xx
    # 契约时必须按正向用例处理，不能仅凭策略名误标为 negative。
    if (
        draft.design_method == "boundary_value"
        and (
            basis_status is not None
            and 200 <= basis_status < 300
            or asserted_status is not None
            and 200 <= asserted_status < 300
        )
        and not bool(basis_status is not None and basis_status >= 400)
        and not bool(asserted_status is not None and asserted_status >= 400)
    ):
        negative_strategy = False
    negative = (
        negative_strategy
        or bool(basis_status and basis_status >= 400)
        or bool(asserted_status and asserted_status >= 400)
    )
    assertions, assertion_issues = compile_assertions(draft, endpoint)
    issues.extend(assertion_issues)
    if any(issue.severity == "error" for issue in issues):
        return None, issues
    runnable, risk = execution_policy(endpoint, draft, negative)
    if draft.runnable_hint is False:
        runnable = False
        risk = draft.risk_hint or "documented_response_without_fixture"
    elif (
        draft.runnable_hint is True
        and (
            endpoint.method.upper() in {"GET", "HEAD", "OPTIONS"}
            or safe_absent_resource_request(draft, request)
        )
        and not draft.fixture_requirements
    ):
        # runnable_hint 来自已审计接口契约/覆盖计划，并由计划字段覆盖模型输出。
        # 对只读请求允许它解除“存在 path 参数即不可运行”的保守默认值。
        runnable = True
        risk = draft.risk_hint or "none"
    title = f"{endpoint.summary or endpoint.path} - {concise_case_intent(draft.intent)}"
    case = build_case(
        endpoint,
        counters,
        title=title,
        design_method=draft.design_method,
        tags=unique_strings([*draft.coverage_tags, draft.design_method]),
        query=request["query"],
        path_params=request["path_params"],
        body=request["body"],
        assertions=assertions,
        runnable=runnable,
        risk=risk,
        negative=negative,
        expected_result=draft.expected_outcome,
    )
    case.headers = request["headers"]
    case.priority = normalize_priority(
        draft.priority_hint,
        negative=negative,
        design_method=draft.design_method,
    )
    case.scenario_id = draft.scenario_id
    case.source = f"{endpoint.method} {endpoint.path}; {draft.expected_basis or 'scenario_draft'}"
    case.rule_ids = list(draft.rule_ids)
    case.coverage_partition_ids = list(draft.coverage_partition_ids)
    case.fixture_requirements = list(draft.fixture_requirements)
    return normalize_test_case(
        case,
        module=module_name(endpoint),
        index=counters[module_name(endpoint)],
        source_tag="agent_compiled",
        preserve_valid_id=False,
    ), issues


def compile_chain_draft(
    draft: ScenarioDraft,
    endpoint_by_key: dict[tuple[str, str], Endpoint],
    counters: dict[str, int],
) -> tuple[TestCase | None, list[DraftIssue]]:
    issues: list[DraftIssue] = []
    steps: list[TestStep] = []
    assertions_by_step: dict[int, list[dict[str, Any]]] = {}
    unscoped_assertions: list[dict[str, Any]] = []
    for assertion in draft.assertions:
        try:
            step_number = int(assertion.get("step"))
        except (TypeError, ValueError):
            unscoped_assertions.append(assertion)
            continue
        assertions_by_step.setdefault(step_number, []).append(assertion)
    chain_steps = hydrate_chain_data_flow(draft.chain_steps, endpoint_by_key)
    if unscoped_assertions and chain_steps:
        assertions_by_step.setdefault(len(chain_steps), []).extend(unscoped_assertions)
    for index, raw in enumerate(chain_steps):
        raw = normalize_chain_step(raw)
        method = str(raw.get("method") or "").upper()
        path = str(raw.get("path") or "")
        endpoint = endpoint_by_key.get((method, path))
        if endpoint is None:
            endpoint = resolve_chain_endpoint(method, path, endpoint_by_key)
            if endpoint is not None:
                raw = remap_chain_path_params(raw, endpoint.path)
                path = endpoint.path
        if endpoint is None:
            issues.append(
                DraftIssue(
                    "unknown_chain_endpoint",
                    f"链路第 {index + 1} 步引用不存在的接口：{method} {path}",
                    draft.scenario_id,
                    "chain_steps",
                )
            )
            continue
        assertion_payload = [
            *dict_items(raw.get("assertions")),
            *assertions_by_step.get(index + 1, []),
        ]
        assertions = parse_assertions(assertion_payload)
        if not assertions:
            assertions = success_assertions(endpoint)
        elif not any(item.type == "status_code" for item in assertions):
            assertions = [*success_assertions(endpoint), *assertions]
        assertions = remove_redundant_shape_assertions(assertions)
        # A chain-level expected result can describe several different response
        # schemas. Never attach all mentioned fields to the final step; each
        # claim must be expressed on its owning step and is checked after the
        # complete chain has been compiled.
        issues.extend(validate_assertion_list(draft, endpoint, assertions, field_name=f"chain_steps.{index + 1}.assertions"))
        step = TestStep(
            name=str(raw.get("name") or endpoint.summary or f"{method} {path}"),
            method=method,
            path=path,
            headers=dict(raw.get("headers") or default_headers(endpoint)),
            path_params=dict(raw.get("path_params") or default_path_params(endpoint)),
            query=dict(raw.get("query") or default_query(endpoint, include_optional=True)),
            parameter_serialization=parameter_serialization(endpoint),
            body=copy.deepcopy(raw["body"]) if "body" in raw else default_body(endpoint),
            assertions=assertions,
            extract={str(key): str(value) for key, value in dict(raw.get("extract") or {}).items()},
            poll=copy.deepcopy(raw.get("poll") or {}),
        )
        steps.append(step)
    if issues:
        return None, issues

    counters["chain"] = counters.get("chain", 0) + 1
    title = ensure_dependency_chain_title(
        draft.intent,
        [
            {
                "name": step.name,
                "method": step.method,
                "path": step.path,
                "assertions": to_jsonable(step.assertions),
            }
            for step in steps
        ],
        endpoint_by_key.values(),
    )
    basis_status = documented_status_from_basis(draft.expected_basis)
    negative = bool(basis_status is not None and basis_status >= 400)
    if not negative:
        negative = any(
            status >= 400
            for step in steps
            for status in status_values(step.assertions)
        )
    case = TestCase(
        id=f"TC-CHAIN-{counters['chain']:04d}",
        title=title,
        method="CHAIN",
        path="",
        priority=normalize_priority(draft.priority_hint, design_method="dependency_chain"),
        tags=unique_strings(
            [
                *draft.coverage_tags,
                "dependency_chain",
                "negative" if negative else "",
                "agent_compiled",
            ]
        ),
        design_method="dependency_chain",
        assertions=[],
        source=draft.expected_basis or "scenario_draft",
        runnable=draft.runnable_hint is not False,
        risk=draft.risk_hint or ("fixture_required" if draft.fixture_requirements else "data_dependent"),
        negative=negative,
        preconditions=["测试环境及链路中的接口均可访问。", "链路变量由前置步骤响应提取，不依赖硬编码业务数据。"],
        test_steps=[f"执行链路步骤 {index + 1}：{step.name}。" for index, step in enumerate(steps)],
        expected_result=draft.expected_outcome,
        steps=steps,
        scenario_id=draft.scenario_id,
        rule_ids=list(draft.rule_ids),
        coverage_partition_ids=list(draft.coverage_partition_ids),
        fixture_requirements=list(draft.fixture_requirements),
    )
    return normalize_test_case(case, module="CHAIN", index=counters["chain"], source_tag="agent_compiled"), issues


def validate_expected_basis(draft: ScenarioDraft, endpoint: Endpoint) -> list[DraftIssue]:
    status = documented_status_from_basis(draft.expected_basis)
    if status is None:
        return []
    if status not in documented_status_codes(endpoint):
        return [
            DraftIssue(
                "undocumented_expected_status",
                f"expected_basis 引用了接口未声明的状态码：{status}",
                draft.scenario_id,
                "expected_basis",
            )
        ]
    return []


def compile_assertions(draft: ScenarioDraft, endpoint: Endpoint) -> tuple[list[Assertion], list[DraftIssue]]:
    issues: list[DraftIssue] = []
    assertions = parse_assertions(
        [
            *draft.assertions,
            *rule_assertion_obligations(endpoint, draft.rule_ids),
        ]
    )
    assertions = complete_expected_response_assertions(draft, endpoint, assertions)
    if assertions and not any(item.type == "status_code" for item in assertions):
        assertions = [*default_status_assertions(draft, endpoint), *assertions]
    assertions = remove_redundant_shape_assertions(assertions)
    issues.extend(validate_assertion_list(draft, endpoint, assertions))
    if assertions:
        issues.extend(validate_status_assertions(draft, endpoint, assertions))
        return assertions, issues

    return default_status_assertions(draft, endpoint), issues


def rule_assertion_obligations(endpoint: Endpoint, rule_ids: list[str]) -> list[dict[str, Any]]:
    requested = {str(item) for item in rule_ids}
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rule in endpoint.business_rules:
        if not isinstance(rule, dict) or str(rule.get("rule_id") or "") not in requested:
            continue
        expected = rule.get("expected") if isinstance(rule.get("expected"), dict) else {}
        for assertion in expected.get("assertions") or []:
            if not isinstance(assertion, dict):
                continue
            key = json.dumps(assertion, ensure_ascii=False, sort_keys=True, default=str)
            if key not in seen:
                seen.add(key)
                result.append(copy.deepcopy(assertion))
    return result


def default_status_assertions(draft: ScenarioDraft, endpoint: Endpoint) -> list[Assertion]:
    documented = documented_status_codes(endpoint)
    basis_status = documented_status_from_basis(draft.expected_basis)
    if basis_status is not None:
        return [Assertion(type="status_code", operator="eq", expected=basis_status)]
    if draft.design_method in NEGATIVE_DESIGN_METHODS:
        errors = [status for status in documented if 400 <= status < 500]
        preferred = preferred_negative_statuses(draft.design_method, errors)
        if preferred:
            errors = preferred
        if errors:
            operator = "eq" if len(errors) == 1 else "in"
            expected: Any = errors[0] if len(errors) == 1 else errors
            return [Assertion(type="status_code", operator=operator, expected=expected)]
        successes = [status for status in documented if 200 <= status < 300]
        if successes:
            operator = "eq" if len(successes) == 1 else "in"
            expected = successes[0] if len(successes) == 1 else successes
            return [Assertion(type="status_code", operator=operator, expected=expected)]
        return [Assertion(type="status_code", operator="lt", expected=500)]
    return success_assertions(endpoint)


def preferred_negative_statuses(design_method: str, documented: list[int]) -> list[int]:
    """Narrow a generic negative strategy to compatible documented statuses."""
    preferences = {
        "auth_error": {401, 403},
        "path_parameter_error": {400, 404, 422},
        "required_field": {400, 422},
        "type_error": {400, 415, 422},
        "boundary_value": {400, 416, 422},
    }
    accepted = preferences.get(design_method, set())
    return [status for status in documented if status in accepted]


def validate_assertion_list(
    draft: ScenarioDraft,
    endpoint: Endpoint,
    assertions: list[Assertion],
    *,
    field_name: str = "assertions",
) -> list[DraftIssue]:
    issues: list[DraftIssue] = []
    documented = set(documented_status_codes(endpoint))
    for item in assertions:
        if item.type == "json_schema":
            try:
                check_schema(item.expected)
            except Exception as exc:
                issues.append(DraftIssue("invalid_assertion_schema", f"Schema 无效：{exc}", draft.scenario_id, field_name))
        if item.type not in ALLOWED_ASSERTION_TYPES:
            issues.append(
                DraftIssue(
                    "invalid_assertion_type",
                    f"不支持的断言类型：{item.type}",
                    draft.scenario_id,
                    field_name,
                )
            )
        if item.operator not in ALLOWED_ASSERTION_OPERATORS:
            issues.append(
                DraftIssue(
                    "invalid_assertion_operator",
                    f"不支持的断言操作符：{item.operator}",
                    draft.scenario_id,
                    field_name,
                )
            )
        if assertion_requires_expected(item.operator) and item.expected is None:
            issues.append(
                DraftIssue(
                    "missing_assertion_expected",
                    f"断言 {item.type} {item.target or '-'} 的 {item.operator} 操作缺少期望值",
                    draft.scenario_id,
                    field_name,
                )
            )
        if item.type != "status_code" or not documented:
            continue
        values = item.expected if isinstance(item.expected, list) else [item.expected]
        for value in values:
            try:
                status = int(value)
            except (TypeError, ValueError):
                continue
            if status not in documented:
                issues.append(
                    DraftIssue(
                        "undocumented_status_assertion",
                        f"断言了接口文档未声明的状态码：{status}",
                        draft.scenario_id,
                        field_name,
                    )
                )
    return issues


def validate_status_assertions(
    draft: ScenarioDraft,
    endpoint: Endpoint,
    assertions: list[Assertion],
) -> list[DraftIssue]:
    if draft.design_method != "status_code_coverage":
        return []
    documented = set(documented_status_codes(endpoint))
    issues: list[DraftIssue] = []
    for assertion in assertions:
        if assertion.type != "status_code":
            continue
        values = assertion.expected if isinstance(assertion.expected, list) else [assertion.expected]
        for value in values:
            try:
                status = int(value)
            except (TypeError, ValueError):
                continue
            if status not in documented:
                issues.append(
                    DraftIssue(
                        "undocumented_status_assertion",
                        f"status_code_coverage 断言了文档未声明的状态码：{status}",
                        draft.scenario_id,
                        "assertions",
                    )
                )
    return issues


def execution_policy(endpoint: Endpoint, draft: ScenarioDraft, negative: bool) -> tuple[bool, str]:
    method = endpoint.method.upper()
    basis_status = documented_status_from_basis(draft.expected_basis)
    if basis_status in {401, 403}:
        return False, "env_dependent"
    if basis_status == 429:
        return False, "behavior_dependent"
    if draft.design_method == "auth_error":
        return False, "env_dependent"
    if negative and method in WRITE_METHODS:
        return False, "negative_behavior"
    if negative:
        return True, "negative_behavior"
    if method in WRITE_METHODS and endpoint.path.find("{") >= 0:
        return False, "data_dependent"
    if endpoint.path.find("{") >= 0:
        return False, "data_dependent"
    return True, "none"


def safe_absent_resource_request(
    draft: ScenarioDraft,
    request: dict[str, Any],
) -> bool:
    path_params = request.get("path_params")
    if not isinstance(path_params, dict) or not path_params:
        return False
    if not all(
        isinstance(value, str) and "{{uuid}}" in value
        for value in path_params.values()
    ):
        return False
    semantic_text = f"{draft.intent} {draft.expected_outcome}".lower()
    return bool(
        re.search(
            r"不存在|从未创建|未创建|missing|nonexistent|not[\s_-]*found",
            semantic_text,
            flags=re.I,
        )
    )


def apply_request_overrides(request: dict[str, Any], overrides: dict[str, Any]) -> None:
    for section in ("headers", "path_params", "query"):
        value = overrides.get(section)
        if isinstance(value, dict):
            request[section] = deep_merge_dict(request.get(section) or {}, value)
    if "body" in overrides:
        body_override = overrides["body"]
        if isinstance(request.get("body"), dict) and isinstance(body_override, dict):
            request["body"] = deep_merge_dict(request["body"], body_override)
        else:
            request["body"] = copy.deepcopy(body_override)
    for key, value in overrides.items():
        if "." in key and key.split(".", 1)[0] in {"headers", "path_params", "query", "body"}:
            set_dotted_value(request, key, value)


def apply_semantic_request_omissions(
    request: dict[str, Any],
    draft: ScenarioDraft,
    endpoint: Endpoint,
) -> None:
    semantic_text = " ".join(
        [draft.intent, draft.expected_outcome, draft.expected_basis]
    ).lower()
    for parameter in endpoint.parameters:
        location = str(parameter.get("in") or "")
        if location not in {"query", "header", "cookie"} or parameter.get("required"):
            continue
        name = str(parameter.get("name") or "").strip()
        if not name:
            continue
        if not semantic_omits_field(semantic_text, name):
            continue
        if location == "query":
            request.get("query", {}).pop(name, None)
        elif location == "header":
            for key in list(request.get("headers", {})):
                if str(key).lower() == name.lower():
                    request["headers"].pop(key, None)
        elif location == "cookie":
            for key in list(request.get("headers", {})):
                if str(key).lower() != "cookie":
                    continue
                parts = [
                    item.strip()
                    for item in str(request["headers"].get(key) or "").split(";")
                    if item.strip() and item.split("=", 1)[0].strip().lower() != name.lower()
                ]
                if parts:
                    request["headers"][key] = "; ".join(parts)
                else:
                    request["headers"].pop(key, None)

    schema = request_body_schema(endpoint)
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = {str(item) for item in schema.get("required", []) if str(item)} if isinstance(schema.get("required"), list) else set()
    if isinstance(request.get("body"), dict):
        for name in properties:
            if str(name) not in required and semantic_omits_field(semantic_text, str(name)):
                request["body"].pop(str(name), None)


def semantic_omits_field(text: str, name: str) -> bool:
    escaped = re.escape(name.lower())
    return any(
        re.search(pattern, text)
        for pattern in (
            rf"不传\s*{escaped}",
            rf"未传\s*{escaped}",
            rf"不提供\s*{escaped}",
            rf"不指定\s*{escaped}",
            rf"省略\s*{escaped}",
            rf"{escaped}\s*缺省",
        )
    )


def complete_expected_response_assertions(
    draft: ScenarioDraft,
    endpoint: Endpoint,
    assertions: list[Assertion],
) -> list[Assertion]:
    result = list(assertions)
    semantic_text = " ".join(
        [draft.intent, draft.expected_outcome, draft.expected_basis]
    )
    response_schema = response_schema_for_draft(draft, endpoint)
    root_type = response_schema.get("type") if isinstance(response_schema, dict) else None
    if root_type == "array" and re.search(r"数组|列表|\barray\b|\blist\b", semantic_text, flags=re.I):
        if not any(
            item.type == "json_path" and item.target in {"", "$"} and item.operator == "is_array"
            for item in result
        ):
            result.append(Assertion(type="json_path", target="$", operator="is_array", expected=True))
    ordering = expected_array_order(semantic_text)
    if root_type == "array" and ordering is not None:
        operator, field_name = ordering
        if not any(
            item.type == "json_path" and item.target in {"", "$"} and item.operator == operator
            for item in result
        ):
            result.append(Assertion(type="json_path", target="$", operator=operator, expected=field_name))

    for field_name, schema in response_properties_for_draft(draft, endpoint).items():
        target = f"$.{field_name}"
        if not mentions_response_field(semantic_text, field_name):
            continue
        explicit, expected = expected_field_value(semantic_text, field_name)
        if explicit:
            operator = "is_null" if expected is None else "eq"
            assertion_expected = True if expected is None else expected
            if not any(
                item.type == "json_path"
                and item.target == target
                and item.operator == operator
                and item.expected == assertion_expected
                for item in result
            ):
                result.append(
                    Assertion(
                        type="json_path",
                        target=target,
                        operator=operator,
                        expected=assertion_expected,
                    )
                )
        elif not any(item.type == "json_path" and item.target == target for item in result):
            schema_type = schema.get("type") if isinstance(schema, dict) else None
            if isinstance(schema_type, list):
                schema_type = next((item for item in schema_type if item != "null"), "")
            result.append(
                Assertion(
                    type="json_path",
                    target=target,
                    operator=type_check_operator(schema_type),
                    expected=True,
                )
            )
    return result


def response_properties_for_draft(draft: ScenarioDraft, endpoint: Endpoint) -> dict[str, dict[str, Any]]:
    schema = response_schema_for_draft(draft, endpoint)
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict):
        return {}
    return {
        str(name): value if isinstance(value, dict) else {}
        for name, value in properties.items()
    }


def response_schema_for_draft(draft: ScenarioDraft, endpoint: Endpoint) -> dict[str, Any]:
    status = documented_status_from_basis(draft.expected_basis)
    if status is None:
        for raw in draft.assertions:
            normalized = normalize_draft_assertion(raw)
            if normalized.get("type") != "status_code":
                continue
            expected = normalized.get("expected")
            if isinstance(expected, int):
                status = expected
                break
    responses = endpoint.responses
    response = responses.get(str(status)) or responses.get(status) if status is not None else None
    if not isinstance(response, dict):
        response = next(
            (
                value
                for key, value in responses.items()
                if str(key).startswith("2") and isinstance(value, dict)
            ),
            {},
        )
    schemas: list[Any] = [response.get("schema")]
    content = response.get("content") if isinstance(response.get("content"), dict) else {}
    schemas.extend(
        media.get("schema")
        for media in content.values()
        if isinstance(media, dict)
    )
    for schema in schemas:
        if not isinstance(schema, dict):
            continue
        return schema
    return {}


def mentions_field(text: str, field_name: str) -> bool:
    return bool(
        re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(field_name)}(?![A-Za-z0-9_])",
            str(text or ""),
            flags=re.I,
        )
    )


def mentions_response_field(text: str, field_name: str) -> bool:
    if mentions_field(text, field_name):
        return True
    aliases = {
        "created_at": r"创建时间|刷新时间|创建/刷新时间|(?:unix\s*)?时间戳",
        "expires_in": r"剩余有效期|有效期(?:重置|刷新|重新计算)?|剩余时间",
        "email": r"邮箱地址|邮件地址",
        "valid": r"有效状态|状态(?:为|变为|返回)?(?:有效|无效)",
    }
    pattern = aliases.get(str(field_name).lower())
    return bool(pattern and re.search(pattern, str(text or ""), flags=re.I))


def expected_field_value(text: str, field_name: str) -> tuple[bool, Any]:
    match = re.search(
        rf"(?<![A-Za-z0-9_]){re.escape(field_name)}(?![A-Za-z0-9_])\s*(?::|：|=|为|应为)\s*"
        r"(`[^`]*`|'[^']*'|\"[^\"]*\"|-?\d+(?:\.\d+)?|true|false|null)",
        str(text or ""),
        flags=re.I,
    )
    if not match:
        match = re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(field_name)}(?![A-Za-z0-9_])\s*"
            r"(?:(?:为|是|=|:|：)\s*)?(?:(?:正)?整数|integer|秒数|秒)?\s*[（(]\s*"
            r"(-?\d+(?:\.\d+)?|true|false|null)\s*[）)]",
            str(text or ""),
            flags=re.I,
        )
    if not match:
        return False, None
    raw = match.group(1).strip()
    if raw.startswith(("`", "'", '"')) and raw[-1:] == raw[:1]:
        return True, raw[1:-1]
    lowered = raw.lower()
    if lowered == "true":
        return True, True
    if lowered == "false":
        return True, False
    if lowered == "null":
        return True, None
    try:
        number = float(raw)
    except ValueError:
        return True, raw
    return True, int(number) if number.is_integer() else number


def expected_array_order(text: str) -> tuple[str, str] | None:
    match = re.search(
        r"按\s*([A-Za-z_][A-Za-z0-9_]*)\s*(倒序|降序|升序|正序)",
        str(text or ""),
        flags=re.I,
    )
    if not match:
        return None
    operator = "sorted_desc_by" if match.group(2) in {"倒序", "降序"} else "sorted_asc_by"
    return operator, match.group(1)


def assertion_requires_expected(operator: str) -> bool:
    return operator in {
        "eq",
        "ne",
        "in",
        "gt",
        "gte",
        "lt",
        "lte",
        "contains",
        "matches",
        "regex",
        "starts_with",
        "ends_with",
        "length_eq",
        "gte_path",
        "lte_path",
        "gt_path",
        "lt_path",
        "sorted_desc_by",
        "sorted_asc_by",
        "approx_eq",
    }


def validate_compiled_cases(
    cases: list[TestCase],
    endpoints: list[Endpoint],
    *,
    expected_scenario_ids: Iterable[str] | None = None,
) -> list[DraftIssue]:
    issues: list[DraftIssue] = []
    endpoint_by_key = {(endpoint.method.upper(), endpoint.path): endpoint for endpoint in endpoints}
    scenario_ids = [case.scenario_id for case in cases if case.scenario_id]
    duplicate_ids = {item for item in scenario_ids if scenario_ids.count(item) > 1}
    for scenario_id in sorted(duplicate_ids):
        issues.append(DraftIssue("duplicate_compiled_scenario", "一个场景编译出了多条用例", scenario_id))
    expected = {str(item) for item in (expected_scenario_ids or []) if str(item)}
    actual = set(scenario_ids)
    for scenario_id in sorted(expected - actual):
        issues.append(DraftIssue("missing_compiled_scenario", "场景没有编译出最终用例", scenario_id))
    for scenario_id in sorted(actual - expected) if expected else []:
        issues.append(DraftIssue("unexpected_compiled_scenario", "最终用例不属于当前计划", scenario_id))

    seen_behaviors: dict[str, TestCase] = {}
    for case in cases:
        signature = case_execution_signature(case)
        previous = seen_behaviors.get(signature)
        if previous:
            issues.append(
                DraftIssue(
                    "duplicate_compiled_behavior",
                    f"最终请求、步骤和断言与场景 {previous.scenario_id or previous.id} 完全重复",
                    case.scenario_id,
                    "request",
                    severity=(
                        "warning"
                        if case.rule_ids
                        and previous.rule_ids
                        and set(case.rule_ids).isdisjoint(previous.rule_ids)
                        else "error"
                    ),
                )
            )
        else:
            seen_behaviors[signature] = case

    for case in cases:
        if case.method == "CHAIN":
            if not case.expected_result.strip():
                issues.append(DraftIssue("compiled_missing_expected_result", "最终链路用例缺少预期结果", case.scenario_id))
            issues.extend(validate_step_variables(case, endpoint_by_key))
            issues.extend(validate_chain_expected_result(case, endpoint_by_key))
            continue
        endpoint = endpoint_by_key.get((case.method.upper(), case.path))
        if endpoint is None:
            issues.append(
                DraftIssue(
                    "compiled_unknown_endpoint",
                    f"最终用例引用不存在的接口：{case.method} {case.path}",
                    case.scenario_id,
                )
            )
            continue
        if not case.expected_result.strip():
            issues.append(DraftIssue("compiled_missing_expected_result", "最终用例缺少预期结果", case.scenario_id))
        issues.extend(validate_request_contract(case, endpoint))
        if case_requires_absent_resource(case) and case.setup_steps:
            issues.append(
                DraftIssue(
                    "absent_resource_has_creation_setup",
                    "用例标题要求不存在资源，但前置步骤又创建了该资源",
                    case.scenario_id,
                    "setup_steps",
                )
            )
        issues.extend(
            validate_final_assertions(
                case.scenario_id,
                endpoint,
                case.assertions,
                semantic_text=f"{case.title} {case.expected_result}",
            )
        )
        issues.extend(validate_case_variables(case, endpoint, endpoint_by_key))
    return dedupe_issues(issues)


def validate_chain_expected_result(
    case: TestCase,
    endpoint_by_key: dict[tuple[str, str], Endpoint],
) -> list[DraftIssue]:
    """Ensure claims made by a chain are backed by executable step assertions."""
    issues: list[DraftIssue] = []
    semantic_text = f"{case.title} {case.expected_result}"
    assertions = [assertion for step in case.steps for assertion in step.assertions]
    expected_statuses = expected_statuses_from_text(semantic_text)
    asserted_statuses = status_values(assertions)
    if asserted_statuses and all(status >= 400 for status in asserted_statuses) and has_unnegated_success_claim(
        semantic_text
    ):
        issues.append(
            DraftIssue(
                "error_status_success_expectation_conflict",
                "预期结果声称请求成功，但状态码断言只接受错误响应",
                case.scenario_id,
                "steps.assertions",
            )
        )
    if expected_statuses and not expected_statuses.issubset(asserted_statuses):
        missing = ", ".join(str(item) for item in sorted(expected_statuses - asserted_statuses))
        issues.append(
            DraftIssue(
                "chain_expected_status_not_asserted",
                f"链路预期结果中的状态码没有对应步骤断言：{missing}",
                case.scenario_id,
                "steps.assertions",
            )
        )
    if re.search(r"空数组|empty\s+array", semantic_text, flags=re.I):
        has_empty_assertion = any(
            assertion.type == "json_path"
            and assertion.target in {"", "$"}
            and (
                (assertion.operator == "eq" and assertion.expected == [])
                or (assertion.operator == "length_eq" and assertion.expected == 0)
            )
            for assertion in assertions
        )
        if not has_empty_assertion:
            issues.append(
                DraftIssue(
                    "chain_empty_array_not_asserted",
                    "链路预期空数组，但步骤中只有数组类型校验或没有空数组断言",
                    case.scenario_id,
                    "steps.assertions",
                )
            )
    if re.search(r"相同|一致|\bsame\b|\bequal\b", semantic_text, flags=re.I):
        has_cross_step_comparison = any(
            assertion.operator in {"eq", "ne", "gt", "gte", "lt", "lte", "approx_eq"}
            and isinstance(assertion.expected, str)
            and bool(re.fullmatch(r"\{\{\s*[A-Za-z_][A-Za-z0-9_]*\s*}}", assertion.expected))
            for assertion in assertions
        )
        if not has_cross_step_comparison:
            issues.append(
                DraftIssue(
                    "chain_cross_step_claim_not_asserted",
                    "链路声称前后结果相同或一致，但没有使用前序提取变量进行跨步骤比较",
                    case.scenario_id,
                    "steps.assertions",
                )
            )
    response_field_names: set[str] = set()
    for step in case.steps:
        endpoint = endpoint_by_key.get((step.method.upper(), step.path))
        if endpoint is not None:
            response_field_names.update(response_fields(endpoint))
    for field_name in sorted(response_field_names):
        if not mentions_response_field(semantic_text, field_name):
            continue
        explicit, expected = expected_field_value(semantic_text, field_name)
        matching = [
            assertion
            for assertion in assertions
            if assertion.type == "json_path" and assertion.target == f"$.{field_name}"
        ]
        extracted_for_transfer = any(
            json_path == f"$.{field_name}"
            for step in case.steps
            for json_path in step.extract.values()
        )
        if not matching and not (extracted_for_transfer and not explicit):
            issues.append(
                DraftIssue(
                    "chain_expected_field_not_asserted",
                    f"链路预期结果提到了字段 {field_name}，但没有对应步骤断言",
                    case.scenario_id,
                    "steps.assertions",
                )
            )
            continue
        if (
            matching
            and response_field_claim_requires_value(semantic_text, field_name)
            and not any(assertion_checks_behavior(item) for item in matching)
            and not chain_asserts_equivalent_state_transition(field_name, assertions)
        ):
            issues.append(
                DraftIssue(
                    "chain_response_field_behavior_not_asserted",
                    f"链路声称字段 {field_name} 被刷新、重置或发生变化，但只有类型/存在性断言",
                    case.scenario_id,
                    "steps.assertions",
                )
            )
            continue
        if explicit:
            operator = "is_null" if expected is None else "eq"
            assertion_expected = True if expected is None else expected
            if not any(item.operator == operator and item.expected == assertion_expected for item in matching):
                issues.append(
                    DraftIssue(
                        "chain_expected_field_value_not_asserted",
                        f"链路预期 {field_name}={expected!r}，但步骤断言未校验该值",
                        case.scenario_id,
                        "steps.assertions",
                    )
                )
    return issues


def chain_asserts_equivalent_state_transition(
    field_name: str,
    assertions: list[Assertion],
) -> bool:
    if field_name != "created_at":
        return False
    return any(
        assertion.type == "json_path"
        and (
            (
                assertion.target == "$.expires_in"
                and assertion.operator == "eq"
                and assertion.expected == 600
            )
            or (
                assertion.target == "$.valid"
                and assertion.operator == "eq"
                and assertion.expected is True
            )
        )
        for assertion in assertions
    )


def response_field_claim_requires_value(text: str, field_name: str) -> bool:
    aliases = {
        "created_at": r"created_at|创建时间|刷新时间|时间戳",
        "expires_in": r"expires_in|剩余有效期|有效期|剩余时间",
    }
    field_pattern = aliases.get(str(field_name).lower(), re.escape(str(field_name)))
    action_pattern = r"刷新|重置|变化|改变|更新|延长|重新计算|refresh|reset|change|update|extend"
    value = str(text or "")
    return bool(
        re.search(rf"(?:{field_pattern}).{{0,18}}(?:{action_pattern})", value, flags=re.I)
        or re.search(rf"(?:{action_pattern}).{{0,18}}(?:{field_pattern})", value, flags=re.I)
    )


def assertion_checks_behavior(assertion: Assertion) -> bool:
    return assertion.operator not in {
        "exists",
        "not_exists",
        "not_empty",
        "is_array",
        "is_string",
        "is_integer",
        "is_positive_integer",
        "is_number",
        "is_boolean",
        "is_object",
        "is_null",
    }


def validate_request_contract(case: TestCase, endpoint: Endpoint) -> list[DraftIssue]:
    issues: list[DraftIssue] = []
    semantic_text = f"{case.title} {case.expected_result}".lower()
    allows_required_omission = case.design_method in {"required_field", "auth_error"}
    for parameter in endpoint.parameters:
        name = str(parameter.get("name") or "").strip()
        location = str(parameter.get("in") or "")
        if not name:
            continue
        if location == "path":
            if name not in case.path_params:
                issues.append(
                    DraftIssue(
                        "missing_required_path_parameter",
                        f"最终用例缺少路径参数：{name}",
                        case.scenario_id,
                        "path_params",
                    )
                )
        if parameter.get("required") and not allows_required_omission:
            present = request_parameter_present(case, location, name)
            if not present:
                issues.append(
                    DraftIssue(
                        "missing_required_request_parameter",
                        f"最终用例缺少必填 {location} 参数：{name}",
                        case.scenario_id,
                        location,
                    )
                )
            elif is_schema_placeholder(case.path_params.get(name)):
                issues.append(
                    DraftIssue(
                        "schema_placeholder_request_value",
                        f"路径参数 {name} 仍是 Schema 占位值，未形成可交付测试数据",
                        case.scenario_id,
                        "path_params",
                    )
                )
        if location in {"query", "header", "cookie"} and not parameter.get("required"):
            if request_parameter_present(case, location, name) and semantic_omits_field(semantic_text, name):
                issues.append(
                    DraftIssue(
                        "contradictory_optional_parameter",
                        f"用例语义要求不传 {name}，最终请求却仍携带该 {location} 参数",
                        case.scenario_id,
                        location,
                    )
                )
    schema = request_body_schema(endpoint)
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = {str(item) for item in schema.get("required", []) if str(item)} if isinstance(schema.get("required"), list) else set()
    if isinstance(case.body, dict):
        for name in properties:
            if str(name) not in required and str(name) in case.body and semantic_omits_field(semantic_text, str(name)):
                issues.append(
                    DraftIssue(
                        "contradictory_optional_body_field",
                        f"用例语义要求不传 {name}，最终请求体却仍携带该字段",
                        case.scenario_id,
                        "body",
                    )
                )
    if not allows_required_omission:
        issues.extend(validate_required_body_fields(case.scenario_id, endpoint, case.body, "body"))
    return issues


def request_parameter_present(case: TestCase, location: str, name: str) -> bool:
    if location == "path":
        return name in case.path_params and present_value(case.path_params.get(name))
    if location == "query":
        return name in case.query and present_value(case.query.get(name))
    if location == "header":
        return any(str(key).lower() == name.lower() and present_value(value) for key, value in case.headers.items())
    if location == "cookie":
        cookie = next((str(value) for key, value in case.headers.items() if str(key).lower() == "cookie" and value), "")
        return bool(re.search(rf"(?:^|;\s*){re.escape(name)}=", cookie))
    return True


def present_value(value: Any) -> bool:
    return value is not None and value != ""


def request_body_schema(endpoint: Endpoint) -> dict[str, Any]:
    body = endpoint.request_body if isinstance(endpoint.request_body, dict) else {}
    content = body.get("content") if isinstance(body.get("content"), dict) else {}
    media = content.get("application/json") or next(iter(content.values()), {}) if content else {}
    return media.get("schema", {}) if isinstance(media, dict) and isinstance(media.get("schema"), dict) else {}


def minimal_valid_body(endpoint: Endpoint, body: Any) -> Any:
    if not isinstance(body, dict):
        return body
    schema = request_body_schema(endpoint)
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = {
        str(item)
        for item in schema.get("required", [])
        if str(item)
    } if isinstance(schema.get("required"), list) else set()
    if not properties:
        return body
    return {
        str(name): value
        for name, value in body.items()
        if str(name) in required or str(name) not in properties
    }


def validate_required_body_fields(
    scenario_id: str,
    endpoint: Endpoint,
    body: Any,
    field_name: str,
) -> list[DraftIssue]:
    issues: list[DraftIssue] = []
    request_body = endpoint.request_body if isinstance(endpoint.request_body, dict) else {}
    if request_body.get("required") and body is None:
        issues.append(DraftIssue("missing_required_request_body", "最终用例缺少必填请求体", scenario_id, field_name))
        return issues
    schema = request_body_schema(endpoint)
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    if required and not isinstance(body, dict):
        issues.append(DraftIssue("invalid_required_request_body", "必填请求体必须是对象", scenario_id, field_name))
        return issues
    for name in required:
        if str(name) not in body or body.get(str(name)) is None:
            issues.append(
                DraftIssue(
                    "missing_required_body_field",
                    f"最终用例缺少请求体必填字段：{name}",
                    scenario_id,
                    field_name,
                )
            )
    return issues


def validate_final_assertions(
    scenario_id: str,
    endpoint: Endpoint,
    assertions: list[Assertion],
    *,
    semantic_text: str = "",
    field_name: str = "assertions",
) -> list[DraftIssue]:
    issues: list[DraftIssue] = []
    documented = set(documented_status_codes(endpoint))
    if not any(item.type == "status_code" for item in assertions):
        issues.append(
            DraftIssue(
                "missing_status_assertion",
                "每个可执行请求都必须包含 HTTP 状态码断言",
                scenario_id,
                field_name,
            )
        )
    for item in assertions:
        if item.type == "json_schema":
            try:
                check_schema(item.expected)
            except Exception as exc:
                issues.append(DraftIssue("invalid_assertion_schema", f"Schema 无效：{exc}", scenario_id, field_name))
        if item.type not in ALLOWED_ASSERTION_TYPES:
            issues.append(DraftIssue("invalid_assertion_type", f"不支持的断言类型：{item.type}", scenario_id, field_name))
        if item.operator not in ALLOWED_ASSERTION_OPERATORS:
            issues.append(DraftIssue("invalid_assertion_operator", f"不支持的断言操作符：{item.operator}", scenario_id, field_name))
        if assertion_requires_expected(item.operator) and item.expected is None:
            issues.append(
                DraftIssue(
                    "missing_assertion_expected",
                    f"断言 {item.type} {item.target or '-'} 的 {item.operator} 操作缺少期望值",
                    scenario_id,
                    field_name,
                )
            )
        if item.type == "status_code" and documented:
            values = item.expected if isinstance(item.expected, list) else [item.expected]
            for value in values:
                try:
                    status = int(value)
                except (TypeError, ValueError):
                    continue
                if status not in documented:
                    issues.append(
                        DraftIssue(
                            "undocumented_status_assertion",
                            f"断言了接口文档未声明的状态码：{status}",
                            scenario_id,
                            field_name,
                        )
                    )

    expected_statuses = expected_statuses_from_text(semantic_text)
    asserted_statuses = status_values(assertions)
    if asserted_statuses and all(status >= 400 for status in asserted_statuses) and has_unnegated_success_claim(
        semantic_text
    ):
        issues.append(
            DraftIssue(
                "error_status_success_expectation_conflict",
                "预期结果声称请求成功，但状态码断言只接受错误响应",
                scenario_id,
                field_name,
            )
        )
    if expected_statuses and not expected_statuses.issubset(asserted_statuses):
        missing = ", ".join(str(item) for item in sorted(expected_statuses - asserted_statuses))
        issues.append(
            DraftIssue(
                "expected_status_not_asserted",
                f"预期结果中的状态码没有对应断言：{missing}",
                scenario_id,
                field_name,
            )
        )

    schema = response_schema_for_assertions(endpoint, assertions)
    if not schema:
        return issues
    for item in assertions:
        if item.type != "json_path" or not item.target or item.target == "$":
            continue
        if not json_path_exists_in_schema(item.target, schema):
            issues.append(
                DraftIssue(
                    "assertion_field_not_in_schema",
                    f"断言字段不在对应响应 Schema 中：{item.target}",
                    scenario_id,
                    field_name,
                )
            )
            continue
        field_schema = json_path_schema(item.target, schema)
        if (
            item.operator in {"eq", "ne", "in"}
            and item.expected is not None
            and not (isinstance(item.expected, str) and "{{" in item.expected)
            and not assertion_value_matches_schema(item.expected, field_schema, operator=item.operator)
        ):
            issues.append(
                DraftIssue(
                    "assertion_expected_type_mismatch",
                    f"断言 {item.target} 的期望值类型与响应 Schema 不一致",
                    scenario_id,
                    field_name,
                )
            )

    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    for response_field in properties:
        if not mentions_response_field(semantic_text, str(response_field)):
            continue
        target = f"$.{response_field}"
        matching = [item for item in assertions if item.type == "json_path" and item.target == target]
        textual = [
            item
            for item in assertions
            if item.type == "body_contains" and str(response_field) in str(item.expected or "")
        ]
        if not matching and not textual:
            issues.append(
                DraftIssue(
                    "expected_field_not_asserted",
                    f"预期结果提到了响应字段 {response_field}，但没有对应断言",
                    scenario_id,
                    field_name,
                )
            )
            continue
        explicit, expected = expected_field_value(semantic_text, str(response_field))
        if explicit and not textual:
            operator = "is_null" if expected is None else "eq"
            assertion_expected = True if expected is None else expected
            if not any(item.operator == operator and item.expected == assertion_expected for item in matching):
                issues.append(
                    DraftIssue(
                        "expected_field_value_not_asserted",
                        f"预期结果明确了 {response_field}={expected!r}，但断言未校验该值",
                        scenario_id,
                        field_name,
                    )
                )

    root_type = schema.get("type")
    if root_type == "array" and re.search(r"数组|列表|\barray\b|\blist\b", semantic_text, flags=re.I):
        if not any(item.type == "json_path" and item.target in {"", "$"} and item.operator == "is_array" for item in assertions):
            issues.append(DraftIssue("expected_array_not_asserted", "预期结果要求数组响应，但没有数组类型断言", scenario_id, field_name))
    ordering = expected_array_order(semantic_text)
    if ordering and not any(
        item.type == "json_path" and item.target in {"", "$"} and item.operator == ordering[0] and item.expected == ordering[1]
        for item in assertions
    ):
        issues.append(DraftIssue("expected_order_not_asserted", "预期结果要求响应排序，但没有对应顺序断言", scenario_id, field_name))
    return issues


def has_unnegated_success_claim(text: str) -> bool:
    value = str(text or "")
    for match in re.finditer(r"成功|\bsuccess(?:ful(?:ly)?)?\b", value, flags=re.I):
        prefix = value[max(0, match.start() - 20) : match.start()].lower()
        if re.search(r"(?:不|未|不会|并非|而非|禁止|不能)\s*$", prefix):
            continue
        if re.search(r"\b(?:not|never|without)\s*$", prefix):
            continue
        return True
    return False


def case_execution_signature(case: TestCase) -> str:
    from ai_apitest.core.models import execution_case_options
    payload = {
        **execution_case_options(case),
        "parameter_serialization": case.parameter_serialization,
        "method": case.method.upper(),
        "path": case.path,
        "headers": case.headers,
        "path_params": case.path_params,
        "query": case.query,
        "body": case.body,
        "assertions": to_jsonable(case.assertions),
        "setup_steps": [step_execution_payload(step) for step in case.setup_steps],
        "steps": [step_execution_payload(step) for step in case.steps],
        "cleanup_steps": [step_execution_payload(step) for step in case.cleanup_steps],
        "runnable": case.runnable,
        "fixture_requirements": sorted(case.fixture_requirements),
        "fixture_state_rule_ids": (
            sorted(case.rule_ids)
            if case.fixture_requirements
            else []
        ),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def case_request_signature(case: TestCase) -> str:
    """Execution identity without main-response assertions.

    Two cases that send the same request under the same fixture state can merge
    compatible assertions and rule lineage instead of executing twice.
    """
    from ai_apitest.core.models import execution_case_options
    payload = {
        **execution_case_options(case),
        "parameter_serialization": case.parameter_serialization,
        "method": case.method.upper(),
        "path": case.path,
        "headers": case.headers,
        "path_params": case.path_params,
        "query": case.query,
        "body": case.body,
        "setup_steps": [step_execution_payload(step) for step in case.setup_steps],
        "steps": [step_execution_payload(step) for step in case.steps],
        "cleanup_steps": [step_execution_payload(step) for step in case.cleanup_steps],
        "runnable": case.runnable,
        "risk": case.risk,
        "negative": case.negative,
        "fixture_requirements": sorted(case.fixture_requirements),
        "fixture_state_rule_ids": (
            sorted(case.rule_ids)
            if case.fixture_requirements
            else []
        ),
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def step_execution_payload(step: TestStep) -> dict[str, Any]:
    # name 仅用于展示，不改变实际 HTTP 调用语义，不能阻碍等价链路去重。
    return {
        "poll": step.poll,
        "method": step.method.upper(),
        "path": step.path,
        "headers": step.headers,
        "path_params": step.path_params,
        "query": step.query,
        "parameter_serialization": step.parameter_serialization,
        "body": step.body,
        "assertions": to_jsonable(step.assertions),
        "extract": step.extract,
    }


def validate_case_variables(
    case: TestCase,
    endpoint: Endpoint,
    endpoint_by_key: dict[tuple[str, str], Endpoint],
) -> list[DraftIssue]:
    available = set(BUILTIN_TEMPLATE_VARIABLES) | set(case.variables)
    if case.dataset is not None:
        available.update(template_variables([case.headers, case.path_params, case.query, case.body]))
    for step in case.setup_steps:
        available.update(step.extract)
    required = template_variables([case.headers, case.path_params, case.query, case.body])
    issues = variable_issues(required - available, case.scenario_id, "request")
    available.update(response_fields(endpoint))
    for step in case.cleanup_steps:
        needed = template_variables(
            [step.headers, step.path_params, step.query, step.body, *[item.expected for item in step.assertions]]
        )
        issues.extend(variable_issues(needed - available, case.scenario_id, "cleanup_steps"))
        available.update(step.extract)
    return issues


def validate_step_variables(
    case: TestCase,
    endpoint_by_key: dict[tuple[str, str], Endpoint],
) -> list[DraftIssue]:
    available = set(BUILTIN_TEMPLATE_VARIABLES) | set(case.variables)
    if case.dataset is not None:
        available.update(template_variables([step.headers, step.path_params, step.query, step.body] for step in case.steps))
    issues: list[DraftIssue] = []
    transferred_variables: set[str] = set()
    for index, step in enumerate(case.steps, start=1):
        endpoint = endpoint_by_key.get((step.method.upper(), step.path))
        if endpoint is None:
            issues.append(
                DraftIssue(
                    "compiled_unknown_chain_endpoint",
                    f"最终链路步骤引用不存在的接口：{step.method} {step.path}",
                    case.scenario_id,
                    f"chain_steps.{index}",
                )
            )
            continue
        for name in re.findall(r"\{([^{}]+)\}", endpoint.path):
            if name not in step.path_params:
                issues.append(
                    DraftIssue(
                        "missing_required_path_parameter",
                        f"链路第 {index} 步缺少路径参数：{name}",
                        case.scenario_id,
                        f"chain_steps.{index}.path_params",
                    )
                )
            elif is_schema_placeholder(step.path_params.get(name)):
                issues.append(
                    DraftIssue(
                        "schema_placeholder_request_value",
                        f"链路第 {index} 步的路径参数 {name} 仍是 Schema 占位值",
                        case.scenario_id,
                        f"chain_steps.{index}.path_params",
                    )
                )
        needed = template_variables(
            [step.headers, step.path_params, step.query, step.body, *[item.expected for item in step.assertions]]
        )
        issues.extend(variable_issues(needed - available, case.scenario_id, "chain_steps"))
        transferred_variables.update(needed & available - BUILTIN_TEMPLATE_VARIABLES)
        issues.extend(
            validate_final_assertions(
                case.scenario_id,
                endpoint,
                step.assertions,
                field_name=f"chain_steps.{index}.assertions",
            )
        )
        step_case = TestCase(
            id=case.id,
            title=step.name,
            method=step.method,
            path=step.path,
            design_method="dependency_chain",
            headers=step.headers,
            path_params=step.path_params,
            query=step.query,
            body=step.body,
            scenario_id=case.scenario_id,
        )
        issues.extend(validate_request_contract(step_case, endpoint))
        available.update(step.extract)
    if len(case.steps) > 1 and not transferred_variables:
        issues.append(
            DraftIssue(
                "chain_without_data_transfer",
                "依赖链没有把任何前置步骤提取变量注入后续步骤",
                case.scenario_id,
                "chain_steps",
            )
        )
    for step in case.cleanup_steps:
        needed = template_variables([step.headers, step.path_params, step.query, step.body])
        issues.extend(variable_issues(needed - available, case.scenario_id, "cleanup_steps"))
        available.update(step.extract)
    return issues


def variable_issues(missing: set[str], scenario_id: str, field_name: str) -> list[DraftIssue]:
    return [
        DraftIssue(
            "undefined_template_variable",
            f"模板变量没有生产来源：{name}",
            scenario_id,
            field_name,
        )
        for name in sorted(missing)
    ]


def response_fields(endpoint: Endpoint) -> set[str]:
    fields: set[str] = set()
    for response in endpoint.responses.values():
        if not isinstance(response, dict):
            continue
        schemas = [response.get("schema")]
        for media in (response.get("content") or {}).values():
            if isinstance(media, dict):
                schemas.append(media.get("schema"))
        for schema in schemas:
            if isinstance(schema, dict):
                fields.update(str(name) for name in (schema.get("properties") or {}))
    return fields


def expected_statuses_from_text(text: str) -> set[int]:
    statuses: set[int] = set()
    for pattern in (
        r"\bHTTP\s*([1-5]\d{2})\b",
        r"(?:返回|响应|状态码(?:为|是|=)?|status(?:_code)?\s*(?:=|为|是)?)\s*([1-5]\d{2})\b",
        r"\b([1-5]\d{2})\s+(?:Not Found|OK|Created|No Content|Bad Request|Unauthorized|Forbidden|Unprocessable|Too Many)",
    ):
        for match in re.finditer(pattern, str(text or ""), flags=re.I):
            if status_mention_is_negated(str(text or ""), match.start(), match.end()):
                continue
            statuses.add(int(match.group(1)))
    return statuses


def status_mention_is_negated(text: str, start: int, end: int) -> bool:
    prefix = text[max(0, start - 24) : start].lower()
    suffix = text[end : min(len(text), end + 24)].lower()
    return bool(
        re.search(r"(?:不|未|不会|不能|并非|而非|避免|禁止)\s*$", prefix)
        or re.search(r"\bnot\s+(?:return|respond(?:\s+with)?|be)\s*$", prefix)
        or re.match(r"\s*(?:而是|instead|should\s+not\s+be\s+returned)", suffix)
    )


def status_values(assertions: list[Assertion]) -> set[int]:
    values: set[int] = set()
    for assertion in assertions:
        if assertion.type != "status_code":
            continue
        expected = assertion.expected if isinstance(assertion.expected, list) else [assertion.expected]
        for value in expected:
            try:
                values.add(int(value))
            except (TypeError, ValueError):
                continue
    return values


def response_schema_for_assertions(endpoint: Endpoint, assertions: list[Assertion]) -> dict[str, Any]:
    statuses = status_values(assertions)
    response: Any = None
    for status in statuses:
        response = endpoint.responses.get(str(status)) or endpoint.responses.get(status)
        if isinstance(response, dict):
            break
    if not isinstance(response, dict):
        return {}
    schemas: list[Any] = [response.get("schema")]
    content = response.get("content") if isinstance(response.get("content"), dict) else {}
    schemas.extend(media.get("schema") for media in content.values() if isinstance(media, dict))
    return next((schema for schema in schemas if isinstance(schema, dict)), {})


def json_path_exists_in_schema(path: str, schema: dict[str, Any]) -> bool:
    target = str(path or "")
    current: Any = schema
    tokens = re.findall(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[(?:\d+|\*)\]", target)
    names = [name for name in tokens if name]
    if target.startswith("$[") and isinstance(current, dict):
        current = current.get("items") or {}
    for name in names:
        if not isinstance(current, dict):
            return False
        if current.get("type") == "array":
            current = current.get("items") or {}
        properties = current.get("properties") if isinstance(current.get("properties"), dict) else {}
        if name not in properties:
            return False
        current = properties[name]
    return True


def json_path_schema(path: str, schema: dict[str, Any]) -> dict[str, Any]:
    current: Any = schema
    target = str(path or "")
    if target.startswith("$[") and isinstance(current, dict):
        current = current.get("items") or {}
    for name in [item for item in re.findall(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[(?:\d+|\*)\]", target) if item]:
        if not isinstance(current, dict):
            return {}
        if current.get("type") == "array":
            current = current.get("items") or {}
        properties = current.get("properties") if isinstance(current.get("properties"), dict) else {}
        current = properties.get(name)
        if not isinstance(current, dict):
            return {}
    return current if isinstance(current, dict) else {}


def assertion_value_matches_schema(value: Any, schema: dict[str, Any], *, operator: str) -> bool:
    if not schema:
        return True
    candidates = value if operator == "in" and isinstance(value, list) else [value]
    schema_type = schema.get("type")
    allowed = set(schema_type) if isinstance(schema_type, list) else {schema_type}
    allowed.discard(None)
    if not allowed:
        return True

    def matches(item: Any) -> bool:
        if item is None:
            return "null" in allowed or bool(schema.get("nullable"))
        if isinstance(item, bool):
            return "boolean" in allowed
        if isinstance(item, int):
            return bool({"integer", "number"} & allowed)
        if isinstance(item, float):
            return "number" in allowed
        if isinstance(item, str):
            return "string" in allowed
        if isinstance(item, list):
            return "array" in allowed
        if isinstance(item, dict):
            return "object" in allowed
        return True

    return all(matches(item) for item in candidates)


def parse_assertions(value: Any) -> list[Assertion]:
    if not isinstance(value, list):
        return []
    assertions: list[Assertion] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        try:
            normalized = normalize_draft_assertion(raw)
            if not normalized.get("type"):
                continue
            assertions.append(assertion_from_dict(normalized))
        except (KeyError, TypeError, ValueError):
            continue
    return assertions


def remove_redundant_shape_assertions(assertions: list[Assertion]) -> list[Assertion]:
    shape_operators = {
        "exists",
        "is_string",
        "is_integer",
        "is_positive_integer",
        "is_number",
        "is_boolean",
        "is_object",
    }
    stronger_targets = {
        item.target
        for item in assertions
        if item.type == "json_path"
        and item.target
        and item.operator not in shape_operators
    }
    result: list[Assertion] = []
    seen: set[str] = set()
    for item in assertions:
        if (
            item.type == "json_path"
            and item.target in stronger_targets
            and item.operator in shape_operators
        ):
            continue
        if (
            item.type == "json_path"
            and item.operator in {"matches", "regex"}
            and broad_string_pattern_is_implied(item, assertions)
        ):
            continue
        signature = json.dumps(to_jsonable(item), ensure_ascii=False, sort_keys=True, default=str)
        if signature in seen:
            continue
        seen.add(signature)
        result.append(item)
    return result


def broad_string_pattern_is_implied(
    candidate: Assertion,
    assertions: list[Assertion],
) -> bool:
    pattern = str(candidate.expected or "")
    return any(
        other is not candidate
        and other.type == "json_path"
        and other.target == candidate.target
        and (
            (
                other.operator in {"matches", "regex"}
                and str(other.expected or "") != pattern
                and regex_pattern_implies(str(other.expected or ""), pattern)
            )
            or (
                other.operator == "eq"
                and isinstance(other.expected, str)
                and regex_matches_value(pattern, other.expected)
            )
        )
        for other in assertions
    )


def regex_matches_value(pattern: str, value: str) -> bool:
    try:
        return re.fullmatch(pattern, value) is not None
    except re.error:
        return False


def normalize_draft_assertion(raw: dict[str, Any]) -> dict[str, Any]:
    item = dict(raw)
    assertion_type = str(item.get("type") or "").strip()
    if assertion_type in {"field", "jsonpath"}:
        assertion_type = "json_path"
    check = str(item.get("check") or "").strip()
    raw_operator = str(item.get("operator") or "").strip()
    if not assertion_type and raw_operator in {"status", "status_code", "response_status"}:
        return {
            "type": "status_code",
            "operator": "in" if isinstance(item.get("value"), list) else "eq",
            "expected": item["expected"] if "expected" in item else item.get("value"),
        }
    raw_field = str(item.get("field") or item.get("path") or item.get("json_path") or "").strip()
    raw_expected = item["expected"] if "expected" in item else item.get("value")
    if (
        not assertion_type
        and raw_field.lower() in {"status", "http_status", "status_code", "response_status", "response.status_code"}
        and isinstance(raw_expected, int)
        and not isinstance(raw_expected, bool)
        and 100 <= raw_expected <= 599
    ):
        return {"type": "status_code", "operator": "eq", "expected": raw_expected}
    if not assertion_type and check:
        if check == "status_code" or check.endswith("_status_code"):
            assertion_type = "status_code"
        elif "response_body." in check:
            assertion_type = "json_path"
            item["path"] = "$." + check.split("response_body.", 1)[1]
        else:
            return {}
    if not assertion_type and (
        item.get("field") is not None or item.get("path") is not None or item.get("json_path") is not None
    ):
        operator, expected = normalize_semantic_comparison(
            str(item.get("operator") or "eq"),
            item["expected"] if "expected" in item else item.get("value"),
        )
        return {
            "type": "json_path",
            "target": json_path_target(str(item.get("field") or item.get("path") or item.get("json_path") or "$")),
            "operator": operator,
            "expected": expected,
        }
    if assertion_type in {"status_code", "response_status", "status"}:
        expected = item["expected"] if "expected" in item else item.get("value")
        return {
            "type": "status_code",
            "operator": "in" if isinstance(expected, list) else str(item.get("operator") or "eq"),
            "expected": expected,
        }
    if assertion_type == "status_code_range":
        expected = item.get("expected")
        return {"type": "status_code", "operator": "in", "expected": expected if isinstance(expected, list) else [expected]}
    if assertion_type == "json_field_exists":
        path = str(item.get("path") or item.get("target") or "").strip()
        return {
            "type": "json_path",
            "target": json_path_target(path),
            "operator": "exists",
            "expected": True,
        }
    if assertion_type in {"response_json", "json_field", "json_field_compare", "json"}:
        operator = str(item.get("operator") or item.get("condition") or "eq").strip()
        expected = item["expected"] if "expected" in item else item.get("value")
        if assertion_type == "json_field" and str(expected).lower() in {
            "array", "string", "integer", "number", "boolean", "object"
        } and not item.get("operator"):
            return {
                "type": "json_path",
                "target": json_path_target(str(item.get("field") or item.get("path") or item.get("target") or "")),
                "operator": type_check_operator(expected),
                "expected": True,
            }
        if "contains" in item:
            operator = "contains"
            expected = item.get("contains")
        return {
            "type": "json_path",
            "target": json_path_target(str(item.get("field") or item.get("path") or item.get("target") or "")),
            "operator": normalize_semantic_operator(operator),
            "expected": expected,
        }
    if assertion_type == "json_type":
        expected_type = str(item.get("expected") or item.get("value") or "").strip().lower()
        operator = {
            "array": "is_array",
            "string": "is_string",
            "integer": "is_integer",
            "number": "is_number",
            "boolean": "is_boolean",
        }.get(expected_type, "exists")
        return {
            "type": "json_path",
            "target": json_path_target(str(item.get("path") or item.get("field") or "$")),
            "operator": operator,
            "expected": True,
        }
    if assertion_type == "json_length":
        return {
            "type": "json_path",
            "target": json_path_target(str(item.get("path") or item.get("field") or "$")),
            "operator": "length_eq",
            "expected": item.get("expected", item.get("value")),
        }
    if assertion_type == "response_present":
        return {"type": "json_path", "target": "$", "operator": "exists", "expected": True}
    if assertion_type == "json_schema":
        return {"type": "json_schema", "target": "$", "operator": "eq", "expected": item.get("schema", item.get("expected"))}
    if assertion_type == "field_exists":
        return {
            "type": "json_path",
            "target": json_path_target(str(item.get("field") or item.get("path") or "")),
            "operator": "exists",
            "expected": True,
        }
    if assertion_type == "field_type":
        expected_type = str(item.get("expected") or item.get("type_check") or "")
        operator = "is_positive_integer" if expected_type == "positive_integer" else type_check_operator(expected_type)
        return {
            "type": "json_path",
            "target": json_path_target(str(item.get("field") or item.get("path") or "$")),
            "operator": operator,
            "expected": True,
        }
    if assertion_type == "field_value":
        return {
            "type": "json_path",
            "target": json_path_target(str(item.get("field") or item.get("path") or "")),
            "operator": normalize_semantic_operator(str(item.get("operator") or "eq")),
            "expected": item["expected"] if "expected" in item else item.get("value"),
        }
    shorthand = {
        "field_equals": "eq",
        "field_greater_than": "gt",
        "field_less_than": "lt",
        "field_approx_equal": "approx_eq",
    }
    if assertion_type in shorthand:
        operator, expected = normalize_semantic_comparison(
            shorthand[assertion_type],
            item["expected"] if "expected" in item else item.get("value"),
        )
        return {
            "type": "json_path",
            "target": json_path_target(str(item.get("field") or item.get("path") or "$")),
            "operator": operator,
            "expected": expected,
        }
    if assertion_type in {"array_empty", "array_not_empty"}:
        return {
            "type": "json_path",
            "target": json_path_target(str(item.get("field") or item.get("path") or "$")),
            "operator": "length_eq" if assertion_type == "array_empty" else "not_empty",
            "expected": 0 if assertion_type == "array_empty" else True,
        }
    if assertion_type == "response_body":
        if item.get("pattern") is not None:
            operator = "matches"
            expected = item.get("pattern")
        elif item.get("type_check") is not None:
            operator = type_check_operator(item.get("type_check"))
            expected = True
        else:
            operator = normalize_semantic_operator(str(item.get("condition") or item.get("operator") or "eq"))
            expected = item["expected"] if "expected" in item else item.get("value")
            if expected is None and "expected" in item:
                operator = "is_null"
                expected = True
        return {
            "type": "json_path",
            "target": json_path_target(str(item.get("field") or item.get("path") or "$")),
            "operator": operator,
            "expected": expected,
        }
    if assertion_type == "json_path":
        operator = str(item.get("operator") or item.get("assertion") or item.get("matcher") or "eq").strip()
        expected = item["expected"] if "expected" in item else item.get("value", item.get("variable"))
        target = json_path_target(str(item.get("target") or item.get("path") or item.get("field") or "$"))
        if target == "$.length" and operator in {"eq", "equals", "equal", "=="}:
            return {"type": "json_path", "target": "$", "operator": "length_eq", "expected": expected}
        if operator == "type":
            return {
                "type": "json_path",
                "target": target,
                "operator": type_check_operator(expected),
                "expected": True,
            }
        if item.get("pattern") is not None:
            operator = "matches"
            expected = item.get("pattern")
        elif item.get("type_check") is not None:
            operator = type_check_operator(item.get("type_check"))
            expected = True
        if "contains" in item:
            operator = "contains"
            expected = item.get("contains")
        if operator in {"matches", "regex"} and expected is None:
            expected = item.get("pattern")
        if expected is None and "expected" in item:
            operator = "is_null"
            expected = True
        operator, expected = normalize_semantic_comparison(operator, expected)
        return {
            "type": "json_path",
            "target": target,
            "operator": operator,
            "expected": expected,
        }
    if assertion_type == "body_contains":
        return {
            "type": "body_contains",
            "target": str(item.get("target") or ""),
            "operator": str(item.get("operator") or "contains"),
            "expected": item["expected"] if "expected" in item else item.get("value"),
        }
    return item


def normalize_semantic_operator(value: str) -> str:
    return {
        "equals": "eq",
        "equal": "eq",
        "greater_than": "gt",
        "greater_or_equal": "gte",
        "less_than": "lt",
        "less_or_equal": "lte",
        "exists": "exists",
        "not_empty": "not_empty",
        "matches": "matches",
        "contains": "contains",
        "length_equals": "length_eq",
        "approx_equal": "approx_eq",
    }.get(str(value or "eq").strip(), str(value or "eq").strip())


def type_check_operator(value: Any) -> str:
    return {
        "array": "is_array",
        "string": "is_string",
        "integer": "is_integer",
        "number": "is_number",
        "boolean": "is_boolean",
        "object": "is_object",
    }.get(str(value or "").strip().lower(), "exists")


def normalize_semantic_comparison(operator: str, expected: Any) -> tuple[str, Any]:
    operator = normalize_semantic_operator(operator)
    text = str(expected or "").strip()
    if text in {"存在且非空", "非空", "not empty"}:
        return "not_empty", True
    match = re.fullmatch(r"大于\s*(-?\d+(?:\.\d+)?)", text)
    if match:
        value = float(match.group(1))
        return "gt", int(value) if value.is_integer() else value
    match = re.fullmatch(r"大于等于\s*(\$[^\s]+)", text)
    if match:
        return "gte_path", match.group(1)
    return operator, expected


def normalize_chain_step(raw: dict[str, Any]) -> dict[str, Any]:
    step = copy.deepcopy(raw)
    raw_path = str(step.get("path") or step.get("endpoint") or step.get("endpoint_path") or "")
    template_names = re.findall(r"\{\{\s*([^{}]+?)\s*\}\}", raw_path)
    step["path"] = re.sub(r"\{\{\s*([^{}]+?)\s*\}\}", r"{\1}", raw_path)
    if not step.get("method"):
        step["method"] = str(step.get("http_method") or step.get("endpoint_method") or "")
    if "query" not in step and isinstance(step.get("query_params"), dict):
        step["query"] = dict(step["query_params"])

    expect = step.get("expect") if isinstance(step.get("expect"), dict) else {}
    assertions = [dict(item) for item in step.get("assertions") or [] if isinstance(item, dict)]
    expected_status = expect.get("status")
    if expected_status is None:
        expected_status = step.get("expect_status")
    if expected_status is not None:
        assertions.insert(0, {"type": "status_code", "operator": "eq", "expected": expected_status})
    assertions.extend(dict(item) for item in expect.get("assertions") or [] if isinstance(item, dict))
    if assertions:
        step["assertions"] = assertions

    raw_extract = step.get("extract") if isinstance(step.get("extract"), dict) else {}
    capture = expect.get("capture") if isinstance(expect.get("capture"), dict) else {}
    extract: dict[str, str] = {}
    step_number = str(step.get("step") or "").strip()
    direct_json_path = str(raw_extract.get("json_path") or raw_extract.get("jsonpath") or "").strip()
    direct_variable = canonical_variable_name(str(raw_extract.get("variable") or raw_extract.get("as") or ""))
    if direct_variable and direct_json_path.startswith("$"):
        extract[direct_variable] = direct_json_path
    for key, value in raw_extract.items():
        if key in {"json_path", "jsonpath", "variable", "as"}:
            continue
        if isinstance(value, str) and value.startswith("$"):
            extract[canonical_variable_name(str(key))] = value
            continue
        if not isinstance(value, dict):
            continue
        field_name = str(value.get("field") or key).strip()
        variable = canonical_variable_name(str(value.get("variable") or value.get("as") or key))
        json_path = str(value.get("jsonpath") or value.get("json_path") or value.get("path") or "").strip()
        if not json_path and field_name:
            json_path = field_name if field_name.startswith("$") else f"$.{field_name}"
        if variable and json_path.startswith("$"):
            extract[variable] = json_path
            if step_number and field_name:
                extract.setdefault(canonical_variable_name(f"step{step_number}.{field_name}"), json_path)
    if capture:
        capture_path = str(capture.get("json_path") or capture.get("jsonpath") or capture.get("path") or "").strip()
        capture_variable = canonical_variable_name(str(capture.get("variable") or capture.get("as") or ""))
        if capture_variable and capture_path.startswith("$"):
            extract[capture_variable] = capture_path
    field = str(raw_extract.get("field") or "").strip()
    if field:
        variable = canonical_variable_name(str(raw_extract.get("variable") or raw_extract.get("as") or field))
        extract.setdefault(variable, f"$.{field}")
    step["extract"] = extract

    path_params = dict(step.get("path_params") or step.get("path_parameters") or {})
    for name in template_names:
        path_params.setdefault(name, "{{" + name + "}}")
    inject = step.get("inject") if isinstance(step.get("inject"), dict) else {}
    for section in ("path", "path_params", "path_parameters"):
        values = inject.get(section)
        if isinstance(values, dict):
            path_params.update(values)
    for dotted, value in inject.items():
        dotted = str(dotted)
        if dotted.startswith(("path.", "path_params.", "path_parameters.")):
            path_params[dotted.split(".", 1)[1]] = value
    if str(inject.get("to") or "") in {"path", "path_parameter"}:
        variable = str(inject.get("variable") or inject.get("from") or inject.get("field") or "").strip()
        target = str(inject.get("target_field") or inject.get("target") or variable).strip()
        target = str(inject.get("field") or target).strip()
        if variable and target:
            path_params[target] = "{{" + variable + "}}"
    if path_params:
        step["path_params"] = normalize_template_values(path_params)
    return step


def normalize_chain_step_sequence(raw_steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in raw_steps:
        step = copy.deepcopy(raw)
        action = str(step.get("action") or "").strip().lower()
        if action == "extract" and result:
            field = str(step.get("field") or "").strip()
            variable = str(step.get("as") or step.get("variable") or field).strip()
            if field and variable:
                json_path = field if field.startswith("$") else f"$.{field}"
                result[-1].setdefault("extract", {})[variable] = json_path
            continue
        if action == "call" and isinstance(step.get("call"), dict):
            step = {**step, **step["call"]}
        if action == "inject":
            method = str(step.get("target_method") or step.get("method") or "").strip()
            path = str(step.get("target_path") or step.get("path") or "").strip()
            variable = str(step.get("variable") or step.get("from") or "").strip()
            inject_to = str(step.get("inject_to") or step.get("to") or "").strip()
            if method and path:
                step = {**step, "method": method, "path": path}
                if variable and inject_to:
                    section, _, name = inject_to.partition(".")
                    if section in {"path", "path_params", "path_parameters"} and name:
                        step.setdefault("path_params", {})[name] = "{{" + canonical_variable_name(variable) + "}}"
        normalized_step = normalize_chain_step(step)
        if not normalized_step.get("method") or not normalized_step.get("path"):
            continue
        result.append(normalized_step)
    return result


def hydrate_chain_data_flow(
    raw_steps: list[dict[str, Any]],
    endpoint_by_key: dict[tuple[str, str], Endpoint],
) -> list[dict[str, Any]]:
    steps = [normalize_chain_step(item) for item in raw_steps]
    future_path_fields: list[set[str]] = []
    for step in steps:
        method = str(step.get("method") or "").upper()
        path = str(step.get("path") or "")
        endpoint = endpoint_by_key.get((method, path)) or resolve_chain_endpoint(method, path, endpoint_by_key)
        future_path_fields.append(set(re.findall(r"\{([^{}]+)\}", endpoint.path if endpoint else path)))

    produced: dict[str, str] = {}
    for index, step in enumerate(steps):
        method = str(step.get("method") or "").upper()
        path = str(step.get("path") or "")
        endpoint = endpoint_by_key.get((method, path)) or resolve_chain_endpoint(method, path, endpoint_by_key)
        if endpoint is not None:
            step = remap_chain_path_params(step, endpoint.path) if path != endpoint.path else step
            path_params = dict(step.get("path_params") or {})
            for field_name in re.findall(r"\{([^{}]+)\}", endpoint.path):
                variable = produced.get(field_name)
                current = path_params.get(field_name)
                if variable and (current is None or is_schema_placeholder(current)):
                    path_params[field_name] = "{{" + variable + "}}"
            if path_params:
                step["path_params"] = path_params

            needed_later = set().union(*future_path_fields[index + 1 :]) if index + 1 < len(steps) else set()
            extracts = dict(step.get("extract") or {})
            for field_name in sorted(response_fields(endpoint) & needed_later):
                existing = next(
                    (name for name, json_path in extracts.items() if json_path == f"$.{field_name}"),
                    "",
                )
                variable = existing or field_name
                extracts.setdefault(variable, f"$.{field_name}")
                produced[field_name] = variable
            step["extract"] = extracts
        for variable, json_path in dict(step.get("extract") or {}).items():
            match = re.fullmatch(r"\$\.([A-Za-z_][A-Za-z0-9_]*)", str(json_path))
            if match:
                produced[match.group(1)] = str(variable)
        steps[index] = step
    return steps


def is_schema_placeholder(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text in {"string", "integer", "number", "boolean", "object", "array", "value"} or bool(
        re.fullmatch(r"<[^<>]+>", text)
    )


def resolve_chain_endpoint(
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


def remap_chain_path_params(step: dict[str, Any], endpoint_path: str) -> dict[str, Any]:
    normalized = copy.deepcopy(step)
    source_names = re.findall(r"\{([^{}]+)\}", str(step.get("path") or ""))
    target_names = re.findall(r"\{([^{}]+)\}", endpoint_path)
    path_params = dict(step.get("path_params") or {})
    for source, target in zip(source_names, target_names):
        value = path_params.pop(source, "{{" + source + "}}")
        path_params[target] = value
    normalized["path"] = endpoint_path
    normalized["path_params"] = path_params
    return normalized


def normalize_request_overrides(overrides: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in overrides.items():
        normalized_key = str(key)
        if normalized_key in {"path", "path_parameters"} and isinstance(value, dict):
            normalized_key = "path_params"
        elif normalized_key.startswith("path."):
            normalized_key = "path_params." + normalized_key.split(".", 1)[1]
        elif normalized_key != "path_params" and normalized_key.startswith("path_") and len(normalized_key) > len("path_"):
            normalized_key = "path_params." + normalized_key[len("path_") :]
        elif normalized_key.startswith("query_") and len(normalized_key) > len("query_"):
            normalized_key = "query." + normalized_key[len("query_") :]
        result[normalized_key] = normalize_template_values(value)
    return result


def normalize_template_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): normalize_template_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize_template_values(item) for item in value]
    if isinstance(value, str):
        normalized = value.replace("%s", "{{timestamp}}")
        normalized = re.sub(r"\$\{([^{}]+)\}", r"{{\1}}", normalized)
        direct_variable = re.fullmatch(r"\$([A-Za-z_][A-Za-z0-9_]*)", normalized)
        if direct_variable:
            return "{{" + canonical_variable_name(direct_variable.group(1)) + "}}"
        return re.sub(
            r"\{\{\s*([^{}]+?)\s*\}\}",
            lambda match: "{{" + canonical_variable_name(match.group(1)) + "}}",
            normalized,
        )
    return value


def canonical_variable_name(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "").strip()).strip("_")
    if normalized and normalized[0].isdigit():
        normalized = "var_" + normalized
    return normalized


def asserted_status_from_raw(assertions: list[dict[str, Any]]) -> int | None:
    for raw in assertions:
        normalized = normalize_draft_assertion(raw)
        if normalized.get("type") != "status_code":
            continue
        expected = normalized.get("expected")
        if isinstance(expected, list):
            if len(expected) != 1:
                continue
            expected = expected[0]
        try:
            return int(expected)
        except (TypeError, ValueError):
            continue
    return None


def json_path_target(value: str) -> str:
    path = str(value or "").strip()
    if not path:
        return "$"
    if path == "$" or path.startswith("$.") or path.startswith("$["):
        return path
    return "$." + path.lstrip(".")


def documented_status_from_basis(value: str) -> int | None:
    match = re.search(r"(?:^|\.)responses\.(\d{3})(?!\d)", str(value or ""))
    return int(match.group(1)) if match else None


def template_variables(values: Iterable[Any]) -> set[str]:
    found: set[str] = set()
    for value in values:
        if isinstance(value, dict):
            found.update(template_variables(value.values()))
        elif isinstance(value, list):
            found.update(template_variables(value))
        elif isinstance(value, str):
            found.update(match.strip() for match in re.findall(r"\{\{\s*([^{}]+?)\s*\}\}", value))
    return found


def set_dotted_value(container: dict[str, Any], path: str, value: Any) -> None:
    parts = [part for part in path.split(".") if part]
    if not parts:
        return
    current: dict[str, Any] = container
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = copy.deepcopy(value)


def remove_dotted_value(container: dict[str, Any], path: str) -> None:
    parts = [part for part in path.split(".") if part]
    if not parts:
        return
    current: Any = container
    for part in parts[:-1]:
        if not isinstance(current, dict) or part not in current:
            return
        current = current[part]
    if isinstance(current, dict):
        current.pop(parts[-1], None)


def deep_merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge_dict(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def string_items(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return unique_strings(str(item).strip() for item in value if str(item).strip())


def dict_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [copy.deepcopy(item) for item in value if isinstance(item, dict)]


def unique_strings(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def dedupe_issues(issues: Iterable[DraftIssue]) -> list[DraftIssue]:
    result: list[DraftIssue] = []
    seen: set[tuple[str, str, str, str]] = set()
    for issue in issues:
        key = (issue.code, issue.scenario_id, issue.field, issue.message)
        if key not in seen:
            seen.add(key)
            result.append(issue)
    return result
