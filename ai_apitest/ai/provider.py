from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from contextvars import ContextVar
from typing import Any, Callable

import httpx

DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS = 180
MODEL_HTTP_ATTEMPTS = ContextVar("iatest_model_http_attempts", default=0)


def usage_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (ValueError, TypeError, OverflowError):
        return 0


def check_task_deadline() -> None:
    from ai_apitest.agents.policy import ACTIVE_TASK_BUDGET
    if budget := ACTIVE_TASK_BUDGET.get():
        budget.check()


@dataclass(slots=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    estimated: bool = False
    raw_usage: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_openai(cls, value: dict[str, Any] | None) -> "TokenUsage":
        value = dict(value) if isinstance(value, dict) else {}
        input_tokens = usage_count(value.get("prompt_tokens") or value.get("input_tokens"))
        output_tokens = usage_count(value.get("completion_tokens") or value.get("output_tokens"))
        details = value.get("prompt_tokens_details") or value.get("input_tokens_details") or {}
        output_details = value.get("completion_tokens_details") or value.get("output_tokens_details") or {}
        details = details if isinstance(details, dict) else {}
        output_details = output_details if isinstance(output_details, dict) else {}
        return cls(input_tokens, output_tokens, usage_count(value.get("total_tokens")) or input_tokens + output_tokens, usage_count(details.get("cached_tokens") or value.get("cache_read_input_tokens")), usage_count(details.get("cache_write_tokens") or value.get("cache_creation_input_tokens")), usage_count(output_details.get("reasoning_tokens")), raw_usage=value)

    @classmethod
    def from_anthropic(cls, value: dict[str, Any] | None) -> "TokenUsage":
        value = dict(value) if isinstance(value, dict) else {}
        input_tokens = usage_count(value.get("input_tokens"))
        output_tokens = usage_count(value.get("output_tokens"))
        cached = usage_count(value.get("cache_read_input_tokens"))
        written = usage_count(value.get("cache_creation_input_tokens"))
        # Messages input_tokens excludes cache reads/writes; input_tokens here
        # always means the complete context, with the subsets recorded separately.
        input_tokens += cached + written
        return cls(input_tokens, output_tokens, input_tokens + output_tokens, cached, written, usage_count(value.get("reasoning_tokens")), raw_usage=value)

    def add(self, other: "TokenUsage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.total_tokens += other.total_tokens
        self.cached_input_tokens += other.cached_input_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.reasoning_tokens += other.reasoning_tokens
        self.estimated = self.estimated or other.estimated


@dataclass(slots=True)
class ModelToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""
    parse_error: str = ""
    original_raw_arguments: str = ""
    repair_warning: str = ""

    def to_openai_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": self.raw_arguments or json.dumps(self.arguments, ensure_ascii=False),
            },
        }


@dataclass(slots=True)
class ModelResponse:
    text: str = ""
    tool_calls: list[ModelToolCall] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    finish_reason: str = ""
    provider_metadata: dict[str, Any] = field(default_factory=dict)

    def assistant_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.text or ""}
        if self.tool_calls:
            message["tool_calls"] = [item.to_openai_dict() for item in self.tool_calls]
        return message


def chat_completion(
    *,
    base_url: str,
    model: str,
    api_key_env: str,
    messages: list[dict[str, str]],
    provider: str = "openai-compatible",
    temperature: float | None = None,
    timeout: int | None = 60,
    stream_callback: Callable[[str], None] | None = None,
    max_tokens: int | None = None,
) -> str:
    api_key = resolve_api_key(api_key_env)
    _debug_llm_log(messages, model, provider)
    if provider == "anthropic":
        result = anthropic_chat_completion(
            base_url=base_url, model=model, api_key=api_key, messages=messages,
            temperature=temperature, timeout=timeout, stream_callback=stream_callback,
            max_tokens=max_tokens,
        )
    elif provider == "openai-compatible":
        result = openai_compatible_chat_completion(
            base_url=base_url, model=model, api_key=api_key, messages=messages,
            temperature=temperature, timeout=timeout, stream_callback=stream_callback,
            max_tokens=max_tokens,
        )
    else:
        raise ValueError(f"不支持的大模型 Provider: {provider}")
    _debug_llm_log_response(result)
    return result


def model_completion(
    *,
    base_url: str,
    model: str,
    api_key_env: str,
    messages: list[dict[str, Any]],
    provider: str = "openai-compatible",
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
    thinking: dict[str, Any] | None = None,
    temperature: float | None = None,
    timeout: int | None = 60,
    stream_callback: Callable[[str], None] | None = None,
    max_tokens: int | None = None,
) -> ModelResponse:
    """Provider-neutral response with text, tool calls, finish reason and token usage."""
    api_key = resolve_api_key(api_key_env)
    if provider == "anthropic":
        return anthropic_model_completion(
            base_url=base_url,
            model=model,
            api_key=api_key,
            messages=messages,
            tools=tools or [],
            tool_choice=tool_choice,
            temperature=temperature,
            timeout=timeout,
            stream_callback=stream_callback,
            max_tokens=max_tokens,
        )
    if provider == "openai-compatible":
        return openai_compatible_model_completion(
            base_url=base_url,
            model=model,
            api_key=api_key,
            messages=messages,
            tools=tools or [],
            tool_choice=tool_choice,
            thinking=thinking,
            temperature=temperature,
            timeout=timeout,
            stream_callback=stream_callback,
            max_tokens=max_tokens,
        )
    raise ValueError(f"不支持的大模型 Provider: {provider}")


def openai_compatible_model_completion(
    *,
    base_url: str,
    model: str,
    api_key: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    tool_choice: str | dict[str, Any] | None,
    thinking: dict[str, Any] | None,
    temperature: float | None,
    timeout: int | None,
    stream_callback: Callable[[str], None] | None,
    max_tokens: int | None,
) -> ModelResponse:
    endpoint = base_url.rstrip("/") + "/chat/completions"
    request_body: dict[str, Any] = {"model": model, "messages": messages}
    if tools:
        request_body["tools"] = tools
        request_body["tool_choice"] = tool_choice or "auto"
    if thinking:
        request_body["thinking"] = thinking
    if stream_callback:
        request_body["stream"] = True
    if temperature is not None:
        request_body["temperature"] = temperature
    if max_tokens is not None:
        request_body["max_tokens"] = max_tokens
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        if stream_callback:
            with httpx.Client(timeout=timeout) as client:
                MODEL_HTTP_ATTEMPTS.set(MODEL_HTTP_ATTEMPTS.get() + 1)
                with client.stream("POST", endpoint, json=request_body, headers=headers) as response:
                    response.raise_for_status()
                    return read_openai_model_stream(response.iter_lines(), stream_callback)
        else:
            data = post_json(endpoint, request_body, headers=headers, timeout=timeout)
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(http_status_error_message(exc)) from exc
    except httpx.TimeoutException as exc:
        raise RuntimeError(timeout_message(timeout)) from exc
    raise_if_api_error(data)
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("LLM 响应中没有 choices 字段")
    choice = choices[0]
    message = choice.get("message") or {}
    return ModelResponse(
        text=model_message_text(message.get("content")),
        tool_calls=parse_openai_tool_calls(message.get("tool_calls") or []),
        usage=TokenUsage.from_openai(data.get("usage")),
        finish_reason=str(choice.get("finish_reason") or ""),
        provider_metadata={"id": data.get("id"), "created": data.get("created")},
    )


def anthropic_model_completion(
    *,
    base_url: str,
    model: str,
    api_key: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    tool_choice: str | dict[str, Any] | None,
    temperature: float | None,
    timeout: int | None,
    stream_callback: Callable[[str], None] | None,
    max_tokens: int | None,
) -> ModelResponse:
    system, anthropic_messages = to_anthropic_messages(messages)
    request_body: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens or 4096,
        "messages": anthropic_messages,
    }
    if system:
        request_body["system"] = system
    if tools:
        request_body["tools"] = [openai_tool_to_anthropic(item) for item in tools]
        if tool_choice and tool_choice != "auto":
            request_body["tool_choice"] = anthropic_tool_choice(tool_choice)
    if temperature is not None:
        request_body["temperature"] = temperature
    if stream_callback:
        request_body["stream"] = True
    errors: list[str] = []
    for endpoint in anthropic_endpoint_candidates(base_url):
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
        try:
            if stream_callback:
                return post_anthropic_model_stream(endpoint, request_body, headers, stream_callback, timeout=timeout)
            data = post_json(endpoint, request_body, headers=headers, timeout=timeout)
            raise_if_api_error(data)
        except RuntimeError as exc:
            errors.append(str(exc))
            if is_not_found_error(exc):
                continue
            raise
        blocks = data.get("content") or []
        return ModelResponse(
            text="\n".join(str(item.get("text") or "") for item in blocks if item.get("type") == "text").strip(),
            tool_calls=parse_anthropic_tool_calls(blocks),
            usage=TokenUsage.from_anthropic(data.get("usage")),
            finish_reason=str(data.get("stop_reason") or ""),
            provider_metadata={"id": data.get("id"), "stop_sequence": data.get("stop_sequence")},
        )
    raise RuntimeError(errors[-1] if errors else "LLM 请求失败")


def read_openai_model_stream(response, stream_callback: Callable[[str], None]) -> ModelResponse:
    text_parts: list[str] = []
    calls: dict[int, dict[str, str]] = {}
    usage = TokenUsage()
    finish_reason = ""
    stream_callback("已连接到大模型服务，正在等待模型返回内容...\n")
    for raw_line in response:
        check_task_deadline()
        line = raw_line.decode("utf-8", errors="replace").strip() if isinstance(raw_line, bytes) else str(raw_line).strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if line == "[DONE]":
            break
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        raise_if_api_error(data)
        if data.get("usage"):
            usage = TokenUsage.from_openai(data["usage"])
        for choice in data.get("choices") or []:
            finish_reason = str(choice.get("finish_reason") or finish_reason)
            delta = choice.get("delta") or {}
            visible = stream_text_delta(delta)
            if visible:
                stream_callback(visible)
            content = delta.get("content")
            if isinstance(content, str) and content:
                text_parts.append(content)
            for raw_call in delta.get("tool_calls") or []:
                index = int(raw_call.get("index") or 0)
                target = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                target["id"] += str(raw_call.get("id") or "")
                function = raw_call.get("function") or {}
                target["name"] += str(function.get("name") or "")
                target["arguments"] += str(function.get("arguments") or "")
    parsed_calls = [tool_call_from_parts(item["id"], item["name"], item["arguments"], index) for index, item in sorted(calls.items())]
    return ModelResponse("".join(text_parts), parsed_calls, usage, finish_reason)


def post_anthropic_model_stream(
    endpoint: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    stream_callback: Callable[[str], None],
    timeout: int | None,
) -> ModelResponse:
    text_parts: list[str] = []
    calls: dict[int, dict[str, str]] = {}
    usage = TokenUsage()
    finish_reason = ""
    try:
        with httpx.Client(timeout=timeout) as client:
            MODEL_HTTP_ATTEMPTS.set(MODEL_HTTP_ATTEMPTS.get() + 1)
            with client.stream("POST", endpoint, json=payload, headers=headers) as response:
                response.raise_for_status()
                stream_callback("已连接到大模型服务，正在等待模型返回内容...\n")
                for raw_line in response.iter_lines():
                    check_task_deadline()
                    line = raw_line.decode("utf-8", errors="replace").strip() if isinstance(raw_line, bytes) else str(raw_line).strip()
                    if not line or not line.startswith("data:"):
                        continue
                    try:
                        data = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    event_type = data.get("type")
                    if event_type == "error":
                        raise RuntimeError(f"LLM 请求失败: {data.get('error') or data}")
                    if event_type == "message_start":
                        usage = TokenUsage.from_anthropic((data.get("message") or {}).get("usage"))
                    elif event_type == "content_block_start":
                        index = int(data.get("index") or 0)
                        block = data.get("content_block") or {}
                        if block.get("type") == "tool_use":
                            calls[index] = {
                                "id": str(block.get("id") or ""),
                                "name": str(block.get("name") or ""),
                                "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False) if block.get("input") else "",
                            }
                        elif block.get("type") == "text" and block.get("text"):
                            text_parts.append(str(block["text"]))
                    elif event_type == "content_block_delta":
                        index = int(data.get("index") or 0)
                        delta = data.get("delta") or {}
                        visible = anthropic_stream_text_delta(delta)
                        if visible:
                            stream_callback(visible)
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            text_parts.append(str(delta["text"]))
                        elif delta.get("type") == "input_json_delta":
                            calls.setdefault(index, {"id": "", "name": "", "arguments": ""})["arguments"] += str(delta.get("partial_json") or "")
                    elif event_type == "message_delta":
                        delta = data.get("delta") or {}
                        finish_reason = str(delta.get("stop_reason") or finish_reason)
                        output_tokens = int((data.get("usage") or {}).get("output_tokens") or 0)
                        if output_tokens:
                            usage.output_tokens = output_tokens
                            usage.total_tokens = usage.input_tokens + output_tokens
                            usage.raw_usage.update(data.get("usage") or {})
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(http_status_error_message(exc)) from exc
    except httpx.TimeoutException as exc:
        raise RuntimeError(timeout_message(timeout)) from exc
    parsed_calls = [tool_call_from_parts(item["id"], item["name"], item["arguments"], index) for index, item in sorted(calls.items())]
    return ModelResponse("".join(text_parts), parsed_calls, usage, finish_reason)


def parse_openai_tool_calls(values: list[dict[str, Any]]) -> list[ModelToolCall]:
    result = []
    for index, value in enumerate(values):
        function = value.get("function") or {}
        result.append(tool_call_from_parts(str(value.get("id") or ""), str(function.get("name") or ""), function.get("arguments"), index))
    return result


def parse_anthropic_tool_calls(blocks: list[dict[str, Any]]) -> list[ModelToolCall]:
    result = []
    for index, block in enumerate(blocks):
        if block.get("type") != "tool_use":
            continue
        raw_input = block.get("input") or {}
        if isinstance(raw_input, dict):
            result.append(
                ModelToolCall(
                    id=str(block.get("id") or f"tool-{index}"),
                    name=str(block.get("name") or ""),
                    arguments=raw_input,
                    raw_arguments=json.dumps(raw_input, ensure_ascii=False),
                )
            )
        else:
            result.append(tool_call_from_parts(str(block.get("id") or ""), str(block.get("name") or ""), raw_input, index))
    return result


def tool_call_from_parts(call_id: str, name: str, raw_arguments: Any, index: int) -> ModelToolCall:
    if isinstance(raw_arguments, dict):
        return ModelToolCall(call_id or f"tool-{index}", name, raw_arguments, json.dumps(raw_arguments, ensure_ascii=False))
    raw = str(raw_arguments or "{}")
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("工具参数必须是 JSON 对象")
        return ModelToolCall(call_id or f"tool-{index}", name, parsed, raw)
    except (json.JSONDecodeError, ValueError) as exc:
        try:
            from json_repair import loads as repair_json_loads

            repaired = repair_json_loads(raw, skip_json_loads=True)
            if not isinstance(repaired, dict):
                raise ValueError("修复后的工具参数不是 JSON 对象")
            repaired_raw = json.dumps(repaired, ensure_ascii=False, separators=(",", ":"))
            return ModelToolCall(
                call_id or f"tool-{index}",
                name,
                repaired,
                repaired_raw,
                "",
                raw,
                f"模型工具参数 JSON 已确定性修复：{exc}",
            )
        except Exception:
            return ModelToolCall(call_id or f"tool-{index}", name, {}, raw, str(exc))


def model_message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(item.get("text") or "") for item in content if isinstance(item, dict) and item.get("type") == "text").strip()
    return ""


def to_anthropic_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            system_parts.append(model_message_text(message.get("content")))
            continue
        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            text = model_message_text(message.get("content"))
            if text:
                blocks.append({"type": "text", "text": text})
            for raw_call in message.get("tool_calls") or []:
                function = raw_call.get("function") or {}
                call = tool_call_from_parts(str(raw_call.get("id") or ""), str(function.get("name") or ""), function.get("arguments"), len(blocks))
                blocks.append({"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments})
            converted.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
            continue
        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": str(message.get("tool_call_id") or ""),
                "content": str(message.get("content") or ""),
                "is_error": bool(message.get("is_error", False)),
            }
            if converted and converted[-1]["role"] == "user" and isinstance(converted[-1]["content"], list) and all(
                item.get("type") == "tool_result" for item in converted[-1]["content"]
            ):
                converted[-1]["content"].append(block)
            else:
                converted.append({"role": "user", "content": [block]})
            continue
        converted.append({"role": "user", "content": message.get("content") or ""})
    return "\n\n".join(part for part in system_parts if part), converted


def openai_tool_to_anthropic(tool: dict[str, Any]) -> dict[str, Any]:
    function = tool.get("function") or tool
    return {
        "name": str(function.get("name") or ""),
        "description": str(function.get("description") or ""),
        "input_schema": function.get("parameters") or {"type": "object", "properties": {}},
    }


def anthropic_tool_choice(value: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, dict):
        function = value.get("function") or value
        name = function.get("name")
        return {"type": "tool", "name": name} if name else {"type": "auto"}
    if value in {"required", "any"}:
        return {"type": "any"}
    return {"type": "auto"}


def openai_compatible_chat_completion(
    *,
    base_url: str,
    model: str,
    api_key: str,
    messages: list[dict[str, str]],
    temperature: float | None = None,
    timeout: int | None = 60,
    stream_callback: Callable[[str], None] | None = None,
    max_tokens: int | None = None,
) -> str:
    endpoint = base_url.rstrip("/") + "/chat/completions"
    request_body: dict[str, Any] = {
        "model": model,
        "messages": messages,
    }
    if stream_callback:
        request_body["stream"] = True
    if temperature is not None:
        request_body["temperature"] = temperature
    if max_tokens is not None:
        request_body["max_tokens"] = max_tokens
    headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
    }
    try:
        if stream_callback:
            with httpx.Client(timeout=timeout) as client:
                MODEL_HTTP_ATTEMPTS.set(MODEL_HTTP_ATTEMPTS.get() + 1)
                with client.stream("POST", endpoint, json=request_body, headers=headers) as response:
                    response.raise_for_status()
                    return read_openai_stream(response.iter_lines(), stream_callback)
        data = post_json(endpoint, request_body, headers=headers, timeout=timeout)
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(http_status_error_message(exc)) from exc
    except httpx.TimeoutException as exc:
        raise RuntimeError(timeout_message(timeout)) from exc
    raise_if_api_error(data)
    if not data.get("choices"):
        raise RuntimeError("LLM 响应中没有 choices 字段")
    return model_message_text(data["choices"][0].get("message", {}).get("content"))


def anthropic_chat_completion(
    *,
    base_url: str,
    model: str,
    api_key: str,
    messages: list[dict[str, str]],
    temperature: float | None = None,
    timeout: int | None = 60,
    stream_callback: Callable[[str], None] | None = None,
    max_tokens: int | None = None,
) -> str:
    system_messages = [item["content"] for item in messages if item.get("role") == "system"]
    user_messages = [item for item in messages if item.get("role") != "system"]
    request_body: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens or 4096,
        "messages": user_messages,
    }
    if stream_callback:
        request_body["stream"] = True
    if system_messages:
        request_body["system"] = "\n".join(system_messages)
    if temperature is not None:
        request_body["temperature"] = temperature
    errors: list[str] = []
    for endpoint in anthropic_endpoint_candidates(base_url):
        try:
            headers = {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            }
            if stream_callback:
                return post_anthropic_stream(endpoint, request_body, headers, stream_callback, timeout=timeout)
            data = post_json(endpoint, request_body, headers=headers, timeout=timeout)
            raise_if_api_error(data)
        except RuntimeError as exc:
            errors.append(str(exc))
            if is_not_found_error(exc):
                continue
            raise
        text_blocks = [item.get("text", "") for item in data.get("content", []) if item.get("type") == "text"]
        text = "\n".join(text_blocks).strip()
        if not text:
            raise RuntimeError("LLM 响应中没有可解析的 text content")
        return text
    raise RuntimeError(errors[-1] if errors else "LLM 请求失败")


def read_openai_stream(response, stream_callback: Callable[[str], None]) -> str:
    parts: list[str] = []
    stream_callback("已连接到大模型服务，正在等待模型返回内容...\n")
    for raw_line in response:
        check_task_deadline()
        line = raw_line.decode("utf-8", errors="replace").strip() if isinstance(raw_line, bytes) else str(raw_line).strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if line == "[DONE]":
            break
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        raise_if_api_error(data)
        for choice in data.get("choices", []):
            delta = choice.get("delta") or {}
            text = stream_text_delta(delta)
            if text:
                stream_callback(text)
            content = delta.get("content") or ""
            if content:
                parts.append(content)
    return "".join(parts)


def post_anthropic_stream(
    endpoint: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    stream_callback: Callable[[str], None],
    timeout: int | None = 60,
) -> str:
    parts: list[str] = []
    try:
        with httpx.Client(timeout=timeout) as client:
            MODEL_HTTP_ATTEMPTS.set(MODEL_HTTP_ATTEMPTS.get() + 1)
            with client.stream("POST", endpoint, json=payload, headers=headers) as response:
                response.raise_for_status()
                stream_callback("已连接到大模型服务，正在等待模型返回内容...\n")
                for raw_line in response.iter_lines():
                    check_task_deadline()
                    line = raw_line.decode("utf-8", errors="replace").strip() if isinstance(raw_line, bytes) else str(raw_line).strip()
                    if not line or not line.startswith("data:"):
                        continue
                    content = line[5:].strip()
                    try:
                        data = json.loads(content)
                    except json.JSONDecodeError:
                        continue
                    if data.get("type") == "error":
                        raise RuntimeError(f"LLM 请求失败: {data.get('error') or data}")
                    delta = data.get("delta") or {}
                    text = anthropic_stream_text_delta(delta)
                    if text:
                        stream_callback(text)
                    content = delta.get("text") or ""
                    if content:
                        parts.append(content)
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(http_status_error_message(exc)) from exc
    except httpx.TimeoutException as exc:
        raise RuntimeError(timeout_message(timeout)) from exc
    return "".join(parts)


def post_json(endpoint: str, payload: dict[str, Any], headers: dict[str, str], timeout: int | None = 60) -> dict[str, Any]:
    last_error: Exception | None = None
    with httpx.Client(timeout=timeout) as client:
        for attempt in range(3):
            check_task_deadline()
            try:
                MODEL_HTTP_ATTEMPTS.set(MODEL_HTTP_ATTEMPTS.get() + 1)
                response = client.post(endpoint, json=payload, headers=headers)
                response.raise_for_status()
                try:
                    data = response.json()
                except json.JSONDecodeError as exc:
                    raise RuntimeError("LLM 响应不是有效 JSON") from exc
                if not isinstance(data, dict):
                    raise RuntimeError("LLM 响应 JSON 根节点不是对象")
                return data
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if exc.response.status_code != 429 and exc.response.status_code < 500:
                    raise RuntimeError(http_status_error_message(exc)) from exc
            except (httpx.ConnectError, httpx.RemoteProtocolError) as exc:
                last_error = exc
            except httpx.TimeoutException as exc:
                raise RuntimeError(timeout_message(timeout)) from exc
            if attempt < 2:
                time.sleep(0.5 * (2**attempt))
    if isinstance(last_error, httpx.HTTPStatusError):
        raise RuntimeError(http_status_error_message(last_error)) from last_error
    raise RuntimeError(f"LLM 请求失败: {last_error}") from last_error


def timeout_message(timeout: int | None) -> str:
    if timeout is None:
        return "LLM 请求超时"
    return f"LLM 请求空闲超时：超过 {timeout} 秒未收到服务端数据；这不是总生成时长限制"


def http_status_error_message(exc: httpx.HTTPStatusError) -> str:
    try:
        detail = exc.response.text.strip()[:2000]
    except (httpx.ResponseNotRead, httpx.StreamClosed):
        detail = ""
    suffix = f" {detail}" if detail else ""
    return f"LLM 请求失败: HTTP {exc.response.status_code}{suffix}"


def stream_text_delta(delta: dict[str, Any]) -> str:
    for key in ("content", "reasoning_content", "reasoning", "thinking"):
        value = delta.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def anthropic_stream_text_delta(delta: dict[str, Any]) -> str:
    for key in ("text", "thinking"):
        value = delta.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def anthropic_endpoint_candidates(base_url: str) -> list[str]:
    stripped = base_url.rstrip("/")
    if stripped.endswith("/messages"):
        return [stripped]
    if stripped.endswith("/v1"):
        return [stripped + "/messages"]
    return [stripped + "/v1/messages", stripped + "/messages"]


def is_not_found_error(exc: RuntimeError) -> bool:
    message = str(exc)
    return "HTTP 404" in message


def raise_if_api_error(data: dict[str, Any]) -> None:
    if data.get("success") is False:
        raise RuntimeError(f"LLM 请求失败: {data.get('code', '')} {data.get('msg') or data.get('message') or data}")
    if data.get("error"):
        raise RuntimeError(f"LLM 请求失败: {data['error']}")
    if "code" in data and "msg" in data and "content" not in data and "choices" not in data:
        raise RuntimeError(f"LLM 请求失败: {data.get('code')} {data.get('msg')}")


def resolve_api_key(api_key_ref: str) -> str:
    api_key_ref = api_key_ref.strip()
    if not api_key_ref:
        raise ValueError("API Key 未配置")
    api_key = os.getenv(api_key_ref)
    if api_key:
        return api_key
    if looks_like_api_key(api_key_ref):
        return api_key_ref
    raise ValueError(f"环境变量 {api_key_ref} 未设置；如果要直接填写 API Key，请在配置中填写完整 Key")


def looks_like_api_key(value: str) -> bool:
    stripped = value.strip()
    if not stripped or any(char.isspace() for char in stripped):
        return False
    if stripped.startswith(("sk-", "sk_", "ak-", "ak_")):
        return True
    if "." in stripped or "-" in stripped:
        return len(stripped) >= 20
    if re.fullmatch(r"[A-Z_][A-Z0-9_]*", stripped):
        return False
    return len(stripped) >= 32


def llm_check(
    *,
    base_url: str,
    model: str,
    api_key_env: str,
    provider: str = "openai-compatible",
    temperature: float | None = None,
) -> str:
    return chat_completion(
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
        provider=provider,
        messages=[
            {"role": "system", "content": "你是连通性检查助手。"},
            {"role": "user", "content": "只回复 OK。"},
        ],
        temperature=temperature,
    ).strip()


def _looks_truncated(content: str) -> bool:
    """Heuristic: output may be truncated if it doesn't end cleanly."""
    stripped = content.rstrip()
    if not stripped:
        return True
    # Clean endings: ], }
    if stripped[-1] in ("]", "}"):
        return False
    # Suspicious endings: comma, unclosed bracket, mid-word
    if stripped[-1] in (",", "[", "{", ":", "\n"):
        return True
    # Doesn't end with a natural JSON terminator
    if stripped[-1] not in ('"', '0', '1', '2', '3', '4', '5', '6', '7', '8', '9', 'e', 'l', 's', 'n', 't', 'r', 'u', 'f', 'a'):
        return True
    return False


def retry_with_json_repair(
    content: str,
    *,
    parse_json,
    base_url: str,
    model: str,
    api_key_env: str,
    provider: str = "openai-compatible",
    max_attempts: int = 2,
    max_tokens: int | None = None,
    description: str = "",
) -> tuple[object, str]:
    """Generic JSON self-repair for all structured LLM outputs.

    Attempts to parse `content` with `parse_json(content) -> object`.
    On failure, asks the same model to repair the JSON, then retries.
    Returns (parsed_object, warning). Warning is empty on first-try success.
    Raises ValueError if all repair attempts are exhausted.

    `parse_json` should be a callable that takes a string and returns the
    parsed object (e.g. `extract_json_array` or `json.loads`). It should
    raise `json.JSONDecodeError` or `ValueError` on failure.
    """
    import json as _json

    truncated = _looks_truncated(content)

    # First attempt
    try:
        parsed = parse_json(content)
        return parsed, ""  # parse succeeded → not truncated
    except (_json.JSONDecodeError, ValueError) as exc:
        error = str(exc)
        if truncated:
            error = f"（疑似截断）{error}"

    # Repair loop
    for attempt in range(1, max_attempts + 1):
        try:
            repaired = repair_json_output(
                original=content, parse_error=error,
                base_url=base_url, model=model, api_key_env=api_key_env, provider=provider,
                max_tokens=max_tokens,
                description=description,
            )
            parsed = parse_json(repaired)
            hint = f"大模型输出格式异常，已自动修复并解析。" if attempt == 1 else f"大模型输出格式异常，自动修复 {attempt} 次后已解析。"
            return parsed, hint
        except (_json.JSONDecodeError, ValueError) as exc:
            error = str(exc)
        except Exception:
            break  # network error etc., don't retry

    raise ValueError(
        f"大模型输出格式异常，自动修复 {max_attempts} 次仍未成功。"
    )


def repair_json_output(
    original: str,
    parse_error: str,
    *,
    base_url: str,
    model: str,
    api_key_env: str,
    provider: str = "openai-compatible",
    max_tokens: int | None = None,
    description: str = "",
) -> str:
    """Ask the model to repair its own malformed JSON output (array, object, or embedded block).

    When the original output is ≤8000 chars, the full content is sent so the model
    has complete context. Above that threshold, only the ~2000 chars around the
    error location are sent, and the model is explicitly asked to output the
    *complete* repaired array (not just the fragment).
    """
    import re as _re
    REPAIR_FULL_THRESHOLD = 8000

    pos_match = _re.search(r'line (\d+) column (\d+)|char (\d+)', parse_error)
    if len(original) <= REPAIR_FULL_THRESHOLD:
        # Short enough — send complete original
        snippet = original
        truncated_hint = ""
    elif not pos_match:
        # Can't locate error — send first 4000 chars
        snippet = original[:4000]
        truncated_hint = "\n原始输出较长且无法定位错误位置，以上为前 4000 字符。请输出完整修复后的 JSON 数组。"
    else:
        char_pos = 0
        if pos_match.group(3):
            char_pos = int(pos_match.group(3))
        elif pos_match.group(1):
            line_num = int(pos_match.group(1))
            lines = original.splitlines()
            char_pos = sum(len(line) + 1 for line in lines[: line_num - 1]) + int(pos_match.group(2))
        # Show ~1000 chars around the error
        start = max(0, char_pos - 1000)
        end = min(len(original), char_pos + 1000)
        snippet = original[start:end]
        if start > 0:
            snippet = f"... (前略 {start} chars) ...\n{snippet}"
        if end < len(original):
            snippet = f"{snippet}\n... (后略 {len(original) - end} chars) ..."
        truncated_hint = "\n原始输出较长，以上为错误位置附近片段。请输出完整修复后的 JSON 数组。"

    repair_prompt = f"""你刚才输出的 JSON 无法通过解析。
请只修复 JSON 语法错误，不要新增、删除、重排或改写任何内容。
只输出修复后的合法 JSON{description}，不要解释，不要 Markdown 代码块。

解析错误：
{parse_error}

原始输出：
{snippet}{truncated_hint}"""

    return chat_completion(
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
        provider=provider,
        messages=[{"role": "user", "content": repair_prompt}],
        max_tokens=max_tokens,
    )


def extract_json_array(text: str) -> list[Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.removeprefix("```json").removeprefix("```").strip()
        if stripped.endswith("```"):
            stripped = stripped[:-3].strip()
    if not stripped.startswith("["):
        start = stripped.find("[")
        end = stripped.rfind("]")
        if start == -1 or end == -1:
            raise ValueError("LLM 输出中没有 JSON 数组")
        stripped = stripped[start : end + 1]
    data = json.loads(stripped)
    if not isinstance(data, list):
        raise ValueError("LLM 输出不是 JSON 数组")
    return data


# Backward-compatible alias
repair_json_array = repair_json_output


# ═══════════════════════════════════════════════════════════════
# LLM Debug Log — controlled by IATEST_DEBUG_LLM=1 env var
# ═══════════════════════════════════════════════════════════════

_DEBUG_LOG_PATH = "/tmp/iatest_llm_debug.log"
_DEBUG_CALL_SEQ = 0


def _debug_llm_log(messages: list[dict[str, str]], model: str, provider: str) -> None:
    global _DEBUG_CALL_SEQ
    if not os.getenv("IATEST_DEBUG_LLM"):
        return
    _DEBUG_CALL_SEQ += 1
    import datetime
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"\n{'='*80}\n")
        f.write(f"#{_DEBUG_CALL_SEQ} [{ts}] model={model} provider={provider}\n")
        f.write(f"{'='*80}\n")
        for i, msg in enumerate(messages):
            role = msg.get("role", "?")
            content = msg.get("content", "")
            f.write(f"\n--- [{role}] ({len(content)} chars) ---\n")
            f.write(content[:8000])
            if len(content) > 8000:
                f.write(f"\n... (truncated, total {len(content)} chars)")
        f.write(f"\n\n--- [waiting for response] ---\n")
        f.flush()


def _debug_llm_log_response(result: str) -> None:
    if not os.getenv("IATEST_DEBUG_LLM"):
        return
    with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"\n--- [response] ({len(result)} chars) ---\n")
        f.write(result[:12000])
        if len(result) > 12000:
            f.write(f"\n... (truncated, total {len(result)} chars)")
        f.write(f"\n{'='*80}\n")
        f.flush()
