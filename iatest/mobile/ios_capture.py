"""Host-side Instruments recording/export with bounded lifecycle and no scripts."""

from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit

from iatest.execution.control import RunControl
from iatest.mobile.devices import command
from iatest.mobile.ios_metrics import SCHEMAS, parse_process_xml, toc_metadata

CAPTURE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "mode": {"enum": ["attach", "launch", "device_processes"]},
        "template": {
            "enum": ["activity_monitor", "animation_hitches", "frame_lifetimes"]
        },
        "tool_launch": {"enum": ["standard", "isolated_copy"]},
        "stop_strategy": {"enum": ["interrupt", "time_limit"]},
        "target_pid": {"type": "integer", "minimum": 1, "maximum": 2147483647},
        "duration_seconds": {"type": "number", "minimum": 1, "maximum": 600},
        "ready_timeout_seconds": {"type": "number", "minimum": 1, "maximum": 60},
        "max_trace_bytes": {"type": "integer", "minimum": 1024, "maximum": 536870912},
        "finalize_timeout_seconds": {"type": "number", "minimum": 1, "maximum": 60},
    },
    "allOf": [
        {
            "if": {
                "properties": {"mode": {"const": "device_processes"}},
                "required": ["mode"],
            },
            "then": {"properties": {"template": {"const": "activity_monitor"}}},
        },
        {
            "if": {"properties": {"mode": {"const": "launch"}}, "required": ["mode"]},
            "then": {"not": {"required": ["target_pid"]}},
        },
    ],
}


def device_query(profile, control, *args, runner=command):
    value = runner(
        [
            "xcrun",
            "devicectl",
            "device",
            "info",
            *args,
            "--device",
            profile["device_id"],
            "--timeout",
            "10",
            "--json-output",
            "-",
        ],
        timeout=12,
        control=control,
    )
    if len(value.encode()) > 16777216:
        raise ValueError("Device process metadata exceeds its size budget")
    return json.loads(value).get("result", {})


def app_record(profile, control, *, runner=command):
    apps = device_query(
        profile, control, "apps", "--bundle-id", profile["app_id"], runner=runner
    ).get("apps", [])
    matches = [a for a in apps if a.get("bundleIdentifier") == profile["app_id"]]
    if len(matches) != 1:
        raise ValueError("Installed App identity cannot be resolved uniquely")
    return matches[0]


def resolve_pid(
    profile, control, *, target_pid=None, runner=command, allow_missing=False
):
    def query(*args):
        return device_query(profile, control, *args, runner=runner)

    processes = query("processes")
    items = processes.get("runningProcesses", processes.get("processes", []))
    if not isinstance(items, list) or len(items) > 10000:
        raise TypeError("Unrecognized devicectl process metadata")
    if target_pid is None:
        owned = [
            p
            for p in items
            if p.get("bundleIdentifier", p.get("bundleID")) == profile["app_id"]
        ]
        if not owned:
            apps = query("apps", "--bundle-id", profile["app_id"]).get("apps", [])
            app = next(
                (a for a in apps if a.get("bundleIdentifier") == profile["app_id"]), {}
            )
            location = _device_path(
                app.get("url", app.get("installationURL", ""))
            ).rstrip("/")
            if location.endswith(".app"):
                owned = [
                    p
                    for p in items
                    if not p.get("bundleIdentifier", p.get("bundleID"))
                    and _device_path(
                        p.get("executable", p.get("executableURL", ""))
                    ).startswith(location + "/")
                ]
        if not owned and allow_missing:
            return None
        if len(owned) != 1:
            raise ValueError("App process PID cannot be resolved uniquely")
        target_pid = int(owned[0].get("processIdentifier", owned[0].get("pid", 0)))
        if target_pid <= 0:
            raise ValueError("Resolved App PID is invalid")
    matches = [
        p
        for p in items
        if str(p.get("processIdentifier", p.get("pid", ""))) == str(target_pid)
    ]
    if len(matches) != 1:
        raise ValueError("Measured PID is unavailable or ambiguous")
    process = matches[0]
    bundle = process.get("bundleIdentifier", process.get("bundleID"))
    if bundle:
        if bundle != profile["app_id"]:
            raise ValueError("Measured PID belongs to another App")
        return target_pid
    apps = query("apps", "--bundle-id", profile["app_id"]).get("apps", [])
    app = next((a for a in apps if a.get("bundleIdentifier") == profile["app_id"]), {})

    application = _device_path(app.get("url", app.get("installationURL", ""))).rstrip(
        "/"
    )
    executable = _device_path(
        process.get("executable", process.get("executableURL", ""))
    )
    if not application.endswith(".app") or not executable.startswith(application + "/"):
        raise ValueError(
            "Cannot prove that measured PID belongs to the target App; refusing unrelated process metrics"
        )
    return target_pid


def _device_path(value):
    if not isinstance(value, str):
        return ""
    parts = urlsplit(value)
    if parts.scheme not in {"", "file"} or parts.netloc not in {"", "localhost"}:
        return ""
    return posixpath.normpath(unquote(parts.path)) if parts.path.startswith("/") else ""


def verify_pid(profile, target_pid, control, *, runner=command):
    return resolve_pid(profile, control, target_pid=target_pid, runner=runner)


class CaptureFailure(ValueError):
    """Only allowlisted diagnostics escape the owned raw recorder log."""

    def __init__(self, code, phase, message):
        self.code, self.phase = code, phase
        super().__init__(f"{code} ({phase}): {message}")


def terminate_app_pid(profile, pid, control):
    verify_pid(profile, pid, control)
    command(
        [
            "xcrun",
            "devicectl",
            "device",
            "process",
            "terminate",
            "--device",
            profile["device_id"],
            "--pid",
            str(pid),
            "--timeout",
            "10",
        ],
        timeout=12,
        control=control,
    )
    deadline = time.monotonic() + 3
    while resolve_pid(profile, control, allow_missing=True) == pid:
        if time.monotonic() >= deadline:
            raise CaptureFailure(
                "cleanup_unconfirmed", "cleanup", "The owned App process did not stop"
            )
        control.wait(0.1)


class IOSCapture:
    session_lifecycle = True

    def __init__(self, root, project, profile, control, spec):
        self.root, self.project, self.profile, self.control, self.spec = (
            root,
            project,
            profile,
            control,
            spec,
        )
        self.values, self.notes, self.artifact_id, self.source = {}, [], "", {}
        self.recorder = None
        self.mode = spec.get("mode", "attach")
        self.launched_pid = None
        self.cleanup_status = "not_owned"
        self.ui_session_started = False
        self.ui_session_completed = False
        self.measurement_errors = []
        from iatest.engines.http import validate

        validate(spec, CAPTURE_SCHEMA)

    def begin_ui_session(self, session):
        self.ui_session_completed = False
        if any(
            session.profile[key] != self.profile[key]
            for key in ("platform", "device_id", "app_id")
        ):
            raise ValueError(
                "The recorder and UI session target different Apps/devices"
            )
        activate_before = (
            self.mode == "attach" and self.spec.get("tool_launch") == "isolated_copy"
        )
        if (
            activate_before or self.mode in {"launch", "device_processes"}
        ) and "activate_running" not in session.device.input.capabilities.get(
            "actions", []
        ):
            raise CaptureFailure(
                "runner_upgrade_required",
                "activation",
                "Rebuild the native Runner to activate the recorded App",
            )
        if activate_before:
            self.spec = {
                **self.spec,
                "target_pid": resolve_pid(
                    self.profile, self.control, target_pid=self.spec.get("target_pid")
                ),
            }
            self._activate_running(session, self.spec["target_pid"])
        self.before()
        self.ui_session_started = True
        if self.mode in {"launch", "device_processes"}:
            # XCTest may not bind an externally launched App until activation.
            # Never accept a replacement PID or replay an unconfirmed input.
            pid = (
                self.launched_pid if self.mode == "launch" else self.spec["target_pid"]
            )
            self._activate_running(session, pid)

    def _activate_running(self, session, pid):
        verify_pid(self.profile, pid, self.control)
        activation = session.device.input.request(
            "activate_running", {}, operation_id=uuid.uuid4().hex, timeout=20
        )
        verify_pid(self.profile, pid, self.control)
        if (
            activation.get("accepted") is not True
            or activation.get("status") == "unknown"
        ):
            raise CaptureFailure(
                "activation_unconfirmed",
                "activation",
                "The recorded App could not be confirmed in the foreground",
            )

    def finish_ui_session(self):
        if self.ui_session_started:
            try:
                if getattr(self.recorder, "failure", ""):
                    raise self.recorder.capture_failure or ValueError(
                        self.recorder.failure
                    )
                self.after()
                self.ui_session_completed = True
            finally:
                self.ui_session_started = False

    def before(self):
        if self.mode == "launch":
            app_record(self.profile, self.control)
            existing = resolve_pid(self.profile, self.control, allow_missing=True)
            if existing is not None:
                terminate_app_pid(self.profile, existing, self.control)
            self.cleanup_status = "pending"
        elif self.spec.get("target_pid"):
            verify_pid(self.profile, self.spec["target_pid"], self.control)
        else:
            self.spec = {
                **self.spec,
                "target_pid": resolve_pid(self.profile, self.control),
            }
        self.recorder = XctraceRecorder(
            control=self.control,
            device_id=self.profile["device_id"],
            app_id=self.profile["app_id"] if self.mode == "launch" else None,
            **self.spec,
        )
        self.recorder.start()
        if self.mode == "launch":
            self.launched_pid = resolve_pid(self.profile, self.control)
            self.spec = {**self.spec, "target_pid": self.launched_pid}
            self.recorder.target_pid = self.launched_pid

    def after(self):
        from iatest.application.ios_measurements import save_measurement

        verify_pid(self.profile, self.spec["target_pid"], self.control)
        parsed = self.recorder.finish()
        # Instruments can end the process it launched while finalizing. Its
        # trace must still bind that PID; non-owning modes must keep it alive.
        if self.mode != "launch":
            verify_pid(self.profile, self.spec["target_pid"], self.control)
        self._cleanup_launched_app()
        if not parsed.get("samples"):
            raise CaptureFailure(
                "samples_unavailable",
                "export",
                "No supported samples were recorded for the verified App",
            )
        actual = parsed["metadata"]
        if (
            actual["platform"] not in {"iOS", "iPadOS", "iPhoneOS"}
            or actual["device_id"].lower() != self.profile["device_id"].lower()
        ):
            raise ValueError("Captured trace belongs to another platform/device")
        artifact = save_measurement(
            self.root,
            self.project,
            parsed,
            {
                **actual,
                "platform": "ios",
                "app_id": self.profile["app_id"],
                "provider": "xctrace",
                "capture_mode": self.mode,
                "app_cleanup": self.cleanup_status,
                "trust": "App process identity and trace device/PID checked; device compatibility requires acceptance",
            },
        )
        self.artifact_id = artifact["id"]
        self.source = artifact["source"]
        self.values = {
            k: v["max"]
            for k, v in artifact["measurements"].items()
            if artifact.get("metric_quality", {})
            .get(k, {"assessed": True})
            .get("assessed")
        }
        self.notes = [
            *parsed["notes"],
            "Device process samples cover the recorder window, including bounded start/stop overhead; values are maxima within each recording",
            "Activity Monitor does not infer launch/frame metrics; Animation Hitches reports observed frame lifetimes as a separate metric",
            *(
                [
                    "Explicit launch mode restarted and owns the target App process; no attach-to-launch fallback"
                ]
                if self.mode == "launch"
                else []
            ),
        ]

    def _cleanup_launched_app(self):
        if self.mode != "launch" or self.cleanup_status != "pending":
            return
        control = RunControl(timeout_seconds=18)
        current = resolve_pid(self.profile, control, allow_missing=True)
        if current is None:
            self.cleanup_status = "stopped"
        elif self.launched_pid == current:
            terminate_app_pid(self.profile, current, control)
            self.cleanup_status = "stopped"
        elif self.launched_pid is not None:
            self.cleanup_status = "owned_pid_gone_other_process_left"
        else:
            raise CaptureFailure(
                "cleanup_unconfirmed",
                "cleanup",
                "A running App could not be bound to this recording; it was not terminated",
            )

    def close(self):
        if self.recorder:
            self.recorder.close()
            self._cleanup_launched_app()


def capabilities():
    value = {
        "available": sys.platform == "darwin" and bool(shutil.which("xcrun")),
        "provider": "xctrace",
        "templates": [],
        "live_metrics": [
            "cpu_percent",
            "memory_footprint_kb",
            "resident_memory_kb",
            "frame_lifetime_ms",
        ],
        "metrics_by_template": {
            "activity_monitor": [
                "cpu_percent",
                "memory_footprint_kb",
                "resident_memory_kb",
            ],
            "animation_hitches": ["frame_lifetime_ms"],
            "frame_lifetimes": ["frame_lifetime_ms"],
        },
        "capture_modes": ["attach", "launch", "device_processes"],
        "tool_launches": ["standard", "isolated_copy"],
        "capture_templates": ["activity_monitor", "animation_hitches", "frame_lifetimes"],
        "stop_strategies": ["interrupt", "time_limit"],
        "device_processes_scope": "Device process statistics are recorded temporarily; only the verified App PID is retained, without restarting it",
        "launch_owns_app_process": True,
        "device_acceptance": "not_validated",
        "errors": [],
    }
    if value["available"]:
        try:
            listed = command(["xcrun", "xctrace", "list", "templates"], timeout=15)
            value["templates"] = [
                name
                for name in ("Activity Monitor", "App Launch", "Animation Hitches")
                if name in listed.splitlines()
            ]
            value["available"] = "Activity Monitor" in value["templates"]
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            value["available"] = False
            value["errors"].append(type(exc).__name__)
    return value


def tree_bytes(path, maximum):
    path = Path(path)
    if not path.is_dir() or path.is_symlink():
        raise ValueError("Expected a local trace bundle directory")
    total = 0
    for entries, child in enumerate(path.rglob("*"), 1):
        if child.is_symlink() or entries > 30000:
            raise ValueError("Trace contains symlinks or too many files")
        if child.is_file():
            total += child.stat().st_size
            if total > maximum:
                raise CaptureFailure(
                    "trace_budget_exceeded",
                    "trace_validation",
                    "Trace exceeds its size budget",
                )
    return total


def export_xml(trace, output, arguments, control):
    """Export to disk; never accumulate arbitrary XML/environment in subprocess output."""
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(
            [
                "xcrun",
                "xctrace",
                "export",
                "--input",
                str(trace),
                *arguments,
                "--output",
                str(output),
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
        )
        try:
            while process.poll() is None:
                control.check()
                if Path(output).exists() and Path(output).stat().st_size > 16777216:
                    raise ValueError("Instruments XML exceeds 16 MiB")
                time.sleep(0.05)
            if process.returncode or not Path(output).is_file():
                raise ValueError(
                    "xctrace export failed; no raw process environment was retained in the report"
                )
            if Path(output).stat().st_size > 16777216:
                raise ValueError("Instruments XML exceeds 16 MiB")
            return Path(output).read_bytes()
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)


def read_trace(
    path,
    *,
    target_pid,
    run_number=1,
    max_bytes=268435456,
    control=None,
    allow_device_scope=False,
):
    path = Path(path).expanduser().resolve()
    trace_size = tree_bytes(path, max_bytes)
    control = control or RunControl(timeout_seconds=60)
    with tempfile.TemporaryDirectory(prefix="iatest-trace-export-") as temp:
        directory = Path(temp)
        metadata = toc_metadata(
            export_xml(path, directory / "toc.xml", ["--toc"], control), run_number
        )
        metadata["trace_bytes"] = trace_size
        if allow_device_scope:
            if (
                metadata["target_pid"] != 0
                or metadata["template"] != "Activity Monitor"
            ):
                raise ValueError(
                    "Device process statistics require a device-wide Activity Monitor trace"
                )
            metadata.update(
                recording_scope="device_process_statistics", selected_pid=target_pid
            )
        elif metadata["target_pid"] != target_pid:
            raise ValueError("Trace target PID differs from the declared process")
        schema = next((s for s in SCHEMAS if s in metadata["schemas"]), "")
        if not schema:
            from iatest.mobile.ios_frames import FRAME_SCHEMAS, parse_frame_xml

            if all(s in metadata["schemas"] for s in FRAME_SCHEMAS):
                tables = [
                    export_xml(
                        path,
                        directory / (s + ".xml"),
                        [
                            "--xpath",
                            f'/trace-toc/run[@number="{int(run_number)}"]/data/table[@schema="{s}"]',
                        ],
                        control,
                    )
                    for s in FRAME_SCHEMAS
                ]
                return {**parse_frame_xml(*tables, target_pid), "metadata": metadata}
        if not schema:
            return {
                "samples": [],
                "missing": ["cpu_percent", "memory_footprint_kb", "resident_memory_kb"],
                "schema": "",
                "notes": ["No supported process table in the selected trace run"],
                "metadata": metadata,
            }
        xpath = f'/trace-toc/run[@number="{int(run_number)}"]/data/table[@schema="{schema}"]'
        parsed = parse_process_xml(
            export_xml(path, directory / "process.xml", ["--xpath", xpath], control),
            target_pid,
        )
        return {**parsed, "metadata": metadata}


class XctraceRecorder:
    """Explicit recording scopes; device_processes never launches the target."""

    def __init__(
        self,
        target_pid=None,
        control=None,
        *,
        device_id=None,
        mode="attach",
        app_id=None,
        duration_seconds=120,
        ready_timeout_seconds=20,
        max_trace_bytes=268435456,
        finalize_timeout_seconds=30,
        template="activity_monitor",
        tool_launch="standard",
        stop_strategy="interrupt",
        process_factory=subprocess.Popen,
    ):
        if mode not in {"attach", "launch", "device_processes"}:
            raise ValueError("Unknown recording mode")
        if template not in {"activity_monitor", "animation_hitches", "frame_lifetimes"}:
            raise ValueError("Unknown Instruments recording template")
        if tool_launch not in {"standard", "isolated_copy"}:
            raise ValueError("Unknown Instruments tool launch strategy")
        if stop_strategy not in {"interrupt", "time_limit"}:
            raise ValueError("Unknown Instruments stop strategy")
        self.stop_strategy = stop_strategy
        if mode == "device_processes" and template != "activity_monitor":
            raise ValueError("Device process statistics require Activity Monitor")
        self.template = {
            "activity_monitor": "Activity Monitor",
            "animation_hitches": "Animation Hitches",
            "frame_lifetimes": "Blank",
        }[template]
        self.template_key, self.template_path = template, None
        if mode in {"attach", "device_processes"} and (
            type(target_pid) is not int or not 0 < target_pid <= 2147483647
        ):
            raise ValueError("Recording requires an explicit target PID")
        if mode == "launch" and (
            target_pid is not None
            or not device_id
            or not isinstance(app_id, str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_-]+)+", app_id)
        ):
            raise ValueError(
                "Launch recording requires a device/App ID and cannot bind a pre-launch PID"
            )
        self.target_pid, self.control, self.device_id = target_pid, control, device_id
        self.control = control or RunControl(timeout_seconds=duration_seconds + 90)
        self.mode, self.app_id = mode, app_id
        self.finalize_timeout_seconds = finalize_timeout_seconds
        self.duration_seconds, self.ready_timeout_seconds, self.maximum = (
            duration_seconds,
            ready_timeout_seconds,
            max_trace_bytes,
        )
        self.process_factory, self.process = process_factory, None
        self.temporary = tempfile.TemporaryDirectory(prefix="iatest-instruments-")
        self.directory = Path(self.temporary.name)
        self.trace = self.directory / "capture.trace"
        self.log = (self.directory / "recorder.log").open("w+b")
        self.stopping, self.watchdog = threading.Event(), None
        self.failure, self.ready = "", False
        self.capture_failure = None
        self.finishing, self.ready_at = False, None
        self.executable_prefix = ["xcrun", "xctrace"]
        self.launch_metadata = {"tool_launch": tool_launch}

    def argv(self):
        return [
            *self.executable_prefix,
            "record",
            "--template",
            str(self.template_path or self.template),
            *(
                ["--instrument", "Hitches", "--instrument", "Frame Lifetimes"]
                if self.template_key == "frame_lifetimes"
                else []
            ),
            "--time-limit",
            f"{self.duration_seconds}s",
            "--output",
            str(self.trace),
            "--no-prompt",
            *(["--device", self.device_id] if self.device_id else []),
            *(
                ["--launch", "--", self.app_id]
                if self.mode == "launch"
                else ["--all-processes"]
                if self.mode == "device_processes"
                else ["--attach", str(self.target_pid)]
            ),
        ]

    def _startup_error(self):
        content = (self.directory / "recorder.log").read_bytes()[:65536].lower()
        code, message = (
            "recorder_exited",
            "Instruments exited before readiness; no measured UI actions were started",
        )
        if b"cannot find process" in content:
            code, message = (
                "target_not_found",
                "Instruments did not discover the verified target process; no measured UI actions were started",
            )
        elif any(
            text in content for text in (b"device is locked", b"unlock the device")
        ):
            code, message = "device_locked", "The selected device must be unlocked"
        elif any(
            text in content
            for text in (b"permission denied", b"not authorized", b"not trusted")
        ):
            code, message = (
                "authorization_failed",
                "Instruments could not authorize recording",
            )
        elif any(
            text in content for text in (b"no device matching", b"device disconnected")
        ):
            code, message = (
                "device_unavailable",
                "The selected device is unavailable to Instruments",
            )
        return CaptureFailure(
            code,
            "target_discovery" if code == "target_not_found" else "startup",
            message,
        )

    def start(self):
        try:
            if sys.platform != "darwin":
                raise ValueError("Live Instruments recording requires macOS/Xcode")
            environment = {**os.environ, "LC_ALL": "C", "LANG": "en_US.UTF-8"}
            if self.launch_metadata["tool_launch"] == "isolated_copy":
                from iatest.mobile.ios_tool import prepare_isolated_xctrace

                self.executable_prefix, environment, self.launch_metadata = (
                    prepare_isolated_xctrace(self.directory, self.control)
                )
            if self.template_key == "frame_lifetimes":
                from iatest.mobile.ios_tool import prepare_frame_template

                self.template_path, developer, metadata = prepare_frame_template(
                    self.control
                )
                if (
                    self.launch_metadata["tool_launch"] == "isolated_copy"
                    and environment["DEVELOPER_DIR"] != str(developer)
                ):
                    raise ValueError("Selected Xcode changed while preparing frame recording")
                environment["DEVELOPER_DIR"] = str(developer)
                self.launch_metadata.update(metadata)
            self.process = self.process_factory(
                self.argv(),
                stdin=subprocess.DEVNULL,
                stdout=self.log,
                stderr=self.log,
                env=environment,
            )
            deadline = time.monotonic() + self.ready_timeout_seconds
            while True:
                self.control.check()
                if self.process.poll() is not None:
                    raise self._startup_error()
                if self.log.tell() > 65536:
                    raise ValueError("Instruments recorder log exceeds its budget")
                # This is the readiness message from the installed CLI, not a sleep.
                if (
                    b"Ctrl-C to stop the recording"
                    in (self.directory / "recorder.log").read_bytes()
                ):
                    self.ready = True
                    self.ready_at = time.monotonic()
                    break
                if time.monotonic() >= deadline:
                    raise CaptureFailure(
                        "startup_timeout",
                        "startup",
                        "Instruments did not acknowledge recording readiness",
                    )
                self.control.wait(0.05)
            self.watchdog = threading.Thread(target=self._watch, daemon=True)
            self.watchdog.start()
        except BaseException:
            self.close()
            raise

    def _watch(self):
        while not self.stopping.wait(0.1):
            try:
                if self.process.poll() is not None and not self.finishing:
                    self.failure = (
                        "Recorder ended before the measured UI journey finished"
                    )
                elif (self.directory / "recorder.log").stat().st_size > 65536:
                    self.failure = "Recorder log exceeded its budget"
                elif self.trace.exists():
                    tree_bytes(self.trace, self.maximum)
            except (OSError, ValueError) as exc:
                self.failure = str(exc)
                if isinstance(exc, CaptureFailure):
                    self.capture_failure = exc
            if self.failure:
                self.control.cancelled.set()
                return

    def stop(self):
        self.stopping.set()
        if self.watchdog:
            self.watchdog.join(timeout=1)
        if not self.process:
            return
        if self.process.poll() is None:
            try:
                self.process.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass  # The owned recorder can finish between poll and signal.
            try:
                self.process.wait(
                    timeout=3
                    if self.control.reason() or self.failure
                    else self.finalize_timeout_seconds
                )
            except subprocess.TimeoutExpired:
                self.failure = (
                    self.failure
                    or "Recorder did not finalize within its cleanup budget"
                )
                self.capture_failure = self.capture_failure or CaptureFailure(
                    "finalization_timeout", "finalization", self.failure
                )
                try:
                    self.process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        self.process.kill()
                    except ProcessLookupError:
                        pass
                    self.process.wait(timeout=2)
        if self.process.returncode:
            self.failure = self.failure or "Recorder did not finalize successfully"

    def finish(self):
        if self.process and self.process.poll() is not None:
            self.failure = (
                self.failure or "Recorder ended before the measured UI journey finished"
            )
        self.finishing = True
        if self.stop_strategy == "time_limit" and not self.failure:
            try:
                self._wait_for_window()
            except BaseException:
                self.failure = self.failure or "Fixed recording window did not complete"
                self.stop()
                raise
        self.stop()
        if self.failure:
            raise self.capture_failure or ValueError(self.failure)
        remaining = self.control.remaining()
        export_control = RunControl(
            timeout_seconds=min(20, remaining) if remaining is not None else 20,
            cancellation_check=lambda: bool(self.control.reason()),
        )
        parsed = read_trace(
            self.trace,
            target_pid=self.target_pid,
            max_bytes=self.maximum,
            control=export_control,
            allow_device_scope=self.mode == "device_processes",
        )
        self.control.check()
        if self.template_key == "frame_lifetimes" and (
            parsed.get("schema") != "xctrace-frame-lifetimes/1"
            or parsed["metadata"].get("template") != "Blank"
        ):
            raise CaptureFailure(
                "frame_tables_unavailable",
                "export",
                "The selected frame instruments did not export the required frame tables",
            )
        parsed["metadata"].update(self.launch_metadata)
        parsed["metadata"]["stop_strategy"] = self.stop_strategy
        return parsed

    def _wait_for_window(self):
        if not self.ready or self.ready_at is None:
            raise ValueError("A fixed recording window requires recorder readiness")
        self.stopping.set()
        if self.watchdog:
            self.watchdog.join(timeout=1)
        deadline = self.ready_at + self.duration_seconds + self.finalize_timeout_seconds
        while self.process.poll() is None:
            self.control.check()
            if time.monotonic() >= deadline:
                raise CaptureFailure(
                    "finalization_timeout",
                    "finalization",
                    "Instruments did not complete its fixed recording window",
                )
            if (self.directory / "recorder.log").stat().st_size > 65536:
                raise ValueError("Instruments recorder log exceeds its budget")
            if self.trace.exists():
                tree_bytes(self.trace, self.maximum)
            self.control.wait(0.05)

    def close(self):
        try:
            self.stop()
        finally:
            if not self.log.closed:
                self.log.close()
                self.temporary.cleanup()
