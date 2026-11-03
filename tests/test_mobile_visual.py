import copy
import struct
import zlib
from types import SimpleNamespace

import pytest

from iatest.engines.contracts import ExecutionPlan
from iatest.mobile.contracts import validate_mobile_plan
from iatest.mobile.protocol import DeliveryUnknown, ProtocolError
from iatest.mobile.runtime import MobileSession
from iatest.mobile.visual import MobileVisual


def png(color=0):
    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 32, 16, 8, 6, 0, 0, 0))
        + chunk(
            b"IDAT", zlib.compress((b"\x00" + bytes([color, 0, 255, 255]) * 32) * 16)
        )
        + chunk(b"IEND", b"")
    )


class NativeBoundary(MobileVisual):
    unique = staticmethod(MobileSession.unique)

    def __init__(self, platform="android"):
        self.profile = {"platform": platform}
        self.color, self.rotation, self.queries = 0, 0, 0
        self.actions = []
        self.bounds = [10, 20, 110, 70]
        self.unknown = False
        self.device = SimpleNamespace(semantic=self, input=self)
        self.epoch = "fixture-driver-epoch"
        self.capabilities = {"visual_timestamp_checks": True}

    def query(self, target, *, cache, timeout):
        assert cache is False
        self.queries += 1
        return {
            "complete": True,
            "match_count": 1,
            "state_version": 1,
            "display": {
                "id": 0,
                "width": 400,
                "height": 800,
                "rotation": self.rotation,
            },
            "candidates": [
                {
                    "ref": "fresh-" + str(self.queries),
                    "enabled": True,
                    "bounds": list(self.bounds),
                }
            ],
        }

    def request(self, op, args, **kwargs):
        if op == "screenshot":
            assert args["region"] == {"x": 10, "y": 20, "width": 100, "height": 50}
            return {
                "binary": png(self.color),
                "width": 32,
                "height": 16,
                "driver_epoch": self.epoch,
                "captured_uptime_ms": 1000,
                "crop_rect": {"x": 10, "y": 20, "width": 100, "height": 50},
                "viewport": {
                    "width": 400,
                    "height": 800,
                    "dpr": 1,
                    "rotation": self.rotation,
                },
            }
        assert op in {"tap", "visual_tap"}
        self.actions.append((op, args, kwargs))
        if self.unknown:
            raise DeliveryUnknown("lost reply")
        return {"status": "injection_call_returned", "accepted": True}


def anchor(native):
    value = native.visual_region({"identifier": "canvas"})
    return {
        **{k: value[k] for k in ("sha256", "width", "height", "viewport")},
        "x": 16,
        "y": 8,
    }


def test_explicit_visual_crop_maps_only_verified_local_pixels_to_the_native_parent():
    class Cropped(NativeBoundary):
        def request(self, op, args, **kwargs):
            value = super().request(
                op,
                {"region": {"x": 10, "y": 20, "width": 100, "height": 50}}
                if op == "screenshot"
                else args,
                **kwargs,
            )
            if op == "screenshot":
                value["crop_rect"] = dict(args["region"])
                self.last_crop = dict(args["region"])
            return value

    native = Cropped("android")
    crop = {"x": 0.5, "y": 0.5, "width": 0.5, "height": 0.5}
    region = native.visual_region({"identifier": "canvas"}, crop=crop)
    observed = {
        **{k: region[k] for k in ("sha256", "width", "height", "viewport")},
        "x": 16,
        "y": 8,
    }
    native.visual_click(
        {
            "target": {"identifier": "canvas"},
            "visual_crop": crop,
            "visual_anchor": observed,
        },
        "cropped-click",
    )
    assert native.last_crop == {"x": 60, "y": 45, "width": 50, "height": 25}
    _, args, _ = native.actions[-1]
    assert args["x"] == 85 and args["y"] == 57.5


@pytest.mark.parametrize(
    "crop",
    [
        {"x": 0.8, "y": 0, "width": 0.3, "height": 1},
        {"x": 0, "y": 0, "width": 0, "height": 1},
        {"x": float("nan"), "y": 0, "width": 1, "height": 1},
    ],
)
def test_visual_crop_outside_parent_is_rejected_before_native_queries(crop):
    native = NativeBoundary("android")
    with pytest.raises(ValueError, match="crop"):
        native.visual_region({"identifier": "canvas"}, crop=crop)
    assert not native.actions and native.queries == 0


@pytest.mark.parametrize("platform,wire", [("android", "tap"), ("ios", "visual_tap")])
def test_native_visual_crops_and_revalidates_before_real_input_boundary(platform, wire):
    native = NativeBoundary(platform)
    step = {
        "op": "visual_click",
        "target": {"identifier": "canvas"},
        "visual_anchor": anchor(native),
    }
    plan = ExecutionPlan(
        "visual",
        platform,
        {
            "profile": "phone",
            "steps": [
                step,
                {
                    "op": "expect",
                    "target": {"identifier": "status"},
                    "expected": "saved",
                },
            ],
        },
    )
    validate_mobile_plan(plan)
    native.visual_click(step, "once")
    op, args, kwargs = native.actions[0]
    assert op == wire and (args["x"], args["y"]) == (60, 45)
    assert args["ref"] == ("fresh-2" if platform == "ios" else "fresh-3")
    assert native.queries == (2 if platform == "ios" else 3)
    assert kwargs["operation_id"] == "once"
    if platform == "ios":
        assert (
            args["image_captured_uptime_ms"] == 1000
            and args["max_image_age_ms"] == 2000
        )


def test_pre_capture_query_time_does_not_age_fresh_pixels(monkeypatch):
    from iatest.mobile import visual

    clock = [0.0]
    monkeypatch.setattr(visual.time, "monotonic", lambda: clock[0])

    class SlowDiscovery(NativeBoundary):
        def query(self, target, **options):
            clock[0] += 3 if self.queries < 2 else 0.3
            return super().query(target, **options)

    native = SlowDiscovery()
    native.visual_click(
        {"target": {"identifier": "canvas"}, "visual_anchor": anchor(native)},
        "fresh-pixels",
    )
    assert len(native.actions) == 1


def test_visual_requery_shares_remaining_image_age_and_rejects_late_result(monkeypatch):
    from iatest.mobile import visual

    clock = [0.0]
    monkeypatch.setattr(visual.time, "monotonic", lambda: clock[0])

    class SlowCapture(NativeBoundary):
        def request(self, op, args, **options):
            if op == "screenshot":
                clock[0] += 1.8
            return super().request(op, args, **options)

        def query(self, target, **options):
            if self.queries == 2:
                assert 0 < options["timeout"] <= 0.201
                clock[0] += 0.3
            return super().query(target, **options)

    native = SlowCapture()
    step = {"target": {"identifier": "canvas"}, "visual_anchor": anchor(native)}
    with pytest.raises(ProtocolError, match="stale"):
        native.visual_click(step, "late-result")
    assert not native.actions


def test_ios_visual_click_requires_native_expiry_support_before_input():
    native = NativeBoundary("ios")
    step = {"target": {"identifier": "canvas"}, "visual_anchor": anchor(native)}
    native.capabilities = {}
    with pytest.raises(ProtocolError, match="Rebuild"):
        native.visual_click(step, "legacy-driver")
    assert not native.actions


@pytest.mark.parametrize("mutation", ["pixels", "rotation"])
def test_native_visual_expiry_blocks_input(mutation):
    native = NativeBoundary()
    value = anchor(native)
    if mutation == "pixels":
        native.color = 200
    else:
        native.rotation = 1
    with pytest.raises(ProtocolError, match="expired"):
        native.visual_click(
            {"target": {"identifier": "canvas"}, "visual_anchor": value}, "blocked"
        )
    assert not native.actions


def test_native_visual_unknown_is_not_replayed_and_plan_needs_postcondition():
    native = NativeBoundary()
    step = {
        "op": "visual_click",
        "target": {"identifier": "canvas"},
        "visual_anchor": copy.deepcopy(anchor(native)),
    }
    native.unknown = True
    with pytest.raises(DeliveryUnknown):
        native.visual_click(step, "unknown")
    assert len(native.actions) == 1
    with pytest.raises(ValueError, match="expectation"):
        validate_mobile_plan(
            ExecutionPlan("no-oracle", "android", {"profile": "phone", "steps": [step]})
        )


@pytest.mark.parametrize("captured_display", [None, 0, 7, 1])
def test_secondary_display_visual_input_requires_pixels_from_the_selected_display(captured_display):
    class Secondary(NativeBoundary):
        def query(self, target, **kwargs):
            observed = super().query(target, **kwargs)
            observed["display"]["id"] = 1
            return observed

        def request(self, op, args, **kwargs):
            value = super().request(op, args, **kwargs)
            if op == "screenshot":
                value["display_id"] = captured_display
            return value

    native = Secondary()
    native.profile["display_id"] = 1
    native.capabilities["selected_display_capture"] = True
    if captured_display != 1:
        with pytest.raises(ProtocolError, match="selected display"):
            anchor(native)
        assert not native.actions
    else:
        native.visual_click({"target": {"identifier": "canvas"}, "visual_anchor": anchor(native)}, "secondary")
        assert native.actions[0][1]["display"]["id"] == 1
