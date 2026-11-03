"""Radio recovery must preserve authorization, session fences and finite deadlines."""

from types import SimpleNamespace

import pytest

from iatest.execution.control import RunCancelled, RunControl
from iatest.mobile import bluetooth_recovery as recovery
from iatest.mobile.protocol import CommandRejected


def settings():
    return {"android_device_id": "recovery-fixture", "host_address": "AA:BB:CC:DD:EE:FF",
            "transport": "ble", "lifecycle": "adb_managed"}


def prepared(monkeypatch):
    control = RunControl(timeout_seconds=90)
    monkeypatch.setattr(control.__class__, "wait", lambda self, seconds: self.check())
    controller = recovery.BluetoothHIDRecovery(settings(), control=control)
    calls = []
    monkeypatch.setattr(controller, "_preflight", lambda: calls.append("preflight"))
    monkeypatch.setattr(controller, "_available", lambda: True)
    monkeypatch.setattr(controller, "_ui", lambda: {"running": True, "foreground": True, "input_ready": True,
                                                   "ble_lifecycle": "adb_managed", "ble_owner": {"epoch": "replacement"}})
    monkeypatch.setattr(controller.owner, "status", lambda: {"running": False})
    monkeypatch.setattr(controller.owner, "adb", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(controller.owner, "start", lambda seconds: (calls.append(("start", seconds))
                                                                    or {"owner": {"epoch": "replacement"}}))
    return controller, calls


def test_recovery_requires_consent_before_any_device_work(monkeypatch):
    controller, calls = prepared(monkeypatch)
    with pytest.raises(ValueError, match="consent"):
        controller.recover()
    assert calls == []


@pytest.mark.parametrize("flag", ["session_active", "delivery_uncertain"])
def test_recovery_rejects_active_or_unknown_input_before_radio_reset(monkeypatch, flag):
    controller, calls = prepared(monkeypatch)
    monkeypatch.setattr(controller, "_ui", lambda: {"running": True, flag: True})
    with pytest.raises(CommandRejected, match="active or uncertain"):
        controller.recover(restart_bluetooth=True)
    assert calls == ["preflight"]


def test_cancel_during_radio_reset_still_restores_radio_and_never_starts_input(monkeypatch):
    controller, calls = prepared(monkeypatch)

    def cancel(self, seconds):
        raise RunCancelled("cancelled during radio transition")

    monkeypatch.setattr(RunControl, "wait", cancel)
    with pytest.raises(RunCancelled):
        controller.recover(restart_bluetooth=True)
    assert calls[-1] == (("shell", "svc", "bluetooth", "enable"), {"cleanup": True})
    assert not any(isinstance(c, tuple) and c[0] == "start" for c in calls)


def test_recovery_orders_real_disconnect_before_rebuild_without_claiming_business(monkeypatch):
    controller, calls = prepared(monkeypatch)
    result = controller.recover(restart_bluetooth=True)
    assert calls[1:3] == [(("shell", "svc", "bluetooth", "disable"), {}),
                          (("shell", "svc", "bluetooth", "enable"), {"cleanup": True})]
    assert calls[3][0] == "start" and 5 <= calls[3][1] <= 3600
    assert result["recovered"] and not result["input_sent"] and not result["business_runtime_verified"]


def test_watch_cancel_is_bound_to_target_and_does_not_require_adb(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery, "_watch_path", lambda settings: tmp_path / "watch.json")
    recovery._write_watch(settings(), {"protocol": recovery.WATCH_PROTOCOL,
                                     "host_address": settings()["host_address"], "token": "a" * 32})
    with pytest.raises(CommandRejected, match="another target"):
        recovery.cancel_watch({**settings(), "host_address": "AA:BB:CC:DD:EE:00"})
    assert recovery.cancel_watch(settings())
    assert recovery._read_watch(settings())["cancelled"] is True


def test_watch_keeps_old_deadline_and_explicit_cancel_prevents_resurrection(tmp_path, monkeypatch):
    controller, _ = prepared(monkeypatch)
    monkeypatch.setattr(recovery, "_watch_path", lambda settings: tmp_path / "watch.json")
    live = iter([(True, "original", {"owner": {"remaining_seconds": 40}}),
                 (False, None, {}), (True, "replacement", {})])
    monkeypatch.setattr(controller, "_live", lambda: next(live))
    attempts = []

    def restore(seconds, **kwargs):
        attempts.append(seconds)
        return {"state": {"input_ready": True}}

    monkeypatch.setattr(controller, "recover", restore)
    monkeypatch.setattr(recovery, "ManagedBLEOwner", lambda *a, **kw: SimpleNamespace(status=lambda: {"running": False}))

    def wait(self, seconds):
        recovery.cancel_watch(settings())
        self.check()

    monkeypatch.setattr(RunControl, "wait", wait)
    events = []
    result = controller.watch(90, restart_bluetooth=True, emit=events.append)
    assert len(attempts) == 1 and 5 <= attempts[0] <= 40
    assert result["stopped"] and result["recoveries"] == 1
    assert events[-1]["event"] == "watch_cancelled"
    assert not (tmp_path / "watch.json").exists()


def test_recovery_rejects_changed_artifacts_before_installed_apk_or_radio(monkeypatch):
    controller = recovery.BluetoothHIDRecovery(settings())
    controller.pinned_hashes = {"iatest-input.jar": "original"}
    monkeypatch.setattr(controller.owner, "_artifacts", lambda: {"sha256": {"iatest-input.jar": "changed"}})
    monkeypatch.setattr(controller.owner, "_installed_uid", lambda *a: pytest.fail("APK lookup must not run"))
    with pytest.raises(CommandRejected, match="artifacts changed"):
        controller._preflight()


def test_owner_stop_fences_watch_even_when_adb_is_offline(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery, "_watch_path", lambda settings: tmp_path / "watch.json")
    recovery._write_watch(settings(), {"protocol": recovery.WATCH_PROTOCOL,
                                     "host_address": settings()["host_address"], "token": "a" * 32})
    owner = recovery.ManagedBLEOwner(settings())

    def offline(*a, **kw):
        assert recovery._read_watch(settings())["cancelled"] is True
        raise RuntimeError("device offline")

    monkeypatch.setattr(owner, "adb", offline)
    with pytest.raises(RuntimeError, match="offline"):
        owner.stop()


def test_watch_does_not_adopt_or_stop_an_externally_replaced_owner(tmp_path, monkeypatch):
    controller, _ = prepared(monkeypatch)
    monkeypatch.setattr(recovery, "_watch_path", lambda settings: tmp_path / "watch.json")
    states = iter([(True, "ours", {"owner": {"remaining_seconds": 40}}), (True, "other", {})])
    monkeypatch.setattr(controller, "_live", lambda: next(states))
    monkeypatch.setattr(recovery, "ManagedBLEOwner", lambda *a, **kw: SimpleNamespace(
        status=lambda: {"running": True, "owner": {"epoch": "other"}},
        stop=lambda **kw: pytest.fail("Do not stop another owner generation")))
    with pytest.raises(CommandRejected, match="outside this watch"):
        controller.watch(90, restart_bluetooth=True)
    assert not (tmp_path / "watch.json").exists()


def test_watch_cleanup_waits_for_an_explicit_stop_holding_owner_lock(monkeypatch):
    controller, _ = prepared(monkeypatch)
    states = iter([{"running": True, "owner": {"epoch": "ours"}}, {"running": False}])
    attempts = []

    def busy(**kwargs):
        attempts.append(kwargs)
        raise RuntimeError("Device is already leased by another test or manual session")

    monkeypatch.setattr(recovery, "ManagedBLEOwner", lambda *a, **kw: SimpleNamespace(
        status=lambda: next(states), stop=busy))
    controller._stop_owned("ours")
    assert attempts == [{"cancel_supervisor": False}]


def test_watch_cleanup_waits_for_socket_close_during_external_stop(monkeypatch):
    from iatest.mobile.protocol import ProtocolError

    controller, _ = prepared(monkeypatch)
    calls = []

    def stopping():
        calls.append(True)
        if len(calls) == 1:
            raise ProtocolError("Managed BLE response ended early")
        return {"running": False}

    monkeypatch.setattr(recovery, "ManagedBLEOwner", lambda *a, **kw: SimpleNamespace(status=stopping))
    controller._stop_owned("ours")
    assert len(calls) == 2


def test_cancel_after_new_owner_start_cleans_only_that_generation(monkeypatch):
    controller, calls = prepared(monkeypatch)
    monkeypatch.setattr(controller, "_ui", lambda: {"running": True, "input_ready": False, "ble_lifecycle": "adb_managed"})
    waits = []
    cleaned = []

    def wait(self, seconds):
        waits.append(seconds)
        if len(waits) == 3:
            raise RunCancelled("cancel after owner startup")

    monkeypatch.setattr(RunControl, "wait", wait)
    monkeypatch.setattr(controller, "_stop_owned", cleaned.append)
    with pytest.raises(RunCancelled):
        controller.recover(restart_bluetooth=True)
    assert cleaned == ["replacement"]
    assert calls[2] == (("shell", "svc", "bluetooth", "enable"), {"cleanup": True})


def test_lifecycle_handoff_prevents_old_app_services_before_radio_returns(monkeypatch):
    controller, calls = prepared(monkeypatch)
    states = iter([{"running": True, "ble_lifecycle": "app"},
                   {"running": True, "foreground": True, "input_ready": True,
                    "ble_lifecycle": "adb_managed", "ble_owner": {"epoch": "replacement"}}])
    monkeypatch.setattr(controller, "_ui", lambda: next(states))
    controller.recover(restart_bluetooth=True)
    disable = calls.index((("shell", "svc", "bluetooth", "disable"), {}))
    enable = calls.index((("shell", "svc", "bluetooth", "enable"), {"cleanup": True}))
    starts = [i for i, call in enumerate(calls) if isinstance(call, tuple) and call[0] == "start"]
    assert len(starts) == 2 and starts[0] < disable < enable < starts[1]


def test_app_watch_recovers_radio_gap_even_when_ui_process_did_not_change(tmp_path, monkeypatch):
    cfg = {**settings(), "lifecycle": "app"}
    controller = recovery.BluetoothHIDRecovery(cfg, control=RunControl(timeout_seconds=90))
    monkeypatch.setattr(recovery, "_watch_path", lambda settings: tmp_path / "watch.json")
    monkeypatch.setattr(controller, "_preflight", lambda: None)
    available = iter([False, True])
    monkeypatch.setattr(controller, "_available", lambda: next(available))
    monkeypatch.setattr(controller, "_live", lambda: (True, "same-ui-process", {}))
    attempts = []
    monkeypatch.setattr(controller, "recover", lambda *a, **kw: (attempts.append(True) or {"state": {"input_ready": True}}))
    waits = []

    def wait(self, seconds):
        waits.append(seconds)
        if len(waits) > 1:
            recovery.cancel_watch(cfg)
        self.check()

    monkeypatch.setattr(RunControl, "wait", wait)
    result = controller.watch(90, restart_bluetooth=True)
    assert result["recoveries"] == 1 and attempts == [True]
