"""Small JSON controller transport; workers authenticate and can only access their tasks."""

from __future__ import annotations

import json
import ssl
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from iatest.nodes.store import ControllerStore


def make_controller(
    root, project, host="127.0.0.1", port=0, *, certfile="", keyfile=""
):
    store = ControllerStore(root, project)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, value):
            data = json.dumps(
                {**value, "server_time": time.time()}, allow_nan=False
            ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            node_id = self.headers.get("X-IATest-Node", "")
            auth = self.headers.get("Authorization", "").removeprefix("Bearer ")
            node = store.authenticate(node_id, auth)
            if not node:
                self.reply(401, {"error": "Node authentication failed"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 16777216:
                    raise ValueError("Node message exceeds 16 MiB")
                value = json.loads(self.rfile.read(length))
                instance = value["instance"]
                if self.path == "/v1/register":
                    store.register(node_id, instance, value["capabilities"])
                    self.reply(
                        200,
                        {
                            "registered": True,
                            "allowed_origins": node["profile"]["allowed_origins"],
                            "allowed_kinds": node["profile"].get(
                                "allowed_kinds", ["performance"]
                            ),
                        },
                    )
                    return
                store.touch(node_id, instance)
                if self.path == "/v1/poll":
                    self.reply(200, {"job": store.poll(node_id, instance)})
                    return
                job = store.job(value["job_id"])
                if job["node_id"] != node_id or job["instance"] != instance:
                    raise ValueError("Task is owned by another node instance")
                if self.path == "/v1/heartbeat":
                    self.reply(
                        200,
                        {
                            "state": job["state"],
                            "cancel": job["state"] in {"cancelled", "error"}
                            or not store.global_authority_alive(job),
                        },
                    )
                    return
                if self.path == "/v1/asset":
                    if job["state"] not in {"preparing", "ready"}:
                        raise ValueError(
                            "Assets can only be fetched during task preparation"
                        )
                    self.reply(
                        200,
                        store.asset(
                            job["id"], value["kind"], value["asset_id"], value["offset"]
                        ),
                    )
                    return
                if self.path == "/v1/ready":
                    store.ready(job["id"], value["proof"])
                elif self.path == "/v1/samples":
                    store.put_samples(job["id"], value["sequence"], value["rows"])
                elif self.path == "/v1/complete":
                    from iatest.nodes.worker import import_evidence

                    import_evidence(root, project, value.get("evidence_files", []))
                    store.complete(job["id"], value["result"], value["samples_digest"])
                elif self.path == "/v1/error":
                    store.fail(job["id"], value["error"])
                else:
                    self.reply(404, {"error": "Unknown node endpoint"})
                    return
                self.reply(200, {"accepted": True})
            except (ValueError, TypeError, KeyError, OSError) as exc:
                self.reply(400, {"error": type(exc).__name__, "detail": str(exc)[:500]})

    context = None
    if certfile:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile, keyfile)
    elif host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(
            "LAN controllers require a TLS certificate/key; no plaintext bearer tokens on the LAN"
        )
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    if context:
        try:
            server.socket = context.wrap_socket(server.socket, server_side=True)
        except BaseException:
            server.server_close()
            raise
    return server
