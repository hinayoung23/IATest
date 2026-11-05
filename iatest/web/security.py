"""Console request boundary shared by HTML, actions, tasks and evidence."""
from __future__ import annotations

import hmac
import secrets
from contextvars import ContextVar
from dataclasses import dataclass, field
from http.cookies import CookieError, SimpleCookie
from urllib.parse import parse_qs, urlsplit

CSRF_TOKEN = secrets.token_urlsafe(32)
ACTIVE_CSRF: ContextVar[str] = ContextVar("iatest_console_csrf", default=CSRF_TOKEN)
LOOPBACKS = {"localhost", "127.0.0.1", "::1"}


def origin_parts(value: str) -> tuple[str, str, int]:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or parsed.path not in {"", "/"} or "\\" in value:
        raise ValueError("Invalid console origin")
    return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)


def origin_matches_host(origin: str, host: str) -> bool:
    try:
        source = origin_parts(origin)
        target = origin_parts(source[0] + "://" + host)
        return source[1] in LOOPBACKS and target[1] in LOOPBACKS and source[2] == target[2]
    except ValueError:
        return False


@dataclass
class ConsoleSecurity:
    port: int = 8765
    allowed_origins: tuple[str, ...] = ()
    require_session: bool = False
    csrf: str = CSRF_TOKEN
    bootstrap: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    session: str = field(default_factory=lambda: secrets.token_urlsafe(32))

    def valid_host(self, headers) -> bool:
        hosts = headers.get_all("Host", [])
        if len(hosts) != 1:
            return False
        try:
            host = origin_parts("http://" + hosts[0])
            explicit_port = urlsplit("//" + hosts[0]).port
            allowed = {origin_parts(value)[1:] for value in self.allowed_origins}
            proxy_match = any(host[1] == name and (explicit_port == port if explicit_port is not None else port in {80, 443}) for name, port in allowed)
            return (host[1] in LOOPBACKS and host[2] == self.port) or proxy_match
        except ValueError:
            return False

    def valid_origin(self, value: str) -> bool:
        try:
            parsed = origin_parts(value)
            return (parsed[0] == "http" and parsed[1] in LOOPBACKS and parsed[2] == self.port) or parsed in {origin_parts(item) for item in self.allowed_origins}
        except ValueError:
            return False

    def authenticated(self, headers) -> bool:
        if not self.require_session:
            return True
        cookie = SimpleCookie()
        try:
            cookie.load(headers.get("Cookie", ""))
            value = cookie.get("iatest_session")
            return bool(value and hmac.compare_digest(value.value, self.session))
        except (ValueError, TypeError, CookieError):
            return False

    def authorize(self, handler) -> bool:
        if not self.valid_host(handler.headers):
            handler.respond_error(403, "请求 Host 不在控制台许可范围内")
            return False
        parsed = urlsplit(handler.path)
        if handler.command == "GET" and parsed.path == "/session":
            supplied = parse_qs(parsed.query).get("token", [""])[0]
            if self.bootstrap and hmac.compare_digest(supplied.encode(), self.bootstrap.encode()):
                self.bootstrap = ""
                handler.send_response(303)
                handler.send_header("Set-Cookie", f"iatest_session={self.session}; Path=/; HttpOnly; SameSite=Strict")
                handler.send_header("Location", "/")
                handler.send_header("Cache-Control", "no-store")
                handler.end_headers()
            else:
                handler.respond_error(403, "启动授权链接已失效，请使用当前控制台会话")
            return False
        if not self.authenticated(handler.headers):
            handler.respond_error(401, "请使用启动时显示的本机授权链接打开控制台")
            return False
        return True

    def valid_post(self, headers, token: str) -> bool:
        if not hmac.compare_digest(token.encode(), self.csrf.encode()):
            return False
        origin = headers.get("Origin", "")
        if origin:
            return self.valid_origin(origin)
        # A synchronizer token also supports non-browser clients without Origin.
        return headers.get("Sec-Fetch-Site", "") not in {"cross-site", "same-site"}
