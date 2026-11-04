from __future__ import annotations

import hashlib
import json
import math
import threading
import time
import uuid
from pathlib import Path

from iatest.core.files import atomic_write_bytes
from iatest.core.schema import schema_errors
from iatest.engines.contracts import EngineContext, EngineResult, ExecutionPlan
from iatest.execution.control import RunCancelled, RunControl
from iatest.mobile.contracts import TARGET_SCHEMA, validate_mobile_plan
from iatest.mobile.devices import NativeDevice
from iatest.mobile.lease import DeviceLease
from iatest.mobile.protocol import (
    CommandRejected,
    DeliveryUnknown,
    ObservationRejected,
    ProtocolError,
)
from iatest.mobile.store import MobileStore
from iatest.mobile.visual import MobileVisual


class ObservationIncomplete(RuntimeError):
    pass


class MobileSession(MobileVisual):
    def __init__(
        self,
        profile: dict,
        *,
        device_factory=NativeDevice,
        control=None,
        root=None,
        project=None,
    ):
        self.emulator = None
        if profile.get("managed_emulator"):
            from iatest.mobile.emulator import AndroidEmulator, borrowed_vm
            from iatest.mobile.store import validate_profile

            validate_profile(profile)
            if root is None or project is None:
                raise ValueError("Managed VM requires a project and frozen App artifact")
            if not borrowed_vm(profile):
                self.emulator = AndroidEmulator(profile["managed_emulator"], control)
                profile = self.emulator.prepare_profile(profile, root, project)
        self.profile = profile
        self.id = "session-" + uuid.uuid4().hex[:20]
        self.lease = DeviceLease(profile["platform"], profile["device_id"])
        try:
            self.device = device_factory(profile, self.id)
        except BaseException:
            if self.emulator:
                self.emulator.close()
            raise
        self.device_factory = device_factory
        self.device.control = control
        from iatest.mobile.isolation import DeviceNetworkIsolation

        self.isolation = DeviceNetworkIsolation(self.device, profile)
        self.global_guard = None

        def unknown():
            from iatest.nodes.coordinator import HELD

            for guard in HELD.get().values():
                guard.uncertain = True
            if self.global_guard:
                self.global_guard.uncertain = True

        self.device.on_unknown = unknown
        self.lock = threading.RLock()
        self.mode = "automatic"
        self.last_used = time.monotonic()
        self.closed = False
        self.media_receiver = None
        self.native_snapshot_attempts = 0
        self.stop_heartbeat = threading.Event()
        try:
            self.lease.acquire()
            if root is not None:
                from iatest.nodes.coordinator import device_guard

                self.global_guard = device_guard(root, profile)
                if self.global_guard:
                    self.device.control = RunControl(
                        cancellation_check=lambda: (
                            not self.global_guard.alive()
                            or bool(control and control.reason())
                        )
                    )
            self.isolation.open()
            self.device.open()
            if self.global_guard:
                self.global_guard.safe = True
        except BaseException as error:
            try:
                try:
                    self.device.close()
                finally:
                    self.isolation.close()
            except Exception as cleanup_error:  # noqa: BLE001 -- Preserve the original startup/cancellation error.
                error.add_note(f"Native startup cleanup failed: {cleanup_error}")
            finally:
                if self.emulator:
                    self.emulator.close()
                self.lease.close()
                if self.global_guard:
                    self.global_guard.close()
            raise

        def heartbeat():
            while not self.stop_heartbeat.wait(10):
                with self.lock:
                    if self.closed:
                        return
                    try:
                        for channel in {self.device.input, self.device.semantic}:
                            channel.request("ping", {}, timeout=2)
                    except (OSError, EOFError, RuntimeError):
                        self.closed = True
                        try:
                            try:
                                self.device.close()
                            finally:
                                self.isolation.close()
                        finally:
                            if self.emulator:
                                self.emulator.close()
                            self.lease.close()
                            if self.global_guard:
                                self.global_guard.uncertain = True
                                self.global_guard.close()
                        return

        self.heartbeat = threading.Thread(
            target=heartbeat, name="iatest-mobile-heartbeat", daemon=True
        )
        self.heartbeat.start()

    @property
    def query_timeout(self) -> float:
        # XCTest snapshots include opaque platform calls on a physical device.
        # This is still capped by the caller's task/step budget during execution.
        if self.profile["platform"] == "ios":
            return 5
        # Software-rendered cold VMs can spend longer in opaque Binder calls.
        # Keep the phone default, and cap the VM snapshot separately on-device.
        return 2 if self.profile.get("managed_emulator") else 0.5

    @property
    def action_timeout(self) -> float:
        if self.profile["platform"] == "ios":
            return 20
        return 15 if self.profile.get("managed_emulator") else 5

    def query(
        self,
        target: dict,
        *,
        timeout: float | None = None,
        max_nodes: int = 1024,
        limit: int = 3,
        cache: bool = True,
    ) -> dict:
        if errors := schema_errors(target, TARGET_SCHEMA):
            raise ValueError("; ".join(errors))
        if "bounds" in target:
            bounds = target["bounds"]
            if (
                not all(math.isfinite(v) for v in bounds)
                or bounds[2] <= bounds[0]
                or bounds[3] <= bounds[1]
            ):
                raise ValueError(
                    "Spatial bounds require a non-empty target rectangle"
                )
            if self.profile["platform"] == "android" and any(int(v) != v for v in bounds):
                raise ValueError("Android spatial bounds require integer pixels")
            if self.profile["platform"] == "ios" and not self.device.semantic.capabilities.get("spatial_bounds"):
                raise ValueError("Rebuild the iOS Runner to enable exact spatial bounds")
        self.last_used = time.monotonic()
        timeout = self.query_timeout if timeout is None else timeout
        arguments = {"target": target, "limit": limit, "max_nodes": max_nodes, "cache": cache}
        if self.profile["platform"] == "android" and self.profile.get("managed_emulator"):
            arguments["snapshot_budget_ms"] = min(1000, max(1, int(timeout * 1000) - 200))
        response = self.device.semantic.request(
            "query",
            arguments,
            timeout=timeout,
        )
        self.native_snapshot_attempts = getattr(
            self, "native_snapshot_attempts", 0
        ) + response.get("query_attempts", 1)
        return response

    def outline(self, *, timeout=2, max_nodes=512, limit=12):
        if self.closed:
            raise ProtocolError("Session is closed")
        if "outline" not in self.device.semantic.capabilities.get("actions", []):
            raise ValueError(
                "Rebuild the native driver to enable bounded control discovery"
            )
        if not 1 <= max_nodes <= 1024 or not 1 <= limit <= 12:
            raise ValueError("Outline node/control budget is out of range")
        self.last_used = time.monotonic()
        return self.device.semantic.request(
            "outline", {"max_nodes": max_nodes, "limit": limit}, timeout=timeout
        )

    @staticmethod
    def unique(observation: dict) -> dict:
        if observation.get("scope_error"):
            raise ObservationIncomplete(observation["scope_error"])
        if observation.get("complete") is not True or observation.get(
            "match_count_is_lower_bound"
        ):
            raise ObservationIncomplete(
                "Query incomplete; cannot prove target uniqueness or absence"
            )
        count, candidates = (
            observation.get("match_count"),
            observation.get("candidates", []),
        )
        if count != 1 or len(candidates) != 1:
            raise ObservationIncomplete(f"Expected one target, observed {count}")
        return candidates[0]

    def wait_android_ready(self, timeout):
        deadline = time.monotonic() + timeout
        previous_bounds = None
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise ObservationIncomplete("App launch acknowledged but native focus not ready")
            try:
                state_timeout = 5 if self.device.semantic.capabilities.get("window_animation_sync") else self.query_timeout
                state = self.device.semantic.request("state", {}, timeout=min(state_timeout, left))
                if not self.device.semantic.capabilities.get("lifecycle_geometry_checks"):
                    break
                bounds, display = state.get("root_bounds", []), state.get("display", {})
                visible = len(bounds) == 4 and 0 <= bounds[0] < bounds[2] <= display.get("width", 0) and 0 <= bounds[1] < bounds[3] <= display.get("height", 0)
                if visible and previous_bounds == bounds:
                    break
                previous_bounds = bounds if visible else None
                if self.device.control:
                    self.device.control.wait(min(0.1, left))
                else:
                    time.sleep(min(0.1, left))
            except CommandRejected as error:
                if not any(message in str(error) for message in (
                    "App is not focused on selected display", "Target App is not foreground"
                )):
                    raise
                if self.device.control:
                    self.device.control.wait(min(0.05, left))
                else:
                    time.sleep(min(0.05, left))

    def action(
        self, step: dict, observation: dict | None, operation_id: str, timeout: float
    ) -> dict:
        if self.closed:
            raise RuntimeError("Session is closed")
        op = step["op"]
        if self.profile.get("ios_input_backend") in {"usb_hid", "android_bluetooth_hid"} and op in {
            "tap", "text", "swipe", "pinch", "gesture", "ime", "key", "visual_click", "fragment",
        }:
            raise ValueError("External HID profile requires explicit keyboard/mouse hid steps")
        tap_backend = step.get("tap_backend", "pointer")
        if "tap_backend" in step and (
            op != "tap"
            or self.profile["platform"] != "android"
            or tap_backend not in {"pointer", "accessibility"}
        ):
            raise ValueError("Invalid Android tap backend")
        arguments = {}
        channel = self.device.input
        if op in {"tap", "text", "swipe", "pinch", "gesture", "ime"}:
            node = self.unique(observation)
            if not node.get("enabled", True):
                raise ObservationIncomplete("Target is disabled")
            arguments = {
                "ref": node["ref"],
                "state_version": observation.get("state_version"),
            }
            if op == "tap" and tap_backend == "accessibility":
                if "accessibility" not in self.device.semantic.capabilities.get(
                    "tap_backends", []
                ) or not node.get("supports_accessibility_tap"):
                    raise ValueError(
                        "Driver/target does not advertise accessibility tap"
                    )
                channel = self.device.semantic
                arguments["tap_backend"] = tap_backend
            if self.profile["platform"] == "android" and op != "text":
                left, top, right, bottom = node["bounds"]
                if right <= left or bottom <= top:
                    raise ObservationIncomplete("Target geometry is empty")
                arguments.update(
                    x=(left + right) / 2,
                    y=(top + bottom) / 2,
                    display=observation["display"],
                )
                if op == "swipe":
                    direction = step.get("direction", "up")
                    dx, dy = {
                        "up": (0, -0.35),
                        "down": (0, 0.35),
                        "left": (-0.35, 0),
                        "right": (0.35, 0),
                    }[direction]
                    arguments.update(
                        end_x=arguments["x"] + dx * (right - left),
                        end_y=arguments["y"] + dy * (bottom - top),
                        duration_ms=step.get("duration_ms", 300),
                    )
                elif op == "pinch":
                    radius = (
                        min(right - left, bottom - top) * 0.2 / max(1, step["scale"])
                    )
                    arguments.update(
                        radius=radius,
                        scale=step["scale"],
                        duration_ms=step.get("duration_ms", 300),
                    )
            if op == "text":
                if step["mode"] == "key_events" and not node.get("focused"):
                    raise ObservationIncomplete(
                        "Key-event text requires target focus; add an explicit tap before typing"
                    )
                arguments.update(value=step["value"], mode=step["mode"])
                channel = (
                    self.device.semantic
                    if step["mode"] in {"semantic", "keyboard"}
                    else self.device.input
                )
            elif op == "swipe":
                arguments.update(
                    direction=step.get("direction", "up"),
                    duration_ms=step.get("duration_ms", 300),
                )
            elif op == "pinch":
                arguments.update(scale=step["scale"], velocity=step.get("velocity", 1))
            elif op == "gesture":
                arguments.update(contacts=step["contacts"], bounds=node["bounds"])
            elif op == "ime":
                arguments.update(
                    ime_events=step["ime_events"], ime_backend=step["ime_backend"]
                )
                channel = self.device.semantic
        elif op == "key":
            self.device.semantic.request("state", {}, timeout=timeout)
            arguments["keycode"] = step["keycode"]
        if (
            self.profile["platform"] == "android" and op in {"tap", "swipe", "pinch", "gesture"}
            and channel is self.device.input
            and self.device.semantic.capabilities.get("pointer_target_validation")
        ):
            dispatch_deadline = time.monotonic() + timeout
            validation = self.device.semantic.request("pointer_validate", arguments, timeout=timeout)
            if validation.get("validated") is not True:
                raise ObservationIncomplete("Native pointer target was not validated")
            timeout = dispatch_deadline - time.monotonic()
            if timeout <= 0:
                raise ObservationIncomplete("Pointer budget exhausted before dispatch")
        if self.profile.get("ios_input_backend") == "simulator_hid" and op in {
            "tap",
            "swipe",
        }:
            from iatest.mobile.simulator_hid import inject

            response = inject(self.device, step, arguments, timeout)
        else:
            if self.profile["platform"] == "android" and op in {"launch", "terminate"}:
                deadline = time.monotonic() + timeout
                response = self.device.android_lifecycle(op, operation_id, timeout)
                if "reset_observation" in self.device.semantic.capabilities.get("actions", []):
                    self.device.semantic.request("reset_observation", {}, timeout=max(0.001, deadline-time.monotonic()))
                if op == "launch":
                    self.wait_android_ready(max(0.001, deadline-time.monotonic()))
            else:
                response = channel.request(
                    op, arguments, operation_id=operation_id, timeout=timeout
                )
        self.last_action_started_ms = response.get("started_timestamp_ms")
        if response.get("status") == "unknown":
            raise DeliveryUnknown(
                response.get("error", "Native action outcome unknown")
            )
        if response.get("status") == "failed" or response.get("accepted") is False:
            raise AssertionError(response.get("error", "Platform rejected input"))
        if response.get("status") != "injection_call_returned":
            raise ProtocolError("Backend did not report an action dispatch result")
        return response

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.stop_heartbeat.set()
            try:
                if self.media_receiver:
                    self.media_receiver.close()
                try:
                    self.device.close()
                finally:
                    self.isolation.close()
            except BaseException:
                if self.global_guard:
                    self.global_guard.uncertain = True
                raise
            finally:
                if self.emulator:
                    self.emulator.close()
                self.lease.close()
                if self.global_guard:
                    state = self.global_guard.close()
                    if state not in {"released", "already_closed"}:
                        raise ProtocolError(
                            "Shared device lease remains uncertain; reconciliation is required"
                        )

    def preview(self, *, separate=False, codec="png", request_keyframe=False):
        if self.closed:
            raise ProtocolError("Session is closed")
        limits = self.profile.get("media_limits", {})
        budgets = {"duration": limits.get("duration_seconds", 120),
                   "byte_limit": limits.get("byte_limit", 67108864)}
        if "media_limits" in self.profile and self.media_receiver and self.media_receiver.stop.is_set():
            raise ProtocolError("Explicit media budget stream stopped; stop the preview before opening another stream")
        if separate and codec == "h264-native":
            from iatest.mobile.video import EncodedPacketSource

            if not self.device.semantic.capabilities.get("continuous_video"):
                raise ProtocolError("Encoded video is unavailable")
            if self.media_receiver and not isinstance(
                self.media_receiver, EncodedPacketSource
            ):
                self.media_receiver.close()
                self.media_receiver = None
            if self.media_receiver is None or self.media_receiver.stop.is_set():
                self.media_receiver = EncodedPacketSource(
                    self.device.open_media("h264"), **budgets
                )
            return self.media_receiver.frame(request_keyframe)
        if separate and codec == "h264":
            from iatest.mobile.video import EncodedMediaReceiver

            if not self.device.semantic.capabilities.get("continuous_video"):
                raise ProtocolError("This driver does not support encoded video")
            if self.media_receiver and not isinstance(
                self.media_receiver, EncodedMediaReceiver
            ):
                self.media_receiver.close()
                self.media_receiver = None
            if self.media_receiver is None or self.media_receiver.stop.is_set():
                if self.media_receiver:
                    self.media_receiver.close()
                self.media_receiver = EncodedMediaReceiver(
                    self.device.open_media("h264"), **budgets
                )
            return self.media_receiver.frame()
        if separate and self.device.semantic.capabilities.get("separate_png_media"):
            from iatest.mobile.media import MediaReceiver

            if self.media_receiver and not isinstance(
                self.media_receiver, MediaReceiver
            ):
                self.media_receiver.close()
                self.media_receiver = None
            if self.media_receiver is None or self.media_receiver.stop.is_set():
                if self.media_receiver is not None:
                    self.media_receiver.close()
                self.media_receiver = MediaReceiver(self.device.open_media(), **budgets)
            return self.media_receiver.frame()
        return self.device.semantic.request("screenshot", {}, timeout=5)

    def stop_preview(self, receiver_id):
        if self.media_receiver and self.media_receiver.id == receiver_id:
            self.media_receiver.close()
            self.media_receiver = None

    def fragment(self, step, operation_id, timeout):
        if self.profile.get("ios_input_backend") in {"usb_hid", "android_bluetooth_hid"}:
            raise ValueError("External HID profile cannot dispatch XCTest input fragments")
        if "fragment" not in self.device.semantic.capabilities.get("actions", []):
            raise ProtocolError("Device does not support on-device fragments")
        result = self.device.semantic.request(
            "fragment",
            {"steps": step["steps"]},
            operation_id=operation_id,
            timeout=min(5, timeout),
        )
        if result.get("status") == "unknown":
            raise DeliveryUnknown(
                "Native fragment stopped with uncertain input; never replay the fragment"
            )
        if result.get("status") == "failed":
            raise AssertionError("Native fragment postcondition failed")
        if result.get("status") != "injection_call_returned" or not result.get(
            "postcondition_verified"
        ):
            raise ProtocolError(
                "Native fragment did not return complete postcondition evidence"
            )
        return result


class MobileEngine:
    def __init__(
        self, root: Path, project: str, platform: str, *, device_factory=NativeDevice
    ):
        self.store = MobileStore(root, project)
        self.root, self.project = root, project
        self.platform, self.device_factory = platform, device_factory

    def capabilities(self) -> dict:
        profiles = [p for p in self.store.list("profile") if p["platform"] == self.platform] if self.store.has_assets() else []
        return {
            "kind": self.platform,
            "available": True,
            "availability": "requires_device_profile_and_native_artifacts",
            "readiness": {
                "state": "requires_verification" if profiles else "requires_configuration",
                "configured_profiles": len(profiles),
                "runtime_verified": False,
                "requirements": ["Connected authorized device", "Matching native artifacts and device profile", "Session capability probe and business result readback"],
            },
            "actions": [
                "find",
                "tap",
                "swipe",
                "key",
                "text",
                "expect",
                "wait",
                "screenshot",
                "fragment",
                "pinch",
                "visual_click",
                "gesture",
                "ime",
                "switch_app",
                "permission",
                "clipboard_set",
                "clipboard_expect",
                "web",
                "input_state_expect",
                "hid",
            ],
            "query": "bounded-target-query",
            "continuous_video": True,
            "os_sandbox": "owned Android emulator VM only; physical phones require their own platform facilities",
            "ios_input_backends": ["xctest", "simulator_hid", "usb_hid", "android_bluetooth_hid"] if self.platform == "ios" else [],
            "limitations": [
                "Device capabilities are probed at session start",
                "No unverified IME or continuous-video fallback",
            ],
        }

    def execute(self, plan: ExecutionPlan, context: EngineContext) -> EngineResult:
        validate_mobile_plan(plan)
        if plan.kind != self.platform:
            raise ValueError("Plan platform does not match engine")
        profile = self.store.get("profile", plan.spec["profile"])
        if profile["platform"] != self.platform:
            raise ValueError("Plan platform does not match device profile")
        result = EngineResult(
            plan.id, plan.kind, run_id="mobile-" + uuid.uuid4().hex[:20]
        )
        result.metrics = {
            "actions": 0,
            "queries": 0,
            "native_snapshot_attempts": 0,
            "evidence_bytes": 0,
            "visited_nodes": 0,
            "model_calls": 0,
            "query_recipe_hits": 0,
            "query_cache_enabled": plan.spec.get("cache", True),
        }
        session = None
        system = None
        collector = None
        budget = plan.budget
        parent_control = context.control
        control = RunControl(
            timeout_seconds=float(budget.get("timeout_seconds", 60)),
            cancellation_check=lambda: bool(parent_control.reason()),
        )
        start = time.monotonic()
        local_deadline = start + float(budget.get("timeout_seconds", 60))
        recipes, bound = {}, {}

        def remaining(default=5):
            control.check()
            seconds = min(default, local_deadline - time.monotonic())
            parent = control.remaining()
            if parent is not None:
                seconds = min(seconds, parent)
            if seconds <= 0:
                raise TimeoutError("Mobile task deadline exceeded")
            return seconds

        def observe(target, *, timeout=None):
            if result.metrics["queries"] >= budget.get("max_queries", 200):
                raise ObservationIncomplete("Query budget exhausted")
            key = json.dumps(target, sort_keys=True, ensure_ascii=False)
            if plan.spec.get("cache", True) and key in recipes:
                # Cache descriptions only. Every action/assertion obtains a new platform observation.
                result.metrics["query_recipe_hits"] += 1
            recipes[key] = target
            result.metrics["queries"] += 1
            value = session.query(
                target,
                timeout=remaining(
                    session.query_timeout
                    if timeout is None
                    else min(session.query_timeout, timeout)
                ),
                cache=plan.spec.get("cache", True)
                and target.get("require_unique", True),
            )
            result.metrics["native_query_plan_cache_hits"] = value.get(
                "query_plan_cache_hits", 0
            )
            result.metrics["native_node_cache_hits"] = value.get("node_cache_hits", 0)
            result.metrics["visited_nodes"] += value.get("visited_nodes", 0)
            if value.get("visited_nodes_unknown"):
                result.metrics["platform_internal_queries_unknown"] = (
                    result.metrics.get("platform_internal_queries_unknown", 0) + 1
                )
            return value

        try:
            remaining()
            from iatest.mobile.system import preflight

            preflight(profile, plan.spec)
            session = MobileSession(
                profile, device_factory=self.device_factory, control=control,
                **({"root": self.root, "project": self.project} if profile.get("managed_emulator") else {}),
            )
            from iatest.engines.client_performance import ACTIVE_CLIENT_CAPTURE

            active_capture = ACTIVE_CLIENT_CAPTURE.get()
            if self.platform == "ios":
                collector = active_capture
            elif active_capture and hasattr(active_capture, "prepare_ui_session"):
                active_capture.prepare_ui_session(session)
            result.metrics["native_capabilities"] = {
                "semantic": session.device.semantic.capabilities,
                "input": session.device.input.capabilities,
            }
            semantic = session.device.semantic.capabilities
            input_caps = session.device.input.capabilities
            supported = set(semantic.get("actions", [])) | set(
                input_caps.get("actions", [])
            )
            from iatest.mobile.system import SYSTEM_OPS, MobileSystem

            if any(step["op"] in SYSTEM_OPS for step in plan.spec["steps"]):
                system = MobileSystem(
                    session, self.root, self.project, plan.spec.get("webview")
                )
            for step in plan.spec["steps"]:
                if step["op"] in SYSTEM_OPS:
                    continue
                required = (
                    "query" if step["op"] in {"find", "wait", "expect"} else step["op"]
                )
                if required == "visual_click":
                    required = "tap"
                if required == "ime" and (
                    step["ime_backend"] not in semantic.get("ime_backends", [])
                    or self.platform == "android"
                    and not semantic.get("ime_selected")
                ):
                    raise ProtocolError("Declared IME backend is not ready/selected")
                if required == "gesture" and len(step["contacts"]) > input_caps.get(
                    "max_touch_points", 0
                ):
                    raise ProtocolError("Gesture exceeds native contact capability")
                if required not in supported:
                    raise ProtocolError(f"Native backend does not support {required}")
                if step["op"] == "text" and step["mode"] not in set(
                    semantic.get("input_modes", [])
                ) | set(input_caps.get("input_modes", [])):
                    raise ProtocolError(
                        f"Native backend does not support input mode {step['mode']}"
                    )
                if (
                    step["op"] == "key"
                    and "keycodes" in input_caps
                    and step["keycode"] not in input_caps["keycodes"]
                ):
                    raise ProtocolError(
                        "Native backend does not support requested keycode"
                    )
            if collector:
                collector.begin_ui_session(session)
            for index, step in enumerate(plan.spec["steps"]):
                remaining()
                started = time.monotonic()
                record = {"index": index, "op": step["op"], "status": "running"}
                result.steps.append(record)
                op = step["op"]
                target = step.get("target") or bound.get(step.get("ref"))
                if op in SYSTEM_OPS:
                    if result.metrics["actions"] >= budget.get("max_actions", 100):
                        raise ObservationIncomplete("System action budget exhausted")
                    result.metrics["actions"] += 1
                    response = system.execute(
                        step, budget.get("max_queries", 200) - result.metrics["queries"],
                        **({"max_actions": budget.get("max_actions", 100) - result.metrics["actions"] + 1} if op == "hid" else {}),
                    )
                    result.metrics["actions"] += response.pop("actions", 1) - 1
                    result.metrics["queries"] += response.pop("queries", 0)
                    record.update(
                        status="postcondition_verified"
                        if response.get("postcondition_verified")
                        else "injection_call_returned",
                        result=response,
                    )
                elif op == "find":
                    observation = observe(target)
                    session.unique(observation)
                    if step.get("bind"):
                        bound[step["bind"]] = target
                    record.update(status="observed", observation=observation)
                elif op in {"expect", "wait"}:
                    deadline = time.monotonic() + min(
                        step.get("timeout_ms", 5000) / 1000, remaining(60)
                    )
                    while True:
                        if time.monotonic() >= deadline:
                            last = record.get("observation", {})
                            if not last.get("complete") or last.get("scope_error"):
                                raise ObservationIncomplete("Expectation observation remained incomplete")
                            raise AssertionError("UI expectation did not match within the declared wait")
                        observation = observe(
                            target, timeout=max(0.001, deadline - time.monotonic())
                        )
                        # Retain the final bounded evidence when an expectation fails.
                        record["observation"] = observation
                        if (
                            observation.get("scope_error")
                            or observation.get("complete") is not True
                        ):
                            matched = False
                        else:
                            attribute = step.get("attribute", "exists")
                            expected = step.get("expected", True)
                            if attribute == "exists":
                                actual = observation.get("match_count", 0) > 0
                                if actual and target.get("require_unique", True):
                                    session.unique(observation)
                            else:
                                candidate = session.unique(observation)
                                if candidate.get("secure") and attribute in {
                                    "text",
                                    "value",
                                }:
                                    raise ObservationIncomplete(
                                        "Masked secure text cannot prove a raw-value assertion"
                                    )
                                if attribute in candidate.get(
                                    "truncated_attributes", []
                                ):
                                    raise ObservationIncomplete(
                                        "Attribute evidence was truncated"
                                    )
                                if attribute in candidate.get(
                                    "ambiguous_attributes", []
                                ):
                                    raise ObservationIncomplete(
                                        "Attribute evidence is ambiguous (placeholder or input)"
                                    )
                                actual = candidate.get(attribute)
                            operator = step.get("operator", "eq")
                            matched = (
                                actual == expected
                                if operator == "eq"
                                else actual != expected
                                if operator == "ne"
                                else isinstance(actual, str)
                                and isinstance(expected, str)
                                and expected in actual
                            )
                        if matched:
                            if time.monotonic() > deadline:
                                raise ObservationIncomplete(
                                    "Expectation observation arrived after its declared wait"
                                )
                            break
                        if time.monotonic() >= deadline:
                            if not observation.get("complete") or observation.get(
                                "scope_error"
                            ):
                                raise ObservationIncomplete(
                                    "Expectation observation remained incomplete"
                                )
                            raise AssertionError(
                                "UI expectation did not match within the declared wait"
                            )
                        control.wait(min(0.1, max(0.001, deadline - time.monotonic())))
                    record.update(
                        status="postcondition_verified", observation=observation
                    )
                elif op == "screenshot":
                    response = session.device.semantic.request(
                        "screenshot", {}, timeout=remaining()
                    )
                    data = response.pop("binary", b"")
                    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                        raise ProtocolError("Backend did not provide PNG evidence")
                    if result.metrics["evidence_bytes"] + len(data) > budget.get(
                        "max_evidence_bytes", 8 * 1024 * 1024
                    ):
                        raise ObservationIncomplete("Evidence budget exhausted")
                    name = uuid.uuid4().hex + ".png"
                    path = self.store.evidence_path(result.run_id, name, kind=result.kind)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_bytes(path, data)
                    result.metrics["evidence_bytes"] += len(data)
                    evidence = {
                        "type": "screenshot",
                        "name": name,
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "bytes": len(data),
                        "metadata": response,
                    }
                    result.evidence.append(evidence)
                    record.update(status="observed", evidence=evidence)
                elif op == "visual_click":
                    if result.metrics["actions"] + 1 > budget.get(
                        "max_actions", 100
                    ) or result.metrics["queries"] + 2 > budget.get("max_queries", 500):
                        raise ObservationIncomplete(
                            "Native visual action exceeds remaining budget"
                        )
                    result.metrics["actions"] += 1
                    result.metrics["queries"] += 2
                    response = session.visual_click(
                        step, f"{result.run_id}-{index}", remaining()
                    )
                    record.update(status="injection_call_returned", dispatch=response)
                elif op == "ime":
                    actions = sum(e["op"] != "expect" for e in step["ime_events"])
                    if result.metrics["actions"] + actions > budget.get(
                        "max_actions", 100
                    ):
                        raise ObservationIncomplete(
                            "IME sequence exceeds remaining action budget"
                        )
                    observation = observe(target)
                    result.metrics["actions"] += actions
                    response = session.action(
                        step,
                        observation,
                        f"{result.run_id}-{index}",
                        remaining(max(8, session.action_timeout)),
                    )
                    if not response.get("postcondition_verified"):
                        raise ObservationIncomplete(
                            "IME did not verify its declared native conditions"
                        )
                    record.update(status="postcondition_verified", dispatch=response)
                elif op == "fragment":
                    actions = sum(s["op"] != "expect" for s in step["steps"])
                    if result.metrics["actions"] + actions > budget.get(
                        "max_actions", 100
                    ) or result.metrics["queries"] + len(step["steps"]) > budget.get(
                        "max_queries", 500
                    ):
                        raise ObservationIncomplete(
                            "Native fragment exceeds remaining action/query budget"
                        )
                    result.metrics["actions"] += actions
                    result.metrics["queries"] += len(step["steps"])
                    response = session.fragment(
                        step, f"{result.run_id}-{index}", remaining()
                    )
                    record.update(status="postcondition_verified", dispatch=response)
                else:
                    step_deadline = time.monotonic() + remaining(max(8 if op == "gesture" else 5, session.action_timeout))
                    for attempt in range(3):
                        left = min(remaining(60), step_deadline - time.monotonic())
                        if left <= 0:
                            raise TimeoutError("Native step budget exhausted")
                        if result.metrics["actions"] >= budget.get("max_actions", 100):
                            raise ObservationIncomplete("Action budget exhausted")
                        observation = observe(target, timeout=left) if target else None
                        if observation is not None and not observation.get("complete"):
                            if attempt < 2 and not observation.get("scope_error"):
                                remaining()
                                continue
                            session.unique(observation)
                        result.metrics["actions"] += 1
                        try:
                            left = min(remaining(60), step_deadline - time.monotonic())
                            if left <= 0:
                                raise TimeoutError("Native step budget exhausted")
                            response = session.action(
                                step, observation,
                                f"{result.run_id}-{index}" + (f"-guard-{attempt}" if attempt else ""),
                                left,
                            )
                            break
                        except ObservationRejected:
                            # Only a correlated explicit pre-dispatch rejection can
                            # obtain a new observation and operation ID. Unknown
                            # delivery, timeouts and general errors never retry.
                            if attempt == 2 or not target:
                                raise
                            record["pre_dispatch_rejections"] = attempt + 1
                    record.update(status="injection_call_returned", dispatch=response)
                record["elapsed_ms"] = round((time.monotonic() - started) * 1000)
            result.verdict = "passed"
        except DeliveryUnknown as exc:
            result.verdict = "inconclusive"
            result.error = str(exc)
            if result.steps:
                result.steps[-1]["status"] = "unknown"
        except (ObservationIncomplete, ProtocolError) as exc:
            result.verdict = "inconclusive"
            result.error = str(exc)
        except AssertionError as exc:
            result.verdict = "failed"
            result.error = str(exc)
        except RunCancelled as exc:
            result.lifecycle = (
                "timed_out"
                if "时限" in (str(exc) + parent_control.reason())
                else "cancelled"
            )
            result.verdict = "inconclusive"
            result.error = str(exc)
        except TimeoutError as exc:
            result.lifecycle = "timed_out"
            result.verdict = "inconclusive"
            result.error = str(exc)
        except Exception as exc:  # noqa: BLE001 -- Engine boundary persists unexpected failures.
            result.lifecycle = "execution_error"
            result.verdict = "inconclusive"
            result.error = str(exc)
            from iatest.mobile.ios_capture import CaptureFailure

            if isinstance(exc, CaptureFailure):
                result.metrics["capture_diagnostics"] = {
                    "code": exc.code,
                    "phase": exc.phase,
                }
        finally:
            if session:
                result.metrics["native_snapshot_attempts"] = (
                    session.native_snapshot_attempts
                )
            if result.steps and result.steps[-1]["status"] == "running":
                result.steps[-1]["status"] = result.verdict
            if session:
                try:
                    try:
                        if system:
                            system.close()
                    finally:
                        try:
                            if collector:
                                try:
                                    collector.finish_ui_session()
                                except RunCancelled as exc:
                                    collector.values = {}
                                    collector.measurement_errors.append(str(exc))
                                    result.lifecycle = (
                                        "timed_out"
                                        if "时限" in (str(exc) + parent_control.reason())
                                        else "cancelled"
                                    )
                                    result.verdict = "inconclusive"
                                    result.error = str(exc)
                                except (OSError, RuntimeError, ValueError) as exc:
                                    collector.values = {}
                                    collector.measurement_errors.append(str(exc))
                                    collector.notes.append(
                                        "Measurement unavailable: " + str(exc)
                                    )
                                    from iatest.mobile.ios_capture import (
                                        CaptureFailure,
                                    )

                                    if isinstance(exc, CaptureFailure):
                                        result.metrics["capture_diagnostics"] = {
                                            "code": exc.code,
                                            "phase": exc.phase,
                                        }
                                finally:
                                    collector.close()
                        finally:
                            session.close()
                except Exception as exc:  # noqa: BLE001 -- Preserve teardown failures in the result.
                    result.limitations.append(
                        f"Native cleanup could not be confirmed: {exc}"
                    )
                    if result.verdict == "passed":
                        result.verdict = "inconclusive"
            if session and getattr(session, "emulator", None):
                result.metrics["os_sandbox"] = dict(session.emulator.evidence)
            result.metrics["elapsed_ms"] = round((time.monotonic() - start) * 1000)
            self.store.save_result(result)
        return result


class SessionManager:
    def __init__(self):
        self.sessions: dict[tuple[str, str], MobileSession] = {}
        self.lock = threading.RLock()

    def open(self, root: Path, project: str, profile_id: str) -> MobileSession:
        key = (str(root.resolve()), project)
        self.close(root, project)
        from iatest.execution.control import ACTIVE_RUNTIME

        runtime = ACTIVE_RUNTIME.get()
        session = MobileSession(
            MobileStore(root, project).get("profile", profile_id),
            control=runtime.control if runtime else None,
            root=root,
            project=project,
        )
        with self.lock:
            if key in self.sessions:
                session.close()
                raise ValueError("Another device session was opened concurrently")
            self.sessions[key] = session
            return session

    def get(self, root: Path, project: str) -> MobileSession:
        with self.lock:
            session = self.sessions.get((str(root.resolve()), project))
            if not session or session.closed:
                raise ValueError("Open a device session first")
            return session

    def close(self, root: Path, project: str) -> None:
        with self.lock:
            session = self.sessions.pop((str(root.resolve()), project), None)
        if session:
            session.close()

    def close_all(self) -> None:
        with self.lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
        for session in sessions:
            session.close()


SESSIONS = SessionManager()
