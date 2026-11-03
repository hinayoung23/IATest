import base64
import json
import re
import uuid
from contextlib import closing
from pathlib import Path

import pytest

from iatest.execution.control import RunCancelled, RunControl
from iatest.mobile.bluetooth_hid import (
    PROTOCOL,
    AndroidBluetoothHIDSession,
)
from iatest.mobile.protocol import CommandRejected, DeliveryUnknown, ProtocolError
from iatest.mobile.store import validate_profile
from iatest.mobile.system import preflight


def test_android_descriptor_matches_host_wire_lengths():
    source = (
        Path(__file__).parents[1]
        / "iatest/mobile/native/android-hid/src/dev/iatest/mobile/hid/MainActivity.java"
    ).read_text(encoding="utf-8")
    body = source.split("static byte[] reportDescriptor()", 1)[1].split(
        "void bleState", 1
    )[0]
    descriptor = bytes.fromhex("".join(re.findall(r'"([0-9a-f]+)"', body)))
    identifier = size = count = offset = depth = 0
    bits = {}
    while offset < len(descriptor):
        prefix = descriptor[offset]
        length = (0, 1, 2, 4)[prefix & 3]
        value = int.from_bytes(descriptor[offset + 1 : offset + 1 + length], "little")
        tag = prefix & 0xFC
        if tag == 0x84:
            identifier = value
        elif tag == 0x74:
            size = value
        elif tag == 0x94:
            count = value
        elif tag in {0x80, 0x90, 0xB0}:
            key = (tag, identifier)
            bits[key] = bits.get(key, 0) + size * count
        elif tag == 0xA0:
            depth += 1
        elif tag == 0xC0:
            depth -= 1
            assert depth >= 0
        offset += 1 + length
    assert depth == 0 and offset == len(descriptor)
    assert bits == {(0x80, 1): 64, (0x90, 1): 8, (0x80, 2): 40, (0x80, 3): 32}


@pytest.fixture
def transport(monkeypatch):
    state = {"mode": "ok", "requests": [], "replies": {}, "epoch": uuid.uuid4().hex}

    def adb(self, *args, cleanup=False):
        if args[:3] == ("shell", "am", "start"):
            state["transport"] = args[args.index("transport") + 1]
            return ""
        if args[:2] == ("shell", "pidof"):
            return "123"
        if args[-1] == "files/status.json":
            return json.dumps(
                {
                    "foreground": True,
                    "registered": True,
                    "epoch": state["epoch"],
                    "pid": 123,
                    "transport": state.get("transport", "classic"),
                    "input_ready": True,
                }
            )
        if args[:3] == ("shell", "am", "broadcast"):
            envelope = json.loads(base64.b64decode(args[-1]))
            state["requests"].append(envelope)
            if envelope["op"] == "execute" and (
                state["mode"] == "lost"
                or state["mode"] == "lost_second"
                and sum(r["op"] == "execute" for r in state["requests"]) == 2
            ):
                raise TimeoutError("lost ADB receipt after command")
            reply = {
                k: envelope[k]
                for k in (
                    "protocol",
                    "epoch",
                    "session",
                    "sequence",
                    "operation_id",
                    "host_address",
                    "transport",
                )
            }
            reply.update(
                status="acknowledged",
                capabilities=state.get("capabilities", ["keyboard", "relative_mouse"]),
                max_touch_points=state.get("max_touch_points", 0),
                controlled_reconnect=envelope["transport"] == "ble",
                reports_queued=len(envelope.get("reports", [])),
            )
            if envelope["op"] == "execute":
                if state["mode"] == "rejected":
                    reply.update(
                        status="rejected", error="Foreground lost before dispatch"
                    )
                if state["mode"] == "binding":
                    reply["host_address"] = "AA:BB:CC:DD:EE:00"
                if state["mode"] == "count":
                    reply["reports_queued"] += 1
            state["replies"][envelope["operation_id"]] = reply
            return "Broadcast completed"
        operation = args[-1].split("/")[-1].removesuffix(".json")
        return json.dumps(state["replies"][operation])

    monkeypatch.setattr(AndroidBluetoothHIDSession, "adb", adb)
    return state


def config():
    return {"android_device_id": "fixture-android", "host_address": "AA:BB:CC:DD:EE:FF"}


def test_bluetooth_digitizer_requires_subscription_and_validates_wire_contacts(transport):
    command = {'op':'touch','frames':[{'t_ms':0,'contacts':[{'id':0,'x':10,'y':20},{'id':4,'x':30,'y':40}]},
                                      {'t_ms':100,'contacts':[]}]}
    with closing(AndroidBluetoothHIDSession({**config(), 'transport':'ble', 'experimental_digitizer':True})) as bridge:
        with pytest.raises(ValueError, match='did not subscribe'):
            bridge.execute(command)
    assert not any(r['op'] == 'execute' for r in transport['requests'])
    transport.update(capabilities=['keyboard','relative_mouse','digitizer'], max_touch_points=5)
    with closing(AndroidBluetoothHIDSession({**config(), 'transport':'ble', 'experimental_digitizer':True})) as bridge:
        bridge.execute(command)
        sent = [r for r in transport['requests'] if r['op']=='execute'][-1]['reports']
        assert all(r['id']==4 and len(base64.b64decode(r['data']))==31 for r in sent)
        command['frames'][0]['contacts'][1]['id'] = 5
        before = len(transport['requests'])
        with pytest.raises(ValueError, match='IDs 0–4'):
            bridge.execute(command)
        assert len(transport['requests']) == before


@pytest.mark.parametrize("wrong_owner", [False, True])
def test_managed_ui_restart_republishes_binder_and_fences_owner_epoch(transport, monkeypatch, wrong_owner):
    from iatest.mobile.managed_ble import ManagedBLEOwner

    generation = "b" * 32
    binding = {"socket": "iatest_hid_owner_" + generation, "owner_epoch": generation}
    published = []
    original = AndroidBluetoothHIDSession.adb
    monkeypatch.setattr("iatest.mobile.managed_ble.input_binding", lambda *a: binding)

    def publish(self, owner, op):
        assert owner == binding and op == "publish" and not transport["requests"]
        published.append(owner)

    def adb(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if args[:3] == ("shell", "am", "start"):
            transport["transport"] = args[args.index("transport") + 1]
            assert args[args.index("ble_lifecycle") + 1] == "adb_managed"
        if args[-1] == "files/status.json":
            state = json.loads(result)
            state.update(ble_lifecycle="adb_managed", ble_owner_epoch=generation,
                         ble_owner={"epoch": "c" * 32 if wrong_owner else generation})
            return json.dumps(state)
        return result

    monkeypatch.setattr(ManagedBLEOwner, "_rpc", publish)
    monkeypatch.setattr(AndroidBluetoothHIDSession, "adb", adb)
    settings = {**config(), "transport": "ble", "lifecycle": "adb_managed"}
    if wrong_owner:
        with pytest.raises((ProtocolError, RunCancelled)):
            AndroidBluetoothHIDSession(settings, RunControl(timeout_seconds=.3))
        assert not transport["requests"]
    else:
        with_session = AndroidBluetoothHIDSession(settings)
        with_session.close()
        assert [request["op"] for request in transport["requests"]] == ["hello", "close"]
    assert published == [binding]


def test_paired_android_hid_envelope_and_release_without_business_claim(transport):
    channel = AndroidBluetoothHIDSession(config(), RunControl(timeout_seconds=10))
    try:
        result = channel.execute({"op": "text", "text": "aA"})
        assert (
            result["backend"] == "android_bluetooth_hid"
            and result["reports_queued"] == 4
        )
        assert result["postcondition_verified"] is False
        wire = transport["requests"][-1]
        assert wire["protocol"] == PROTOCOL and wire["sequence"] == 2
        assert [base64.b64decode(r["data"]) for r in wire["reports"]] == [
            bytes([0, 0, 4, 0, 0, 0, 0, 0]),
            bytes(8),
            bytes([2, 0, 4, 0, 0, 0, 0, 0]),
            bytes(8),
        ]
        with pytest.raises(RuntimeError, match="leased"):
            AndroidBluetoothHIDSession(config())
        count = len(transport["requests"])
        with pytest.raises(ValueError, match="relative mouse"):
            channel.execute({"op": "point", "x": 1, "y": 1})
        assert len(transport["requests"]) == count
    finally:
        channel.close()
    assert transport["requests"][-1]["op"] == "close"


def test_ble_startup_recovery_waits_without_handshake_or_input(transport, monkeypatch):
    original = AndroidBluetoothHIDSession.adb
    phases = iter(["discover", "disconnect", "connect", "announce", "subscribe", "ready"])
    observed = []

    def adb(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if args[-1] == "files/status.json":
            assert not transport["requests"]
            phase = next(phases)
            observed.append(phase)
            return json.dumps({**json.loads(result), "ble_recovery": phase})
        return result

    monkeypatch.setattr(AndroidBluetoothHIDSession, "adb", adb)
    channel = AndroidBluetoothHIDSession({**config(), "transport": "ble"})
    channel.close()
    assert observed == ["discover", "disconnect", "connect", "announce", "subscribe", "ready"]
    assert [item["op"] for item in transport["requests"]] == ["hello", "close"]


def test_ble_failed_recovery_rejects_before_handshake(transport, monkeypatch):
    original = AndroidBluetoothHIDSession.adb

    def adb(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if args[-1] == "files/status.json":
            return json.dumps({**json.loads(result), "ble_recovery": "failed"})
        return result

    monkeypatch.setattr(AndroidBluetoothHIDSession, "adb", adb)
    with pytest.raises(CommandRejected, match="before input"):
        AndroidBluetoothHIDSession({**config(), "transport": "ble"})
    assert not transport["requests"]


def test_ble_waits_for_host_when_recovery_has_not_started_yet(transport, monkeypatch):
    original = AndroidBluetoothHIDSession.adb
    observed = []

    def adb(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if args[-1] == "files/status.json":
            assert not transport["requests"]
            observed.append(True)
            return json.dumps({**json.loads(result), "ble_recovery": "none",
                               "input_ready": len(observed) > 1})
        return result

    monkeypatch.setattr(AndroidBluetoothHIDSession, "adb", adb)
    channel = AndroidBluetoothHIDSession({**config(), "transport": "ble"})
    channel.close()
    assert len(observed) == 2
    assert [item["op"] for item in transport["requests"]] == ["hello", "close"]


@pytest.mark.parametrize("mode", ["lost", "binding", "count"])
def test_delivery_uncertainty_never_replays_input(transport, mode):
    channel = AndroidBluetoothHIDSession(config())
    transport["mode"] = mode
    try:
        with pytest.raises(DeliveryUnknown, match="never replay"):
            channel.execute({"op": "key", "usage": 4})
        with pytest.raises(ProtocolError, match="reconciliation"):
            channel.execute({"op": "key", "usage": 4})
        assert sum(r["op"] == "execute" for r in transport["requests"]) == 1
    finally:
        channel.close()


def test_explicit_native_rejection_is_distinct_from_unknown_delivery(transport):
    channel = AndroidBluetoothHIDSession(config())
    transport["mode"] = "rejected"
    try:
        with pytest.raises(CommandRejected, match="before input"):
            channel.execute({"op": "key", "usage": 4})
        assert channel.failed is False
    finally:
        channel.close()


def test_ios_absolute_motion_uses_relative_buttons_and_preserves_report_ids(transport):
    channel = AndroidBluetoothHIDSession(config())
    channel.capabilities.add("absolute_mouse")
    try:
        channel.execute({"op": "point", "x": 1000, "y": 2000, "button": "left"})
        reports = transport["requests"][-1]["reports"]
        assert [(r["t_ms"], r["id"]) for r in reports] == [(0, 2), (40, 2), (90, 3), (170, 3)]
        assert base64.b64decode(reports[0]["data"]) == bytes([0, 232, 4, 208, 7])
        assert base64.b64decode(reports[1]["data"]) == bytes([0, 232, 3, 208, 7])
        assert base64.b64decode(reports[2]["data"]) == bytes([1, 0, 0, 0])
        assert base64.b64decode(reports[3]["data"]) == bytes(4)
    finally:
        channel.close()


@pytest.mark.parametrize("x", [0, 32767])
def test_repeated_absolute_click_repositions_without_pressed_motion(transport, x):
    channel = AndroidBluetoothHIDSession(config())
    channel.capabilities.add("absolute_mouse")
    try:
        for _ in range(2):
            channel.execute({"op": "point", "x": x, "y": 32767, "button": "left"})
            reports = transport["requests"][-1]["reports"]
            nudge, target = [base64.b64decode(r["data"]) for r in reports[:2]]
            assert nudge[0] == target[0] == 0
            assert 0 <= int.from_bytes(nudge[1:3], "little") <= 32767
            assert int.from_bytes(nudge[1:3], "little") != x
            assert int.from_bytes(target[1:3], "little") == x
            assert nudge[3:5] == target[3:5] == b"\xff\x7f"
    finally:
        channel.close()


def test_controlled_reconnect_releases_leases_and_never_claims_input(transport):
    channel = AndroidBluetoothHIDSession({**config(), "transport": "ble"})
    assert channel.reconnect() == {"status": "reconnect_requested", "business_postcondition_verified": False}
    channel.close()
    assert [request["op"] for request in transport["requests"]] == ["hello", "reconnect"]
    assert channel.closed and not channel.leases
    replacement = AndroidBluetoothHIDSession({**config(), "transport": "ble"})
    replacement.close()


def test_long_text_is_batched_only_after_key_release(transport):
    channel = AndroidBluetoothHIDSession({**config(), "transport": "ble"})
    try:
        result = channel.execute({"op": "text", "text": "a" * 70})
        batches = [r for r in transport["requests"] if r["op"] == "execute"]
        assert [len(r["reports"]) for r in batches] == [64, 64, 12]
        assert result["reports_queued"] == 140
        assert all(
            r["transport"] == "ble" and 0 < r["deadline_ms"] <= 5000 for r in batches
        )
        assert all(
            r["reports"][0]["t_ms"] == 0
            and base64.b64decode(r["reports"][-1]["data"]) == bytes(8)
            for r in batches
        )
    finally:
        channel.close()


@pytest.mark.parametrize("wheel", [-5, 5])
def test_wheel_notches_keep_total_motion_and_have_ios_cadence(transport, wheel):
    channel = AndroidBluetoothHIDSession(config())
    try:
        result = channel.execute({"op": "move", "dx": 3, "dy": -2, "wheel": wheel})
        reports = transport["requests"][-1]["reports"]
        assert result["reports_queued"] == 6
        assert [report["t_ms"] for report in reports] == [0, 16, 32, 48, 64, 80]
        assert all(report["id"] == 3 for report in reports)
        payloads = [base64.b64decode(report["data"]) for report in reports]

        def signed(value):
            return value if value < 128 else value - 256

        assert [
            sum(signed(data[index]) for data in payloads) for index in (1, 2, 3)
        ] == [3, -2, wheel]
        assert payloads[-1] == bytes(4)
    finally:
        channel.close()


def test_lost_middle_text_batch_stops_unsent_tail(transport):
    channel = AndroidBluetoothHIDSession({**config(), "transport": "ble"})
    transport["mode"] = "lost_second"
    try:
        with pytest.raises(DeliveryUnknown, match="never replay"):
            channel.execute({"op": "text", "text": "a" * 100})
        assert len([r for r in transport["requests"] if r["op"] == "execute"]) == 2
        assert channel.failed
    finally:
        channel.close()


def test_bluetooth_profile_requires_physical_pair_and_separate_observation():
    profile = {
        "id": "bluetooth",
        "platform": "ios",
        "device_id": "iphone",
        "app_id": "dev.iatest.mobile.lab",
        "ios_input_backend": "android_bluetooth_hid",
        "bluetooth_hid": config(),
        "ios_observation": {"transport": "reverse_tls", "host": "192.168.0.2"},
    }
    validate_profile(profile)
    preflight(
        profile,
        {"steps": [{"op": "hid", "hid_command": {"op": "text", "text": "hello"}}]},
    )
    with pytest.raises(ValueError, match="physical iPhone"):
        validate_profile({**profile, "simulator": True})
    with pytest.raises(ValueError, match="physical iPhone"):
        validate_profile({k: v for k, v in profile.items() if k != "ios_observation"})
    for step in (
        {"op": "text"},
        {"op": "tap"},
        {"op": "fragment"},
        {"op": "hid", "hid_command": {"op": "touch"}},
    ):
        with pytest.raises(ValueError):
            preflight(profile, {"steps": [step]})


def test_status_does_not_reuse_a_stopped_process_registration(monkeypatch):
    from iatest.mobile import bluetooth_hid

    def call(args, **kwargs):
        if "pidof" in args:
            raise RuntimeError("process no longer running")
        return json.dumps(
            {"pid": 123, "registered": True, "foreground": True, "connected": True}
        )

    monkeypatch.setattr(bluetooth_hid, "command", call)
    state = bluetooth_hid.status("fixture-android")
    assert state["running"] is False and state["registered"] is False
    assert state["connected"] is False and state["business_runtime_verified"] is False
