import os
import shutil
import socket
import subprocess
import time

import pytest
from test_wire_protocols import mqtt_packet, read, serve

from iatest.application.testing_service import execute_test_plan
from iatest.core.config import init_project
from iatest.engines.http import NetworkScope
from iatest.engines.wire import MQTT, mqtt_string
from iatest.execution.control import RunControl


def packet(sock, kind, data):
    length = bytearray()
    n = len(data)
    while True:
        value = n % 128
        n //= 128
        length.append(value | (128 if n else 0))
        if not n:
            break
    sock.sendall(bytes([kind]) + length + data)


@pytest.mark.parametrize("version", ["3.1.1", "5.0"])
def test_qos2_duplicate_publish_pubrel_and_outgoing_pubrec_are_idempotent(
    tmp_path, version
):
    five = version == "5.0"
    properties = b"\0" if five else b""

    def broker(sock):
        _, connect = mqtt_packet(sock)
        assert connect[6] == (5 if five else 4)
        packet(sock, 0x20, b"\0\0" + properties)
        kind, body = mqtt_packet(sock)
        assert kind == 0x82
        packet(sock, 0x90, body[:2] + properties + b"\2")
        kind, body = mqtt_packet(sock)
        assert kind == 0x34
        end = 2 + int.from_bytes(body[:2], "big")
        outgoing = body[end : end + 2]
        incoming = mqtt_string("owned/topic") + b"\0\x0b" + properties + b"one"
        packet(sock, 0x34, incoming)
        assert mqtt_packet(sock) == (0x50, b"\0\x0b")
        packet(sock, 0x3C, incoming)
        assert mqtt_packet(sock) == (0x50, b"\0\x0b")
        for _ in range(2):
            packet(sock, 0x62, b"\0\x0b")
            assert mqtt_packet(sock) == (0x70, b"\0\x0b")
        packet(sock, 0x50, outgoing)
        assert mqtt_packet(sock) == (0x62, outgoing)
        packet(sock, 0x50, outgoing)
        assert mqtt_packet(sock) == (0x62, outgoing)
        packet(sock, 0x70, outgoing)
        packet(sock, 0x30, mqtt_string("owned/topic") + properties + b"two")
        read(sock, 1)  # Wait for the client to consume both before closing TCP.

    init_project(tmp_path, "suite")
    with serve(broker) as port:
        endpoint = f"mqtt://127.0.0.1:{port}"
        result = execute_test_plan(
            tmp_path,
            "suite",
            {
                "id": "qos2",
                "kind": "protocol",
                "spec": {
                    "transport": "mqtt",
                    "mqtt_version": version,
                    "endpoint": endpoint,
                    "allowed_origins": [f"http://127.0.0.1:{port}"],
                    "exchanges": [
                        {"op": "subscribe", "topic": "owned/topic", "qos": 2},
                        {
                            "op": "publish",
                            "topic": "owned/topic",
                            "qos": 2,
                            "text": "trigger",
                        },
                        {"op": "receive", "topic": "owned/topic", "expect_text": "one"},
                        {"op": "receive", "topic": "owned/topic", "expect_text": "two"},
                    ],
                },
            },
        )
        assert result["verdict"] == "passed", result


@pytest.mark.parametrize(
    "body",
    [b"", b"\x80", b"\x80\0", b"\2\x21\0", b"\2\x24\2", b"\2\xff\0", b"\4\x24\1\x24\1"],
)
def test_mqtt5_malformed_properties_are_rejected(body):
    client = MQTT.__new__(MQTT)
    with pytest.raises(ValueError):
        client.properties(body, 0, "connack")


def test_mqtt5_server_keepalive_is_honored_while_receiving(tmp_path):
    def broker(sock):
        mqtt_packet(sock)
        packet(sock, 0x20, b"\0\0\3\x13\0\1")
        kind, body = mqtt_packet(sock)
        assert kind == 0x82
        packet(sock, 0x90, body[:2] + b"\0\0")
        for _ in range(3):
            assert mqtt_packet(sock) == (0xC0, b"")
            packet(sock, 0xD0, b"")
        packet(sock, 0x30, mqtt_string("owned/topic") + b"\0alive")
        read(sock, 1)

    init_project(tmp_path, "suite")
    with serve(broker) as port:
        client = MQTT(
            NetworkScope(tmp_path, "suite", [f"http://127.0.0.1:{port}"]),
            RunControl(timeout_seconds=10),
            f"mqtt://127.0.0.1:{port}",
            {"mqtt_version": "5.0"},
        )
        try:
            client.exchange({"op": "subscribe", "topic": "owned/topic", "qos": 0}, b"")
            assert client.exchange({"op": "receive"}, b"") == b"alive"
        finally:
            client.close()


@pytest.mark.parametrize("version", ["3.1.1", "5.0"])
@pytest.mark.parametrize("qos", [0, 1, 2])
def test_real_mosquitto_versions_and_qos_roundtrip(tmp_path, version, qos):
    executable = os.environ.get("IATEST_MOSQUITTO_PATH") or shutil.which("mosquitto")
    if not executable:
        pytest.skip("Requires real Mosquitto system broker")
    with socket.socket() as address:
        address.bind(("127.0.0.1", 0))
        port = address.getsockname()[1]
    config = tmp_path / "mosquitto.conf"
    config.write_text(
        f"listener {port} 127.0.0.1\nallow_anonymous true\npersistence false\n"
    )
    broker = subprocess.Popen(
        [executable, "-c", str(config)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    client = None
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError("Mosquitto did not start") from None
                time.sleep(0.05)
        init_project(tmp_path, "suite")
        scope = NetworkScope(tmp_path, "suite", [f"http://127.0.0.1:{port}"])
        client = MQTT(
            scope,
            RunControl(timeout_seconds=15),
            f"mqtt://127.0.0.1:{port}",
            {"mqtt_version": version},
        )
        client.exchange({"op": "subscribe", "topic": "owned/topic", "qos": qos}, b"")
        for data in (b"first", b"second"):
            client.exchange({"op": "publish", "topic": "owned/topic", "qos": qos}, data)
            assert (
                client.exchange({"op": "receive", "topic": "owned/topic"}, b"") == data
            )
    finally:
        if client:
            client.close()
        broker.terminate()
        broker.wait(timeout=5)
