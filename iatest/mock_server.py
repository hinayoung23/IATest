from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


SENSITIVE_HEADERS = {"authorization", "cookie", "proxy-authorization", "x-api-key", "api-key"}
MOCK_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}


@dataclass(slots=True)
class MockState:
    routes: list[dict[str, Any]]
    max_history: int = 200
    requests: list[dict[str, Any]] = field(default_factory=list)
    route_calls: dict[int, int] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def next_response(self, route_index: int, route: dict[str, Any]) -> dict[str, Any]:
        responses = route.get("responses")
        if not isinstance(responses, list) or not responses:
            if "response" not in route:
                return route
            response = route.get("response")
            return response if isinstance(response, dict) else {"body": response}
        with self.lock:
            call_index = self.route_calls.get(route_index, 0)
            self.route_calls[route_index] = call_index + 1
        response = responses[min(call_index, len(responses) - 1)]
        return response if isinstance(response, dict) else {"body": response}

    def record(self, item: dict[str, Any]) -> None:
        with self.lock:
            self.requests.append(item)
            if len(self.requests) > self.max_history:
                del self.requests[: len(self.requests) - self.max_history]

    def reset(self) -> None:
        with self.lock:
            self.requests.clear()
            self.route_calls.clear()


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def build_mock_server(
    config: dict[str, Any],
    *,
    host: str | None = None,
    port: int | None = None,
) -> ReusableThreadingHTTPServer:
    mock = config.get("mock", {})
    if not isinstance(mock, dict):
        raise ValueError("mock 配置必须是对象")
    routes = validate_mock_routes(mock.get("routes", []))
    state = MockState(
        routes=routes,
        max_history=max(1, min(5000, int(mock.get("max_history", 200)))),
    )
    max_request_bytes = max(1, int(mock.get("max_request_bytes", 1_000_000)))
    server = ReusableThreadingHTTPServer(
        (
            host or str(mock.get("host") or "127.0.0.1"),
            int(port if port is not None else mock.get("port", 8780)),
        ),
        make_mock_handler(state, max_request_bytes=max_request_bytes),
    )
    server.mock_state = state  # type: ignore[attr-defined]
    return server


def serve_mock(
    config: dict[str, Any],
    *,
    host: str | None = None,
    port: int | None = None,
) -> None:
    server = build_mock_server(config, host=host, port=port)
    server.serve_forever()


def validate_mock_routes(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("mock.routes 必须是数组")
    routes = []
    names = set()
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"Mock 路由 #{index} 必须是对象")
        if "enabled" in raw and not isinstance(raw["enabled"], bool):
            raise ValueError(f"Mock 路由 #{index} 的 enabled 必须是布尔值")
        if not raw.get("enabled", True):
            continue
        method = str(raw.get("method") or "GET").upper()
        path = str(raw.get("path") or "").strip()
        name = str(raw.get("name") or f"{method} {path}").strip()
        if not path.startswith("/"):
            raise ValueError(f"Mock 路由 {name or index} 的 path 必须以 / 开头")
        if method not in MOCK_METHODS:
            raise ValueError(f"Mock 路由 {name or index} 不支持请求方法 {method}")
        if "match" in raw and not isinstance(raw["match"], dict):
            raise ValueError(f"Mock 路由 {name or index} 的 match 必须是对象")
        if name in names:
            raise ValueError(f"Mock 路由名称重复：{name}")
        responses = raw.get("responses")
        if responses is not None and (not isinstance(responses, list) or not responses):
            raise ValueError(f"Mock 路由 {name or index} 的 responses 必须是非空数组")
        response_items = responses if isinstance(responses, list) else [raw.get("response", raw)]
        for response in response_items:
            if not isinstance(response, dict):
                continue
            try:
                status = int(response.get("status", 200))
                delay_ms = int(response.get("delay_ms", 0))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Mock 路由 {name or index} 的 status/delay_ms 必须是整数") from exc
            if not 100 <= status <= 599:
                raise ValueError(f"Mock 路由 {name or index} 的 status 必须在 100–599 之间")
            if not 0 <= delay_ms <= 30_000:
                raise ValueError(f"Mock 路由 {name or index} 的 delay_ms 必须在 0–30000 之间")
        names.add(name)
        routes.append({**raw, "name": name, "method": method, "path": path})
    return routes


def make_mock_handler(state: MockState, *, max_request_bytes: int):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            self.handle_mock_request()

        def do_POST(self) -> None:  # noqa: N802
            self.handle_mock_request()

        def do_PUT(self) -> None:  # noqa: N802
            self.handle_mock_request()

        def do_PATCH(self) -> None:  # noqa: N802
            self.handle_mock_request()

        def do_DELETE(self) -> None:  # noqa: N802
            self.handle_mock_request()

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.handle_mock_request()

        def do_HEAD(self) -> None:  # noqa: N802
            self.handle_mock_request()

        def handle_mock_request(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/__iatest_mock__/health" and self.command == "GET":
                self.respond_json({"status": "ok", "routes": len(state.routes)})
                return
            if parsed.path == "/__iatest_mock__/requests" and self.command == "GET":
                with state.lock:
                    requests = list(state.requests)
                self.respond_json({"requests": requests})
                return
            if parsed.path == "/__iatest_mock__/reset" and self.command == "POST":
                state.reset()
                self.respond_json({"status": "reset"})
                return

            try:
                content_length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self.respond_json({"error": "invalid content-length"}, status=400)
                return
            if content_length > max_request_bytes:
                self.respond_json({"error": "request body too large"}, status=413)
                return
            raw_body = self.rfile.read(content_length) if content_length else b""
            request_json = parse_request_json(raw_body, self.headers.get("Content-Type", ""))
            query = parse_qs(parsed.query, keep_blank_values=True)
            request_headers = {str(key).lower(): str(value) for key, value in self.headers.items()}

            matched = None
            for route_index, route in enumerate(state.routes):
                path_params = match_route_path(str(route["path"]), parsed.path)
                if path_params is None or str(route["method"]) != self.command:
                    continue
                if not request_matches(route.get("match"), query, request_headers, request_json, raw_body):
                    continue
                matched = (route_index, route, path_params)
                break

            if matched is None:
                state.record(request_record(self.command, parsed.path, query, request_headers, request_json, 404, ""))
                self.respond_json({"error": "no mock route matched"}, status=404)
                return

            route_index, route, path_params = matched
            response = state.next_response(route_index, route)
            context = response_context(self.command, parsed.path, path_params, query, request_json)
            status = int(response.get("status", 200))
            delay_ms = max(0, min(30_000, int(response.get("delay_ms", 0))))
            if delay_ms:
                time.sleep(delay_ms / 1000)
            headers = render_mock_template(response.get("headers", {}), context)
            body = render_mock_template(response.get("body"), context)
            state.record(
                request_record(
                    self.command,
                    parsed.path,
                    query,
                    request_headers,
                    request_json,
                    status,
                    str(route.get("name") or ""),
                )
            )
            self.respond(status, headers if isinstance(headers, dict) else {}, body)

        def respond_json(self, payload: Any, *, status: int = 200) -> None:
            self.respond(status, {"Content-Type": "application/json; charset=utf-8"}, payload)

        def respond(self, status: int, headers: dict[str, Any], body: Any) -> None:
            response_headers = {str(key): str(value) for key, value in headers.items()}
            if isinstance(body, (dict, list)) or body is None:
                content = json.dumps(body, ensure_ascii=False).encode("utf-8")
                response_headers.setdefault("Content-Type", "application/json; charset=utf-8")
            elif isinstance(body, bytes):
                content = body
                response_headers.setdefault("Content-Type", "application/octet-stream")
            else:
                content = str(body).encode("utf-8")
                response_headers.setdefault("Content-Type", "text/plain; charset=utf-8")
            self.send_response(status)
            for key, value in response_headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(content)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def match_route_path(pattern: str, actual: str) -> dict[str, str] | None:
    names: list[str] = []
    position = 0
    parts = ["^"]
    for match in re.finditer(r"{([A-Za-z_][A-Za-z0-9_]*)}", pattern):
        parts.append(re.escape(pattern[position:match.start()]))
        parts.append("([^/]+)")
        names.append(match.group(1))
        position = match.end()
    parts.append(re.escape(pattern[position:]))
    parts.append("$")
    matched = re.fullmatch("".join(parts), actual)
    if not matched:
        return None
    return {name: unquote(value) for name, value in zip(names, matched.groups())}


def request_matches(
    match: Any,
    query: dict[str, list[str]],
    headers: dict[str, str],
    request_json: Any,
    raw_body: bytes,
) -> bool:
    if not isinstance(match, dict):
        return True
    expected_query = match.get("query", {})
    if isinstance(expected_query, dict):
        for name, expected in expected_query.items():
            actual = query.get(str(name), [])
            expected_values = [str(item) for item in expected] if isinstance(expected, list) else [str(expected)]
            if actual != expected_values:
                return False
    expected_headers = match.get("headers", {})
    if isinstance(expected_headers, dict):
        for name, expected in expected_headers.items():
            if headers.get(str(name).lower()) != str(expected):
                return False
    if "json" in match and not mapping_contains(request_json, match.get("json")):
        return False
    if "body" in match and raw_body.decode("utf-8", errors="replace") != str(match.get("body")):
        return False
    return True


def mapping_contains(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and mapping_contains(actual[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, list) and actual == expected
    return actual == expected


def parse_request_json(body: bytes, content_type: str) -> Any:
    if not body or "json" not in content_type.lower():
        return None
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def response_context(
    method: str,
    path: str,
    path_params: dict[str, str],
    query: dict[str, list[str]],
    request_json: Any,
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "request_method": method,
        "request_path": path,
        **path_params,
        **{name: values[0] if len(values) == 1 else values for name, values in query.items()},
    }
    if isinstance(request_json, dict):
        for name, value in request_json.items():
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(name)):
                context.setdefault(str(name), value)
    return context


def render_mock_template(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, str):
        match = re.fullmatch(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}", value)
        if match:
            return context.get(match.group(1), "")
        return re.sub(
            r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}",
            lambda item: str(context.get(item.group(1), "")),
            value,
        )
    if isinstance(value, list):
        return [render_mock_template(item, context) for item in value]
    if isinstance(value, dict):
        return {key: render_mock_template(item, context) for key, item in value.items()}
    return value


def request_record(
    method: str,
    path: str,
    query: dict[str, list[str]],
    headers: dict[str, str],
    request_json: Any,
    response_status: int,
    route: str,
) -> dict[str, Any]:
    return {
        "method": method,
        "path": path,
        "query": query,
        "headers": {
            name: ("***" if name in SENSITIVE_HEADERS else value)
            for name, value in headers.items()
        },
        "json": redact_payload(request_json),
        "response_status": response_status,
        "route": route,
        "received_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                "***"
                if re.search(r"token|secret|password|authorization|api[_-]?key|cookie", str(key), re.I)
                else redact_payload(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    return value
