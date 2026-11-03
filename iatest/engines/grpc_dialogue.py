"""Incremental, bounded gRPC conversations on one HTTP/2 stream."""

from __future__ import annotations

import json
import re
import struct
import time
from collections import deque

from jsonschema import Draft202012Validator

from iatest.execution.assertions import MISSING, json_path_value

SCHEMA = {
    "type": "array",
    "minItems": 2,
    "maxItems": 20001,
    "items": {
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["op", "message"],
                "properties": {"op": {"const": "send"}, "message": {"type": "object"}},
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["op"],
                "properties": {
                    "op": {"const": "receive"},
                    "expected_message": {"type": "object"},
                    "expected_schema": {"type": "object"},
                },
                "oneOf": [
                    {"required": ["expected_message"]},
                    {"required": ["expected_schema"]},
                ],
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["op"],
                "properties": {"op": {"const": "half_close"}},
            },
        ]
    },
}


def resolve(value, reply, *, check_only=False, depth=0):
    if depth > 32:
        raise ValueError("gRPC dialogue message nesting exceeds quota")
    if isinstance(value, dict):
        if "$reply" in value:
            path = value["$reply"]
            if (
                len(value) != 1
                or not isinstance(path, str)
                or len(path) > 256
                or not re.fullmatch(
                    r"\$(?:\.[A-Za-z_][A-Za-z_0-9]*|\[\d{1,4}\])*", path
                )
            ):
                raise ValueError("Invalid previous-reply reference")
            if check_only:
                return None
            result = json_path_value(reply, path, missing=MISSING)
            if result is MISSING:
                raise ValueError("Previous gRPC reply field is missing")
            return result
        return {
            k: resolve(v, reply, check_only=check_only, depth=depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [
            resolve(v, reply, check_only=check_only, depth=depth + 1) for v in value
        ]
    return value


def validate_dialogue(exchange, codec):
    if not codec:
        raise ValueError("Interactive gRPC requires a frozen protobuf descriptor")
    if {
        "message",
        "messages",
        "text",
        "base64",
        "expected_messages",
        "expect_text",
        "expected_sha256",
    } & exchange.keys():
        raise ValueError("Dialogue cannot be combined with batch RPC payloads/oracles")
    steps = exchange["dialogue"]
    counts = {"send": 0, "receive": 0, "half_close": 0}
    for step in steps:
        op = step["op"]
        counts[op] += 1
        if op == "send":
            if counts["half_close"]:
                raise ValueError("Cannot send after half-close")
            resolve(step["message"], None, check_only=True)
            if '"$reply"' not in json.dumps(step["message"]):
                codec.encode(step["message"])
            elif not counts["receive"]:
                raise ValueError("Reply reference requires an earlier receive")
        elif op == "receive":
            if "expected_message" in step:
                codec.expected(step["expected_message"])
            else:
                schema = step["expected_schema"]
                if len(json.dumps(schema).encode()) > 8192 or any(
                    '"' + key + '"' in json.dumps(schema)
                    for key in ("$ref", "$dynamicRef", "$recursiveRef")
                ):
                    raise ValueError("Reply schema must be bounded and self-contained")
                Draft202012Validator.check_schema(schema)
    if counts["half_close"] != 1 or not counts["receive"]:
        raise ValueError("Dialogue requires one half-close and observable receives")
    if not codec.method.client_streaming and counts["send"] != 1:
        raise ValueError("Non-client-streaming RPC requires one send")
    if not codec.method.server_streaming and counts["receive"] != 1:
        raise ValueError("Non-server-streaming RPC requires one receive")
    if counts["send"] + counts["receive"] > exchange.get("max_messages", 10000):
        raise ValueError("Dialogue exceeds its message budget")


class Conversation:
    def __init__(self, client, codec, exchange):
        self.client, self.codec, self.exchange = client, codec, exchange
        self.sid = client.h2.get_next_available_stream_id()
        self.buffer = bytearray()
        self.queue = deque()
        self.queued_bytes = self.sent = self.received = 0
        self.ended = False
        self.status = self.http_status = None
        self.content_type = ""

    def flush(self):
        data = self.client.h2.data_to_send()
        if data:
            self.client.send(data)

    def check(self):
        self.client.control.check()
        if time.monotonic() >= self.client.deadline:
            raise TimeoutError("gRPC dialogue deadline exceeded")

    def pump(self):
        from h2.events import (
            ConnectionTerminated,
            DataReceived,
            ResponseReceived,
            StreamEnded,
            StreamReset,
            TrailersReceived,
        )

        self.check()
        self.flush()
        try:
            packet = self.client.socket.recv(65536)
        except TimeoutError:
            return
        if not packet:
            raise EOFError(
                "gRPC conversation closed before trailer proof; never replay"
            )
        self.client.total += len(packet)
        if self.client.total > 33554432:
            raise ValueError("gRPC dialogue exceeds transport byte quota")
        for event in self.client.h2.receive_data(packet):
            if isinstance(event, (StreamReset, ConnectionTerminated)):
                raise RuntimeError("gRPC dialogue lost; never replay")  # noqa: TRY004 -- Protocol state loss, not an argument type error.
            if getattr(event, "stream_id", self.sid) not in (0, self.sid):
                raise ValueError("Unrequested gRPC stream")
            if isinstance(event, (ResponseReceived, TrailersReceived)):
                headers = dict(event.headers)
                if ":status" in headers:
                    self.http_status = headers[":status"]
                    self.content_type = headers.get("content-type", "")
                if "grpc-status" in headers:
                    if self.status is not None or not re.fullmatch(
                        r"[0-9]{1,2}", headers["grpc-status"]
                    ):
                        raise ValueError("Invalid/duplicate gRPC status")
                    self.status = int(headers["grpc-status"])
                    if self.status > 16:
                        raise ValueError("Invalid gRPC status")
            if isinstance(event, DataReceived):
                self.buffer.extend(event.data)
                self.client.h2.acknowledge_received_data(
                    event.flow_controlled_length, self.sid
                )
                while len(self.buffer) >= 5:
                    size = struct.unpack(">I", self.buffer[1:5])[0]
                    if self.buffer[0] != 0 or size > 1048576:
                        raise ValueError("Compressed/oversized gRPC frame")
                    if len(self.buffer) < size + 5:
                        break
                    self.queue.append(bytes(self.buffer[5 : 5 + size]))
                    del self.buffer[: 5 + size]
                    self.received += 1
                    self.queued_bytes += size
                    if self.sent + self.received > self.exchange.get(
                        "max_messages", 10000
                    ):
                        raise ValueError("gRPC dialogue message quota exceeded")
                if self.queued_bytes + len(self.buffer) > 4194304:
                    raise ValueError("gRPC receive queue exceeds quota")
            if isinstance(event, StreamEnded):
                if (
                    self.buffer
                    or self.http_status != "200"
                    or self.content_type.split(";", 1)[0]
                    not in {"application/grpc", "application/grpc+proto"}
                    or self.status is None
                ):
                    raise ValueError("Missing gRPC framing/trailer proof")
                self.ended = True
        self.flush()

    def send_message(self, message):
        self.sent += 1
        if self.sent + self.received > self.exchange.get("max_messages", 10000):
            raise ValueError("gRPC dialogue message quota exceeded")
        data = self.codec.encode(message)
        payload = b"\0" + struct.pack(">I", len(data)) + data
        pos = 0
        while pos < len(payload):
            self.check()
            if self.ended:
                raise ValueError("Peer ended gRPC dialogue before send")
            available = min(
                self.client.h2.local_flow_control_window(self.sid),
                self.client.h2.max_outbound_frame_size,
                len(payload) - pos,
            )
            if available:
                self.client.h2.send_data(self.sid, payload[pos : pos + available])
                pos += available
                self.flush()
            else:
                self.pump()

    def run(self):
        milliseconds = max(1, int((self.client.deadline - time.monotonic()) * 1000))
        self.client.h2.send_headers(
            self.sid,
            [
                (":method", "POST"),
                (":scheme", "https" if self.client.secure else "http"),
                (":authority", self.client.p.netloc),
                (":path", self.client.p.path),
                ("content-type", "application/grpc"),
                ("te", "trailers"),
                ("grpc-timeout", str(milliseconds) + "m"),
            ],
        )
        self.flush()
        reply = None
        for step in self.exchange["dialogue"]:
            self.check()
            if step["op"] == "send":
                self.send_message(resolve(step["message"], reply))
            elif step["op"] == "half_close":
                self.client.h2.end_stream(self.sid)
                self.flush()
            else:
                while not self.queue:
                    if self.ended:
                        raise ValueError("gRPC stream ended before expected reply")
                    self.pump()
                data = self.queue.popleft()
                self.queued_bytes -= len(data)
                reply = self.codec.decode(data)
                if "expected_message" in step:
                    valid = reply == self.codec.expected(step["expected_message"])
                else:
                    valid = Draft202012Validator(step["expected_schema"]).is_valid(
                        reply
                    )
                if not valid:
                    raise ValueError("gRPC dialogue reply postcondition failed")
        while not self.ended:
            self.pump()
        if self.queue:
            raise ValueError("Unasserted extra gRPC replies")
        return self.status, {
            "sent_messages": self.sent,
            "received_messages": self.received,
        }
