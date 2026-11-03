import copy

import pytest

from iatest.mobile.devices import NativeDevice
from iatest.mobile.ime import validate_ime


def sequence():
    return {
        "ime_backend": "input_connection",
        "ime_events": [
            {"op": "compose", "text": "ni", "candidates": ["你", "呢"]},
            {"op": "expect", "composing": "ni"},
            {"op": "candidate", "index": 0},
            {"op": "expect", "before_cursor": "你", "composing": ""},
        ],
    }


def test_ime_contract_keeps_platforms_explicit_and_requires_native_conditions():
    value = sequence()
    validate_ime(value, "android")
    for changed in [
        {**value, "ime_backend": "keyboard_ui"},
        {**value, "ime_events": value["ime_events"][:-1]},
        {**value, "ime_events": [{"op": "compose", "text": "x"}, {"op": "expect"}]},
    ]:
        with pytest.raises(ValueError):
            validate_ime(changed, "android")
    ios = {
        "ime_backend": "keyboard_ui",
        "ime_events": [
            {"op": "compose", "text": "ni"},
            {"op": "candidate", "text": "你"},
            {"op": "expect", "value": "你"},
        ],
    }
    validate_ime(ios, "ios")
    for condition in ({"marked": True}, {"composing": "ni"}, {"before_cursor": "你"}):
        bad = copy.deepcopy(ios)
        bad["ime_events"][-1] = {"op": "expect", **condition}
        with pytest.raises(ValueError):
            validate_ime(bad, "ios")


@pytest.mark.parametrize(
    "current,restore",
    [
        ("dev.iatest.mobile/.IATestIme", True),
        ("dev.iatest.mobile/dev.iatest.mobile.IATestIme", True),
        ("external.keyboard/.Ime", False),
    ],
)
def test_ime_restore_only_changes_framework_owned_default(current, restore):
    device = NativeDevice(
        {
            "id": "p",
            "platform": "android",
            "device_id": "synthetic",
            "app_id": "com.example.app",
        },
        "fixture",
    )
    device.ime_previous = {"default": "original.keyboard/.Ime", "enabled": False}
    calls = []

    def adb(*args, **kwargs):
        calls.append(args)
        return current if "get" in args else ""

    device.adb = adb
    device.close()
    assert (("shell", "ime", "set", "original.keyboard/.Ime") in calls) == restore
    assert ("shell", "ime", "disable", "dev.iatest.mobile/.IATestIme") in calls
    assert device.ime_previous is None
