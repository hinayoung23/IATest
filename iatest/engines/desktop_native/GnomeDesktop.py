"""Scoped GNOME geometry, portal pointer input and native window PNG cropping.

Only stdlib and the desktop's GIO/GdkPixbuf are used. The Shell adapter requires
separate per-process consent; all input uses the authorized XDG portal.
"""

from __future__ import annotations

import base64
import ctypes as C
import ctypes.util
import hashlib
import json
import math
import struct
import time


def need(value, message):
    if not value:
        raise ValueError(message)


def contains(outer, inner):
    x, y, w, h = outer
    xx, yy, ww, hh = inner
    return (
        w > 0
        and h > 0
        and ww > 0
        and hh > 0
        and xx >= x - 0.01
        and yy >= y - 0.01
        and xx + ww <= x + w + 0.01
        and yy + hh <= y + h + 0.01
    )


class GnomeDesktop:
    def __init__(self, api, portal, pid):
        self.api, self.portal, self.pid = api, portal, pid

    def extents(self, node):
        interface = self.api.own(self.api.call("accessible_get_component_iface", node))
        need(interface, "Target has no native geometry")
        value = self.api.call("component_get_extents", interface, 1, error=True)
        need(value, "Window-local target geometry unavailable")
        try:
            return list(C.cast(value, C.POINTER(C.c_int * 4)).contents)
        finally:
            self.api.glib.g_free(value)

    def region(self, node):
        root = node
        for _ in range(32):
            if self.api.string("accessible_get_role_name", root) in (
                "frame",
                "window",
                "dialog",
                "alert",
            ):
                break
            root = self.api.own(
                self.api.call("accessible_get_parent", root, error=True)
            )
            need(root, "Target has no native window")
        else:
            raise ValueError("Window ancestry budget exceeded")
        title = self.api.string("accessible_get_name", root)
        local, whole = self.extents(node), self.extents(root)
        need(
            local[2] > 0 and local[3] > 0 and contains(whole, local),
            "Target is not completely visible in its window",
        )
        state = self.portal.inspect(title)
        need(
            state["pid"] == self.pid and len(state["windows"]) == 1,
            "One matching process-owned Wayland window is required",
        )
        window = state["windows"][0]
        need(
            window["active"] and window["visible"],
            "Selected Wayland window must be active and visible",
        )
        bases = [
            r
            for r in (window["frame"], window["buffer"])
            if abs(r[2] - whole[2]) <= 0.01 and abs(r[3] - whole[3]) <= 0.01
        ]
        need(
            bases and all(r == bases[0] for r in bases),
            "Native and compositor window sizes do not agree",
        )
        base = bases[0]
        rect = [
            base[0] + local[0] - whole[0],
            base[1] + local[1] - whole[1],
            local[2],
            local[3],
        ]
        node_id, props = self.portal.streams[0]
        monitor = [*props.get("position", []), *props.get("size", [])]
        need(
            len(monitor) == 4
            and all(isinstance(v, (int, float)) and math.isfinite(v) for v in monitor),
            "Authorized monitor geometry is unavailable",
        )
        need(
            monitor in state["monitors"] and contains(monitor, rect),
            "Target left the authorized monitor; start a new consent session",
        )
        binding = hashlib.sha256(
            json.dumps(
                [state["epoch"], state["pid"], state["start"], window["id"]]
            ).encode()
        ).hexdigest()
        return {
            "rect": rect,
            "local": local,
            "window": window["id"],
            "epoch": state["epoch"],
            "binding": binding,
            "node": node_id,
            "monitor": monitor,
            "title": title,
        }

    def move(self, info, x, y):
        need(
            self.portal.check_point(info["window"], x, y), "Pointer target is obscured"
        )
        self.portal.motion(info["node"], x - info["monitor"][0], y - info["monitor"][1])
        deadline = time.monotonic() + 0.4
        while not self.portal.check_point(info["window"], x, y, pointer=True):
            if time.monotonic() > deadline:
                actual = self.portal.inspect(info["title"]).get("pointer")
                raise ValueError(
                    f"Portal pointer did not reach the verified target: requested {[x, y]}, actual {actual}"
                )
            time.sleep(0.01)

    def action(self, node, op, packet):
        info = self.region(node)
        x, y, w, h = info["rect"]
        if op == "screenshot":
            meta, data = self.portal.capture(info["window"])
            need(
                meta["epoch"] == info["epoch"]
                and meta["pid"] == self.pid
                and meta["window"] == info["window"],
                "Capture window identity changed",
            )
            need(self.region(node) == info, "Window geometry changed during capture")
            png, geometry = crop_png(data, meta["area"], info["rect"])
            return {
                "data": base64.b64encode(png).decode(),
                "geometry": geometry,
                "target_geometry": info["rect"],
                "binding": info["binding"],
            }
        fx, fy = packet.get("fraction_x", 0.5), packet.get("fraction_y", 0.5)
        need(
            all(
                isinstance(v, (int, float)) and math.isfinite(v) and 0 <= v < 1
                for v in (fx, fy)
            ),
            "Invalid pointer fractions",
        )
        px, py = x + w * fx, y + h * fy
        if "capture_point" in packet:
            need(
                packet.get("capture_binding") == info["binding"]
                and packet.get("capture_geometry") == info["rect"],
                "Visual window or geometry changed before input",
            )
            px, py = packet["capture_point"]
            need(
                contains(info["rect"], [px, py, 0.001, 0.001]),
                "Visual point exceeds its semantic target",
            )
        self.api.hit(
            node,
            round(info["local"][0] + px - x),
            round(info["local"][1] + py - y),
            coordinates=1,
        )
        self.move(info, px, py)
        if op == "drag":
            found, complete, _ = self.api.query(self.pid, packet.get("destination", {}))
            need(complete and len(found) == 1, "Drag destination must be unique")
            end = self.region(found[0][0])
            need(
                end["window"] == info["window"],
                "Drag must remain in the selected window",
            )
            ex, ey, ew, eh = end["rect"]
            ex += ew / 2
            ey += eh / 2
            self.api.hit(
                found[0][0],
                round(end["local"][0] + ew / 2),
                round(end["local"][1] + eh / 2),
                coordinates=1,
            )
            need(
                self.portal.check_point(end["window"], ex, ey),
                "Drag destination is obscured",
            )
            try:
                self.portal.button(0x110, True)
                for n in range(1, 13):
                    need(
                        self.region(node) == info,
                        "Window moved during drag; input delivery may be partial",
                    )
                    self.move(info, px + (ex - px) * n / 12, py + (ey - py) * n / 12)
                    time.sleep(0.015)
            finally:
                self.portal.button(0x110, False)
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
            self.portal.scroll(dx, dy)
        elif op in ("right_click", "pointer_click"):
            button = 0x111 if op == "right_click" else 0x110
            try:
                self.portal.button(button, True)
            finally:
                self.portal.button(button, False)
        else:
            raise ValueError("Unsupported Wayland pointer operation")
        return {
            "dispatch": "acknowledged",
            "backend": "GNOME/XDG portal",
            "business_postcondition": "pending",
        }


def crop_png(data, area, rect):
    """Crop only the owned window; preserve exact pixel-to-logical geometry."""
    need(
        24 <= len(data) <= 8388608 and data.startswith(b"\x89PNG\r\n\x1a\n"),
        "Invalid native window PNG",
    )
    width, height = struct.unpack(">II", data[16:24])
    need(
        0 < width <= 16384 and 0 < height <= 16384 and width * height <= 16777216,
        "Native PNG pixel budget exceeded",
    )
    need(
        len(area) == 4
        and all(isinstance(v, (int, float)) and math.isfinite(v) for v in area)
        and contains(area, rect),
        "Capture area does not contain the target",
    )
    lib = C.CDLL(ctypes.util.find_library("gdk_pixbuf-2.0") or "libgdk_pixbuf-2.0.so.0")
    glib = C.CDLL(ctypes.util.find_library("glib-2.0") or "libglib-2.0.so.0")
    obj = C.CDLL(ctypes.util.find_library("gobject-2.0") or "libgobject-2.0.so.0")
    p, i, s = C.c_void_p, C.c_int, C.c_char_p
    signatures = {
        "gdk_pixbuf_loader_new": (p, []),
        "gdk_pixbuf_loader_write": (i, [p, p, C.c_size_t, C.POINTER(p)]),
        "gdk_pixbuf_loader_close": (i, [p, C.POINTER(p)]),
        "gdk_pixbuf_loader_get_pixbuf": (p, [p]),
        "gdk_pixbuf_get_width": (i, [p]),
        "gdk_pixbuf_get_height": (i, [p]),
        "gdk_pixbuf_new_subpixbuf": (p, [p, i, i, i, i]),
        "gdk_pixbuf_scale_simple": (p, [p, i, i, i]),
        "gdk_pixbuf_save_to_bufferv": (
            i,
            [p, C.POINTER(p), C.POINTER(C.c_size_t), s, p, p, C.POINTER(p)],
        ),
    }
    for name, (result, args) in signatures.items():
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = result, args
    for name in ("g_free", "g_error_free"):
        getattr(glib, name).argtypes = [p]
    obj.g_object_unref.argtypes = [p]
    loader = lib.gdk_pixbuf_loader_new()
    crop = scaled = None
    output = p()
    error = p()

    def check(ok):
        if error.value:
            glib.g_error_free(error)
            error.value = None
            raise ValueError("Native PNG decode/crop failed")
        need(ok, "Native PNG decode/crop failed")

    try:
        raw = C.create_string_buffer(data)
        check(lib.gdk_pixbuf_loader_write(loader, raw, len(data), C.byref(error)))
        check(lib.gdk_pixbuf_loader_close(loader, C.byref(error)))
        image = lib.gdk_pixbuf_loader_get_pixbuf(loader)
        need(image, "Native PNG has no pixels")
        width, height = (
            lib.gdk_pixbuf_get_width(image),
            lib.gdk_pixbuf_get_height(image),
        )
        need(
            0 < width <= 16384 and 0 < height <= 16384 and width * height <= 16777216,
            "Native PNG pixel budget exceeded",
        )
        sx, sy = width / area[2], height / area[3]
        need(
            abs(sx - sy) < 0.01 and 0.25 <= sx <= 8,
            f"Window image scaling is not uniform: PNG {width}x{height}, area {area}",
        )
        left = max(0, math.floor((rect[0] - area[0]) * sx))
        top = max(0, math.floor((rect[1] - area[1]) * sy))
        right = min(width, math.ceil((rect[0] + rect[2] - area[0]) * sx))
        bottom = min(height, math.ceil((rect[1] + rect[3] - area[1]) * sy))
        w, h = right - left, bottom - top
        need(w > 0 and h > 0, "Empty capture region")
        crop = lib.gdk_pixbuf_new_subpixbuf(image, left, top, w, h)
        need(crop, "Native crop failed")
        factor = min(1, 1024 / w, 1024 / h, math.sqrt(524288 / (w * h)))
        if factor < 1:
            scaled = lib.gdk_pixbuf_scale_simple(
                crop, max(1, int(w * factor)), max(1, int(h * factor)), 2
            )
            need(scaled, "Native crop scaling failed")
        size = C.c_size_t()
        check(
            lib.gdk_pixbuf_save_to_bufferv(
                scaled or crop,
                C.byref(output),
                C.byref(size),
                b"png",
                None,
                None,
                C.byref(error),
            )
        )
        need(size.value <= 700000, "Capture PNG byte budget exceeded")
        return C.string_at(output, size.value), [
            area[0] + left / sx,
            area[1] + top / sy,
            w / sx,
            h / sy,
        ]
    finally:
        if output:
            glib.g_free(output)
        for value in (scaled, crop, loader):
            if value:
                obj.g_object_unref(value)
