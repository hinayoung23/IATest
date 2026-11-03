import plistlib
import socket
import struct
import time

import pytest

from iatest.mobile import devices, usbmux
from iatest.mobile.protocol import ProtocolError


class MuxSocket:
    """Explicit daemon protocol boundary; no iPhone or pairing state is faked."""

    def __init__(self, code):
        body = plistlib.dumps({"MessageType": "Result", "Number": code})
        self.incoming = struct.pack("<IIII", len(body) + 16, 1, 8, 1) + body
        self.sent = b""
        self.closed = False

    def sendall(self, value):
        self.sent += value

    def recv(self, length):
        value, self.incoming = self.incoming[:length], self.incoming[length:]
        return value

    def close(self):
        self.closed = True


def native_device(monkeypatch, codes):
    sockets = []

    def daemon():
        sock = MuxSocket(next(codes))
        sockets.append(sock)
        return sock

    monkeypatch.setattr(usbmux, "_daemon", daemon)
    monkeypatch.setattr(
        usbmux,
        "list_devices",
        lambda: [{"device_id": "test-phone", "mux_id": 7, "connection": "USB"}],
    )
    device = devices.NativeDevice(
        {
            "id": "phone",
            "platform": "ios",
            "device_id": "test-phone",
            "app_id": "com.example.lab",
        },
        "connection-test",
    )
    return device, sockets


def test_runner_startup_waits_only_for_refused_connections_then_handshakes_once(
    monkeypatch,
):
    device, sockets = native_device(monkeypatch, iter([3, 3, 0]))
    handshakes = []

    class Channel:
        def __init__(self, sock, **credentials):
            assert not sock.closed
            handshakes.append(sock)

    monkeypatch.setattr(devices, "DeviceChannel", Channel)
    channel = device._channel(
        lambda: usbmux.connect("test-phone", 8101), time.monotonic() + 2
    )
    assert isinstance(channel, Channel)
    assert len(sockets) == 3 and handshakes == [sockets[-1]]
    assert [sock.closed for sock in sockets] == [True, True, False]
    for sock in sockets:
        length, version, kind, tag = struct.unpack("<IIII", sock.sent[:16])
        assert (length, version, kind, tag) == (len(sock.sent), 1, 8, 1)
        request = plistlib.loads(sock.sent[16:])
        assert request["MessageType"] == "Connect"
        assert request["DeviceID"] == 7 and request["PortNumber"] == socket.htons(8101)
    sockets[-1].close()


@pytest.mark.parametrize("code", [1, 2, 6, False])
def test_other_daemon_errors_are_not_retried_or_handshaken(monkeypatch, code):
    device, sockets = native_device(monkeypatch, iter([code]))
    monkeypatch.setattr(
        devices, "DeviceChannel", lambda *a, **k: pytest.fail("No handshake")
    )
    with pytest.raises(ProtocolError, match="port unavailable"):
        device._channel(
            lambda: usbmux.connect("test-phone", 8101), time.monotonic() + 1
        )
    assert len(sockets) == 1 and sockets[0].closed


def test_unavailable_runner_stops_at_connection_deadline(monkeypatch):
    device, sockets = native_device(monkeypatch, iter([3]))
    monkeypatch.setattr(
        devices, "DeviceChannel", lambda *a, **k: pytest.fail("No handshake")
    )
    with pytest.raises(RuntimeError, match="did not become ready"):
        device._channel(
            lambda: usbmux.connect("test-phone", 8101), time.monotonic() + 0.01
        )
    assert len(sockets) == 1 and sockets[0].closed


def test_startup_diagnostics_keep_native_error_before_xcode_diagnostic_noise(monkeypatch):
    device, _ = native_device(monkeypatch, iter([]))
    device.logs = [bytearray((
        "NetworkTransport.m: error: TCP connection failed " + device.token
        + "\n" + "automatic diagnostics\n" * 500
    ).encode())]
    diagnostic = device.diagnostics()
    assert "TCP connection failed [REDACTED]" in diagnostic
    assert device.token not in diagnostic
    assert len(diagnostic) < 5100
