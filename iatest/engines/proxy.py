"""Origin-gated browser proxy. This is not an OS network namespace."""

from __future__ import annotations

import select
import socket
import socketserver
import threading
from urllib.parse import urlsplit


class BrowserProxy:
    def __init__(self, scope):
        self.scope = scope
        self.blocked = 0
        self.connections = set()
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.slots = threading.BoundedSemaphore(64)
        owner = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                upstream = None
                try:
                    self.connection.settimeout(2)
                    first = self.rfile.readline(8193)
                    if len(first) > 8192:
                        return
                    method, url, version = first.decode("latin1").strip().split(" ", 2)
                    headers = []
                    size = 0
                    while True:
                        line = self.rfile.readline(8193)
                        size += len(line)
                        if size > 65536 or len(line) > 8192 or not line:
                            raise ValueError("Proxy header limit")
                        if line == b"\r\n":
                            break
                        headers.append(line)
                    if method == "CONNECT":
                        parsed = urlsplit("https://" + url)
                        owner.scope.check("https://" + url)
                        port = parsed.port or 443
                    else:
                        parsed = urlsplit(url)
                        if parsed.scheme != "http":
                            raise ValueError("Proxy requires absolute HTTP URL")
                        owner.scope.check(url)
                        port = parsed.port or 80
                    upstream = socket.create_connection(
                        (parsed.hostname, port), timeout=3
                    )
                    with owner.lock:
                        owner.connections.update((upstream, self.connection))
                    if method == "CONNECT":
                        self.connection.sendall(
                            b"HTTP/1.1 200 Connection established\r\n\r\n"
                        )
                    else:
                        path = parsed.path or "/"
                        if parsed.query:
                            path += "?" + parsed.query
                        # Close each HTTP proxy connection so every redirected destination is rechecked.
                        filtered = [
                            h
                            for h in headers
                            if h.split(b":", 1)[0].lower()
                            not in {
                                b"proxy-connection",
                                b"connection",
                                b"proxy-authorization",
                            }
                        ]
                        upstream.sendall(
                            f"{method} {path} {version}\r\n".encode("latin1")
                            + b"".join(filtered)
                            + b"Connection: close\r\n\r\n"
                        )
                    # No reads beyond headers are buffered: body/tunnel bytes stay on the socket.
                    self.connection.settimeout(0.5)
                    upstream.settimeout(0.5)
                    while not owner.stopped.is_set():
                        ready, _, _ = select.select(
                            [self.connection, upstream], [], [], 0.2
                        )
                        for source in ready:
                            data = source.recv(65536)
                            if not data:
                                return
                            (
                                upstream
                                if source is self.connection
                                else self.connection
                            ).sendall(data)
                except (OSError, ValueError):
                    with owner.lock:
                        owner.blocked += 1
                    try:
                        self.connection.sendall(
                            b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                        )
                    except OSError:
                        pass
                finally:
                    with owner.lock:
                        owner.connections.discard(self.connection)
                        owner.connections.discard(upstream)
                    if upstream:
                        upstream.close()

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

            def process_request(self, request, client_address):
                if not owner.slots.acquire(blocking=False):
                    self.shutdown_request(request)
                    return
                try:
                    super().process_request(request, client_address)
                except Exception:
                    owner.slots.release()
                    raise

            def process_request_thread(self, request, client_address):
                try:
                    super().process_request_thread(request, client_address)
                finally:
                    owner.slots.release()

            # Prevent StreamRequestHandler from prefetching POST body before relay.

        Handler.rbufsize = 0
        self.server = Server(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True
        )
        self.thread.start()

    @property
    def address(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.stopped.set()
        with self.lock:
            for connection in list(self.connections):
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
