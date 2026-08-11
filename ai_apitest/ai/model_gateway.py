from __future__ import annotations

import json
from typing import Any, Callable, Protocol

from ai_apitest.ai.provider import ModelResponse, ModelToolCall, model_completion


class ModelGatewayProtocol(Protocol):
    model_config: dict[str, Any]

    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        timeout: int,
        max_tokens: int,
        stream_callback: Callable[[str], None] | None = None,
    ) -> ModelResponse: ...


class ProviderModelGateway:
    """Provider adapter used by AgentRuntime.

    Native tool calling is the default. Setting supports_tools=false enables the
    compatibility envelope for older OpenAI-compatible endpoints.
    """

    def __init__(self, model_config: dict[str, Any]) -> None:
        self.model_config = dict(model_config)

    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        timeout: int,
        max_tokens: int,
        stream_callback: Callable[[str], None] | None = None,
    ) -> ModelResponse:
        supports_tools = bool(self.model_config.get("supports_tools", True))
        if supports_tools:
            return model_completion(
                base_url=str(self.model_config.get("base_url") or ""),
                model=str(self.model_config.get("model") or ""),
                api_key_env=str(self.model_config.get("api_key_env") or ""),
                provider=str(self.model_config.get("provider") or "openai-compatible"),
                messages=messages,
                tools=tools,
                tool_choice=self.model_config.get("tool_choice"),
                thinking=self.model_config.get("thinking"),
                temperature=self.model_config.get("temperature"),
                timeout=timeout,
                stream_callback=stream_callback,
                max_tokens=max_tokens,
            )
        return self._complete_with_envelope(
            messages=messages,
            tools=tools,
            timeout=timeout,
            max_tokens=max_tokens,
            stream_callback=stream_callback,
        )

    def _complete_with_envelope(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        timeout: int,
        max_tokens: int,
        stream_callback: Callable[[str], None] | None,
    ) -> ModelResponse:
        definitions = [item.get("function") or item for item in tools]
        instruction = (
            "当前模型端点不支持原生工具协议。需要调用工具时，只输出一个 JSON 对象："
            '{"tool":"工具名","arguments":{...}}。不要输出 Markdown 或解释。'
            "一次只能调用一个工具。可用工具定义：\n"
            + json.dumps(definitions, ensure_ascii=False, separators=(",", ":"))
        )
        compatible_messages = envelope_messages(messages)
        compatible_messages.insert(0, {"role": "system", "content": instruction})
        response = model_completion(
            base_url=str(self.model_config.get("base_url") or ""),
            model=str(self.model_config.get("model") or ""),
            api_key_env=str(self.model_config.get("api_key_env") or ""),
            provider=str(self.model_config.get("provider") or "openai-compatible"),
            messages=compatible_messages,
            tools=[],
            thinking=self.model_config.get("thinking"),
            temperature=self.model_config.get("temperature"),
            timeout=timeout,
            stream_callback=stream_callback,
            max_tokens=max_tokens,
        )
        call = parse_tool_envelope(response.text)
        if call is not None:
            response.tool_calls = [call]
            response.finish_reason = response.finish_reason or "tool_call"
        return response


def parse_tool_envelope(text: str) -> ModelToolCall | None:
    value = str(text or "").strip()
    if value.startswith("```"):
        value = value.removeprefix("```json").removeprefix("```").strip()
        if value.endswith("```"):
            value = value[:-3].strip()
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        start = value.find("{")
        end = value.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(value[start : end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    envelope = data.get("tool_call") if isinstance(data.get("tool_call"), dict) else data
    name = str(envelope.get("tool") or envelope.get("name") or "").strip()
    arguments = envelope.get("arguments")
    if not name or not isinstance(arguments, dict):
        return None
    return ModelToolCall(
        id="compat-tool-1",
        name=name,
        arguments=arguments,
        raw_arguments=json.dumps(arguments, ensure_ascii=False),
    )


def envelope_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "tool":
            converted.append(
                {
                    "role": "user",
                    "content": f"工具 {message.get('name') or message.get('tool_call_id')} 返回：{message.get('content') or ''}",
                }
            )
            continue
        content = str(message.get("content") or "")
        if role == "assistant" and message.get("tool_calls"):
            content += "\n已请求工具：" + json.dumps(message["tool_calls"], ensure_ascii=False, separators=(",", ":"))
        converted.append({"role": role, "content": content})
    return converted
