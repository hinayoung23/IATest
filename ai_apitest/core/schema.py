"""Validate documented schemas without downloading references during a test."""
from __future__ import annotations

import copy
from functools import lru_cache
from itertools import islice
import json
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.validators import validator_for
from referencing import Registry
from referencing.exceptions import NoSuchResource


def normalize_schema(schema: Any, *, dialect: str = "") -> Any:
    """Translate OpenAPI 3.0 nullable/exclusive bounds to JSON Schema syntax."""
    if isinstance(schema, list):
        return [normalize_schema(item, dialect=dialect) for item in schema]
    if not isinstance(schema, dict):
        return schema
    dialect = schema.get("$schema", dialect)
    result = copy.deepcopy(schema)
    result.pop("nullable", None)
    for key in ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas"):
        if isinstance(result.get(key), dict):
            result[key] = {name: normalize_schema(child, dialect=dialect) for name, child in result[key].items()}
    for key in ("allOf", "anyOf", "oneOf", "prefixItems", "items", "additionalProperties", "additionalItems", "unevaluatedProperties", "unevaluatedItems", "contains", "propertyNames", "not", "if", "then", "else"):
        if key in result:
            result[key] = normalize_schema(result[key], dialect=dialect)
    for bound in ("Minimum", "Maximum"):
        key = "exclusive" + bound
        if isinstance(result.get(key), bool) and "draft-04" not in dialect:
            enabled = result.pop(key)
            if enabled and bound.lower() in result:
                result[key] = result.pop(bound.lower())
    if schema.get("nullable"):
        anchors = {key: result.pop(key) for key in ("$defs", "definitions", "$schema", "$id") if key in result}
        result = {**anchors, "anyOf": [result, {"type": "null"}]}
    return result


def _no_remote_reference(uri: str):
    raise NoSuchResource(ref=uri)


@lru_cache(maxsize=256)
def _validator(encoded: str):
    schema = json.loads(encoded)
    dialect = schema.get("$schema") if isinstance(schema, dict) else None
    cls = validator_for(schema, default=None) if dialect else Draft202012Validator
    if cls is None:
        raise ValueError(f"不支持的 JSON Schema dialect：{dialect}")
    cls.check_schema(schema)
    return cls(schema, format_checker=FormatChecker(), registry=Registry(retrieve=_no_remote_reference))


def schema_errors(value: Any, schema: Any, *, limit: int = 10) -> list[str]:
    if not isinstance(schema, (dict, bool)):
        return ["JSON Schema 必须是对象或布尔值"]
    try:
        normalized = normalize_schema(copy.deepcopy(schema))
        validator = _validator(json.dumps(normalized, sort_keys=True, ensure_ascii=False))
        return [
            f"$" + "".join(f"[{key}]" if isinstance(key, int) else f".{key}" for key in error.absolute_path) + f": {error.message}"
            for error in islice(validator.iter_errors(value), limit)
        ]
    except Exception as exc:
        return [f"Schema 无法校验：{exc}"]


def check_schema(schema: Any) -> None:
    if not isinstance(schema, (dict, bool)):
        raise ValueError("JSON Schema 必须是对象或布尔值")
    _validator(json.dumps(normalize_schema(schema), sort_keys=True, ensure_ascii=False))
