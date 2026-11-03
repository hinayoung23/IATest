"""AT-SPI driver for an explicitly selected process in an X11 desktop.

Only the standard library and the desktop's libatspi/GObject are used. Requests
and replies are bounded newline JSON, matching the AX and UIAutomation drivers.
"""

from __future__ import annotations

import base64
import ctypes as C
import ctypes.util
import json
import os
import struct
import sys
import time
import zlib
from pathlib import Path


def need(test, message):
    if not test:
        raise ValueError(message)


def process_identity(pid, app_id):
    root = Path("/proc") / str(pid)
    executable = (root / "exe").resolve(strict=True)
    need(app_id in {executable.name, str(executable)}, "App identity mismatch")
    # comm may contain spaces and parentheses. starttime is field 22.
    tail = (root / "stat").read_text().rsplit(")", 1)[1].split()
    need(tail[0] != "Z", "Selected process exited")
    return pid, str(executable), tail[19]


class Atspi:
    def __init__(self):
        self.lib = C.CDLL(ctypes.util.find_library("atspi") or "libatspi.so.0")
        self.glib = C.CDLL(ctypes.util.find_library("glib-2.0") or "libglib-2.0.so.0")
        self.gobject = C.CDLL(
            ctypes.util.find_library("gobject-2.0") or "libgobject-2.0.so.0"
        )
        self.functions = {}
        p, i, e = C.c_void_p, C.c_int, C.POINTER(C.c_void_p)
        signatures = {
            "init": (i, []),
            "exit": (i, []),
            "set_timeout": (None, [i, i]),
            "get_desktop": (p, [i]),
            "accessible_get_child_count": (i, [p, e]),
            "accessible_get_child_at_index": (p, [p, i, e]),
            "accessible_get_process_id": (C.c_uint, [p, e]),
            "accessible_get_parent": (p, [p, e]),
            "accessible_get_name": (p, [p, e]),
            "accessible_get_role_name": (p, [p, e]),
            "accessible_get_accessible_id": (p, [p, e]),
            "accessible_get_state_set": (p, [p]),
            "accessible_clear_cache": (None, [p]),
            "state_set_contains": (i, [p, i]),
            "text_get_character_count": (i, [p, e]),
            "text_get_text": (p, [p, i, i, e]),
            "editable_text_set_text_contents": (i, [p, C.c_char_p, e]),
            "action_get_n_actions": (i, [p, e]),
            "action_get_name": (p, [p, i, e]),
            "action_do_action": (i, [p, i, e]),
            "component_grab_focus": (i, [p, e]),
            "component_get_extents": (p, [p, i, e]),
            "component_get_accessible_at_point": (p, [p, i, i, i, e]),
            "generate_mouse_event": (i, [C.c_long, C.c_long, C.c_char_p, e]),
            "generate_keyboard_event": (i, [C.c_long, C.c_char_p, i, e]),
        }
        for interface in ("text", "editable_text", "action", "component"):
            signatures["accessible_get_" + interface + "_iface"] = (p, [p])
        for name, (restype, args) in signatures.items():
            function = getattr(self.lib, "atspi_" + name)
            function.restype, function.argtypes = restype, args
            self.functions[name] = function
        for name in ("g_free", "g_error_free"):
            f = getattr(self.glib, name)
            f.argtypes, f.restype = [p], None
        self.gobject.g_object_unref.argtypes = [p]
        self.gobject.g_object_unref.restype = None
        need(self.call("init") == 0, "AT-SPI accessibility bus is unavailable")
        self.call("set_timeout", 250, 250)
        self.refs = []

    def call(self, name, *args, error=False):
        if not error:
            return self.functions[name](*args)
        problem = C.c_void_p()
        result = self.functions[name](*args, C.byref(problem))
        if problem.value:
            self.glib.g_error_free(problem)
            raise ValueError("AT-SPI request failed; observation is incomplete")
        return result

    def own(self, value):
        if value:
            self.refs.append(value)
        return value

    def string(self, name, *args):
        value = self.call(name, *args, error=True)
        if not value:
            return ""
        try:
            return C.string_at(value).decode("utf-8", "strict")
        finally:
            self.glib.g_free(value)

    def state(self, node, number):
        states = self.own(self.call("accessible_get_state_set", node))
        need(states, "AT-SPI states unavailable")
        return bool(self.call("state_set_contains", states, number))

    def fields(self, node):
        return {
            k: self.string("accessible_get_" + n, node)
            for k, n in (
                ("identifier", "accessible_id"),
                ("name", "name"),
                ("role", "role_name"),
            )
        }

    def row(self, node, fields):
        # Password contents never cross the process boundary, including names.
        secure = fields["role"] == "password text"
        text, truncated = None, []
        if secure:
            fields = {**fields, "name": "[REDACTED]"}
        else:
            interface = self.own(self.call("accessible_get_text_iface", node))
            if interface:
                count = self.call("text_get_character_count", interface, error=True)
                text = self.string(
                    "text_get_text", interface, 0, min(2000, max(0, count))
                )
                if count > 2000:
                    truncated.extend(["value", "text"])
        if len(fields["name"]) > 500:
            truncated.append("name")
        return {
            **fields,
            "name": fields["name"][:500],
            "value": text,
            "text": text if text is not None else fields["name"][:500],
            "secure": secure,
            "enabled": self.state(node, 8) and self.state(node, 24),
            "truncated_attributes": truncated,
        }

    def query(self, pid, target):
        desktop = self.own(self.call("get_desktop", 0))
        need(desktop, "AT-SPI desktop is unavailable")
        roots = []
        applications = self.call("accessible_get_child_count", desktop, error=True)
        need(0 <= applications <= 256, "AT-SPI application budget exceeded")
        for index in range(applications):
            app = self.own(
                self.call("accessible_get_child_at_index", desktop, index, error=True)
            )
            if app and self.call("accessible_get_process_id", app, error=True) == pid:
                roots.append(app)
        need(
            len(roots) == 1,
            "Selected process must expose exactly one AT-SPI application",
        )
        self.call("accessible_clear_cache", roots[0])
        pending, found, visited, complete = [(roots[0], 0)], [], 0, True
        deadline = time.monotonic() + 0.25
        while pending:
            node, depth = pending.pop()
            if visited >= 1024 or time.monotonic() >= deadline:
                complete = False
                break
            visited += 1
            need(
                self.call("accessible_get_process_id", node, error=True) == pid,
                "AT-SPI node belongs to another process",
            )
            fields = self.fields(node)
            if all(fields[k] == value for k, value in target.items()):
                found.append((node, fields))
                if len(found) >= 4:
                    complete = False
                    break
            count = self.call("accessible_get_child_count", node, error=True)
            need(count >= 0, "AT-SPI child count is unavailable")
            if count and depth >= 32:
                complete = False
                continue
            if count > 1024 - visited - len(pending):
                complete = False
                break
            for index in reversed(range(count)):
                child = self.own(
                    self.call("accessible_get_child_at_index", node, index, error=True)
                )
                need(child, "AT-SPI child disappeared")
                pending.append((child, depth + 1))
        return found, complete and time.monotonic() < deadline, visited

    def action(self, node, op, packet):
        if getattr(self, "portal", None):
            # Revocation also gates AT-SPI semantic actions, which otherwise
            # would bypass the portal's keyboard/pointer checks.
            self.portal.pump()
            if getattr(self, "gnome", None) and op in ("click", "fill", "press"):
                self.gnome.region(node)
        need(self.state(node, 8) and self.state(node, 24), "Target is disabled")
        if op in ("right_click", "drag", "scroll", "pointer_click", "screenshot"):
            if getattr(self, "gnome", None):
                return self.gnome.action(node, op, packet)
            need(
                not getattr(self, "portal", None),
                "Wayland window coordinates are not globally observable; pointer and capture require a supported window-stream backend",
            )
            region = self.rect(node)
            if op == "screenshot":
                return screenshot(packet["process_id"], region)
            self.active(node)
            fx, fy = packet.get("fraction_x", 0.5), packet.get("fraction_y", 0.5)
            need(0 <= fx < 1 and 0 <= fy < 1, "Invalid pointer fractions")
            x, y = round(region[0] + region[2] * fx), round(region[1] + region[3] * fy)
            self.pointer(x, y, b"abs")
            pointer_owner(packet["process_id"])
            self.hit(node, x, y)
            if op == "drag":
                destination = packet.get("destination")
                need(
                    isinstance(destination, dict)
                    and destination
                    and all(
                        k in ("identifier", "name", "role") and isinstance(v, str)
                        for k, v in destination.items()
                    ),
                    "Semantic drag destination required",
                )
                found, complete, _ = self.query(packet["process_id"], destination)
                need(complete and len(found) == 1, "Drag destination must be unique")
                d = self.rect(found[0][0])
                ex, ey = round(d[0] + d[2] / 2), round(d[1] + d[3] / 2)
                self.hit(found[0][0], ex, ey)
                self.pointer(x, y, b"b1p")
                try:
                    for n in range(1, 13):
                        self.active(node)
                        self.pointer(
                            round(x + (ex - x) * n / 12),
                            round(y + (ey - y) * n / 12),
                            b"abs",
                        )
                        time.sleep(0.015)
                finally:
                    self.pointer(-1, -1, b"b1r")
            elif op == "scroll":
                dx, dy = packet.get("delta_x", 0), packet.get("delta_y", 0)
                need(
                    type(dx) is int
                    and type(dy) is int
                    and abs(dx) <= 20
                    and abs(dy) <= 20
                    and (dx or dy),
                    "Invalid wheel steps",
                )
                for delta, negative, positive in (
                    (dy, b"b4c", b"b5c"),
                    (dx, b"b6c", b"b7c"),
                ):
                    for _ in range(abs(delta)):
                        self.pointer(x, y, negative if delta < 0 else positive)
            else:
                self.pointer(x, y, b"b3c" if op == "right_click" else b"b1c")
        elif op == "fill":
            value = packet.get("value")
            need(
                isinstance(value, str)
                and len(value.encode()) <= 16384
                and "\0" not in value,
                "Invalid text",
            )
            interface = self.own(self.call("accessible_get_editable_text_iface", node))
            need(interface, "Editable text interface unavailable")
            need(
                self.call(
                    "editable_text_set_text_contents",
                    interface,
                    value.encode(),
                    error=True,
                ),
                "Text was rejected",
            )
        elif op == "click":
            interface = self.own(self.call("accessible_get_action_iface", node))
            need(interface, "Action interface unavailable")
            count = self.call("action_get_n_actions", interface, error=True)
            need(0 <= count <= 32, "Action budget exceeded")
            actions = [
                i
                for i in range(count)
                if self.string("action_get_name", interface, i)
                in ("click", "press", "activate")
            ]
            need(len(actions) == 1, "One explicit primary action is required")
            need(
                self.call("action_do_action", interface, actions[0], error=True),
                "Action was rejected",
            )
        else:
            keys = {
                "Enter": 0xFF0D,
                "Tab": 0xFF09,
                "Escape": 0xFF1B,
                "Backspace": 0xFF08,
                "Delete": 0xFFFF,
                "Space": 0x20,
                "ArrowLeft": 0xFF51,
                "ArrowUp": 0xFF52,
                "ArrowRight": 0xFF53,
                "ArrowDown": 0xFF54,
                "Home": 0xFF50,
                "End": 0xFF57,
                "PageUp": 0xFF55,
                "PageDown": 0xFF56,
            }
            need(packet.get("key") in keys, "Unsupported key")
            self.call("accessible_clear_cache", node)
            if not self.state(node, 12):
                interface = self.own(self.call("accessible_get_component_iface", node))
                need(
                    interface
                    and self.call("component_grab_focus", interface, error=True),
                    "Focus was rejected",
                )
            self.call("accessible_clear_cache", node)
            need(self.state(node, 12), "Target does not own keyboard focus")
            parent, active = node, False
            for _ in range(32):
                if self.string("accessible_get_role_name", parent) in (
                    "frame",
                    "window",
                    "dialog",
                    "alert",
                ):
                    self.call("accessible_clear_cache", parent)
                    active = active or self.state(parent, 1)
                parent = self.own(
                    self.call("accessible_get_parent", parent, error=True)
                )
                if (
                    not parent
                    or self.string("accessible_get_role_name", parent) == "application"
                ):
                    break
            need(active, "Selected App does not own the active desktop window")
            if getattr(self, "portal", None):
                self.portal.key(keys[packet["key"]])
            else:
                need(
                    self.call(
                        "generate_keyboard_event",
                        keys[packet["key"]],
                        None,
                        3,
                        error=True,
                    ),
                    "Key was rejected",
                )
        return {
            "dispatch": "acknowledged",
            "backend": "AT-SPI",
            "business_postcondition": "pending",
        }

    def rect(self, node):
        interface = self.own(self.call("accessible_get_component_iface", node))
        need(
            interface and self.state(node, 25) and self.state(node, 30),
            "Visible target geometry required",
        )
        value = self.call("component_get_extents", interface, 0, error=True)
        need(value, "Target geometry unavailable")
        try:
            result = list(C.cast(value, C.POINTER(C.c_int * 4)).contents)
        finally:
            self.glib.g_free(value)
        need(result[2] > 0 and result[3] > 0, "Empty target geometry")
        return result

    def active(self, node):
        parent = node
        for _ in range(32):
            if self.string("accessible_get_role_name", parent) in (
                "frame",
                "window",
                "dialog",
                "alert",
            ):
                self.call("accessible_clear_cache", parent)
                need(
                    self.state(parent, 1), "Selected App does not own the active window"
                )
                return
            parent = self.own(self.call("accessible_get_parent", parent, error=True))
            need(parent, "Target has no active window")
        raise ValueError("Window ancestry budget exceeded")

    def pointer(self, x, y, event):
        need(
            self.call("generate_mouse_event", x, y, event, error=True),
            "Pointer event was rejected",
        )

    def hit(self, node, x, y, coordinates=0):
        window = node
        for _ in range(32):
            if self.string("accessible_get_role_name", window) in (
                "frame",
                "window",
                "dialog",
                "alert",
            ):
                break
            window = self.own(self.call("accessible_get_parent", window, error=True))
            need(window, "Target has no window")
        current = window
        for _ in range(32):
            interface = self.own(self.call("accessible_get_component_iface", current))
            if not interface:
                break
            child = self.own(
                self.call(
                    "component_get_accessible_at_point",
                    interface,
                    x,
                    y,
                    coordinates,
                    error=True,
                )
            )
            if not child or child == current:
                break
            current = child
        for _ in range(32):
            if current == node:
                return
            current = self.own(self.call("accessible_get_parent", current, error=True))
            if not current:
                break
        raise ValueError(
            "Pointer target is obscured or its accessibility identity changed"
        )

    def release(self):
        for ref in reversed(self.refs):
            self.gobject.g_object_unref(ref)
        self.refs.clear()


def pointer_owner(pid):
    """Verify the actual X11 window under the moved pointer before input."""
    lib = C.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
    p, u, i = C.c_void_p, C.c_ulong, C.c_int
    signatures = {
        "XOpenDisplay": (p, [C.c_char_p]),
        "XDefaultRootWindow": (u, [p]),
        "XCloseDisplay": (i, [p]),
        "XFree": (i, [p]),
        "XInternAtom": (u, [p, C.c_char_p, i]),
        "XQueryPointer": (
            i,
            [
                p,
                u,
                C.POINTER(u),
                C.POINTER(u),
                C.POINTER(i),
                C.POINTER(i),
                C.POINTER(i),
                C.POINTER(i),
                C.POINTER(C.c_uint),
            ],
        ),
        "XGetWindowProperty": (
            i,
            [
                p,
                u,
                u,
                C.c_long,
                C.c_long,
                i,
                u,
                C.POINTER(u),
                C.POINTER(i),
                C.POINTER(u),
                C.POINTER(u),
                C.POINTER(p),
            ],
        ),
    }
    for name, (result, args) in signatures.items():
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = result, args
    display = lib.XOpenDisplay(None)
    need(display, "X11 pointer display unavailable")
    try:
        current = lib.XDefaultRootWindow(display)
        atom = lib.XInternAtom(display, b"_NET_WM_PID", 1)
        owner = 0
        for _ in range(32):
            kind, count, remaining, fmt, data = u(), u(), u(), i(), p()
            status = lib.XGetWindowProperty(
                display,
                current,
                atom,
                0,
                1,
                0,
                6,
                C.byref(kind),
                C.byref(fmt),
                C.byref(count),
                C.byref(remaining),
                C.byref(data),
            )
            if status == 0 and data and count.value == 1 and fmt.value == 32:
                owner = C.cast(data, C.POINTER(u))[0]
            if data:
                lib.XFree(data)
            root, child = u(), u()
            rx, ry, wx, wy = i(), i(), i(), i()
            mask = C.c_uint()
            need(
                lib.XQueryPointer(
                    display,
                    current,
                    C.byref(root),
                    C.byref(child),
                    C.byref(rx),
                    C.byref(ry),
                    C.byref(wx),
                    C.byref(wy),
                    C.byref(mask),
                ),
                "Pointer left the selected X11 screen",
            )
            if not child.value:
                break
            current = child.value
        need(owner == pid, "Pointer target is obscured by another App")
    finally:
        lib.XCloseDisplay(display)


def screenshot(pid, region):
    """Read only a selected App's redirected X11 pixmap, never the root screen."""
    x, y, width, height = region
    need(
        0 < width <= 1024 and 0 < height <= 1024 and width * height <= 524288,
        "Screenshot pixel budget exceeded",
    )
    lib = C.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
    composite = C.CDLL(ctypes.util.find_library("Xcomposite") or "libXcomposite.so.1")
    p, u, i, l = C.c_void_p, C.c_ulong, C.c_int, C.c_long
    definitions = {
        "XOpenDisplay": (p, [C.c_char_p]),
        "XDefaultRootWindow": (u, [p]),
        "XCloseDisplay": (i, [p]),
        "XInternAtom": (u, [p, C.c_char_p, i]),
        "XGetWindowProperty": (
            i,
            [
                p,
                u,
                u,
                l,
                l,
                i,
                u,
                C.POINTER(u),
                C.POINTER(i),
                C.POINTER(u),
                C.POINTER(u),
                C.POINTER(p),
            ],
        ),
        "XQueryTree": (
            i,
            [
                p,
                u,
                C.POINTER(u),
                C.POINTER(u),
                C.POINTER(C.POINTER(u)),
                C.POINTER(C.c_uint),
            ],
        ),
        "XTranslateCoordinates": (
            i,
            [p, u, u, i, i, C.POINTER(i), C.POINTER(i), C.POINTER(u)],
        ),
        "XGetGeometry": (
            i,
            [
                p,
                u,
                C.POINTER(u),
                C.POINTER(i),
                C.POINTER(i),
                C.POINTER(C.c_uint),
                C.POINTER(C.c_uint),
                C.POINTER(C.c_uint),
                C.POINTER(C.c_uint),
            ],
        ),
        "XGetImage": (p, [p, u, i, i, C.c_uint, C.c_uint, u, i]),
        "XGetPixel": (u, [p, i, i]),
        "XDestroyImage": (i, [p]),
        "XFreePixmap": (i, [p, u]),
        "XFree": (i, [p]),
        "XSync": (i, [p, i]),
    }
    for name, (restype, args) in definitions.items():
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = restype, args
    for name, args, restype in (
        ("XCompositeRedirectWindow", [p, u, i], None),
        ("XCompositeUnredirectWindow", [p, u, i], None),
        ("XCompositeNameWindowPixmap", [p, u], u),
    ):
        fn = getattr(composite, name)
        fn.argtypes, fn.restype = args, restype
    display = lib.XOpenDisplay(None)
    need(display, "X11 display unavailable")
    window = pixmap = image = None
    try:
        root = lib.XDefaultRootWindow(display)
        atom = lib.XInternAtom(display, b"_NET_WM_PID", 1)
        pending = [(root, 0)]
        matches = []
        visited = 0
        while pending:
            candidate, depth = pending.pop()
            visited += 1
            need(visited <= 1024, "Window query budget exceeded")
            kind, n, remaining, fmt, data = u(), u(), u(), i(), p()
            status = lib.XGetWindowProperty(
                display,
                candidate,
                atom,
                0,
                1,
                0,
                6,
                C.byref(kind),
                C.byref(fmt),
                C.byref(n),
                C.byref(remaining),
                C.byref(data),
            )
            owner = (
                C.cast(data, C.POINTER(u))[0]
                if status == 0 and data and n.value == 1 and fmt.value == 32
                else 0
            )
            if data:
                lib.XFree(data)
            if owner == pid:
                rr, child = u(), u()
                xx, yy = i(), i()
                w, h, b, d = C.c_uint(), C.c_uint(), C.c_uint(), C.c_uint()
                if (
                    lib.XGetGeometry(
                        display,
                        candidate,
                        C.byref(rr),
                        C.byref(xx),
                        C.byref(yy),
                        C.byref(w),
                        C.byref(h),
                        C.byref(b),
                        C.byref(d),
                    )
                    and lib.XTranslateCoordinates(
                        display,
                        candidate,
                        root,
                        0,
                        0,
                        C.byref(xx),
                        C.byref(yy),
                        C.byref(child),
                    )
                    and (
                        d.value == 24
                        and w.value * h.value <= 16777216
                        and xx.value <= x
                        and yy.value <= y
                        and x + width <= xx.value + w.value
                        and y + height <= yy.value + h.value
                    )
                ):
                    matches.append(
                        (w.value * h.value, candidate, x - xx.value, y - yy.value)
                    )
            if depth < 4:
                rr, parent, children, count = u(), u(), C.POINTER(u)(), C.c_uint()
                if lib.XQueryTree(
                    display,
                    candidate,
                    C.byref(rr),
                    C.byref(parent),
                    C.byref(children),
                    C.byref(count),
                ):
                    need(count.value <= 1024, "Window children exceed budget")
                    pending.extend((children[k], depth + 1) for k in range(count.value))
                    if children:
                        lib.XFree(children)
        need(matches, "No owned window contains the selected region")
        _, window, left, top = min(matches)
        composite.XCompositeRedirectWindow(display, window, 1)
        lib.XSync(display, 0)
        pixmap = composite.XCompositeNameWindowPixmap(display, window)
        lib.XSync(display, 0)
        image = lib.XGetImage(display, pixmap, left, top, width, height, 0xFFFFFFFF, 2)
        need(image, "Owned window capture failed")
        rows = bytearray()
        for row in range(height):
            rows.append(0)
            for col in range(width):
                pixel = lib.XGetPixel(image, col, row)
                rows.extend(((pixel >> 16) & 255, (pixel >> 8) & 255, pixel & 255))

        def chunk(name, data):
            return (
                struct.pack(">I", len(data))
                + name
                + data
                + struct.pack(">I", zlib.crc32(name + data) & 0xFFFFFFFF)
            )

        raw = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows))
            + chunk(b"IEND", b"")
        )
        need(len(raw) <= 700000, "Screenshot byte budget exceeded")
        return {"data": base64.b64encode(raw).decode(), "geometry": region}
    finally:
        if image:
            lib.XDestroyImage(image)
        if pixmap:
            lib.XFreePixmap(display, pixmap)
        if window:
            composite.XCompositeUnredirectWindow(display, window, 1)
        lib.XCloseDisplay(display)


def main():
    portal_mode = os.environ.get("IATEST_WAYLAND_PORTAL") == "1"
    need(
        sys.platform.startswith("linux")
        and (
            portal_mode
            and os.environ.get("WAYLAND_DISPLAY")
            or os.environ.get("DISPLAY")
            and os.environ.get("XDG_SESSION_TYPE", "").lower() != "wayland"
            and not os.environ.get("WAYLAND_DISPLAY")
        ),
        "An X11 desktop or explicit Wayland portal mode is required",
    )
    api, bound, sequence = Atspi(), None, 0
    try:
        while True:
            line = sys.stdin.buffer.readline(65538)
            if not line:
                break
            reply = {}
            try:
                need(
                    len(line) <= 65536 and line.endswith(b"\n"), "Input quota exceeded"
                )
                packet = json.loads(line)
                reply["sequence"] = packet.get("sequence")
                need(
                    type(packet.get("sequence")) is int
                    and packet["sequence"] == sequence + 1,
                    "Sequence mismatch",
                )
                sequence = packet["sequence"]
                pid, app_id = packet.get("process_id"), packet.get("app_id")
                need(
                    type(pid) is int and pid > 0 and isinstance(app_id, str),
                    "Invalid process identity",
                )
                generation = process_identity(pid, app_id)
                if bound is None:
                    bound = generation
                need(bound == generation, "Process generation changed")
                op, target = packet.get("op"), packet.get("target")
                if op == "portal_start":
                    need(
                        portal_mode and not getattr(api, "portal", None),
                        "Explicit, single Wayland portal session required",
                    )
                    import importlib.util

                    spec = importlib.util.spec_from_file_location(
                        "iatest_wayland_portal",
                        Path(__file__).with_name("WaylandPortal.py"),
                    )
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    api.portal = module.Portal()
                    backend = packet.get("backend", "keyboard")
                    reply["result"] = api.portal.start(backend, pid)
                    if backend == "gnome":
                        spec = importlib.util.spec_from_file_location(
                            "iatest_gnome_desktop",
                            Path(__file__).with_name("GnomeDesktop.py"),
                        )
                        gnome = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(gnome)
                        api.gnome = gnome.GnomeDesktop(api, api.portal, pid)
                    print(json.dumps(reply), flush=True)
                    continue
                need(
                    op
                    in (
                        "query",
                        "click",
                        "fill",
                        "press",
                        "right_click",
                        "drag",
                        "scroll",
                        "screenshot",
                        "pointer_click",
                    )
                    and isinstance(target, dict)
                    and target,
                    "Explicit target and supported operation required",
                )
                need(
                    all(
                        k in ("identifier", "name", "role")
                        and isinstance(v, str)
                        and 0 < len(v) <= 500
                        for k, v in target.items()
                    ),
                    "Invalid target",
                )
                found, complete, visited = api.query(pid, target)
                need(
                    process_identity(pid, app_id) == bound, "Process generation changed"
                )
                if op == "query":
                    reply["result"] = {
                        "complete": complete,
                        "match_count": len(found),
                        "visited_nodes": visited,
                        "candidates": [api.row(n, f) for n, f in found[:3]],
                    }
                else:
                    need(
                        complete and len(found) == 1,
                        "Action requires exactly one current complete target",
                    )
                    reply["result"] = api.action(found[0][0], op, packet)
            except ValueError as exc:
                reply["error"] = str(exc)
            except (OSError, KeyError, TypeError, UnicodeError):
                reply["error"] = (
                    "Linux desktop request rejected; check identity, complete target and AT-SPI support"
                )
            finally:
                api.release()
            print(json.dumps(reply, ensure_ascii=False, allow_nan=False), flush=True)
            if len(line) > 65536:
                break
    finally:
        if getattr(api, "portal", None):
            api.portal.close()
        api.release()
        api.call("exit")


if __name__ == "__main__":
    main()
