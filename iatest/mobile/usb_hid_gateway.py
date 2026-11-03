"""Linux USB-gadget report writer and an authenticated loopback control service."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import select
import signal
import stat
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from iatest.mobile.protocol import ProtocolError
from iatest.mobile.usb_hid import PROTOCOL, scheduled_reports


class GadgetWriter:
    def __init__(self, keyboard, mouse, absolute_mouse=None, digitizer=None):
        if sys.platform != "linux":
            raise ValueError("Linux USB gadget character devices are required")
        self.handles = {}
        self.last_absolute = bytes(6)
        self.last_touch = bytes([1, 0]) + bytes(60)
        try:
            endpoints = (("keyboard", keyboard), ("mouse", mouse), ("absolute_mouse", absolute_mouse), ("digitizer", digitizer))
            identities = set()
            for name, path in endpoints:
                if path is None:
                    continue
                descriptor = os.open(
                    path, os.O_WRONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
                )
                self.handles[name] = descriptor
                if not stat.S_ISCHR(os.fstat(descriptor).st_mode):
                    raise ValueError("USB gadget endpoints must be character devices")
                identity = os.fstat(descriptor).st_rdev
                if identity in identities:
                    raise ValueError("Each HID function requires a distinct character endpoint")
                identities.add(identity)
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if digitizer is not None:
                from iatest.mobile.hid_reports import feature_report_ioctl

                request, payload = feature_report_ioctl()
                # A missing kernel GET_REPORT ABI fails before advertising touch.
                fcntl.ioctl(self.handles["digitizer"], request, payload)
            self.capabilities = ["keyboard", "relative_mouse"] + [name for name in ("absolute_mouse", "digitizer") if name in self.handles]
        except BaseException:
            self.close()
            raise

    def write(self, name, data, deadline):
        descriptor = self.handles[name]
        left = deadline - time.monotonic()
        if left <= 0 or not select.select([], [descriptor], [], left)[1]:
            raise TimeoutError("USB gadget host is not reading reports")
        if os.write(descriptor, data) != len(data):
            raise ProtocolError("Partial USB HID report; never replay")
        if name == "absolute_mouse":
            self.last_absolute = data
        elif name == "digitizer":
            self.last_touch = data

    def release(self):
        deadline = time.monotonic() + 0.5
        failure = None
        from iatest.mobile.hid_reports import touch_release

        releases = (
            ("keyboard", bytes(8)), ("mouse", bytes(4)),
            ("absolute_mouse", bytes([0]) + self.last_absolute[1:5] + bytes(1)),
            ("digitizer", touch_release(self.last_touch)),
        )
        for name, data in releases:
            try:
                if name in self.handles:
                    self.write(name, data, deadline)
            except (OSError, RuntimeError) as exc:
                failure = exc
        if failure:
            raise ProtocolError(
                "USB key/button release could not be confirmed"
            ) from failure

    def close(self):
        for descriptor in self.handles.values():
            os.close(descriptor)
        self.handles.clear()


class Gateway:
    def __init__(self, writer, binding_id):
        self.writer, self.binding_id = writer, binding_id
        self.epoch = uuid.uuid4().hex
        self.session = ""
        self.sequence = 0
        self.last_seen = 0
        self.ledger = {}
        self.lock = threading.Lock()

    def handle(self, value):
        with self.lock:
            if not isinstance(value, dict) or set(value) != {
                "protocol",
                "binding_id",
                "session",
                "epoch",
                "sequence",
                "operation_id",
                "op",
                "command",
                "deadline_ms",
            }:
                raise ValueError("Invalid HID envelope")
            if value["protocol"] != PROTOCOL or value["binding_id"] != self.binding_id:
                raise ValueError("USB cable binding differs")
            session, operation = value["session"], value["operation_id"]
            if any(
                not isinstance(v, str)
                or len(v) != 32
                or any(c not in "0123456789abcdef" for c in v)
                for v in (session, operation)
            ):
                raise ValueError("Invalid HID session/operation ID")
            if (
                type(value["sequence"]) is not int
                or not 1 <= value["sequence"] <= 100000
                or type(value["deadline_ms"]) is not int
                or not 1 <= value["deadline_ms"] <= 10000
            ):
                raise ValueError("Invalid HID sequence/deadline")
            op = value["op"]
            if op == "hello":
                if self.session == session:
                    raise ValueError(
                        "HID handshake cannot reset an existing operation ledger"
                    )
                if (
                    self.session
                    and self.session != session
                    and time.monotonic() - self.last_seen < 30
                ):
                    raise ValueError("USB HID hardware is leased by another controller")
                self.writer.release()
                self.session, self.sequence, self.ledger = session, 0, {}
            elif session != self.session or value["epoch"] != self.epoch:
                raise ValueError("Stale/unbound HID session")
            if value["sequence"] <= self.sequence:
                raise ValueError("HID sequence must increase")
            self.sequence, self.last_seen = value["sequence"], time.monotonic()
            response = {
                k: value[k]
                for k in (
                    "protocol",
                    "binding_id",
                    "session",
                    "sequence",
                    "operation_id",
                )
            }
            response.update(epoch=self.epoch, status="acknowledged")
            if op == "hello":
                response["capabilities"] = getattr(self.writer, "capabilities", ["keyboard", "relative_mouse"])
            elif op == "close":
                self.writer.release()
                self.session = ""
            elif op == "execute":
                if operation in self.ledger or len(self.ledger) >= 512:
                    raise ValueError(
                        "Duplicate/exhausted HID operation ledger; never replay"
                    )
                encoded = scheduled_reports(value["command"])
                capabilities = set(getattr(self.writer, "capabilities", ["keyboard", "relative_mouse"]))
                if any({"mouse": "relative_mouse"}.get(name, name) not in capabilities for _, name, _ in encoded):
                    raise ValueError("HID endpoint was not configured")
                if encoded[-1][0] >= value["deadline_ms"]:
                    raise ValueError("HID schedule exceeds command deadline")
                self.ledger[operation] = "unknown"
                started = time.monotonic()
                deadline = started + value["deadline_ms"] / 1000
                try:
                    for stamp, name, report in encoded:
                        delay = started + stamp / 1000 - time.monotonic()
                        if delay > 0:
                            time.sleep(delay)
                        self.writer.write(name, report, deadline)
                finally:
                    self.writer.release()
                self.ledger[operation] = "written"
                response["reports_written"] = len(encoded)
            else:
                raise ValueError("Unknown HID operation")
            return response


def serve(gateway, token, port):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(2)

        def do_POST(self):
            length = self.headers.get("Content-Length", "")
            if (
                not length.isdigit()
                or not 1 <= int(length) <= 65536
                or self.headers.get("Transfer-Encoding")
            ):
                self.send_error(400)
                return
            self.connection.settimeout(2)
            try:
                body = self.rfile.read(int(length))
            except (OSError, TimeoutError):
                self.send_error(408)
                return
            if len(body) != int(length):
                self.send_error(400)
                return
            if self.path != "/command" or not hmac.compare_digest(
                self.headers.get("Authorization", ""), "Bearer " + token
            ):
                self.send_error(403)
                return
            try:
                value = gateway.handle(json.loads(body))
                status = 200
            except (ValueError, OSError, RuntimeError):
                value, status = (
                    {"error": "USB HID operation rejected or unconfirmed"},
                    409,
                )
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keyboard", default="/dev/hidg0")
    parser.add_argument("--mouse", default="/dev/hidg1")
    parser.add_argument("--absolute-mouse", help="Optional matching 6-byte absolute-pointer function")
    parser.add_argument("--digitizer", help="Optional matching 62-byte digitizer; requires GET_REPORT ioctl")
    parser.add_argument("--binding-id", required=True)
    parser.add_argument("--token-env", required=True)
    parser.add_argument("--port", type=int, default=19430)
    args = parser.parse_args()
    token = os.environ.get(args.token_env, "")
    if not 32 <= len(token) <= 256 or not token.isascii() or not token.isalnum():
        raise ValueError("Provide an existing 32–256 character alphanumeric token env")
    writer = GadgetWriter(args.keyboard, args.mouse, args.absolute_mouse, args.digitizer)
    server = None

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    gateway = Gateway(writer, args.binding_id)
    try:
        server = serve(gateway, token, args.port)
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.server_close()
        with gateway.lock:
            try:
                writer.release()
            finally:
                writer.close()


if __name__ == "__main__":
    main()
