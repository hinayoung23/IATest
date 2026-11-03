"""Loopback UI persistence lab with a deliberate broken-save comparison."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PAGE = """<!doctype html><html><head><title>Profile lab</title><meta charset="utf-8"></head>
<body><main><h1>Account</h1><button id="settings" onclick="document.querySelector('#profile').hidden=false">Profile settings</button>
<section id="profile" hidden><label for="nickname">Nickname</label><input id="nickname">
<button id="save" onclick="save()">Save nickname</button><p id="status" role="status">Ready</p></section></main>
<script>
fetch('/state').then(r=>r.json()).then(v=>document.querySelector('#nickname').value=v.nickname);
async function save(){const nickname=document.querySelector('#nickname').value;
if(!location.search.includes('broken'))await fetch('/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({nickname})});
document.querySelector('#status').textContent='Saved';}
</script></body></html>"""


def create_server(port=0):
    state, lock = {"nickname": "initial"}, threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, body, content_type="application/json"):
            encoded = (
                body.encode() if isinstance(body, str) else json.dumps(body).encode()
            )
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            if urlparse(self.path).path == "/state":
                with lock:
                    self.reply(dict(state))
            else:
                self.reply(PAGE, "text/html; charset=utf-8")

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            if length > 65536:
                self.send_error(413)
                return
            body = json.loads(self.rfile.read(length) or b"{}")
            with lock:
                state["nickname"] = (
                    "initial"
                    if self.path == "/cleanup"
                    else body.get("nickname", "initial")
                )
            self.reply({"ok": True})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
