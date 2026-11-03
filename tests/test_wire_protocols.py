import base64
import hashlib
import socketserver
import struct
import threading
from contextlib import contextmanager

import pytest

from iatest.application.testing_service import execute_test_plan
from iatest.core.config import init_project, load_config, save_config


def read(sock, n):
    data = b""
    while len(data) < n:
        part = sock.recv(n - len(data))
        if not part:
            raise EOFError
        data += part
    return data


@contextmanager
def serve(function):
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(3)
            try:
                function(self.request)
            except (EOFError, OSError):
                pass

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def websocket(sock):
    header = b""
    while not header.endswith(b"\r\n\r\n"):
        header += read(sock, 1)
    key = next(
        l.split(b":", 1)[1].strip()
        for l in header.split(b"\r\n")
        if l.lower().startswith(b"sec-websocket-key:")
    )
    accept = base64.b64encode(
        hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()
    )
    sock.sendall(
        b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: "
        + accept
        + b"\r\n\r\n"
    )
    a, b = read(sock, 2)
    n = b & 127
    if n == 126:
        n = struct.unpack(">H", read(sock, 2))[0]
    mask = read(sock, 4)
    payload = read(sock, n)
    payload = bytes(v ^ mask[i % 4] for i, v in enumerate(payload))
    assert a & 15 == 1 and b & 128
    sock.sendall(
        b"\x01\x02"
        + payload[:2]
        + b"\x89\x01?"
        + bytes([0x80, len(payload) - 2])
        + payload[2:]
    )
    # Consume the masked pong before closing. Closing with unread client bytes
    # resets TCP on Windows and discards the response we are trying to test.
    opcode, length = read(sock, 2)
    assert opcode == 0x8A and length == 0x81
    mask = read(sock, 4)
    assert bytes([read(sock, 1)[0] ^ mask[0]]) == b"?"
    read(sock, 1)  # Client closes after reading the complete fragmented message.


def mqtt_packet(sock):
    kind = read(sock, 1)[0]
    n = 0
    for i in range(4):
        b = read(sock, 1)[0]
        n += (b & 127) * 128**i
        if not b & 128:
            return kind, read(sock, n)
    raise AssertionError


def mqtt(sock):
    assert mqtt_packet(sock)[0] == 0x10
    sock.sendall(b"\x20\x02\x00\x00")
    kind, data = mqtt_packet(sock)
    assert kind == 0x82
    sock.sendall(b"\x90\x03" + data[:2] + b"\x01")
    kind, data = mqtt_packet(sock)
    assert kind == 0x32
    n = struct.unpack(">H", data[:2])[0]
    topic = data[: n + 2]
    number = data[n + 2 : n + 4]
    payload = data[n + 4 :]
    # Publish arrives before PUBACK: the client must retain it while waiting.
    sock.sendall(
        bytes([0x30, len(topic) + len(payload)])
        + topic
        + payload
        + b"\x40\x02"
        + number
    )


def grpc(sock, missing=False, duplicate=False):
    from h2.config import H2Configuration
    from h2.connection import H2Connection
    from h2.events import DataReceived, StreamEnded

    conn = H2Connection(
        config=H2Configuration(client_side=False, header_encoding="utf-8")
    )
    conn.initiate_connection()
    sock.sendall(conn.data_to_send())
    bodies = {}
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            return
        for event in conn.receive_data(chunk):
            if isinstance(event, DataReceived):
                bodies.setdefault(event.stream_id, bytearray()).extend(event.data)
                conn.acknowledge_received_data(
                    event.flow_controlled_length, event.stream_id
                )
            if isinstance(event, StreamEnded):
                conn.send_headers(
                    event.stream_id,
                    [(":status", "200"), ("content-type", "application/grpc")],
                )
                conn.send_data(
                    event.stream_id,
                    bytes(bodies[event.stream_id]) * (2 if duplicate else 1),
                    end_stream=missing,
                )
                if not missing:
                    conn.send_headers(
                        event.stream_id, [("grpc-status", "0")], end_stream=True
                    )
        sock.sendall(conn.data_to_send())


@pytest.mark.parametrize("transport", ["websocket", "mqtt", "grpc"])
@pytest.mark.parametrize("isolation", ["policy", "process"])
def test_actual_binary_protocols_and_bounded_users(tmp_path, transport, isolation):
    init_project(tmp_path, "suite")
    config = load_config(tmp_path)
    config["sandbox"]["mode"] = isolation
    save_config(tmp_path, config)
    with serve({"websocket": websocket, "mqtt": mqtt, "grpc": grpc}[transport]) as port:
        exchanges = {
            "websocket": [{"op": "send", "text": "hello", "expect_text": "hello"}],
            "mqtt": [
                {"op": "subscribe", "topic": "iatest"},
                {"op": "publish", "topic": "iatest", "text": "hello"},
                {"op": "receive", "topic": "iatest", "expect_text": "hello"},
            ],
            "grpc": [
                {
                    "op": "rpc",
                    "text": "hello",
                    "expect_text": "hello",
                    "expected_grpc_status": 0,
                }
            ],
        }[transport]
        scheme = {"websocket": "ws", "mqtt": "mqtt", "grpc": "grpc"}[transport]
        value = {
            "id": "wire",
            "kind": "performance",
            "budget": {"timeout_seconds": 15, "max_requests": 8},
            "spec": {
                "transport": transport,
                "endpoint": f"{scheme}://127.0.0.1:{port}"
                + ("/test.Echo/Call" if transport == "grpc" else "/"),
                "allowed_origins": [f"http://127.0.0.1:{port}"],
                "exchanges": exchanges,
                "load": {"users": 2, "iterations": 2},
                "thresholds": {"max_error_ratio": 0},
            },
        }
        result = execute_test_plan(tmp_path, "suite", value)
        assert result["verdict"] == "passed", result
        assert result["metrics"]["sessions"] == 4
        assert "hello" not in str(result)


@pytest.mark.parametrize("invalid", ["missing_status", "multiple_messages"])
def test_grpc_http_success_without_valid_unary_status_and_framing_is_not_a_pass(
    tmp_path, invalid
):
    init_project(tmp_path, "suite")
    with serve(
        lambda sock: grpc(
            sock,
            missing=invalid == "missing_status",
            duplicate=invalid == "multiple_messages",
        )
    ) as port:
        result = execute_test_plan(
            tmp_path,
            "suite",
            {
                "id": "grpc",
                "kind": "protocol",
                "spec": {
                    "transport": "grpc",
                    "endpoint": f"grpc://127.0.0.1:{port}/test.Echo/Call",
                    "allowed_origins": [f"http://127.0.0.1:{port}"],
                    "exchanges": [
                        {"op": "rpc", "text": "fixture", "expected_grpc_status": 0}
                    ],
                },
            },
        )
        assert result["verdict"] == "inconclusive", result
        assert result["steps"][0]["unknown_delivery"]
