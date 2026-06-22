from __future__ import annotations

from typing import Any

from ai_apitest.core.models import Assertion


def evaluate(assertion: Assertion, response: dict[str, Any]) -> str | None:
    actual = resolve_actual(assertion, response)
    ok = compare(actual, assertion.operator, assertion.expected)
    if ok:
        return None
    return (
        f"{assertion.type} {assertion.target or '-'} "
        f"{assertion.operator} {assertion.expected!r} 失败，实际值 {actual!r}"
    )


def resolve_actual(assertion: Assertion, response: dict[str, Any]) -> Any:
    if assertion.type == "status_code":
        return response.get("status_code")
    if assertion.type == "json_path":
        return json_path(response.get("json"), assertion.target)
    if assertion.type == "header":
        return response.get("headers", {}).get(assertion.target.lower())
    if assertion.type == "body_contains":
        return response.get("text", "")
    return None


def compare(actual: Any, operator: str, expected: Any) -> bool:
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
    if operator == "contains":
        return expected in actual if actual is not None else False
    if operator == "not_empty":
        return actual not in (None, "", [], {})
    if operator == "exists":
        return actual is not None
    if operator == "is_array":
        return isinstance(actual, list)
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
    if not path or path == "$":
        return data
    if not path.startswith("$."):
        return None
    current = data
    for part in path[2:].split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        else:
            return None
    return current
