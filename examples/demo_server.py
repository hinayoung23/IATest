from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class DemoHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            self.respond(200, {"status": "ok"})
            return
        self.respond(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path == "/users":
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            payload = json.loads(body or "{}")
            self.respond(201, {"id": 1, "name": payload.get("name")})
            return
        self.respond(404, {"error": "not found"})

    def respond(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        return


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8000), DemoHandler)
    print("Demo API server: http://127.0.0.1:8000")
    server.serve_forever()
