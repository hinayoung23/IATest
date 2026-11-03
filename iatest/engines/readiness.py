"""Prerequisite discovery and an explicit, owned browser runtime check."""

from __future__ import annotations

import html
import tempfile
import threading
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def prerequisite_status(installed: bool, requirements: list[str]) -> dict:
    return {
        "state": "requires_verification" if installed else "missing_prerequisites",
        "prerequisites_found": installed,
        "runtime_verified": False,
        "requirements": requirements,
    }


def browser_prerequisites() -> dict:
    from iatest.engines.web import chromium_path
    from iatest.engines.webdriver import driver_path

    return {
        browser: prerequisite_status(bool(path and Path(path).is_file()), requirements)
        for browser, path, requirements in (
            (
                "chromium",
                chromium_path(),
                [
                    "Executable Chromium browser",
                    "Origin proxy and isolated browser session",
                ],
            ),
            (
                "firefox",
                driver_path("firefox"),
                ["Firefox and geckodriver", "Confirmed origin proxy capability"],
            ),
            (
                "webkit",
                driver_path("webkit"),
                [
                    "Safari with remote automation enabled",
                    "Confirmed origin proxy capability",
                ],
            ),
        )
    }


def check_browser(browser: str, *, network_policy="strict") -> dict:
    """Run one new browser against a new loopback fixture, with no model calls."""
    from iatest.application.testing_service import execute_test_plan
    from iatest.core.config import init_project

    if browser not in {"chromium", "firefox", "webkit"}:
        raise ValueError("Unknown browser backend")
    from iatest.engines.web import browser_network_policy

    browser_network_policy({"browser": browser, "network_policy": network_policy})
    report = {
        **browser_prerequisites()[browser],
        "browser": browser,
        "checked_at": datetime.now(UTC).isoformat(),
        "scope": "owned_loopback_fixture",
        "model_calls": 0,
        "network_policy": network_policy,
        "background_requests_restricted": network_policy == "strict",
    }
    if network_policy == "navigation_only":
        report["requirements"] = ["Safari with remote automation enabled", "Explicit navigation_only policy; page background requests are unrestricted"]
    if not report["prerequisites_found"]:
        return report
    marker = "ready-" + uuid.uuid4().hex
    page = (
        "<!doctype html><meta charset=utf-8><button id=save "
        "onclick=\"document.querySelector('#status').textContent=this.dataset.value\" "
        f'data-value="{html.escape(marker)}">Save</button><p id=status>Waiting</p>'
    ).encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="iatest-browser-check-") as temporary:
            root = Path(temporary)
            init_project(root, "doctor")
            base = f"http://127.0.0.1:{server.server_port}"
            result = execute_test_plan(
                root,
                "doctor",
                {
                    "id": "browser-readiness",
                    "kind": "web",
                    "budget": {"timeout_seconds": 45},
                    "spec": {
                        "browser": browser,
                        "network_policy": network_policy,
                        "start_url": base,
                        "allowed_origins": [base],
                        "steps": [
                            {"op": "click", "target": {"id": "save"}},
                            {
                                "op": "expect",
                                "target": {"id": "status"},
                                "assert": "text",
                                "expected": marker,
                            },
                        ],
                    },
                },
            )
            report.update(
                state="ready" if result["verdict"] == "passed" else "blocked",
                runtime_verified=result["verdict"] == "passed",
                verdict=result["verdict"],
                error=result.get("error", ""),
            )
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        report.update(state="blocked", error=f"{type(exc).__name__}: {exc}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
    return report
