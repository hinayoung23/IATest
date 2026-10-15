"""Bounded Streamable HTTP bridge for explicitly configured fixture tools."""
from __future__ import annotations

import json
import math

from ai_apitest.core.sandbox import check_url
from ai_apitest.execution.control import ACTIVE_RUNTIME, check_control
from ai_apitest.integrations.mcp_server import strict_json

PROTOCOL = "2025-11-25"
MAX_BYTES = 1_000_000


class HTTPMCPClient:
    def __init__(self, client, url, headers=None, timeout=10):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("MCP 时限必须为有限正数")
        self.client, self.url, self.timeout = client, url, timeout
        self.headers = {**(headers or {}), "Accept": "application/json, text/event-stream"}
        self.counter = 0
        self.initialized = False

    def rpc(self, method, params, *, notification=False):
        check_control()
        check_url(self.url)
        self.counter += 1
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            payload["id"] = self.counter
        timeout = self.timeout
        if runtime := ACTIVE_RUNTIME.get():
            remaining = runtime.control.remaining()
            if remaining is not None:
                timeout = min(timeout, remaining)
        with self.client.stream("POST", self.url, json=payload, headers=self.headers, timeout=timeout, follow_redirects=False) as response:
            if response.status_code == 404 and "MCP-Session-Id" in self.headers:
                self.headers.pop("MCP-Session-Id", None)
                self.initialized = False
                raise ValueError("MCP 会话已失效；未自动重放可能有副作用的调用")
            response.raise_for_status()
            if notification:
                if response.status_code != 202:
                    raise ValueError("MCP 通知需要 HTTP 202")
                return {}
            if method == "initialize" and response.headers.get("MCP-Session-Id"):
                session = response.headers["MCP-Session-Id"]
                if len(session) > 512 or any(not 0x21 <= ord(char) <= 0x7e for char in session):
                    raise ValueError("MCP 会话标识非法")
                self.headers["MCP-Session-Id"] = session
            if response.headers.get("content-type", "").startswith("text/event-stream"):
                buffer, size, result = b"", 0, None
                for chunk in response.iter_bytes():
                    check_control()
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError("MCP 响应超出配额")
                    buffer += chunk
                    buffer = buffer.replace(b"\r\n", b"\n")
                    while b"\n\n" in buffer:
                        event, buffer = buffer.split(b"\n\n", 1)
                        data = b"\n".join(line[5:].lstrip(b" ") for line in event.split(b"\n") if line.startswith(b"data:"))
                        if not data:
                            continue
                        row = strict_json(data)
                        if not isinstance(row, dict):
                            raise ValueError("MCP SSE 帧必须是对象")
                        if row.get("id") == payload["id"] and ("result" in row or "error" in row):
                            result = row
                            break
                        if "method" in row and "id" in row:
                            raise ValueError("该测试数据接入不支持服务端主动调用客户端工具")
                    if result is not None:
                        break
                if result is None:
                    raise ValueError("MCP SSE 响应缺少当前请求结果")
            else:
                from ai_apitest.execution.runner import read_response_content
                content, truncated = read_response_content(response, MAX_BYTES)
                if truncated:
                    raise ValueError("MCP 响应超出配额")
                result = strict_json(content)
        if not isinstance(result, dict) or result.get("jsonrpc") != "2.0" or result.get("id") != payload["id"]:
            raise ValueError("MCP 响应标识或协议非法")
        if "error" in result:
            raise ValueError("MCP 返回工具协议错误：" + json.dumps(result["error"]))
        if not isinstance(result.get("result"), dict):
            raise ValueError("MCP 结果必须是对象")
        return result["result"]

    def initialize(self):
        result = self.rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "iatest-fixtures", "version": "1"}})
        if result.get("protocolVersion") not in {PROTOCOL, "2025-06-18", "2025-03-26"}:
            raise ValueError("MCP 服务端协议版本不受支持")
        self.headers["MCP-Protocol-Version"] = result["protocolVersion"]
        self.rpc("notifications/initialized", {}, notification=True)
        self.initialized = True

    def call_tool(self, name, arguments):
        if not self.initialized:
            self.initialize()
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise ValueError("MCP 测试数据工具返回失败")
        value = result.get("structuredContent")
        if value is None:
            text = [block.get("text", "") for block in result.get("content", []) if block.get("type") == "text"]
            value = strict_json("\n".join(text))
        if not isinstance(value, dict):
            raise ValueError("测试数据工具必须返回结构化 JSON 对象")
        return value

    def close(self):
        if "MCP-Session-Id" not in self.headers:
            return
        check_control()
        check_url(self.url)
        timeout = self.timeout
        if runtime := ACTIVE_RUNTIME.get():
            remaining = runtime.control.remaining()
            if remaining is not None:
                timeout = min(timeout, remaining)
        response = self.client.delete(self.url, headers=self.headers, timeout=timeout, follow_redirects=False)
        if response.status_code not in {200, 202, 204, 404, 405}:
            raise ValueError("无法确认 MCP 会话已释放")
        self.headers.pop("MCP-Session-Id", None)
        self.initialized = False


class MCPFixtureAdapter:
    provider_type = "mcp"
    def __init__(self, provider, client, timeout):
        self.provider = provider
        self.bridge = HTTPMCPClient(client, provider["url"], provider.get("headers"), timeout)

    def action(self, name, context):
        from ai_apitest.execution.fixtures import render_fixture_template
        import re
        action = self.provider[name]
        args = action.get("arguments", {})
        if not isinstance(args, dict):
            raise ValueError("MCP arguments 必须为对象")
        missing = {value for value in re.findall(r"{{\s*(\w+)\s*}}", json.dumps(args)) if value not in context}
        if missing:
            raise ValueError("MCP 夹具缺少变量，无法确认清理完成：" + ", ".join(sorted(missing)))
        return self.bridge.call_tool(action["tool"], render_fixture_template(args, context))

    def prepare(self, provider, context, client):
        from ai_apitest.execution.assertions import json_path
        value = self.action("prepare", context)
        extract = provider["prepare"].get("extract", {})
        if not isinstance(extract, dict):
            raise ValueError("MCP extract 必须为对象")
        values = {}
        for name, path in extract.items():
            item = json_path(value, path)
            if item is None:
                raise ValueError("MCP 工具缺少需要的数据字段")
            values[name] = item
            context[name] = item
        return {"values": values, "cleanup_state": True}

    def cleanup(self, state, context, client):
        try:
            if self.provider.get("cleanup"):
                self.action("cleanup", context)
        finally:
            self.bridge.close()
