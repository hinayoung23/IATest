from __future__ import annotations

import json
import socket
import struct
import threading
import time
from typing import Any

VERSION = "iatest-mobile/1"
MAX_FRAME = 65536
MAX_BINARY = 8 * 1024 * 1024


class ProtocolError(RuntimeError):
    pass


class DeliveryUnknown(ProtocolError):
    """A request may have reached a backend; side effects must not be replayed."""


class CommandRejected(ProtocolError):
    """A correlated backend response explicitly rejected the command."""


class ObservationRejected(CommandRejected):
    """The backend proves its stale-state check rejected input before dispatch."""


def receive_exact(
    sock: socket.socket, size: int, deadline: float | None = None
) -> bytes:
    parts = bytearray()
    while len(parts) < size:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Device response deadline exceeded")
            sock.settimeout(remaining)
        chunk = sock.recv(size - len(parts))
        if not chunk:
            raise EOFError("Device connection closed")
        parts.extend(chunk)
    return bytes(parts)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _constant(value):
    raise ProtocolError(f"Non-finite JSON number: {value}")


def read_frame(sock: socket.socket, deadline: float | None = None) -> dict[str, Any]:
    length = struct.unpack(">I", receive_exact(sock, 4, deadline))[0]
    if not 0 < length <= MAX_FRAME:
        raise ProtocolError("Control frame length exceeds limit")
    try:
        value = json.loads(
            receive_exact(sock, length, deadline),
            object_pairs_hook=_object,
            parse_constant=_constant,
        )
    except (ValueError, RecursionError, UnicodeError) as exc:
        raise ProtocolError("Invalid control JSON") from exc
    if not isinstance(value, dict):
        raise ProtocolError("Control message must be an object")
    return value


def write_frame(sock: socket.socket, value: dict) -> None:
    data = json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode()
    if not 0 < len(data) <= MAX_FRAME:
        raise ProtocolError("Control frame length exceeds limit")
    sock.sendall(struct.pack(">I", len(data)) + data)


class DeviceChannel:
    def __init__(
        self, sock: socket.socket, *, session_id: str, token: str, app_id: str
    ) -> None:
        self.sock, self.session_id, self.token, self.app_id = (
            sock,
            session_id,
            token,
            app_id,
        )
        self.epoch = ""
        self.sequence = 0
        self.lock = threading.Lock()
        self.closed = False
        try:
            hello = self.request("hello", {})
            if (
                not isinstance(hello.get("driver_epoch"), str)
                or not hello["driver_epoch"]
            ):
                raise ProtocolError("Native handshake requires a driver epoch")
        except BaseException:
            self.close()
            raise
        self.epoch = str(hello["driver_epoch"])
        self.capabilities = hello

    def request(
        self, op: str, arguments: dict, *, operation_id: str = "", timeout: float = 5
    ) -> dict:
        with self.lock:
            if op != "close" and getattr(self, "control", None):
                self.control.check()
            if self.closed:
                raise ProtocolError("Device channel is closed")
            self.sequence += 1
            request = {
                "protocol": VERSION,
                "session_id": self.session_id,
                "token": self.token,
                "driver_epoch": self.epoch,
                "sequence": self.sequence,
                "operation_id": operation_id,
                "app_id": self.app_id,
                "deadline_ms": max(1, min(60000, int(timeout * 1000))),
                "op": op,
                "arguments": arguments,
            }
            self.sock.settimeout(timeout)
            deadline = time.monotonic() + timeout
            try:
                write_frame(self.sock, request)
                response = read_frame(self.sock, deadline)
                if response.get("protocol") != VERSION:
                    raise ProtocolError("Native response protocol mismatch")
                if (
                    response.get("sequence") != self.sequence
                    or response.get("session_id") != self.session_id
                ):
                    raise ProtocolError("Response correlation mismatch")
                if self.epoch and response.get("driver_epoch") != self.epoch:
                    raise ProtocolError("Driver epoch changed")
                if response.get("status") == "rejected":
                    if response.get("dispatch_started") is False and response.get("error_code") == "stale_observation":
                        raise ObservationRejected(str(response.get("error", "Stale native observation")))
                    raise CommandRejected(
                        str(response.get("error", "Device rejected command"))
                    )
                binary_length = response.get("binary_length", 0)
                if (
                    type(binary_length) is not int
                    or not 0 <= binary_length <= MAX_BINARY
                ):
                    raise ProtocolError("Invalid binary evidence length")
                if binary_length:
                    response["binary"] = receive_exact(
                        self.sock, binary_length, deadline
                    )
                if response.get("status") == "unknown" and getattr(
                    self, "on_unknown", None
                ):
                    self.on_unknown()
                return response
            except CommandRejected:
                raise
            except (OSError, EOFError, ProtocolError) as exc:
                self.close()
                if op in {
                    "tap",
                    "swipe",
                    "pinch",
                    "key",
                    "text",
                    "launch",
                    "activate_running",
                    "terminate",
                    "fragment",
                    "visual_tap",
                    "web_tap",
                    "web_type",
                    "clipboard_set",
                    "clipboard_restore",
                    "gesture",
                    "ime",
                }:
                    if getattr(self, "on_unknown", None):
                        self.on_unknown()
                    raise DeliveryUnknown(str(exc)) from exc
                raise

    def close(self) -> None:
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
