from __future__ import annotations

from dataclasses import dataclass
import threading
import json
import time
from typing import Any
from urllib.parse import urljoin

from ai_apitest.core.sandbox import check_url

import httpx

from ai_apitest.execution.control import ACTIVE_RUNTIME, check_control
from ai_apitest.execution.assertions import json_path


@dataclass
class Token:
    value: str
    expires_at: float
    refresh_token: str = ""


class AuthManager:
    def __init__(self, environment: dict[str, Any]) -> None:
        self.environment = environment
        self.config = environment.get("auth", {})
        self._tokens: dict[str, Token] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()

    def identity(self, name: str = "") -> tuple[str, dict]:
        name = name or str(self.config.get("default_identity", ""))
        if not name:
            return "", {}
        identities = self.config.get("identities", {})
        if name not in identities or not isinstance(identities[name], dict):
            raise ValueError(f"鉴权身份未配置：{name}")
        return name, identities[name]

    def token(self, client: httpx.Client, name: str = "", *, invalidate: bool = False, stale_value: str = "") -> str:
        name, config = self.identity(name)
        if not config:
            return ""
        if config.get("type", "bearer") == "bearer":
            value = str(config.get("token") or "")
            if not value:
                raise ValueError(f"鉴权身份 {name} 缺少 token")
            return value
        if config.get("type") != "oauth2":
            raise ValueError("鉴权身份仅支持 bearer 或 oauth2")
        with self._lock:
            lock = self._locks.setdefault(name, threading.Lock())
        while not lock.acquire(timeout=0.1):
            check_control()
        try:
            check_control()
            current = self._tokens.get(name)
            if current and current.expires_at > time.monotonic() and (not invalidate or (stale_value and stale_value != current.value)):
                return current.value
            url = urljoin(str(self.environment["base_url"]).rstrip("/") + "/", str(config.get("token_url") or ""))
            if not config.get("token_url") or not url.startswith(("http://", "https://")):
                raise ValueError("OAuth2 必须配置 token_url")
            data = dict(config.get("data", {}))
            for key in ("client_id", "client_secret", "scope"):
                if key in config:
                    data[key] = config[key]
            if current and current.refresh_token:
                data.update(grant_type="refresh_token", refresh_token=current.refresh_token)
            else:
                data.setdefault("grant_type", config.get("grant_type", "client_credentials"))
            timeout = float(config.get("timeout_seconds", 10))
            if runtime := ACTIVE_RUNTIME.get():
                remaining = runtime.control.remaining()
                if remaining is not None:
                    timeout = min(timeout, remaining)
            from ai_apitest.execution.runner import read_response_content
            check_url(url)
            with client.stream("POST", url, data=data, headers=config.get("headers", {}), timeout=timeout) as response:
                response.raise_for_status()
                content, truncated = read_response_content(response, 1000000)
            if truncated:
                raise ValueError("OAuth2 响应超过大小限制")
            body = json.loads(content)
            value = json_path(body, str(config.get("token_path", "$.access_token")))
            if not isinstance(value, str) or not value:
                raise ValueError("OAuth2 响应缺少有效 access_token")
            expires = json_path(body, str(config.get("expires_path", "$.expires_in")))
            ttl = float(config.get("default_ttl_seconds", 300) if expires is None else expires)
            margin = min(float(config.get("refresh_margin_seconds", 30)), max(0, ttl / 2))
            self._tokens[name] = Token(value, time.monotonic() + max(0, ttl - margin), str(body.get("refresh_token") or (current.refresh_token if current else "")))
            return value
        finally:
            lock.release()
