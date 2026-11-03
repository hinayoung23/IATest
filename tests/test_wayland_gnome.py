"""Authorization, native geometry and image binding regressions.

Real compositor input/capture is exercised by validate_desktop_interactions.
"""

import base64
import copy
import ctypes.util
import struct
from types import SimpleNamespace

import pytest

from iatest.application.ui_exploration import context_spec
from iatest.engines.desktop import DesktopSession
from iatest.engines.desktop_native.GnomeDesktop import GnomeDesktop, crop_png
from iatest.engines.desktop_native.LinuxDesktop import Atspi
from iatest.engines.desktop_native.WaylandPortal import Portal
from iatest.engines.desktop_setup import prepare_wayland_helper
from iatest.execution.control import RunControl

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def test_full_backend_requires_explicit_portal_before_spawning(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Driver spawned without explicit authorization")

    monkeypatch.setattr("iatest.engines.desktop.subprocess.Popen", unexpected)
    with pytest.raises(ValueError, match="explicitly authorized"):
        DesktopSession({"platform": "linux", "wayland_backend": "gnome"}, RunControl())
    context = {
        "kind": "desktop",
        "platform": "linux",
        "process_id": 123,
        "app_id": "fixture",
        "wayland_portal": True,
        "wayland_backend": "gnome",
    }
    assert context_spec(context)["wayland_backend"] == "gnome"


def visual_session(bindings):
    session = DesktopSession.__new__(DesktopSession)
    calls = []

    def call(op, target, **kwargs):
        calls.append((op, kwargs))
        return (
            {
                "data": base64.b64encode(PNG).decode(),
                "geometry": [20, 30, 2, 2],
                "target_geometry": [19, 29, 5, 5],
                "binding": next(bindings),
            }
            if op == "screenshot"
            else {}
        )

    session.call = call
    return session, calls


def test_identical_pixels_cannot_cross_window_or_consent_generation():
    session, calls = visual_session(iter(["a"] * 2 + ["b"] * 2))
    region = session.visual_region({"identifier": "pad"})
    anchor = {
        k: region[k] for k in ("sha256", "width", "height", "viewport", "binding")
    }
    with pytest.raises(ValueError, match="window identity"):
        session.visual_click(
            {
                "target": {"identifier": "pad"},
                "visual_anchor": {**anchor, "x": 0.5, "y": 0.5},
            }
        )
    assert all(op != "pointer_click" for op, _ in calls)


def test_stable_png_does_not_hide_identity_change_between_captures():
    session, _ = visual_session(iter(["a", "b"]))
    with pytest.raises(ValueError, match="identity changed"):
        session.visual_region({"identifier": "pad"})


def test_scaled_pixel_anchor_preserves_native_crop_geometry():
    session, calls = visual_session(iter(["a"] * 4))
    region = session.visual_region({"identifier": "pad"})
    anchor = {
        k: region[k] for k in ("sha256", "width", "height", "viewport", "binding")
    }
    session.visual_click(
        {
            "target": {"identifier": "pad"},
            "visual_anchor": {**anchor, "x": 0.5, "y": 0.5},
        }
    )
    assert calls[-1] == (
        "pointer_click",
        {
            "fraction_x": 0.5,
            "fraction_y": 0.5,
            "capture_point": [21, 31],
            "capture_geometry": [19, 29, 5, 5],
            "capture_binding": "a",
        },
    )


def geometry_fixture():
    state = {
        "epoch": "consent-1",
        "pid": 123,
        "start": "77",
        "monitors": [[0, 0, 800, 600]],
        "windows": [
            {
                "id": "win-1",
                "frame": [100, 90, 500, 400],
                "buffer": [74, 64, 552, 452],
                "active": True,
                "visible": True,
            }
        ],
    }
    api = SimpleNamespace(
        string=lambda method, node: (
            ("frame" if node == 2 else "button")
            if method == "accessible_get_role_name"
            else "Owned"
        ),
        own=lambda node: node,
        call=lambda *a, **k: 2,
    )
    portal = SimpleNamespace(
        inspect=lambda title: copy.deepcopy(state),
        streams=[[7, {"position": [0, 0], "size": [800, 600]}]],
    )
    gnome = GnomeDesktop(api, portal, 123)
    gnome.extents = lambda node: [10, 20, 40, 30] if node == 1 else [0, 0, 500, 400]
    return gnome, state


def test_window_local_geometry_uses_verified_frame_not_shadow_buffer():
    gnome, state = geometry_fixture()
    before = gnome.region(1)
    assert before["rect"] == [110, 110, 40, 30]
    state["start"] = "78"
    assert gnome.region(1)["binding"] != before["binding"]


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda s: s["windows"][0].update(active=False), "active"),
        (lambda s: s["windows"][0].update(frame=[100, 90, 499, 400]), "sizes"),
        (lambda s: s.update(monitors=[[0, 0, 1024, 768]]), "authorized monitor"),
        (lambda s: s["windows"].append(copy.deepcopy(s["windows"][0])), "One matching"),
    ],
)
def test_unproven_native_window_mapping_is_rejected(mutation, reason):
    gnome, state = geometry_fixture()
    mutation(state)
    with pytest.raises(ValueError, match=reason):
        gnome.region(1)


def test_drag_releases_button_if_compositor_changes_mid_input():
    gnome, _ = geometry_fixture()
    info = gnome.region(1)
    buttons = []
    gnome.region = lambda node: info
    gnome.api.hit = lambda *a, **k: None
    gnome.api.query = lambda *a: ([(1, {})], True, None)
    gnome.portal.button = lambda button, down: buttons.append((button, down))
    gnome.portal.check_point = lambda *a: True
    moves = iter([None, ValueError("Window changed")])

    def move(*args):
        error = next(moves)
        if error:
            raise error

    gnome.move = move
    with pytest.raises(ValueError, match="Window changed"):
        gnome.action(1, "drag", {"destination": {"identifier": "pad"}})
    assert buttons == [(0x110, True), (0x110, False)]


def test_oversized_png_is_rejected_before_loading_native_decoder(monkeypatch):
    def unexpected(*args):
        pytest.fail("Native decoder loaded before checking pixel quota")

    monkeypatch.setattr(
        "iatest.engines.desktop_native.GnomeDesktop.C.CDLL", unexpected
    )
    data = PNG[:16] + struct.pack(">II", 16384, 16384) + PNG[24:]
    with pytest.raises(ValueError, match="pixel budget"):
        crop_png(data, [0, 0, 1, 1], [0, 0, 1, 1])


@pytest.mark.skipif(
    not ctypes.util.find_library("gdk_pixbuf-2.0"), reason="System GdkPixbuf required"
)
def test_native_png_crop_requires_containment_and_returns_actual_geometry():
    data, geometry = crop_png(PNG, [10, 20, 2, 2], [10, 20, 1, 1])
    assert struct.unpack(">II", data[16:24]) == (1, 1)
    assert geometry == [10, 20, 2, 2]
    with pytest.raises(ValueError, match="does not contain"):
        crop_png(PNG, [10, 20, 2, 2], [9, 20, 1, 1])


def test_keyboard_portal_does_not_request_screen_or_shell_permissions():
    portal = Portal.__new__(Portal)
    calls = []

    def request(method, *args, **kwargs):
        calls.append(method)
        return (
            {"session_handle": "/session/owned"}
            if method == "CreateSession"
            else {"devices": 1}
        )

    portal.request = request
    assert portal.start()["pointer"] is False
    assert calls == ["CreateSession", "SelectDevices", "Start"]


def test_revoked_portal_cannot_send_keyboard_input():
    portal = Portal.__new__(Portal)
    portal.closed = True
    portal.glib = SimpleNamespace(g_main_context_iteration=lambda *a: False)
    portal.call = lambda *a, **k: pytest.fail("Input sent after revocation")
    with pytest.raises(ValueError, match="revoked"):
        portal.key(13)


@pytest.mark.parametrize(
    "operation", ["click", "fill", "press", "right_click", "screenshot"]
)
def test_portal_revocation_also_blocks_semantic_actions(operation):
    api = Atspi.__new__(Atspi)

    def revoked():
        raise ValueError("Portal revoked")

    api.portal = SimpleNamespace(pump=revoked)
    api.state = lambda *a: pytest.fail(
        "Semantic action reached AT-SPI after revocation"
    )
    with pytest.raises(ValueError, match="revoked"):
        api.action(1, operation, {"value": "must not be delivered"})


def test_helper_install_never_silently_replaces_modified_code(tmp_path):
    result = prepare_wayland_helper(tmp_path)
    directory = tmp_path / result["uuid"]
    script = directory / "extension.js"
    script.write_text("user reviewed adapter")
    with pytest.raises(ValueError, match="--overwrite"):
        prepare_wayland_helper(tmp_path)
    assert script.read_text() == "user reviewed adapter"
    script.unlink()
    script.symlink_to(directory / "metadata.json")
    with pytest.raises(ValueError, match="symbolic"):
        prepare_wayland_helper(tmp_path, overwrite=True)
