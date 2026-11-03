"""Managed ownership must fence UI generations independently of GATT lifetime."""

import subprocess
import hashlib
import json
import struct
from pathlib import Path

import pytest

from iatest.mobile.managed_ble import ManagedBLEOwner, PROTOCOL, input_binding
from iatest.mobile.protocol import CommandRejected, ProtocolError


def settings():
    return {"android_device_id": "managed-fixture", "host_address": "AA:BB:CC:DD:EE:FF",
            "transport": "ble", "lifecycle": "adb_managed"}


def binding():
    return {"protocol": PROTOCOL, "socket": "iatest_hid_owner_" + "a" * 32,
            "owner_epoch": "a" * 32, "host_address": "AA:BB:CC:DD:EE:FF", "pid": 123}


def test_managed_mode_is_explicit_and_requires_ble():
    from iatest.mobile.bluetooth_hid import validate_config

    with pytest.raises(ValueError, match="explicit BLE"):
        validate_config({**settings(), "transport": "classic"})
    with pytest.raises(ValueError, match="Explicit BLE"):
        ManagedBLEOwner({**settings(), "lifecycle": "app"})


def test_managed_binding_never_adopts_another_host_or_missing_owner():
    with pytest.raises(CommandRejected, match="another paired host"):
        input_binding({**settings(), "host_address": "AA:BB:CC:DD:EE:00"}, lambda *args: json.dumps(binding()))
    with pytest.raises(CommandRejected, match="digitizer configuration differs"):
        input_binding({**settings(), "experimental_digitizer": True}, lambda *args: json.dumps(binding()))
    with pytest.raises(ProtocolError, match="binding differs"):
        input_binding(settings(), lambda *args: json.dumps({**binding(), "experimental_digitizer": "false"}))

    def missing(*args):
        raise RuntimeError("cat: No such file or directory")

    with pytest.raises(CommandRejected, match="Explicitly start"):
        input_binding(settings(), missing)
    with pytest.raises(ProtocolError, match="binding differs"):
        input_binding(settings(), lambda *args: json.dumps({**binding(), "socket": "foreign_socket"}))


@pytest.mark.parametrize("fault", [None, "wrong_epoch", "oversize", "truncated"])
def test_management_framing_and_owned_forward_cleanup(monkeypatch, fault):
    owner = ManagedBLEOwner(settings())
    calls = []

    def adb(*args, **kwargs):
        calls.append(args)
        return "47123" if args[:2] == ("forward", "tcp:0") else ""

    monkeypatch.setattr(owner, "adb", adb)

    class Channel:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def sendall(self, data):
            request = json.loads(data[4:])
            reply = {**request, "status": "acknowledged", "state": {"owner": {"running": True}}}
            if fault == "wrong_epoch":
                reply["owner_epoch"] = "b" * 32
            encoded = json.dumps(reply).encode()
            self.data = struct.pack("!I", 16385 if fault == "oversize" else len(encoded)) + encoded
            if fault == "truncated":
                self.data = self.data[:7]

        def recv(self, length):
            value, self.data = self.data[:min(length, 3)], self.data[min(length, 3):]
            return value

    monkeypatch.setattr("iatest.mobile.managed_ble.socket.create_connection", lambda *a, **kw: Channel())
    if fault:
        with pytest.raises(ProtocolError):
            owner._rpc(binding(), "status")
    else:
        assert owner._rpc(binding(), "status")["state"]["owner"]["running"]
    assert calls[-1] == ("forward", "--remove", "tcp:47123")
    with pytest.raises(ValueError, match="does not accept input"):
        owner._rpc(binding(), "send")


def test_managed_start_validates_lifetime_and_artifact_before_device_changes(tmp_path, monkeypatch):
    owner = ManagedBLEOwner(settings(), tmp_path)
    calls = []
    monkeypatch.setattr(owner, "adb", lambda *a, **kw: calls.append(a))
    with pytest.raises(ValueError, match="5–7200"):
        owner.start(0)
    (tmp_path / "manifest.json").write_text(json.dumps({"protocol": "iatest-bluetooth-hid/1"}))
    with pytest.raises(ValueError, match="Rebuild"):
        owner.start()
    assert calls == []


@pytest.mark.parametrize("seconds", [-1, 1, 301, True])
def test_pairing_window_bounds_reject_before_device_changes(tmp_path, monkeypatch, seconds):
    owner = ManagedBLEOwner(settings(), tmp_path)
    calls = []
    monkeypatch.setattr(owner, "adb", lambda *a, **kw: calls.append(a))
    with pytest.raises(ValueError, match="pairing window"):
        owner.start(pairing_window_seconds=seconds)
    assert calls == []


def test_pairing_preparation_requires_matching_artifact_capability(tmp_path, monkeypatch):
    owner = ManagedBLEOwner(settings(), tmp_path)
    hashes = {}
    for name in ("iatest-input.jar", "iatest-bluetooth-hid.apk"):
        (tmp_path / name).write_bytes(b"artifact")
        hashes[name] = hashlib.sha256(b"artifact").hexdigest()
    (tmp_path / "manifest.json").write_text(json.dumps({"protocol": "iatest-bluetooth-hid/1",
        "managed_ble_protocol": PROTOCOL, "sha256": hashes}))
    calls = []
    monkeypatch.setattr(owner, "adb", lambda *a, **kw: calls.append(a))
    with pytest.raises(ValueError, match="pairing preparation"):
        owner.start(pairing_window_seconds=120)
    assert calls == []


def test_native_pairing_window_never_authorizes_unverified_or_late_bonds(tmp_path, java_bin):
    harness = tmp_path / "PairingAcceptance.java"
    harness.write_text('''package dev.iatest.mobile.hid;
        public class PairingAcceptance {
            static final String HOST="AA:BB:CC:DD:EE:FF", RAW="40:11:22:33:44:55", OTHER="BB:11:22:33:44:55";
            static void check(boolean value){if(!value)throw new AssertionError();}
            public static void main(String[] args) {
                ManagedBlePairing ordinary=new ManagedBlePairing(HOST,0,0);
                check(!ordinary.accept(HOST,false,null,1,true));
                check(!ordinary.accept(RAW,true,OTHER,1,true));
                check(ordinary.accept(RAW,true,HOST,1,true)&&ordinary.verified(RAW,true)); // Restart resolves the same bonded identity without reopening pairing.
                check(!ordinary.accept(OTHER,false,null,1,true));
                check(ordinary.accept(HOST,true,null,1,false)&&ordinary.verified(HOST,true));
                check(!ordinary.verified(HOST,false));
                ManagedBlePairing waiting=new ManagedBlePairing(HOST,1000,10);
                check(!waiting.accept(RAW,false,null,1001,false));
                check(waiting.accept(RAW,false,null,1001,true));
                check(!waiting.verified(RAW,false)&&waiting.phase(1001).equals("waiting"));
                check(!waiting.accept(OTHER,true,OTHER,1002,true));
                check(!waiting.accept(RAW,true,null,1003,true));
                check(waiting.expired(11000));
                check(!waiting.accept(RAW,true,HOST,11000,true)); // A bond at/after expiry cannot revive the window.
                ManagedBlePairing active=new ManagedBlePairing(HOST,1000,10);
                check(active.accept(RAW,false,null,1001,true));
                check(active.accept(RAW,true,HOST,2000,false)); // Foreground lease is checked separately for input.
                check(active.verified(RAW,true)&&!active.verified(HOST,true));
                check(active.phase(12000).equals("bound")&&!active.expired(12000)&&active.remaining(12000)==0);
                check(!active.accept(OTHER,false,null,2001,true));
                check(!active.accept(OTHER,true,OTHER,2001,true));
                check(active.accept(OTHER,true,HOST,12000,true)); // Same verified identity may reconnect with a new raw alias.
                check(active.verified(OTHER,true)&&!active.verified(RAW,true));
                System.out.println("pairing gates passed");
            }
        }''')
    source = Path("iatest/mobile/native/android-hid/src/dev/iatest/mobile/hid/ManagedBlePairing.java").resolve()
    subprocess.run([str(java_bin / "javac"), "-d", str(tmp_path), str(source), str(harness)],
                   check=True, capture_output=True, timeout=15)
    result = subprocess.run([str(java_bin / "java"), "-cp", str(tmp_path), "dev.iatest.mobile.hid.PairingAcceptance"],
                            check=True, capture_output=True, text=True, timeout=10)
    assert "pairing gates passed" in result.stdout


@pytest.mark.parametrize("message,absent", [("No such file or directory", True), ("Permission denied", False)])
def test_unreachable_owner_is_stale_only_when_kernel_confirms_absence(monkeypatch, message, absent):
    owner = ManagedBLEOwner(settings())
    monkeypatch.setattr("iatest.mobile.managed_ble.input_binding", lambda *a: binding())

    def unreachable(*a, **kw):
        raise OSError("socket unavailable")

    def adb(*args, **kwargs):
        assert args == ("shell", "cat", "/proc/123/stat")
        raise RuntimeError(message)

    monkeypatch.setattr(owner, "_rpc", unreachable)
    monkeypatch.setattr(owner, "adb", adb)
    if absent:
        assert owner.status() == {"running": False, "stale_registry": True, "owner_epoch": "a" * 32,
                                  "business_runtime_verified": False}
    else:
        with pytest.raises(RuntimeError, match="Permission denied"):
            owner.status()


def test_boot_identity_proves_previous_owner_absent_even_when_pid_is_reused(monkeypatch):
    owner = ManagedBLEOwner(settings())
    calls = []

    def adb(*args):
        calls.append(args)
        assert args == ("shell", "cat", "/proc/sys/kernel/random/boot_id")
        return "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

    monkeypatch.setattr(owner, "adb", adb)
    assert owner._process_absent({**binding(), "boot_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"})
    assert len(calls) == 1


@pytest.mark.parametrize("same_generation", [True, False])
def test_failed_start_cleanup_never_stops_a_different_generation(monkeypatch, same_generation):
    owner = ManagedBLEOwner(settings())
    calls = []

    def adb(*args, **kwargs):
        assert kwargs["cleanup"] is True
        return json.dumps(binding())

    monkeypatch.setattr(owner, "adb", adb)
    monkeypatch.setattr(owner, "_rpc", lambda *a, **kw: calls.append((a, kw)))
    error = RuntimeError("registration failure")
    owner._failed_start_cleanup("a" * 32 if same_generation else "b" * 32, error)
    assert calls == ([((binding(), "stop"), {"cleanup": True})] if same_generation else [])


def test_native_managed_ble_gate_fences_identity_foreground_and_expiry(tmp_path, java_bin):
    harness = tmp_path / "ManagedGateAcceptance.java"
    harness.write_text('''package dev.iatest.mobile.hid;
        public class ManagedGateAcceptance {
            static final String A="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", B="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
            static void check(boolean value) {if(!value)throw new AssertionError();}
            static void denied(Runnable value) {
                try {value.run();}catch(SecurityException|IllegalStateException expected){return;}
                throw new AssertionError("Expected rejection");
            }
            public static void main(String[] args) {
                ManagedBleGate g=new ManagedBleGate(10001);
                denied(()->g.attach(2000,12,A,0));
                denied(()->g.attach(10001,0,A,0));
                g.attach(10001,12,A,0);check(!g.ready(0));
                g.heartbeat(10001,12,A,true,true,10);check(g.ready(10));
                denied(()->g.requireInput(10002,12,A,20));
                denied(()->g.requireInput(10001,13,A,20));
                denied(()->g.requireInput(10001,12,B,20));
                denied(()->g.attach(10001,13,B,20));
                check(!g.ready(9));
                g.heartbeat(10001,12,A,false,true,30);check(!g.ready(30)&&g.takeRelease(30));
                check(!g.takeRelease(31));
                g.heartbeat(10001,12,A,true,false,40);check(!g.ready(40));
                g.heartbeat(10001,12,A,true,true,50);g.requireInput(10001,12,A,51);
                check(g.ready(3049)&&!g.ready(3050));
                denied(()->g.requireInput(10001,12,A,3050));
                check(g.takeRelease(3050)&&!g.takeRelease(3051));
                g.attach(10001,13,B,3100);check(g.uiPid()==13&&!g.ready(3100));
                denied(()->g.heartbeat(10001,12,A,true,true,3101));
                g.heartbeat(10001,13,B,true,true,3110);check(g.ready(3110));
                g.detach(10001,13,B);check(!g.ready(3111)&&g.uiPid()==0&&g.takeRelease(3111));
                denied(()->g.heartbeat(10001,13,B,true,true,3112));
                denied(()->g.requireInput(10001,13,B,3112));
                System.out.println("managed gate passed");
            }
        }''')
    source = Path("iatest/mobile/native/android-hid/src/dev/iatest/mobile/hid/ManagedBleGate.java").resolve()
    subprocess.run([str(java_bin / "javac"), "-d", str(tmp_path), str(source), str(harness)],
                   check=True, capture_output=True, timeout=15)
    result = subprocess.run([str(java_bin / "java"), "-cp", str(tmp_path),
                             "dev.iatest.mobile.hid.ManagedGateAcceptance"],
                            check=True, capture_output=True, text=True, timeout=10)
    assert "managed gate passed" in result.stdout
