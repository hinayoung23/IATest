from __future__ import annotations

import re
from typing import Any

from ai_apitest.core.models import TestCase

PRIORITIES = ("P0", "P1", "P2", "P3")

PRIORITY_ALIASES = {
    "p0": "P0",
    "blocker": "P0",
    "critical": "P0",
    "fatal": "P0",
    "致命": "P0",
    "阻塞": "P0",
    "p1": "P1",
    "high": "P1",
    "major": "P1",
    "高": "P1",
    "重要": "P1",
    "p2": "P2",
    "medium": "P2",
    "normal": "P2",
    "中": "P2",
    "一般": "P2",
    "p3": "P3",
    "low": "P3",
    "minor": "P3",
    "低": "P3",
}

DESIGN_METHOD_ALIASES = {
    "happy": "happy_path",
    "happyPath": "happy_path",
    "normal": "happy_path",
    "success": "happy_path",
    "optional": "optional_field",
    "optionalField": "optional_field",
    "typeError": "type_error",
    "boundary": "boundary_value",
    "boundaryValue": "boundary_value",
    "pathParameterError": "path_parameter_error",
    "path_param_error": "path_parameter_error",
    "dependency": "dependency_chain",
    "dependencyChain": "dependency_chain",
    "auth": "auth_error",
    "authError": "auth_error",
    "equivalence": "equivalence_class",
    "equivalenceClass": "equivalence_class",
    "statusCodeCoverage": "status_code_coverage",
    "status_coverage": "status_code_coverage",
}

ALLOWED_DESIGN_METHODS = {
    "happy_path",
    "optional_field",
    "required_field",
    "type_error",
    "boundary_value",
    "path_parameter_error",
    "dependency_chain",
    "auth_error",
    "equivalence_class",
    "status_code_coverage",
}

RISK_ALIASES = {
    "": "none",
    "no": "none",
    "false": "none",
    "none": "none",
    "low": "none",
    "negative": "negative_behavior",
    "negative_case": "negative_behavior",
    "destructive": "destructive",
    "delete": "destructive",
    "data": "data_dependent",
    "data_dependent": "data_dependent",
    "data_dependent_without_fixture": "data_dependent_without_fixture",
    "behavior": "behavior_dependent",
    "behavior_dependent": "behavior_dependent",
    "env": "env_dependent",
    "environment": "env_dependent",
    "env_dependent": "env_dependent",
}


def normalize_case_id(value: Any, module: str, index: int, *, preserve_valid: bool = False) -> str:
    candidate = str(value or "").strip().upper()
    module = normalize_module(module)
    if preserve_valid and re.fullmatch(rf"TC-{re.escape(module)}-\d{{3,}}", candidate):
        return candidate
    return f"TC-{module}-{index:04d}"


def normalize_module(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "_", str(value or "API")).strip("_").upper()
    return text or "API"


def normalize_title(value: Any, fallback: str) -> str:
    title = re.sub(r"\s+", " ", str(value or fallback or "未命名用例")).strip()
    return title[:120]


def normalize_priority(value: Any, *, negative: bool = False, design_method: str = "") -> str:
    text = str(value or "").strip()
    key = text.lower().replace(" ", "_")
    if key in PRIORITY_ALIASES:
        return PRIORITY_ALIASES[key]
    if text.upper() in PRIORITIES:
        return text.upper()
    if design_method in {"happy_path", "dependency_chain"} and not negative:
        return "P0"
    if design_method in {"optional_field", "equivalence_class", "status_code_coverage", "auth_error"}:
        return "P1"
    if negative or design_method in {"type_error", "boundary_value", "path_parameter_error", "required_field"}:
        return "P2"
    return "P1"


def normalize_design_method(value: Any) -> str:
    raw = str(value or "happy_path").strip()
    mapped = DESIGN_METHOD_ALIASES.get(raw) or DESIGN_METHOD_ALIASES.get(raw.lower())
    if mapped:
        return mapped
    normalized = to_snake(raw)
    return normalized if normalized in ALLOWED_DESIGN_METHODS else "happy_path"


def normalize_tags(tags: Any, *, design_method: str, source_tag: str = "") -> list[str]:
    values = tags if isinstance(tags, list) else []
    normalized = []
    for item in values:
        tag = to_snake(str(item))
        if tag and tag not in normalized:
            normalized.append(tag)
    for required in [design_method, source_tag]:
        tag = to_snake(required)
        if tag and tag not in normalized:
            normalized.append(tag)
    return normalized


def normalize_risk(value: Any, *, runnable: bool, negative: bool) -> str:
    key = str(value or "").strip().lower().replace(" ", "_")
    if key in RISK_ALIASES:
        return RISK_ALIASES[key]
    if not runnable:
        return "data_dependent" if not negative else "negative_behavior"
    return "negative_behavior" if negative else "none"


def to_snake(value: str) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    text = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "_", text).strip("_").lower()
    return text


def normalize_test_case(
    case: TestCase,
    *,
    module: str = "",
    index: int = 1,
    source_tag: str = "",
    preserve_valid_id: bool = False,
) -> TestCase:
    design_method = normalize_design_method(case.design_method)
    negative = bool(case.negative)
    runnable = bool(case.runnable)
    module_name = module or case_module_from_path(case.path)
    case.id = normalize_case_id(case.id, module_name, index, preserve_valid=preserve_valid_id)
    case.title = normalize_title(case.title, f"{case.method} {case.path} - {design_method}")
    case.priority = normalize_priority(case.priority, negative=negative, design_method=design_method)
    case.design_method = design_method
    case.tags = normalize_tags(case.tags, design_method=design_method, source_tag=source_tag)
    case.risk = normalize_risk(case.risk, runnable=runnable, negative=negative)
    return case


def normalize_cases(cases: list[TestCase], *, source_tag: str = "") -> list[TestCase]:
    counters: dict[str, int] = {}
    normalized = []
    for case in cases:
        module = case_module_from_path(case.path)
        counters[module] = counters.get(module, 0) + 1
        normalized.append(normalize_test_case(case, module=module, index=counters[module], source_tag=source_tag, preserve_valid_id=False))
    return normalized


def case_module_from_path(path: str) -> str:
    parts = [part for part in str(path or "").split("/") if part and not part.startswith("{")]
    if not parts:
        return "API"
    return normalize_module(parts[0])
