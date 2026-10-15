"""Read-only MCP stdio server. No shell, arbitrary path or write tools."""
from __future__ import annotations

from dataclasses import asdict
import json
import math
from pathlib import Path
import re
import sqlite3
import sys

from ai_apitest import __version__
from ai_apitest.core.redaction import redact
from ai_apitest.core.sandbox import policy_scope, read_file
from ai_apitest.tools.registry import ToolExecutionContext, ToolRegistry, ToolSpec

PROTOCOL = "2025-11-25"
MAX_MESSAGE = 1_000_000


def strict_json(data):
    def reject(value):
        raise ValueError(f"Non-finite JSON number: {value}")
    def number(value):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else reject(value)
    return json.loads(data, parse_constant=reject, parse_float=number)


def project_directory(root, name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,79}", name):
        raise ValueError("项目名非法")
    directory = root / "projects" / name
    if directory.is_symlink() or not directory.is_dir() or directory.resolve().parent != (root / "projects").resolve():
        raise ValueError("项目目录不存在或越界")
    return directory


def artifact_path(directory, relative):
    path = directory / relative
    if not path.resolve().is_relative_to(directory.resolve()) or any(item.is_symlink() for item in [path, *path.parents] if item != directory.parent):
        raise ValueError("项目产物路径越界或包含符号链接")
    return path


def load_artifact(directory, relative, default):
    path = artifact_path(directory, relative)
    if not path.is_file():
        return default
    with policy_scope(None):
        return strict_json(read_file(path, 20_000_000))


class MCPServer:
    def __init__(self, root: Path):
        self.root, self.initialized, self.ready = root.resolve(), False, False
        self.tools = ToolRegistry()
        project = {"type": "string", "pattern": "^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,79}$"}
        paged = {"type": "object", "properties": {"project": project, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, "required": ["project"], "additionalProperties": False}
        methods = [("list_projects", {"type": "object", "additionalProperties": False}, self.list_projects), ("get_contracts", paged, self.contracts), ("get_cases", paged, self.cases), ("get_coverage", {"type": "object", "properties": {"project": project}, "required": ["project"], "additionalProperties": False}, self.coverage), ("get_results", paged, self.results)]
        for name, schema, handler in methods:
            self.tools.register(ToolSpec(name, "只读查询 IATest 项目产物；不执行请求或修改文件。", schema, lambda args, context, fn=handler: fn(args), ("mcp_read",), output_schema={"type": "object"}))

    def list_projects(self, args):
        base = self.root / "projects"
        if not base.exists():
            return {"items": []}
        if base.is_symlink():
            raise ValueError("项目根目录不能是符号链接")
        names = []
        for path in sorted(base.iterdir()):
            if not path.is_symlink() and path.is_dir():
                try:
                    project_directory(self.root, path.name)
                    names.append(path.name)
                except ValueError:
                    continue
            if len(names) >= 1000:
                break
        return {"items": names}

    def contracts(self, args):
        directory = project_directory(self.root, args["project"])
        items = load_artifact(directory, "api_docs/endpoints.json", [])
        return self.page(items, args)

    def cases(self, args):
        from ai_apitest.generator.cases import parse_cases_markdown
        directory = project_directory(self.root, args["project"])
        path = artifact_path(directory, f"test_cases/{args['project']}.md")
        if not path.is_file():
            return self.page([], args)
        with policy_scope(None):
            cases = parse_cases_markdown(read_file(path, 20_000_000).decode("utf-8"))
        return self.page([{key: asdict(case)[key] for key in ("id", "title", "method", "path", "priority", "design_method", "runnable", "samples")} for case in cases], args)

    def coverage(self, args):
        directory = project_directory(self.root, args["project"])
        return {"manifest": load_artifact(directory, "test_cases/coverage-manifest.json", {})}

    def results(self, args):
        directory = project_directory(self.root, args["project"])
        db = artifact_path(directory, ".iatest/results.sqlite3")
        if not db.is_file():
            return {"run": None, "items": []}
        with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            run = conn.execute("select id, created_at, metadata_json from runs order by id desc limit 1").fetchone()
            if not run:
                return {"run": None, "items": []}
            items = [dict(row) for row in conn.execute("select case_id, title, status, status_code, elapsed_ms, error from case_results where run_id = ? order by id limit ? offset ?", (run["id"], args.get("limit", 50), args.get("offset", 0)))]
            return {"run": {"id": run["id"], "created_at": run["created_at"], "metadata": strict_json(run["metadata_json"])}, "items": items}

    @staticmethod
    def page(items, args):
        if not isinstance(items, list):
            raise ValueError("产物格式非法")
        start, limit = args.get("offset", 0), args.get("limit", 50)
        return {"items": items[start:start + limit], "total": len(items), "offset": start, "has_more": start + limit < len(items)}

    def dispatch(self, message):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return self.error(None, -32600, "Invalid Request")
        method, identifier, params = message["method"], message.get("id"), message.get("params", {})
        if "id" not in message:
            if method == "notifications/initialized" and self.initialized:
                self.ready = True
            return None
        if isinstance(identifier, bool) or not isinstance(identifier, (str, int)):
            return self.error(None, -32600, "Invalid request id")
        if not isinstance(params, dict):
            return self.error(identifier, -32602, "Invalid params")
        if method == "initialize":
            if self.initialized:
                return self.error(identifier, -32600, "Already initialized")
            client = params.get("clientInfo", {})
            if not isinstance(params.get("protocolVersion"), str) or not isinstance(client, dict) or any(not isinstance(client.get(key), str) or not client[key] for key in ("name", "version")) or not isinstance(params.get("capabilities", {}), dict):
                return self.error(identifier, -32602, "Invalid initialize params")
            self.initialized = True
            result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "iatest", "version": __version__}, "instructions": "Read-only IATest project queries."}
        elif method == "ping":
            result = {}
        elif not self.ready:
            return self.error(identifier, -32002, "Initialization required")
        elif method == "tools/list":
            if params.get("cursor"):
                return self.error(identifier, -32602, "Invalid cursor")
            result = {"tools": [{"name": spec.name, "description": spec.description, "inputSchema": spec.input_schema, "outputSchema": spec.output_schema, "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}} for spec in self.tools.all()]}
        elif method == "tools/call":
            name, args = params.get("name"), params.get("arguments", {})
            if not isinstance(name, str) or name not in {spec.name for spec in self.tools.all()}:
                return self.error(identifier, -32602, "Unknown tool")
            try:
                context = ToolExecutionContext(self.root, "default", None)
                value = self.tools.execute(role_id="mcp_read", allowed_tools=tuple(spec.name for spec in self.tools.all()), name=name, arguments=args, context=context).value
                value = redact(value)
                if len(json.dumps(value, ensure_ascii=False).encode()) > MAX_MESSAGE - 2000:
                    raise ValueError("结果超出响应配额，请缩小查询范围")
                result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "structuredContent": value, "isError": False}
            except Exception as exc:
                result = {"content": [{"type": "text", "text": json.dumps(redact({"error": type(exc).__name__, "detail": str(exc)}), ensure_ascii=False)}], "isError": True}
        else:
            return self.error(identifier, -32601, "Method not found")
        reply = {"jsonrpc": "2.0", "id": identifier, "result": result}
        if len(json.dumps(reply, ensure_ascii=False).encode()) > MAX_MESSAGE:
            return self.error(identifier, -32000, "Response exceeds limit")
        return reply

    @staticmethod
    def error(identifier, code, message):
        return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}

    def serve(self, input=None, output=None):
        source, destination = input or sys.stdin.buffer, output or sys.stdout.buffer
        while line := source.readline(MAX_MESSAGE + 1):
            if len(line) > MAX_MESSAGE:
                while not line.endswith(b"\n") and line:
                    line = source.readline(MAX_MESSAGE + 1)
                response = self.error(None, -32600, "Message exceeds limit")
            else:
                try:
                    response = self.dispatch(strict_json(line))
                except (ValueError, UnicodeError):
                    response = self.error(None, -32700, "Parse error")
            if response is not None:
                destination.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
                destination.flush()
