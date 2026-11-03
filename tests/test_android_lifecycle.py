from types import SimpleNamespace

import pytest

from iatest.mobile.devices import NativeDevice
from iatest.mobile.protocol import CommandRejected, DeliveryUnknown
from iatest.mobile.runtime import MobileSession


def device():
    return NativeDevice({"id": "phone", "platform": "android", "device_id": "fixture", "app_id": "dev.example.app", "display_id": 2}, "session")


def test_launch_is_bound_to_app_display_and_operation_ledger():
    phone = device()
    calls = []

    def adb(*args, **kwargs):
        calls.append(args)
        return "dev.example.app/.Main" if "resolve-activity" in args else "Status: ok\n"

    phone.adb = adb
    result = phone.android_lifecycle("launch", "one", 1)
    assert phone.android_lifecycle("launch", "one", 1) == result
    assert len(calls) == 2
    assert calls[-1] == ("shell", "am", "start", "-W", "--display", "2", "-n", "dev.example.app/.Main")
    assert result["postcondition_verified"] is False
    with pytest.raises(ValueError, match="another action"):
        phone.android_lifecycle("terminate", "one", 1)


@pytest.mark.parametrize("component", ["", "other.app/.Main", "dev.example.app/.Main;whoami"])
def test_launch_rejects_unowned_or_malformed_resolution(component):
    phone = device()
    calls = []
    phone.adb = lambda *args, **kwargs: calls.append(args) or component
    with pytest.raises(ValueError, match="owned launch activity"):
        phone.android_lifecycle("launch", "one", 1)
    assert len(calls) == 1 and not phone.lifecycle_actions


def test_uncertain_termination_is_never_replayed():
    phone = device()
    calls = []
    uncertain = []
    phone.on_unknown = lambda: uncertain.append(True)

    def adb(*args, **kwargs):
        calls.append(args)
        raise TimeoutError("Disconnected after dispatch")

    phone.adb = adb
    with pytest.raises(DeliveryUnknown):
        phone.android_lifecycle("terminate", "one", 1)
    assert phone.android_lifecycle("terminate", "one", 1)["status"] == "unknown"
    assert len(calls) == len(uncertain) == 1


def test_launch_wait_retries_only_read_only_focus_observation():
    phone = device()
    calls = []
    phone.adb = lambda *args, **kwargs: calls.append(args) or ("dev.example.app/.Main" if "resolve-activity" in args else "Status: ok")
    observations = []

    def request(op, args, **kwargs):
        observations.append(op)
        if len(observations) == 1:
            raise CommandRejected("App is not focused on selected display")
        return {"app_id": "dev.example.app"}

    phone.semantic = SimpleNamespace(request=request, capabilities={})
    phone.input = SimpleNamespace()
    session = object.__new__(MobileSession)
    session.profile, session.device, session.closed = phone.profile, phone, False
    result = session.action({"op": "launch"}, None, "one", 1)
    assert result["accepted"] and observations == ["state", "state"]
    assert len(calls) == 2


@pytest.mark.parametrize("user,expected", [("0", False), ("10", True)])
def test_permission_read_is_scoped_to_current_user(user, expected):
    phone = device()
    phone.profile["allowed_permissions"] = ["android.permission.CAMERA"]
    output = """Packages:
  Package [dev.example.app]:
    User 0: installed=true
      runtime permissions:
        android.permission.CAMERA: granted=false, flags=[]
    User 10: installed=true
      runtime permissions:
        android.permission.CAMERA: granted=true, flags=[]
"""
    phone.adb = lambda *args: user if args[-1] == "get-current-user" else output
    assert phone.android_permission_state("dev.example.app", "android.permission.CAMERA") is expected
    with pytest.raises(ValueError, match="exceeds"):
        phone.android_permission_state("other.app", "android.permission.CAMERA")


def test_missing_permission_record_cannot_be_treated_as_denied():
    phone = device()
    phone.profile["allowed_permissions"] = ["android.permission.CAMERA"]
    phone.adb = lambda *args: "0" if args[-1] == "get-current-user" else "    User 0: installed=true\n"
    with pytest.raises(ValueError, match="cannot be verified"):
        phone.android_permission_state("dev.example.app", "android.permission.CAMERA")


def test_launch_does_not_use_transitional_or_offscreen_geometry():
    phone = device()
    calls = []
    phone.adb = lambda *args, **kwargs: calls.append(args) or ("dev.example.app/.Main" if "resolve-activity" in args else "Status: ok")
    states = iter([[-20, 0, 80, 100], [0, 0, 100, 100], [0, 0, 100, 100]])
    observed = []

    def request(op, args, **kwargs):
        if op == "reset_observation":
            return {"cleared": True}
        bounds = next(states)
        observed.append(bounds)
        return {"root_bounds": bounds, "display": {"width": 100, "height": 100}}

    phone.semantic = SimpleNamespace(request=request, capabilities={"actions": ["reset_observation"], "lifecycle_geometry_checks": True})
    phone.input = SimpleNamespace()
    session = object.__new__(MobileSession)
    session.profile, session.device, session.closed = phone.profile, phone, False
    assert session.action({"op": "launch"}, None, "one", 1)["accepted"]
    assert len(observed) == 3 and len(calls) == 2


def test_changed_pointer_target_is_rejected_before_any_input():
    from iatest.mobile.protocol import ObservationRejected

    phone = device()
    dispatched = []
    def validate(op, arguments, **kwargs):
        assert op == "pointer_validate"
        raise ObservationRejected("Window transformed before dispatch")
    phone.semantic = SimpleNamespace(request=validate, capabilities={"pointer_target_validation": True})
    phone.input = SimpleNamespace(request=lambda *a, **k: dispatched.append(a))
    session = object.__new__(MobileSession)
    session.profile, session.device, session.closed = phone.profile, phone, False
    observation = {"complete": True, "match_count": 1, "state_version": 1,
                   "display": {"id": 0, "width": 100, "height": 100, "rotation": 0},
                   "candidates": [{"ref": "save", "bounds": [10, 10, 30, 30]}]}
    with pytest.raises(ObservationRejected):
        session.action({"op": "tap"}, observation, "never-dispatch", 1)
    assert dispatched == []


def test_app_switch_waits_for_new_focus_without_replaying_launch():
    from iatest.execution.control import RunControl
    from iatest.mobile.system import MobileSystem

    original = device()
    original.profile["allowed_apps"] = ["dev.example.companion"]
    original.control = RunControl(timeout_seconds=5)
    original.on_unknown = lambda: None
    original.close = lambda: setattr(original, "control", None)
    calls, observations = [], []

    def factory(profile, _session):
        target = NativeDevice(profile, "switched")
        target.open = lambda: None
        target.close = lambda: None
        target.adb = lambda *args, **kwargs: calls.append(args) or (
            "dev.example.companion/.Main" if "resolve-activity" in args else "Status: ok"
        )

        def query(op, args, **kwargs):
            observations.append(op)
            if len(observations) == 1:
                raise CommandRejected("App is not focused on selected display")
            return {"app_id": profile["app_id"]}

        target.semantic = SimpleNamespace(request=query, capabilities={})
        target.input = SimpleNamespace()
        return target

    session = object.__new__(MobileSession)
    session.device, session.profile, session.closed = original, original.profile, False
    session.id, session.media_receiver = "session", None
    session.device_factory, session.last_observation = factory, {"old": True}
    result = MobileSystem(session, None, "suite").execute(
        {"op": "switch_app", "app_id": "dev.example.companion"}
    )
    assert result["driver_rebound"] and result["postcondition_verified"] is False
    assert observations == ["state", "state"]
    assert len(calls) == 2 and calls[-1][-1] == "dev.example.companion/.Main"
    assert session.last_observation is None
