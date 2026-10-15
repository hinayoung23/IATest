from __future__ import annotations

from contextlib import contextmanager
import ssl
from pathlib import Path
from typing import Any

import httpx

from ai_apitest.core.sandbox import ACTIVE_POLICY


def client_options(environment: dict[str, Any], timeout: float) -> dict[str, Any]:
    policy = ACTIVE_POLICY.get()
    def tls_path(value: str) -> str:
        path = Path(value).expanduser()
        return str((policy.root / path).resolve() if policy else path)
    verify = environment.get("verify_tls", True)
    if isinstance(verify, str):
        verify = ssl.create_default_context(cafile=tls_path(verify))
    if environment.get("client_cert"):
        if verify is False:
            verify = ssl._create_unverified_context()
        elif verify is True:
            verify = ssl.create_default_context()
        cert = environment["client_cert"]
        if isinstance(cert, dict):
            verify.load_cert_chain(tls_path(cert["certfile"]), tls_path(cert["keyfile"]) if cert.get("keyfile") else None, cert.get("password"))
        else:
            verify.load_cert_chain(tls_path(str(cert)))
    result = {"timeout": timeout, "verify": verify, "trust_env": bool(environment.get("trust_env", True)), "follow_redirects": bool(environment.get("follow_redirects", False))}
    def request_control(request):
        from ai_apitest.execution.control import ACTIVE_RUNTIME
        if runtime := ACTIVE_RUNTIME.get():
            runtime.control.request()
    result["event_hooks"] = {"request": [request_control]}
    if policy and policy.mode != "off":
        result["trust_env"] = False
        result["event_hooks"]["request"].insert(0, policy.request_hook)
    if environment.get("proxy"):
        result["proxy"] = environment["proxy"]
    if environment.get("cookies"):
        result["cookies"] = environment["cookies"]
    return result


class BorrowedTransport(httpx.BaseTransport):
    """Borrow a client's pool without borrowing its cookie jar or owning its lifetime."""
    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        # HTTPX has no public pool-borrowing API. Keep this compatibility point
        # isolated and exercise it with both MockTransport and real HTTP in CI.
        return self.client._transport_for_url(request.url).handle_request(request)

    def close(self) -> None:
        pass


@contextmanager
def isolated_client(client: httpx.Client, cookies: httpx.Cookies):
    with httpx.Client(transport=BorrowedTransport(client), timeout=client.timeout, headers=client.headers, cookies=cookies, follow_redirects=client.follow_redirects, trust_env=False, event_hooks=client.event_hooks) as session:
        yield session
