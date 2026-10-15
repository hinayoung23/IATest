from __future__ import annotations

import json
import hashlib
import math
import time
from contextlib import nullcontext
from dataclasses import replace
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ai_apitest.data.repositories import ProjectDataAccess


ToolHandler = Callable[[dict[str, Any], "ToolExecutionContext"], Any]


class ToolPermissionError(PermissionError):
    pass


class ToolInputError(ValueError):
    pass


class ToolOutputError(ValueError):
    pass


class ToolTimeoutError(ValueError):
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
    output_schema: Any = None
    timeout_seconds: float | None = None
    max_calls: int | None = None

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
        from ai_apitest.core.schema import check_schema
        check_schema(spec.input_schema)
        if spec.output_schema is not None:
            check_schema(spec.output_schema)
        if spec.timeout_seconds is not None and (type(spec.timeout_seconds) not in (int, float) or spec.timeout_seconds <= 0 or not math.isfinite(spec.timeout_seconds)):
            raise ValueError("工具时限必须为有限正数")
        if spec.max_calls is not None and (type(spec.max_calls) is not int or spec.max_calls < 1):
            raise ValueError("工具调用预算必须为正整数")
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
        counts = context.state.setdefault("_tool_call_counts", {})
        counts[name] = counts.get(name, 0) + 1
        if spec.max_calls is not None and counts[name] > spec.max_calls:
            raise ToolInputError("工具调用次数超出预算")
        from ai_apitest.execution.control import ACTIVE_RUNTIME, ExecutionRuntime, RunControl, RunCancelled, activate, check_control
        runtime = ACTIVE_RUNTIME.get()
        manager = nullcontext()
        if spec.timeout_seconds is not None:
            remaining = runtime.control.remaining() if runtime else None
            timeout = min(spec.timeout_seconds, remaining) if remaining is not None else spec.timeout_seconds
            control = RunControl(timeout_seconds=timeout, request_parent=runtime.control if runtime else None, cancellation_check=(lambda: bool(runtime.control.reason())) if runtime else None)
            manager = activate(replace(runtime, control=control) if runtime else ExecutionRuntime(control=control))
        started = time.monotonic()
        try:
            with manager:
                check_control()
                value = spec.handler(arguments, context)
        except RunCancelled as exc:
            if spec.timeout_seconds is not None and (not runtime or not runtime.control.reason()):
                raise ToolTimeoutError("工具执行超过协作时限；不能确认其副作用完成状态") from exc
            raise
        if spec.timeout_seconds is not None and time.monotonic() - started >= spec.timeout_seconds:
            raise ToolTimeoutError("工具执行超过协作时限；不能确认其副作用完成状态")
        check_control()
        if isinstance(value, ToolExecutionResult):
            result = value
        else:
            content = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            result = ToolExecutionResult(content=content, value=value, terminal=spec.terminal)
        if not result.is_error and spec.output_schema is not None:
            from ai_apitest.core.schema import schema_errors
            if errors := schema_errors(result.value, spec.output_schema):
                raise ToolOutputError("工具结果不符合契约：" + "; ".join(errors))
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
    """Validate complete local JSON Schema; runtime refs never access the network."""
    from ai_apitest.core.schema import schema_errors
    if errors := schema_errors(value, schema):
        raise ToolInputError(path + ": " + "; ".join(errors))
