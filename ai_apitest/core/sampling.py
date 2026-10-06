"""Generate a small, validated request example from documented constraints."""
from __future__ import annotations

import copy
from decimal import Decimal, ROUND_CEILING
import math
import re
from re import _parser as regex_parser, _constants as regex_constants
from typing import Any

from ai_apitest.core.schema import normalize_schema, schema_errors


MAX_SAMPLE_SIZE = 10000


def request_schema(schema: Any) -> Any:
    if not isinstance(schema, dict):
        return schema
    result = copy.deepcopy(schema)
    properties = result.get("properties", {})
    if isinstance(properties, dict):
        result["properties"] = {key: request_schema(value) for key, value in properties.items() if not isinstance(value, dict) or not value.get("readOnly")}
        if "required" in result:
            result["required"] = [key for key in result["required"] if key in result["properties"] or key not in properties]
    if "items" in result:
        result["items"] = request_schema(result["items"])
    for key in ("allOf", "anyOf", "oneOf"):
        if isinstance(result.get(key), list):
            result[key] = [request_schema(value) for value in result[key]]
    return result


def _merge(left: dict, right: dict) -> dict:
    result = copy.deepcopy(left)
    for key, value in right.items():
        if key == "required":
            result[key] = list(dict.fromkeys([*result.get(key, []), *value]))
        elif key == "properties":
            result[key] = {name: _merge(result.get(key, {}).get(name, {}), item) if isinstance(item, dict) else item for name, item in {**result.get(key, {}), **value}.items()}
        elif key in {"minimum", "minLength", "minItems"} and key in result:
            result[key] = max(result[key], value)
        elif key in {"maximum", "maxLength", "maxItems"} and key in result:
            result[key] = min(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _regex_sample(pattern: str, minimum: int) -> str:
    def render(tokens, required_length=0) -> str:
        output = []
        for operation, argument in tokens:
            if operation == regex_constants.LITERAL:
                output.append(chr(argument))
            elif operation == regex_constants.IN:
                choices = []
                for op, value in argument:
                    if op == regex_constants.LITERAL:
                        choices.append(chr(value))
                    elif op == regex_constants.RANGE:
                        choices.append(chr(value[0]))
                    elif op == regex_constants.CATEGORY:
                        choices.append("0" if value == regex_constants.CATEGORY_DIGIT else "a")
                if not choices:
                    raise ValueError("无法生成该正则字符集合")
                output.append(choices[0])
            elif operation in {regex_constants.MAX_REPEAT, regex_constants.MIN_REPEAT}:
                low, high, child = argument
                unit = render(child) or "a"
                count = min(high, max(low, math.ceil(required_length / len(unit))))
                if count * len(unit) > MAX_SAMPLE_SIZE:
                    raise ValueError("正则样例超过大小限制")
                output.append(unit * count)
            elif operation == regex_constants.SUBPATTERN:
                output.append(render(argument[-1], required_length))
            elif operation == regex_constants.BRANCH:
                output.append(render(argument[1][0], required_length))
            elif operation == regex_constants.CATEGORY:
                output.append("0" if argument == regex_constants.CATEGORY_DIGIT else "a")
            elif operation == regex_constants.ANY:
                output.append("a")
            elif operation != regex_constants.AT:
                raise ValueError("正则需要项目方提供 example 或数据集")
        return "".join(output)
    return render(regex_parser.parse(pattern), minimum)


def sample_from_schema(schema: Any) -> Any:
    normalized = request_schema(normalize_schema(schema))

    def sample(item: Any, depth: int = 0) -> Any:
        if depth > 30:
            raise ValueError("Schema 嵌套过深，需要项目方提供测试数据")
        if item is False:
            raise ValueError("false Schema 不允许任何请求数据")
        if item is True or not item:
            return ""
        if not isinstance(item, dict):
            raise ValueError("Schema 必须是对象或布尔值")
        for name in ("example", "default", "const"):
            if name in item and not schema_errors(item[name], item):
                return copy.deepcopy(item[name])
        for value in item.get("enum", []):
            if not schema_errors(value, item):
                return copy.deepcopy(value)
        if "$ref" in item:
            reference = str(item["$ref"])
            if not reference.startswith("#/"):
                raise ValueError("Schema 引用未解析，需要有效 example 或完整本地契约")
            target = normalized
            for key in reference[2:].split("/"):
                target = target[key.replace("~1", "/").replace("~0", "~")]
            return sample(target, depth + 1)
        if "allOf" in item:
            merged = {key: value for key, value in item.items() if key != "allOf"}
            for branch in item["allOf"]:
                merged = _merge(merged, branch)
            return sample(merged, depth + 1)
        for keyword in ("oneOf", "anyOf"):
            if keyword in item:
                for branch in item[keyword]:
                    try:
                        candidate = sample(_merge({key: value for key, value in item.items() if key != keyword}, branch), depth + 1)
                        if not schema_errors(candidate, item):
                            return candidate
                    except ValueError:
                        continue
                raise ValueError(f"无法为 {keyword} 生成有效样例，需要 example 或数据集")
        kind = item.get("type", "object" if "properties" in item or "required" in item else "string")
        if isinstance(kind, list):
            for value in kind:
                try:
                    candidate = sample({**item, "type": value}, depth + 1)
                    if not schema_errors(candidate, item):
                        return candidate
                except ValueError:
                    continue
            raise ValueError("无法生成类型联合的样例")
        if kind == "object":
            values = {}
            for key, child in item.get("properties", {}).items():
                try:
                    values[key] = sample(child, depth + 1)
                except (ValueError, KeyError):
                    if key in item.get("required", []):
                        raise ValueError(f"必填字段 {key} 无法生成有效数据，请提供 example 或数据集")
            return values
        if kind == "array":
            count = max(0, min(int(item.get("maxItems", MAX_SAMPLE_SIZE)), max(1, int(item.get("minItems", 0)))))
            if count > MAX_SAMPLE_SIZE:
                raise ValueError("数组样例超过大小限制")
            values = [sample(child, depth + 1) for child in item.get("prefixItems", [])[:count]]
            values += [sample(item.get("items", {}), depth + 1) for _ in range(count - len(values))]
            if item.get("uniqueItems"):
                for index in range(1, len(values)):
                    if values[index] in values[:index]:
                        if isinstance(values[index], int) and not isinstance(values[index], bool):
                            values[index] += index
                        elif isinstance(values[index], str):
                            values[index] += str(index)
            return values
        if kind in {"integer", "number"}:
            value = max(Decimal("1"), Decimal(str(item.get("minimum", 1))))
            if "exclusiveMinimum" in item:
                value = max(value, Decimal(str(item["exclusiveMinimum"])) + (1 if kind == "integer" else Decimal("0.01")))
            if "maximum" in item:
                value = min(value, Decimal(str(item["maximum"])))
            if "exclusiveMaximum" in item and value >= Decimal(str(item["exclusiveMaximum"])):
                value = Decimal(str(item["exclusiveMaximum"])) - (1 if kind == "integer" else Decimal("0.01"))
            if "multipleOf" in item:
                multiple = Decimal(str(item["multipleOf"]))
                value = (value / multiple).to_integral_value(rounding=ROUND_CEILING) * multiple
            return int(value.to_integral_value(rounding=ROUND_CEILING)) if kind == "integer" else float(value)
        if kind == "boolean":
            return True
        if kind == "null":
            return None
        formats = {"email": "test@example.invalid", "uuid": "12345678-1234-4234-8234-123456789abc", "date": "2026-01-01", "date-time": "2026-01-01T00:00:00Z", "uri": "https://example.invalid", "ipv4": "192.0.2.1", "ipv6": "2001:db8::1", "hostname": "example.invalid"}
        minimum, maximum = int(item.get("minLength", 0)), int(item.get("maxLength", MAX_SAMPLE_SIZE))
        if max(minimum, maximum if maximum < 0 else 0) > MAX_SAMPLE_SIZE:
            raise ValueError("字符串样例超过大小限制")
        value = formats.get(item.get("format"), "string")
        if "pattern" in item:
            value = _regex_sample(item["pattern"], max(1, minimum))
        elif len(value) < minimum:
            value += "a" * (minimum - len(value))
        return value[:maximum]

    value = sample(normalized)
    errors = schema_errors(value, normalized)
    if errors:
        raise ValueError("无法生成满足 Schema 的默认数据，请提供 example 或数据集：" + "; ".join(errors))
    return value
