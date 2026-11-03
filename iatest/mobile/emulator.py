"""Owned Android VM lifecycle with fresh writable disks and no shared AVD state."""

from __future__ import annotations

import os
import random
import socket
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from iatest.execution.control import RunControl
from iatest.mobile.lease import DeviceLease

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["sdk_root", "system_image", "artifact_id"],
    "properties": {
        "sdk_root": {"type": "string", "minLength": 1, "maxLength": 4096},
        "system_image": {
            "type": "string",
            "pattern": r"^android-[0-9]{2};(?:google_apis|default);(?:arm64-v8a|x86_64)$",
        },
        "artifact_id": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,80}$"},
        "memory_mb": {"type": "integer", "minimum": 1536, "maximum": 4096},
        "boot_timeout_seconds": {"type": "number", "minimum": 10, "maximum": 240},
    },
}
OWNED = {}
BOUND_PROFILES = ContextVar("iatest_owned_vm_profiles", default=None)


def bound_profile(root, project, profile):
    key = (str(Path(root).resolve()), project, profile["id"])
    bound = (BOUND_PROFILES.get() or {}).get(key)
    if bound is None:
        return profile
    if any(profile.get(k) != bound.get(k) for k in ("app_id", "managed_emulator")):
        raise ValueError("Managed profile changed during its plan lifecycle")
    require_owned(bound)
    return {**profile, "device_id": bound["device_id"], "require_os_sandbox": True}


def borrowed_vm(profile):
    return any(
        all(profile.get(k) == bound.get(k) for k in ("id", "app_id", "device_id", "managed_emulator"))
        for bound in (BOUND_PROFILES.get() or {}).values()
    )


@contextmanager
def plan_vm_scope(root, project, plan, control):
    """Keep one owned VM across preparation, seed journeys and measurements."""
    if root is None or plan.kind not in {"android", "client_performance"}:
        yield None
        return
    journey = plan.spec if plan.kind == "android" else plan.spec.get("plan", {}).get("spec", {})
    if plan.kind == "client_performance" and plan.spec.get("platform") != "android":
        yield None
        return
    from iatest.mobile.store import MobileStore
    from iatest.mobile.system import preflight

    profile = MobileStore(root, project).get("profile", journey["profile"])
    if not profile.get("managed_emulator") or borrowed_vm(profile):
        yield None
        return
    # Reject unauthorized UI operations before creating or installing anything.
    preflight(profile, journey)
    instance = AndroidEmulator(profile["managed_emulator"], control)
    token = None
    try:
        owned = instance.prepare_profile(profile, root, project, install=not bool(plan.preparation.get("app")))
        key = (str(Path(root).resolve()), project, profile["id"])
        token = BOUND_PROFILES.set({**(BOUND_PROFILES.get() or {}), key: owned})
        yield instance.evidence
    finally:
        if token is not None:
            BOUND_PROFILES.reset(token)
        instance.close()


def require_owned(profile):
    if not profile.get("require_os_sandbox") and not profile.get("managed_emulator"):
        return
    instance = OWNED.get(profile["device_id"])
    if (
        instance is None
        or instance.process is None
        or instance.process.poll() is not None
    ):
        raise ValueError("OS isolation requires a live VM created by this controller")
    if instance.settings != profile.get("managed_emulator"):
        raise ValueError("Managed VM ownership does not match the profile")


class AndroidEmulator:
    def __init__(self, settings, control=None):
        from iatest.engines.http import validate

        validate(settings, SCHEMA)
        self.settings = dict(settings)
        self.control = control or RunControl(timeout_seconds=300)
        self.sdk = Path(settings["sdk_root"]).expanduser().resolve(strict=True)
        self.image = (
            self.sdk / "system-images" / settings["system_image"].replace(";", "/")
        )
        self.executable = (
            self.sdk / "emulator" / ("emulator.exe" if os.name == "nt" else "emulator")
        )
        self.adb_path = (
            self.sdk / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb")
        )
        for path in (
            self.executable,
            self.adb_path,
            self.image / "system.img",
            self.image / "ramdisk.img",
            self.image / "kernel-ranchu",
        ):
            if not path.is_file() or not path.resolve().is_relative_to(self.sdk):
                raise ValueError(
                    "Install the selected emulator, platform-tools and system image first"
                )
        self.process = self.temporary = self.port_lease = self.log = None
        self.serial = self.name = ""
        self.evidence = {}

    def adb(self, *args, timeout=5, cleanup=False):
        from iatest.mobile.devices import command

        if self.process is None or self.process.poll() is not None:
            raise RuntimeError("Owned emulator process is no longer running")
        return command(
            [str(self.adb_path), "-s", self.serial, *args],
            timeout=timeout,
            control=None if cleanup else self.control,
        ).strip()

    def start(self):
        self.control.check()
        if self.process is not None:
            raise ValueError("An emulator instance cannot be started twice")
        self.temporary = tempfile.TemporaryDirectory(prefix="iatest-owned-avd-")
        root = Path(self.temporary.name)
        self.name = "iatest_" + uuid.uuid4().hex
        self.evidence = {
            "backend": "android_emulator_vm",
            "name": self.name,
            "system_image": self.settings["system_image"],
            "fresh_userdata": True,
            "shared_snapshots": False,
            "network": "emulator NAT; UID policy is a separate setting",
        }
        try:
            ports = list(range(5580, 5680, 2))
            random.SystemRandom().shuffle(ports)
            for port in ports:
                lease = DeviceLease("android-emulator-console", str(port))
                probes = []
                try:
                    lease.acquire()
                    for candidate in (port, port + 1):
                        probe = socket.socket()
                        probes.append(probe)
                        probe.bind(("127.0.0.1", candidate))
                except (OSError, RuntimeError):
                    lease.close()
                    continue
                finally:
                    for probe in probes:
                        probe.close()
                self.port_lease = lease
                self.serial = "emulator-" + str(port)
                break
            else:
                raise ValueError("No free owned emulator console/ADB port pair")
            avds = root / "avds"
            directory = avds / (self.name + ".avd")
            directory.mkdir(parents=True)
            (avds / (self.name + ".ini")).write_text(
                "avd.ini.encoding=UTF-8\npath=" + str(directory) + "\n",
                encoding="utf-8",
            )
            arch = self.settings["system_image"].split(";")[-1]
            config = {
                "AvdId": self.name,
                "avd.ini.displayname": self.name,
                "image.sysdir.1": str(self.image) + os.sep,
                "abi.type": arch,
                "hw.cpu.arch": "arm64" if arch == "arm64-v8a" else "x86_64",
                "tag.id": self.settings["system_image"].split(";")[1],
                "hw.ramSize": str(self.settings.get("memory_mb", 2048)),
                "hw.cpu.ncore": "2",
                "hw.lcd.width": "393",
                "hw.lcd.height": "852",
                "hw.lcd.density": "160",
                "hw.keyboard": "yes",
                "hw.gpu.enabled": "yes",
                "hw.gpu.mode": "swiftshader",
                "disk.dataPartition.size": "2G",
                "fastboot.forceColdBoot": "yes",
                "showDeviceFrame": "no",
            }
            (directory / "config.ini").write_text(
                "".join(k + "=" + v + "\n" for k, v in config.items()), encoding="utf-8"
            )
            environment = dict(
                os.environ,
                ANDROID_AVD_HOME=str(avds),
                ANDROID_EMULATOR_HOME=str(root / "emulator"),
                ANDROID_USER_HOME=str(root / "android-user"),
                ANDROID_SDK_ROOT=str(self.sdk),
            )
            for path in (root / "emulator", root / "android-user"):
                path.mkdir()
            self.log = tempfile.TemporaryFile()  # noqa: SIM115 -- Owned for the VM lifecycle; close() handles every path.
            self.process = subprocess.Popen(
                [
                    str(self.executable),
                    "-avd",
                    self.name,
                    "-port",
                    str(port),
                    "-no-window",
                    "-no-audio",
                    "-no-snapshot",
                    "-no-boot-anim",
                    "-no-metrics",
                    "-camera-back",
                    "none",
                    "-camera-front",
                    "none",
                    "-gpu",
                    "swiftshader",
                ],
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=self.log,
                stderr=subprocess.STDOUT,
            )
            deadline = time.monotonic() + min(
                self.settings.get("boot_timeout_seconds", 180),
                self.control.remaining() or 180,
            )
            while True:
                self.control.check()
                if self.process.poll() is not None:
                    self.log.seek(0, 2)
                    self.log.seek(max(0, self.log.tell() - 4000))
                    raise RuntimeError(
                        "Owned emulator exited: "
                        + self.log.read().decode(errors="replace")
                    )
                try:
                    if (
                        self.adb("shell", "getprop", "sys.boot_completed", timeout=2)
                        == "1"
                    ):
                        break
                except (OSError, RuntimeError, subprocess.TimeoutExpired):
                    pass
                if time.monotonic() >= deadline:
                    raise TimeoutError("Owned Android VM boot timed out")
                self.control.wait(0.5)
            if (
                self.adb("shell", "getprop", "ro.kernel.qemu") != "1"
                or self.adb("emu", "avd", "name").splitlines()[0] != self.name
            ):
                raise ValueError("ADB serial does not identify the newly owned VM")
            OWNED[self.serial] = self
            self.evidence.update(
                device_id=self.serial, pid=self.process.pid, boot_completed=True
            )
            return self.serial
        except BaseException:
            self.close()
            raise

    def close(self):
        OWNED.pop(self.serial, None)
        if self.process is not None:
            if self.process.poll() is None:
                # Signal only our Popen child, never a process discovered by name/serial.
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            self.evidence["process_stopped"] = self.process.poll() is not None
            self.process = None
        if self.port_lease:
            self.port_lease.close()
            self.port_lease = None
        if self.log:
            self.log.close()
            self.log = None
        if self.temporary:
            self.temporary.cleanup()
            self.temporary = None
            self.evidence["userdata_removed"] = True

    def prepare_profile(self, profile, root, project, *, install=True):
        from iatest.mobile.delivery import AppInstaller, cached_app

        serial = self.start()
        owned = {**profile, "device_id": serial, "require_os_sandbox": True}
        try:
            if install:
                artifact, path = cached_app(root, project, self.settings["artifact_id"])
                AppInstaller(owned, self.control).install(artifact, path, "fresh")
        except BaseException:
            self.close()
            raise
        return owned
