"""Consent-bound XDG RemoteDesktop session through system GIO.

No X11 fallback, persistent permission token, private compositor API or input
replay. Full GNOME mode uses separately authorized native window identity.
"""

from __future__ import annotations

import ctypes as C
import ctypes.util
import json
import time
import uuid
from pathlib import Path


class GError(C.Structure):
    _fields_ = [("domain", C.c_uint), ("code", C.c_int), ("message", C.c_char_p)]


class Portal:
    def __init__(self):
        self.gio = C.CDLL(ctypes.util.find_library("gio-2.0") or "libgio-2.0.so.0")
        self.glib = C.CDLL(ctypes.util.find_library("glib-2.0") or "libglib-2.0.so.0")
        self.objects = C.CDLL(
            ctypes.util.find_library("gobject-2.0") or "libgobject-2.0.so.0"
        )
        p, s, i, u = C.c_void_p, C.c_char_p, C.c_int, C.c_uint
        self.callback_type = C.CFUNCTYPE(None, p, s, s, s, s, p, p)
        signatures = {
            "g_bus_get_sync": (p, [i, p, C.POINTER(p)]),
            "g_dbus_connection_call_sync": (
                p,
                [p, s, s, s, s, p, p, i, i, p, C.POINTER(p)],
            ),
            "g_dbus_connection_signal_subscribe": (
                u,
                [p, s, s, s, s, s, i, self.callback_type, p, p],
            ),
            "g_dbus_connection_signal_unsubscribe": (None, [p, u]),
        }
        for name, (result, args) in signatures.items():
            fn = getattr(self.gio, name)
            fn.restype, fn.argtypes = result, args
        signatures = {
            "g_variant_parse": (p, [p, s, p, p, C.POINTER(p)]),
            "g_variant_type_new": (p, [s]),
            "g_variant_type_free": (None, [p]),
            "g_variant_unref": (None, [p]),
            "g_variant_classify": (C.c_char, [p]),
            "g_variant_get_type_string": (s, [p]),
            "g_variant_get_fixed_array": (p, [p, C.POINTER(C.c_size_t), C.c_size_t]),
            "g_variant_n_children": (C.c_size_t, [p]),
            "g_variant_get_child_value": (p, [p, C.c_size_t]),
            "g_variant_get_variant": (p, [p]),
            "g_variant_get_string": (s, [p, p]),
            "g_variant_get_uint32": (u, [p]),
            "g_variant_get_int32": (i, [p]),
            "g_variant_get_boolean": (i, [p]),
            "g_variant_get_double": (C.c_double, [p]),
            "g_error_free": (None, [p]),
            "g_main_context_iteration": (i, [p, i]),
        }
        for name, (result, args) in signatures.items():
            fn = getattr(self.glib, name)
            fn.restype, fn.argtypes = result, args
        self.objects.g_object_unref.argtypes = [p]
        problem = p()
        self.connection = self.gio.g_bus_get_sync(2, None, C.byref(problem))
        self.error(problem, "Wayland session bus unavailable")
        if not self.connection:
            raise ValueError("Wayland session bus unavailable")
        self.responses = {}
        self.session = None
        self.closed = False
        self.helper_owner = self.helper_token = None
        self.streams = []
        self.backend = "keyboard"
        self.pressed = set()
        self.callback = self.callback_type(self.signal)
        self.subscription = self.gio.g_dbus_connection_signal_subscribe(
            self.connection,
            b"org.freedesktop.portal.Desktop",
            None,
            None,
            None,
            None,
            0,
            self.callback,
            None,
            None,
        )

    def error(self, value, message):
        if value.value:
            detail = C.cast(value, C.POINTER(GError)).contents.message
            detail = detail.decode("utf-8", "replace")[:500] if detail else ""
            for secret in (
                getattr(self, "helper_token", None),
                getattr(self, "session", None),
            ):
                if secret:
                    detail = detail.replace(secret, "[session]")
            self.glib.g_error_free(value)
            raise ValueError(message + (": " + detail if detail else ""))

    def decode(self, value):
        kind = self.glib.g_variant_classify(value)
        if kind in (b"s", b"o", b"g"):
            return self.glib.g_variant_get_string(value, None).decode()
        if kind == b"u":
            return self.glib.g_variant_get_uint32(value)
        if kind == b"i":
            return self.glib.g_variant_get_int32(value)
        if kind == b"b":
            return bool(self.glib.g_variant_get_boolean(value))
        if kind == b"d":
            return self.glib.g_variant_get_double(value)
        if kind == b"v":
            child = self.glib.g_variant_get_variant(value)
            try:
                return self.decode(child)
            finally:
                self.glib.g_variant_unref(child)
        if kind == b"a" and self.glib.g_variant_get_type_string(value) == b"ay":
            count = C.c_size_t()
            data = self.glib.g_variant_get_fixed_array(value, C.byref(count), 1)
            if count.value > 8388608:
                raise ValueError("Native window PNG exceeds byte budget")
            return C.string_at(data, count.value)
        result = []
        count = self.glib.g_variant_n_children(value)
        if count > 128:
            raise ValueError("Portal reply exceeds budget")
        for n in range(count):
            child = self.glib.g_variant_get_child_value(value, n)
            try:
                result.append(self.decode(child))
            finally:
                self.glib.g_variant_unref(child)
        return (
            dict(result)
            if kind == b"a"
            and result
            and isinstance(result[0], list)
            and len(result[0]) == 2
            and isinstance(result[0][0], str)
            else result
        )

    def signal(self, connection, sender, path, interface, name, parameters, data):
        try:
            path = path.decode()
            if name == b"Response" and len(self.responses) < 16:
                self.responses[path] = self.decode(parameters)
            elif name == b"Closed" and path == self.session:
                self.closed = True
        except (ValueError, TypeError, UnicodeError):
            self.closed = True

    def call(
        self,
        interface,
        method,
        signature,
        arguments,
        *,
        path="/org/freedesktop/portal/desktop",
        destination="org.freedesktop.portal.Desktop",
        timeout_ms=3000,
    ):
        if self.closed:
            raise ValueError("Wayland portal session was revoked; never replay input")
        problem = C.c_void_p()
        kind = self.glib.g_variant_type_new(signature.encode())
        try:
            params = self.glib.g_variant_parse(
                kind, arguments.encode(), None, None, C.byref(problem)
            )
        finally:
            self.glib.g_variant_type_free(kind)
        self.error(problem, "Invalid portal arguments")
        try:
            value = self.gio.g_dbus_connection_call_sync(
                self.connection,
                destination.encode(),
                path.encode(),
                interface.encode(),
                method.encode(),
                params,
                None,
                0,
                timeout_ms,
                None,
                C.byref(problem),
            )
        finally:
            self.glib.g_variant_unref(params)
        self.error(problem, "Wayland portal request failed; no X11 fallback")
        if not value:
            raise ValueError("Missing portal reply")
        try:
            return self.decode(value)
        finally:
            self.glib.g_variant_unref(value)

    def request(
        self,
        method,
        signature,
        arguments,
        *,
        interface="org.freedesktop.portal.RemoteDesktop",
    ):
        handle = self.call(interface, method, signature, arguments)[0]
        until = time.monotonic() + (90 if self.backend == "gnome" else 50)
        while handle not in self.responses:
            self.pump()
            if time.monotonic() > until:
                self.call(
                    "org.freedesktop.portal.Request", "Close", "()", "()", path=handle
                )
                raise ValueError("Wayland portal " + method + " approval timed out")
            time.sleep(0.01)
        status, result = self.responses.pop(handle)
        if status != 0:
            reason = "permission was declined" if status == 1 else "backend failed"
            raise ValueError("Wayland portal " + method + " " + reason)
        return result

    def start(self, backend="keyboard", pid=None):
        if backend not in {"keyboard", "gnome"}:
            raise ValueError("Unsupported Wayland backend")
        self.backend = backend
        if backend == "gnome":
            owners = [
                self.call(
                    "org.freedesktop.DBus",
                    "GetNameOwner",
                    "(s)",
                    "(" + json.dumps(name) + ",)",
                    destination="org.freedesktop.DBus",
                    path="/org/freedesktop/DBus",
                )[0]
                for name in ("dev.iatest.Wayland", "org.gnome.Shell")
            ]
            if owners[0] != owners[1]:
                raise ValueError("IATest geometry service must belong to GNOME Shell")
            shell_pid = self.call(
                "org.freedesktop.DBus",
                "GetConnectionUnixProcessID",
                "(s)",
                "(" + json.dumps(owners[0]) + ",)",
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
            )[0]
            if (
                Path(f"/proc/{shell_pid}/exe").resolve(strict=True).name
                != "gnome-shell"
            ):
                raise ValueError("GNOME Shell service identity differs")
            self.helper_owner = owners[0]
        token = "iatest_" + uuid.uuid4().hex
        result = self.request(
            "CreateSession",
            "(a{sv})",
            "({'handle_token': <'"
            + token
            + "'>, 'session_handle_token': <'"
            + token
            + "'>},)",
        )
        self.session = result["session_handle"]
        session = json.dumps(self.session)
        self.request(
            "SelectDevices",
            "(oa{sv})",
            "("
            + session
            + ", {'types': <uint32 "
            + ("3" if backend == "gnome" else "1")
            + ">, 'handle_token': <'"
            + token
            + "_devices'>})",
        )
        if backend == "gnome":
            self.request(
                "SelectSources",
                "(oa{sv})",
                "("
                + session
                + ", {'types': <uint32 1>, 'multiple': <false>, 'cursor_mode': <uint32 1>, 'handle_token': <'"
                + token
                + "_screen'>})",
                interface="org.freedesktop.portal.ScreenCast",
            )
        result = self.request(
            "Start",
            "(osa{sv})",
            "(" + session + ", '', {'handle_token': <'" + token + "_start'>})",
        )
        required = 3 if backend == "gnome" else 1
        if result.get("devices", 0) & required != required:
            raise ValueError("Portal did not grant all requested input devices")
        if backend == "gnome":
            self.streams = result.get("streams", [])
            if len(self.streams) != 1 or self.streams[0][1].get("source_type") != 1:
                raise ValueError(
                    "Full Wayland mode requires one authorized monitor stream"
                )
            self.helper_token = self.helper(
                "Bind", "(u)", "(" + str(pid) + ",)", timeout_ms=50000
            )[0]
        return {
            "backend": "Wayland RemoteDesktop portal",
            "keyboard": True,
            "pointer": backend == "gnome",
            "capture": backend == "gnome",
            "persistent_permissions": False,
        }

    def helper(self, method, signature, arguments, **kwargs):
        if not self.helper_owner:
            raise ValueError(
                "Install and explicitly enable IATest's GNOME capture adapter"
            )
        self.pump()
        return self.call(
            "dev.iatest.Wayland",
            method,
            signature,
            arguments,
            destination=self.helper_owner,
            path="/dev/iatest/Wayland",
            **kwargs,
        )

    def inspect(self, title):
        return json.loads(
            self.helper(
                "Inspect",
                "(ss)",
                "(" + json.dumps(self.helper_token) + ", " + json.dumps(title) + ")",
            )[0]
        )

    def check_point(self, window, x, y, *, pointer=False):
        return self.helper(
            "Check",
            "(ssddb)",
            "("
            + json.dumps(self.helper_token)
            + ", "
            + json.dumps(window)
            + ", "
            + str(float(x))
            + ", "
            + str(float(y))
            + ", "
            + str(pointer).lower()
            + ")",
        )[0]

    def motion(self, stream, x, y):
        self.pump()
        self.call(
            "org.freedesktop.portal.RemoteDesktop",
            "NotifyPointerMotionAbsolute",
            "(oa{sv}udd)",
            "("
            + json.dumps(self.session)
            + ", {}, "
            + str(stream)
            + ", "
            + str(float(x))
            + ", "
            + str(float(y))
            + ")",
        )

    def button(self, button, down):
        if down:
            self.pressed.add(button)
        self.call(
            "org.freedesktop.portal.RemoteDesktop",
            "NotifyPointerButton",
            "(oa{sv}iu)",
            "("
            + json.dumps(self.session)
            + ", {}, "
            + str(button)
            + ", "
            + str(int(down))
            + ")",
        )
        if not down:
            self.pressed.discard(button)

    def scroll(self, x, y):
        for axis, value in ((0, y), (1, x)):
            if value:
                self.call(
                    "org.freedesktop.portal.RemoteDesktop",
                    "NotifyPointerAxisDiscrete",
                    "(oa{sv}ui)",
                    "("
                    + json.dumps(self.session)
                    + ", {}, "
                    + str(axis)
                    + ", "
                    + str(value)
                    + ")",
                )

    def capture(self, window):
        metadata, data = self.helper(
            "Capture",
            "(ss)",
            "(" + json.dumps(self.helper_token) + ", " + json.dumps(window) + ")",
        )
        return json.loads(metadata), data

    def pump(self):
        for _ in range(32):
            if not self.glib.g_main_context_iteration(None, False):
                break
        if self.closed:
            raise ValueError("Wayland portal session was revoked; never replay input")

    def key(self, keysym):
        self.pump()
        for state in (1, 0):
            self.call(
                "org.freedesktop.portal.RemoteDesktop",
                "NotifyKeyboardKeysym",
                "(oa{sv}iu)",
                "("
                + json.dumps(self.session)
                + ", {}, "
                + str(keysym)
                + ", "
                + str(state)
                + ")",
            )

    def close(self):
        if not self.connection:
            return
        for button in list(self.pressed):
            try:
                self.button(button, False)
            except ValueError:
                pass
        if self.helper_token and not self.closed:
            try:
                self.helper(
                    "Release", "(s)", "(" + json.dumps(self.helper_token) + ",)"
                )
            except ValueError:
                pass
        if self.session and not self.closed:
            try:
                self.call(
                    "org.freedesktop.portal.Session",
                    "Close",
                    "()",
                    "()",
                    path=self.session,
                )
            except ValueError:
                pass
        self.gio.g_dbus_connection_signal_unsubscribe(
            self.connection, self.subscription
        )
        self.objects.g_object_unref(self.connection)
        self.connection = None
        self.closed = True
