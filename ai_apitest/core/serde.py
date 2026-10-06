from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, TypeVar

from ai_apitest.core.case_schema import normalize_test_case
from ai_apitest.core.files import atomic_write_text
from ai_apitest.core.models import Assertion, Endpoint, TestCase, TestStep

T = TypeVar("T")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    return value


def endpoint_from_dict(data: dict[str, Any]) -> Endpoint:
    return Endpoint(
        method=data["method"],
        path=data["path"],
        summary=data.get("summary", ""),
        description=data.get("description", ""),
        parameters=data.get("parameters", []),
        request_body=data.get("request_body"),
        responses=data.get("responses", {}),
        tags=data.get("tags", []),
        operation_id=data.get("operation_id", ""),
        deprecated=bool(data.get("deprecated", False)),
        security=data.get("security", []),
        business_rules=data.get("business_rules", []),
    )


def assertion_from_dict(data: dict[str, Any]) -> Assertion:
    assertion_type = data["type"]
    return Assertion(
        type=assertion_type,
        target=normalize_assertion_target(assertion_type, data.get("target", "")),
        operator=normalize_assertion_operator(data.get("operator", "eq")),
        expected=data.get("expected"),
    )


def normalize_assertion_target(assertion_type: str, target: Any) -> str:
    text = str(target or "").strip()
    if assertion_type != "json_path":
        return text
    if text in {"", "response", "response.body", "body", "json"}:
        return "$"
    for prefix in ("response.body.", "body.", "response.json.", "json."):
        if text.startswith(prefix):
            return "$." + text[len(prefix) :]
    if text.startswith("$."):
        return text
    return text


def normalize_assertion_operator(operator: Any) -> str:
    value = str(operator or "eq").strip()
    aliases = {
        "equals": "eq",
        "equal": "eq",
        "==": "eq",
        "not_equals": "ne",
        "!=": "ne",
        "greater_than": "gt",
        ">": "gt",
        "less_than": "lt",
        "<": "lt",
        "greater_or_equal": "gte",
        ">=": "gte",
        "less_or_equal": "lte",
        "<=": "lte",
        "notEmpty": "not_empty",
        "not_empty": "not_empty",
        "isArray": "is_array",
    }
    return aliases.get(value, value)


def step_from_dict(data: dict[str, Any]) -> TestStep:
    return TestStep(
        name=data.get("name", ""),
        method=data["method"],
        path=data["path"],
        headers=data.get("headers", {}),
        path_params=data.get("path_params", {}),
        query=data.get("query", {}),
        parameter_serialization=data.get("parameter_serialization", {}),
        body=data.get("body"),
        assertions=[assertion_from_dict(item) for item in data.get("assertions", [])],
        extract=data.get("extract", {}),
        poll=data.get("poll", {}),
    )


def case_from_dict(data: dict[str, Any]) -> TestCase:
    case = TestCase(
        id=data["id"],
        title=data["title"],
        method=data["method"],
        path=data["path"],
        priority=data.get("priority", "P1"),
        tags=data.get("tags", []),
        design_method=data.get("design_method", "happy_path"),
        headers=data.get("headers", {}),
        path_params=data.get("path_params", {}),
        query=data.get("query", {}),
        parameter_serialization=data.get("parameter_serialization", {}),
        body=data.get("body"),
        assertions=[assertion_from_dict(item) for item in data.get("assertions", [])],
        source=data.get("source", ""),
        runnable=data.get("runnable", True),
        risk=data.get("risk", "none"),
        negative=data.get("negative", False),
        preconditions=string_list(data.get("preconditions")) or default_case_preconditions(data),
        test_steps=string_list(data.get("test_steps")) or default_case_steps(data),
        expected_result=data.get("expected_result") or default_expected_result(data),
        setup_steps=[step_from_dict(item) for item in data.get("setup_steps", [])],
        steps=[step_from_dict(item) for item in data.get("steps", [])],
        cleanup_steps=[step_from_dict(item) for item in data.get("cleanup_steps", [])],
        scenario_id=data.get("scenario_id", ""),
        rule_ids=string_list(data.get("rule_ids")),
        coverage_partition_ids=string_list(data.get("coverage_partition_ids")),
        fixture_requirements=string_list(data.get("fixture_requirements")),
        variables=data.get("variables", {}),
        dataset=data.get("dataset"),
        dataset_id=str(data.get("dataset_id", "")),
        origin_case_id=str(data.get("origin_case_id", "")),
        identity=str(data.get("identity", "")),
        resources=string_list(data.get("resources")),
        parallel_safe=data.get("parallel_safe", False),
        retry_safe=data.get("retry_safe", False),
        poll=data.get("poll", {}),
    )
    return normalize_test_case(case, preserve_valid_id=True)


def string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def default_case_preconditions(data: dict[str, Any]) -> list[str]:
    items = [
        "测试环境和接口基础地址已正确配置。",
        f"接口 {data.get('method', '')} {data.get('path', '')} 在当前环境可访问。",
    ]
    if data.get("path_params"):
        items.append("路径参数使用用例中的测试值；如果该值依赖真实业务数据，需要先确认数据存在或由链路步骤创建。")
    return items


def default_case_steps(data: dict[str, Any]) -> list[str]:
    if data.get("steps"):
        return [
            f"执行链路步骤：{item.get('name') or item.get('method', '') + ' ' + item.get('path', '')}。"
            for item in data.get("steps", [])
            if isinstance(item, dict)
        ]
    steps = [f"发送 {data.get('method', '')} {data.get('path', '')} 请求。"]
    if data.get("path_params"):
        steps.append(f"替换路径参数：{data.get('path_params')}。")
    if data.get("query"):
        steps.append(f"设置 Query 参数：{data.get('query')}。")
    if data.get("body") is not None:
        steps.append(f"设置请求体：{data.get('body')}。")
    steps.append("检查实际响应是否满足断言列表；如断言与业务逻辑不符，应修正用例预期。")
    return steps


def default_expected_result(data: dict[str, Any]) -> str:
    assertions = data.get("assertions", [])
    if assertions:
        return "实际响应应满足断言列表；如断言与业务规则不一致，应优先修正用例预期。"
    return "以接口文档和业务规则为准。"
