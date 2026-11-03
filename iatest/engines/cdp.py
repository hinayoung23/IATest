"""Minimal bounded CDP transport for framework-owned Chromium processes."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import time
from collections import deque
from urllib.parse import urlsplit


class CDP:
    def __init__(self, url, control, *, message_hook=None):
        self.message_hook = message_hook
        parsed = urlsplit(url)
        if parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("CDP must use a local framework-owned endpoint")
        self.control = control
        self.socket = socket.create_connection(
            (parsed.hostname, parsed.port), timeout=2
        )
        self.socket.settimeout(0.1)
        self.buffer = bytearray()
        self.sequence = 0
        self.calls, self.sent, self.received = 0, 0, 0
        self.events = deque(maxlen=256)
        self.event_overflows = 0
        key = base64.b64encode(os.urandom(16)).decode()
        request = f"GET {parsed.path} HTTP/1.1\r\nHost: {parsed.hostname}:{parsed.port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        self.socket.sendall(request.encode())
        deadline = time.monotonic() + 3
        while b"\r\n\r\n" not in self.buffer:
            self._receive(deadline)
            if len(self.buffer) > 16384:
                raise ValueError("Oversized WebSocket handshake")
        header, rest = self.buffer.split(b"\r\n\r\n", 1)
        self.buffer = bytearray(rest)
        expected = base64.b64encode(
            hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
            ).digest()
        ).decode()
        headers = {
            line.split(":", 1)[0].lower(): line.split(":", 1)[1].strip()
            for line in header.decode().split("\r\n")[1:]
            if ":" in line
        }
        if (
            not header.startswith(b"HTTP/1.1 101 ")
            or headers.get("sec-websocket-accept") != expected
        ):
            raise ValueError("Invalid WebSocket handshake")

    def _receive(self, deadline):
        while True:
            self.control.check()
            if time.monotonic() > deadline:
                raise TimeoutError("CDP response deadline exceeded")
            try:
                data = self.socket.recv(65536)
                if not data:
                    raise ConnectionError("Chromium disconnected")
                self.received += len(data)
                self.buffer.extend(data)
                if len(self.buffer) > 16777232:
                    raise ValueError("CDP buffer exceeded")
                return
            except TimeoutError:
                continue

    def _take(self, size, deadline):
        while len(self.buffer) < size:
            self._receive(deadline)
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data

    def _send(self, opcode, payload):
        mask = os.urandom(4)
        length = len(payload)
        if length > 1048576:
            raise ValueError("CDP request exceeds 1 MiB")
        header = bytes([0x80 | opcode])
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 65536:
            header += bytes([0xFE]) + struct.pack("!H", length)
        else:
            header += bytes([0xFF]) + struct.pack("!Q", length)
        frame = header + mask + bytes(x ^ mask[i % 4] for i, x in enumerate(payload))
        self.socket.sendall(frame)
        self.sent += len(frame)

    def call(self, method, params=None, session="", timeout=10):
        self.control.check()
        self.calls += 1
        if self.calls > 20000:
            raise ValueError("CDP command budget exhausted")
        self.sequence += 1
        identifier = self.sequence
        request = {"id": identifier, "method": method, "params": params or {}}
        if session:
            request["sessionId"] = session
        self._send(1, json.dumps(request, ensure_ascii=False, allow_nan=False).encode())
        deadline = time.monotonic() + min(timeout, self.control.remaining() or timeout)
        fragments = bytearray()
        while True:
            a, b = self._take(2, deadline)
            opcode, fin = a & 15, a & 128
            if b & 128:
                raise ValueError("Server WebSocket frame must be unmasked")
            length = b & 127
            if length == 126:
                length = struct.unpack("!H", self._take(2, deadline))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._take(8, deadline))[0]
            if length > 16777216 or len(fragments) + length > 16777216:
                raise ValueError("CDP response exceeds 16 MiB")
            payload = self._take(length, deadline)
            if opcode == 8:
                raise ConnectionError("CDP socket closed")
            if opcode == 9:
                self._send(10, payload)
                continue
            if opcode == 10:
                continue
            if opcode not in {0, 1}:
                raise ValueError("Unsupported CDP frame")
            fragments.extend(payload)
            if not fin:
                continue
            result = json.loads(fragments)
            fragments.clear()
            if self.message_hook:
                result = self.message_hook(result)
                if result is None:
                    continue
            if result.get("id") == identifier:
                if "error" in result:
                    raise RuntimeError(
                        f"CDP {method}: {result['error'].get('message')}"
                    )
                return result.get("result", {})
            if result.get("method") in {
                "Browser.downloadWillBegin",
                "Browser.downloadProgress",
            }:
                if len(self.events) == self.events.maxlen:
                    self.event_overflows += 1
                self.events.append(result)

    def take_events(self):
        values = list(self.events)
        self.events.clear()
        return values

    def close(self):
        self.socket.close()
