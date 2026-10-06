from __future__ import annotations

from typing import Any

import httpx

from ai_apitest.core.sandbox import ACTIVE_POLICY, check_url


def fetch_text_limited(
    source: str,
    *,
    headers: dict[str, str] | None = None,
    max_bytes: int,
    timeout: float = 20,
) -> str:
    """Download text without buffering an unbounded response body."""
    last_error: Exception | None = None
    check_url(source)
    policy = ACTIVE_POLICY.get()
    for trust_env in ((False,) if policy and policy.mode != "off" else (True, False)):
        try:
            hooks = {"request": [policy.request_hook]} if policy and policy.mode != "off" else None
            with httpx.Client(timeout=timeout, trust_env=trust_env, event_hooks=hooks) as client:
                with client.stream("GET", source, headers=headers or {}) as response:
                    response.raise_for_status()
                    declared = response.headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > max_bytes:
                        raise ValueError(f"远程文档超过 {max_bytes} 字节限制：{source}")
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        if len(content) + len(chunk) > max_bytes:
                            raise ValueError(f"远程文档超过 {max_bytes} 字节限制：{source}")
                        content.extend(chunk)
                    encoding = response.encoding or "utf-8"
                    try:
                        return bytes(content).decode(encoding)
                    except (LookupError, UnicodeDecodeError) as exc:
                        raise ValueError(f"远程文档编码无法解析：{source}") from exc
        except (httpx.ConnectError, httpx.ProxyError) as exc:
            last_error = exc
            if not trust_env:
                raise
    assert last_error is not None
    raise last_error
