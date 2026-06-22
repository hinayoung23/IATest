from __future__ import annotations

import json
import os
import re
from typing import Any, Callable

import httpx

DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS = 180


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
        with httpx.Client(timeout=timeout) as client:
            if stream_callback:
                with client.stream("POST", endpoint, json=request_body, headers=headers) as response:
                    response.raise_for_status()
                    return read_openai_stream(response.iter_lines(), stream_callback)
            response = client.post(endpoint, json=request_body, headers=headers)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"LLM 请求失败: HTTP {exc.response.status_code} {exc.response.text}") from exc
    except httpx.TimeoutException as exc:
        raise RuntimeError(timeout_message(timeout)) from exc
    raise_if_api_error(data)
    if not data.get("choices"):
        raise RuntimeError("LLM 响应中没有 choices 字段")
    return data["choices"][0]["message"]["content"]


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
            with client.stream("POST", endpoint, json=payload, headers=headers) as response:
                response.raise_for_status()
                stream_callback("已连接到大模型服务，正在等待模型返回内容...\n")
                for raw_line in response.iter_lines():
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
        raise RuntimeError(f"LLM 请求失败: HTTP {exc.response.status_code} {exc.response.text}") from exc
    except httpx.TimeoutException as exc:
        raise RuntimeError(timeout_message(timeout)) from exc
    return "".join(parts)


def post_json(endpoint: str, payload: dict[str, Any], headers: dict[str, str], timeout: int | None = 60) -> dict[str, Any]:
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(endpoint, json=payload, headers=headers)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"LLM 请求失败: HTTP {exc.response.status_code} {exc.response.text}") from exc
    except httpx.TimeoutException as exc:
        raise RuntimeError(timeout_message(timeout)) from exc


def timeout_message(timeout: int | None) -> str:
    if timeout is None:
        return "LLM 请求超时"
    return f"LLM 请求空闲超时：超过 {timeout} 秒未收到服务端数据；这不是总生成时长限制"


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
    return "404" in message and "NOT_FOUND" in message


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
# LLM Debug Log — controlled by AITEST_DEBUG_LLM=1 env var
# ═══════════════════════════════════════════════════════════════

_DEBUG_LOG_PATH = "/tmp/aitest_llm_debug.log"
_DEBUG_CALL_SEQ = 0


def _debug_llm_log(messages: list[dict[str, str]], model: str, provider: str) -> None:
    global _DEBUG_CALL_SEQ
    if not os.getenv("AITEST_DEBUG_LLM"):
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
    if not os.getenv("AITEST_DEBUG_LLM"):
        return
    with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"\n--- [response] ({len(result)} chars) ---\n")
        f.write(result[:12000])
        if len(result) > 12000:
            f.write(f"\n... (truncated, total {len(result)} chars)")
        f.write(f"\n{'='*80}\n")
        f.flush()
