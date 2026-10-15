"""Bind positive generated variables to trusted request constraints."""
from __future__ import annotations

import copy
import re

from ai_apitest.core.sampling import request_schema
from ai_apitest.core.schema import schema_errors
from ai_apitest.execution.data_generation import generation_schema


def negative_step(step, case):
    if case.negative:
        return True
    for assertion in step.assertions:
        if assertion.type == "status_code" and assertion.operator in {"eq", "in"}:
            values = assertion.expected if isinstance(assertion.expected, list) else [assertion.expected]
            if values and all(isinstance(value, int) and value >= 400 for value in values):
                return True
    return False


def contract_map(endpoints):
    contracts = {}
    for endpoint in endpoints:
        body = endpoint.request_body or {}
        content = body.get("content", {})
        entry = content.get("application/json", next(iter(content.values()), {}))
        contracts[f"{endpoint.method.upper()} {endpoint.path}"] = {"parameters": endpoint.parameters, "body": request_schema(entry.get("schema", {})), "body_required": body.get("required", False)}
    return contracts


def resolved_schema(schema, root):
    if not isinstance(schema, dict):
        return schema
    visited = set()
    while "$ref" in schema:
        ref = schema["$ref"]
        if ref in visited or not ref.startswith("#/"):
            raise ValueError("生成数据引用需要完整的非循环字段契约")
        visited.add(ref)
        schema = root
        for key in ref[2:].split("/"):
            schema = schema[key.replace("~1", "/").replace("~0", "~")]
    if isinstance(schema, dict) and root.get("$defs"):
        schema = {**schema, "$defs": root["$defs"]}
    return schema


def bind_rules(case, rules, contracts):
    from ai_apitest.core.models import TestStep
    rules = copy.deepcopy(rules)
    steps = [*case.setup_steps, *(case.steps or [TestStep("request", case.method, case.path, headers=case.headers, path_params=case.path_params, query=case.query, body=case.body, assertions=case.assertions)]), *case.cleanup_steps]
    def bind(value, schema, root):
        if isinstance(value, str) and (match := re.fullmatch(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}", value)) and match[1] in rules:
            spec = rules[match[1]]
            spec["schema"] = {"allOf": [generation_schema(spec), resolved_schema(schema, root)]}
        elif isinstance(value, dict) and isinstance(schema, dict):
            schema = resolved_schema(schema, root)
            for name, item in value.items():
                if name in schema.get("properties", {}):
                    bind(item, schema["properties"][name], root)
        elif isinstance(value, list) and isinstance(schema, dict):
            for item in value:
                bind(item, schema.get("items", {}), root)
    for step in steps:
        contract = contracts.get(f"{step.method.upper()} {step.path}")
        if contract is None or negative_step(step, case):
            continue
        bind(step.body, contract["body"], contract["body"])
        for parameter in contract["parameters"]:
            values = {"query": step.query, "path": step.path_params, "header": step.headers}.get(parameter.get("in"), {})
            name = parameter.get("name")
            if name in values:
                schema = parameter.get("schema", {})
                bind(values[name], schema, schema)
    return {name: spec for name, spec in rules.items() if name not in case.variables}


def validate_generated_request(case, step, body, query, path_params, headers, contracts):
    if not case or not case.data_generation or negative_step(step, case):
        return
    contract = contracts.get(f"{step.method.upper()} {step.path}")
    if not contract:
        return
    if body is not None:
        if errors := schema_errors(body, contract["body"]):
            raise ValueError("生成后的请求体不符合可信契约：" + "; ".join(errors))
    elif contract["body_required"]:
        raise ValueError("生成用例缺少必填请求体")
    for parameter in contract["parameters"]:
        values = {"query": query, "path": path_params, "header": headers}.get(parameter.get("in"), {})
        name = parameter.get("name")
        if name not in values:
            if parameter.get("required"):
                raise ValueError(f"生成用例缺少必填参数 {name}")
            continue
        value, schema = values[name], parameter.get("schema", {})
        if isinstance(value, str) and parameter.get("in") in {"path", "query", "header"}:
            try:
                value = int(value) if schema.get("type") == "integer" else float(value) if schema.get("type") == "number" else value
            except ValueError:
                pass
        if errors := schema_errors(value, schema):
            raise ValueError(f"生成参数 {name} 不符合可信契约：" + "; ".join(errors))
