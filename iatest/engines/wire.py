"""Native bounded WebSocket, MQTT 3.1.1/5.0 and binary gRPC/h2 sessions and load."""

from __future__ import annotations
from iatest.core.env import environment_values

import base64
import hashlib
import math
import os
import socket
import ssl
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit, urlunsplit

from iatest.engines.contracts import EngineResult
from iatest.engines.grpc_dialogue import SCHEMA as DIALOGUE_SCHEMA
from iatest.engines.grpc_dialogue import Conversation, validate_dialogue
from iatest.engines.http import NetworkScope, validate
from iatest.engines.protobuf import DESCRIPTOR_SCHEMA, Messages

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["transport", "endpoint", "allowed_origins", "exchanges"],
    "properties": {
        "transport": {"enum": ["websocket", "mqtt", "grpc"]},
        "endpoint": {"type": "string", "maxLength": 8192},
        "allowed_origins": {
            "type": "array",
            "minItems": 1,
            "maxItems": 32,
            "items": {"type": "string"},
        },
        "username_env": {"type": "string"},
        "password_env": {"type": "string"},
        "mqtt_version": {"enum": ["3.1.1", "5.0"]},
        "protobuf": DESCRIPTOR_SCHEMA,
        "distribution": {
            "type": "object",
            "additionalProperties": False,
            "required": ["nodes"],
            "properties": {
                "nodes": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 32,
                    "uniqueItems": True,
                    "items": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,80}$"},
                }
            },
        },
        "exchanges": {
            "type": "array",
            "minItems": 1,
            "maxItems": 64,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["op"],
                "properties": {
                    "op": {"enum": ["send", "subscribe", "publish", "receive", "rpc"]},
                    "topic": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "qos": {"type": "integer", "minimum": 0, "maximum": 2},
                    "text": {"type": "string", "maxLength": 1048576},
                    "base64": {"type": "string", "maxLength": 1398104},
                    "rpc_type": {
                        "enum": ["unary", "server_stream", "client_stream", "bidi"]
                    },
                    "message": {"type": "object"},
                    "messages": {
                        "type": "array",
                        "maxItems": 128,
                        "items": {"type": "object"},
                    },
                    "dialogue": DIALOGUE_SCHEMA,
                    "dialogue_timeout_seconds": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "maximum": 600,
                    },
                    "max_messages": {"type": "integer", "minimum": 1, "maximum": 10000},
                    "expected_messages": {
                        "type": "array",
                        "maxItems": 128,
                        "items": {"type": "object"},
                    },
                    "expect_text": {"type": "string", "maxLength": 1048576},
                    "expected_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                    "expected_grpc_status": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 16,
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "maximum": 30,
                    },
                },
            },
        },
        "load": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "users": {"type": "integer", "minimum": 1, "maximum": 32},
                "iterations": {"type": "integer", "minimum": 1, "maximum": 10000},
                "duration_seconds": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "maximum": 600,
                },
                "pacing_seconds": {"type": "number", "minimum": 0, "maximum": 30},
                "rate": {"type": "number", "exclusiveMinimum": 0, "maximum": 10000},
                "arrival_phase": {
                    "type": "number",
                    "minimum": 0,
                    "exclusiveMaximum": 1,
                },
            },
        },
        "thresholds": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "max_error_ratio": {"type": "number", "minimum": 0, "maximum": 1},
                "p95_ms": {"type": "number", "minimum": 0},
                "min_sessions_per_second": {"type": "number", "minimum": 0},
                "max_dropped_ratio": {"type": "number", "minimum": 0, "maximum": 1},
            },
        },
    },
}


def endpoint(value, transport):
    p = urlsplit(value)
    schemes = {
        "websocket": {"ws": 80, "wss": 443},
        "mqtt": {"mqtt": 1883, "mqtts": 8883},
        "grpc": {"grpc": 50051, "grpcs": 443},
    }[transport]
    if (
        p.scheme not in schemes
        or not p.hostname
        or p.username
        or p.password
        or p.fragment
    ):
        raise ValueError(
            "Invalid protocol endpoint; credentials require environment references"
        )
    port = p.port or schemes[p.scheme]
    secure = p.scheme in {"wss", "mqtts", "grpcs"}
    host = "[" + p.hostname + "]" if ":" in p.hostname else p.hostname
    return (
        p,
        port,
        secure,
        urlunsplit(
            (
                "https" if secure else "http",
                host + ":" + str(port),
                p.path or "/",
                p.query,
                "",
            )
        ),
    )


def validate_wire(plan):
    validate(plan.spec, SCHEMA)
    s = plan.spec
    load = s.get("load", {})
    if load.get("users", 1) * load.get("iterations", 1) > 10000:
        raise ValueError("Total protocol sessions exceed the 10,000-result quota")
    p, *_ = endpoint(s["endpoint"], s["transport"])
    if s["transport"] == "grpc" and (p.query or p.path.count("/") != 2):
        raise ValueError("gRPC requires /package.Service/Method")
    if "distribution" in s and plan.kind != "performance":
        raise ValueError("Distributed protocol load requires kind=performance")
    if "distribution" in s and load.get("users", 1) < len(s["distribution"]["nodes"]):
        raise ValueError("Each protocol load node requires at least one user")
    codec = Messages(s["protobuf"], p.path) if "protobuf" in s else None
    if codec and s["transport"] != "grpc":
        raise ValueError("Protobuf descriptors require gRPC")
    if load.get("rate", 0) * load.get("duration_seconds", 600) > 10000:
        raise ValueError("Offered protocol sessions exceed 10,000")
    if "mqtt_version" in s and s["transport"] != "mqtt":
        raise ValueError("mqtt_version requires MQTT transport")
    for e in s["exchanges"]:
        if "qos" in e and (
            s["transport"] != "mqtt" or e["op"] not in {"subscribe", "publish"}
        ):
            raise ValueError("MQTT QoS is only valid for subscribe/publish")
        if {
            "dialogue_timeout_seconds",
            "max_messages",
        } & e.keys() and "dialogue" not in e:
            raise ValueError("Dialogue budgets require dialogue steps")
        if "dialogue" in e:
            validate_dialogue(e, codec)
        if not codec and e.get("rpc_type", "unary") != "unary":
            raise ValueError("Streaming RPC requires a frozen method descriptor")
        if {"message", "messages", "expected_messages"} & e.keys() and not codec:
            raise ValueError("Structured gRPC messages require a frozen descriptor")
        if (
            "message" in e
            and "messages" in e
            or ({"message", "messages"} & e.keys() and {"text", "base64"} & e.keys())
        ):
            raise ValueError("Select exactly one request message representation")
        if codec:
            kind = {
                (False, False): "unary",
                (False, True): "server_stream",
                (True, False): "client_stream",
                (True, True): "bidi",
            }[codec.method.client_streaming, codec.method.server_streaming]
            if e.get("rpc_type", kind) != kind:
                raise ValueError("RPC type differs from the frozen method descriptor")
            inputs = (
                [] if "dialogue" in e else e.get("messages", [e.get("message", {})])
            )
            if (
                "dialogue" not in e
                and not codec.method.client_streaming
                and len(inputs) != 1
            ):
                raise ValueError("Non-client-streaming RPC accepts exactly one request")
            for item in inputs:
                codec.encode(item)
        if "text" in e and len(e["text"].encode()) > 1048576:
            raise ValueError("Message exceeds 1 MiB UTF-8")
        if (
            e["op"]
            not in {
                "websocket": {"send", "receive"},
                "mqtt": {"subscribe", "publish", "receive"},
                "grpc": {"rpc"},
            }[s["transport"]]
        ):
            raise ValueError("Exchange belongs to a different transport")
        if e["op"] in {"subscribe", "publish"} and not e.get("topic"):
            raise ValueError("MQTT requires an explicit topic")
        if (
            "base64" in e
            and len(base64.b64decode(e["base64"], validate=True)) > 1048576
        ):
            raise ValueError("Message exceeds 1 MiB")
    if not any(
        {
            "expect_text",
            "expected_sha256",
            "expected_grpc_status",
            "expected_messages",
            "dialogue",
        }
        & e.keys()
        for e in s["exchanges"]
    ):
        raise ValueError("An observable protocol postcondition is required")


class WireSocket:
    def __init__(self, scope, control, value, transport):
        self.control = control
        self.p, self.port, self.secure, mapped = endpoint(value, transport)
        scope.check(mapped)
        self.deadline = time.monotonic() + min(10, control.remaining() or 10)
        self.total = 0
        self.socket = socket.create_connection(
            (self.p.hostname, self.port), timeout=min(3, control.remaining() or 3)
        )
        try:
            if self.secure:
                ctx = ssl.create_default_context()
                if transport == "grpc":
                    ctx.set_alpn_protocols(["h2"])
                self.socket = ctx.wrap_socket(
                    self.socket, server_hostname=self.p.hostname
                )
                if transport == "grpc" and self.socket.selected_alpn_protocol() != "h2":
                    raise ValueError("Peer did not negotiate h2")
            self.socket.settimeout(0.1)
        except BaseException:
            self.socket.close()
            raise

    def send(self, data):
        self.control.check()
        self.total += len(data)
        if self.total > 33554432:
            raise ValueError("Protocol byte budget exhausted")
        self.socket.settimeout(min(3, max(0.001, self.deadline - time.monotonic())))
        try:
            self.socket.sendall(data)
        finally:
            self.socket.settimeout(0.1)

    def read(self, n):
        data = bytearray()
        while len(data) < n:
            self.control.check()
            self.idle()
            if time.monotonic() >= self.deadline:
                raise TimeoutError("Protocol response deadline exceeded")
            try:
                chunk = self.socket.recv(min(n - len(data), 65536))
            except TimeoutError:
                continue
            if not chunk:
                raise EOFError("Protocol peer closed before confirmation")
            data.extend(chunk)
            self.total += len(chunk)
            if self.total > 33554432:
                raise ValueError("Protocol byte budget exhausted")
        return bytes(data)

    def idle(self):
        pass

    def close(self):
        self.socket.close()


class WebSocket(WireSocket):
    def __init__(self, scope, control, value):
        super().__init__(scope, control, value, "websocket")
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            path = self.p.path or "/"
            if self.p.query:
                path += "?" + self.p.query
            if any(c in path for c in "\r\n"):
                raise ValueError("Invalid WebSocket path")
            self.send(
                f"GET {path} HTTP/1.1\r\nHost: {self.p.netloc}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode()
            )
            raw = bytearray()
            while not raw.endswith(b"\r\n\r\n"):
                raw.extend(self.read(1))
                if len(raw) > 16384:
                    raise ValueError("WebSocket handshake exceeds bounds")
            lines = raw.decode("ascii").split("\r\n")
            h = {
                k.lower().strip(): v.strip()
                for l in lines[1:]
                if ":" in l
                for k, v in [l.split(":", 1)]
            }
            wanted = base64.b64encode(
                hashlib.sha1(
                    (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
                ).digest()
            ).decode()
            if (
                not lines[0].startswith("HTTP/1.1 101 ")
                or h.get("sec-websocket-accept") != wanted
                or h.get("upgrade", "").lower() != "websocket"
                or h.get("sec-websocket-extensions")
            ):
                raise ValueError("Invalid WebSocket handshake")
        except BaseException:
            self.close()
            raise

    def frame(self, opcode, data):
        if len(data) > 1048576:
            raise ValueError("Message exceeds 1 MiB")
        n = len(data)
        mask = os.urandom(4)
        header = bytes(
            [128 | opcode, 128 | (n if n < 126 else 126 if n <= 65535 else 127)]
        ) + (b"" if n < 126 else struct.pack(">H" if n <= 65535 else ">Q", n))
        self.send(header + mask + bytes(v ^ mask[i % 4] for i, v in enumerate(data)))

    def message(self):
        data = bytearray()
        kind = None
        for _ in range(256):
            a, b = self.read(2)
            op = a & 15
            n = b & 127
            if a & 0x70 or b & 128:
                raise ValueError("Unsupported frame flags")
            if n >= 126:
                n = struct.unpack(
                    ">H" if n == 126 else ">Q", self.read(2 if n == 126 else 8)
                )[0]
            if n > 1048576 - len(data):
                raise ValueError("WebSocket response exceeds quota")
            payload = self.read(n)
            if op >= 8:
                if not a & 128 or n > 125:
                    raise ValueError("Malformed control frame")
                if op == 8:
                    raise EOFError("WebSocket closed")
                if op == 9:
                    self.frame(10, payload)
                elif op != 10:
                    raise ValueError("Invalid control opcode")
                continue
            if op in {1, 2} and kind is None:
                kind = op
            elif op != 0 or kind is None:
                raise ValueError("Invalid fragmented message")
            data.extend(payload)
            if a & 128:
                if kind == 1:
                    data.decode("utf-8")
                return bytes(data)
        raise ValueError("WebSocket fragment budget exhausted")


def mqtt_string(s):
    data = s.encode()
    if len(data) > 65535 or b"\0" in data:
        raise ValueError("Invalid MQTT string")
    return struct.pack(">H", len(data)) + data


class MQTT(WireSocket):
    def __init__(self, scope, control, value, spec):
        super().__init__(scope, control, value, "mqtt")
        self.number = 0
        self.messages = []
        self.version = spec.get("mqtt_version", "3.1.1")
        self.incoming = {}
        self.completed = []
        self.maximum_qos = 2
        self.maximum_packet = 1048576
        self.keep_alive = 30
        self.last_sent = time.monotonic()
        self.ping_pending = None
        try:
            flags = 2
            credentials = b""
            for name, bit in (("username_env", 128), ("password_env", 64)):
                if name in spec:
                    if spec[name] not in environment_values():
                        raise ValueError("MQTT credential environment is missing")
                    flags |= bit
                    credentials += mqtt_string(environment_values()[spec[name]])
            if flags & 64 and not flags & 128:
                raise ValueError("Password requires username")
            self.packet(
                0x10,
                b"\x00\x04MQTT"
                + (b"\x05" if self.version == "5.0" else b"\x04")
                + bytes([flags])
                + b"\x00\x1e"
                + (b"\x00" if self.version == "5.0" else b"")
                + mqtt_string("iatest-" + os.urandom(8).hex())
                + credentials,
            )
            kind, body = self.take()
            if kind != 0x20 or body[:2] != b"\0\0":
                raise ValueError("MQTT connection rejected")
            if self.version == "5.0":
                if self.properties(body, 2, "connack") != len(body):
                    raise ValueError("Malformed MQTT CONNACK")
                self.maximum_qos = self.last_properties.get(0x24, 2)
                self.maximum_packet = min(
                    self.last_properties.get(0x27, 1048576), 1048576
                )
                self.keep_alive = self.last_properties.get(0x13, 30)
            elif len(body) != 2:
                raise ValueError("Malformed MQTT CONNACK")
        except BaseException:
            self.close()
            raise

    def packet(self, kind, data):
        n = len(data)
        length = bytearray()
        while True:
            b = n % 128
            n //= 128
            length.append(b | (128 if n else 0))
            if not n:
                break
        raw = bytes([kind]) + length + data
        if len(raw) > self.maximum_packet:
            raise ValueError("MQTT packet exceeds broker/local maximum")
        self.send(raw)
        self.last_sent = time.monotonic()

    def idle(self):
        if not self.keep_alive:
            return
        now = time.monotonic()
        if self.ping_pending is not None:
            if now - self.ping_pending > self.keep_alive:
                raise TimeoutError("MQTT PINGRESP deadline exceeded")
        elif now - self.last_sent >= self.keep_alive / 2:
            self.ping_pending = now
            self.packet(0xC0, b"")

    def take(self):
        kind = self.read(1)[0]
        if kind >> 4 != 3 and kind not in {
            0x20,
            0x40,
            0x50,
            0x62,
            0x70,
            0x90,
            0xD0,
            0xE0,
        }:
            raise ValueError("Invalid or unsupported MQTT fixed header")
        n = 0
        for i in range(4):
            b = self.read(1)[0]
            n += (b & 127) * 128**i
            if not b & 128:
                if i and not b:
                    raise ValueError("Noncanonical MQTT packet length")
                break
        else:
            raise ValueError("Malformed MQTT packet length")
        if n > 1048576:
            raise ValueError("MQTT response exceeds quota")
        return kind, self.read(n)

    def remember(self, kind, body):
        if len(body) < 2:
            raise ValueError("Malformed MQTT publish")
        end = 2 + struct.unpack(">H", body[:2])[0]
        qos = (kind >> 1) & 3
        if qos > 2 or end > len(body) or end == 2 or not qos and kind & 8:
            raise ValueError("Unsupported MQTT QoS or topic")
        topic = body[2:end].decode()
        mqtt_string(topic)
        if "+" in topic or "#" in topic:
            raise ValueError("Wildcard in MQTT publish topic")
        number = None
        if qos:
            if end + 2 > len(body):
                raise ValueError("Missing packet ID")
            number = struct.unpack(">H", body[end : end + 2])[0]
            if not number:
                raise ValueError("Zero MQTT packet ID")
            end += 2
        if self.version == "5.0":
            end = self.properties(body, end, "publish")
        message = (topic, body[end:])
        if qos == 2:
            previous = self.incoming.get(number)
            if previous is not None and (not kind & 8 or previous != message):
                raise ValueError("MQTT QoS2 packet ID reused before completion")
            if previous is None:
                if len(self.incoming) >= 64:
                    raise ValueError("MQTT QoS2 in-flight budget exceeded")
                self.incoming[number] = message
                if number in self.completed:
                    self.completed.remove(number)
            self.packet(0x50, struct.pack(">H", number))
            return
        if len(self.messages) >= 64:
            raise ValueError("MQTT queue exceeds quota")
        self.messages.append(message)
        if qos == 1:
            self.packet(0x40, struct.pack(">H", number))

    def properties(self, body, offset, context="ack"):
        """Parse typed v5 properties without accepting unknown framing/aliases."""
        length = 0
        for n in range(4):
            if offset >= len(body):
                raise ValueError("Missing MQTT property length")
            value = body[offset]
            offset += 1
            length += (value & 127) * 128**n
            if not value & 128:
                if n and not value:
                    raise ValueError("Noncanonical MQTT property length")
                break
        else:
            raise ValueError("Malformed MQTT property length")
        if offset + length > len(body):
            raise ValueError("Truncated MQTT properties")
        end = offset + length
        allowed = {
            "connack": {
                0x11,
                0x12,
                0x13,
                0x1A,
                0x1C,
                0x1F,
                0x21,
                0x22,
                0x24,
                0x25,
                0x26,
                0x27,
                0x28,
                0x29,
                0x2A,
            },
            "publish": {1, 2, 3, 8, 9, 0x0B, 0x26},
            "ack": {0x1F, 0x26},
        }[context]
        strings = {3, 8, 0x12, 0x1A, 0x1C, 0x1F}
        sizes = {
            1: 1,
            2: 4,
            0x11: 4,
            0x13: 2,
            0x21: 2,
            0x22: 2,
            0x24: 1,
            0x25: 1,
            0x27: 4,
            0x28: 1,
            0x29: 1,
            0x2A: 1,
        }
        values = {}

        def string(binary=False):
            nonlocal offset
            if offset + 2 > end:
                raise ValueError("Truncated MQTT property string")
            count = int.from_bytes(body[offset : offset + 2], "big")
            offset += 2
            if offset + count > end:
                raise ValueError("Truncated MQTT property string")
            raw = body[offset : offset + count]
            offset += count
            if not binary:
                mqtt_string(raw.decode())
            return raw

        while offset < end:
            identifier = body[offset]
            offset += 1
            if (
                identifier not in allowed
                or identifier in values
                and identifier not in {0x26, 0x0B}
            ):
                raise ValueError("Unknown, disallowed or repeated MQTT property")
            if identifier in sizes:
                size = sizes[identifier]
                if offset + size > end:
                    raise ValueError("Truncated MQTT property integer")
                value = int.from_bytes(body[offset : offset + size], "big")
                offset += size
                if (
                    identifier in {1, 0x24, 0x25, 0x28, 0x29, 0x2A}
                    and value not in {0, 1}
                    or identifier in {0x21, 0x27}
                    and not value
                ):
                    raise ValueError("Invalid MQTT property value")
            elif identifier in strings:
                value = string()
            elif identifier == 9:
                value = string(binary=True)
            elif identifier == 0x26:
                value = (string(), string())
            else:
                value = 0
                for n in range(4):
                    if offset >= end:
                        raise ValueError("Truncated MQTT subscription identifier")
                    byte = body[offset]
                    offset += 1
                    value += (byte & 127) * 128**n
                    if not byte & 128:
                        if n and not byte:
                            raise ValueError(
                                "Noncanonical MQTT subscription identifier"
                            )
                        break
                else:
                    raise ValueError("Malformed MQTT subscription identifier")
                if not value:
                    raise ValueError("Zero MQTT subscription identifier")
            values[identifier] = value
        self.last_properties = values
        return end

    def acknowledgment(self, body):
        if len(body) < 2 or body[:2] == b"\0\0":
            raise ValueError("Malformed MQTT acknowledgment ID")
        if self.version == "5.0":
            if len(body) > 2 and body[2] >= 128:
                raise ValueError("MQTT operation rejected with reason " + str(body[2]))
            if len(body) > 3 and self.properties(body, 3) != len(body):
                raise ValueError("Malformed MQTT acknowledgment properties")
            if len(body) > 2 and body[2] not in {0, 0x10}:
                raise ValueError("Invalid MQTT acknowledgment reason")
        elif len(body) != 2:
            raise ValueError("Malformed MQTT acknowledgment")
        return struct.unpack(">H", body[:2])[0]

    def incoming_packet(self, kind, body):
        if kind == 0xD0:
            if body or self.ping_pending is None:
                raise ValueError("Unexpected MQTT PINGRESP")
            self.ping_pending = None
            return True
        if kind >> 4 == 3:
            self.remember(kind, body)
            return True
        if kind == 0x62:
            number = self.acknowledgment(body)
            message = self.incoming.get(number)
            if message is not None:
                if len(self.messages) >= 64:
                    raise ValueError("MQTT queue exceeds quota")
                self.messages.append(self.incoming.pop(number))
                self.completed.append(number)
                self.completed = self.completed[-64:]
            reply = struct.pack(">H", number)
            if (
                message is None
                and number not in self.completed
                and self.version == "5.0"
            ):
                reply += b"\x92"
            self.packet(0x70, reply)
            return True
        if kind >> 4 == 14:
            raise ValueError("MQTT broker disconnected the session")
        return False

    def ack(self, kind, number, qos=None):
        for _ in range(128):
            k, b = self.take()
            if k == kind and b[:2] == struct.pack(">H", number):
                if kind == 0x90:
                    offset = self.properties(b, 2) if self.version == "5.0" else 2
                    if (
                        len(b) != offset + 1
                        or b[offset] not in {0, 1, 2}
                        or b[offset] > qos
                    ):
                        raise ValueError("Subscription rejected or granted invalid QoS")
                else:
                    self.acknowledgment(b)
                return
            if not self.incoming_packet(k, b):
                if kind == 0x70 and k == 0x50 and self.acknowledgment(b) == number:
                    self.packet(0x62, struct.pack(">H", number))
                    continue
                raise ValueError("Unexpected MQTT packet")
        raise ValueError("MQTT ACK budget exhausted")

    def exchange(self, e, data):
        self.number = self.number % 65535 + 1
        if e["op"] == "subscribe":
            qos = e.get("qos", 1)
            self.packet(
                0x82,
                struct.pack(">H", self.number)
                + (b"\x00" if self.version == "5.0" else b"")
                + mqtt_string(e["topic"])
                + bytes([qos]),
            )
            self.ack(0x90, self.number, qos)
            return None
        if e["op"] == "publish":
            qos = e.get("qos", 1)
            if qos > self.maximum_qos:
                raise ValueError("Publish QoS exceeds broker maximum")
            if "+" in e["topic"] or "#" in e["topic"]:
                raise ValueError("MQTT publish topic cannot contain wildcards")
            self.packet(
                0x30 | qos << 1,
                mqtt_string(e["topic"])
                + (struct.pack(">H", self.number) if qos else b"")
                + (b"\x00" if self.version == "5.0" else b"")
                + data,
            )
            if qos == 1:
                self.ack(0x40, self.number)
            elif qos == 2:
                self.ack(0x50, self.number)
                self.packet(0x62, struct.pack(">H", self.number))
                self.ack(0x70, self.number)
            return None
        for _ in range(128):
            if self.messages:
                topic, data = self.messages.pop(0)
                if "topic" not in e or topic == e["topic"]:
                    return data
            else:
                k, b = self.take()
                if not self.incoming_packet(k, b):
                    raise ValueError("Expected MQTT publish")
        raise ValueError("MQTT receive budget exhausted")


class GRPC(WireSocket):
    def __init__(self, scope, control, value):
        super().__init__(scope, control, value, "grpc")
        from h2.config import H2Configuration
        from h2.connection import H2Connection

        self.h2 = H2Connection(
            config=H2Configuration(client_side=True, header_encoding="utf-8")
        )
        self.h2.initiate_connection()
        self.send(self.h2.data_to_send())

    def rpc(self, data):
        messages, status = self.rpc_stream([data])
        return b"".join(messages), status

    def rpc_stream(self, data, *, client_stream=False, server_stream=False):
        from h2.events import (
            ConnectionTerminated,
            DataReceived,
            ResponseReceived,
            StreamEnded,
            StreamReset,
            TrailersReceived,
        )

        if len(data) > 128 or not client_stream and len(data) != 1:
            raise ValueError("Invalid gRPC request message count")
        if any(len(item) > 1048576 for item in data):
            raise ValueError("gRPC request exceeds message quota")
        payload = b"".join(b"\0" + struct.pack(">I", len(item)) + item for item in data)
        if len(payload) > 33554432:
            raise ValueError("gRPC stream exceeds byte quota")
        sid = self.h2.get_next_available_stream_id()
        self.h2.send_headers(
            sid,
            [
                (":method", "POST"),
                (":scheme", "https" if self.secure else "http"),
                (":authority", self.p.netloc),
                (":path", self.p.path),
                ("content-type", "application/grpc"),
                ("te", "trailers"),
            ],
            end_stream=not payload,
        )
        self.send(self.h2.data_to_send())
        pos = 0
        received = bytearray()
        status = None
        http_status = None
        content_type = ""
        while True:
            available = min(
                self.h2.local_flow_control_window(sid),
                self.h2.max_outbound_frame_size,
                len(payload) - pos,
            )
            if available > 0:
                self.h2.send_data(
                    sid,
                    payload[pos : pos + available],
                    end_stream=pos + available == len(payload),
                )
                pos += available
            outbound = self.h2.data_to_send()
            if outbound:
                self.send(outbound)
            if available > 0 and pos < len(payload):
                continue
            if time.monotonic() >= self.deadline:
                raise TimeoutError("gRPC deadline exceeded")
            self.control.check()
            try:
                packet = self.socket.recv(65536)
            except TimeoutError:
                continue
            if not packet:
                raise EOFError("gRPC connection closed")
            self.total += len(packet)
            if self.total > 33554432:
                raise ValueError("gRPC transport exceeds quota")
            for event in self.h2.receive_data(packet):
                if isinstance(event, (StreamReset, ConnectionTerminated)):
                    raise RuntimeError("gRPC stream lost; never replay")  # noqa: TRY004 -- Protocol state, not an invalid Python argument type.
                if getattr(event, "stream_id", sid) != sid:
                    raise ValueError("Unrequested gRPC stream")
                if isinstance(event, (ResponseReceived, TrailersReceived)):
                    h = dict(event.headers)
                    if ":status" in h:
                        http_status = h[":status"]
                        content_type = h.get("content-type", "")
                    if "grpc-status" in h:
                        status = int(h["grpc-status"])
                if isinstance(event, DataReceived):
                    received.extend(event.data)
                    self.h2.acknowledge_received_data(event.flow_controlled_length, sid)
                    if len(received) > 33554432:
                        raise ValueError("gRPC message quota exceeded")
                if isinstance(event, StreamEnded):
                    if (
                        http_status != "200"
                        or not content_type.startswith("application/grpc")
                        or status is None
                    ):
                        raise ValueError("Missing gRPC trailer/status proof")
                    messages = []
                    offset = 0
                    while offset < len(received):
                        if len(received) - offset < 5 or received[offset] != 0:
                            raise ValueError("Compressed or malformed gRPC frame")
                        n = struct.unpack(">I", received[offset + 1 : offset + 5])[0]
                        offset += 5
                        if n > 1048576 or offset + n > len(received):
                            raise ValueError("Truncated gRPC frame")
                        messages.append(bytes(received[offset : offset + n]))
                        if len(messages) > 128:
                            raise ValueError("gRPC response message quota exceeded")
                        offset += n
                    if not server_stream and (
                        len(messages) > 1 or status == 0 and len(messages) != 1
                    ):
                        raise ValueError(
                            "Unary gRPC response must contain exactly one message on success"
                        )
                    return messages, status


class WireEngine:
    def __init__(self, root, project, sample_sink=None):
        self.root, self.project, self.sample_sink = root, project, sample_sink

    def execute(self, plan, context):
        validate_wire(plan)
        s = plan.spec
        if "distribution" in s:
            from iatest.nodes.wire_load import execute_distributed_wire

            return execute_distributed_wire(self.root, self.project, plan, context)
        scope = NetworkScope(self.root, self.project, s["allowed_origins"])
        scope.check(endpoint(s["endpoint"], s["transport"])[3])
        load = s.get("load", {})
        started = time.monotonic()
        deadline = started + load.get("duration_seconds", 600)
        codec = (
            Messages(s["protobuf"], urlsplit(s["endpoint"]).path)
            if "protobuf" in s
            else None
        )
        rate = load.get("rate")
        phase = load.get("arrival_phase", 0)
        slots = (
            max(0, math.ceil(load.get("duration_seconds", 600) * rate - phase))
            if rate
            else 0
        )
        counter = 0
        lock = threading.Lock()

        def user(index):
            nonlocal counter
            rows = []
            for iteration in range(slots if rate else load.get("iterations", 1)):
                context.control.check()
                due = time.monotonic()
                if rate:
                    with lock:
                        slot = counter
                        counter += 1
                    if slot >= slots:
                        break
                    due = started + (slot + phase) / rate
                    context.control.wait(max(0, due - time.monotonic()))
                    if time.monotonic() - due > max(0.05, 1 / rate):
                        continue
                if time.monotonic() > deadline:
                    break
                before = time.monotonic()
                client = None
                passed = True
                error = ""
                unknown = False
                conversation_metrics = {}
                try:
                    context.control.request()
                    from iatest.execution import native_ipc

                    if native_ipc.authorize_request is not None:
                        native_ipc.authorize_request(context.control)
                    client = {
                        "websocket": lambda: WebSocket(
                            scope, context.control, s["endpoint"]
                        ),
                        "mqtt": lambda: MQTT(scope, context.control, s["endpoint"], s),
                        "grpc": lambda: GRPC(scope, context.control, s["endpoint"]),
                    }[s["transport"]]()
                    for e in s["exchanges"]:
                        client.deadline = min(
                            deadline,
                            time.monotonic()
                            + e.get(
                                "dialogue_timeout_seconds", e.get("timeout_seconds", 5)
                            ),
                            time.monotonic() + (context.control.remaining() or 600),
                        )
                        data = (
                            base64.b64decode(e["base64"], validate=True)
                            if "base64" in e
                            else e.get("text", "").encode()
                        )
                        response = None
                        if s["transport"] == "websocket":
                            if e["op"] == "send":
                                unknown = True
                                client.frame(2 if "base64" in e else 1, data)
                            if (
                                e["op"] == "receive"
                                or {"expect_text", "expected_sha256"} & e.keys()
                            ):
                                response = client.message()
                                unknown = False
                        elif s["transport"] == "mqtt":
                            if e["op"] == "publish":
                                unknown = True
                            response = client.exchange(e, data)
                            if response is not None:
                                unknown = False
                        else:
                            unknown = True
                            if "dialogue" in e:
                                conversation = Conversation(client, codec, e)
                                try:
                                    status, _ = conversation.run()
                                finally:
                                    for key, count in {
                                        "sent_messages": conversation.sent,
                                        "received_messages": conversation.received,
                                    }.items():
                                        conversation_metrics[key] = (
                                            conversation_metrics.get(key, 0) + count
                                        )
                            elif codec:
                                messages = e.get("messages", [e.get("message", {})])
                                encoded = [codec.encode(item) for item in messages]
                                outputs, status = client.rpc_stream(
                                    encoded,
                                    client_stream=codec.method.client_streaming,
                                    server_stream=codec.method.server_streaming,
                                )
                                decoded = [codec.decode(item) for item in outputs]
                                if "expected_messages" in e:
                                    passed &= decoded == [
                                        codec.expected(item)
                                        for item in e["expected_messages"]
                                    ]
                                response = outputs[0] if len(outputs) == 1 else None
                            else:
                                response, status = client.rpc(data)
                            unknown = False
                            passed &= status == e.get("expected_grpc_status", 0)
                        if "expect_text" in e:
                            passed &= response == e["expect_text"].encode()
                        if "expected_sha256" in e:
                            passed &= (
                                response is not None
                                and hashlib.sha256(response).hexdigest()
                                == e["expected_sha256"]
                            )
                        if not passed:
                            break
                except (OSError, EOFError, RuntimeError, ValueError) as exc:
                    error = type(exc).__name__ + ": " + str(exc)[:300]
                    passed = False
                finally:
                    if client:
                        client.close()
                rows.append(
                    {
                        "user": index,
                        "iteration": iteration,
                        "passed": bool(passed),
                        "unknown_delivery": unknown,
                        "error": error,
                        "latency_ms": (time.monotonic() - before) * 1000,
                        "schedule_delay_ms": max(0, before - due) * 1000,
                        **conversation_metrics,
                    }
                )
                if unknown:
                    break
                if load.get("pacing_seconds"):
                    context.control.wait(load["pacing_seconds"])
            return rows

        with ThreadPoolExecutor(
            max_workers=load.get("users", 1), thread_name_prefix="iatest-wire"
        ) as pool:
            rows = [
                r
                for batch in pool.map(user, range(load.get("users", 1)))
                for r in batch
            ]
        elapsed = time.monotonic() - started
        if rate:
            context.control.wait(max(0, deadline - time.monotonic()))
            elapsed = time.monotonic() - started
        errors = sum(not r["passed"] for r in rows)
        latencies = sorted(r["latency_ms"] for r in rows)
        result = EngineResult(plan.id, plan.kind)
        result.steps = rows
        result.metrics = {
            "transport": s["transport"],
            "sessions": len(rows),
            "errors": errors,
            "error_ratio": errors / len(rows) if rows else 1,
            "p95_ms": latencies[max(0, math.ceil(len(latencies) * 0.95) - 1)]
            if latencies
            else None,
            "sessions_per_second": len(rows) / max(elapsed, 0.000001),
            "requests": len(rows),
            "scheduled": slots if rate else len(rows),
            "dropped": max(0, slots - len(rows)) if rate else 0,
            "elapsed_seconds": elapsed,
        }
        result.metrics["dropped_ratio"] = result.metrics["dropped"] / max(
            1, result.metrics["scheduled"]
        )
        if any("sent_messages" in row for row in rows):
            for key in ("sent_messages", "received_messages"):
                result.metrics["dialogue_" + key] = sum(row.get(key, 0) for row in rows)
        if self.sample_sink:
            for row in rows:
                self.sample_sink(
                    {
                        "kind": "request",
                        "ok": row["passed"],
                        "duration_ms": row["latency_ms"],
                        "unknown_delivery": row["unknown_delivery"],
                    }
                )
        allowed_errors = s.get("thresholds", {}).get("max_error_ratio", 0)
        result.verdict = (
            "passed"
            if rows and result.metrics["error_ratio"] <= allowed_errors
            else "failed"
        )
        if any(r["error"] or r["unknown_delivery"] for r in rows):
            result.verdict = "inconclusive"
        if result.metrics["dropped"] and "max_dropped_ratio" not in s.get(
            "thresholds", {}
        ):
            result.verdict = "inconclusive"
        for key, bound in s.get("thresholds", {}).items():
            metric = {
                "max_error_ratio": "error_ratio",
                "p95_ms": "p95_ms",
                "min_sessions_per_second": "sessions_per_second",
                "max_dropped_ratio": "dropped_ratio",
            }[key]
            value = result.metrics[metric]
            if value is None or (
                value < bound if key.startswith("min_") else value > bound
            ):
                result.verdict = (
                    "failed" if result.verdict != "inconclusive" else result.verdict
                )
        return result
