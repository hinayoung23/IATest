"""Bounded native HTTP operations shared by typed engines; no retry of writes."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from iatest.core.config import load_config
from iatest.core.sandbox import ACTIVE_POLICY, SandboxPolicy, origin
from iatest.core.schema import schema_errors
from iatest.execution.control import RunControl

REQUEST_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["url"],
    "properties": {
        "url": {"type": "string", "minLength": 1, "maxLength": 8192},
        "method": {
            "enum": ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
        },
        "headers": {
            "type": "object",
            "additionalProperties": {"type": "string", "maxLength": 16384},
        },
        "json": {},
        "expected_status": {
            "type": "array",
            "minItems": 1,
            "maxItems": 100,
            "items": {"type": "integer", "minimum": 100, "maximum": 599},
        },
        "timeout_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 30},
        "follow_redirects": {"type": "boolean"},
    },
}


def validate(value: dict, schema: dict) -> None:
    json.dumps(value, allow_nan=False)
    if errors := schema_errors(value, schema):
        raise ValueError("; ".join(errors))


class NetworkScope:
    def __init__(self, root: Path, project: str, origins: list[str]):
        if not origins or len(origins) > 32:
            raise ValueError("Declare 1–32 allowed_origins")
        self.origins = frozenset(origin(x) for x in origins)
        config = load_config(root)
        self.mode = config.get("sandbox", {}).get("mode", "policy")
        if self.mode in {"process", "container"}:
            raise ValueError(
                "Native multi-type engines currently support policy isolation; requested process/container isolation is unavailable"
            )
        self.policy = SandboxPolicy.from_config(root, config, project=project)
        self.parent = ACTIVE_POLICY.get()
        # An explicitly configured sandbox allowlist always narrows the plan.
        self.configured_origins = bool(config.get("sandbox", {}).get("allowed_origins"))

    def check(self, url: str) -> None:
        if origin(url) not in self.origins:
            raise PermissionError("Request outside plan allowed_origins")
        if self.configured_origins or self.policy.allowed_networks:
            from dataclasses import replace

            replace(
                self.policy,
                allowed_origins=self.policy.allowed_origins
                if self.configured_origins
                else self.origins,
                mode="policy",
            ).check_url(url)
        if self.parent:
            self.parent.check_url(url)

    def check_browser(self, url: str) -> None:
        parsed = urlsplit(url)
        self.check(urlunsplit(parsed._replace(fragment="")))


def resolve_secrets(value):
    """Credentials stay references in saved plans, never expand them for a model."""
    if isinstance(value, dict):
        if set(value) == {"$env"}:
            key = value["$env"]
            if not isinstance(key, str) or not key or key not in os.environ:
                raise ValueError(
                    "Referenced credential environment variable is missing"
                )
            return os.environ[key]
        return {k: resolve_secrets(v) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_secrets(v) for v in value]
    return value


class HTTPExecutor:
    def __init__(self, scope: NetworkScope, control: RunControl):
        self.scope, self.control = scope, control
        self.client = httpx.Client(trust_env=False, follow_redirects=False, timeout=10)

    def close(self):
        self.client.close()

    def request(
        self, spec: dict, *, headers: dict | None = None, max_bytes: int = 1048576
    ) -> dict:
        validate(spec, REQUEST_SCHEMA)
        url = spec["url"]
        method = spec.get("method", "GET")
        request_headers = {**spec.get("headers", {}), **(headers or {})}
        body = {"json": spec["json"]} if "json" in spec else {}
        if len(json.dumps(body, ensure_ascii=False).encode()) > 1048576:
            raise ValueError("Request body exceeds 1 MiB")
        started = time.monotonic()
        received = 0
        # Cookie jars are isolated per executor (user/identity), not shared globally.
        for redirect in range(6):
            self.control.check()
            self.scope.check(url)
            self.control.request()
            from iatest.execution import native_ipc

            if native_ipc.authorize_request is not None:
                try:
                    native_ipc.authorize_request(self.control)
                except BaseException:
                    # Authorization failed before HTTP dispatch: release only the
                    # worker-local precharge. The host keeps conservative grants.
                    from contextlib import ExitStack

                    chain, current = [], self.control
                    while current is not None:
                        if not current.request_parent:
                            chain.append(current)
                        current = (
                            getattr(current, "parent", None) or current.request_parent
                        )
                    with ExitStack() as locks:
                        for item in chain:
                            locks.enter_context(item.request_lock)
                        for item in chain:
                            item.requests_started -= 1
                    raise
            remaining = self.control.remaining()
            timeout = min(float(spec.get("timeout_seconds", 10)), remaining or 30)
            with self.client.stream(
                method, url, headers=request_headers, timeout=timeout, **body
            ) as response:
                content = bytearray()
                truncated = False
                for chunk in response.iter_bytes():
                    self.control.check()
                    received += len(chunk)
                    if len(content) + len(chunk) > max_bytes:
                        truncated = True
                        break
                    content.extend(chunk)
                status = response.status_code
                response_headers = dict(response.headers)
            if (
                spec.get("follow_redirects")
                and status in {301, 302, 303, 307, 308}
                and response_headers.get("location")
            ):
                if redirect == 5:
                    raise ValueError("Redirect limit exceeded")
                next_url = urljoin(url, response_headers["location"])
                self.scope.check(next_url)
                # Do not forward credentials across an origin boundary, even if both are authorized.
                if origin(next_url) != origin(url):
                    request_headers = {
                        k: v
                        for k, v in request_headers.items()
                        if k.lower()
                        not in {"authorization", "cookie", "proxy-authorization"}
                    }
                    self.client.cookies.clear()
                if status == 303 or (status in {301, 302} and method == "POST"):
                    method, body = "GET", {}
                url = next_url
                continue
            text = bytes(content).decode("utf-8", errors="replace")
            try:
                parsed = json.loads(text) if not truncated else None
                json_valid = not truncated
            except ValueError:
                parsed, json_valid = None, False
            return {
                "status_code": status,
                "http_version": response.http_version,
                "headers": response_headers,
                "json": parsed,
                "json_valid": json_valid,
                "text": text,
                "truncated": truncated,
                "elapsed_ms": (time.monotonic() - started) * 1000,
                "bytes": received,
                "ok": not truncated
                and status in spec.get("expected_status", list(range(200, 300))),
            }
        raise RuntimeError("Unreachable redirect state")
