"""Android Bluetooth HID over explicit shell-permission broadcasts, without root."""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import time
import uuid

from iatest.engines.http import validate
from iatest.execution.control import RunControl
from iatest.mobile.devices import command
from iatest.mobile.lease import DeviceLease
from iatest.mobile.protocol import CommandRejected, DeliveryUnknown, ProtocolError
from iatest.mobile.usb_hid import CONFIG_SCHEMA as USB_CONFIG_SCHEMA
from iatest.mobile.usb_hid import scheduled_reports

PROTOCOL = "iatest-bluetooth-hid/1"
PACKAGE = "dev.iatest.mobile.hid"
CONFIG_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["android_device_id", "host_address"],
    "properties": {
        "android_device_id": {"type": "string", "pattern": "^[A-Za-z0-9_.:-]{1,200}$"},
        "host_address": {
            "type": "string",
            "pattern": "^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$",
        },
        "pointer_calibration": USB_CONFIG_SCHEMA["properties"]["pointer_calibration"],
        "transport": {"enum": ["classic", "ble"]},
        "lifecycle": {"enum": ["app", "adb_managed"]},
        "experimental_digitizer": {"type": "boolean"},
    },
}


def validate_config(settings):
    validate(settings, CONFIG_SCHEMA)
    if settings.get("lifecycle") == "adb_managed" and settings.get("transport") != "ble":
        raise ValueError("Managed BLE ownership requires explicit BLE transport")
    if settings.get("experimental_digitizer") and settings.get("transport") != "ble":
        raise ValueError("Experimental digitizer requires explicit BLE transport")


def status(android_device_id, *, list_hosts=False):
    if not re.fullmatch("[A-Za-z0-9_.:-]{1,200}", android_device_id):
        raise ValueError("Invalid Android device identifier")
    raw = command(
        [
            shutil.which("adb") or "adb",
            "-s",
            android_device_id,
            "exec-out",
            "run-as",
            PACKAGE,
            "cat",
            "files/status.json",
        ],
        timeout=5,
    )
    if len(raw) > 16384:
        raise ProtocolError("Bluetooth HID status exceeds bounds")
    state = json.loads(raw)
    try:
        pids = command(
            [
                shutil.which("adb") or "adb",
                "-s",
                android_device_id,
                "shell",
                "pidof",
                PACKAGE,
            ],
            timeout=5,
        ).split()
    except RuntimeError:
        pids = []
    running = len(pids) == 1 and pids[0] == str(state.get("pid"))
    result = {
        key: state.get(key)
        for key in (
            "sdk",
            "foreground",
            "proxy_available",
            "registration_requested",
            "registered",
            "connected",
            "error",
            "transport",
            "input_ready",
            "experimental_digitizer",
            "digitizer_ready",
            "ble_recovery",
            "ble_lifecycle",
            "ble_owner",
            "session_active",
            "delivery_uncertain",
        )
    }
    result["business_runtime_verified"] = False
    result["running"] = running
    if not running:
        result.update(
            foreground=False, registered=False, connected=False, input_ready=False
        )
    if list_hosts:
        result["paired_hosts"] = state.get("paired_hosts", [])[:32]
        result["host_address"] = state.get("host_address")
    return result


class AndroidBluetoothHIDSession:
    def __init__(self, settings, control=None):
        validate_config(settings)
        self.settings, self.control = (
            dict(settings),
            control or RunControl(timeout_seconds=60),
        )
        self.session, self.sequence = uuid.uuid4().hex, 0
        self.closed = self.failed = False
        self.leases = []
        try:
            for platform, identity in (
                ("android", settings["android_device_id"]),
                ("bluetooth-hid", settings["host_address"]),
            ):
                lease = DeviceLease(platform, identity)
                lease.acquire()
                self.leases.append(lease)
            owner_binding = None
            owner_arguments = []
            if self.settings.get("lifecycle") == "adb_managed":
                from iatest.mobile.managed_ble import input_binding

                owner_binding = input_binding(self.settings, self.adb)
                owner_arguments = ["--es", "ble_lifecycle", "adb_managed", "--es", "ble_owner_socket", owner_binding["socket"],
                                   "--es", "ble_owner_epoch", owner_binding["owner_epoch"]]
            elif self.settings.get("lifecycle") == "app":
                owner_arguments = ["--es", "ble_lifecycle", "app"]
            owner_arguments += ["--ez", "experimental_digitizer", str(self.settings.get("experimental_digitizer", False)).lower()]
            self.adb(
                "shell",
                "am",
                "start",
                "-W",
                "-n",
                PACKAGE + "/.MainActivity",
                "--es",
                "transport",
                self.settings.get("transport", "classic"),
                *owner_arguments,
            )
            if owner_binding is not None:
                from iatest.mobile.managed_ble import ManagedBLEOwner

                ManagedBLEOwner(self.settings, control=self.control)._rpc(owner_binding, "publish")
            pids = self.adb("shell", "pidof", PACKAGE).split()
            if len(pids) != 1 or not pids[0].isdigit():
                raise ProtocolError("Expected one current Android HID process")
            registration_budget = 15 if self.settings.get("transport") == "ble" else 5
            deadline = time.monotonic() + min(registration_budget, self.control.remaining() or registration_budget)
            while True:
                state = json.loads(
                    self.adb("exec-out", "run-as", PACKAGE, "cat", "files/status.json")
                )
                if (
                    state.get("foreground")
                    and state.get("registered")
                    and str(state.get("pid")) == pids[0]
                    and state.get("transport")
                    == self.settings.get("transport", "classic")
                    and state.get("ble_lifecycle", "app") == self.settings.get("lifecycle", "app")
                    and (owner_binding is None or state.get("ble_owner_epoch") == owner_binding["owner_epoch"]
                         and state.get("ble_owner", {}).get("epoch") == owner_binding["owner_epoch"])
                    and state.get("ble_recovery") not in {"discover", "disconnect", "connect", "announce", "subscribe"}
                    and (
                        self.settings.get("transport") != "ble"
                        or state.get("input_ready")
                        or state.get("ble_recovery") == "failed"
                    )
                ):
                    break
                if time.monotonic() >= deadline:
                    raise ProtocolError(
                        "Current foreground HID registration is not ready"
                    )
                self.control.wait(0.05)
            if (
                not state.get("foreground")
                or not state.get("registered")
                or not re.fullmatch("[a-f0-9]{32}", state.get("epoch", ""))
            ):
                raise ProtocolError("Android foreground HID registration is not ready")
            self.epoch = state["epoch"]
            if state.get("ble_recovery") == "failed":
                raise CommandRejected("BLE startup recovery failed before input; reconnect the host Bluetooth radio")
            reply = self._request("hello")
            capabilities = set(reply.get("capabilities", []))
            if (
                not {"keyboard", "relative_mouse"}
                <= capabilities
                <= {"keyboard", "relative_mouse", "absolute_mouse", "digitizer"}
            ):
                raise ProtocolError("Bluetooth HID capability handshake differs")
            self.capabilities = capabilities
            self.max_touch_points = reply.get("max_touch_points", 0)
            if "digitizer" in capabilities and not self.settings.get("experimental_digitizer"):
                raise ProtocolError("Experimental digitizer was not explicitly enabled")
            if "digitizer" in capabilities and (type(self.max_touch_points) is not int or self.max_touch_points != 5):
                raise ProtocolError("Bluetooth digitizer contact negotiation differs")
            self.reconnect_supported = reply.get("controlled_reconnect") is True
        except BaseException as error:
            if getattr(self, "epoch", None) and self.sequence:
                try:
                    self._request("close", cleanup=True)
                except (
                    OSError,
                    RuntimeError,
                    ValueError,
                    subprocess.TimeoutExpired,
                ) as cleanup_error:
                    error.add_note(
                        "Bluetooth HID cleanup unconfirmed: "
                        + type(cleanup_error).__name__
                    )
            for lease in reversed(self.leases):
                lease.close()
            raise

    def adb(self, *args, cleanup=False):
        return command(
            [
                shutil.which("adb") or "adb",
                "-s",
                self.settings["android_device_id"],
                *args,
            ],
            timeout=min(8, self.control.remaining() or 8) if not cleanup else 2,
            control=None if cleanup else self.control,
        )

    def _request(self, op, reports=None, *, cleanup=False):
        if not cleanup:
            self.control.check()
        self.sequence += 1
        request = {
            "protocol": PROTOCOL,
            "epoch": self.epoch,
            "session": self.session,
            "sequence": self.sequence,
            "operation_id": uuid.uuid4().hex,
            "host_address": self.settings["host_address"],
            "op": op,
            "transport": self.settings.get("transport", "classic"),
            "deadline_ms": 1000
            if cleanup
            else min(5000, max(1, int((self.control.remaining() or 5) * 1000))),
        }
        if reports is not None:
            request["reports"] = reports
        encoded = base64.b64encode(
            json.dumps(request, separators=(",", ":")).encode()
        ).decode()
        dispatched = False
        try:
            # DUMP is a signature permission available to the ADB shell, not
            # granted to arbitrary apps. Payload is a shell-safe base64 token.
            dispatched = op == "execute"
            self.adb(
                "shell",
                "am",
                "broadcast",
                "-n",
                PACKAGE + "/.ControlReceiver",
                "--es",
                "payload",
                encoded,
                cleanup=cleanup,
            )
            deadline = time.monotonic() + (
                2 if cleanup else min(8, self.control.remaining() or 8)
            )
            while True:
                if not cleanup:
                    self.control.check()
                try:
                    raw = self.adb(
                        "exec-out",
                        "run-as",
                        PACKAGE,
                        "cat",
                        "files/replies/" + request["operation_id"] + ".json",
                        cleanup=cleanup,
                    )
                except RuntimeError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Bluetooth HID reply not observed")
                    if cleanup:
                        time.sleep(0.05)
                    else:
                        self.control.wait(0.05)
                    continue
                if len(raw) > 4096:
                    raise ProtocolError("Bluetooth HID reply exceeds bounds")
                reply = json.loads(raw)
                break
            if any(
                reply.get(key) != request[key]
                for key in (
                    "protocol",
                    "epoch",
                    "session",
                    "sequence",
                    "operation_id",
                    "host_address",
                    "transport",
                )
            ):
                raise ProtocolError("Bluetooth HID response binding differs")
            if reply.get("status") == "rejected":
                dispatched = False
                raise CommandRejected(
                    "Bluetooth HID rejected before input: "
                    + str(reply.get("error", ""))[:300]
                )
            if reply.get("status") != "acknowledged":
                raise ProtocolError("Bluetooth HID input unconfirmed")
            return reply
        except BaseException as error:
            if dispatched:
                self.failed = True
                raise DeliveryUnknown(
                    "Bluetooth HID delivery unknown; never replay"
                ) from error
            raise

    def execute(self, value):
        if self.closed or self.failed:
            raise ProtocolError("Bluetooth HID session requires reconciliation")
        schedule = scheduled_reports(value)
        if value["op"] == "touch" and len(schedule) > 64:
            raise ValueError("Bluetooth digitizer paths allow at most 64 frames, ending with all contacts released")
        if value["op"] == "touch":
            if "digitizer" not in self.capabilities:
                raise ValueError("Paired host did not subscribe to the experimental digitizer endpoint")
            for _, _, data in schedule:
                if data[1] > self.max_touch_points or any(data[3+i*6] >= self.max_touch_points for i in range(data[1])):
                    raise ValueError("Bluetooth digitizer supports contact IDs 0–4 and at most five records per frame")
        if value["op"] == "move" and value.get("wheel"):
            # iOS filters isolated wheel reports. Preserve total X/Y/Wheel
            # deltas while delivering wheel notches at a steady cadence.
            ticks = abs(value["wheel"])
            direction = 1 if value["wheel"] > 0 else -1
            schedule = [
                (
                    index * 16,
                    "mouse",
                    bytes(
                        [
                            0,
                            value["dx"] & 255 if index == 0 else 0,
                            value["dy"] & 255 if index == 0 else 0,
                            direction & 255,
                        ]
                    ),
                )
                for index in range(ticks)
            ]
            schedule.append((ticks * 16, "mouse", bytes(4)))
        elif value["op"] == "point" and value.get("button"):
            # iOS uses the absolute report for position but requires mouse
            # buttons through the relative mouse report. Re-establish position
            # even when iOS moved/hidden the cursor after the same prior report.
            _, kind, pressed = schedule[0]
            from iatest.mobile.usb_hid import reports

            down, up = reports({"op": "click", "button": value["button"]})
            nudge = bytearray(pressed)
            nudge[0] = 0
            x = value["x"]
            nudge[1:3] = (x + 256 if x <= 32511 else x - 256).to_bytes(2, "little")
            schedule = [(0, kind, bytes(nudge)), (40, kind, bytes([0]) + pressed[1:]), (90, *down), (170, *up)]
        elif value["op"] == "click":
            schedule[-1] = (80, schedule[-1][1], schedule[-1][2])
        if any(
            {"mouse": "relative_mouse"}.get(kind, kind) not in self.capabilities
            for _, kind, _ in schedule
        ):
            raise ValueError(
                "Android Bluetooth HID requires a negotiated pointer endpoint; relative mouse and keyboard are always required"
            )
        if schedule[-1][0] / 1000 >= min(5, self.control.remaining() or 5):
            raise ValueError("Bluetooth report path exceeds remaining time")
        reports = [
            {
                "t_ms": stamp,
                "id": {"keyboard": 1, "mouse": 3, "absolute_mouse": 2, "digitizer": 4}[kind],
                "data": base64.b64encode(
                    data[:5] if kind == "absolute_mouse" else data[1:32] if kind == "digitizer" else data
                ).decode(),
            }
            for stamp, kind, data in schedule
        ]
        started = time.time() * 1000
        # Split only at released-key boundaries. Each batch has its own binding
        # and deadline; an uncertain batch stops the remaining text immediately.
        for offset in range(0, len(reports), 64):
            batch = reports[offset : offset + 64]
            origin = batch[0]["t_ms"]
            batch = [{**item, "t_ms": item["t_ms"] - origin} for item in batch]
            reply = self._request("execute", batch)
            if reply.get("reports_queued") != len(batch):
                self.failed = True
                raise DeliveryUnknown("Bluetooth report count differs; never replay")
        return {
            "backend": "android_bluetooth_hid",
            "dispatch": "acknowledged",
            "reports_queued": len(reports),
            "started_timestamp_ms": started,
            "postcondition_verified": False,
            "input_scope": "explicit paired Bluetooth host; negotiated HID report endpoints",
        }

    def reconnect(self):
        """Release the confirmed session and request this exact BLE peer to reconnect."""
        if self.closed or self.failed:
            raise ProtocolError("Bluetooth HID session requires reconciliation")
        if self.settings.get("transport", "classic") != "ble":
            raise ValueError("Controlled reconnect requires BLE transport")
        if not self.reconnect_supported:
            raise ValueError("Rebuild the Android bridge for controlled BLE reconnect")
        self._request("reconnect")
        self.closed = True
        for lease in reversed(self.leases):
            lease.close()
        self.leases.clear()
        return {"status": "reconnect_requested", "business_postcondition_verified": False}

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self._request("close", cleanup=True)
        finally:
            for lease in reversed(self.leases):
                lease.close()
