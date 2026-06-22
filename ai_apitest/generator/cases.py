from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ai_apitest.core.case_schema import normalize_cases, normalize_priority, normalize_test_case
from ai_apitest.core.models import Assertion, Endpoint, TestCase, TestStep
from ai_apitest.core.paths import project_cases_file, safe_project_name
from ai_apitest.core.serde import case_from_dict, to_jsonable


def generate_cases(endpoints: list[Endpoint]) -> list[TestCase]:
    cases: list[TestCase] = []
    counters: dict[str, int] = {}
    for endpoint in endpoints:
        cases.extend(generate_endpoint_cases(endpoint, counters))
    cases.extend(generate_dependency_chain_cases(endpoints, counters))
    return attach_cleanup_steps(attach_setup_steps(cases, endpoints), endpoints)


def generate_endpoint_cases(endpoint: Endpoint, counters: dict[str, int]) -> list[TestCase]:
    generated = [
        build_case(
            endpoint,
            counters,
            title=f"{endpoint.summary or endpoint.path} - 正常场景",
            design_method="happy_path",
            tags=["happy_path"],
            query=default_query(endpoint, include_optional=True),
            path_params=default_path_params(endpoint),
            body=default_body(endpoint),
            assertions=success_assertions(endpoint),
            runnable=default_runnable(endpoint),
            risk=default_risk(endpoint),
            expected_result="接口使用合法参数时应成功返回",
        )
    ]

    if has_optional_query(endpoint):
        generated.append(
            build_case(
                endpoint,
                counters,
                title=f"{endpoint.summary or endpoint.path} - 不传可选参数",
                design_method="optional_field",
                tags=["optional_field"],
                query=default_query(endpoint, include_optional=False),
                path_params=default_path_params(endpoint),
                body=default_body(endpoint),
                assertions=success_assertions(endpoint),
                runnable=default_runnable(endpoint),
                risk=default_risk(endpoint),
                expected_result="可选参数缺省时接口仍应成功返回",
            )
        )

    for parameter in endpoint.parameters:
        location = parameter.get("in")
        name = parameter.get("name", "")
        if location == "query":
            generated.extend(query_parameter_cases(endpoint, parameter, counters))
        elif location == "path":
            generated.extend(path_parameter_cases(endpoint, parameter, counters))

    if endpoint.request_body:
        generated.extend(body_negative_cases(endpoint, counters))

    generated.extend(equivalence_class_cases(endpoint, counters))
    generated.extend(status_code_coverage_cases(endpoint, counters))
    if has_auth_requirement(endpoint):
        generated.extend(auth_error_cases(endpoint, counters))

    return generated


def build_case(
    endpoint: Endpoint,
    counters: dict[str, int],
    *,
    title: str,
    design_method: str,
    tags: list[str],
    query: dict[str, Any] | None = None,
    path_params: dict[str, Any] | None = None,
    body: Any = None,
    assertions: list[Assertion] | None = None,
    runnable: bool = True,
    risk: str = "none",
    negative: bool = False,
    preconditions: list[str] | None = None,
    test_steps: list[str] | None = None,
    expected_result: str = "",
) -> TestCase:
    module = module_name(endpoint)
    counters[module] = counters.get(module, 0) + 1
    case = TestCase(
        id=f"TC-{module.upper()}-{counters[module]:03d}",
        title=title,
        method=endpoint.method,
        path=endpoint.path,
        priority=normalize_priority("", negative=negative, design_method=design_method),
        tags=[*endpoint.tags, *tags],
        design_method=design_method,
        headers=default_headers(endpoint),
        path_params=path_params or {},
        query=query or {},
        body=body,
        assertions=assertions or success_assertions(endpoint),
        source=f"{endpoint.method} {endpoint.path}",
        runnable=runnable,
        risk=risk,
        negative=negative,
        preconditions=preconditions or default_preconditions(endpoint, path_params or {}),
        test_steps=test_steps or default_test_steps(endpoint, query or {}, path_params or {}, body, assertions or success_assertions(endpoint)),
        expected_result=expected_result,
    )
    return normalize_test_case(case, module=module, index=counters[module], source_tag="rule_generated")


def default_preconditions(endpoint: Endpoint, path_params: dict[str, Any]) -> list[str]:
    items = [
        "测试环境和接口基础地址已正确配置。",
        f"接口 {endpoint.method} {endpoint.path} 在当前环境可访问。",
    ]
    if path_params:
        items.append("路径参数使用用例中给出的测试值；如该值依赖真实业务数据，需要先确认数据存在或由链路步骤创建。")
    if endpoint.request_body:
        items.append("请求体字段含义以接口文档和当前接口信息页配置为准。")
    return items


def default_test_steps(
    endpoint: Endpoint,
    query: dict[str, Any],
    path_params: dict[str, Any],
    body: Any,
    assertions: list[Assertion],
) -> list[str]:
    steps = [
        f"按用例数据准备请求：{endpoint.method} {endpoint.path}。",
    ]
    if path_params:
        steps.append(f"替换路径参数：{path_params}。")
    if query:
        steps.append(f"设置 Query 参数：{query}。")
    if body is not None:
        steps.append(f"设置请求体：{body}。")
    steps.append("发送请求并记录实际响应状态码、响应头和响应体。")
    if assertions:
        steps.append("按断言列表校验实际响应是否符合预期。")
    return steps


def query_parameter_cases(endpoint: Endpoint, parameter: dict[str, Any], counters: dict[str, int]) -> list[TestCase]:
    name = parameter.get("name", "")
    cases = []
    if parameter.get("required"):
        query = default_query(endpoint, include_optional=True)
        query.pop(name, None)
        cases.append(
            build_case(
                endpoint,
                counters,
                title=f"{endpoint.summary or endpoint.path} - 缺失必填 Query 参数 {name}",
                design_method="required_field",
                tags=["required_field", "negative"],
                query=query,
                path_params=default_path_params(endpoint),
                body=default_body(endpoint),
                assertions=client_error_assertions(),
                runnable=False,
                risk="negative_behavior",
                negative=True,
                expected_result="缺失必填 Query 参数时应返回客户端错误",
            )
        )
    cases.append(
        build_case(
            endpoint,
            counters,
            title=f"{endpoint.summary or endpoint.path} - Query 参数 {name} 类型错误",
            design_method="type_error",
            tags=["type_error", "negative"],
            query={**default_query(endpoint, include_optional=True), name: wrong_type_value(parameter)},
            path_params=default_path_params(endpoint),
            body=default_body(endpoint),
            assertions=client_error_assertions(),
            runnable=False,
            risk="negative_behavior",
            negative=True,
            expected_result="Query 参数类型错误时应返回客户端错误或业务错误",
        )
    )
    for label, value in boundary_values(parameter):
        cases.append(
            build_case(
                endpoint,
                counters,
                title=f"{endpoint.summary or endpoint.path} - Query 参数 {name} 边界值 {label}",
                design_method="boundary_value",
                tags=["boundary_value"],
                query={**default_query(endpoint, include_optional=True), name: value},
                path_params=default_path_params(endpoint),
                body=default_body(endpoint),
                assertions=flexible_assertions(),
                runnable=False,
                risk="behavior_dependent",
                expected_result="边界值应得到明确成功或明确错误响应",
            )
        )
    return cases


def path_parameter_cases(endpoint: Endpoint, parameter: dict[str, Any], counters: dict[str, int]) -> list[TestCase]:
    name = parameter.get("name", "")
    cases = []
    for label, value in [("空值", ""), ("非法格式", f"invalid-{name}")]:
        params = default_path_params(endpoint)
        params[name] = value
        cases.append(
            build_case(
                endpoint,
                counters,
                title=f"{endpoint.summary or endpoint.path} - Path 参数 {name} {label}",
                design_method="path_parameter_error",
                tags=["path_parameter", "negative"],
                query=default_query(endpoint, include_optional=True),
                path_params=params,
                body=default_body(endpoint),
                assertions=flexible_assertions(),
                runnable=True,
                risk="data_dependent",
                negative=True,
                expected_result="非法路径参数应返回明确错误，或返回表示资源不存在的业务结果",
            )
        )
    return cases


def body_negative_cases(endpoint: Endpoint, counters: dict[str, int]) -> list[TestCase]:
    return [
        build_case(
            endpoint,
            counters,
            title=f"{endpoint.summary or endpoint.path} - 请求体为空",
            design_method="required_field",
            tags=["required_field", "negative"],
            query=default_query(endpoint, include_optional=True),
            path_params=default_path_params(endpoint),
            body={},
            assertions=client_error_assertions(),
            runnable=False,
            risk="negative_behavior",
            negative=True,
            expected_result="请求体缺失必要字段时应返回客户端错误",
        ),
        build_case(
            endpoint,
            counters,
            title=f"{endpoint.summary or endpoint.path} - 请求体类型错误",
            design_method="type_error",
            tags=["type_error", "negative"],
            query=default_query(endpoint, include_optional=True),
            path_params=default_path_params(endpoint),
            body="invalid_body",
            assertions=client_error_assertions(),
            runnable=False,
            risk="negative_behavior",
            negative=True,
            expected_result="请求体结构错误时应返回客户端错误",
        ),
    ]


def equivalence_class_cases(endpoint: Endpoint, counters: dict[str, int]) -> list[TestCase]:
    if not endpoint.parameters and not endpoint.request_body:
        return []
    return [
        build_case(
            endpoint,
            counters,
            title=f"{endpoint.summary or endpoint.path} - 有效等价类代表值",
            design_method="equivalence_class",
            tags=["equivalence_class"],
            query=default_query(endpoint, include_optional=True),
            path_params=default_path_params(endpoint),
            body=default_body(endpoint),
            assertions=success_assertions(endpoint),
            runnable=default_runnable(endpoint),
            risk=default_risk(endpoint),
            expected_result="使用各参数有效等价类代表值时，接口应按业务规则返回成功或明确业务结果",
        )
    ]


def status_code_coverage_cases(endpoint: Endpoint, counters: dict[str, int]) -> list[TestCase]:
    expected = documented_status_codes(endpoint)
    if not expected:
        return []
    return [
        build_case(
            endpoint,
            counters,
            title=f"{endpoint.summary or endpoint.path} - 文档状态码覆盖",
            design_method="status_code_coverage",
            tags=["status_code_coverage"],
            query=default_query(endpoint, include_optional=True),
            path_params=default_path_params(endpoint),
            body=default_body(endpoint),
            assertions=[Assertion(type="status_code", operator="in", expected=expected)],
            runnable=default_runnable(endpoint),
            risk=default_risk(endpoint),
            expected_result=f"响应状态码应落在接口文档声明的状态码集合中：{expected}",
        )
    ]


def auth_error_cases(endpoint: Endpoint, counters: dict[str, int]) -> list[TestCase]:
    headers = default_headers(endpoint)
    headers.pop("Authorization", None)
    headers.pop("authorization", None)
    return [
        build_case(
            endpoint,
            counters,
            title=f"{endpoint.summary or endpoint.path} - 缺少鉴权信息",
            design_method="auth_error",
            tags=["auth_error", "negative"],
            query=default_query(endpoint, include_optional=True),
            path_params=default_path_params(endpoint),
            body=default_body(endpoint),
            assertions=[Assertion(type="status_code", operator="in", expected=[401, 403])],
            runnable=False,
            risk="auth_dependent",
            negative=True,
            expected_result="缺少鉴权信息时应返回 401 或 403",
        )
    ]


def has_auth_requirement(endpoint: Endpoint) -> bool:
    candidates = [
        str(parameter.get("name", "")).lower()
        for parameter in endpoint.parameters
        if parameter.get("in") in {"header", "cookie", "query"}
    ]
    candidates.extend(str(item).lower() for item in endpoint.tags)
    description = f"{endpoint.summary} {endpoint.description}".lower()
    return any(token in " ".join(candidates + [description]) for token in ["authorization", "token", "apikey", "api_key", "auth", "鉴权", "认证"])


def documented_status_codes(endpoint: Endpoint) -> list[int]:
    codes = []
    for key in endpoint.responses:
        try:
            codes.append(int(str(key)))
        except ValueError:
            continue
    return sorted(set(codes))


def generate_dependency_chain_cases(endpoints: list[Endpoint], counters: dict[str, int]) -> list[TestCase]:
    cases = []
    for producer in dependency_producers(endpoints):
        for endpoint in endpoints:
            if endpoint is producer:
                continue
            shared_param = first_path_param(endpoint)
            if not shared_param or shared_param not in likely_produced_fields(producer):
                continue
            cases.append(build_dependency_chain_case(producer, endpoint, shared_param, counters))
    return cases


def build_dependency_chain_case(generator: Endpoint, target: Endpoint, field_name: str, counters: dict[str, int]) -> TestCase:
    module = module_name(target)
    counters[module] = counters.get(module, 0) + 1
    case_id = f"TC-{module.upper()}-{counters[module]:03d}"
    target_name = target.summary or f"{target.method} {target.path}"
    case = TestCase(
        id=case_id,
        title=f"生成邮箱后执行：{target_name}",
        method="CHAIN",
        path=f"{generator.path} -> {target.path}",
        priority=normalize_priority("", negative=False, design_method="dependency_chain"),
        tags=[*target.tags, "dependency_chain"],
        design_method="dependency_chain",
        assertions=[],
        source=f"{generator.method} {generator.path} -> {target.method} {target.path}",
        runnable=True,
        risk="none",
        negative=False,
        expected_result=f"使用前置接口生成的 {field_name} 作为后续步骤输入，链路应成功完成",
        preconditions=[
            "测试环境和接口基础地址已正确配置。",
            f"前置接口可用，且生成的 {field_name} 可作为后续接口的路径参数。",
        ],
        test_steps=[
            f"调用前置接口，提取响应中的 {field_name}。",
            f"使用提取到的 {field_name} 调用目标接口：{target.method} {target.path}。",
            "校验链路中每一步的状态码和关键响应字段。",
        ],
        steps=[
            TestStep(
                name=generator.summary or f"{generator.method} {generator.path}",
                method=generator.method,
                path=generator.path,
                query=default_query(generator, include_optional=True),
                assertions=[
                    Assertion(type="status_code", operator="eq", expected=200),
                    Assertion(type="json_path", target=f"$.{field_name}", operator="not_empty"),
                ],
                extract={field_name: f"$.{field_name}"},
            ),
            TestStep(
                name=target_name,
                method=target.method,
                path=target.path,
                path_params={field_name: "{{" + field_name + "}}"},
                assertions=success_assertions(target),
            ),
        ],
    )
    return normalize_test_case(case, module=module, index=counters[module], source_tag="rule_generated")


def dependency_producers(endpoints: list[Endpoint]) -> list[Endpoint]:
    return [
        endpoint
        for endpoint in endpoints
        if endpoint.method.upper() in {"GET", "POST"}
        and any(token in f"{endpoint.path} {endpoint.summary} {endpoint.description}".lower() for token in ["generate", "create", "new", "生成", "创建"])
    ]


def first_path_param(endpoint: Endpoint) -> str:
    match = re.search(r"\{([^}]+)\}", endpoint.path)
    return match.group(1) if match else ""


def likely_produced_fields(endpoint: Endpoint) -> set[str]:
    fields = set()
    text = json.dumps(endpoint.responses, ensure_ascii=False).lower()
    for name in re.findall(r'"([a-zA-Z_][a-zA-Z0-9_]*)"', text):
        fields.add(name)
    if "email" in f"{endpoint.path} {endpoint.summary} {endpoint.description}".lower():
        fields.add("email")
    if not fields:
        fields.update({"id", "email", "token"})
    return fields


def attach_cleanup_steps(cases: list[TestCase], endpoints: list[Endpoint]) -> list[TestCase]:
    cleanup_by_field = cleanup_endpoints_by_field(endpoints)
    if not cleanup_by_field:
        return cases
    endpoint_by_key = {(endpoint.method.upper(), endpoint.path): endpoint for endpoint in endpoints}
    for case in cases:
        case.cleanup_steps = [step for step in case.cleanup_steps if not is_auto_cleanup_step(step)]
        if case.method.upper() == "DELETE":
            continue
        fields = produced_fields_for_case(case, endpoint_by_key)
        steps = []
        for field_name in sorted(fields):
            endpoint = cleanup_by_field.get(field_name)
            if not endpoint or case_already_deletes_field(case, endpoint, field_name):
                continue
            steps.append(
                TestStep(
                    name=f"清理测试数据：删除 {field_name}",
                    method=endpoint.method,
                    path=endpoint.path,
                    path_params={field_name: "{{" + field_name + "}}"},
                    assertions=[Assertion(type="status_code", operator="in", expected=[200, 202, 204, 404])],
                )
            )
        if steps:
            case.cleanup_steps.extend(steps)
            if not any("清理" in item for item in case.test_steps):
                case.test_steps.append("用例结束后执行清理步骤，释放本次测试创建的临时数据。")
    return cases


def attach_setup_steps(cases: list[TestCase], endpoints: list[Endpoint]) -> list[TestCase]:
    producers = producers_by_field(endpoints)
    if not producers:
        return cases
    for case in cases:
        if case.steps or case.setup_steps:
            continue
        if not should_auto_prepare_case(case):
            continue
        prepared = False
        for field_name, value in list(case.path_params.items()):
            if field_name not in producers or is_template_value(value):
                continue
            producer = producers[field_name]
            case.setup_steps.append(
                TestStep(
                    name=f"准备测试数据：生成 {field_name}",
                    method=producer.method,
                    path=producer.path,
                    query=default_query(producer, include_optional=True),
                    assertions=[
                        Assertion(type="status_code", operator="eq", expected=200),
                        Assertion(type="json_path", target=f"$.{field_name}", operator="not_empty"),
                    ],
                    extract={field_name: f"$.{field_name}"},
                )
            )
            case.path_params[field_name] = "{{" + field_name + "}}"
            prepared = True
        if prepared:
            case.runnable = True
            if case.risk in {"data_dependent_without_fixture", "destructive_without_fixture", "data_dependent"}:
                case.risk = "none"
            if not any("准备" in item for item in case.test_steps):
                case.test_steps.insert(0, "执行前置准备步骤，创建本用例需要的临时测试数据。")
            if not any("前置准备" in item for item in case.preconditions):
                case.preconditions.append("本用例会通过前置准备步骤创建所需测试数据，不依赖外部预置数据。")
    return cases


def producers_by_field(endpoints: list[Endpoint]) -> dict[str, Endpoint]:
    producers: dict[str, Endpoint] = {}
    for endpoint in dependency_producers(endpoints):
        for field_name in likely_produced_fields(endpoint):
            producers.setdefault(field_name, endpoint)
    return producers


def should_auto_prepare_case(case: TestCase) -> bool:
    if case.negative or case.design_method in {"type_error", "boundary_value", "path_parameter_error", "required_field", "auth_error"}:
        return False
    if not case.path_params:
        return False
    return True


def is_template_value(value: Any) -> bool:
    return isinstance(value, str) and "{{" in value and "}}" in value


def cleanup_endpoints_by_field(endpoints: list[Endpoint]) -> dict[str, Endpoint]:
    cleanup: dict[str, Endpoint] = {}
    for endpoint in endpoints:
        if endpoint.method.upper() != "DELETE":
            continue
        field_name = first_path_param(endpoint)
        if field_name:
            cleanup.setdefault(field_name, endpoint)
    return cleanup


def produced_fields_for_case(case: TestCase, endpoint_by_key: dict[tuple[str, str], Endpoint]) -> set[str]:
    fields = set()
    for step in case.setup_steps:
        endpoint = endpoint_by_key.get((step.method.upper(), step.path))
        if endpoint and is_resource_producer(endpoint):
            fields.update(likely_produced_fields(endpoint))
        fields.update(name for name in step.extract.keys() if name)
    if case.steps:
        for step in case.steps:
            if step.method.upper() == "DELETE":
                continue
            endpoint = endpoint_by_key.get((step.method.upper(), step.path))
            if endpoint and is_resource_producer(endpoint):
                fields.update(likely_produced_fields(endpoint))
            fields.update(name for name in step.extract.keys() if name)
        return fields
    endpoint = endpoint_by_key.get((case.method.upper(), case.path))
    if endpoint and endpoint.method.upper() != "DELETE" and is_resource_producer(endpoint):
        fields.update(likely_produced_fields(endpoint))
    return fields


def case_already_deletes_field(case: TestCase, cleanup_endpoint: Endpoint, field_name: str) -> bool:
    if case.method.upper() == "DELETE" and case.path == cleanup_endpoint.path:
        return True
    return any(
        step.method.upper() == "DELETE"
        and step.path == cleanup_endpoint.path
        and field_name in step.path_params
        for step in case.steps
    )


def is_auto_cleanup_step(step: TestStep) -> bool:
    return str(step.name).startswith("清理测试数据：")


def is_resource_producer(endpoint: Endpoint) -> bool:
    return endpoint in dependency_producers([endpoint])


def module_name(endpoint: Endpoint) -> str:
    if endpoint.tags:
        return slug(endpoint.tags[0])
    parts = [part for part in endpoint.path.split("/") if part and not part.startswith("{")]
    return slug(parts[0] if parts else "api")


def slug(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return normalized or "api"


def default_headers(endpoint: Endpoint) -> dict[str, str]:
    if endpoint.request_body:
        return {"Content-Type": "application/json"}
    return {}


def default_query(endpoint: Endpoint, include_optional: bool = True) -> dict[str, Any]:
    query: dict[str, Any] = {}
    for parameter in endpoint.parameters:
        if parameter.get("in") == "query" and (include_optional or parameter.get("required")):
            query[parameter["name"]] = sample_parameter_value(parameter)
    return query


def sample_parameter_value(parameter: dict[str, Any]) -> Any:
    name = str(parameter.get("name", "")).lower()
    if name == "prefix":
        return "aitest_{{timestamp}}"
    return sample_from_schema(parameter.get("schema", {}))


def default_path_params(endpoint: Endpoint) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for parameter in endpoint.parameters:
        if parameter.get("in") == "path":
            params[parameter["name"]] = sample_from_schema(parameter.get("schema", {}))
    for name in re.findall(r"{([^}]+)}", endpoint.path):
        params.setdefault(name, sample_path_value(name))
    return params


def sample_path_value(name: str) -> str:
    if name.lower() == "email":
        return "test123@mail.tempbox.cn"
    if name.lower().endswith("id"):
        return "1"
    return "sample"


def default_body(endpoint: Endpoint) -> Any:
    if not endpoint.request_body:
        return None
    content = endpoint.request_body.get("content", {})
    media = content.get("application/json") or next(iter(content.values()), {})
    return sample_from_schema(media.get("schema", {}))


def default_assertions(endpoint: Endpoint) -> list[Assertion]:
    return success_assertions(endpoint)


def success_assertions(endpoint: Endpoint) -> list[Assertion]:
    expected_status = "200"
    for status in endpoint.responses:
        if str(status).startswith("2"):
            expected_status = str(status)
            break
    return [Assertion(type="status_code", operator="eq", expected=int(expected_status))]


def client_error_assertions() -> list[Assertion]:
    return [Assertion(type="status_code", operator="in", expected=[400, 401, 403, 404, 409, 422, 429])]


def flexible_assertions() -> list[Assertion]:
    return [Assertion(type="status_code", operator="in", expected=[200, 201, 204, 400, 404, 409, 422, 429])]


def has_optional_query(endpoint: Endpoint) -> bool:
    return any(parameter.get("in") == "query" and not parameter.get("required") for parameter in endpoint.parameters)


def wrong_type_value(parameter: dict[str, Any]) -> Any:
    schema_type = parameter.get("schema", {}).get("type", "string")
    if schema_type in {"integer", "number"}:
        return "not-a-number"
    if schema_type == "boolean":
        return "not-a-boolean"
    if schema_type == "array":
        return "not-an-array"
    return 12345


def boundary_values(parameter: dict[str, Any]) -> list[tuple[str, Any]]:
    schema_type = parameter.get("schema", {}).get("type", "string")
    if schema_type in {"integer", "number"}:
        return [("0", 0), ("负数", -1), ("超大值", 999999999)]
    if schema_type == "boolean":
        return [("true", True), ("false", False)]
    return [
        ("空字符串", ""),
        ("单字符", "a"),
        ("特殊字符", "!@#$%^&*()"),
        ("超长字符串", "x" * 256),
    ]


def default_runnable(endpoint: Endpoint) -> bool:
    if endpoint.method in {"DELETE", "POST", "PUT", "PATCH"} and has_path_params(endpoint):
        return False
    return True


def default_risk(endpoint: Endpoint) -> str:
    if endpoint.method == "DELETE":
        return "destructive_without_fixture"
    if endpoint.method in {"POST", "PUT", "PATCH"} and has_path_params(endpoint):
        return "data_dependent_without_fixture"
    if has_path_params(endpoint):
        return "data_dependent"
    return "none"


def has_path_params(endpoint: Endpoint) -> bool:
    return bool(re.findall(r"{([^}]+)}", endpoint.path)) or any(
        parameter.get("in") == "path" for parameter in endpoint.parameters
    )


def sample_from_schema(schema: dict[str, Any]) -> Any:
    if not schema:
        return ""
    if "example" in schema:
        return schema["example"]
    if "default" in schema:
        return schema["default"]
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    ref = schema.get("$ref")
    if ref:
        return f"<{ref.rsplit('/', 1)[-1]}>"
    schema_type = schema.get("type", "object" if "properties" in schema else "string")
    if schema_type == "object":
        return {
            name: sample_from_schema(prop)
            for name, prop in schema.get("properties", {}).items()
        }
    if schema_type == "array":
        return [sample_from_schema(schema.get("items", {}))]
    if schema_type == "integer":
        return 1
    if schema_type == "number":
        return 1.0
    if schema_type == "boolean":
        return True
    return "string"


def save_cases(root: Path, cases: list[TestCase], project: str = "default") -> Path:
    path = project_cases_file(root, project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_cases_markdown(project, normalize_cases(cases)), encoding="utf-8")
    return path


def load_cases_from_markdown(root: Path, project: str = "default") -> list[TestCase]:
    path = project_cases_file(root, project)
    if not path.exists():
        return []
    return parse_cases_markdown(path.read_text(encoding="utf-8"))


def parse_cases_markdown(content: str) -> list[TestCase]:
    match = re.search(r"```aitest-cases-json\s*(.*?)\s*```", content, flags=re.S)
    if not match:
        return []
    payload = json.loads(match.group(1))
    cases = payload.get("cases", []) if isinstance(payload, dict) else []
    return normalize_cases([case_from_dict(item) for item in cases if isinstance(item, dict)])


def render_cases_markdown(project: str, cases: list[TestCase]) -> str:
    payload = {
        "version": 1,
        "project": safe_project_name(project),
        "cases": to_jsonable(cases),
    }
    sections = "\n\n".join(render_case_markdown(case) for case in cases)
    return f"""# {safe_project_name(project)} 接口测试用例集

> 这个文件是该项目的用例源文件。浏览器页面会直接读取和保存本文件。

## 结构化用例数据

```aitest-cases-json
{json.dumps(payload, ensure_ascii=False, indent=2)}
```

## 用例详情

{sections}
"""


def render_case_markdown(case: TestCase) -> str:
    assertions = "\n".join(
        f"| {index} | {item.type} | {item.target or '-'} | {item.operator} | {item.expected} |"
        for index, item in enumerate(case.assertions, start=1)
    ) or "| - | - | - | - | - |"
    preconditions = render_bullets(case.preconditions or ["无特殊前置条件。"])
    test_steps = render_numbered(case.test_steps or inferred_case_steps(case))
    setup_steps = render_setup_steps_markdown(case)
    steps = render_steps_markdown(case)
    cleanup_steps = render_cleanup_steps_markdown(case)
    return f"""### [{case.id}] {case.title}

#### 基本信息
| 属性 | 值 |
|------|-----|
| 用例ID | {case.id} |
| 接口路径 | {case.method} {case.path} |
| 用例等级 | {case.priority} |
| 设计方法 | {case.design_method} |
| 是否负向 | {case.negative} |
| 可执行 | {case.runnable} |
| 风险 | {case.risk} |
| 标签 | {", ".join(case.tags)} |
| 预期结果 | {case.expected_result} |

#### 前置条件
{preconditions}

#### 用例步骤
{test_steps}

#### 预期结果
{case.expected_result or "以断言列表为准。"}

#### 请求信息
- 方法：`{case.method}`
- 路径：`{case.path}`
- 路径参数：`{case.path_params}`
- Query：`{case.query}`

#### 断言列表
| 序号 | 断言类型 | 断言目标 | 操作符 | 期望值 |
|------|----------|----------|--------|--------|
{assertions}

{setup_steps}

{steps}

{cleanup_steps}

#### 生成说明
- 当前版本使用规则生成多设计方法用例；AI 生成可作为后续增强。
- 生成依据：{case.source}
"""


def render_bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) or "- 无"


def render_numbered(items: list[str]) -> str:
    return "\n".join(f"{index}. {item}" for index, item in enumerate(items, start=1)) or "1. 无"


def inferred_case_steps(case: TestCase) -> list[str]:
    if case.steps:
        return [f"执行链路步骤：{step.name}（{step.method} {step.path}）。" for step in case.steps]
    steps = [f"发送 {case.method} {case.path} 请求。"]
    if case.query:
        steps.append(f"使用 Query 参数：{case.query}。")
    if case.path_params:
        steps.append(f"替换路径参数：{case.path_params}。")
    if case.body is not None:
        steps.append(f"使用请求体：{case.body}。")
    steps.append("检查响应是否满足断言列表。")
    return steps


def render_steps_markdown(case: TestCase) -> str:
    if not case.steps:
        return ""
    lines = ["#### 执行步骤", ""]
    for index, step in enumerate(case.steps, start=1):
        assertions = ", ".join(
            f"{item.type} {item.target or '-'} {item.operator} {item.expected}"
            for item in step.assertions
        )
        lines.extend(
            [
                f"{index}. **{step.name}**",
                f"   - 请求：`{step.method} {step.path}`",
                f"   - 路径参数：`{step.path_params}`",
                f"   - Query：`{step.query}`",
                f"   - 提取：`{step.extract}`",
                f"   - 断言：{assertions or '-'}",
            ]
        )
    return "\n".join(lines)


def render_setup_steps_markdown(case: TestCase) -> str:
    if not case.setup_steps:
        return ""
    return render_lifecycle_steps_markdown("前置准备步骤", case.setup_steps)


def render_cleanup_steps_markdown(case: TestCase) -> str:
    if not case.cleanup_steps:
        return ""
    return render_lifecycle_steps_markdown("清理步骤", case.cleanup_steps)


def render_lifecycle_steps_markdown(title: str, steps: list[TestStep]) -> str:
    lines = [f"#### {title}", ""]
    for index, step in enumerate(steps, start=1):
        assertions = ", ".join(
            f"{item.type} {item.target or '-'} {item.operator} {item.expected}"
            for item in step.assertions
        )
        lines.extend(
            [
                f"{index}. **{step.name}**",
                f"   - 请求：`{step.method} {step.path}`",
                f"   - 路径参数：`{step.path_params}`",
                f"   - Query：`{step.query}`",
                f"   - 断言：{assertions or '-'}",
            ]
        )
    return "\n".join(lines)
