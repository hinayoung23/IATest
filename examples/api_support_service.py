"""Loopback demonstration of project-owned time and mail fixture capabilities."""
from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse


class SupportServer(ThreadingHTTPServer):
    def __init__(self, address):
        super().__init__(address, SupportHandler)
        self.states = {}
        self.state_lock = threading.Lock()


class SupportHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self.respond(200, {"status": "ok"})
            return
        name = unquote(path.removeprefix("/mailboxes/"))
        with self.server.state_lock:
            state = self.server.states.get(name)
            if not path.startswith("/mailboxes/") or state is None:
                self.respond(404, {"error": "not found"})
                return
            self.respond(200, {"id": name, "expired": time.monotonic() + state["offset"] >= state["expires_at"], "messages": list(state["messages"])})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        if not 0 <= length <= 1000000:
            self.respond(413, {"error": "body too large"})
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            name = str(body["id"])
            path = urlparse(self.path).path
            with self.server.state_lock:
                if path == "/__test__/prepare":
                    self.server.states[name] = {"expires_at": time.monotonic() + 60, "offset": float(body.get("advance_seconds", 0)), "messages": list(body.get("messages", []))}
                elif path == "/__test__/cleanup":
                    self.server.states.pop(name, None)
                else:
                    self.respond(404, {"error": "not found"})
                    return
            self.respond(200, {"id": name})
        except (ValueError, KeyError, TypeError):
            self.respond(400, {"error": "invalid body"})

    def respond(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8799)
    args = parser.parse_args()
    server = SupportServer(("127.0.0.1", args.port))
    print(f"Test support demo: http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
