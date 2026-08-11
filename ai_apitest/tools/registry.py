from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ai_apitest.data.repositories import ProjectDataAccess


ToolHandler = Callable[[dict[str, Any], "ToolExecutionContext"], Any]


class ToolPermissionError(PermissionError):
    pass


class ToolInputError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    allowed_roles: tuple[str, ...]
    side_effect: str = "read"
    idempotent: bool = True
    terminal: bool = False
    result_limit_chars: int = 24000
    version: str = "1.0"

    def provider_definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


@dataclass(slots=True)
class ToolExecutionContext:
    root: Path
    project: str
    data: ProjectDataAccess
    state: dict[str, Any] = field(default_factory=dict)
    run_id: str = ""
    evidence_reader: Callable[[str, int, int], dict[str, Any]] | None = None


@dataclass(slots=True)
class ToolExecutionResult:
    content: str
    value: Any = None
    is_error: bool = False
    terminal: bool = False
    truncated: bool = False


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if not spec.name or not spec.name.replace("_", "").isalnum():
            raise ValueError(f"非法工具名：{spec.name}")
        if spec.side_effect not in {"read", "transform", "write", "execute"}:
            raise ValueError(f"工具 {spec.name} 的副作用等级无效：{spec.side_effect}")
        if spec.name in self._tools:
            raise ValueError(f"工具重复注册：{spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"未知工具：{name}") from exc

    def definitions_for(self, role_id: str, allowed_tools: tuple[str, ...]) -> list[dict[str, Any]]:
        definitions = []
        for name in allowed_tools:
            spec = self.get(name)
            if role_id not in spec.allowed_roles:
                raise ToolPermissionError(f"角色 {role_id} 的身份定义与工具 {name} 权限不一致")
            definitions.append(spec.provider_definition())
        return definitions

    def execute(
        self,
        *,
        role_id: str,
        allowed_tools: tuple[str, ...],
        name: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        if name not in allowed_tools:
            raise ToolPermissionError(f"角色 {role_id} 无权调用工具 {name}")
        spec = self.get(name)
        if role_id not in spec.allowed_roles:
            raise ToolPermissionError(f"工具 {name} 不允许角色 {role_id} 调用")
        validate_json_schema(arguments, spec.input_schema, path="arguments")
        value = spec.handler(arguments, context)
        if isinstance(value, ToolExecutionResult):
            result = value
        else:
            content = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            result = ToolExecutionResult(content=content, value=value, terminal=spec.terminal)
        if len(result.content) > spec.result_limit_chars:
            result.content = bounded_tool_content(result.value, result.content, spec.result_limit_chars)
            result.truncated = True
        result.terminal = result.terminal or spec.terminal
        return result

    def all(self) -> list[ToolSpec]:
        return sorted(self._tools.values(), key=lambda item: item.name)


def bounded_tool_content(value: Any, original: str, limit: int) -> str:
    """Keep truncation valid JSON so a model never receives a broken document."""
    preview_value = compact_tool_value(value, depth=0) if value is not None else None
    envelope = {
        "truncated": True,
        "sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
        "preview": preview_value if preview_value is not None else original[: max(0, limit - 500)],
        "next_action": "缩小查询范围或使用分页参数继续读取",
    }
    text = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(text) <= limit:
        return text
    envelope["preview"] = str(envelope["preview"])[: max(0, limit - 500)]
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), default=str)


def compact_tool_value(value: Any, *, depth: int) -> Any:
    if depth >= 4:
        return str(value)[:500]
    if isinstance(value, dict):
        return {str(key): compact_tool_value(item, depth=depth + 1) for key, item in list(value.items())[:50]}
    if isinstance(value, list):
        return [compact_tool_value(item, depth=depth + 1) for item in value[:30]]
    if isinstance(value, str):
        return value[:2000]
    return value


def validate_json_schema(value: Any, schema: dict[str, Any], *, path: str) -> None:
    """Validate the JSON Schema subset used by IATest tool definitions."""
    any_of = schema.get("anyOf")
    if isinstance(any_of, list) and any_of:
        errors: list[str] = []
        for option in any_of:
            if not isinstance(option, dict):
                continue
            try:
                validate_json_schema(value, option, path=path)
                return
            except ToolInputError as exc:
                errors.append(str(exc))
        raise ToolInputError(f"{path} 不符合任一允许类型：{'；'.join(errors)}")
    expected_type = schema.get("type")
    if expected_type:
        valid = {
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "null": value is None,
        }.get(expected_type)
        if valid is False:
            raise ToolInputError(f"{path} 应为 {expected_type}")
    if "enum" in schema and value not in schema["enum"]:
        raise ToolInputError(f"{path} 必须是 {schema['enum']} 之一")
    if isinstance(value, dict):
        required = schema.get("required") or []
        missing = [name for name in required if name not in value]
        if missing:
            raise ToolInputError(f"{path} 缺少必填字段：{', '.join(missing)}")
        properties = schema.get("properties") or {}
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                raise ToolInputError(f"{path} 包含未知字段：{', '.join(extra)}")
        for name, item in value.items():
            if name in properties:
                validate_json_schema(item, properties[name], path=f"{path}.{name}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            raise ToolInputError(f"{path} 至少需要 {schema['minItems']} 项")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            raise ToolInputError(f"{path} 最多允许 {schema['maxItems']} 项")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                validate_json_schema(item, item_schema, path=f"{path}[{index}]")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            raise ToolInputError(f"{path} 长度不能小于 {schema['minLength']}")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            raise ToolInputError(f"{path} 长度不能大于 {schema['maxLength']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ToolInputError(f"{path} 不能小于 {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ToolInputError(f"{path} 不能大于 {schema['maximum']}")
