import io
import json
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from ai_apitest.core.config import load_config, save_config
from ai_apitest.core.models import TestCase as Case, Assertion, Endpoint
from ai_apitest.core.paths import project_docs_file
from ai_apitest.core.serde import write_json
from ai_apitest.core.sandbox import SandboxPolicy, policy_scope, SandboxViolation
from ai_apitest.execution.fixtures import FixtureManager
from ai_apitest.generator.cases import save_cases
from ai_apitest.integrations.mcp_server import MCPServer, MAX_MESSAGE


def initialize(server):
    reply = server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
    assert "result" in reply
    server.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"})


def test_mcp_real_stdio_protocol_read_only_and_size_limit(tmp_path):
    write_json(project_docs_file(tmp_path, "demo"), [{"method": "GET", "path": "/health", "responses": {"200": {}}}])
    messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}, {"jsonrpc": "2.0", "method": "notifications/initialized"}, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_contracts", "arguments": {"project": "demo"}}}, {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "get_results", "arguments": {"project": "../demo"}}}]
    process = subprocess.run([sys.executable, "-m", "ai_apitest", "mcp"], cwd=tmp_path, input="\n".join(json.dumps(row) for row in messages) + "\n", capture_output=True, text=True, timeout=10)
    assert process.returncode == 0, process.stderr
    replies = [json.loads(row) for row in process.stdout.splitlines()]
    assert len(replies) == 4
    assert all(tool["annotations"]["readOnlyHint"] for tool in replies[1]["result"]["tools"])
    assert replies[2]["result"]["structuredContent"]["items"][0]["path"] == "/health"
    assert replies[3]["result"]["isError"]
    server = MCPServer(tmp_path)
    output = io.BytesIO()
    server.serve(io.BytesIO(b'x' * (MAX_MESSAGE + 1) + b'\n{}\n'), output)
    assert [json.loads(row)["error"]["code"] for row in output.getvalue().splitlines()] == [-32600, -32600]


@pytest.mark.parametrize("sse", [False, True])
def test_http_mcp_fixture_initialization_session_extract_and_cleanup(sse):
    seen = []
    def respond(request):
        if request.method == "DELETE":
            seen.append("close")
            return httpx.Response(204)
        data = json.loads(request.content)
        method = data["method"]
        seen.append(method)
        if method == "notifications/initialized":
            assert request.headers["MCP-Session-Id"] == "test-session"
            assert request.headers["MCP-Protocol-Version"] == "2025-11-25"
            return httpx.Response(202)
        result = {"protocolVersion": "2025-11-25", "capabilities": {}, "serverInfo": {"name": "data", "version": "1"}} if method == "initialize" else {"structuredContent": {"id": "entity-1"}, "isError": False}
        if method == "tools/call" and data["params"]["name"] == "delete":
            assert data["params"]["arguments"]["id"] == "entity-1"
        reply = {"jsonrpc": "2.0", "id": data["id"], "result": result}
        return httpx.Response(200, headers={"content-type": "text/event-stream" if sse else "application/json", "MCP-Session-Id": "test-session"}, content=("data: " + json.dumps(reply) + "\n\n").encode() if sse else json.dumps(reply).encode())
    provider = {"enabled": True, "type": "mcp", "url": "https://data.invalid/mcp", "prepare": {"tool": "create", "arguments": {"namespace": "{{iatest_namespace}}"}, "extract": {"entity_id": "$.id"}}, "cleanup": {"tool": "delete", "arguments": {"id": "{{entity_id}}"}}}
    config = {"fixtures": {"providers": {"data": provider}}}
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        manager = FixtureManager(config, client)
        context = {"iatest_namespace": "run"}
        setup, session = manager.prepare(Case("C", "C", "GET", "/", fixture_requirements=["data"]), context)
        assert setup["status"] == "passed" and context["entity_id"] == "entity-1"
        assert manager.cleanup(session, context)["status"] == "passed"
    assert seen == ["initialize", "notifications/initialized", "tools/call", "tools/call", "close"]


def test_mcp_fixture_network_policy_blocks_unapproved_service(tmp_path):
    from ai_apitest.integrations.mcp_client import HTTPMCPClient
    policy = SandboxPolicy.from_config(tmp_path, {}, base_url="https://target.invalid")
    with policy_scope(policy), httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as client:
        with pytest.raises(SandboxViolation):
            HTTPMCPClient(client, "https://other.invalid/mcp").initialize()


@pytest.mark.parametrize("invalid", [b"NaN", b"Infinity", b"1e999"])
def test_mcp_rejects_invalid_json_numbers_and_init_params(tmp_path, invalid):
    server = MCPServer(tmp_path)
    assert server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "clientInfo": {}}})["error"]["code"] == -32602
    output = io.BytesIO()
    server.serve(io.BytesIO(b'{"jsonrpc":"2.0","id":' + invalid + b',"method":"ping"}\n'), output)
    assert json.loads(output.getvalue())["error"]["code"] == -32700
