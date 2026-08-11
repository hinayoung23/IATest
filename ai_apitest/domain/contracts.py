from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Iterable

from ai_apitest.core.models import Endpoint, TestCase
from ai_apitest.core.serde import to_jsonable


TRUSTED_CONTRACT_VERSION = "2026-08-03.15"
BUSINESS_RULE_VERSION = "1"
SUPPORTED_RULE_STRATEGIES = {
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


def merge_endpoint_contract(deterministic: Endpoint, semantic: Endpoint | None) -> Endpoint:
    """Merge parser facts with source-backed semantic enrichment field by field.

    The parser remains authoritative for endpoint identity. A parser heading or
    placeholder parameter must never replace a richer value extracted from the
    same source document.
    """
    if semantic is None:
        return normalize_endpoint_contract(copy.deepcopy(deterministic))
    return normalize_endpoint_contract(
        Endpoint(
            method=deterministic.method.upper(),
            path=deterministic.path,
            summary=prefer_richer_text(deterministic.summary, semantic.summary),
            description=prefer_richer_text(deterministic.description, semantic.description),
            parameters=merge_parameters(deterministic.parameters, semantic.parameters),
            request_body=merge_optional_mapping(deterministic.request_body, semantic.request_body),
            responses=merge_mapping(deterministic.responses, semantic.responses),
            tags=unique_strings([*deterministic.tags, *semantic.tags]),
            operation_id=deterministic.operation_id or semantic.operation_id,
            deprecated=deterministic.deprecated or semantic.deprecated,
            security=merge_security(deterministic.security, semantic.security),
            business_rules=merge_rule_lists(
                deterministic.business_rules,
                semantic.business_rules,
                method=deterministic.method,
                path=deterministic.path,
            ),
        )
    )


def normalize_endpoint_contract(endpoint: Endpoint) -> Endpoint:
    endpoint.method = endpoint.method.upper()
    endpoint.summary = str(endpoint.summary or "").strip()
    endpoint.description = str(endpoint.description or "").strip()
    endpoint.parameters = [
        enrich_parameter_constraints(copy.deepcopy(item))
        for item in endpoint.parameters
        if isinstance(item, dict)
    ]
    normalized_rules = normalize_business_rules(
        endpoint.business_rules,
        method=endpoint.method,
        path=endpoint.path,
    )
    endpoint.business_rules = enrich_business_rule_contracts(
        drop_redundant_parameter_schema_rules(normalized_rules),
        endpoint,
    )
    return endpoint


def prefer_richer_text(parser_value: Any, semantic_value: Any) -> str:
    parser_text = str(parser_value or "").strip()
    semantic_text = str(semantic_value or "").strip()
    if not semantic_text:
        return parser_text
    if not parser_text:
        return semantic_text
    parser_compact = compact_text(parser_text)
    semantic_compact = compact_text(semantic_text)
    if parser_compact == semantic_compact:
        return semantic_text if len(semantic_text) >= len(parser_text) else parser_text
    if parser_compact in semantic_compact:
        return semantic_text
    if semantic_compact in parser_compact:
        return parser_text
    # Parser descriptions for Markdown are commonly only the section heading.
    # Prefer materially richer source-backed prose without concatenating two
    # competing contract statements.
    if len(semantic_compact) >= len(parser_compact) + 20:
        return semantic_text
    return parser_text


def merge_parameters(
    parser_items: Iterable[dict[str, Any]],
    semantic_items: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    semantic_by_key = {
        (str(item.get("in") or ""), str(item.get("name") or "")): item
        for item in semantic_items
        if isinstance(item, dict)
    }
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for parser_item in parser_items:
        if not isinstance(parser_item, dict):
            continue
        key = (str(parser_item.get("in") or ""), str(parser_item.get("name") or ""))
        semantic_item = semantic_by_key.get(key)
        merged.append(merge_parameter(parser_item, semantic_item))
        seen.add(key)
    for key, semantic_item in semantic_by_key.items():
        if key not in seen:
            merged.append(enrich_parameter_constraints(copy.deepcopy(semantic_item)))
    return merged


def merge_parameter(parser_item: dict[str, Any], semantic_item: dict[str, Any] | None) -> dict[str, Any]:
    if semantic_item is None:
        return enrich_parameter_constraints(copy.deepcopy(parser_item))
    result = merge_mapping(parser_item, semantic_item)
    result["name"] = str(parser_item.get("name") or semantic_item.get("name") or "")
    result["in"] = str(parser_item.get("in") or semantic_item.get("in") or "")
    if "required" in parser_item:
        result["required"] = bool(parser_item.get("required"))
    result["description"] = prefer_richer_text(
        parser_item.get("description"),
        semantic_item.get("description"),
    )
    result["schema"] = merge_mapping(
        parser_item.get("schema") if isinstance(parser_item.get("schema"), dict) else {},
        semantic_item.get("schema") if isinstance(semantic_item.get("schema"), dict) else {},
    )
    return enrich_parameter_constraints(result)


def merge_optional_mapping(parser_value: Any, semantic_value: Any) -> Any:
    if parser_value is None:
        return copy.deepcopy(semantic_value)
    if semantic_value is None:
        return copy.deepcopy(parser_value)
    if isinstance(parser_value, dict) and isinstance(semantic_value, dict):
        return merge_mapping(parser_value, semantic_value)
    return copy.deepcopy(parser_value)


def merge_mapping(parser_value: Any, semantic_value: Any) -> Any:
    if not isinstance(parser_value, dict):
        return copy.deepcopy(semantic_value)
    if not isinstance(semantic_value, dict):
        return copy.deepcopy(parser_value)
    result: dict[str, Any] = {}
    for key in set(parser_value) | set(semantic_value):
        parser_item = parser_value.get(key)
        semantic_item = semantic_value.get(key)
        if key == "description":
            result[key] = prefer_richer_text(parser_item, semantic_item)
        elif isinstance(parser_item, dict) and isinstance(semantic_item, dict):
            result[key] = merge_mapping(parser_item, semantic_item)
        elif parser_item in (None, "", [], {}):
            result[key] = copy.deepcopy(semantic_item)
        else:
            result[key] = copy.deepcopy(parser_item)
    return result


def merge_security(parser_items: list[dict[str, Any]], semantic_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    serialized: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in [*parser_items, *semantic_items]:
        if not isinstance(item, dict):
            continue
        key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        if key not in serialized:
            serialized.add(key)
            result.append(copy.deepcopy(item))
    return result


def enrich_parameter_constraints(parameter: dict[str, Any]) -> dict[str, Any]:
    schema = parameter.get("schema")
    if not isinstance(schema, dict):
        schema = {}
        parameter["schema"] = schema
    description = " ".join(
        str(item or "")
        for item in (parameter.get("description"), schema.get("description"))
    )
    if "maxLength" not in schema:
        match = re.search(
            r"(?:最长|最大长度(?:为)?|length\s*(?:must\s*be\s*)?(?:at\s*most|<=|maximum))\s*"
            r"[:：]?\s*(\d+)",
            description,
            flags=re.I,
        )
        if match:
            schema["maxLength"] = int(match.group(1))
    if "minLength" not in schema:
        match = re.search(
            r"(?:最短|最小长度(?:为)?|length\s*(?:must\s*be\s*)?(?:at\s*least|>=|minimum))\s*"
            r"[:：]?\s*(\d+)",
            description,
            flags=re.I,
        )
        if match:
            schema["minLength"] = int(match.group(1))
    allowed_text = compact_text(description)
    if "pattern" not in schema and all(
        token in allowed_text for token in ("字母", "数字")
    ) and ("下划线" in allowed_text or "underscore" in allowed_text.lower()):
        hyphen = "中划线" in allowed_text or "连字符" in allowed_text or "hyphen" in allowed_text.lower()
        schema["pattern"] = r"^[A-Za-z0-9_-]*$" if hyphen else r"^[A-Za-z0-9_]*$"
    if re.search(r"单个.+参数|只能传(?:入)?一次|must\s+be\s+(?:a\s+)?single", description, flags=re.I):
        parameter["x-iatest-allow-duplicate"] = False
    return parameter


def merge_rule_lists(
    parser_rules: Iterable[dict[str, Any]],
    semantic_rules: Iterable[dict[str, Any]],
    *,
    method: str,
    path: str,
) -> list[dict[str, Any]]:
    result = normalize_business_rules(parser_rules, method=method, path=path)
    for semantic_rule in normalize_business_rules(semantic_rules, method=method, path=path):
        match_index = next(
            (
                index
                for index, existing in enumerate(result)
                if rules_describe_same_fact(existing, semantic_rule)
            ),
            None,
        )
        if match_index is None:
            result.append(semantic_rule)
        else:
            result[match_index] = merge_rule(result[match_index], semantic_rule)
    deduplicated: list[dict[str, Any]] = []
    for rule in result:
        existing = next(
            (item for item in deduplicated if rules_describe_same_fact(item, rule)),
            None,
        )
        if existing is None:
            deduplicated.append(rule)
        else:
            index = deduplicated.index(existing)
            deduplicated[index] = merge_rule(existing, rule)
    return ensure_unique_rule_ids(deduplicated, method=method, path=path)


def normalize_business_rules(
    values: Iterable[dict[str, Any]] | Any,
    *,
    method: str,
    path: str,
) -> list[dict[str, Any]]:
    if not isinstance(values, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    expanded_values = [
        variant
        for raw in values
        if isinstance(raw, dict)
        for variant in split_parser_rule_variants(raw)
    ]
    for index, raw in enumerate(expanded_values, start=1):
        if not isinstance(raw, dict):
            continue
        description = str(
            raw.get("description")
            or raw.get("rule")
            or raw.get("content")
            or raw.get("expected_behavior")
            or raw.get("title")
            or ""
        ).strip()
        title = str(raw.get("title") or description).strip()
        source_evidence = str(
            raw.get("source_evidence")
            or raw.get("source_quote")
            or raw.get("evidence")
            or description
        ).strip()
        if not description and not source_evidence:
            continue
        source_condition = str(raw.get("source_condition") or description or title).strip()
        semantic_text = " ".join([title, description, source_condition])
        category = str(raw.get("category") or classify_rule_category(semantic_text))
        strategy_candidate = str(raw.get("strategy") or raw.get("design_method") or "")
        strategy = (
            strategy_candidate
            if strategy_candidate in SUPPORTED_RULE_STRATEGIES
            else strategy_for_rule(
                semantic_text,
                classify_rule_category(semantic_text),
            )
        )
        expected = normalize_expected(raw, semantic_text)
        trigger = normalize_trigger(raw, semantic_text)
        # Fixture requirements are derived from the source-backed rule text,
        # never trusted verbatim from a model draft. This prevents a model from
        # making an otherwise runnable scenario non-runnable by inventing an
        # opaque or overly broad fixture label.
        fixtures = unique_strings(infer_fixture_requirements(semantic_text))
        if "isolated_rate_limit" in fixtures:
            strategy = "status_code_coverage"
        runnable = not fixtures
        # testable 是框架执行策略，不允许模型把配置说明提升为可执行规则，
        # 也不允许模型隐藏源文本中明确可验证的行为。
        testable = rule_is_testable_text(semantic_text)
        rule_id = str(raw.get("rule_id") or raw.get("id") or "").strip()
        inferred_lifecycle_state = infer_lifecycle_state(semantic_text)
        trigger = normalize_missing_resource_trigger(
            trigger,
            inferred_lifecycle_state,
            path,
        )
        normalized = {
            "rule_id": rule_id
            or stable_rule_id(
                method,
                path,
                source_condition or description or source_evidence,
                index=index,
            ),
            "version": str(raw.get("version") or BUSINESS_RULE_VERSION),
            "title": title or description,
            "description": description or source_evidence,
            "category": category,
            "strategy": strategy,
            "testable": testable,
            "preconditions": string_list(raw.get("preconditions")),
            # 生命周期由源条件确定，不能信任模型遗留的自由文本标签。
            "lifecycle_state": inferred_lifecycle_state,
            "trigger": trigger,
            "expected": expected,
            "runnable": runnable,
            "risk": fixture_risk(fixtures),
            "fixture_requirements": fixtures,
            "source_section": str(raw.get("source_section") or ""),
            "source_evidence": source_evidence or description,
            "source_condition": source_condition,
            "coverage_partitions": [
                copy.deepcopy(item)
                for item in raw.get("coverage_partitions") or []
                if isinstance(item, dict)
            ],
        }
        result.append(normalized)
    deduplicated: list[dict[str, Any]] = []
    for rule in result:
        existing_index = next(
            (
                index
                for index, existing in enumerate(deduplicated)
                if rules_describe_same_fact(existing, rule)
            ),
            None,
        )
        if existing_index is None:
            deduplicated.append(rule)
        else:
            deduplicated[existing_index] = merge_rule(
                deduplicated[existing_index],
                rule,
            )
    deduplicated = drop_aggregate_lifecycle_rules(deduplicated)
    return ensure_unique_rule_ids(deduplicated, method=method, path=path)


def drop_aggregate_lifecycle_rules(
    rules: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    states = {
        str(rule.get("lifecycle_state") or "")
        for rule in rules
        if isinstance(rule, dict)
    }
    if "active" not in states or "expired_in_grace" not in states:
        return rules
    return [
        rule
        for rule in rules
        if business_condition_signature(
            str(
                rule.get("source_condition")
                or rule.get("description")
                or rule.get("title")
                or ""
            )
        )
        != "renewable_mailbox_condition"
    ]


def split_parser_rule_variants(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Split source bullets that encode distinct input/lifecycle partitions."""
    if any(
        raw.get(key)
        for key in ("rule_id", "id", "trigger", "request_overrides", "chain_steps", "preconditions")
    ):
        return [raw]
    description = str(
        raw.get("description")
        or raw.get("source_evidence")
        or raw.get("title")
        or ""
    ).strip()
    random_match = re.match(
        r"^不传\s+(.+?)\s+或传入空白\s+\1\s+时[，,]?\s*(.+)$",
        description,
    )
    if random_match:
        parameter, outcome = random_match.groups()
        return [
            rule_variant(raw, f"不传 {parameter} 时，{outcome}"),
            rule_variant(raw, f"传入空白 {parameter} 时，{outcome}"),
        ]

    status = raw.get("expected_status") or raw.get("status")
    status_match = re.match(
        r"^(.+?)[，,]\s*返回\s*`?([1-5]\d\d)`?\s*$",
        description,
    )
    if isinstance(status, int) and status_match:
        conditions = split_chinese_conditions(status_match.group(1))
        if len(conditions) > 1:
            return [
                rule_variant(raw, f"{condition}，返回 {status_match.group(2)}")
                for condition in conditions
            ]

    response_match = re.match(
        r"^(如果)?(邮箱不存在)[，,]\s*或(.+?)(?:时)?[，,]\s*((?:也)?返回.+)$",
        description,
    )
    if response_match:
        conditional, first, second, outcome = response_match.groups()
        normalized_outcome = outcome.removeprefix("也")
        return [
            rule_variant(raw, f"{conditional or '如果'}{first}，{normalized_outcome}"),
            rule_variant(raw, f"如果{second}，{normalized_outcome}"),
        ]
    return [raw]


def split_chinese_conditions(value: str) -> list[str]:
    return unique_strings(
        re.sub(r"^(?:以下情况|满足以下条件)[:：]?", "", item).strip(" ，,。")
        for item in re.split(r"\s*(?:、|，\s*或|或)\s*", value)
    )


def rule_variant(raw: dict[str, Any], evidence: str) -> dict[str, Any]:
    copied = copy.deepcopy(raw)
    copied["title"] = evidence
    copied["description"] = evidence
    copied["source_condition"] = evidence
    return copied


def normalize_expected(raw: dict[str, Any], semantic_text: str) -> dict[str, Any]:
    candidate = raw.get("expected")
    expected = copy.deepcopy(candidate) if isinstance(candidate, dict) else {}
    status = expected.get("status") or raw.get("expected_status") or raw.get("status")
    if isinstance(status, str) and re.fullmatch(r"[1-5]\d\d", status.strip()):
        status = int(status.strip())
    if not isinstance(status, int) or isinstance(status, bool):
        status = infer_expected_status(semantic_text)
    expected.pop("status", None)
    if isinstance(status, int) and not isinstance(status, bool):
        expected["status"] = status
    description = str(
        expected.get("description")
        or raw.get("expected_outcome")
        or raw.get("expected_result")
        or ""
    ).strip()
    if description:
        expected["description"] = description
    assertions = expected.get("assertions") or raw.get("assertions") or []
    normalized_assertions = [copy.deepcopy(item) for item in assertions if isinstance(item, dict)]
    inferred = infer_assertion_obligations(semantic_text, status=status)
    expected["assertions"] = merge_assertions(normalized_assertions, inferred)
    basis = str(expected.get("basis") or raw.get("expected_basis") or "")
    if isinstance(status, int) and not re.search(
        rf"(?:^|\.)responses\.{status}(?!\d)",
        basis,
    ):
        basis = f"responses.{status}"
    expected["basis"] = basis
    return expected


def infer_expected_status(text: str) -> int | None:
    pattern = re.compile(
        r"(?:返回|响应(?:状态码)?(?:为|是)?|HTTP)\s*[:：]?\s*`?([1-5]\d\d)`?",
        flags=re.I,
    )
    for match in pattern.finditer(text):
        prefix = text[max(0, match.start() - 12) : match.start()]
        if re.search(r"(?:不|不会|不使用|不是)\s*$", prefix, flags=re.I):
            continue
        return int(match.group(1))
    return None


def normalize_trigger(raw: dict[str, Any], semantic_text: str) -> dict[str, Any]:
    candidate = raw.get("trigger")
    source_trigger = copy.deepcopy(candidate) if isinstance(candidate, dict) else {}
    overrides = (
        source_trigger.get("request_overrides")
        or raw.get("request_overrides")
        or raw.get("request")
        or {}
    )
    omit_fields = source_trigger.get("omit_fields") or raw.get("omit_fields") or []
    chain_steps = source_trigger.get("chain_steps") or raw.get("chain_steps") or []
    trigger = {
        "request_overrides": copy.deepcopy(overrides) if isinstance(overrides, dict) else {},
        "omit_fields": string_list(omit_fields),
        "chain_steps": [copy.deepcopy(item) for item in chain_steps if isinstance(item, dict)],
    }
    if not trigger["request_overrides"] and not trigger["omit_fields"]:
        inferred_overrides, inferred_omissions = infer_request_trigger(semantic_text)
        trigger["request_overrides"] = inferred_overrides
        trigger["omit_fields"] = inferred_omissions
    documented_chain = infer_documented_postcondition_chain(semantic_text)
    if documented_chain:
        # 这些链路是框架根据已识别 source_condition 编译出的可执行契约。
        # 不保留模型早期提交的弱化版本，否则升级后的必要断言无法进入正式产物。
        trigger["chain_steps"] = documented_chain
    return trigger


def normalize_missing_resource_trigger(
    trigger: dict[str, Any],
    lifecycle_state: str,
    path: str,
) -> dict[str, Any]:
    if lifecycle_state != "missing":
        return trigger
    result = copy.deepcopy(trigger)
    overrides = result.setdefault("request_overrides", {})
    path_params = overrides.setdefault("path_params", {})
    for name in re.findall(r"\{([^{}]+)\}", str(path or "")):
        if re.search(r"mail|email", name, flags=re.I):
            path_params[name] = "missing-{{uuid}}@example.invalid"
        else:
            path_params[name] = "missing-{{uuid}}"
    if not path_params:
        overrides.pop("path_params", None)
    return result


def infer_documented_postcondition_chain(text: str) -> list[dict[str, Any]]:
    if re.search(r"删除成功后.+不能再查询邮件.+续期", text):
        return [
            {
                "name": "生成真实临时邮箱",
                "method": "GET",
                "path": "/api/generate",
                "extract": {"email": "$.email"},
            },
            {
                "name": "删除刚生成的临时邮箱",
                "method": "DELETE",
                "path": "/api/delete/{email}",
                "path_params": {"email": "{{email}}"},
                "assertions": [
                    {"type": "status_code", "target": "", "operator": "eq", "expected": 200},
                    {"type": "json_path", "target": "$.status", "operator": "eq", "expected": "ok"},
                    {
                        "type": "json_path",
                        "target": "$.message",
                        "operator": "eq",
                        "expected": "Mailbox deleted",
                    },
                ],
            },
            {
                "name": "验证删除后无法再查询邮件",
                "method": "GET",
                "path": "/api/messages/{email}",
                "path_params": {"email": "{{email}}"},
                "assertions": [
                    {"type": "status_code", "target": "", "operator": "eq", "expected": 200},
                    {"type": "json_path", "target": "$", "operator": "is_array", "expected": True},
                    {"type": "json_path", "target": "$", "operator": "length_eq", "expected": 0},
                ],
            },
            {
                "name": "验证删除后无法续期",
                "method": "POST",
                "path": "/api/extend/{email}",
                "path_params": {"email": "{{email}}"},
                "assertions": [
                    {"type": "status_code", "target": "", "operator": "eq", "expected": 404},
                    {
                        "type": "json_path",
                        "target": "$.detail",
                        "operator": "eq",
                        "expected": "Mailbox not found or expired",
                    },
                ],
            },
        ]
    if re.search(r"同一个.+前缀.+已经存在.+刷新.+创建时间", text):
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
    if re.search(r"续期成功后.+(?:创建|刷新)时间更新为当前时间.+新的剩余有效期", text):
        return [
            {
                "name": "生成真实临时邮箱",
                "method": "GET",
                "path": "/api/generate",
                "extract": {"email": "$.email"},
            },
            {
                "name": "续期刚生成的临时邮箱",
                "method": "POST",
                "path": "/api/extend/{email}",
                "path_params": {"email": "{{email}}"},
                "assertions": [
                    {"type": "status_code", "target": "", "operator": "eq", "expected": 200},
                    {"type": "json_path", "target": "$.status", "operator": "eq", "expected": "ok"},
                    {"type": "json_path", "target": "$.expires_in", "operator": "eq", "expected": 600},
                ],
            },
            {
                "name": "验证续期后邮箱恢复有效",
                "method": "GET",
                "path": "/api/check/{email}",
                "path_params": {"email": "{{email}}"},
                "assertions": [
                    {"type": "status_code", "target": "", "operator": "eq", "expected": 200},
                    {"type": "json_path", "target": "$.valid", "operator": "eq", "expected": True},
                    {
                        "type": "json_path",
                        "target": "$.expires_in",
                        "operator": "is_positive_integer",
                        "expected": True,
                    },
                ],
            },
        ]
    return []


def enrich_business_rule_contracts(
    rules: list[dict[str, Any]],
    endpoint: Endpoint,
) -> list[dict[str, Any]]:
    parameter_locations = {
        str(item.get("name") or ""): str(item.get("in") or "query")
        for item in endpoint.parameters
        if isinstance(item, dict) and item.get("name")
    }
    parameter_schemas = {
        str(item.get("name") or ""): item.get("schema") or {}
        for item in endpoint.parameters
        if isinstance(item, dict) and item.get("name")
    }
    result: list[dict[str, Any]] = []
    for rule in rules:
        copied = copy.deepcopy(rule)
        semantic_text = " ".join(
            str(copied.get(key) or "")
            for key in ("title", "description", "source_condition")
        )
        trigger = copied.get("trigger") if isinstance(copied.get("trigger"), dict) else {}
        raw_overrides = trigger.get("request_overrides")
        overrides = canonical_request_overrides(
            raw_overrides if isinstance(raw_overrides, dict) else {},
            parameter_locations,
        )
        inferred_overrides, inferred_omissions = infer_request_trigger(semantic_text)
        boundary_overrides = infer_boundary_request_trigger(
            semantic_text,
            parameter_locations,
            parameter_schemas,
        )
        if boundary_overrides:
            overrides = boundary_overrides
        elif inferred_overrides:
            overrides = inferred_overrides
        trigger["request_overrides"] = overrides
        if inferred_omissions:
            trigger["request_overrides"] = {}
            trigger["omit_fields"] = inferred_omissions
        trigger["chain_steps"] = [
            copy.deepcopy(item)
            for item in trigger.get("chain_steps") or []
            if isinstance(item, dict) and item.get("method") and item.get("path")
        ]
        copied["trigger"] = trigger
        copied["coverage_partitions"] = normalize_coverage_partitions(
            copied.get("coverage_partitions"),
            semantic_text=str(copied.get("source_evidence") or semantic_text),
            parameter_locations=parameter_locations,
        )
        expected = copied.get("expected") if isinstance(copied.get("expected"), dict) else {}
        status = expected.get("status")
        if (
            not isinstance(status, int)
            and ("200" in endpoint.responses or 200 in endpoint.responses)
            and re.search(r"返回|响应", semantic_text)
            and not re.search(r"返回\s*`?[345]\d\d", semantic_text)
        ):
            status = 200
            expected["status"] = 200
            expected["assertions"] = merge_assertions(
                expected.get("assertions") or [],
                [{"type": "status_code", "target": "", "operator": "eq", "expected": 200}],
            )
        if isinstance(status, int) and not re.search(
            rf"(?:^|\.)responses\.{status}(?!\d)",
            str(expected.get("basis") or ""),
        ):
            expected["basis"] = f"responses.{status}"
        expected["assertions"] = filter_assertions_by_response_schema(
            expected.get("assertions") or [],
            endpoint.responses,
            status,
        )
        if isinstance(status, int) and status >= 400:
            expected["assertions"] = merge_assertions(
                expected.get("assertions") or [],
                response_example_assertions(endpoint.responses, status),
            )
        elif isinstance(status, int) and 200 <= status < 300:
            expected["assertions"] = merge_assertions(
                expected.get("assertions") or [],
                success_rule_assertions(endpoint.responses, semantic_text),
            )
        expectation_text = " ".join(
            [
                semantic_text,
                str(expected.get("description") or ""),
            ]
        )
        expected["assertions"] = merge_assertions(
            expected.get("assertions") or [],
            [
                *infer_assertion_obligations(expectation_text, status=status),
                *mentioned_response_field_assertions(
                    endpoint.responses,
                    status,
                    expectation_text,
                ),
                *required_response_field_assertions(endpoint.responses, status),
                *stable_success_example_assertions(endpoint.responses, status),
            ],
        )
        expected["assertions"] = filter_assertions_by_response_schema(
            expected.get("assertions") or [],
            endpoint.responses,
            status,
        )
        expected["assertions"] = filter_assertions_by_expected_status(
            expected.get("assertions") or [],
            status,
        )
        if (
            re.search(r"历史邮件|当前已入库的邮件", semantic_text)
            and not re.search(r"空数组|没有邮件|无邮件", semantic_text)
        ):
            expected["assertions"] = [
                item
                for item in expected.get("assertions") or []
                if not (
                    item.get("type") == "json_path"
                    and item.get("target") in {"", "$"}
                    and item.get("operator") == "length_eq"
                    and item.get("expected") == 0
                )
            ]
        copied["expected"] = expected
        condition = business_condition_signature(semantic_text)
        if not copied.get("preconditions"):
            copied["preconditions"] = inferred_rule_preconditions(condition)
        result.append(copied)
    return result


def normalize_coverage_partitions(
    raw_partitions: Any,
    *,
    semantic_text: str,
    parameter_locations: dict[str, str],
) -> list[dict[str, Any]]:
    """Normalize explicit coverage obligations within one business rule.

    A source sentence may describe one business outcome but enumerate several
    independently constructible triggers. Those triggers must remain separate
    coverage obligations instead of being collapsed into one representative
    equivalence-class value.
    """
    inferred = infer_invalid_character_partitions(semantic_text, parameter_locations)
    candidates = inferred or (
        raw_partitions if isinstance(raw_partitions, (list, tuple)) else []
    )
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(candidates, start=1):
        if not isinstance(raw, dict):
            continue
        partition_id = re.sub(
            r"[^a-z0-9]+",
            "_",
            str(raw.get("partition_id") or raw.get("id") or f"partition_{index}").lower(),
        ).strip("_")
        if not partition_id or partition_id in seen:
            continue
        request_overrides = canonical_request_overrides(
            raw.get("request_overrides")
            if isinstance(raw.get("request_overrides"), dict)
            else {},
            parameter_locations,
        )
        omit_fields = unique_strings(raw.get("omit_fields") or [])
        if not request_overrides and not omit_fields:
            continue
        seen.add(partition_id)
        result.append(
            {
                "partition_id": partition_id,
                "title": str(raw.get("title") or partition_id).strip(),
                "request_overrides": request_overrides,
                "omit_fields": omit_fields,
                "source_evidence": str(
                    raw.get("source_evidence") or semantic_text
                ).strip(),
            }
        )
    return result


def infer_invalid_character_partitions(
    text: str,
    parameter_locations: dict[str, str],
) -> list[dict[str, Any]]:
    if business_condition_signature(text) != "invalid_character":
        return []
    parameter_name = (
        "prefix"
        if "prefix" in parameter_locations
        else next(
            (
                name
                for name, location in parameter_locations.items()
                if location == "query"
            ),
            "",
        )
    )
    if not parameter_name:
        return []
    location = parameter_locations.get(parameter_name) or "query"
    container = "path_params" if location == "path" else location
    if container not in {"headers", "path_params", "query", "body"}:
        container = "query"

    values: list[tuple[str, str, str]] = []
    if re.search(r"`@`|(?:例如|如|包含|字符)[^。；\n]{0,32}@", text, flags=re.I):
        values.append(("invalid_symbol", "包含 @ 非法字符", "invalid@"))
    if re.search(r"空格|\bspace\b", text, flags=re.I):
        values.append(("embedded_space", "包含空格", "invalid prefix"))
    if re.search(r"中文|\bchinese\b|\bunicode\b", text, flags=re.I):
        values.append(("unicode_chinese", "包含中文字符", "中文"))
    return [
        {
            "partition_id": partition_id,
            "title": title,
            "request_overrides": {container: {parameter_name: value}},
            "omit_fields": [],
            "source_evidence": text,
        }
        for partition_id, title, value in values
    ]


def coverage_partition_ref(rule_id: Any, partition_id: Any) -> str:
    rule = str(rule_id or "").strip()
    partition = str(partition_id or "").strip()
    return f"{rule}#{partition}" if rule and partition else rule


def business_rule_coverage_obligations(rule: dict[str, Any]) -> list[str]:
    rule_id = str(rule.get("rule_id") or "").strip()
    if not rule_id:
        return []
    partitions = [
        coverage_partition_ref(rule_id, item.get("partition_id"))
        for item in rule.get("coverage_partitions") or []
        if isinstance(item, dict) and item.get("partition_id")
    ]
    return unique_strings(partitions) or [rule_id]


def inferred_rule_preconditions(condition: str) -> list[str]:
    values = {
        "missing": ["目标资源从未创建"],
        "deleted": ["目标资源已创建后被删除"],
        "cleaned": ["目标资源已超过有效期和保留窗口并被清理"],
        "expired_in_grace": ["目标资源已过期但仍在保留窗口内"],
        "active": ["目标资源已创建且仍在有效期内"],
    }
    return list(values.get(condition, []))


def filter_assertions_by_response_schema(
    assertions: Iterable[Any],
    responses: dict[str, Any],
    status: Any,
) -> list[dict[str, Any]]:
    normalized = merge_assertions(assertions, [])
    if not isinstance(status, int):
        return normalized
    response = responses.get(str(status)) or responses.get(status)
    if not isinstance(response, dict):
        return normalized
    media = response.get("content")
    json_media = media.get("application/json") if isinstance(media, dict) else None
    schema = json_media.get("schema") if isinstance(json_media, dict) else None
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict) or not properties:
        return normalized
    result: list[dict[str, Any]] = []
    for assertion in normalized:
        if assertion.get("type") != "json_path":
            result.append(assertion)
            continue
        target = str(assertion.get("target") or "")
        if (
            target in {"", "$"}
            and schema.get("type") == "object"
            and assertion.get("operator") in {"is_array", "length_eq", "sorted_desc_by", "sorted_asc_by"}
        ):
            continue
        match = re.match(r"^\$\.([A-Za-z_][A-Za-z0-9_]*)", str(assertion.get("target") or ""))
        if not match:
            result.append(assertion)
            continue
        field_schema = properties.get(match.group(1))
        if not isinstance(field_schema, dict):
            continue
        if (
            assertion.get("operator") == "eq"
            and assertion.get("expected") is None
            and not schema_allows_null(field_schema)
        ):
            type_assertion = response_field_type_assertion(match.group(1), field_schema)
            if type_assertion:
                result.append(type_assertion)
            continue
        result.append(assertion)
    return result


def filter_assertions_by_expected_status(
    assertions: Iterable[Any],
    status: Any,
) -> list[dict[str, Any]]:
    normalized = merge_assertions(assertions, [])
    if not isinstance(status, int):
        return normalized
    result: list[dict[str, Any]] = []
    for assertion in normalized:
        if assertion.get("type") != "status_code":
            result.append(assertion)
            continue
        operator = str(assertion.get("operator") or "eq")
        expected = assertion.get("expected")
        if operator in {"eq", "equals", "equal", "=="}:
            try:
                if int(expected) != status:
                    continue
            except (TypeError, ValueError):
                continue
        elif operator == "in" and isinstance(expected, list):
            if status not in {
                int(item)
                for item in expected
                if isinstance(item, (int, str)) and str(item).isdigit()
            }:
                continue
        result.append(assertion)
    if not any(item.get("type") == "status_code" for item in result):
        result.insert(
            0,
            {"type": "status_code", "target": "", "operator": "eq", "expected": status},
        )
    return result


def mentioned_response_field_assertions(
    responses: dict[str, Any],
    status: Any,
    text: str,
) -> list[dict[str, Any]]:
    if not isinstance(status, int):
        return []
    response = responses.get(str(status)) or responses.get(status)
    media = response.get("content") if isinstance(response, dict) else None
    json_media = media.get("application/json") if isinstance(media, dict) else None
    schema = json_media.get("schema") if isinstance(json_media, dict) else None
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict):
        return []
    result: list[dict[str, Any]] = []
    for field_name, field_schema in properties.items():
        if not isinstance(field_schema, dict) or not re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(str(field_name))}(?![A-Za-z0-9_])",
            text,
            flags=re.I,
        ):
            continue
        assertion = response_field_type_assertion(str(field_name), field_schema)
        if assertion:
            result.append(assertion)
    return result


def required_response_field_assertions(
    responses: dict[str, Any],
    status: Any,
) -> list[dict[str, Any]]:
    if not isinstance(status, int) or not 200 <= status < 300:
        return []
    response = responses.get(str(status)) or responses.get(status)
    media = response.get("content") if isinstance(response, dict) else None
    json_media = media.get("application/json") if isinstance(media, dict) else None
    schema = json_media.get("schema") if isinstance(json_media, dict) else None
    properties = schema.get("properties") if isinstance(schema, dict) else None
    required = schema.get("required") if isinstance(schema, dict) else None
    if not isinstance(properties, dict) or not isinstance(required, list):
        return []
    result: list[dict[str, Any]] = []
    for field_name in required:
        field_schema = properties.get(str(field_name))
        if not isinstance(field_schema, dict):
            continue
        default = field_schema.get("default")
        if default is not None:
            result.append(
                {
                    "type": "json_path",
                    "target": f"$.{field_name}",
                    "operator": "eq",
                    "expected": default,
                }
            )
            continue
        assertion = response_field_type_assertion(str(field_name), field_schema)
        if assertion:
            result.append(assertion)
    return result


def stable_success_example_assertions(
    responses: dict[str, Any],
    status: Any,
) -> list[dict[str, Any]]:
    if not isinstance(status, int) or not 200 <= status < 300:
        return []
    response = responses.get(str(status)) or responses.get(status)
    media = response.get("content") if isinstance(response, dict) else None
    json_media = media.get("application/json") if isinstance(media, dict) else None
    example = json_media.get("example") if isinstance(json_media, dict) else None
    if not isinstance(example, dict):
        return []
    result: list[dict[str, Any]] = []
    for field_name in ("status", "message"):
        expected = example.get(field_name)
        if isinstance(expected, (str, int, float, bool)) and not (
            isinstance(expected, float) and not expected.is_integer()
        ):
            result.append(
                {
                    "type": "json_path",
                    "target": f"$.{field_name}",
                    "operator": "eq",
                    "expected": expected,
                }
            )
    return result
def response_field_type_assertion(
    field_name: str,
    schema: dict[str, Any],
) -> dict[str, Any] | None:
    schema_type = schema.get("type")
    operators = {
        "string": "is_string",
        "integer": "is_integer",
        "number": "is_number",
        "boolean": "is_boolean",
        "array": "is_array",
        "object": "is_object",
        "null": "is_null",
    }
    operator = operators.get(str(schema_type or ""))
    if not operator:
        return None
    return {
        "type": "json_path",
        "target": f"$.{field_name}",
        "operator": operator,
        "expected": True,
    }


def schema_allows_null(schema: dict[str, Any]) -> bool:
    if schema.get("nullable") is True:
        return True
    schema_type = schema.get("type")
    if schema_type == "null":
        return True
    if isinstance(schema_type, list) and "null" in schema_type:
        return True
    return any(
        isinstance(item, dict) and item.get("type") == "null"
        for key in ("anyOf", "oneOf")
        for item in (schema.get(key) if isinstance(schema.get(key), list) else [])
    )


def canonical_request_overrides(
    value: dict[str, Any],
    parameter_locations: dict[str, str],
) -> dict[str, Any]:
    sanitized = sanitize_json_mapping(value)
    result: dict[str, Any] = {}
    for container in ("headers", "path_params", "query", "body"):
        item = sanitized.get(container)
        if isinstance(item, dict):
            result[container] = item
    for name, location in parameter_locations.items():
        if name not in sanitized:
            continue
        container = "path_params" if location == "path" else location
        if container not in {"headers", "path_params", "query", "body"}:
            container = "query"
        result.setdefault(container, {})
        if isinstance(result[container], dict) and name not in result[container]:
            result[container][name] = sanitized[name]
    return result


def sanitize_json_mapping(value: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key or "")
        if not key or re.search(r"[{}\"',]", key):
            continue
        if isinstance(raw_value, dict):
            result[key] = sanitize_json_mapping(raw_value)
        elif isinstance(raw_value, list):
            result[key] = [
                copy.deepcopy(item)
                for item in raw_value
                if isinstance(item, (str, int, float, bool)) or item is None
            ]
        elif isinstance(raw_value, (str, int, float, bool)) or raw_value is None:
            result[key] = raw_value
    return result


def infer_boundary_request_trigger(
    text: str,
    parameter_locations: dict[str, str],
    parameter_schemas: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    for name, schema in parameter_schemas.items():
        compact = compact_text(text)
        if name not in compact and not (name == "prefix" and "前缀" in compact):
            continue
        if re.search(r"超过.*长度|长度限制校验|超出.*长度", text):
            maximum = schema.get("maxLength") if isinstance(schema, dict) else None
            if isinstance(maximum, int) and maximum >= 0:
                location = parameter_locations.get(name) or "query"
                container = "path_params" if location == "path" else location
                return {container: {name: "a" * (maximum + 1)}}
    return {}


def response_example_assertions(
    responses: dict[str, Any],
    status: int,
) -> list[dict[str, Any]]:
    response = responses.get(str(status)) or responses.get(status)
    if not isinstance(response, dict):
        return []
    media = response.get("content")
    if not isinstance(media, dict):
        return []
    json_media = media.get("application/json")
    if not isinstance(json_media, dict):
        return []
    example = json_media.get("example")
    if not isinstance(example, dict):
        return []
    assertions: list[dict[str, Any]] = []
    for name, expected in example.items():
        if isinstance(expected, (str, int, float, bool)) or expected is None:
            assertions.append(
                {
                    "type": "json_path",
                    "target": f"$.{name}",
                    "operator": "eq",
                    "expected": expected,
                }
            )
    return assertions


def success_rule_assertions(
    responses: dict[str, Any],
    semantic_text: str,
) -> list[dict[str, Any]]:
    response = responses.get("200") or responses.get(200)
    if not isinstance(response, dict):
        return []
    media = response.get("content")
    json_media = media.get("application/json") if isinstance(media, dict) else None
    example = json_media.get("example") if isinstance(json_media, dict) else None
    if not isinstance(example, dict):
        return []
    assertions: list[dict[str, Any]] = []
    if re.search(r"续期|删除成功", semantic_text):
        status = example.get("status")
        if isinstance(status, str):
            assertions.append(
                {"type": "json_path", "target": "$.status", "operator": "eq", "expected": status}
            )
    if re.search(r"10\s*分钟|续期|有效期从当前时间|恢复为新的", semantic_text):
        expires_in = example.get("expires_in")
        if isinstance(expires_in, int) and not isinstance(expires_in, bool):
            assertions.append(
                {
                    "type": "json_path",
                    "target": "$.expires_in",
                    "operator": "eq",
                    "expected": expires_in,
                }
            )
    return assertions


def infer_request_trigger(text: str) -> tuple[dict[str, Any], list[str]]:
    lowered = compact_text(text).lower()
    if "prefix" in lowered or "前缀" in lowered:
        if re.search(r"不传|未传|省略|omit", text, flags=re.I):
            return {}, ["query.prefix"]
        if re.search(r"空白|空字符串|blank|empty", text, flags=re.I):
            return {"query": {"prefix": ""}}, []
        if re.search(r"重复传入|多个\s*`?prefix|multiple\s+prefix|duplicate\s+prefix", text, flags=re.I):
            return {"query": {"prefix": ["iatest_a", "iatest_b"]}}, []
        if re.search(r"非法字符|包含\s*@|illegal\s+character", text, flags=re.I):
            return {"query": {"prefix": "invalid@"}}, []
        if re.search(r"中文|unicode", text, flags=re.I):
            return {"query": {"prefix": "中文"}}, []
        if re.search(r"空格|space", text, flags=re.I):
            return {"query": {"prefix": "invalid prefix"}}, []
    return {}, []


def infer_assertion_obligations(text: str, *, status: int | None) -> list[dict[str, Any]]:
    assertions: list[dict[str, Any]] = []
    if isinstance(status, int):
        assertions.append({"type": "status_code", "target": "", "operator": "eq", "expected": status})
    value_patterns = {
        "valid": r"\bvalid\b\s*(?::|：|=|为|应为)\s*(true|false)",
        "expires_in": r"\bexpires_in\b\s*(?::|：|=|为|应为)\s*(\d+)",
        "status": r"\bstatus\b\s*(?::|：|=|为|应为)\s*[`'\"]?([A-Za-z_-]+)",
    }
    for field_name, pattern in value_patterns.items():
        match = re.search(pattern, text, flags=re.I)
        if not match:
            continue
        raw = match.group(1)
        expected: Any
        if raw.lower() in {"true", "false"}:
            expected = raw.lower() == "true"
        elif raw.isdigit():
            expected = int(raw)
        else:
            expected = raw
        assertions.append(
            {
                "type": "json_path",
                "target": f"$.{field_name}",
                "operator": "eq",
                "expected": expected,
            }
        )
    if re.search(r"空数组|\[\]|empty\s+array", text, flags=re.I):
        assertions.append({"type": "json_path", "target": "$", "operator": "is_array", "expected": True})
        assertions.append({"type": "json_path", "target": "$", "operator": "length_eq", "expected": 0})
    order = re.search(r"按\s*`?([A-Za-z_][A-Za-z0-9_]*)`?\s*(倒序|降序|升序|正序)", text)
    if order:
        assertions.append(
            {
                "type": "json_path",
                "target": "$",
                "operator": "sorted_desc_by" if order.group(2) in {"倒序", "降序"} else "sorted_asc_by",
                "expected": order.group(1),
            }
        )
    if re.search(r"随机生成\s*8\s*位|random\s+8", text, flags=re.I):
        assertions.append(
            {
                "type": "json_path",
                "target": "$.email",
                "operator": "matches",
                "expected": r"^[A-Za-z0-9]{8}@mail\.tempbox\.cn$",
            }
        )
    domain = re.search(r"邮箱域名固定为\s*`?([A-Za-z0-9.-]+)`?", text, flags=re.I)
    if domain:
        assertions.append(
            {
                "type": "json_path",
                "target": "$.email",
                "operator": "matches",
                "expected": rf"^[^@]+@{re.escape(domain.group(1))}$",
            }
        )
    duration = re.search(r"默认有效期\s*[:：为]?\s*`?(\d+)\s*分钟", text)
    if duration:
        assertions.append(
            {
                "type": "json_path",
                "target": "$.expires_in",
                "operator": "eq",
                "expected": int(duration.group(1)) * 60,
            }
        )
    generated = re.search(
        r"生成邮箱地址为\s*`?\{?prefix\}?@([A-Za-z0-9.-]+)`?",
        text,
        flags=re.I,
    )
    if generated:
        assertions.append(
            {
                "type": "json_path",
                "target": "$.email",
                "operator": "matches",
                "expected": rf"^[A-Za-z0-9_-]+@{re.escape(generated.group(1))}$",
            }
        )
    if re.search(r"valid\s*:\s*true", text, flags=re.I) and re.search(r"剩余秒数|expires_in\s*>\s*0", text):
        assertions.append(
            {
                "type": "json_path",
                "target": "$.expires_in",
                "operator": "is_positive_integer",
                "expected": True,
            }
        )
    if re.search(r"返回.+邮件|历史邮件|当前已入库的邮件", text):
        assertions.append(
            {
                "type": "json_path",
                "target": "$",
                "operator": "is_array",
                "expected": True,
            }
        )
    return assertions


def infer_fixture_requirements(text: str) -> list[str]:
    result: list[str] = []
    if re.search(
        r"刚到期|(?:过期|到期).*保留窗口|超过.*保留窗口|等待.*过期|"
        r"固定时间|控制时间|time.?control|grace",
        text,
        flags=re.I,
    ):
        result.append("time_control")
    if re.search(r"收到的邮件|历史邮件|邮件按|最多保留.+邮件|HTML\s*邮件|收件时间", text, flags=re.I):
        result.append("mail_injection")
    if re.search(r"\b429\b|同一\s*IP.+\d+\s*秒|限流|rate.?limit", text, flags=re.I):
        result.append("isolated_rate_limit")
    if re.search(r"前缀包含受限内容|restricted\s+(?:word|content|prefix)", text, flags=re.I):
        result.append("restricted_prefix")
    if re.search(r"服务保留邮箱|保留邮箱|reserved\s+(?:mailbox|prefix)", text, flags=re.I):
        result.append("reserved_mailbox_prefix")
    return result


def classify_rule_category(text: str) -> str:
    if re.search(r"最长|最短|最大|最小|长度|超过\s*\d+\s*个?字符|范围|边界", text, flags=re.I):
        return "boundary"
    if re.search(r"非法|只能包含|重复传入|多个.+参数|受限|保留邮箱|等价类", text, flags=re.I):
        return "validation"
    if re.search(r"过期|续期|删除后|已删除|不存在|清理|生命周期|保留窗口", text, flags=re.I):
        return "lifecycle"
    if re.search(r"倒序|升序|字段|空数组|返回|响应", text, flags=re.I):
        return "response"
    return "behavior"


def strategy_for_rule(text: str, category: str) -> str:
    if category == "boundary":
        return "boundary_value"
    if category == "validation":
        return "equivalence_class"
    if re.search(r"不传|空白|可选|optional", text, flags=re.I):
        return "optional_field"
    if category == "lifecycle" or re.search(r"再次|随后|之后|同一个.+已经存在", text, flags=re.I):
        return "dependency_chain"
    return "happy_path"


def infer_lifecycle_state(text: str) -> str:
    patterns = (
        ("deleted", r"已删除|已被.{0,20}删除|删除后|再次删除"),
        ("expired_in_grace", r"刚到期|到期.*保留窗口内|过期.*保留窗口内"),
        ("active_or_grace", r"未超过.*保留窗口|仍存在.*保留窗口"),
        ("cleaned", r"(?<!未)超过.*保留窗口|被清理"),
        ("missing", r"不存在|从未创建"),
        ("active", r"未过期|仍在有效期|当前有效"),
    )
    for state, pattern in patterns:
        if re.search(pattern, text, flags=re.I):
            return state
    return ""


def rule_is_testable_text(text: str) -> bool:
    stripped = compact_text(text)
    if not stripped:
        return False
    if re.search(
        r"(?:由\s*`?[A-Z][A-Z0-9_]+`?\s*控制|TEMP_[A-Z0-9_]+.+(?:条|分钟|小时))",
        stripped,
    ) and not re.search(r"返回|响应|如果|当(?!前).{1,60}时", stripped):
        return False
    if re.search(r"^用于(?:创建|查询|判断|把|提前结束)", stripped) and not re.search(
        r"返回|如果|当|时|后|固定|只能|最长|最大|过期|删除|有效",
        stripped,
    ):
        return False
    return bool(
        re.search(
            r"返回|生成|固定|刷新|不能|不会|可以|只能|最长|最短|最大|最小|"
            r"长度|超过|过期|删除|续期|清理|倒序|升序|有效|重复|非法|空白|"
            r"不存在|从未创建|受限|保留",
            stripped,
            flags=re.I,
        )
    )


def fixture_risk(fixtures: list[str]) -> str:
    if "isolated_rate_limit" in fixtures:
        return "shared_rate_limit"
    if fixtures:
        return "fixture_required"
    return ""


def merge_rule(parser_rule: dict[str, Any], semantic_rule: dict[str, Any]) -> dict[str, Any]:
    result = merge_mapping(parser_rule, semantic_rule)
    for key in ("title", "description", "source_evidence"):
        result[key] = str(parser_rule.get(key) or semantic_rule.get(key) or "")
    result["source_condition"] = str(
        parser_rule.get("source_condition")
        or parser_rule.get("description")
        or semantic_rule.get("source_condition")
        or semantic_rule.get("description")
        or ""
    )
    for key in ("category", "strategy", "lifecycle_state", "risk"):
        result[key] = semantic_rule.get(key) or parser_rule.get(key) or ""
    result["preconditions"] = string_list(parser_rule.get("preconditions"))
    result["trigger"] = merge_mapping(parser_rule.get("trigger", {}), semantic_rule.get("trigger", {}))
    result["expected"] = merge_mapping(parser_rule.get("expected", {}), semantic_rule.get("expected", {}))
    semantic_status_assertions = [
        item
        for item in semantic_rule.get("expected", {}).get("assertions", [])
        if isinstance(item, dict) and item.get("type") == "status_code"
    ]
    result["expected"]["assertions"] = merge_assertions(
        parser_rule.get("expected", {}).get("assertions", []),
        semantic_status_assertions,
    )
    result["fixture_requirements"] = unique_strings(
        [
            *(parser_rule.get("fixture_requirements") or []),
            *(semantic_rule.get("fixture_requirements") or []),
        ]
    )
    result["testable"] = bool(parser_rule.get("testable"))
    # Whether a source-backed scenario can execute is deterministic framework
    # policy. A model may enrich semantics, but may not disable an executable
    # parser rule by returning runnable=false.
    result["runnable"] = bool(
        parser_rule.get("runnable", True)
        and not result["fixture_requirements"]
    )
    if semantic_rule.get("rule_id") and not str(semantic_rule["rule_id"]).startswith("BR-"):
        result["rule_id"] = str(semantic_rule["rule_id"])
    return result


def rules_describe_same_fact(first: dict[str, Any], second: dict[str, Any]) -> bool:
    first_text = compact_text(
        first.get("source_condition") or first.get("description") or first.get("source_evidence")
    ).lower()
    second_text = compact_text(
        second.get("source_condition") or second.get("description") or second.get("source_evidence")
    ).lower()
    if not first_text or not second_text:
        return False
    first_signature = business_condition_signature(first_text)
    second_signature = business_condition_signature(second_text)
    if first_signature and first_signature == second_signature:
        first_status = (first.get("expected") or {}).get("status")
        second_status = (second.get("expected") or {}).get("status")
        if not isinstance(first_status, int) or not isinstance(second_status, int) or first_status == second_status:
            return True
    return first_text == second_text or (
        min(len(first_text), len(second_text)) >= 8
        and (first_text in second_text or second_text in first_text)
    )


def business_condition_signature(text: str) -> str:
    patterns = (
        (
            "renewal_updates_expiry",
            r"续期成功后.+(?:创建|刷新)时间.+(?:剩余有效期|expires_in)",
        ),
        (
            "generate_refreshes_existing_mailbox",
            r"同一个.*?前缀.*?邮箱(?:已经|已)存在.*刷新.*(?:创建时间|有效期)",
        ),
        (
            "renewable_mailbox_condition",
            r"仍存在.+未超过.+保留窗口|未超过.+保留窗口.+可以续期",
        ),
        ("messages_sorted", r"received_at.+倒序|倒序.+received_at"),
        (
            "expired_history_messages",
            r"(?:到期|过期).+保留窗口内.+历史邮件|历史邮件.+(?:到期|过期).+保留窗口内",
        ),
        ("omitted", r"不传|未传|省略"),
        ("blank", r"空白|空字符串"),
        ("duplicate_parameter", r"重复传入|多个\s*prefix"),
        ("invalid_character", r"非法字符|包含\s*@|包含空格|包含中文"),
        ("over_max_length", r"超过长度限制|长度限制校验|超出.*长度|超过\s*\d+\s*个?字符"),
        ("restricted_prefix", r"受限内容"),
        ("reserved_mailbox", r"服务保留邮箱|保留邮箱"),
        ("rate_limit", r"429|同一\s*ip.+(?:10|十).+次|限流"),
        ("missing", r"不存在|从未创建"),
        ("deleted", r"已被(?:用户)?删除|已被用户.{0,40}删除|已删除"),
        ("cleaned", r"超过.+保留窗口.+清理|已超过.+保留窗口"),
        ("expired_in_grace", r"刚到期|已过期但.+保留窗口内|到期后.+保留窗口内"),
        ("active", r"未过期|仍在有效期"),
    )
    lowered = compact_text(text).lower()
    for signature, pattern in patterns:
        if re.search(pattern, lowered, flags=re.I):
            return signature
    return ""


def ensure_unique_rule_ids(
    rules: list[dict[str, Any]],
    *,
    method: str,
    path: str,
) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for index, rule in enumerate(rules, start=1):
        copied = copy.deepcopy(rule)
        rule_id = str(copied.get("rule_id") or "")
        if not rule_id or rule_id in seen:
            rule_id = stable_rule_id(
                method,
                path,
                copied.get("source_condition")
                or copied.get("description")
                or copied.get("source_evidence")
                or copied.get("title")
                or "",
                index=index,
            )
            suffix = 2
            base = rule_id
            while rule_id in seen:
                rule_id = f"{base}-{suffix}"
                suffix += 1
        copied["rule_id"] = rule_id
        seen.add(rule_id)
        result.append(copied)
    return result


def stable_rule_id(method: str, path: str, evidence: Any, *, index: int) -> str:
    identity = f"{str(method).upper()} {path}\n{compact_text(evidence)}\n{index}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12].upper()
    return f"BR-{digest}"


def drop_redundant_parameter_schema_rules(
    rules: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop aggregate model summaries already represented by parameter Schema."""
    result: list[dict[str, Any]] = []
    for rule in rules:
        text = compact_text(
            rule.get("source_condition")
            or rule.get("description")
            or rule.get("title")
        )
        expected = rule.get("expected") if isinstance(rule.get("expected"), dict) else {}
        status = expected.get("status")
        constraint_count = sum(
            bool(re.search(pattern, text, flags=re.I))
            for pattern in (
                r"最长|最大长度",
                r"只能包含|允许字符",
                r"单个.*参数|只能传(?:入)?一次",
            )
        )
        is_concrete_error = bool(
            re.search(
                r"超过\s*(?:长度限制|\d+\s*个?字符)|非法字符|重复传入|多个.+参数|"
                r"包含受限内容|对应服务保留邮箱",
                text,
                flags=re.I,
            )
        )
        is_purpose_only = bool(
            re.search(r"^用于(?:创建|查询|判断|把|提前结束)", text)
            and not re.search(r"固定|返回|只能|必须|最长|过期|删除后", text)
        )
        is_transport_serialization = bool(
            re.search(
                r"(?:url|uri)\s*(?:encode|编码)|(?:encode|编码).{0,12}(?:路径|path)",
                text,
                flags=re.I,
            )
        )
        is_positive_schema_summary = bool(
            constraint_count
            and not is_concrete_error
            and re.search(r"必须|只能|允许|最长|最大长度|单个.*参数", text)
        )
        if is_purpose_only or is_transport_serialization or is_positive_schema_summary:
            continue
        if (
            isinstance(status, int)
            and status >= 400
            and constraint_count >= 2
            and not is_concrete_error
        ):
            continue
        result.append(rule)
    return result


def business_rule_inventory(endpoints: Iterable[Endpoint], *, testable_only: bool = False) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for endpoint in endpoints:
        for rule in endpoint.business_rules:
            if not isinstance(rule, dict):
                continue
            if testable_only and not rule.get("testable", True):
                continue
            result.append(
                {
                    **copy.deepcopy(rule),
                    "endpoint_method": endpoint.method.upper(),
                    "endpoint_path": endpoint.path,
                    "endpoint_summary": endpoint.summary,
                }
            )
    return result


def build_coverage_manifest(endpoints: list[Endpoint], cases: list[TestCase]) -> dict[str, Any]:
    inventory = business_rule_inventory(endpoints, testable_only=True)
    cases_by_rule: dict[str, list[TestCase]] = {}
    cases_by_partition: dict[str, list[TestCase]] = {}
    for case in cases:
        for rule_id in case.rule_ids:
            cases_by_rule.setdefault(str(rule_id), []).append(case)
        for partition_id in case.coverage_partition_ids:
            cases_by_partition.setdefault(str(partition_id), []).append(case)
    rows: list[dict[str, Any]] = []
    partition_rows: list[dict[str, Any]] = []
    for rule in inventory:
        rule_id = str(rule["rule_id"])
        partitions = [
            item
            for item in rule.get("coverage_partitions") or []
            if isinstance(item, dict) and item.get("partition_id")
        ]
        matched = cases_by_rule.get(rule_id, [])
        if partitions:
            rule_partition_rows: list[dict[str, Any]] = []
            matched_partition_cases: list[TestCase] = []
            for partition in partitions:
                partition_id = str(partition.get("partition_id") or "")
                partition_ref = coverage_partition_ref(rule_id, partition_id)
                partition_cases = [
                    case
                    for case in cases_by_partition.get(partition_ref, [])
                    if case_satisfies_partition_request(
                        case,
                        partition,
                        endpoint_method=str(rule["endpoint_method"]),
                        endpoint_path=str(rule["endpoint_path"]),
                    )
                ]
                partition_disposition = coverage_disposition(partition_cases)
                matched_partition_cases.extend(partition_cases)
                row = {
                    "coverage_partition_id": partition_ref,
                    "partition_id": partition_id,
                    "rule_id": rule_id,
                    "endpoint_method": rule["endpoint_method"],
                    "endpoint_path": rule["endpoint_path"],
                    "title": partition.get("title") or partition_id,
                    "source_evidence": partition.get("source_evidence") or rule.get("source_evidence"),
                    "request_overrides": copy.deepcopy(partition.get("request_overrides") or {}),
                    "omit_fields": unique_strings(partition.get("omit_fields") or []),
                    "disposition": partition_disposition,
                    "case_ids": [case.id for case in partition_cases],
                    "scenario_ids": [case.scenario_id for case in partition_cases],
                }
                rule_partition_rows.append(row)
                partition_rows.append(row)
            if any(item["disposition"] == "missing" for item in rule_partition_rows):
                disposition = "missing"
            elif all(item["disposition"] == "executable" for item in rule_partition_rows):
                disposition = "executable"
            else:
                disposition = "fixture_required"
            matched = unique_cases(matched_partition_cases)
        else:
            disposition = coverage_disposition(matched)
        rows.append(
            {
                "rule_id": rule_id,
                "endpoint_method": rule["endpoint_method"],
                "endpoint_path": rule["endpoint_path"],
                "title": rule.get("title") or rule.get("description"),
                "strategy": rule.get("strategy"),
                "lifecycle_state": rule.get("lifecycle_state"),
                "disposition": disposition,
                "coverage_partition_count": len(partitions),
                "case_ids": [case.id for case in matched],
                "scenario_ids": [case.scenario_id for case in matched],
                "fixture_requirements": unique_strings(
                    [
                        *(rule.get("fixture_requirements") or []),
                        *(item for case in matched for item in case.fixture_requirements),
                    ]
                ),
            }
        )
    covered = sum(item["disposition"] != "missing" for item in rows)
    executable = sum(item["disposition"] == "executable" for item in rows)
    fixture_required = sum(item["disposition"] == "fixture_required" for item in rows)
    covered_partitions = sum(
        item["disposition"] != "missing" for item in partition_rows
    )
    return {
        "version": "2",
        "contract_version": TRUSTED_CONTRACT_VERSION,
        "rules": rows,
        "coverage_partitions": partition_rows,
        "stats": {
            "rule_count": len(rows),
            "covered_rule_count": covered,
            "missing_rule_count": len(rows) - covered,
            "executable_rule_count": executable,
            "fixture_required_rule_count": fixture_required,
            "coverage_percent": round(covered * 100 / len(rows), 2) if rows else 100.0,
            "coverage_partition_count": len(partition_rows),
            "covered_partition_count": covered_partitions,
            "missing_partition_count": len(partition_rows) - covered_partitions,
            "partition_coverage_percent": (
                round(covered_partitions * 100 / len(partition_rows), 2)
                if partition_rows
                else 100.0
            ),
            "case_count": len(cases),
            "runnable_case_count": sum(case.runnable for case in cases),
            "non_runnable_case_count": sum(not case.runnable for case in cases),
        },
    }


def coverage_disposition(cases: list[TestCase]) -> str:
    if not cases:
        return "missing"
    if any(case.runnable for case in cases):
        return "executable"
    return "fixture_required"


def unique_cases(cases: Iterable[TestCase]) -> list[TestCase]:
    result: list[TestCase] = []
    seen: set[str] = set()
    for case in cases:
        identity = case.id or str(id(case))
        if identity not in seen:
            seen.add(identity)
            result.append(case)
    return result


def case_satisfies_partition_request(
    case: TestCase,
    partition: dict[str, Any],
    *,
    endpoint_method: str,
    endpoint_path: str,
) -> bool:
    requests: list[dict[str, Any]] = []
    if case.method.upper() == endpoint_method.upper() and case.path == endpoint_path:
        requests.append(
            {
                "headers": case.headers,
                "path_params": case.path_params,
                "query": case.query,
                "body": case.body,
            }
        )
    for step in case.steps:
        if step.method.upper() == endpoint_method.upper() and step.path == endpoint_path:
            requests.append(
                {
                    "headers": step.headers,
                    "path_params": step.path_params,
                    "query": step.query,
                    "body": step.body,
                }
            )
    expected = partition.get("request_overrides") or {}
    omissions = unique_strings(partition.get("omit_fields") or [])
    return any(
        nested_mapping_contains(request, expected)
        and all(not dotted_value_exists(request, field) for field in omissions)
        for request in requests
    )


def nested_mapping_contains(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        return all(
            key in actual and nested_mapping_contains(actual[key], value)
            for key, value in expected.items()
        )
    return actual == expected


def dotted_value_exists(value: Any, field_path: str) -> bool:
    current = value
    for part in str(field_path or "").split("."):
        if not part or not isinstance(current, dict) or part not in current:
            return False
        current = current[part]
    return True


def coverage_manifest_issues(endpoints: list[Endpoint], cases: list[TestCase]) -> list[dict[str, Any]]:
    manifest = build_coverage_manifest(endpoints, cases)
    issues: list[dict[str, Any]] = []
    known_rule_ids = {
        str(item["rule_id"])
        for item in business_rule_inventory(endpoints, testable_only=False)
    }
    for row in manifest["rules"]:
        if row["disposition"] != "missing":
            continue
        if row.get("coverage_partition_count"):
            continue
        issues.append(
            {
                "severity": "critical",
                "dimension": "completeness",
                "location": str(row["rule_id"]),
                "evidence": (
                    f"可信接口契约声明 {row['endpoint_method']} {row['endpoint_path']} "
                    f"业务规则：{row['title']}"
                ),
                "reason": "最终用例集没有任何用例声明覆盖该业务规则。",
                "suggested_fix": "为该 rule_id 生成可执行用例；缺少夹具时生成明确的 fixture_required 契约用例。",
            }
        )
    for row in manifest["coverage_partitions"]:
        if row["disposition"] != "missing":
            continue
        issues.append(
            {
                "severity": "critical",
                "dimension": "completeness",
                "location": str(row["coverage_partition_id"]),
                "evidence": (
                    f"可信接口契约声明 {row['endpoint_method']} {row['endpoint_path']} "
                    f"业务规则 {row['rule_id']} 的独立覆盖分区：{row['title']}"
                ),
                "reason": "最终用例集没有用例声明并执行该独立异常触发条件。",
                "suggested_fix": (
                    "为该 coverage_partition_id 单独生成一条用例；"
                    "不要用同一规则下的其他代表值代替。"
                ),
            }
        )
    known_partition_ids = {
        obligation
        for rule in business_rule_inventory(endpoints, testable_only=False)
        for obligation in business_rule_coverage_obligations(rule)
        if "#" in obligation
    }
    for case in cases:
        for rule_id in case.rule_ids:
            if rule_id in known_rule_ids:
                continue
            issues.append(
                {
                    "severity": "high",
                    "dimension": "reliability",
                    "location": case.scenario_id or case.id,
                    "evidence": f"用例引用了可信接口契约中不存在的 rule_id：{rule_id}",
                    "reason": "用例覆盖血缘无法追溯到当前接口识别产物。",
                    "suggested_fix": "删除失效引用，或基于当前接口契约重新生成该用例。",
                }
            )
        for partition_id in case.coverage_partition_ids:
            if partition_id in known_partition_ids:
                continue
            issues.append(
                {
                    "severity": "high",
                    "dimension": "reliability",
                    "location": case.scenario_id or case.id,
                    "evidence": f"用例引用了可信接口契约中不存在的覆盖分区：{partition_id}",
                    "reason": "用例的细粒度覆盖血缘无法追溯到当前接口识别产物。",
                    "suggested_fix": "删除失效引用，或基于当前接口契约重新生成该用例。",
                }
            )
    cases_by_rule: dict[str, list[TestCase]] = {}
    for case in cases:
        for rule_id in case.rule_ids:
            cases_by_rule.setdefault(str(rule_id), []).append(case)
    for rule in business_rule_inventory(endpoints, testable_only=True):
        rule_id = str(rule.get("rule_id") or "")
        matched = cases_by_rule.get(rule_id, [])
        if not matched:
            continue
        partitions = [
            item
            for item in rule.get("coverage_partitions") or []
            if isinstance(item, dict) and item.get("partition_id")
        ]
        assertion_targets: list[tuple[str, list[TestCase]]] = []
        if partitions:
            for partition in partitions:
                partition_ref = coverage_partition_ref(
                    rule_id,
                    partition.get("partition_id"),
                )
                partition_cases = [
                    case
                    for case in matched
                    if partition_ref in case.coverage_partition_ids
                    and case_satisfies_partition_request(
                        case,
                        partition,
                        endpoint_method=str(rule["endpoint_method"]),
                        endpoint_path=str(rule["endpoint_path"]),
                    )
                ]
                if partition_cases:
                    assertion_targets.append((partition_ref, partition_cases))
        else:
            assertion_targets.append((rule_id, matched))
        obligations = (
            rule.get("expected", {}).get("assertions", [])
            if isinstance(rule.get("expected"), dict)
            else []
        )
        for obligation in obligations:
            if not isinstance(obligation, dict):
                continue
            for location, target_cases in assertion_targets:
                missing_assertion = [
                    case
                    for case in target_cases
                    if not case_satisfies_assertion(case, obligation)
                ]
                if not missing_assertion:
                    continue
                issues.append(
                    {
                        "severity": "critical",
                        "dimension": "reliability",
                        "location": location,
                        "evidence": (
                            f"业务覆盖义务 {location} 要求断言 "
                            f"{assertion_signature(obligation)}，未实现用例为 "
                            f"{', '.join(case.id for case in missing_assertion)}"
                        ),
                        "reason": "用例虽然声明覆盖该规则分区，但没有实现契约要求的响应校验。",
                        "suggested_fix": "在对应主请求或链路步骤中补齐该断言，不能仅修改标题或预期结果文本。",
                    }
                )
    return issues


def case_satisfies_assertion(case: TestCase, obligation: dict[str, Any]) -> bool:
    assertions = [
        *case.assertions,
        *(assertion for step in case.steps for assertion in step.assertions),
    ]
    return any(
        assertion_satisfies_obligation(assertion, obligation)
        for assertion in assertions
    )


def assertion_satisfies_obligation(actual: Any, required: dict[str, Any]) -> bool:
    if normalize_assertion_signature(actual) == normalize_assertion_signature(required):
        return True
    actual_type = str(getattr(actual, "type", "") or "")
    actual_target = str(getattr(actual, "target", "") or "")
    actual_operator = str(getattr(actual, "operator", "eq") or "eq")
    actual_expected = getattr(actual, "expected", None)
    required_type = str(required.get("type") or "")
    required_target = str(required.get("target") or required.get("path") or "")
    required_operator = str(required.get("operator") or "eq")
    required_expected = required.get("expected")
    if (actual_type, actual_target) != (required_type, required_target):
        return False
    if (
        required_operator in {"matches", "regex"}
        and actual_operator in {"matches", "regex"}
    ):
        return regex_pattern_implies(
            str(actual_expected or ""),
            str(required_expected or ""),
        )
    if (
        required_operator in {"matches", "regex"}
        and actual_operator in {"eq", "equals", "equal", "=="}
        and isinstance(actual_expected, str)
    ):
        try:
            return re.fullmatch(str(required_expected or ""), actual_expected) is not None
        except re.error:
            return False
    if required_expected is not True:
        return False
    if required_operator == "is_string":
        return (
            actual_operator in {"matches", "regex", "starts_with", "ends_with"}
            or actual_operator in {"eq", "equals", "equal", "=="} and isinstance(actual_expected, str)
        )
    if required_operator == "is_integer":
        return (
            actual_operator == "is_positive_integer"
            or actual_operator in {"eq", "equals", "equal", "=="}
            and isinstance(actual_expected, int)
            and not isinstance(actual_expected, bool)
        )
    if required_operator == "is_boolean":
        return (
            actual_operator in {"eq", "equals", "equal", "=="}
            and isinstance(actual_expected, bool)
        )
    if required_operator == "is_array":
        return actual_operator == "length_eq" or (
            actual_operator in {"eq", "equals", "equal", "=="}
            and isinstance(actual_expected, list)
        )
    return False


def regex_pattern_implies(actual: str, required: str) -> bool:
    """Recognize a conservative subset of provably stronger regex assertions."""
    if actual == required:
        return True
    broad = re.fullmatch(r"\^\[\^@\]\+(@.+)\$", str(required or ""))
    if broad is None:
        return False
    narrower = re.fullmatch(
        r"\^\[([^\]]+)\](?:\+|\{([1-9]\d*)(?:,\d*)?\})(@.+)\$",
        str(actual or ""),
    )
    if narrower is None or narrower.group(3) != broad.group(1):
        return False
    character_class = narrower.group(1)
    return "@" not in character_class or character_class.startswith("^@")


def normalize_assertion_signature(value: Any) -> tuple[str, str, str, str]:
    if isinstance(value, dict):
        assertion_type = value.get("type")
        target = value.get("target") or value.get("path")
        operator = value.get("operator") or "eq"
        expected = value.get("expected")
    else:
        assertion_type = getattr(value, "type", "")
        target = getattr(value, "target", "")
        operator = getattr(value, "operator", "eq")
        expected = getattr(value, "expected", None)
    aliases = {
        "status": "status_code",
        "jsonpath": "json_path",
    }
    normalized_type = aliases.get(str(assertion_type or "").lower(), str(assertion_type or "").lower())
    return (
        normalized_type,
        str(target or ""),
        str(operator or "eq").lower(),
        json.dumps(expected, ensure_ascii=False, sort_keys=True, default=str),
    )


def assertion_signature(value: Any) -> str:
    assertion_type, target, operator, expected = normalize_assertion_signature(value)
    return f"{assertion_type} {target or '-'} {operator} {expected}"


def contract_catalog_issues(
    candidate: list[Endpoint],
    deterministic: list[Endpoint],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    candidate_by_key = {(item.method.upper(), item.path): item for item in candidate}
    for baseline in deterministic:
        key = (baseline.method.upper(), baseline.path)
        endpoint = candidate_by_key.get(key)
        if endpoint is None:
            continue
        baseline_parameters = {
            (str(item.get("in") or ""), str(item.get("name") or ""))
            for item in baseline.parameters
            if isinstance(item, dict)
        }
        candidate_parameters = {
            (str(item.get("in") or ""), str(item.get("name") or ""))
            for item in endpoint.parameters
            if isinstance(item, dict)
        }
        for location, name in sorted(baseline_parameters - candidate_parameters):
            issues.append(
                {
                    "severity": "critical",
                    "dimension": "completeness",
                    "location": f"{key[0]} {key[1]} parameters.{location}.{name}",
                    "evidence": "本地解析器从接口路径或参数表识别出该参数。",
                    "reason": "接口识别产物遗漏确定性请求参数。",
                    "suggested_fix": "补齐参数位置、名称、必填性和约束。",
                }
            )
        baseline_statuses = {str(status) for status in baseline.responses}
        candidate_statuses = {str(status) for status in endpoint.responses}
        for status in sorted(baseline_statuses - candidate_statuses):
            issues.append(
                {
                    "severity": "critical",
                    "dimension": "completeness",
                    "location": f"{key[0]} {key[1]} responses.{status}",
                    "evidence": "原始文档包含该状态码的 JSON 响应示例。",
                    "reason": "接口识别产物遗漏确定性响应契约。",
                    "suggested_fix": "补齐响应状态码、Schema 和示例。",
                }
            )
        candidate_rules = endpoint.business_rules
        baseline_rules = drop_redundant_parameter_schema_rules(
            normalize_business_rules(
                baseline.business_rules,
                method=baseline.method,
                path=baseline.path,
            )
        )
        for baseline_rule in baseline_rules:
            if any(rules_describe_same_fact(baseline_rule, item) for item in candidate_rules):
                continue
            issues.append(
                {
                    "severity": "critical",
                    "dimension": "completeness",
                    "location": f"{key[0]} {key[1]} business_rules",
                    "evidence": str(
                        baseline_rule.get("source_evidence")
                        or baseline_rule.get("description")
                        or baseline_rule.get("title")
                    ),
                    "reason": "接口识别产物遗漏本地解析器从同一原文提取出的业务规则。",
                    "suggested_fix": "重新读取对应原文片段并补齐结构化业务规则。",
                }
            )
        for rule in candidate_rules:
            if not isinstance(rule, dict):
                continue
            rule_id = str(rule.get("rule_id") or f"{key[0]} {key[1]}")
            if not str(rule.get("source_evidence") or "").strip():
                issues.append(
                    {
                        "severity": "high",
                        "dimension": "reliability",
                        "location": rule_id,
                        "evidence": "业务规则没有 source_evidence。",
                        "reason": "无法追溯该规则是否来自接口文档。",
                        "suggested_fix": "补充原文证据，或删除无法由原文支持的规则。",
                    }
                )
            expected = rule.get("expected") if isinstance(rule.get("expected"), dict) else {}
            assertions = expected.get("assertions") if isinstance(expected.get("assertions"), list) else []
            trigger = rule.get("trigger") if isinstance(rule.get("trigger"), dict) else {}
            chain_has_assertions = any(
                isinstance(step, dict) and bool(step.get("assertions"))
                for step in trigger.get("chain_steps") or []
            )
            if rule.get("testable", True) and not assertions and not chain_has_assertions:
                issues.append(
                    {
                        "severity": "high",
                        "dimension": "completeness",
                        "location": rule_id,
                        "evidence": str(rule.get("source_evidence") or rule.get("description") or ""),
                        "reason": "可测试业务规则没有任何标准化断言义务。",
                        "suggested_fix": "从响应契约补充状态码或字段断言；无法直接验证时声明夹具并标记不可直接运行。",
                    }
                )
            for contradiction in assertion_contradictions(assertions):
                issues.append(
                    {
                        "severity": "critical",
                        "dimension": "reliability",
                        "location": rule_id,
                        "evidence": contradiction,
                        "reason": "同一业务规则包含无法同时成立的断言。",
                        "suggested_fix": "依据该规则的具体生命周期条件删除冲突断言，不能交由用例生成阶段猜测。",
                    }
                )
        duplicate_facts: list[tuple[str, str]] = []
        for index, first in enumerate(candidate_rules):
            if not isinstance(first, dict):
                continue
            for second in candidate_rules[index + 1 :]:
                if isinstance(second, dict) and rules_describe_same_fact(first, second):
                    duplicate_facts.append(
                        (str(first.get("rule_id") or ""), str(second.get("rule_id") or ""))
                    )
        for first_id, second_id in duplicate_facts:
            issues.append(
                {
                    "severity": "high",
                    "dimension": "non_redundancy",
                    "location": f"{key[0]} {key[1]} business_rules",
                    "evidence": f"{first_id} 与 {second_id} 描述同一业务事实。",
                    "reason": "同一接口包含语义重复的业务规则。",
                    "suggested_fix": "合并规则并保留完整触发条件、断言和单一稳定 rule_id。",
                }
            )
    all_ids = [
        str(rule.get("rule_id") or "")
        for endpoint in candidate
        for rule in endpoint.business_rules
        if isinstance(rule, dict)
    ]
    duplicates = sorted({rule_id for rule_id in all_ids if rule_id and all_ids.count(rule_id) > 1})
    for rule_id in duplicates:
        issues.append(
            {
                "severity": "high",
                "dimension": "non_redundancy",
                "location": rule_id,
                "evidence": "同一接口契约包存在重复业务规则 ID。",
                "reason": "业务规则血缘不唯一。",
                "suggested_fix": "合并重复规则并保留一个稳定 rule_id。",
            }
        )
    return issues


def assertion_contradictions(assertions: Iterable[Any]) -> list[str]:
    by_target: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in assertions:
        if not isinstance(item, dict):
            continue
        key = (str(item.get("type") or ""), str(item.get("target") or item.get("path") or ""))
        by_target.setdefault(key, []).append(item)
    result: list[str] = []
    for (assertion_type, target), values in by_target.items():
        equals = {
            json.dumps(item.get("expected"), ensure_ascii=False, sort_keys=True, default=str)
            for item in values
            if str(item.get("operator") or "eq") in {"eq", "equals", "equal", "=="}
        }
        operators = {str(item.get("operator") or "eq") for item in values}
        if len(equals) > 1:
            result.append(f"{assertion_type} {target or '-'} 同时要求多个不同的相等值：{sorted(equals)}")
        if "0" in equals and "is_positive_integer" in operators:
            result.append(f"{assertion_type} {target or '-'} 同时要求等于 0 且为正整数")
    return result


def contract_hash(endpoints: list[Endpoint]) -> str:
    payload = json.dumps(
        to_jsonable(endpoints),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def merge_assertions(first: Iterable[Any], second: Iterable[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in [*first, *second]:
        if not isinstance(item, dict):
            continue
        operator = str(item.get("operator") or "eq")
        operator = {
            "equals": "eq",
            "equal": "eq",
            "==": "eq",
            "not_equals": "ne",
            "!=": "ne",
        }.get(operator, operator)
        normalized = {
            "type": str(item.get("type") or ""),
            "target": str(item.get("target") or item.get("path") or ""),
            "operator": operator,
            "expected": item.get("expected"),
        }
        if normalized["type"] == "status_code":
            values = (
                normalized["expected"]
                if isinstance(normalized["expected"], list)
                else [normalized["expected"]]
            )
            converted = [
                int(value.strip())
                if isinstance(value, str) and re.fullmatch(r"[1-5]\d\d", value.strip())
                else value
                for value in values
            ]
            normalized["expected"] = (
                converted
                if isinstance(normalized["expected"], list)
                else converted[0]
            )
        key = json.dumps(normalized, ensure_ascii=False, sort_keys=True, default=str)
        if normalized["type"] and key not in seen:
            seen.add(key)
            result.append(normalized)
    return result


def compact_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("`", "")).strip()


def string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def unique_strings(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result
