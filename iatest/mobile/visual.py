"""Fresh native regions and image-bound taps, independent from preview video."""

from __future__ import annotations

import copy
import hashlib
import math
import struct
import time

from iatest.engines.web_visual import ANCHOR_SCHEMA as WEB_ANCHOR
from iatest.mobile.protocol import DeliveryUnknown, ProtocolError

ANCHOR_SCHEMA = copy.deepcopy(WEB_ANCHOR)
ANCHOR_SCHEMA["properties"]["viewport"]["properties"]["rotation"] = {
    "type": "integer",
    "minimum": 0,
    "maximum": 6,
}
ANCHOR_SCHEMA["properties"]["viewport"]["required"].append("rotation")

CROP_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["x", "y", "width", "height"],
    "properties": {
        k: {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            **({"exclusiveMinimum": 0} if k in {"width", "height"} else {}),
        }
        for k in ("x", "y", "width", "height")
    },
}


def validate_crop(crop):
    if crop is None:
        return
    from iatest.core.schema import schema_errors

    if (
        schema_errors(crop, CROP_SCHEMA)
        or any(not math.isfinite(v) for v in crop.values())
        or crop["x"] + crop["width"] > 1 + 1e-9
        or crop["y"] + crop["height"] > 1 + 1e-9
    ):
        raise ValueError(
            "Visual crop must be a finite normalized rectangle inside the native target"
        )


class MobileVisual:
    def visual_region(self, target, *, crop=None):
        validate_crop(crop)
        observed = self.query(target, cache=False, timeout=2)
        candidate = self.unique(observed)
        left, top, right, bottom = candidate["bounds"]
        if (
            any(
                not isinstance(v, (int, float)) or not math.isfinite(v)
                for v in (left, top, right, bottom)
            )
            or right <= left
            or bottom <= top
        ):
            raise ProtocolError("Invalid native visual region geometry")
        capture_started = time.monotonic()
        value = self.device.semantic.request(
            "screenshot",
            {
                "region": {
                    "x": left + (crop["x"] if crop else 0) * (right - left),
                    "y": top + (crop["y"] if crop else 0) * (bottom - top),
                    "width": (crop["width"] if crop else 1) * (right - left),
                    "height": (crop["height"] if crop else 1) * (bottom - top),
                }
            },
            timeout=3,
        )
        if self.profile["platform"] == "android" and (
            self.profile.get("display_id", 0) != 0
            or self.device.semantic.capabilities.get("selected_display_capture")
        ) and value.get("display_id") != self.profile.get("display_id", 0):
            raise ProtocolError("Native image does not belong to the selected display")
        data = value.pop("binary", b"")
        if (
            len(data) < 24
            or len(data) > 1048576
            or not data.startswith(b"\x89PNG\r\n\x1a\n")
        ):
            raise ProtocolError("Native visual PNG exceeds bounds")
        width, height = struct.unpack(">II", data[16:24])
        if (
            (width, height) != (value.get("width"), value.get("height"))
            or not 0 < width <= 1024
            or not 0 < height <= 1024
            or width * height > 524288
        ):
            raise ProtocolError("Native visual geometry exceeds pixel budget")
        crop = value.get("crop_rect")
        viewport = value.get("viewport")
        if (
            not isinstance(crop, dict)
            or not isinstance(viewport, dict)
            or not value.get("driver_epoch")
        ):
            raise ProtocolError("Native image lacks crop/viewport/session bindings")
        for k in ("x", "y", "width", "height"):
            if not isinstance(crop.get(k), (int, float)) or not math.isfinite(crop[k]):
                raise ProtocolError("Invalid native crop binding")
        if crop["width"] <= 0 or crop["height"] <= 0:
            raise ProtocolError("Empty native crop")
        from iatest.core.schema import schema_errors

        if schema_errors(
            {
                "sha256": hashlib.sha256(data).hexdigest(),
                "width": width,
                "height": height,
                "x": 0,
                "y": 0,
                "viewport": viewport,
            },
            ANCHOR_SCHEMA,
        ):
            raise ProtocolError("Native viewport binding is invalid")
        return {
            "data": data,
            "sha256": hashlib.sha256(data).hexdigest(),
            "width": width,
            "height": height,
            "viewport": viewport,
            "rect": crop,
            "candidate_bounds": candidate["bounds"],
            "candidate": candidate,
            "state_version": observed.get("state_version"),
            "driver_epoch": value["driver_epoch"],
            "captured": capture_started,
            "captured_uptime_ms": value.get("captured_uptime_ms"),
            "target": target,
        }

    def visual_click(self, step, operation_id, timeout=5):
        ios = self.profile["platform"] == "ios"
        if ios and not self.device.semantic.capabilities.get("visual_timestamp_checks"):
            raise ProtocolError(
                "Rebuild the iOS driver to enforce visual capture expiry"
            )
        anchor = step["visual_anchor"]
        region = self.visual_region(step["target"], crop=step.get("visual_crop"))
        if any(
            anchor[k] != region[k] for k in ("sha256", "width", "height", "viewport")
        ):
            raise ProtocolError("Native visual anchor expired: image or screen changed")
        if (
            not 0 <= anchor["x"] < region["width"]
            or not 0 <= anchor["y"] < region["height"]
        ):
            raise ProtocolError("Visual point outside image")
        age_remaining = 2 - (time.monotonic() - region["captured"])
        if age_remaining <= 0:
            raise ProtocolError("Native image became stale during capture")
        if ios:
            # The Runner repeats uniqueness/identity/geometry checks before input.
            # A full host requery duplicates that work and needlessly ages pixels.
            candidate = region["candidate"]
            state_version = region["state_version"]
        else:
            fresh = self.query(
                step["target"], cache=False, timeout=min(1, age_remaining)
            )
            candidate = self.unique(fresh)
            state_version = fresh.get("state_version")
        if (
            candidate["bounds"] != region["candidate_bounds"]
            or time.monotonic() - region["captured"] > 2
        ):
            raise ProtocolError("Native target moved or image became stale")
        if region["driver_epoch"] != self.device.semantic.epoch:
            raise ProtocolError("Native driver restarted after capture")
        rect = region["rect"]
        x, y = (
            rect["x"] + anchor["x"] / region["width"] * rect["width"],
            rect["y"] + anchor["y"] / region["height"] * rect["height"],
        )
        left, top, right, bottom = candidate["bounds"]
        if (
            not left <= x < right
            or not top <= y < bottom
            or not candidate.get("enabled", True)
        ):
            raise ProtocolError("Visual point outside current enabled target")
        arguments = {
            "ref": candidate["ref"],
            "state_version": state_version,
            "x": x,
            "y": y,
        }
        if ios:
            captured = region.get("captured_uptime_ms")
            if (
                type(captured) not in (int, float)
                or not math.isfinite(captured)
                or captured <= 0
            ):
                raise ProtocolError("iOS image lacks a native capture timestamp")
            arguments.update(image_captured_uptime_ms=captured, max_image_age_ms=2000)
        android = self.profile["platform"] == "android"
        if android:
            arguments["display"] = fresh["display"]
        response = (self.device.input if android else self.device.semantic).request(
            "tap" if android else "visual_tap",
            arguments,
            operation_id=operation_id,
            timeout=min(timeout, 5),
        )
        if response.get("status") == "unknown":
            raise DeliveryUnknown("Visual tap outcome unknown; never replay")
        if (
            response.get("status") != "injection_call_returned"
            or response.get("accepted") is not True
        ):
            raise ProtocolError("Native backend did not confirm visual input dispatch")
        return response
