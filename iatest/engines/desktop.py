"""Framework-owned AX, UIAutomation and Linux AT-SPI desktop execution."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from iatest.core.resources import resource_scope
from iatest.engines.contracts import EngineResult
from iatest.engines.http import validate
from iatest.engines.web_visual import ANCHOR_SCHEMA
from iatest.execution.isolation import worker_environment

KEYS = [
    "Enter",
    "Tab",
    "Escape",
    "Backspace",
    "Delete",
    "Space",
    "ArrowLeft",
    "ArrowRight",
    "ArrowUp",
    "ArrowDown",
    "Home",
    "End",
    "PageUp",
    "PageDown",
]
ACTION_FIELDS = ("value", "key", "destination", "delta_x", "delta_y", "visual_anchor")

TARGET = {
    "type": "object",
    "additionalProperties": False,
    "minProperties": 1,
    "properties": {
        k: {"type": "string", "minLength": 1, "maxLength": 500}
        for k in ("identifier", "name", "role")
    },
}
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["platform", "process_id", "app_id", "steps"],
    "properties": {
        "platform": {"enum": ["macos", "windows", "linux"]},
        "process_id": {"type": "integer", "minimum": 1},
        "app_id": {"type": "string", "minLength": 1, "maxLength": 500},
        "wayland_portal": {"type": "boolean"},
        "wayland_backend": {"enum": ["keyboard", "gnome"]},
        "steps": {
            "type": "array",
            "minItems": 1,
            "maxItems": 200,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["op", "target"],
                "properties": {
                    "op": {
                        "enum": [
                            "find",
                            "click",
                            "fill",
                            "press",
                            "expect",
                            "right_click",
                            "drag",
                            "scroll",
                            "screenshot",
                            "visual_click",
                        ]
                    },
                    "target": TARGET,
                    "destination": TARGET,
                    "delta_x": {"type": "integer", "minimum": -20, "maximum": 20},
                    "delta_y": {"type": "integer", "minimum": -20, "maximum": 20},
                    "visual_anchor": ANCHOR_SCHEMA,
                    "value": {"type": "string", "maxLength": 16384},
                    "key": {"enum": KEYS},
                    "attribute": {
                        "enum": ["exists", "name", "text", "value", "enabled"]
                    },
                    "expected": {"type": ["string", "boolean"]},
                    "timeout_seconds": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "maximum": 10,
                    },
                },
            },
        },
    },
}


def validate_desktop(plan):
    validate(plan.spec, SCHEMA)
    if "wayland_backend" in plan.spec and not plan.spec.get("wayland_portal"):
        raise ValueError(
            "wayland_backend requires explicit wayland_portal authorization"
        )
    for step in plan.spec["steps"]:
        if step["op"] == "drag" and "destination" not in step:
            raise ValueError("Desktop drag requires a semantic destination")
        if step["op"] == "scroll" and not (step.get("delta_x") or step.get("delta_y")):
            raise ValueError("Desktop scroll requires nonzero wheel steps")
        if step["op"] == "visual_click" and "visual_anchor" not in step:
            raise ValueError("Desktop visual click requires an observed anchor")
        if step["op"] == "fill" and "value" not in step:
            raise ValueError("Desktop fill requires value")
        if step["op"] == "press" and "key" not in step:
            raise ValueError("Desktop key requires a supported key")
        if step["op"] == "expect" and not {"attribute", "expected"} <= step.keys():
            raise ValueError("Desktop assertions require an explicit predicate")
        if (
            step["op"] == "expect"
            and step["attribute"] in {"exists", "enabled"}
            and type(step["expected"]) is not bool
        ):
            raise ValueError("Desktop exists/enabled expectations must be booleans")
    if plan.spec["steps"][-1]["op"] != "expect":
        raise ValueError("Desktop plan must finish with a deterministic UI assertion")


def desktop_command(platform):
    if platform == "macos":
        executable = os.environ.get("IATEST_DESKTOP_MACOS_DRIVER", "")
        if sys.platform != "darwin" or not executable or not Path(executable).is_file():
            raise ValueError("Build and configure the macOS AX driver")
        return [executable]
    if platform == "linux":
        import ctypes.util

        if (
            not sys.platform.startswith("linux")
            or not os.environ.get("DISPLAY")
            or os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
            or os.environ.get("WAYLAND_DISPLAY")
        ):
            raise ValueError(
                "Linux desktop execution requires an X11 graphical session"
            )
        if not ctypes.util.find_library("atspi"):
            raise ValueError(
                "Install the desktop system's libatspi2.0 and accessibility bus"
            )
        return [
            sys.executable,
            "-I",
            "-B",
            str(Path(__file__).with_name("desktop_native") / "LinuxDesktop.py"),
        ]
    if platform != "windows":
        raise ValueError("Unsupported desktop platform")
    if os.name != "nt":
        raise ValueError("Windows desktop execution requires a Windows worker")
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if not shell:
        raise ValueError("Windows PowerShell is unavailable")
    return [
        shell,
        "-NoProfile",
        "-NonInteractive",
        # Fixed packaged commands, with request data only on stdin. This does
        # not change ExecutionPolicy or require unsigned script-file execution.
        "-Command",
        (Path(__file__).with_name("desktop_native") / "DesktopDriver.ps1").read_text(
            encoding="utf-8"
        ),
    ]


def wayland_command():
    import ctypes.util

    if (
        not sys.platform.startswith("linux")
        or not os.environ.get("WAYLAND_DISPLAY")
        or not os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    ):
        raise ValueError(
            "Wayland portal mode requires a live Wayland session and session bus"
        )
    if not ctypes.util.find_library("atspi") or not ctypes.util.find_library("gio-2.0"):
        raise ValueError("Wayland requires system AT-SPI and GIO libraries")
    return [
        sys.executable,
        "-I",
        "-B",
        str(Path(__file__).with_name("desktop_native") / "LinuxDesktop.py"),
    ]


class DesktopSession:
    def __init__(self, spec, control):
        self.spec, self.control = spec, control
        self.sequence = 0
        self.responses = queue.Queue(maxsize=2)
        self.temporary = tempfile.TemporaryDirectory(prefix="iatest-desktop-")
        try:
            portal = spec.get("wayland_portal", False)
            if type(portal) is not bool:
                raise ValueError("wayland_portal must be an explicit boolean")
            backend = spec.get("wayland_backend", "keyboard")
            if (
                backend not in {"keyboard", "gnome"}
                or "wayland_backend" in spec
                and not portal
            ):
                raise ValueError("Select an explicitly authorized Wayland backend")
            if portal and spec["platform"] != "linux":
                raise ValueError("Wayland portal mode requires Linux")
            environment = worker_environment(
                {
                    "env_allowlist": [
                        "DISPLAY",
                        "DBUS_SESSION_BUS_ADDRESS",
                        "AT_SPI_BUS_ADDRESS",
                        "XAUTHORITY",
                        "WAYLAND_DISPLAY",
                        "XDG_SESSION_TYPE",
                        "XDG_RUNTIME_DIR",
                    ]
                    if spec["platform"] == "linux"
                    else []
                },
                Path(self.temporary.name),
            )
            if portal:
                environment["IATEST_WAYLAND_PORTAL"] = "1"
            self.process = subprocess.Popen(
                wayland_command() if portal else desktop_command(spec["platform"]),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
                env=environment,
                start_new_session=os.name == "posix",
            )
        except BaseException:
            self.temporary.cleanup()
            raise

        def reader():
            try:
                for line in iter(lambda: self.process.stdout.readline(1048577), ""):
                    if len(line) > 1048576:
                        raise ValueError("Desktop reply exceeds quota")
                    self.responses.put(json.loads(line), timeout=1)
            except (ValueError, queue.Full):
                pass

        self.reader = threading.Thread(target=reader, daemon=True)
        self.reader.start()
        if portal:
            try:
                self.call("portal_start", backend=backend)
            except BaseException:
                self.close()
                raise

    def call(self, op, target=None, **kwargs):
        self.delivery_started = False
        self.control.check()
        self.sequence += 1
        packet = {
            "sequence": self.sequence,
            "process_id": self.spec["process_id"],
            "app_id": self.spec["app_id"],
            "op": op,
            "target": target or {},
            **kwargs,
        }
        raw = json.dumps(packet, ensure_ascii=False, allow_nan=False)
        if len(raw.encode()) > 65536:
            raise ValueError("Desktop input exceeds quota")
        self.delivery_started = op in {
            "click",
            "fill",
            "press",
            "right_click",
            "drag",
            "scroll",
            "pointer_click",
        }
        self.process.stdin.write(raw + "\n")
        self.process.stdin.flush()
        limit = (
            (150 if self.spec.get("wayland_backend") == "gnome" else 55)
            if op == "portal_start"
            else 5
        )
        deadline = time.monotonic() + min(limit, self.control.remaining() or limit)
        while True:
            self.control.check()
            if self.process.poll() is not None:
                raise RuntimeError(
                    "Desktop driver exited; action delivery may be unknown"
                )
            if time.monotonic() > deadline:
                raise TimeoutError("Desktop operation timed out; do not replay input")
            try:
                response = self.responses.get(timeout=0.05)
                break
            except queue.Empty:
                continue
        if response.get("sequence") != self.sequence:
            raise ValueError("Desktop response sequence differs")
        if response.get("error"):
            raise ValueError(response["error"])
        result = response["result"]
        if op == "query":
            candidates = result.get("candidates", [])
            if result.get("complete") and result.get("match_count") != len(candidates):
                raise ValueError(
                    "Desktop complete observation has inconsistent candidates"
                )
            for candidate in candidates:
                field = (
                    "value"
                    if candidate.get("role")
                    in {"AXStaticText", "AXTextField", "AXTextArea", "ControlType.Edit"}
                    else "name"
                )
                if "text" not in candidate:
                    candidate["text"] = (
                        candidate.get(field)
                        if isinstance(candidate.get(field), str)
                        else ""
                    )
                if field in candidate.get("truncated_attributes", []):
                    candidate["truncated_attributes"] = [
                        *candidate["truncated_attributes"],
                        "text",
                    ]
        return result

    def visual_region(self, target):
        import base64
        import hashlib

        from iatest.engines.web_visual import stable_png

        captures = []

        def capture():
            value = self.call("screenshot", target)
            captures.append(value)
            return base64.b64decode(value["data"], validate=True)

        data, width, height = stable_png(capture)
        if any(v["geometry"] != captures[-1]["geometry"] for v in captures):
            raise ValueError("Desktop region moved during capture")
        if any(
            v.get("binding") != captures[-1].get("binding")
            or v.get("target_geometry") != captures[-1].get("target_geometry")
            for v in captures
        ):
            raise ValueError("Desktop window identity changed during capture")
        geometry = captures[-1]["geometry"]
        result = {
            "data": data,
            "sha256": hashlib.sha256(data).hexdigest(),
            "width": width,
            "height": height,
            "viewport": {
                "width": geometry[2],
                "height": geometry[3],
                "dpr": width / geometry[2],
            },
        }
        if "binding" in captures[-1]:
            result["binding"] = captures[-1]["binding"]
            result["native_geometry"] = {
                "pixels": captures[-1]["geometry"],
                "target": captures[-1]["target_geometry"],
            }
        return result

    def visual_click(self, step):
        anchor = step["visual_anchor"]
        observed = self.visual_region(step["target"])
        if any(
            anchor[k] != observed[k] for k in ("sha256", "width", "height", "viewport")
        ):
            raise ValueError(
                "Desktop visual anchor expired: pixels or viewport changed"
            )
        if anchor.get("binding") != observed.get("binding"):
            raise ValueError("Desktop visual anchor expired: window identity changed")
        x, y = anchor["x"], anchor["y"]
        if not (0 <= x < observed["width"] and 0 <= y < observed["height"]):
            raise ValueError("Desktop visual point exceeds its region")
        extra = {}
        if "native_geometry" in observed:
            gx, gy, gw, gh = observed["native_geometry"]["pixels"]
            extra = {
                "capture_point": [
                    gx + x / observed["width"] * gw,
                    gy + y / observed["height"] * gh,
                ],
                "capture_geometry": observed["native_geometry"]["target"],
                "capture_binding": observed["binding"],
            }
        return self.call(
            "pointer_click",
            step["target"],
            fraction_x=x / observed["width"],
            fraction_y=y / observed["height"],
            **extra,
        )

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        self.reader.join(1)
        for stream in (self.process.stdin, self.process.stdout):
            stream.close()
        self.temporary.cleanup()


class DesktopEngine:
    def __init__(self, root, project):
        self.root, self.project = root, project

    def capabilities(self):
        from iatest.engines.readiness import prerequisite_status

        try:
            if sys.platform.startswith("linux") and os.environ.get("WAYLAND_DISPLAY"):
                wayland_command()
            else:
                desktop_command(
                    "macos"
                    if sys.platform == "darwin"
                    else "windows"
                    if os.name == "nt"
                    else "linux"
                )
            available = True
        except ValueError:
            available = False
        return {
            "available": available,
            "readiness": prerequisite_status(
                available,
                [
                    "Logged-in unlocked desktop",
                    "Accessibility permission and one exact target PID",
                    "App controls with supported accessibility patterns",
                ],
            ),
            "backends": [
                "macOS AX",
                "Windows UIAutomation",
                "Linux X11 AT-SPI",
                "Linux Wayland AT-SPI with explicit keyboard portal",
            ],
            "actions": [
                "find",
                "click",
                "fill",
                "press",
                "expect",
                "right_click",
                "drag",
                "scroll",
                "screenshot",
                "visual_click",
            ],
            "wayland_actions": ["find", "click", "fill", "press", "expect"],
            "wayland_gnome_actions": [
                "find",
                "click",
                "fill",
                "press",
                "expect",
                "right_click",
                "drag",
                "scroll",
                "screenshot",
                "visual_click",
            ],
            "limitations": [
                "Explicit PID and App identity; OS accessibility permission is required; no full-tree model dump"
            ],
        }

    def execute(self, plan, context):
        validate_desktop(plan)
        result = EngineResult(plan.id, plan.kind)
        import uuid

        result.run_id = "desktop-" + uuid.uuid4().hex[:20]
        evidence_bytes = 0
        session = None
        pending = False
        try:
            with resource_scope(
                [f"desktop:{plan.spec['platform']}:{plan.spec['process_id']}"],
                context.control,
            ):
                session = DesktopSession(plan.spec, context.control)
                for index, step in enumerate(plan.spec["steps"]):
                    context.control.check()
                    op = step["op"]
                    deadline = time.monotonic() + step.get("timeout_seconds", 3)
                    if op == "expect":
                        while True:
                            observed = session.call("query", step["target"])
                            if not observed.get("complete"):
                                raise ValueError(
                                    "Incomplete desktop query cannot prove an assertion"
                                )
                            candidates = observed["candidates"]
                            attribute = step["attribute"]
                            if any(
                                attribute in c.get("truncated_attributes", [])
                                for c in candidates
                            ):
                                raise ValueError(
                                    "Truncated desktop value cannot prove an assertion"
                                )
                            if attribute == "exists":
                                passed = len(candidates) == (
                                    1 if step["expected"] else 0
                                )
                            else:
                                passed = (
                                    len(candidates) == 1
                                    and candidates[0].get(attribute) == step["expected"]
                                )
                            if passed or time.monotonic() >= deadline:
                                break
                            context.control.wait(0.05)
                        row = {
                            "index": index,
                            "op": op,
                            "passed": passed,
                            "result": {"match_count": observed["match_count"]},
                        }
                        result.steps.append(row)
                        if not passed:
                            result.verdict = "failed"
                            break
                    else:
                        pending = op not in {"find", "screenshot"}
                        value = (
                            session.visual_click(step)
                            if op == "visual_click"
                            else session.call(
                                "query" if op == "find" else op,
                                step["target"],
                                **{k: step[k] for k in ACTION_FIELDS if k in step},
                            )
                        )
                        if op == "screenshot":
                            import base64

                            from iatest.core.files import atomic_write_bytes
                            from iatest.engines.store import EngineStore

                            data = base64.b64decode(value.pop("data"), validate=True)
                            evidence_bytes += len(data)
                            if evidence_bytes > plan.budget.get(
                                "max_evidence_bytes", 8388608
                            ):
                                raise ValueError("Desktop evidence quota exceeded")
                            name = uuid.uuid4().hex + ".png"
                            path = EngineStore(self.root, self.project).evidence_path(
                                result.run_id, name, kind=plan.kind
                            )
                            path.parent.mkdir(parents=True, exist_ok=True)
                            atomic_write_bytes(path, data)
                            value["name"] = name
                            result.evidence.append({"type": "screenshot", "name": name})
                        pending = False
                        result.steps.append({"index": index, "op": op, "result": value})
                if result.verdict == "not_evaluated":
                    result.verdict = "passed"
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            result.verdict = "inconclusive"
            result.error = str(exc)
            result.metrics["unknown_delivery"] = pending and bool(
                session and session.delivery_started
            )
        finally:
            if session:
                session.close()
        return result
