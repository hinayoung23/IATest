"""Small PNG regions and reproducible pixel anchors; no image-only test verdict."""

from __future__ import annotations

import base64
import hashlib
import struct
import time

from iatest.engines.projective import inverse, project

ANCHOR_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["sha256", "x", "y", "width", "height", "viewport"],
    "properties": {
        "binding": {"type": "string", "minLength": 1, "maxLength": 128},
        "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "x": {"type": "number", "minimum": 0, "maximum": 1024},
        "y": {"type": "number", "minimum": 0, "maximum": 1024},
        "width": {"type": "integer", "minimum": 1, "maximum": 1024},
        "height": {"type": "integer", "minimum": 1, "maximum": 1024},
        "viewport": {
            "type": "object",
            "additionalProperties": False,
            "required": ["width", "height", "dpr"],
            "properties": {
                k: {"type": "number", "exclusiveMinimum": 0, "maximum": 10000}
                for k in ("width", "height", "dpr")
            },
        },
    },
}


def stable_png(capture):
    """Require two consecutive bounded PNGs; never retry an input operation."""
    previous = None
    for _ in range(3):
        data = capture()
        if (len(data) > 1048576 or len(data) < 24
                or not data.startswith(b"\x89PNG\r\n\x1a\n")):
            raise ValueError("Visual PNG exceeds its byte budget or is invalid")
        width, height = struct.unpack(">II", data[16:24])
        if not (0 < width <= 1024 and 0 < height <= 1024 and width * height <= 524288):
            raise ValueError("Visual device-pixel budget exceeded")
        if data == previous:
            return data, width, height
        previous = data
    raise ValueError("Visual pixels did not stabilize within three captures")


class BrowserVisual:
    def visual_region(self, target):
        observed = self.query(target, cache=False)
        if not observed["unique"]:
            raise ValueError("Visual region must resolve uniquely and completely")
        # OOPIF hit-test data is published on compositor frames. A screenshot
        # alone does not prove that the input router has consumed new geometry.
        self.evaluate("new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))")
        observed = self.query(target, cache=False)
        if not observed["unique"]:
            raise ValueError("Visual region changed before capture")
        candidate = observed["candidates"][0]
        rect = candidate["rect"]
        clip, matrix = self.root_geometry(rect)
        width, height = clip["width"], clip["height"]
        viewport = self.root_viewport()
        if (
            width <= 0
            or height <= 0
            or width > 1024
            or height > 1024
            or width * height > 524288
        ):
            raise ValueError("Visual region exceeds its edge/pixel budget")
        if (
            clip["x"] < 0
            or clip["y"] < 0
            or clip["x"] + width > viewport["width"]
            or clip["y"] + height > viewport["height"]
        ):
            raise ValueError(
                "Visual region must fit inside the viewport; scroll explicitly"
            )
        handle = self.evaluate(
            f"globalThis.__iatestNodes[{candidate['index']}]", object_result=True
        )
        identity = self.call("DOM.describeNode", {"objectId": handle["objectId"]})[
            "node"
        ]["backendNodeId"]
        def capture():
            self.evaluate("new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))")
            return base64.b64decode(self._session_call(
                self.page_session,
                "Page.captureScreenshot",
                {
                    "format": "png",
                    "captureBeyondViewport": False,
                    "clip": {
                        "x": clip["x"] + viewport["x"],
                        "y": clip["y"] + viewport["y"],
                        "width": width,
                        "height": height,
                        "scale": 1,
                    },
                },
            )["data"], validate=True)

        data, image_width, image_height = stable_png(capture)
        fresh_clip, fresh_matrix = self.root_geometry(rect)
        if fresh_clip != clip or fresh_matrix != matrix:
            raise ValueError("Visual frame geometry changed during capture")
        return {
            "sha256": hashlib.sha256(data).hexdigest(),
            "width": image_width,
            "height": image_height,
            "viewport": {
                "width": viewport["width"],
                "height": viewport["height"],
                "dpr": self.evaluate("devicePixelRatio"),
            },
            "rect": rect,
            "clip": clip,
            "matrix": matrix,
            "backend_id": identity,
            "world": self.world,
            "session": self.session,
            "data": data,
            "captured": time.monotonic(),
        }

    def visual_click(self, step):
        anchor = step["visual_anchor"]
        observed = self.visual_region(step["target"])
        if any(
            anchor[k] != observed[k] for k in ("sha256", "width", "height", "viewport")
        ):
            raise ValueError("Visual anchor expired: pixels or viewport changed")
        if (
            not 0 <= anchor["x"] < observed["width"]
            or not 0 <= anchor["y"] < observed["height"]
        ):
            raise ValueError("Visual point lies outside its image")
        fresh = self.query(step["target"], cache=False)
        if (
            not fresh["unique"]
            or self.world != observed["world"]
            or self.session != observed["session"]
        ):
            raise ValueError("Visual context expired")
        index = fresh["candidates"][0]["index"]
        handle = self.evaluate(f"globalThis.__iatestNodes[{index}]", object_result=True)
        identity = self.call("DOM.describeNode", {"objectId": handle["objectId"]})[
            "node"
        ]["backendNodeId"]
        if (
            identity != observed["backend_id"]
            or fresh["candidates"][0]["rect"] != observed["rect"]
        ):
            raise ValueError("Visual region identity/geometry changed")
        rect = observed["rect"]
        clip, matrix = self.root_geometry(rect)
        if clip != observed["clip"] or matrix != observed["matrix"]:
            raise ValueError("Visual frame geometry changed")
        root_x = clip["x"] + anchor["x"] / anchor["width"] * clip["width"]
        root_y = clip["y"] + anchor["y"] / anchor["height"] * clip["height"]
        x, y = project(inverse(matrix), root_x, root_y)
        if not (rect["x"] <= x < rect["x"] + rect["width"] and rect["y"] <= y < rect["y"] + rect["height"]):
            raise ValueError("Visual point lies outside the projected target")
        self.position_pointer(self.root_point({"x": x, "y": y}, hit_test=True))
        hover = self.query(step["target"], cache=False)
        if (not hover["unique"] or hover["candidates"][0]["rect"] != rect
                or self.world != observed["world"] or self.session != observed["session"]):
            raise ValueError("Visual target changed during pointer movement")
        index = hover["candidates"][0]["index"]
        handle = self.evaluate(f"globalThis.__iatestNodes[{index}]", object_result=True)
        if self.call("DOM.describeNode", {"objectId": handle["objectId"]})["node"]["backendNodeId"] != identity:
            raise ValueError("Visual target replaced during pointer movement")
        ready = self.evaluate(
            f"(()=>{{const e=globalThis.__iatestNodes[{index}],h=globalThis.__iatestHit({x},{y});return !!(e&&h&&(h===e||e.contains(h)));}})()"
        )
        if not ready or time.monotonic() - observed["captured"] > 0.5:
            raise ValueError("Visual target obscured or capture too old")
        point = self.root_point({"x": x, "y": y}, hit_test=True)
        if abs(point["x"] - root_x) > .1 or abs(point["y"] - root_y) > .1:
            raise ValueError("Visual frame moved before input")
        self.pending = True
        try:
            for event in ("mousePressed", "mouseReleased"):
                self.input_call(
                    "Input.dispatchMouseEvent",
                    {
                        "type": event,
                        "x": point["x"],
                        "y": point["y"],
                        "button": "left",
                        "clickCount": 1,
                    },
                )
        except Exception as exc:
            raise RuntimeError("Visual input delivery unknown; do not replay") from exc
        self.pending = False
        return {
            "dispatch": "acknowledged",
            "image_sha256": anchor["sha256"],
            "business_postcondition": "pending",
        }
