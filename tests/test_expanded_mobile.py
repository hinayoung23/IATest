"""Explicit device boundary tests; no physical devices are enumerated or controlled."""

import hashlib
from types import SimpleNamespace

import pytest

from iatest.execution.control import RunControl
from iatest.mobile.protocol import ProtocolError
from iatest.mobile.store import validate_profile
from iatest.mobile.system import MobileSystem, preflight
from iatest.mobile.video import EncodedPacketSource


class Device:
    def __init__(self, profile):
        self.profile, self.control = profile, RunControl(timeout_seconds=20)
        self.input = self.semantic = self
        self.calls, self.granted = [], False

    def android_permission_state(self, app, permission):
        self.control.check()
        return self.granted

    def request(self, op, args, **kwargs):
        self.control.check()
        self.calls.append((op, args, kwargs))
        if op == "permission_state":
            return {"granted": self.granted}
        if op == "clipboard_expect":
            return {
                "matched": args["expected_sha256"]
                == hashlib.sha256(b"hello").hexdigest()
            }
        if op == "clipboard_restore":
            return {"restored": True}
        return {"status": "injection_call_returned", "accepted": True}

    def adb(self, *args):
        self.control.check()
        self.calls.append(("adb", args, {}))
        self.granted = args[2] == "grant"


@pytest.mark.parametrize("changed", [False, True])
def test_webview_waits_for_socket_and_refuses_replacement_pid(tmp_path, changed):
    from iatest.core.config import init_project
    from iatest.mobile.system import WebViewBrowser

    init_project(tmp_path, "suite")
    calls = []
    reads = 0

    def adb(*args, **kwargs):
        nonlocal reads
        calls.append(args)
        if "pidof" in args:
            return "8" if changed and len(calls) > 1 else "7"
        if args[0] == "forward":
            raise RuntimeError("Owned forward reached")
        reads += 1
        return "" if reads == 1 else "@webview_devtools_remote_7"

    device = SimpleNamespace(
        profile={
            "platform": "android",
            "app_id": "dev.example.app",
            "allow_webview": True,
        },
        adb=adb,
    )
    with pytest.raises(
        (ValueError, RuntimeError),
        match="process changed" if changed else "Owned forward",
    ):
        WebViewBrowser(
            device,
            tmp_path,
            "suite",
            {"allowed_origins": ["http://127.0.0.1:18739"]},
            RunControl(timeout_seconds=1),
            20,
        )
    assert not changed or not any(args[0] == "forward" for args in calls)


def test_unauthorized_system_action_is_rejected_before_app_preparation(
    tmp_path, monkeypatch
):
    from iatest.application.testing_service import execute_test_plan
    from iatest.core.config import init_project
    from iatest.mobile.store import MobileStore

    init_project(tmp_path, "suite")
    MobileStore(tmp_path, "suite").save_profile(
        {
            "id": "p",
            "platform": "android",
            "device_id": "boundary",
            "app_id": "com.example.app",
        }
    )
    called = []
    monkeypatch.setattr(
        "iatest.mobile.delivery.prepare_app", lambda *args: called.append(True)
    )
    result = execute_test_plan(
        tmp_path,
        "suite",
        {
            "id": "forbidden",
            "kind": "android",
            "preparation": {
                "app": {
                    "profile": "p",
                    "artifact_id": "explicit-build",
                    "mode": "upgrade",
                }
            },
            "spec": {
                "profile": "p",
                "steps": [
                    {"op": "clipboard_set", "value": "hello"},
                    {"op": "clipboard_expect", "expected": "hello"},
                ],
            },
        },
    )
    assert result["verdict"] == "inconclusive"
    assert not called


def system(profile):
    device = Device(
        {
            "id": "p",
            "platform": "android",
            "device_id": "boundary",
            "app_id": "com.example.app",
            **profile,
        }
    )
    session = SimpleNamespace(profile=device.profile, device=device)
    return MobileSystem(session, None, "suite"), device


def test_permission_and_clipboard_restore_after_normal_cancellation():
    value, device = system(
        {"allowed_permissions": ["android.permission.CAMERA"], "allow_clipboard": True}
    )
    value.execute(
        {"op": "permission", "permission": "android.permission.CAMERA", "granted": True}
    )
    assert device.granted
    value.execute({"op": "clipboard_set", "value": "hello"})
    value.execute({"op": "clipboard_expect", "expected": "hello"})
    original = device.control
    original.cancelled.set()
    value.close()
    assert device.control is original and not device.granted
    ids = [
        k["operation_id"] for op, _, k in device.calls if op.startswith("clipboard_")
    ]
    assert len(ids) == len(set(ids)) == 3


def test_external_permission_change_requires_reconciliation():
    value, device = system({"allowed_permissions": ["android.permission.CAMERA"]})
    value.execute(
        {"op": "permission", "permission": "android.permission.CAMERA", "granted": True}
    )
    device.granted = False
    with pytest.raises(ProtocolError, match="externally"):
        value.close()
    assert len([op for op, _, _ in device.calls if op == "adb"]) == 1


def test_marked_state_needs_fresh_native_action_unique_identity_and_query_budget(
    monkeypatch,
):
    value, _device = system({"platform": "ios"})
    session = value.session
    session.last_action_started_ms = 100
    session.query = lambda *a, **k: {
        "complete": True,
        "match_count": 1,
        "candidates": [{"secure": False}],
    }
    session.unique = lambda observed: observed["candidates"][0]
    snapshot = {
        "timestamp_ms": 101,
        "sequence": 1,
        "identifiers": {"input": "field"},
        "input_state": {
            "input": {
                "first_responder": True,
                "marked": True,
                "marked_length": 2,
                "selected_length": 0,
            }
        },
    }
    monkeypatch.setattr(
        "iatest.mobile.bridge.read_bridge", lambda *a, **k: snapshot
    )
    step = {
        "op": "input_state_expect",
        "target": {"identifier": "field"},
        "bridge_artifact_id": "development",
        "input_identifier": "input",
        "marked": True,
        "marked_length": 2,
    }
    with pytest.raises(ValueError, match="budget"):
        value.execute(step, max_queries=0)
    assert value.execute(step, max_queries=1)["queries"] == 1
    with pytest.raises(ProtocolError, match="consumed"):
        value.execute(step)
    snapshot["sequence"] = 2
    snapshot["timestamp_ms"] = 99
    with pytest.raises(ProtocolError, match="predates"):
        value.execute(step)


@pytest.mark.parametrize(
    "step",
    [
        {"op": "clipboard_set", "value": "hello"},
        {
            "op": "permission",
            "permission": "android.permission.CAMERA",
            "granted": True,
        },
        {"op": "switch_app", "app_id": "com.other.app"},
        {"op": "web", "web_step": {"op": "find", "target": {"id": "x"}}},
    ],
)
def test_system_operations_fail_preflight_without_profile_authorization(step):
    _value, device = system({})
    with pytest.raises(ValueError):
        preflight(device.profile, {"steps": [step]})
    assert not device.calls


def test_simulator_hid_profile_cannot_be_used_on_physical_device():
    value = {
        "id": "p",
        "platform": "ios",
        "device_id": "boundary",
        "app_id": "com.example.app",
        "ios_input_backend": "simulator_hid",
        "simulator_hid_driver": "/tmp/driver",
    }
    with pytest.raises(ValueError, match="Simulator"):
        validate_profile(value)
    assert (
        validate_profile({**value, "simulator": True})["ios_input_backend"]
        == "simulator_hid"
    )
    with pytest.raises(ValueError, match="Simulator"):
        validate_profile({**value, "simulator_orientation": "portrait"})
    assert (
        validate_profile(
            {**value, "simulator": True, "simulator_orientation": "portrait"}
        )["simulator_orientation"]
        == "portrait"
    )


def test_native_h264_generation_recovers_with_keyframe_without_host_decode():
    class Channel:
        def __init__(self):
            self.calls, self.closed = [], False

        def request(self, op, args, **kwargs):
            self.calls.append(args)
            return {
                "binary": b"\x00\x00\x01\x65payload",
                "codec": "h264-annexb",
                "no_b_frames": True,
                "width": 96,
                "height": 64,
                "generation": "new",
                "key_frame": len(self.calls) > 1,
            }

        def close(self):
            self.closed = True

    channel = Channel()
    receiver = EncodedPacketSource(channel)
    frame = receiver.frame()
    assert frame["host_decode"] is False and len(channel.calls) == 2
    assert all(c["request_keyframe"] for c in channel.calls)
    receiver.close()
    assert channel.closed


def test_native_h264_recovery_budget_closes_owned_channel():
    class Channel:
        closed = False

        def request(self, *args, **kwargs):
            return {
                "binary": b"\x00\x00\x01\x41payload",
                "codec": "h264-annexb",
                "no_b_frames": True,
                "width": 96,
                "height": 64,
                "generation": "new",
                "key_frame": False,
            }

        def close(self):
            self.closed = True

    channel = Channel()
    with pytest.raises(ProtocolError, match="recovery"):
        EncodedPacketSource(channel).frame()
    assert channel.closed
