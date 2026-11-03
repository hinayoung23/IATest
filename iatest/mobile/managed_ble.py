"""Explicit, finite ADB ownership of the Android bridge GATT database."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import shutil
import socket
import struct
import time
from pathlib import Path

from iatest.execution.control import RunControl
from iatest.mobile.devices import command
from iatest.mobile.lease import DeviceLease
from iatest.mobile.protocol import CommandRejected, ProtocolError

PROTOCOL = "iatest-managed-ble/1"
PAIRING_PROTOCOL = "iatest-managed-ble-pairing/1"
PACKAGE = "dev.iatest.mobile.hid"
REGISTRY = "/data/local/tmp/iatest-managed-ble.json"
MAX_FRAME = 16384
BOOT_ID = r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}"


def read_binding(adb):
    try:
        raw = adb("shell", "cat", REGISTRY)
    except RuntimeError as error:
        if "No such file" in str(error):
            raise CommandRejected("Explicitly start the ADB BLE owner before input") from error
        raise
    if len(raw) > MAX_FRAME:
        raise ProtocolError("Managed BLE registry exceeds quota")
    value = json.loads(raw)
    if (
        value.get("protocol") != PROTOCOL
        or not re.fullmatch(r"iatest_hid_owner_[a-f0-9]{32}", value.get("socket", ""))
        or not re.fullmatch(r"[a-f0-9]{32}", value.get("owner_epoch", ""))
        or not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", value.get("host_address", ""))
        or type(value.get("pid")) is not int or value["pid"] < 1
        or ("boot_id" in value and (not isinstance(value["boot_id"], str) or not re.fullmatch(BOOT_ID, value["boot_id"])))
        or ("experimental_digitizer" in value and type(value["experimental_digitizer"]) is not bool)
    ):
        raise ProtocolError("Managed BLE owner binding differs")
    return value


def input_binding(settings, adb):
    value = read_binding(adb)
    if value["host_address"] != settings["host_address"]:
        raise CommandRejected("Managed BLE owner is bound to another paired host")
    if value.get("experimental_digitizer", False) != settings.get("experimental_digitizer", False):
        raise CommandRejected("Managed BLE digitizer configuration differs; explicitly stop and recreate ownership")
    return value


class ManagedBLEOwner:
    def __init__(self, settings, artifact_dir="native/mobile/build/android-hid", control=None):
        from iatest.mobile.bluetooth_hid import validate_config

        validate_config(settings)
        if settings.get("transport") != "ble" or settings.get("lifecycle") != "adb_managed":
            raise ValueError("Explicit BLE transport and adb_managed lifecycle required")
        self.settings = dict(settings)
        self.directory = Path(artifact_dir).expanduser().resolve()
        self.control = control or RunControl(timeout_seconds=90)

    def adb(self, *arguments, timeout=15, cleanup=False):
        return command(
            [shutil.which("adb") or "adb", "-s", self.settings["android_device_id"], *arguments],
            timeout=min(timeout, self.control.remaining() or timeout) if not cleanup else 3,
            control=None if cleanup else self.control,
        )

    @staticmethod
    def _receive(channel, length):
        output = bytearray()
        while len(output) < length:
            part = channel.recv(length - len(output))
            if not part:
                raise ProtocolError("Managed BLE response ended early")
            output.extend(part)
        return bytes(output)

    def _rpc(self, binding, op, *, cleanup=False):
        if op not in {"status", "stop", "publish"}:
            raise ValueError("Management channel does not accept input")
        identity = secrets.token_hex(16)
        request = {"protocol": PROTOCOL, "owner_epoch": binding["owner_epoch"], "request_id": identity, "op": op}
        port = self.adb("forward", "tcp:0", "localabstract:" + binding["socket"], cleanup=cleanup).strip()
        if not port.isdigit() or not 0 < int(port) < 65536:
            raise ProtocolError("ADB owner forwarding port differs")
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=3) as channel:
                data = json.dumps(request, separators=(",", ":")).encode()
                channel.sendall(struct.pack("!I", len(data)) + data)
                length = struct.unpack("!I", self._receive(channel, 4))[0]
                if not 0 < length <= MAX_FRAME:
                    raise ProtocolError("Managed BLE response exceeds quota")
                reply = json.loads(self._receive(channel, length))
            if any(reply.get(key) != request[key] for key in ("protocol", "owner_epoch", "request_id")):
                raise ProtocolError("Managed BLE response binding differs")
            if reply.get("status") != "acknowledged":
                raise CommandRejected("Managed BLE management rejected: " + str(reply.get("error", ""))[:300])
            return reply
        finally:
            self.adb("forward", "--remove", "tcp:" + port, cleanup=True)

    def status(self):
        try:
            binding = input_binding(self.settings, self.adb)
        except CommandRejected as error:
            if "Explicitly start" in str(error):
                return {"running": False, "business_runtime_verified": False}
            raise
        try:
            result = self._rpc(binding, "status")["state"]
        except (OSError, RuntimeError, ValueError):
            if not self._process_absent(binding):
                raise
            return {"running": False, "stale_registry": True, "owner_epoch": binding["owner_epoch"],
                    "business_runtime_verified": False}
        result["running"] = result["owner"]["running"]
        result["business_runtime_verified"] = False
        return result

    def _process_absent(self, binding):
        # A verified boot change or kernel ENOENT proves absence. Other errors
        # and a reused PID within the same boot prevent takeover. Native startup
        # still holds the device-wide file lock before replacing the old record.
        if binding.get("boot_id"):
            current = self.adb("shell", "cat", "/proc/sys/kernel/random/boot_id").strip()
            if not re.fullmatch(BOOT_ID, current):
                raise ProtocolError("Android boot identity is unavailable")
            if current != binding["boot_id"]:
                return True  # A previous-boot PID cannot own the current service.
        try:
            self.adb("shell", "cat", "/proc/" + str(binding["pid"]) + "/stat")
        except RuntimeError as error:
            if "No such file or directory" in str(error):
                return True
            raise
        return False

    def _failed_start_cleanup(self, generation, failure):
        try:
            current = read_binding(lambda *args: self.adb(*args, cleanup=True))
            if current["owner_epoch"] == generation:
                self._rpc(current, "stop", cleanup=True)
        except CommandRejected as error:
            if "Explicitly start" not in str(error):
                failure.add_note("Managed BLE startup cleanup unconfirmed: " + type(error).__name__)
        except Exception as error:
            failure.add_note("Managed BLE startup cleanup unconfirmed: " + type(error).__name__)

    def _artifacts(self):
        manifest = json.loads((self.directory / "manifest.json").read_text())
        if manifest.get("protocol") != "iatest-bluetooth-hid/1" or manifest.get("managed_ble_protocol") != PROTOCOL:
            raise ValueError("Rebuild the Android HID artifacts for explicit managed BLE")
        for name in ("iatest-input.jar", "iatest-bluetooth-hid.apk"):
            digest = hashlib.sha256((self.directory / name).read_bytes()).hexdigest()
            if digest != manifest.get("sha256", {}).get(name):
                raise ValueError("Managed BLE artifact hash differs: " + name)
        return manifest

    def _installed_uid(self, expected):
        paths = self.adb("shell", "pm", "path", PACKAGE).splitlines()
        if len(paths) != 1 or not re.fullmatch(r"package:/data/app/[A-Za-z0-9_./+=@~:-]+/base\.apk", paths[0]):
            raise ProtocolError("Expected the installed framework-owned base APK")
        if any(part in {".", ".."} for part in paths[0].split("/")):
            raise ProtocolError("Installed APK path contains traversal")
        installed = self.adb("shell", "toybox", "sha256sum", paths[0].removeprefix("package:")).split()[0]
        if installed != expected:
            raise CommandRejected("Install the selected framework HID APK before starting ownership")
        packages = self.adb("shell", "pm", "list", "packages", "-U", PACKAGE)
        matches = re.findall(r"^package:dev\.iatest\.mobile\.hid uid:(\d+)\s*$", packages, re.M)
        if len(matches) != 1 or int(matches[0]) < 10000:
            raise ProtocolError("Framework bridge application UID differs")
        return int(matches[0])

    def _select_ui(self, binding):
        self.adb("shell", "am", "start", "-W", "-n", PACKAGE + "/.MainActivity", "--es", "transport", "ble",
                 "--es", "ble_lifecycle", "adb_managed", "--es", "ble_owner_socket", binding["socket"],
                 "--es", "ble_owner_epoch", binding["owner_epoch"],
                 "--ez", "experimental_digitizer", str(self.settings.get("experimental_digitizer", False)).lower())
        pids = self.adb("shell", "pidof", PACKAGE).split()
        deadline = time.monotonic() + 5
        while True:
            state = json.loads(self.adb("exec-out", "run-as", PACKAGE, "cat", "files/status.json"))
            if pids == [str(state.get("pid"))] and state.get("managed_ble_protocol") == PROTOCOL:
                if state.get("session_active"):
                    raise CommandRejected("End the existing HID session before selecting managed ownership")
                if state.get("ble_lifecycle") == "adb_managed" and state.get("ble_owner_epoch") == binding["owner_epoch"]:
                    return
            if time.monotonic() >= deadline:
                raise CommandRejected("Current bridge did not accept explicit managed ownership")
            self.control.wait(.05)

    def start(self, lifetime_seconds=3600, *, pairing_window_seconds=0):
        if type(lifetime_seconds) is not int or not 5 <= lifetime_seconds <= 7200:
            raise ValueError("Managed BLE lifetime must be 5–7200 seconds")
        if type(pairing_window_seconds) is not int or not (pairing_window_seconds == 0 or 5 <= pairing_window_seconds <= 300):
            raise ValueError("Managed BLE pairing window must be 0 or 5–300 seconds")
        owner_lease = DeviceLease("android-ble-owner", self.settings["android_device_id"])
        device_lease = DeviceLease("android", self.settings["android_device_id"])
        generation = None
        owner_lease.acquire()
        try:
            device_lease.acquire()
            try:
                manifest = self._artifacts()
                if pairing_window_seconds and manifest.get("managed_pairing_protocol") != PAIRING_PROTOCOL:
                    raise ValueError("Rebuild the HID artifacts for explicit managed pairing preparation")
                uid = self._installed_uid(manifest["sha256"]["iatest-bluetooth-hid.apk"])
                try:
                    existing = read_binding(self.adb)
                except CommandRejected:
                    existing = None
                if existing:
                    try:
                        state = self._rpc(existing, "status")["state"]
                    except (OSError, RuntimeError, ValueError):
                        if not self._process_absent(existing):
                            raise CommandRejected("Existing owner is unreachable; inspect or explicitly stop it first")
                        existing = None
                    if existing and state["owner"]["running"]:
                        if existing["host_address"] != self.settings["host_address"] or existing.get("apk_sha256") != manifest["sha256"]["iatest-bluetooth-hid.apk"] or existing.get("experimental_digitizer", False) != self.settings.get("experimental_digitizer", False):
                            raise CommandRejected("Another explicit owner binding is active")
                        self._select_ui(existing)
                        self._rpc(existing, "publish")
                        return {**state, "reused": True, "lifetime_extended": False, "pairing_window_extended": False}
                    if existing:
                        self._rpc(existing, "stop")
                        self.control.wait(.3)
                generation = secrets.token_hex(16)
                boot_id = self.adb("shell", "cat", "/proc/sys/kernel/random/boot_id").strip()
                if not re.fullmatch(BOOT_ID, boot_id):
                    raise ProtocolError("Android boot identity is unavailable")
                jar_hash = manifest["sha256"]["iatest-input.jar"]
                remote = "/data/local/tmp/iatest-managed-ble-" + jar_hash[:16] + ".jar"
                binding = {"protocol": PROTOCOL, "socket": "iatest_hid_owner_" + generation, "owner_epoch": generation,
                           "host_address": self.settings["host_address"], "app_uid": uid,
                           "apk_sha256": manifest["sha256"]["iatest-bluetooth-hid.apk"],
                           "jar_path": remote, "jar_sha256": jar_hash, "lifetime_seconds": lifetime_seconds,
                           "pairing_window_seconds": pairing_window_seconds, "boot_id": boot_id}
                binding["experimental_digitizer"] = self.settings.get("experimental_digitizer", False)
                self._select_ui(binding)
                self.adb("push", str(self.directory / "iatest-input.jar"), remote)
                self.adb("shell", "chmod", "0600", remote)
                actual = self.adb("shell", "toybox", "sha256sum", remote).split()[0]
                if actual != jar_hash:
                    raise ProtocolError("Uploaded managed BLE artifact differs")
                encoded = base64.b64encode(json.dumps(binding, separators=(",", ":")).encode()).decode()
                self.adb("shell", "CLASSPATH=" + remote, "app_process", "/", "dev.iatest.mobile.hid.ManagedBleMain", "start", encoded, timeout=15)
                live = input_binding(self.settings, self.adb)
                if live["owner_epoch"] != generation:
                    raise ProtocolError("Started owner generation differs")
                deadline = time.monotonic() + 10
                while True:
                    state = self._rpc(live, "status")["state"]
                    if state["registered"]:
                        self._rpc(live, "publish")
                        return {**state, "reused": False, "business_runtime_verified": False}
                    if time.monotonic() >= deadline:
                        raise ProtocolError("Managed GATT registration did not complete")
                    self.control.wait(.1)
            except BaseException as failure:
                if generation is not None:
                    self._failed_start_cleanup(generation, failure)
                raise
            finally:
                device_lease.close()
        finally:
            owner_lease.close()

    def stop(self, *, cancel_supervisor=True):
        if cancel_supervisor:
            from iatest.mobile.bluetooth_recovery import cancel_watch

            cancel_watch(self.settings)
        lease = DeviceLease("android-ble-owner", self.settings["android_device_id"])
        lease.acquire()
        try:
            binding = input_binding(self.settings, self.adb)
            self._rpc(binding, "stop")
            deadline = time.monotonic() + 5
            while True:
                try:
                    current = read_binding(self.adb)
                except CommandRejected:
                    return {"stopped": True, "owner_epoch": binding["owner_epoch"]}
                if current["owner_epoch"] != binding["owner_epoch"]:
                    raise ProtocolError("Owner changed while stopping")
                if time.monotonic() >= deadline:
                    raise ProtocolError("Managed BLE owner stop was not confirmed")
                self.control.wait(.05)
        finally:
            lease.close()
