"""Protocol/report tests only. They do not assert external hardware or phone delivery."""

import struct
import threading
import time
import uuid
from contextlib import contextmanager

import httpx
import pytest

from iatest.execution.control import RunControl
from iatest.mobile.hid_reports import (
    ABSOLUTE_MOUSE,
    feature_report_ioctl,
    touch_descriptor,
    touch_release,
)
from iatest.mobile.protocol import DeliveryUnknown, ProtocolError
from iatest.mobile.store import validate_profile
from iatest.mobile.system import preflight
from iatest.mobile.usb_hid import USBHIDSession, reports, scheduled_reports
from iatest.mobile.usb_hid_gateway import GadgetWriter, Gateway, serve


class Writer:
    def __init__(self, fail=False, capabilities=None):
        self.output, self.releases, self.fail = [], 0, fail
        self.capabilities = capabilities or ["keyboard", "relative_mouse"]

    def write(self, name, data, deadline):
        assert deadline > time.monotonic()
        self.output.append((name, data))
        if self.fail:
            raise OSError("Disconnected test transport")

    def release(self):
        self.releases += 1


@contextmanager
def bridge(monkeypatch, *, fail=False, capabilities=None):
    token = uuid.uuid4().hex + uuid.uuid4().hex
    monkeypatch.setenv("IATEST_TEST_HID_TOKEN", token)
    writer = Writer(fail, capabilities)
    gateway = Gateway(writer, "owned-cable")
    server = serve(gateway, token, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            {
                "endpoint": "http://127.0.0.1:" + str(server.server_port),
                "binding_id": "owned-cable",
                "token_env": "IATEST_TEST_HID_TOKEN",
            },
            writer,
            gateway,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_ascii_and_relative_mouse_have_exact_report_sizes_and_release():
    output = reports({"op": "text", "text": "a A!~\n\t"})
    assert output[:6] == [
        ("keyboard", bytes([0, 0, 4, 0, 0, 0, 0, 0])),
        ("keyboard", bytes(8)),
        ("keyboard", bytes([0, 0, 44, 0, 0, 0, 0, 0])),
        ("keyboard", bytes(8)),
        ("keyboard", bytes([2, 0, 4, 0, 0, 0, 0, 0])),
        ("keyboard", bytes(8)),
    ]
    assert all(len(data) == 8 for _, data in output)
    assert reports({"op": "move", "dx": -127, "dy": 127, "wheel": -1}) == [
        ("mouse", bytes([0, 129, 127, 255]))
    ]
    assert reports({"op": "click", "button": "right"}) == [
        ("mouse", bytes([2, 0, 0, 0])),
        ("mouse", bytes(4)),
    ]
    with pytest.raises(ValueError):
        reports({"op": "text", "text": "中文"})


def test_real_loopback_control_ack_is_not_a_business_postcondition(monkeypatch):
    with bridge(monkeypatch) as (settings, writer, gateway):
        channel = USBHIDSession(settings, RunControl(timeout_seconds=5))
        try:
            result = channel.execute({"op": "text", "text": "abc"})
            assert result["reports_written"] == 6
            assert result["postcondition_verified"] is False
            assert len(writer.output) == 6
            assert list(gateway.ledger.values()) == ["written"]
            with pytest.raises(RuntimeError, match="leased"):
                USBHIDSession(settings)
        finally:
            channel.close()
        assert writer.releases >= 3
        assert gateway.session == ""
        with httpx.Client(trust_env=False) as client:
            assert (
                client.post(settings["endpoint"] + "/command", json={}).status_code
                == 403
            )


def test_lost_usb_write_releases_and_is_not_replayed(monkeypatch):
    with bridge(monkeypatch, fail=True) as (settings, writer, gateway):
        channel = USBHIDSession(settings)
        try:
            with pytest.raises(DeliveryUnknown, match="never replay"):
                channel.execute({"op": "key", "usage": 4})
            assert len(writer.output) == 1
            assert list(gateway.ledger.values()) == ["unknown"]
            assert writer.releases == 2
            with pytest.raises(ProtocolError, match="reconciliation"):
                channel.execute({"op": "key", "usage": 4})
            assert len(writer.output) == 1
        finally:
            channel.close()


def test_regular_files_cannot_masquerade_as_hid_hardware(tmp_path):
    path = tmp_path / "fake-hid"
    path.write_bytes(b"")
    with pytest.raises(ValueError, match="character devices"):
        GadgetWriter(path, path)
    assert path.read_bytes() == b""


def test_usb_profile_rejects_implicit_xctest_input_and_android():
    profile = {
        "id": "hid",
        "platform": "ios",
        "device_id": "phone",
        "app_id": "dev.iatest.lab",
        "ios_input_backend": "usb_hid",
        "ios_observation": {"transport": "reverse_tls", "host": "192.168.0.2"},
        "usb_hid": {
            "endpoint": "http://127.0.0.1:19430",
            "binding_id": "owned-cable",
            "token_env": "HID_TOKEN",
        },
    }
    validate_profile(profile)
    with pytest.raises(ValueError, match="iOS|physical iPhone"):
        validate_profile({**profile, "platform": "android"})
    with pytest.raises(ValueError, match="explicit hid"):
        preflight(profile, {"steps": [{"op": "tap"}]})


def descriptor_sizes(descriptor):
    size, count, identifier, index, depth = 0, 0, 0, 0, 0
    result = {}
    while index < len(descriptor):
        prefix = descriptor[index]
        length = (0, 1, 2, 4)[prefix & 3]
        value = int.from_bytes(descriptor[index + 1:index + 1 + length], "little")
        tag = prefix & 0xfc
        if tag == 0x74:
            size = value
        elif tag == 0x94:
            count = value
        elif tag == 0x84:
            identifier = value
        elif tag in {0x80, 0xb0}:
            key = (tag, identifier)
            result[key] = result.get(key, 0) + size * count
        elif tag == 0xa0:
            depth += 1
        elif tag == 0xc0:
            depth -= 1
            assert depth >= 0
        index += 1 + length
    assert depth == 0 and index == len(descriptor)
    return result


def test_extended_report_bytes_match_hid_descriptor_and_get_report_abi():
    assert descriptor_sizes(ABSOLUTE_MOUSE) == {(0x80, 0): 48}
    assert descriptor_sizes(touch_descriptor()) == {(0x80, 1): 488, (0xb0, 2): 8}
    request, payload = feature_report_ioctl()
    assert request == 0x40486742 and len(payload) == 72
    identifier, persistent, length = struct.unpack_from("<BBH", payload)
    assert (identifier, persistent, length, payload[4:6]) == (2, 0, 2, b"\x02\x0a")
    point = reports({"op": "point", "x": 12345, "y": 32767, "button": "left"})
    assert [struct.unpack("<BHHb", data) for _, data in point] == [(1, 12345, 32767, 0), (0, 12345, 32767, 0)]


def test_staggered_contacts_lift_at_last_position_and_all_release():
    command = {"op": "touch", "frames": [
        {"t_ms": 0, "contacts": [{"id": 0, "x": 100, "y": 200}]},
        {"t_ms": 10, "contacts": [{"id": 0, "x": 110, "y": 200}, {"id": 3, "x": 300, "y": 400}]},
        {"t_ms": 20, "contacts": [{"id": 3, "x": 310, "y": 410}]},
        {"t_ms": 30, "contacts": []},
    ]}
    encoded = scheduled_reports(command)
    assert [stamp for stamp, _, _ in encoded] == [0, 10, 20, 30]
    third, last = encoded[2][2], encoded[3][2]
    assert third[:2] == b"\x01\x02" and len(third) == 62
    assert struct.unpack_from("<BBHH", third, 2) == (1, 3, 310, 410)
    assert struct.unpack_from("<BBHH", third, 8) == (0, 0, 110, 200)
    assert last[:2] == b"\x01\x01"
    assert struct.unpack_from("<BBHH", last, 2) == (0, 3, 310, 410)
    released = touch_release(third)
    assert struct.unpack_from("<BBHH", released, 2) == (0, 3, 310, 410)
    for bad in (
        {**command, "frames": command["frames"][:-1]},
        {**command, "frames": [command["frames"][0], command["frames"][0], command["frames"][-1]]},
        {**command, "frames": [{"t_ms": 0, "contacts": [{"id": 0, "x": 1, "y": 1}] * 2}, command["frames"][-1]]},
    ):
        with pytest.raises(ValueError):
            reports(bad)


def test_unconfigured_extended_endpoint_is_rejected_before_dispatch(monkeypatch):
    with bridge(monkeypatch) as (settings, writer, gateway):
        channel = USBHIDSession(settings)
        try:
            with pytest.raises(ValueError, match="unconfigured"):
                channel.execute({"op": "point", "x": 1, "y": 1})
            assert not writer.output and not gateway.ledger
            assert not channel.failed
        finally:
            channel.close()


def test_negotiated_extended_endpoints_use_real_loopback_without_claiming_hardware(monkeypatch):
    with bridge(monkeypatch, capabilities=["keyboard", "relative_mouse", "absolute_mouse", "digitizer"]) as (settings, writer, _gateway):
        channel = USBHIDSession(settings)
        try:
            value = channel.execute({"op": "point", "x": 123, "y": 456, "button": "left"})
            assert value["reports_written"] == 2 and not value["postcondition_verified"]
            value = channel.execute({"op": "touch", "frames": [{"t_ms": 0, "contacts": [{"id": 2, "x": 10, "y": 20}]}, {"t_ms": 20, "contacts": []}]})
            assert value["reports_written"] == 2
            assert [name for name, _ in writer.output] == ["absolute_mouse"] * 2 + ["digitizer"] * 2
        finally:
            channel.close()
