"""Bounded, reproducible runtime data; recipes never execute user code."""
from __future__ import annotations

import copy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
import random
import re
from re import _parser, _constants
import uuid
from typing import Any

from ai_apitest.core.schema import check_schema, normalize_schema, schema_errors
from ai_apitest.execution.control import check_control

GENERATOR_VERSION = "1"
MAX_SIZE = 4096
MAX_ITEMS = 1000
MAX_ATTEMPTS = 100
RESERVED = {"uuid", "random", "timestamp", "base_url", "iatest_namespace"}


def generation_schema(spec: dict) -> Any:
    if not isinstance(spec, dict):
        raise ValueError("数据生成规则必须是对象")
    schema = copy.deepcopy(spec.get("schema", {key: value for key, value in spec.items() if key not in {"kind", "unique", "charset", "prefix", "suffix", "partitions"}}))
    kind = spec.get("kind")
    if kind:
        if not isinstance(schema, dict):
            raise ValueError("kind 需要对象形式的 Schema")
        aliases = {"email": "email", "uuid": "uuid", "date": "date", "date-time": "date-time"}
        if kind in aliases:
            schema.setdefault("type", "string")
            schema.setdefault("format", aliases[kind])
        else:
            schema.setdefault("type", kind)
    if "unique" in spec and type(spec["unique"]) is not bool:
        raise ValueError("unique 必须是布尔值")
    if any(not isinstance(spec.get(key, ""), str) for key in ("prefix", "suffix")) or len(spec.get("prefix", "") + spec.get("suffix", "")) > MAX_SIZE:
        raise ValueError("prefix/suffix 必须是大小受限的字符串")
    check_schema(schema)
    return normalize_schema(schema)


def regex_value(pattern: str, rng: random.Random, cap: int) -> str:
    if len(pattern) > 256:
        raise ValueError("生成正则超过 256 字符")
    def render(tokens, depth=0):
        if depth > 16:
            raise ValueError("生成正则嵌套过深")
        out = []
        for op, value in tokens:
            if op == _constants.LITERAL:
                out.append(chr(value))
            elif op == _constants.AT:
                continue
            elif op == _constants.ANY:
                out.append(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789"))
            elif op == _constants.SUBPATTERN:
                out.append(render(value[-1], depth + 1))
            elif op == _constants.BRANCH:
                out.append(render(rng.choice(value[1]), depth + 1))
            elif op in {_constants.MAX_REPEAT, _constants.MIN_REPEAT}:
                low, high, child = value
                unit = render(child, depth + 1)
                high = min(high, cap // max(1, len(unit)))
                if low > high:
                    raise ValueError("正则重复次数超过大小限制")
                out.append("".join(render(child, depth + 1) for _ in range(rng.randint(low, high))))
            elif op in {_constants.IN, _constants.CATEGORY}:
                choices = []
                for item, argument in (value if op == _constants.IN else [(op, value)]):
                    if item == _constants.LITERAL:
                        choices.append(chr(argument))
                    elif item == _constants.RANGE:
                        choices.append(chr(rng.randint(*argument)))
                    elif item == _constants.CATEGORY:
                        alphabet = {_constants.CATEGORY_DIGIT: "0123456789", _constants.CATEGORY_WORD: "abcdefghijklmnopqrstuvwxyz0123456789_", _constants.CATEGORY_SPACE: " "}.get(argument)
                        if not alphabet:
                            raise ValueError("不支持该正则类别")
                        choices.append(rng.choice(alphabet))
                    else:
                        raise ValueError("不支持该正则字符集合")
                out.append(rng.choice(choices))
            else:
                raise ValueError("正则无法安全采样，请配置数据集或更简单的规则")
            if sum(map(len, out)) > cap:
                raise ValueError("正则样本超过大小限制")
        return "".join(out)
    return render(_parser.parse(pattern))


def sample_schema(schema: Any, rng: random.Random, *, depth=0, root=None, padding=0) -> Any:
    check_control()
    if depth > 16:
        raise ValueError("数据 Schema 嵌套超过 16 层")
    root = schema if root is None else root
    if schema is False:
        raise ValueError("Schema 不允许任何值")
    if schema is True:
        schema = {"type": "string"}
    if "$ref" in schema:
        ref = schema["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            raise ValueError("数据生成仅支持完整的本地 Schema 引用")
        target = root
        for key in ref[2:].split("/"):
            target = target[key.replace("~1", "/").replace("~0", "~")]
        return sample_schema(target, rng, depth=depth + 1, root=root, padding=padding)
    if "const" in schema:
        return copy.deepcopy(schema["const"])
    if "enum" in schema:
        return copy.deepcopy(rng.choice(schema["enum"]))
    for keyword in ("oneOf", "anyOf"):
        if keyword in schema:
            branch = rng.choice(schema[keyword])
            return sample_schema(branch, rng, depth=depth + 1, root=root, padding=padding)
    if "allOf" in schema:
        from ai_apitest.core.sampling import _merge
        merged = {key: value for key, value in schema.items() if key != "allOf"}
        for item in schema["allOf"]:
            merged = _merge(merged, item)
        return sample_schema(merged, rng, depth=depth + 1, root=root, padding=padding)
    kind = schema.get("type", "object" if "properties" in schema else "string")
    if isinstance(kind, list):
        kind = rng.choice(kind)
    if kind == "object":
        properties = schema.get("properties", {})
        if len(properties) > MAX_ITEMS:
            raise ValueError("数据对象字段超过限制")
        return {name: sample_schema(child, rng, depth=depth + 1, root=root) for name, child in properties.items() if not isinstance(child, dict) or not child.get("readOnly")}
    if kind == "array":
        low, high = int(schema.get("minItems", 0)), int(schema.get("maxItems", max(3, schema.get("minItems", 0))))
        if not 0 <= low <= high <= MAX_ITEMS:
            raise ValueError("数据数组大小超出允许范围")
        values = []
        for index in range(rng.randint(low, high)):
            child = schema.get("prefixItems", [])[index] if index < len(schema.get("prefixItems", [])) else schema.get("items", {})
            for _ in range(MAX_ATTEMPTS):
                value = sample_schema(child, rng, depth=depth + 1, root=root)
                if not schema.get("uniqueItems") or value not in values:
                    values.append(value)
                    break
            else:
                raise ValueError("无法满足 uniqueItems")
        return values
    if kind in {"integer", "number"}:
        step = Decimal(str(schema.get("multipleOf", 1 if kind == "integer" else "0.01")))
        if not step.is_finite() or step <= 0:
            raise ValueError("multipleOf 必须为有限正数")
        low = Decimal(str(schema.get("minimum", schema.get("exclusiveMinimum", 0))))
        high = Decimal(str(schema.get("maximum", schema.get("exclusiveMaximum", low + 100))))
        if not low.is_finite() or not high.is_finite():
            raise ValueError("生成范围必须有限")
        start = int((low / step).to_integral_value(rounding=ROUND_CEILING))
        stop = int((high / step).to_integral_value(rounding=ROUND_FLOOR))
        if "exclusiveMinimum" in schema and step * start <= Decimal(str(schema["exclusiveMinimum"])):
            start = int((Decimal(str(schema["exclusiveMinimum"])) / step).to_integral_value(rounding=ROUND_FLOOR)) + 1
        if "exclusiveMaximum" in schema and step * stop >= Decimal(str(schema["exclusiveMaximum"])):
            stop = int((Decimal(str(schema["exclusiveMaximum"])) / step).to_integral_value(rounding=ROUND_CEILING)) - 1
        if start > stop:
            raise ValueError("数值范围中不存在合法样本")
        value = step * rng.randint(start, stop)
        return int(value) if kind == "integer" else float(value)
    if kind == "boolean":
        return bool(rng.getrandbits(1))
    if kind == "null":
        return None
    if kind != "string":
        raise ValueError(f"不支持的生成类型：{kind}")
    low, high = int(schema.get("minLength", 1)), int(schema.get("maxLength", max(16, schema.get("minLength", 1))))
    if not 0 <= low <= high <= MAX_SIZE:
        raise ValueError("数据字符串长度超出允许范围")
    low, high = max(0, low - padding), high - padding
    if high < low:
        raise ValueError("prefix/suffix 超出字符串长度约束")
    if schema.get("pattern"):
        return regex_value(schema["pattern"], rng, high)
    token = uuid.UUID(int=rng.getrandbits(128), version=4)
    email = None
    if schema.get("format") == "email":
        # Reserve an explicit non-deliverable domain while respecting length limits.
        domain = "@example.invalid" if high >= 17 else "@x.invalid"
        if high <= len(domain):
            raise ValueError("email 长度不足以使用保留的 invalid 域名")
        length = rng.randint(max(1, low - len(domain)), min(64, high - len(domain)))
        email = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(length)) + domain
    formats = {"uuid": str(token), "email": email, "date": (date(2020, 1, 1) + timedelta(days=rng.randrange(3650))).isoformat(), "date-time": (datetime(2020, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=rng.randrange(315360000))).isoformat().replace("+00:00", "Z"), "ipv4": f"192.0.2.{rng.randrange(1, 255)}", "ipv6": f"2001:db8::{rng.randrange(1, 65535):x}", "hostname": f"u{token.hex}.example.invalid", "uri": f"https://example.invalid/{token.hex}"}
    return formats.get(schema.get("format"), "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(rng.randint(low, high))))


def generate_values(rules: dict, seed: Any, case_id: str, namespace: str = "") -> dict:
    if not isinstance(rules, dict) or len(rules) > MAX_ITEMS:
        raise ValueError("data_generation 必须是有界对象")
    values = {}
    for name, spec in sorted(rules.items()):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or name.startswith("_iatest_") or name in RESERVED:
            raise ValueError(f"生成变量名称非法或保留：{name}")
        schema = generation_schema(spec)
        unique = spec.get("unique", False)
        if unique and (not namespace or not isinstance(schema, dict) or schema.get("type", "string") != "string" or schema.get("const") is not None or "enum" in schema or schema.get("maxLength", 16) < 12):
            raise ValueError("unique 需要命名空间及足够大的字符串值域")
        digest = hashlib.sha256(json.dumps([GENERATOR_VERSION, str(seed), case_id, name, namespace if unique else ""], ensure_ascii=False).encode()).digest()
        rng = random.Random(int.from_bytes(digest, "big"))
        first_unique = None
        for _ in range(MAX_ATTEMPTS):
            value = sample_schema(schema, rng, padding=len(spec.get("prefix", "") + spec.get("suffix", "")))
            if isinstance(value, str):
                charset = spec.get("charset")
                if charset:
                    if not isinstance(charset, str) or len(charset) > 256:
                        raise ValueError("charset 必须是有界字符串")
                    value = "".join(rng.choice(charset) for _ in value)
                value = str(spec.get("prefix", "")) + value + str(spec.get("suffix", ""))
                if len(value) > MAX_SIZE:
                    raise ValueError("生成字符串超过大小限制")
            if not schema_errors(value, schema):
                if unique:
                    if first_unique is None:
                        first_unique = value
                        continue
                    if value == first_unique:
                        continue
                    value = first_unique
                values[name] = value
                break
        else:
            raise ValueError(f"变量 {name} 无法生成满足 Schema 的数据，请提供数据集或修改规则")
    if len(json.dumps(values, ensure_ascii=False).encode()) > 1_000_000:
        raise ValueError("生成数据超过 1 MB 限制")
    return values
