from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from jsonpath_ng.ext import parse as parse_json_path

from ai_apitest.core.models import Assertion
from ai_apitest.core.schema import schema_errors


class _MissingValue:
    def __repr__(self) -> str:
        return "<MISSING>"


MISSING = _MissingValue()


def evaluate(assertion: Assertion, response: dict[str, Any]) -> str | None:
    if assertion.type == "custom":
        from ai_apitest.execution.control import ACTIVE_RUNTIME
        runtime = ACTIVE_RUNTIME.get()
        handler = runtime.plugins.assertions.get(assertion.target) if runtime and runtime.plugins else None
        if handler is None:
            return f"自定义断言未注册：{assertion.target}"
        try:
            result = handler(assertion, response, runtime.context)
            if result is not None and not isinstance(result, str):
                return "自定义断言必须返回 None（通过）或错误字符串"
            return result
        except Exception as exc:
            return f"自定义断言执行失败：{exc}"
    if assertion.type == "json_schema":
        if response.get("json_valid") is False or "json" not in response:
            return "json_schema: 响应不是有效 JSON"
        errors = schema_errors(response.get("json"), assertion.expected)
        return "json_schema: " + "; ".join(errors) if errors else None
    actual = resolve_actual(assertion, response)
    expected = assertion.expected
    operator = assertion.operator
    if assertion.type == "json_path" and operator.endswith("_path") and isinstance(expected, str):
        expected = json_path(response.get("json"), expected)
        operator = operator.removesuffix("_path")
    ok = compare(actual, operator, expected)
    if ok:
        return None
    display_actual = None if actual is MISSING else actual
    return (
        f"{assertion.type} {assertion.target or '-'} "
        f"{assertion.operator} {assertion.expected!r} 失败，实际值 {display_actual!r}"
    )


def resolve_actual(assertion: Assertion, response: dict[str, Any]) -> Any:
    if assertion.type == "status_code":
        return response.get("status_code")
    if assertion.type == "json_path":
        return json_path_value(response.get("json"), assertion.target, missing=MISSING)
    if assertion.type == "header":
        return next((value for key, value in response.get("headers", {}).items() if key.lower() == assertion.target.lower()), MISSING)
    if assertion.type == "body_contains":
        return response.get("text", "")
    if assertion.type == "response_time":
        return response.get("elapsed_ms", MISSING)
    return MISSING


def compare(actual: Any, operator: str, expected: Any) -> bool:
    if actual is MISSING:
        return operator == "not_exists"
    if operator == "is_2xx":
        return isinstance(actual, int) and not isinstance(actual, bool) and 200 <= actual < 300
    if operator in {"eq", "equals", "equal", "=="}:
        return actual == expected
    if operator in {"ne", "not_equals", "!="}:
        return actual != expected
    if operator == "in":
        return actual in expected if isinstance(expected, list) else False
    if operator == "gt":
        return safe_order_compare(actual, expected, "gt")
    if operator == "lt":
        return safe_order_compare(actual, expected, "lt")
    if operator == "gte":
        return safe_order_compare(actual, expected, "gte")
    if operator == "lte":
        return safe_order_compare(actual, expected, "lte")
    if operator == "approx_eq":
        if isinstance(expected, dict):
            target = expected.get("value")
            tolerance = expected.get("tolerance", 5)
        else:
            target = expected
            tolerance = 5
        values = (actual, target, tolerance)
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values):
            return False
        return abs(actual - target) <= abs(tolerance)
    if operator == "contains":
        if actual is None:
            return False
        try:
            return expected in actual
        except (TypeError, AttributeError):
            return False
    if operator == "not_empty":
        return actual not in (None, "", [], {})
    if operator == "exists":
        return True
    if operator == "not_exists":
        return False
    if operator == "is_array":
        return isinstance(actual, list)
    if operator == "is_string":
        return isinstance(actual, str)
    if operator == "is_integer":
        return isinstance(actual, int) and not isinstance(actual, bool)
    if operator == "is_positive_integer":
        return isinstance(actual, int) and not isinstance(actual, bool) and actual > 0
    if operator == "is_number":
        return isinstance(actual, (int, float)) and not isinstance(actual, bool)
    if operator == "is_boolean":
        return isinstance(actual, bool)
    if operator == "is_object":
        return isinstance(actual, dict)
    if operator == "is_null":
        return actual is None
    if operator == "length_eq":
        try:
            return len(actual) == expected
        except (TypeError, AttributeError):
            return False
    if operator in {"sorted_desc_by", "sorted_asc_by"}:
        if not isinstance(actual, list) or not isinstance(expected, str):
            return False
        values = [item.get(expected) for item in actual if isinstance(item, dict)]
        if len(values) != len(actual) or any(value is None for value in values):
            return not actual
        try:
            ordered = sorted(values, reverse=operator == "sorted_desc_by")
        except TypeError:
            return False
        return values == ordered
    if operator == "starts_with":
        return isinstance(actual, str) and isinstance(expected, str) and actual.startswith(expected)
    if operator == "ends_with":
        return isinstance(actual, str) and isinstance(expected, str) and actual.endswith(expected)
    if operator in {"matches", "regex"}:
        if not isinstance(actual, str) or not isinstance(expected, str):
            return False
        try:
            return re.search(expected, actual) is not None
        except re.error:
            return False
    return False


def safe_order_compare(actual: Any, expected: Any, operator: str) -> bool:
    if actual is None or expected is None:
        return False
    try:
        if operator == "gt":
            return actual > expected
        if operator == "lt":
            return actual < expected
        if operator == "gte":
            return actual >= expected
        if operator == "lte":
            return actual <= expected
    except TypeError:
        return False
    return False


def json_path(data: Any, path: str) -> Any:
    return json_path_value(data, path, missing=None)


def json_path_value(data: Any, path: str, *, missing: Any) -> Any:
    if not path or path == "$":
        return data
    normalized = normalize_json_path(path)
    if not normalized.startswith("$"):
        return missing
    try:
        expression = compiled_json_path(normalized)
        matches = [match.value for match in expression.find(data)]
    except Exception:
        return missing
    if not matches:
        return missing
    return matches[0] if len(matches) == 1 else matches


def normalize_json_path(path: str) -> str:
    value = str(path or "").strip()
    return re.sub(r"\.(\d+)(?=\.|$)", lambda match: f"[{match.group(1)}]", value)


@lru_cache(maxsize=512)
def compiled_json_path(path: str):
    return parse_json_path(path)
