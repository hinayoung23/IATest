"""Loopback-only test lab, including explicitly vulnerable comparison endpoints."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>IATest lab</title></head>
<body><main style="font:18px system-ui;max-width:720px;margin:60px auto">
<h1>IATest test lab</h1><form id="login-form" onsubmit="event.preventDefault();document.querySelector('#status').textContent='Hello '+document.querySelector('#username').value">
<label for="username">Username</label><input id="username" data-testid="username" value="">
<button id="login" type="submit">Sign in</button></form><p id="status">Waiting</p>
<table id="orders"><tr><td class="order-id">order-1</td><td><button>Details</button></td></tr>
<tr><td class="order-id">order-2</td><td><button>Details</button></td></tr></table>
<p id="order-detail">order-1</p></main></body></html>"""


class LabHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        status = 200
        headers = {"X-Content-Type-Options": "nosniff"}
        body = {
            "ok": True,
            "id": "order-1",
            "url": f"http://127.0.0.1:{self.server.server_port}/ui",
        }
        if (
            path == "/secure-order"
            and self.headers.get("Authorization") != "Bearer lab-owner"
        ):
            status, body = 403, {"error": "forbidden"}
        # /vulnerable-order intentionally returns the same resource to both identities.
        if path == "/ui":
            headers["Content-Type"] = "text/html; charset=utf-8"
            encoded = PAGE.encode()
        else:
            headers["Content-Type"] = "application/json"
            encoded = json.dumps(body).encode()
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self):
        # Finite lab requests only; never execute scripts or touch external state.
        length = int(self.headers.get("Content-Length", 0))
        if length > 1048576:
            self.send_error(413)
            return
        body = json.loads(self.rfile.read(length) or b"{}")
        status = (
            200
            if self.path != "/contract"
            or (set(body) == {"name"} and isinstance(body["name"], str))
            else 422
        )
        encoded = b'{"ok":true,"id":"order-1"}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def create_server(port=0):
    return ThreadingHTTPServer(("127.0.0.1", port), LabHandler)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    server = create_server(args.port)
    print(f"Local test lab: http://127.0.0.1:{server.server_port}/ui", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
