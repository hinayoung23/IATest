"""Explicit, finite ADB recovery. Never sends or replays HID input."""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import time
from pathlib import Path

from iatest.execution.control import RunCancelled, RunControl
from iatest.mobile.bluetooth_hid import PACKAGE, status, validate_config
from iatest.mobile.lease import DeviceLease, device_scope
from iatest.mobile.managed_ble import ManagedBLEOwner
from iatest.mobile.protocol import CommandRejected, ProtocolError

WATCH_PROTOCOL = "iatest-bluetooth-hid-watch/1"


def _watch_path(settings):
    return DeviceLease("bluetooth-hid-watch", settings["android_device_id"]).path.with_suffix(".json")


def _read_watch(settings):
    path = _watch_path(settings)
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    if len(raw) > 4096:
        raise ProtocolError("Bluetooth recovery watch record exceeds quota")
    value = json.loads(raw)
    if value.get("protocol") != WATCH_PROTOCOL or value.get("host_address") != settings["host_address"]:
        raise CommandRejected("Bluetooth recovery watch belongs to another target")
    return value


def _write_watch(settings, value):
    path = _watch_path(settings)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def cancel_watch(settings):
    """Fence recovery before an explicit owner stop, including while ADB is offline."""
    validate_config(settings)
    lock = DeviceLease("bluetooth-hid-watch-control", settings["android_device_id"])
    lock.acquire()
    try:
        value = _read_watch(settings)
        if value is None:
            return False
        value["cancelled"] = True
        _write_watch(settings, value)
        return True
    finally:
        lock.close()


class BluetoothHIDRecovery:
    def __init__(self, settings, artifact_dir="native/mobile/build/android-hid", control=None):
        validate_config(settings)
        if settings.get("transport") != "ble" or settings.get("lifecycle", "app") not in {"app", "adb_managed"}:
            raise ValueError("Recovery requires explicit BLE transport")
        self.settings = dict(settings)
        self._provided_control = control is not None
        self.control = control or RunControl(timeout_seconds=90)
        self.owner = ManagedBLEOwner({**settings, "lifecycle": "adb_managed"}, artifact_dir, self.control)
        self.pinned_hashes = None

    def _preflight(self):
        manifest = self.owner._artifacts()
        if self.pinned_hashes is not None and manifest["sha256"] != self.pinned_hashes:
            raise CommandRejected("Recovery artifacts changed during the authorized watch")
        self.owner._installed_uid(manifest["sha256"]["iatest-bluetooth-hid.apk"])
        self.pinned_hashes = dict(manifest["sha256"])

    def _ui(self):
        return status(self.settings["android_device_id"])

    def _available(self):
        return (
            self.owner.adb("shell", "getprop", "sys.boot_completed").strip() == "1"
            and self.owner.adb("shell", "am", "get-started-user-state", "0").strip() == "RUNNING_UNLOCKED"
            and self.owner.adb("shell", "settings", "get", "global", "bluetooth_on").strip() == "1"
        )

    @staticmethod
    def _idle(ui):
        if ui.get("running") and (ui.get("session_active") or ui.get("delivery_uncertain")):
            raise CommandRejected("End the active or uncertain HID session before radio recovery")

    def recover(self, lifetime_seconds=3600, *, restart_bluetooth=False):
        if restart_bluetooth is not True:
            raise ValueError("Explicit --restart-bluetooth consent is required")
        if type(lifetime_seconds) is not int or not 5 <= lifetime_seconds <= 7200:
            raise ValueError("Recovery lifetime must be 5–7200 seconds")
        began = time.monotonic()
        with device_scope({"platform": "android", "device_id": self.settings["android_device_id"]}):
            self._preflight()
            before = self._ui()
            self._idle(before)
            existing = self.owner.status()  # Reject another target before touching the radio.
            if self.settings.get("lifecycle", "app") == "app" and existing.get("running"):
                raise CommandRejected("Explicitly stop managed ownership before app lifecycle recovery")
            if not self._available():
                raise CommandRejected("Wait for Android boot, unlock and Bluetooth enabled before recovery")
            if (before.get("ble_lifecycle", "app") != self.settings.get("lifecycle", "app")
                    or before.get("experimental_digitizer", False) != self.settings.get("experimental_digitizer", False)):
                # Select the target lifecycle first. Cycling the radio before
                # ownership is selected can reconnect to the previous table.
                if self.settings.get("lifecycle", "app") == "adb_managed":
                    self.owner.start(lifetime_seconds)
                else:
                    self.owner.adb("shell", "am", "start", "-W", "-n", PACKAGE + "/.MainActivity",
                                   "--es", "transport", "ble", "--es", "ble_lifecycle", "app",
                                   "--ez", "experimental_digitizer", str(self.settings.get("experimental_digitizer", False)).lower())
            try:
                self.owner.adb("shell", "svc", "bluetooth", "disable")
                self.control.wait(4)
            finally:
                # Once requested, always attempt to restore the radio, even on
                # cancellation. This cleanup never sends keyboard/mouse input.
                self.owner.adb("shell", "svc", "bluetooth", "enable", cleanup=True)
            self.control.wait(4)
            remaining = lifetime_seconds - int(time.monotonic() - began)
            if remaining < 5:
                raise CommandRejected("Recovery authorization expired before service startup")
            if self.settings.get("lifecycle", "app") == "adb_managed":
                started = self.owner.start(remaining)
                expected_epoch = started["owner"]["epoch"]
            else:
                expected_epoch = None
                self.owner.adb("shell", "am", "start", "-W", "-n", PACKAGE + "/.MainActivity",
                               "--es", "transport", "ble", "--es", "ble_lifecycle", "app",
                               "--ez", "experimental_digitizer", str(self.settings.get("experimental_digitizer", False)).lower())
            try:
                until = time.monotonic() + min(30, self.control.remaining() or 30)
                while True:
                    ui = self._ui()
                    if (ui.get("running") and ui.get("foreground") and ui.get("input_ready")
                        and ui.get("ble_lifecycle", "app") == self.settings.get("lifecycle", "app")
                        and (expected_epoch is None or ui.get("ble_owner", {}).get("epoch") == expected_epoch)):
                        return {"recovered": True, "radio_restarted": True, "input_sent": False,
                                "business_runtime_verified": False, "state": ui}
                    if time.monotonic() >= until:
                        raise ProtocolError("Radio restored, but HID registration/subscriptions are not ready")
                    self.control.wait(.2)
            except BaseException as failure:
                if expected_epoch is not None:
                    try:
                        self._stop_owned(expected_epoch)
                    except Exception as cleanup_error:
                        failure.add_note("Recovery owner cleanup unconfirmed: " + type(cleanup_error).__name__)
                raise

    def _live(self):
        if self.settings.get("lifecycle", "app") == "adb_managed":
            value = self.owner.status()
            return value.get("running") and value.get("registered"), value.get("owner", {}).get("epoch"), value
        value = self._ui()
        # A freshly restarted foreground app can still have a stale iPhone cache.
        raw = json.loads(self.owner.adb("exec-out", "run-as", PACKAGE, "cat", "files/status.json"))
        return (value.get("running") and value.get("registered")
                and value.get("ble_lifecycle", "app") == "app" and value.get("transport") == "ble"
                and value.get("ble_recovery") != "failed"), raw.get("epoch"), value

    def _stop_owned(self, generation):
        control = RunControl(timeout_seconds=10)
        cleanup = ManagedBLEOwner(self.settings, self.owner.directory, control)
        while True:
            try:
                live = cleanup.status()
                if not live.get("running") or live.get("owner", {}).get("epoch") != generation:
                    return
                cleanup.stop(cancel_supervisor=False)
                return
            except RuntimeError as failure:
                # The explicit stop command may hold this same owner lock.
                # Wait for its result instead of treating the race as failure.
                if "already leased" not in str(failure) and str(failure) != "Managed BLE response ended early":
                    raise
                control.wait(.1)

    def watch(self, lifetime_seconds=3600, *, restart_bluetooth=False, emit=None):
        if restart_bluetooth is not True:
            raise ValueError("Explicit --restart-bluetooth consent is required")
        if type(lifetime_seconds) is not int or not 15 <= lifetime_seconds <= 7200:
            raise ValueError("Recovery watch lifetime must be 15–7200 seconds")
        if not self._provided_control:
            self.control = RunControl(timeout_seconds=lifetime_seconds + 30)
            self.owner.control = self.control
        lease = DeviceLease("bluetooth-hid-watch", self.settings["android_device_id"])
        lease.acquire()
        token, deadline = secrets.token_hex(16), time.monotonic() + lifetime_seconds
        record = {"protocol": WATCH_PROTOCOL, "host_address": self.settings["host_address"],
                  "token": token, "pid": os.getpid(), "cancelled": False}
        previous_check = self.control.cancellation_check
        generation = None
        recoveries = 0
        needs_refresh = False

        def cancelled():
            value = _read_watch(self.settings)
            return not value or value.get("token") != token or value.get("cancelled") is True

        def event(name, **details):
            if emit:
                emit({"event": name, "recoveries": recoveries, "input_sent": False, **details})

        try:
            control_lock = DeviceLease("bluetooth-hid-watch-control", self.settings["android_device_id"])
            control_lock.acquire()
            try:
                _write_watch(self.settings, record)
            finally:
                control_lock.close()
            self.control.cancellation_check = lambda: cancelled() or bool(previous_check and previous_check())
            self._preflight()
            live, generation, initial = self._live()
            if live and self.settings.get("lifecycle") == "adb_managed":
                # Reusing an owner must not extend its existing authorization.
                deadline = min(deadline, time.monotonic() + initial["owner"]["remaining_seconds"])
            event("watch_started", lifetime_seconds=max(0, int(deadline - time.monotonic())))
            while time.monotonic() < deadline:
                self.control.check()
                try:
                    available = self._available()
                    live, observed, _ = self._live() if available else (False, None, {})
                except (OSError, RuntimeError) as failure:
                    if isinstance(failure, RunCancelled):
                        raise
                    event("waiting_for_device", reason=type(failure).__name__)
                    self.control.wait(2)
                    continue
                if not available:
                    if self.settings.get("lifecycle", "app") == "app":
                        needs_refresh = True
                    self.control.wait(2)
                    continue
                changed = generation is not None and observed != generation
                if not live or changed or needs_refresh:
                    if live and changed and self.settings.get("lifecycle") == "adb_managed":
                        raise CommandRejected("Managed owner changed outside this watch; refusing takeover")
                    if recoveries >= 10 or deadline - time.monotonic() < 15:
                        raise CommandRejected("Finite recovery budget exhausted")
                    try:
                        result = self.recover(int(deadline - time.monotonic()), restart_bluetooth=True)
                    except RuntimeError as failure:
                        # An in-flight input session owns the Android lease.
                        # Wait for its caller to close it; never replay it here.
                        if "already leased" not in str(failure):
                            raise
                        self.control.wait(1)
                        continue
                    recoveries += 1
                    needs_refresh = False
                    _, generation, _ = self._live()
                    event("service_recovered", generation=generation,
                          input_ready=result["state"]["input_ready"])
                self.control.wait(1)
            event("watch_expired")
        except RunCancelled:
            if not cancelled():
                raise
            event("watch_cancelled")
        finally:
            self.control.cancellation_check = previous_check
            try:
                if self.settings.get("lifecycle") == "adb_managed" and generation is not None:
                    self._stop_owned(generation)
            finally:
                control_lock = DeviceLease("bluetooth-hid-watch-control", self.settings["android_device_id"])
                control_lock.acquire()
                try:
                    value = _read_watch(self.settings)
                    if value and value.get("token") == token:
                        _watch_path(self.settings).unlink(missing_ok=True)
                finally:
                    control_lock.close()
                    lease.close()
        return {"stopped": True, "recoveries": recoveries, "input_sent": False}
