from __future__ import annotations
from iatest.core.env import environment_values

import hashlib
import json
import os
import plistlib
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from iatest.mobile import usbmux
from iatest.mobile.protocol import VERSION, DeliveryUnknown, DeviceChannel, ProtocolError
from iatest.mobile.store import validate_profile


def same_ime(first, second):
    def canonical(value):
        package, separator, name = value.partition("/")
        return (
            (package, package + name if name.startswith(".") else name)
            if separator
            else value
        )

    return canonical(first) == canonical(second)


def command(args: list[str], *, timeout: float = 15, control=None) -> str:
    process = subprocess.Popen(
        args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    deadline = time.monotonic() + timeout
    try:
        while True:
            if control:
                control.check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(args, timeout)
            try:
                output, error = process.communicate(timeout=min(0.1, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        process.kill()
        process.communicate()
        raise
    if process.returncode:
        raise RuntimeError(
            error.decode(errors="replace")[-2000:]
            or output.decode(errors="replace")[-2000:]
        )
    return output.decode(errors="replace")


def doctor() -> dict:
    result = {
        "protocol": VERSION,
        "adb": shutil.which("adb"),
        "xcodebuild": shutil.which("xcodebuild"),
        "devices": [],
        "errors": [],
        "limitations": ["A listed device has not passed native-driver acceptance"],
    }
    if result["adb"]:
        try:
            for line in command([result["adb"], "devices", "-l"]).splitlines()[1:]:
                fields = line.split()
                if len(fields) >= 2:
                    result["devices"].append(
                        {
                            "device_id": fields[0],
                            "platform": "android",
                            "state": fields[1],
                            "model": next(
                                (f[6:] for f in fields[2:] if f.startswith("model:")),
                                "",
                            ),
                        }
                    )
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            result["errors"].append(f"ADB: {exc}")
    try:
        result["devices"].extend(usbmux.list_devices())
    except (OSError, ValueError, RuntimeError, EOFError) as exc:
        result["errors"].append(f"USB iOS: {exc}")
    return result


class NativeDevice:
    def __init__(self, profile: dict, session_id: str):
        self.profile = validate_profile(profile)
        self.session_id, self.token = session_id, secrets.token_urlsafe(32)
        self.input: DeviceChannel | None = None
        self.semantic: DeviceChannel | None = None
        self.media: DeviceChannel | None = None
        self.media_socket_name = ""
        self.processes: list[subprocess.Popen] = []
        self.forwards: list[str] = []
        self.temporary: tempfile.TemporaryDirectory | None = None
        self.observation_listener = None
        self.control = None
        self.logs: list[bytearray] = []
        self.ime_previous = None
        self.log_threads: list[threading.Thread] = []
        self.lifecycle_actions: dict[str, dict] = {}

    def adb(self, *args: str, timeout: float = 15) -> str:
        from iatest.mobile.emulator import require_owned

        require_owned(self.profile)
        return command(
            [shutil.which("adb") or "adb", "-s", self.profile["device_id"], *args],
            timeout=timeout,
            control=self.control,
        )

    def android_lifecycle(self, op, operation_id, timeout):
        if self.profile["platform"] != "android" or op not in {"launch", "terminate"}:
            raise ValueError("Invalid Android lifecycle operation")
        if operation_id in self.lifecycle_actions:
            if self.lifecycle_actions[operation_id].get("lifecycle_op") != op:
                raise ValueError("Lifecycle operation ID reused for another action")
            return dict(self.lifecycle_actions[operation_id])
        if not operation_id or len(self.lifecycle_actions) >= 1024:
            raise ValueError("Lifecycle operation ID missing or ledger exhausted")
        deadline = time.monotonic() + timeout

        def adb(*args):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("App lifecycle budget exhausted")
            return self.adb(*args, timeout=remaining)

        app = self.profile["app_id"]
        if op == "launch":
            resolved = adb("shell", "cmd", "package", "resolve-activity", "--brief", app).strip().splitlines()
            component = resolved[-1] if resolved else ""
            if not re.fullmatch(r"[A-Za-z0-9_.]+/[A-Za-z0-9_.$]+", component) or component.split("/")[0] != app:
                raise ValueError("Target App has no unique owned launch activity")
            command = ("shell", "am", "start", "-W", "--display", str(self.profile.get("display_id", 0)), "-n", shlex.quote(component))
        else:
            command = ("shell", "am", "force-stop", app)
        started = int(time.time() * 1000)
        self.lifecycle_actions[operation_id] = {"status": "unknown", "error": "App lifecycle dispatch unconfirmed", "lifecycle_op": op}
        try:
            output = adb(*command)
            if op == "launch" and "Status: ok" not in output:
                raise RuntimeError("App launch was not acknowledged")
        except BaseException as error:
            if getattr(self, "on_unknown", None):
                self.on_unknown()
            raise DeliveryUnknown("App lifecycle outcome unknown; do not replay") from error
        response = {"status": "injection_call_returned", "accepted": True, "postcondition_verified": False,
                    "lifecycle_backend": "adb_activity_manager", "lifecycle_op": op, "started_timestamp_ms": started}
        self.lifecycle_actions[operation_id] = response
        return dict(response)

    def android_permission_state(self, app, permission):
        if self.profile["platform"] != "android" or app not in {
            self.profile["app_id"], *self.profile.get("allowed_apps", [])
        } or permission not in self.profile.get("allowed_permissions", []):
            raise ValueError("Permission query exceeds the device profile")
        user = self.adb("shell", "am", "get-current-user").strip()
        if not user.isdigit():
            raise ValueError("Current Android user cannot be verified")
        output = self.adb("shell", "dumpsys", "package", app)
        users = list(re.finditer(r"(?m)^    User (\d+):", output))
        for index, match in enumerate(users):
            if match[1] != user:
                continue
            section = output[match.end():users[index+1].start() if index+1 < len(users) else len(output)]
            values = re.findall(r"(?m)^\s+" + re.escape(permission) + r": granted=(true|false)\b", section)
            if len(values) == 1:
                return values[0] == "true"
        raise ValueError("Target runtime permission state cannot be verified")

    def _start(self, args: list[str]) -> None:
        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.processes.append(process)
        buffer = bytearray()
        self.logs.append(buffer)

        def capture():
            try:
                while chunk := process.stdout.read1(4096):
                    buffer.extend(chunk)
                    if len(buffer) > 65536:
                        del buffer[:-65536]
            finally:
                process.stdout.close()

        reader = threading.Thread(target=capture, name="iatest-native-log", daemon=True)
        self.log_threads.append(reader)
        reader.start()

    def diagnostics(self) -> str:
        messages = []
        for buffer in self.logs:
            text = bytes(buffer).decode(errors="replace")
            # Xcode may append kilobytes of automatic diagnostic collection
            # after a test failure, hiding the actual native startup error.
            failures = [line[:1000] for line in text.splitlines() if any(
                marker in line.lower() for marker in (
                    "error:", "encountered an error", "failed to initialize for ui testing",
                    "timed out while enabling automation mode", "testing failed:",
                )
            )]
            messages.append("\n".join(failures[-3:]) + "\n" + text[-2000:])
        return "\n".join(messages).replace(self.token, "[REDACTED]")

    def _channel(
        self, connect: Callable[[], socket.socket], deadline: float
    ) -> DeviceChannel:
        last = None
        while time.monotonic() < deadline:
            if self.control:
                self.control.check()
            sock = None
            try:
                if any(p.poll() is not None for p in self.processes):
                    for reader in self.log_threads:
                        reader.join(timeout=0.1)
                    raise RuntimeError(
                        "Native runner exited during startup: " + self.diagnostics()
                    )
                sock = connect()
                channel = DeviceChannel(
                    sock,
                    session_id=self.session_id,
                    token=self.token,
                    app_id=self.profile["app_id"],
                )
                channel.control = self.control
                channel.on_unknown = getattr(self, "on_unknown", None)
                return channel
            except (OSError, EOFError) as exc:
                if sock:
                    sock.close()
                last = exc
                time.sleep(0.15)
        raise RuntimeError(
            f"Native driver did not become ready: {last}\n{self.diagnostics()}"
        )

    def open(self) -> NativeDevice:
        try:
            if self.profile["platform"] == "android":
                self._android()
            else:
                self._ios()
            return self
        except BaseException:
            self.close()
            raise

    def _android(self) -> None:
        directory = (
            Path(self.profile.get("artifact_dir", "native/mobile/build/android"))
            .expanduser()
            .resolve()
        )
        manifest = json.loads((directory / "manifest.json").read_text())
        if manifest.get("protocol") != VERSION:
            raise ValueError("Native artifact protocol does not match")
        for name in ("iatest-input.jar", "iatest-probe.apk"):
            data = (directory / name).read_bytes()
            if hashlib.sha256(data).hexdigest() != manifest.get("sha256", {}).get(name):
                raise ValueError(f"Native artifact hash mismatch: {name}")
        if self.adb("get-state").strip() != "device":
            raise RuntimeError("Android device must be connected and authorized")
        suffix = secrets.token_hex(8)
        remote = f"/data/local/tmp/iatest-input-{manifest['sha256']['iatest-input.jar'][:16]}.jar"
        self.adb("push", str(directory / "iatest-input.jar"), remote)
        # Only the framework-owned probe is installed; the target App is not replaced.
        self.adb("install", "-r", "-t", str(directory / "iatest-probe.apk"), timeout=60)
        if self.profile.get("allow_ime_setup"):
            import re

            previous = self.adb(
                "shell", "settings", "get", "secure", "default_input_method"
            ).strip()
            enabled = self.adb(
                "shell", "settings", "get", "secure", "enabled_input_methods"
            ).strip()
            if not re.fullmatch(r"[A-Za-z0-9_.]+/[A-Za-z0-9_.]+", previous):
                raise ValueError(
                    "An existing restorable Android input method is required"
                )
            component = "dev.iatest.mobile/.IATestIme"
            self.ime_previous = {
                "default": previous,
                "enabled": any(
                    same_ime(component, v.split(";")[0]) for v in enabled.split(":")
                ),
            }
            self.adb("shell", "ime", "enable", component)
            self.adb("shell", "ime", "set", component)
        names = [f"iatest_input_{suffix}", f"iatest_probe_{suffix}"]
        self.media_socket_name = names[1] + "_media"
        ports = []
        for name in names:
            port = int(self.adb("forward", "tcp:0", "localabstract:" + name).strip())
            self.forwards.append(f"tcp:{port}")
            ports.append(port)
        base = [shutil.which("adb") or "adb", "-s", self.profile["device_id"], "shell"]
        self._start(
            [
                *base,
                f"CLASSPATH={remote}",
                "app_process",
                "/",
                "dev.iatest.mobile.InputMain",
                names[0],
                self.session_id,
                self.token,
                self.profile["app_id"],
                str(self.profile.get("display_id", 0)),
            ]
        )
        self._start(
            [
                *base,
                "am",
                "instrument",
                "-w",
                "-e",
                "socket",
                names[1],
                "-e",
                "session",
                self.session_id,
                "-e",
                "token",
                self.token,
                "-e",
                "app",
                self.profile["app_id"],
                "-e",
                "display_id",
                str(self.profile.get("display_id", 0)),
                "dev.iatest.mobile/dev.iatest.mobile.Probe",
            ]
        )
        deadline = time.monotonic() + 30
        self.input = self._channel(
            lambda: socket.create_connection(("127.0.0.1", ports[0]), timeout=2),
            deadline,
        )
        self.semantic = self._channel(
            lambda: socket.create_connection(("127.0.0.1", ports[1]), timeout=2),
            deadline,
        )

        self.input.capabilities["actions"] = list(dict.fromkeys([
            *self.input.capabilities.get("actions", []), "launch", "terminate"
        ]))
        self.input.capabilities["app_lifecycle_backend"] = "adb_activity_manager"

    def _ios(self) -> None:
        source = Path(self.profile.get("xctestrun", "")).expanduser().resolve()
        if not source.is_file() or source.suffix != ".xctestrun":
            raise ValueError("iOS profile requires a built/signed .xctestrun file")
        # Keep __TESTROOT__ relative to the original build products.
        value = plistlib.loads(source.read_bytes())
        self.temporary = tempfile.TemporaryDirectory(prefix="iatest-xctestrun-")
        observation_env = {}
        if self.profile.get("ios_observation"):
            from iatest.mobile.network_observation import ReverseTLSListener

            self.observation_listener = ReverseTLSListener(
                self.profile["ios_observation"], self.temporary.name,
                session_id=self.session_id, token=self.token,
                app_id=self.profile["app_id"], control=self.control,
            )
            observation_env = self.observation_listener.environment()

        def rewrite(item):
            if isinstance(item, dict):
                result = {k: rewrite(v) for k, v in item.items()}
                if "TestBundlePath" in result:
                    env = {
                        "IATEST_SESSION": self.session_id,
                        "IATEST_TOKEN": self.token,
                        "IATEST_APP": self.profile["app_id"],
                        "IATEST_PORT": str(self.profile.get("runner_port", 8101)),
                        "IATEST_PRIVATE_GESTURES": "1"
                        if self.profile.get("allow_private_ios_gestures")
                        else "0",
                        **observation_env,
                        "IATEST_SIMULATOR_PORTRAIT": "1"
                        if self.profile.get("simulator") and self.profile.get("simulator_orientation") == "portrait"
                        else "0",
                    }
                    result["EnvironmentVariables"] = {
                        **result.get("EnvironmentVariables", {}),
                        **env,
                    }
                    result["TestingEnvironmentVariables"] = {
                        **result.get("TestingEnvironmentVariables", {}),
                        **env,
                    }
                return result
            if isinstance(item, list):
                return [rewrite(v) for v in item]
            return (
                item.replace("__TESTROOT__", str(source.parent))
                if isinstance(item, str)
                else item
            )

        target = Path(self.temporary.name) / "session.xctestrun"
        target.write_bytes(plistlib.dumps(rewrite(value)))
        destination = (
            "platform=iOS Simulator,id="
            if self.profile.get("simulator")
            else "platform=iOS,id="
        ) + self.profile["device_id"]
        self._start(
            [
                "xcodebuild",
                "test-without-building",
                "-xctestrun",
                str(target),
                "-destination",
                destination,
                "-only-testing:IATestMobileTests/MobileRunner/testServe",
                "-resultBundlePath",
                str(Path(self.temporary.name) / "result.xcresult"),
            ]
        )
        port = self.profile.get("runner_port", 8101)
        connect = (
            (lambda: socket.create_connection(("127.0.0.1", port), timeout=2))
            if self.profile.get("simulator")
            else (lambda: usbmux.connect(self.profile["device_id"], port))
        )
        startup_deadline = time.monotonic() + 90
        if self.observation_listener:
            connect = lambda: self.observation_listener.accept(timeout=2, setup_deadline=startup_deadline)
        self.semantic = self.input = self._channel(connect, startup_deadline)

    def open_media(self, codec="png"):
        if self.profile.get("video_capture") == "app_stream":
            if codec != "h264":
                raise ValueError("App ReplayKit stream supplies H264 only")
            token = environment_values().get(self.profile["app_media_token_env"], "")
            if len(token) < 32:
                raise ValueError(
                    "Existing App media authentication environment is missing"
                )
            port = self.profile["app_media_port"]
            sock = (
                socket.create_connection(("127.0.0.1", port), timeout=2)
                if self.profile.get("simulator")
                else usbmux.connect(self.profile["device_id"], port)
            )
            if self.media:
                self.media.close()
            self.media = DeviceChannel(
                sock,
                session_id=self.session_id,
                token=token,
                app_id=self.profile["app_id"],
            )
            self.media.control = self.control
            return self.media
        capability = "continuous_video" if codec == "h264" else "separate_png_media"
        if not self.semantic.capabilities.get(capability):
            raise ValueError(
                "This native driver does not declare a separate media source"
            )
        if self.media:
            self.media.close()
        self.semantic.request(
            "media_open",
            {
                "codec": codec,
                "capture_source": self.profile.get("video_capture", "screenshot"),
                "media_limits": self.profile.get("media_limits", {}),
            },
            timeout=5,
        )
        if self.profile["platform"] == "ios":
            port = self.profile.get("runner_port", 8101) + 1
            connect = (
                (lambda: socket.create_connection(("127.0.0.1", port), timeout=2))
                if self.profile.get("simulator")
                else (lambda: usbmux.connect(self.profile["device_id"], port))
            )
            media_deadline = time.monotonic() + 5
            if self.observation_listener:
                connect = lambda: self.observation_listener.accept("media", timeout=2, setup_deadline=media_deadline)
            self.media = self._channel(connect, media_deadline)
            self._verify_media_limits()
            return self.media
        previous = getattr(self, "media_forward", None)
        if previous:
            self.adb("forward", "--remove", previous)
            self.forwards.remove(previous)
            self.media_forward = None
        port = int(
            command(
                [
                    shutil.which("adb") or "adb",
                    "-s",
                    self.profile["device_id"],
                    "forward",
                    "tcp:0",
                    "localabstract:" + self.media_socket_name,
                ],
                timeout=5,
            ).strip()
        )
        self.media_forward = f"tcp:{port}"
        self.forwards.append(self.media_forward)
        self.media = self._channel(
            lambda: socket.create_connection(("127.0.0.1", port), timeout=2),
            time.monotonic() + 3,
        )
        self._verify_media_limits()
        return self.media

    def _verify_media_limits(self):
        if "media_limits" in self.profile:
            expected = {"duration_seconds": 120, "byte_limit": 67108864, **self.profile["media_limits"]}
            if self.media.capabilities.get("media_limits") != expected:
                self.media.close()
                raise ProtocolError("Native media driver did not confirm the requested budgets; rebuild the driver")

    def close(self) -> None:
        # Cancellation must not prevent teardown commands from running.
        self.control = None
        ime_error = None
        if self.ime_previous:
            try:
                component = "dev.iatest.mobile/.IATestIme"
                current = self.adb(
                    "shell", "settings", "get", "secure", "default_input_method"
                ).strip()
                if same_ime(current, component):
                    self.adb("shell", "ime", "set", self.ime_previous["default"])
                if not self.ime_previous["enabled"]:
                    self.adb("shell", "ime", "disable", component)
                self.ime_previous = None
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                ime_error = exc
        graceful_ios_close = False
        for channel in {self.input, self.semantic, self.media} - {None}:
            try:
                if not channel.closed:
                    response = channel.request("close", {}, timeout=2)
                    graceful_ios_close |= (
                        self.profile["platform"] == "ios"
                        and isinstance(response, dict)
                        and response.get("status") == "closed"
                    )
            except (OSError, EOFError, RuntimeError):
                pass
            finally:
                channel.close()
        for process in self.processes:
            if graceful_ios_close and Path(str(process.args[0])).name == "xcodebuild" and process.poll() is None:
                # Let testServe finish and Xcode dispose its device test session.
                # SIGTERM immediately after the close reply aborts that teardown.
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        for forward in self.forwards:
            try:
                self.adb("forward", "--remove", forward)
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                pass
        self.forwards.clear()
        for reader in self.log_threads:
            reader.join(timeout=0.5)
        if self.observation_listener:
            self.observation_listener.close()
            self.observation_listener = None
        if self.temporary:
            self.temporary.cleanup()
        if ime_error:
            raise RuntimeError(
                "Framework IME settings restoration could not be confirmed"
            ) from ime_error
