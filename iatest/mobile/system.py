"""Explicitly authorized App transitions, temporary permissions, clipboard and WebViews."""

from __future__ import annotations

import hashlib
import re
import time

import httpx

from iatest.engines.cdp import CDP
from iatest.engines.http import NetworkScope
from iatest.engines.web import BrowserSession
from iatest.mobile.protocol import DeliveryUnknown, ProtocolError

SYSTEM_OPS = {
    "switch_app",
    "permission",
    "clipboard_set",
    "clipboard_expect",
    "web",
    "input_state_expect",
    "hid",
}


def preflight(profile, spec):
    """Reject unauthorized system actions before opening/installing any driver."""
    apps = {profile["app_id"], *profile.get("allowed_apps", [])}
    for step in spec["steps"]:
        op = step["op"]
        if op == "hid" and (profile["platform"] != "ios" or profile.get("ios_input_backend") not in {"usb_hid", "android_bluetooth_hid"}):
            raise ValueError("HID input requires an explicit iOS HID profile backend")
        if profile.get("ios_input_backend") in {"usb_hid", "android_bluetooth_hid"} and (
            op in {"tap", "swipe", "pinch", "gesture", "text", "key", "ime", "fragment", "visual_click"}
            or op == "web" and step["web_step"]["op"] in {"click", "fill", "press"}
        ):
            raise ValueError("External HID profile accepts explicit hid keyboard/mouse input steps")
        if profile.get("ios_input_backend") == "android_bluetooth_hid" and op == "hid" and step["hid_command"]["op"] == "touch":
            raise ValueError("Android Bluetooth HID does not advertise a touch digitizer")
        if op == "switch_app" and step["app_id"] not in apps:
            raise ValueError("App transition exceeds the explicit profile allowlist")
        if op == "permission" and (
            profile["platform"] != "android"
            or step["permission"] not in profile.get("allowed_permissions", [])
        ):
            raise ValueError(
                "Permission change is not authorized by this Android profile"
            )
        if op.startswith("clipboard_") and not profile.get("allow_clipboard"):
            raise ValueError("Clipboard access requires explicit profile authorization")
        if op == "web" and (
            not profile.get("allow_webview")
            or not spec.get("webview")
        ):
            raise ValueError(
                "Debuggable WebView requires explicit profile and context authorization"
            )
        if op == "web" and profile["platform"] == "ios" and not {"inspector_url", "native_target"} <= spec["webview"].keys():
            raise ValueError("iOS WKWebView requires Inspector registry and native container target")


class WebViewBrowser(BrowserSession):
    def __new__(cls, *args, **kwargs):
        return object.__new__(cls)

    def __init__(self, device, root, project, spec, control, max_queries):
        if device.profile["platform"] != "android" or not device.profile.get(
            "allow_webview"
        ):
            raise ValueError(
                "Android debuggable WebView requires explicit profile authorization"
            )
        self.control, self.spec, self.scope = (
            control,
            spec,
            NetworkScope(root, project, spec["allowed_origins"]),
        )
        app = device.profile["app_id"]
        pid = device.adb("shell", "pidof", app).strip().split()
        if len(pid) != 1 or not pid[0].isdigit():
            raise ValueError("WebView must bind one current App PID")
        name = "webview_devtools_remote_" + pid[0]
        deadline = time.monotonic() + min(5, control.remaining() or 5)
        while True:
            control.check()
            left = deadline - time.monotonic()
            if left <= 0:
                raise ValueError("No debuggable WebView socket belongs to the selected App PID")
            if device.adb("shell", "pidof", app, timeout=min(1, left)).strip().split() != pid:
                raise ValueError("WebView App process changed while waiting for readiness")
            sockets = device.adb("shell", "cat", "/proc/net/unix", timeout=min(1, max(0.001, deadline-time.monotonic())))
            names = set(re.findall(r"@((?:webview_devtools_remote|chrome_devtools_remote)_[0-9]+)\b", sockets))
            if name in names:
                break
            left = deadline - time.monotonic()
            if left <= 0:
                raise ValueError("No debuggable WebView socket belongs to the selected App PID")
            control.wait(min(0.05, left))
        self.device, self.forward, self.cdp = device, None, None
        try:
            port = device.adb("forward", "tcp:0", "localabstract:" + name).strip()
            if not port.isdigit():
                raise ValueError("Invalid WebView forwarding port")
            self.forward = "tcp:" + port
            with httpx.Client(trust_env=False, timeout=2) as client:
                pages = client.get(f"http://127.0.0.1:{port}/json/list").json()
            selected = [
                p
                for p in pages
                if p.get("type") == "page"
                and (not spec.get("page_id") or p.get("id") == spec["page_id"])
            ]
            if len(selected) != 1:
                raise ValueError("Select a unique WebView page_id")
            page = selected[0]
            self.scope.check_browser(page["url"])
            from urllib.parse import urlsplit, urlunsplit

            url = urlsplit(page["webSocketDebuggerUrl"])
            if url.hostname not in {"127.0.0.1", "localhost"} or url.port != int(port):
                raise ValueError("WebView endpoint escaped its owned ADB forward")
            self.cdp = CDP(urlunsplit(url), control)
            self.session = self.page_session = ""
            self.world = None
            self.pages = {"main": {"session": "", "target": page["id"]}}
            self.page_alias = "main"
            self.context_worlds, self.frame_sessions, self.frame_chain = {}, {}, []
            self.browser_context = None
            self.queries = 0
            self.max_queries = max_queries
            self.pending = False
            self.action_charge = lambda n: None
            self.call("Page.enable")
        except BaseException:
            self.close()
            raise

    def close(self):
        if getattr(self, "cdp", None):
            self.cdp.close()
        if getattr(self, "forward", None):
            self.device.adb("forward", "--remove", self.forward)
            self.forward = None

    def page_action(self, step):
        raise ValueError(
            "App WebViews do not create browser tabs; select an explicit App page"
        )


class MobileSystem:
    def __init__(self, session, root, project, webview=None):
        self.session, self.root, self.project, self.webview = (
            session,
            root,
            project,
            webview,
        )
        self.allowed = {
            session.profile["app_id"],
            *session.profile.get("allowed_apps", []),
        }
        self.original = dict(session.profile)
        self.changes = []
        self.browser = None
        self.hid = None

    def execute(self, step, max_queries=100, *, max_actions=100):
        device = self.session.device
        op = step["op"]
        if op == "hid":
            from iatest.mobile.usb_hid import USBHIDSession

            if self.hid is None:
                if self.original.get("ios_input_backend") == "android_bluetooth_hid":
                    from iatest.mobile.bluetooth_hid import (
                        AndroidBluetoothHIDSession,
                    )

                    self.hid = AndroidBluetoothHIDSession(self.original["bluetooth_hid"], self.session.device.control)
                else:
                    self.hid = USBHIDSession(self.original["usb_hid"], self.session.device.control)
            try:
                if step["hid_command"]["op"] in {"target_click", "ime_text"}:
                    from iatest.mobile.usb_hid_input import observed_input

                    value = observed_input(self.session, self.hid, step, max_queries, max_actions)
                else:
                    value = self.hid.execute(step["hid_command"])
            except DeliveryUnknown:
                if getattr(device, "on_unknown", None):
                    device.on_unknown()
                raise
            self.session.last_action_started_ms = value["started_timestamp_ms"]
            return value
        if op == "input_state_expect":
            from iatest.mobile.bridge import read_bridge

            if max_queries <= 0:
                raise ValueError("Input-state native query budget exhausted")

            floor = getattr(self.session, "last_action_started_ms", None)
            if floor is None:
                raise ValueError(
                    "Input state proof needs a preceding timestamped native action"
                )
            observed = self.session.query(step["target"], cache=False)
            node = self.session.unique(observed)
            if node.get("secure"):
                raise ValueError("Secure input state is not exposed")
            snapshot = read_bridge(
                self.root,
                self.project,
                self.original["id"],
                step["bridge_artifact_id"],
                session=self.session,
            )
            key = step["input_identifier"]
            if snapshot["timestamp_ms"] < floor or snapshot["sequence"] <= getattr(
                self, "input_sequence", 0
            ):
                raise ProtocolError(
                    "Input evidence predates the native action or was already consumed"
                )
            if snapshot["identifiers"].get(key) != step["target"].get("identifier"):
                raise ValueError(
                    "Input evidence identifier differs from the native target"
                )
            state = snapshot.get("input_state", {}).get(key)
            if not state or not state["first_responder"]:
                raise ProtocolError("No fresh first-responder input evidence")
            self.input_sequence = snapshot["sequence"]
            if any(
                state[k] != step[k] for k in ("marked", "marked_length") if k in step
            ):
                raise AssertionError("Native App input state does not match")
            return {
                "input_state": state,
                "queries": 1,
                "postcondition_verified": True,
                "evidence_source": "verified development App; deterministic native UI target rechecked",
            }
        if op == "switch_app":
            app = step["app_id"]
            if app not in self.allowed:
                raise ValueError(
                    "App transition exceeds the explicit profile allowlist"
                )
            if self.browser:
                self.browser.close()
                self.browser = None
            if self.session.media_receiver:
                self.session.media_receiver.close()
                self.session.media_receiver = None
            self.restore_permissions()
            control, on_unknown = device.control, device.on_unknown
            device.close()
            profile = {**self.original, "app_id": app}
            new = self.session.device_factory(profile, self.session.id)
            new.control = control
            new.on_unknown = on_unknown
            self.session.device = new
            self.session.profile = profile
            try:
                new.open()
                if profile["platform"] == "android":
                    self.session.action(
                        {"op": "launch"}, None,
                        "switch-launch-" + str(time.monotonic_ns()),
                        min(10, control.remaining() or 10) if control else 10,
                    )
                else:
                    new.semantic.request(
                        "launch",
                        {},
                        operation_id="launch-" + str(time.monotonic_ns()),
                        timeout=5,
                    )
            except BaseException:
                new.close()
                raise
            self.session.last_observation = None
            return {
                "app_id": app,
                "driver_rebound": True,
                "postcondition_verified": False,
            }
        if op == "permission":
            permission = step["permission"]
            if device.profile[
                "platform"
            ] != "android" or permission not in self.original.get(
                "allowed_permissions", []
            ):
                raise ValueError(
                    "Permission change is not authorized by this Android profile"
                )
            state = device.android_permission_state(device.profile["app_id"], permission)
            wanted = step["granted"]
            if state != wanted:
                device.adb(
                    "shell",
                    "pm",
                    "grant" if wanted else "revoke",
                    device.profile["app_id"],
                    permission,
                )
                self.changes.append(
                    (device.profile["app_id"], permission, state, wanted)
                )
            actual = device.android_permission_state(device.profile["app_id"], permission)
            if actual != wanted:
                raise ProtocolError("Permission state could not be verified")
            return {
                "permission": permission,
                "granted": actual,
                "postcondition_verified": True,
            }
        if op.startswith("clipboard_"):
            if not self.original.get("allow_clipboard"):
                raise ValueError(
                    "Clipboard access requires explicit profile authorization"
                )
            value = step.get("value", step.get("expected", ""))
            expected = hashlib.sha256(value.encode()).hexdigest()
            response = device.input.request(
                op,
                {"value": value}
                if op == "clipboard_set"
                else {"expected_sha256": expected},
                operation_id="clipboard-" + str(time.monotonic_ns()),
                timeout=2,
            )
            if response.get("status") == "unknown":
                raise DeliveryUnknown(response.get("error", "Clipboard outcome unknown; do not replay"))
            if op == "clipboard_expect" and response.get("matched") is not True:
                raise AssertionError("Clipboard did not match the declared value")
            if op == "clipboard_set" and (
                response.get("status") != "injection_call_returned"
                or response.get("accepted") is not True
            ):
                raise ProtocolError("Clipboard update did not confirm dispatch")
            return response
        if op == "web":
            device.semantic.request("state", {}, timeout=1)
            if not self.webview:
                raise ValueError("Declare the WebView context and allowed_origins")
            if self.browser is None:
                if device.profile["platform"] == "ios":
                    from iatest.mobile.webkit import WKWebViewBrowser

                    browser_type = WKWebViewBrowser
                else:
                    browser_type = WebViewBrowser
                self.browser = browser_type(
                    device,
                    self.root,
                    self.project,
                    self.webview,
                    device.control,
                    max_queries,
                )
            browser = self.browser
            browser.max_queries = min(
                browser.max_queries, browser.queries + max_queries
            )
            before = browser.queries
            s = step["web_step"]
            if s["op"] in {"switch_page", "expect_popup"}:
                result = browser.page_action(s)
            elif s["op"] in {"expect", "wait"}:
                deadline = time.monotonic() + min(
                    s.get("timeout_seconds", 5), device.control.remaining() or 5
                )
                while True:
                    passed, result = browser.assertion(s)
                    if passed or s["op"] != "wait" or time.monotonic() >= deadline:
                        break
                    device.control.wait(min(0.1, max(0, deadline - time.monotonic())))
                if not passed:
                    raise AssertionError("WebView postcondition did not match")
            elif s["op"] == "find":
                result = browser.query(s["target"])
            elif s["op"] in {"navigate", "reload"}:
                result = browser.navigate(
                    s.get("url") or browser.evaluate("location.href")
                )
            else:
                result = browser.act(s)
            return {
                "webview": True,
                "queries": browser.queries - before,
                "result": result,
                "postcondition_verified": s["op"] in {"expect", "wait"},
            }
        raise ValueError("Unknown mobile system operation")

    def restore_permissions(self):
        device = self.session.device
        for app, permission, old, ours in reversed(self.changes):
            state = device.android_permission_state(app, permission)
            if state != ours:
                raise ProtocolError(
                    "Permission changed externally; cleanup must be reconciled"
                )
            device.adb("shell", "pm", "grant" if old else "revoke", app, permission)
            if (
                device.android_permission_state(app, permission)
                != old
            ):
                raise ProtocolError("Permission restore could not be confirmed")
        self.changes = []

    def _close(self):
        try:
            if self.hid:
                self.hid.close()
        finally:
            self._close_platform()

    def _close_platform(self):
        try:
            if self.browser:
                self.browser.close()
        finally:
            try:
                self.restore_permissions()
            finally:
                if self.original.get("allow_clipboard"):
                    value = self.session.device.input.request(
                        "clipboard_restore",
                        {},
                        operation_id="cleanup-" + str(time.monotonic_ns()),
                        timeout=2,
                    )
                    if not value.get("restored"):
                        raise ProtocolError("Clipboard restore could not be confirmed")

    def close(self):
        # Restore only our temporary state even when the normal run was cancelled.
        # A separate, finite cleanup deadline must not inherit normal cancellation.
        from iatest.execution.control import RunControl

        control = RunControl(timeout_seconds=10)
        objects = [
            self.session.device,
            self.session.device.input,
            self.session.device.semantic,
        ]
        if self.browser:
            objects.append(self.browser)
            if getattr(self.browser, "cdp", None):
                objects.append(self.browser.cdp)
        previous = [(obj, getattr(obj, "control", None)) for obj in objects]
        try:
            for obj, _ in previous:
                obj.control = control
            self._close()
        finally:
            for obj, value in previous:
                obj.control = value
