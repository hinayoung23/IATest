from __future__ import annotations

import json
import mimetypes
import re
from pathlib import Path
from typing import Any

from ai_apitest.core.case_schema import (
    external_test_capability_summary,
    normalize_cases,
    normalize_priority,
    normalize_test_case,
)
from ai_apitest.core.models import Assertion, Endpoint, TestCase, TestStep
from ai_apitest.core.artifact_language import (
    artifact_text,
    current_artifact_language,
    normalize_artifact_language,
    set_current_artifact_language,
)
from ai_apitest.core.files import atomic_write_text
from ai_apitest.core.paths import (
    project_cases_file,
    project_coverage_manifest_file,
    project_docs_file,
    safe_project_name,
)
from ai_apitest.core.serde import case_from_dict, endpoint_from_dict, read_json, to_jsonable, write_json


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
    headers: dict[str, Any] | None = None,
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
        headers=default_headers(endpoint) if headers is None else headers,
        path_params=path_params or {},
        query=query or {},
        parameter_serialization=parameter_serialization(endpoint),
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
    cases: list[TestCase] = []
    schema = request_body_schema(endpoint)
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    if bool((endpoint.request_body or {}).get("required") or required):
        cases.append(build_case(
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
        ))
    cases.append(build_case(
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
        ))
    return cases


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
    # None is an explicit removal marker. It overrides environment-level auth
    # headers in the executor instead of merely omitting a case-level header.
    for name in ("Authorization", "Cookie", "X-API-Key", "X-Auth-Token"):
        headers[name] = None
    query = default_query(endpoint, include_optional=True)
    for parameter in endpoint.parameters:
        if parameter.get("in") == "query" and is_auth_name(str(parameter.get("name") or "")):
            query.pop(str(parameter.get("name")), None)
    return [
        build_case(
            endpoint,
            counters,
            title=f"{endpoint.summary or endpoint.path} - 缺少鉴权信息",
            design_method="auth_error",
            tags=["auth_error", "negative"],
            headers=headers,
            query=query,
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
    if endpoint.security:
        return True
    candidates = [
        str(parameter.get("name", "")).lower()
        for parameter in endpoint.parameters
        if parameter.get("in") in {"header", "cookie", "query"}
    ]
    candidates.extend(str(item).lower() for item in endpoint.tags)
    description = f"{endpoint.summary} {endpoint.description}".lower()
    return any(token in " ".join(candidates + [description]) for token in ["authorization", "token", "apikey", "api_key", "auth", "鉴权", "认证"])


def is_auth_name(value: str) -> bool:
    normalized = value.strip().lower().replace("-", "_")
    return any(token in normalized for token in ("authorization", "token", "api_key", "apikey", "auth", "cookie"))


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
                parameter_serialization=parameter_serialization(generator),
                assertions=[
                    *success_assertions(generator),
                    Assertion(type="json_path", target=f"$.{field_name}", operator="not_empty"),
                ],
                extract={field_name: f"$.{field_name}"},
            ),
            TestStep(
                name=target_name,
                method=target.method,
                path=target.path,
                path_params={field_name: "{{" + field_name + "}}"},
                parameter_serialization=parameter_serialization(target),
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
        # Resource creation must be expressed by the operation itself. Searching
        # the whole description produces false positives such as an "extend"
        # endpoint whose error description merely says "never created".
        and any(
            token in f"{endpoint.path} {endpoint.summary} {endpoint.operation_id}".lower()
            for token in ["generate", "create", "new", "生成", "创建"]
        )
    ]


def first_path_param(endpoint: Endpoint) -> str:
    match = re.search(r"\{([^}]+)\}", endpoint.path)
    return match.group(1) if match else ""


def likely_produced_fields(endpoint: Endpoint) -> set[str]:
    """Return only response fields proved by the documented response schema."""
    fields: set[str] = set()
    for response in endpoint.responses.values():
        if not isinstance(response, dict):
            continue
        schemas: list[Any] = [response.get("schema")]
        content = response.get("content")
        if isinstance(content, dict):
            schemas.extend(
                media.get("schema")
                for media in content.values()
                if isinstance(media, dict)
            )
        for schema in schemas:
            if not isinstance(schema, dict):
                continue
            properties = schema.get("properties")
            if isinstance(properties, dict):
                fields.update(str(name) for name in properties if str(name))
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
            variable = cleanup_variable_for_case(case, field_name)
            steps.append(
                TestStep(
                    name=f"清理测试数据：删除 {field_name}",
                    method=endpoint.method,
                    path=endpoint.path,
                    path_params={field_name: "{{" + variable + "}}"},
                    parameter_serialization=parameter_serialization(endpoint),
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
        if case.steps or case.setup_steps or case.fixture_requirements:
            continue
        can_auto_prepare = should_auto_prepare_case(case)
        prepared = False
        absent_value_injected = False
        for field_name, value in list(case.path_params.items()):
            if field_name not in producers or is_template_value(value):
                continue
            if case_requires_absent_resource(case):
                case.path_params[field_name] = absent_resource_value(field_name)
                absent_value_injected = True
                continue
            if not can_auto_prepare:
                continue
            producer = producers[field_name]
            case.setup_steps.append(
                TestStep(
                    name=f"准备测试数据：生成 {field_name}",
                    method=producer.method,
                    path=producer.path,
                    query=default_query(producer, include_optional=True),
                    parameter_serialization=parameter_serialization(producer),
                    assertions=[
                        *success_assertions(producer),
                        Assertion(type="json_path", target=f"$.{field_name}", operator="not_empty"),
                    ],
                    extract={field_name: f"$.{field_name}"},
                )
            )
            case.path_params[field_name] = "{{" + field_name + "}}"
            prepared = True
        if prepared or absent_value_injected:
            case.runnable = True
            if absent_value_injected or case.risk in {
                "data_dependent_without_fixture",
                "destructive_without_fixture",
                "data_dependent",
            }:
                case.risk = "none"
            case.test_steps = [
                f"替换路径参数：{case.path_params}。"
                if str(item).startswith("替换路径参数：")
                else item
                for item in case.test_steps
            ]
        if absent_value_injected:
            if not any("不存在资源" in item for item in case.preconditions):
                case.preconditions.append(
                    "本用例使用基于 UUID 的保留测试域值表示不存在资源，不创建前置业务数据。"
                )
        if prepared:
            if not any("准备" in item for item in case.test_steps):
                case.test_steps.insert(0, "执行前置准备步骤，创建本用例需要的临时测试数据。")
            if not any("前置准备" in item for item in case.preconditions):
                case.preconditions.append("本用例会通过前置准备步骤创建所需测试数据，不依赖外部预置数据。")
    return cases


def case_requires_absent_resource(case: TestCase) -> bool:
    title = str(case.title or "")
    return bool(
        re.search(
            r"不存在|未创建|从未创建|未生成|从未生成|未注册|已删除|删除后|"
            r"\b(?:nonexistent|missing|not[\s_-]*found|never[\s_-]*(?:created|generated)|deleted)\b",
            title,
            flags=re.I,
        )
    )


def absent_resource_value(field_name: str) -> str:
    name = str(field_name or "").lower()
    if "email" in name or "mail" in name:
        return "missing_{{uuid}}@invalid.test"
    if name.endswith("id") or name == "id":
        return "missing-{{uuid}}"
    return "missing-{{uuid}}"


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
            if endpoint and is_resource_producer(endpoint) and assertions_allow_success(step.assertions):
                fields.update(likely_produced_fields(endpoint))
            fields.update(name for name in step.extract.keys() if name)
        return fields
    endpoint = endpoint_by_key.get((case.method.upper(), case.path))
    if (
        endpoint
        and endpoint.method.upper() != "DELETE"
        and is_resource_producer(endpoint)
        and assertions_allow_success(case.assertions)
    ):
        fields.update(likely_produced_fields(endpoint))
    return fields


def assertions_allow_success(assertions: list[Assertion]) -> bool:
    status_assertions = [item for item in assertions if item.type == "status_code"]
    if not status_assertions:
        return True
    for assertion in status_assertions:
        if assertion.operator == "is_2xx":
            return True
        values = assertion.expected if isinstance(assertion.expected, list) else [assertion.expected]
        for value in values:
            try:
                if 200 <= int(value) < 300:
                    return True
            except (TypeError, ValueError):
                continue
    return False


def cleanup_variable_for_case(case: TestCase, field_name: str) -> str:
    expected_path = f"$.{field_name}"
    for step in [*case.setup_steps, *case.steps]:
        for variable, path in step.extract.items():
            if path == expected_path:
                return variable
    return field_name


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


def default_headers(endpoint: Endpoint) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    media_type = request_body_media_type(endpoint)
    if media_type:
        headers["Content-Type"] = media_type
    cookies: list[str] = []
    for parameter in endpoint.parameters:
        if not parameter.get("required"):
            continue
        location = str(parameter.get("in") or "")
        name = str(parameter.get("name") or "")
        if not name:
            continue
        value = sample_parameter_value(parameter)
        if location == "header":
            headers[name] = serialize_header_parameter(value, explode=bool(parameter.get("explode", False)))
        elif location == "cookie":
            cookies.extend(serialize_cookie_parameter(name, value, explode=bool(parameter.get("explode", True))))
    if cookies:
        headers["Cookie"] = "; ".join(cookies)
    return headers


def wire_parameter_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def serialize_header_parameter(value: Any, *, explode: bool) -> str:
    if isinstance(value, dict):
        if explode:
            return ",".join(f"{key}={wire_parameter_scalar(item)}" for key, item in value.items())
        return ",".join(
            wire_parameter_scalar(part)
            for key, item in value.items()
            for part in (key, item)
        )
    if isinstance(value, (list, tuple)):
        return ",".join(wire_parameter_scalar(item) for item in value)
    return wire_parameter_scalar(value)


def serialize_cookie_parameter(name: str, value: Any, *, explode: bool) -> list[str]:
    if isinstance(value, dict):
        if explode:
            return [f"{key}={wire_parameter_scalar(item)}" for key, item in value.items()]
        flattened = ",".join(
            wire_parameter_scalar(part)
            for key, item in value.items()
            for part in (key, item)
        )
        return [f"{name}={flattened}"]
    if isinstance(value, (list, tuple)) and explode:
        return [f"{name}={wire_parameter_scalar(item)}" for item in value]
    if isinstance(value, (list, tuple)):
        return [f"{name}=" + ",".join(wire_parameter_scalar(item) for item in value)]
    return [f"{name}={wire_parameter_scalar(value)}"]


def parameter_serialization(endpoint: Endpoint) -> dict[str, dict[str, Any]]:
    rules: dict[str, dict[str, Any]] = {}
    for parameter in endpoint.parameters:
        name = str(parameter.get("name") or "")
        location = str(parameter.get("in") or "")
        if not name or location not in {"path", "query"}:
            continue
        default_style = "simple" if location == "path" else "form"
        style = str(parameter.get("style") or default_style)
        explode = parameter.get("explode")
        if not isinstance(explode, bool):
            explode = style == "form"
        rules[f"{location}.{name}"] = {
            "style": style,
            "explode": explode,
            "allow_reserved": bool(parameter.get("allowReserved", False)),
        }
    return rules


def request_body_media_type(endpoint: Endpoint) -> str:
    if not endpoint.request_body:
        return ""
    if endpoint.request_body.get("mode") == "file":
        path = str(endpoint.request_body.get("file_path") or "")
        return mimetypes.guess_type(path)[0] or "application/octet-stream"
    content = endpoint.request_body.get("content", {})
    if not isinstance(content, dict) or not content:
        return "application/json"
    for preferred in (
        "application/json",
        "application/x-www-form-urlencoded",
        "multipart/form-data",
        "application/octet-stream",
    ):
        if preferred in content:
            return preferred
    return str(next(iter(content)))


def default_query(endpoint: Endpoint, include_optional: bool = True) -> dict[str, Any]:
    query: dict[str, Any] = {}
    for parameter in endpoint.parameters:
        if parameter.get("in") == "query" and (include_optional or parameter.get("required")):
            query[parameter["name"]] = sample_parameter_value(parameter)
    return query


def sample_parameter_value(parameter: dict[str, Any]) -> Any:
    name = str(parameter.get("name", "")).lower()
    if name == "prefix":
        return "iatest_{{timestamp}}"
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
    if endpoint.request_body.get("mode") == "file":
        path = str(endpoint.request_body.get("file_path") or "").strip()
        return {"$iatest_file": path} if path else None
    content = endpoint.request_body.get("content", {})
    media_type = request_body_media_type(endpoint)
    media = content.get(media_type) or next(iter(content.values()), {})
    schema = media.get("schema", {})
    if media_type == "multipart/form-data":
        return sample_multipart_body(schema)
    return sample_from_schema(schema)


def request_body_schema(endpoint: Endpoint) -> dict[str, Any]:
    if not endpoint.request_body:
        return {}
    content = endpoint.request_body.get("content", {})
    if not isinstance(content, dict) or not content:
        return {}
    media = content.get(request_body_media_type(endpoint)) or next(iter(content.values()), {})
    schema = media.get("schema") if isinstance(media, dict) else {}
    return schema if isinstance(schema, dict) else {}


def sample_multipart_body(schema: dict[str, Any]) -> dict[str, Any]:
    body = sample_from_schema(schema)
    if not isinstance(body, dict):
        return {"value": body}
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    for name, property_schema in properties.items() if isinstance(properties, dict) else []:
        if isinstance(property_schema, dict) and property_schema.get("format") in {"binary", "byte"}:
            body[str(name)] = {
                "filename": f"{name}.bin",
                "content": "iatest-sample",
                "content_type": str(property_schema.get("contentMediaType") or "application/octet-stream"),
            }
    return body


def default_assertions(endpoint: Endpoint) -> list[Assertion]:
    return success_assertions(endpoint)


def success_assertions(endpoint: Endpoint) -> list[Assertion]:
    expected_status: str | None = None
    for status in endpoint.responses:
        if str(status).isdigit() and str(status).startswith("2"):
            expected_status = str(status)
            break
    if expected_status is None:
        # An undocumented response must not silently become an invented 200
        # contract. We can still assert the protocol-level success class.
        return [Assertion(type="status_code", operator="is_2xx", expected=None)]
    assertions = [Assertion(type="status_code", operator="eq", expected=int(expected_status))]
    response = endpoint.responses.get(expected_status) or endpoint.responses.get(int(expected_status)) or {}
    if not isinstance(response, dict):
        return assertions
    content = response.get("content") if isinstance(response.get("content"), dict) else {}
    schemas = [
        response.get("schema"),
        *(media.get("schema") for media in content.values() if isinstance(media, dict)),
    ]
    schema = next((item for item in schemas if isinstance(item, dict)), {})
    if schema.get("type") == "array":
        assertions.append(Assertion(type="json_path", target="$", operator="is_array", expected=True))
        return assertions
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    declared_required = schema.get("required")
    required = (
        {str(item) for item in declared_required}
        if isinstance(declared_required, list)
        else {str(item) for item in properties}
    )
    for name, property_schema in properties.items():
        if str(name) not in required or not isinstance(property_schema, dict):
            continue
        schema_type = property_schema.get("type")
        nullable = bool(property_schema.get("nullable")) or (
            isinstance(schema_type, list) and "null" in schema_type
        )
        if isinstance(schema_type, list):
            schema_type = next((item for item in schema_type if item != "null"), "")
        operator = "exists" if nullable else {
            "array": "is_array",
            "string": "is_string",
            "integer": "is_integer",
            "number": "is_number",
            "boolean": "is_boolean",
            "object": "is_object",
        }.get(str(schema_type or ""), "exists")
        assertions.append(
            Assertion(
                type="json_path",
                target=f"$.{name}",
                operator=operator,
                expected=True,
            )
        )
    return assertions


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
    schema = parameter.get("schema", {}) if isinstance(parameter.get("schema"), dict) else {}
    schema_type = schema.get("type", "string")
    if schema_type in {"integer", "number"}:
        values: list[tuple[str, Any]] = []
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        exclusive_minimum = schema.get("exclusiveMinimum")
        exclusive_maximum = schema.get("exclusiveMaximum")
        if isinstance(exclusive_minimum, (int, float)) and not isinstance(exclusive_minimum, bool):
            minimum = exclusive_minimum
        if isinstance(exclusive_maximum, (int, float)) and not isinstance(exclusive_maximum, bool):
            maximum = exclusive_maximum
        if isinstance(minimum, (int, float)) and not isinstance(minimum, bool):
            delta = 1 if schema_type == "integer" else 0.1
            if exclusive_minimum is True or (isinstance(exclusive_minimum, (int, float)) and not isinstance(exclusive_minimum, bool)):
                values.extend([("高于排他最小边界", minimum + delta), ("等于排他最小边界", minimum)])
            else:
                values.extend([("最小边界", minimum), ("低于最小边界", minimum - delta)])
        if isinstance(maximum, (int, float)) and not isinstance(maximum, bool):
            delta = 1 if schema_type == "integer" else 0.1
            if exclusive_maximum is True or (isinstance(exclusive_maximum, (int, float)) and not isinstance(exclusive_maximum, bool)):
                values.extend([("低于排他最大边界", maximum - delta), ("等于排他最大边界", maximum)])
            else:
                values.extend([("最大边界", maximum), ("高于最大边界", maximum + delta)])
        return values or [("0", 0), ("负数", -1), ("超大值", 999999999)]
    if schema_type == "boolean":
        return [("true", True), ("false", False)]
    if schema_type == "array":
        values = []
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        sample = sample_from_schema(schema.get("items", {}))
        if isinstance(minimum, int):
            values.extend([("最小元素数", [sample] * minimum), ("低于最小元素数", [sample] * max(0, minimum - 1))])
        if isinstance(maximum, int):
            values.extend([("最大元素数", [sample] * maximum), ("高于最大元素数", [sample] * (maximum + 1))])
        return values or [("空数组", []), ("单元素", [sample])]
    string_values: list[tuple[str, Any]] = []
    minimum_length = schema.get("minLength")
    maximum_length = schema.get("maxLength")
    if isinstance(minimum_length, int):
        string_values.extend(
            [
                ("最小长度", "a" * minimum_length),
                ("低于最小长度", "a" * max(0, minimum_length - 1)),
            ]
        )
    if isinstance(maximum_length, int):
        string_values.extend(
            [
                ("最大长度", "a" * maximum_length),
                ("高于最大长度", "a" * (maximum_length + 1)),
            ]
        )
    if schema.get("pattern"):
        string_values.append(("不匹配格式", "!invalid-format!"))
    if string_values:
        return string_values
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


def save_cases(
    root: Path,
    cases: list[TestCase],
    project: str = "default",
    *,
    artifact_language: str | None = None,
) -> Path:
    path = project_cases_file(root, project)
    path.parent.mkdir(parents=True, exist_ok=True)
    language = normalize_artifact_language(
        artifact_language or current_artifact_language()
    )
    set_current_artifact_language(language)
    normalized = normalize_cases(cases)
    atomic_write_text(
        path,
        render_cases_markdown(project, normalized, artifact_language=language),
    )
    endpoints = [
        endpoint_from_dict(item)
        for item in read_json(project_docs_file(root, project), [])
        if isinstance(item, dict)
    ]
    from ai_apitest.domain.contracts import build_coverage_manifest

    write_json(
        project_coverage_manifest_file(root, project),
        build_coverage_manifest(endpoints, normalized),
    )
    return path


def load_cases_from_markdown(root: Path, project: str = "default") -> list[TestCase]:
    path = project_cases_file(root, project)
    if not path.exists():
        return []
    return parse_cases_markdown(path.read_text(encoding="utf-8"))


def parse_cases_markdown(content: str) -> list[TestCase]:
    match = re.search(r"```iatest-cases-json\s*(.*?)\s*```", content, flags=re.S)
    if not match:
        return []
    payload = json.loads(match.group(1))
    cases = payload.get("cases", []) if isinstance(payload, dict) else []
    return normalize_cases([case_from_dict(item) for item in cases if isinstance(item, dict)])


def render_cases_markdown(
    project: str,
    cases: list[TestCase],
    *,
    artifact_language: str | None = None,
) -> str:
    language = normalize_artifact_language(
        artifact_language or current_artifact_language()
    )
    t = lambda key: artifact_text(key, language)
    payload = {
        "version": 2,
        "project": safe_project_name(project),
        "artifact_language": language,
        "cases": to_jsonable(cases),
    }
    sections = "\n\n".join(
        render_case_markdown(case, artifact_language=language)
        for case in cases
    )
    return f"""# {safe_project_name(project)} {t('test_case_collection')}

> {t('case_source_note')}

## {t('structured_case_data')}

```iatest-cases-json
{json.dumps(payload, ensure_ascii=False, indent=2)}
```

## {t('case_details')}

{sections}
"""


def render_case_markdown(
    case: TestCase,
    *,
    artifact_language: str | None = None,
) -> str:
    language = normalize_artifact_language(
        artifact_language or current_artifact_language()
    )
    t = lambda key: artifact_text(key, language)
    assertions = "\n".join(
        f"| {index} | {item.type} | {item.target or '-'} | {item.operator} | {item.expected} |"
        for index, item in enumerate(case.assertions, start=1)
    ) or "| - | - | - | - | - |"
    preconditions = render_bullets(case.preconditions or [t("no_special_preconditions")], artifact_language=language)
    test_steps = render_numbered(case.test_steps or inferred_case_steps(case))
    setup_steps = render_setup_steps_markdown(case, artifact_language=language)
    steps = render_steps_markdown(case, artifact_language=language)
    cleanup_steps = render_cleanup_steps_markdown(case, artifact_language=language)
    return f"""### [{case.id}] {case.title}

#### {t('basic_information')}
| {t('attribute')} | {t('value')} |
|------|-----|
| {t('case_id')} | {case.id} |
| {t('endpoint_path')} | {case.method} {case.path} |
| {t('priority')} | {case.priority} |
| {t('design_method')} | {case.design_method} |
| {t('negative')} | {case.negative} |
| {t('runnable')} | {case.runnable} |
| {t('risk')} | {case.risk} |
| {t('tags')} | {", ".join(case.tags)} |
| {t('business_rules')} | {", ".join(case.rule_ids) or "-"} |
| {t('coverage_partitions')} | {", ".join(case.coverage_partition_ids) or "-"} |
| {t('project_must_provide')} | {external_test_capability_summary(case.fixture_requirements, language) or "-"} |
| {t('expected_result')} | {case.expected_result} |

#### {t('preconditions')}
{preconditions}

#### {t('case_steps')}
{test_steps}

#### {t('expected_result')}
{case.expected_result or t('assertions_define_expected')}

#### {t('request_information')}
- {t('method')}: `{case.method}`
- {t('path')}: `{case.path}`
- {t('path_parameters')}: `{case.path_params}`
- Query：`{case.query}`

#### {t('assertions')}
| {t('number')} | {t('assertion_type')} | {t('assertion_target')} | {t('operator')} | {t('expected_value')} |
|------|----------|----------|--------|--------|
{assertions}

{setup_steps}

{steps}

{cleanup_steps}

#### {t('generation_notes')}
- {t('rule_generation_note')}
- {t('generation_basis')}: {case.source}
"""


def render_bullets(items: list[str], *, artifact_language: str | None = None) -> str:
    return "\n".join(f"- {item}" for item in items) or f"- {artifact_text('none', artifact_language)}"


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


def render_steps_markdown(
    case: TestCase,
    *,
    artifact_language: str | None = None,
) -> str:
    if not case.steps:
        return ""
    language = normalize_artifact_language(
        artifact_language or current_artifact_language()
    )
    t = lambda key: artifact_text(key, language)
    lines = [f"#### {t('execution_steps')}", ""]
    for index, step in enumerate(case.steps, start=1):
        assertions = ", ".join(
            f"{item.type} {item.target or '-'} {item.operator} {item.expected}"
            for item in step.assertions
        )
        lines.extend(
            [
                f"{index}. **{step.name}**",
                f"   - {t('request')}: `{step.method} {step.path}`",
                f"   - {t('path_parameters')}: `{step.path_params}`",
                f"   - Query：`{step.query}`",
                f"   - {t('extract')}: `{step.extract}`",
                f"   - {t('assertions')}: {assertions or '-'}",
            ]
        )
    return "\n".join(lines)


def render_setup_steps_markdown(
    case: TestCase,
    *,
    artifact_language: str | None = None,
) -> str:
    if not case.setup_steps:
        return ""
    return render_lifecycle_steps_markdown(
        artifact_text("setup_steps", artifact_language),
        case.setup_steps,
        artifact_language=artifact_language,
    )


def render_cleanup_steps_markdown(
    case: TestCase,
    *,
    artifact_language: str | None = None,
) -> str:
    if not case.cleanup_steps:
        return ""
    return render_lifecycle_steps_markdown(
        artifact_text("cleanup_steps", artifact_language),
        case.cleanup_steps,
        artifact_language=artifact_language,
    )


def render_lifecycle_steps_markdown(
    title: str,
    steps: list[TestStep],
    *,
    artifact_language: str | None = None,
) -> str:
    language = normalize_artifact_language(
        artifact_language or current_artifact_language()
    )
    t = lambda key: artifact_text(key, language)
    lines = [f"#### {title}", ""]
    for index, step in enumerate(steps, start=1):
        assertions = ", ".join(
            f"{item.type} {item.target or '-'} {item.operator} {item.expected}"
            for item in step.assertions
        )
        lines.extend(
            [
                f"{index}. **{step.name}**",
                f"   - {t('request')}: `{step.method} {step.path}`",
                f"   - {t('path_parameters')}: `{step.path_params}`",
                f"   - Query：`{step.query}`",
                f"   - {t('assertions')}: {assertions or '-'}",
            ]
        )
    return "\n".join(lines)
