import copy

import pytest

from iatest.engines.contracts import ExecutionPlan
from iatest.mobile.contracts import validate_mobile_plan
from iatest.mobile.gestures import (
    contact_position,
    preview_contacts,
    validate_contacts,
)


def contacts():
    return [
        {
            "id": 7,
            "points": [{"t_ms": 0, "x": 0, "y": 0}, {"t_ms": 100, "x": 1, "y": 1}],
        },
        {
            "id": 2,
            "points": [{"t_ms": 50, "x": 1, "y": 0}, {"t_ms": 150, "x": 0, "y": 1}],
        },
        {
            "id": 9,
            "points": [
                {"t_ms": 100, "x": 0, "y": 0.5},
                {"t_ms": 200, "x": 1, "y": 0.5},
            ],
        },
    ]


def test_contacts_have_independent_lifetimes_and_all_release():
    value = contacts()
    assert validate_contacts(value) == 200
    assert contact_position(value[0], 50) == (0.5, 0.5)
    events = preview_contacts(value)
    boundaries = [
        (e["t_ms"], e["action"], e.get("id")) for e in events if e["action"] != "move"
    ]
    assert boundaries == [
        (0, "down", 7),
        (50, "down", 2),
        (100, "up", 7),
        (100, "down", 9),
        (150, "up", 2),
        (200, "up", 9),
    ]
    active = set()
    for event in events:
        if event["action"] == "down":
            active.add(event["id"])
        elif event["action"] == "up":
            assert event["id"] in active
            active.remove(event["id"])
    assert not active


@pytest.mark.parametrize("bad", ["duplicate", "time", "point", "start", "overflow"])
def test_invalid_contact_paths_are_rejected_before_dispatch(bad):
    value = copy.deepcopy(contacts())
    if bad == "duplicate":
        value[1]["id"] = 7
    elif bad == "time":
        value[0]["points"][1]["t_ms"] = 0
    elif bad == "point":
        value[0]["points"][1]["x"] = 1.1
    elif bad == "start":
        value[0]["points"][0]["t_ms"] = 1
    else:
        value *= 4
    with pytest.raises(ValueError):
        validate_contacts(value)


def test_gesture_plan_requires_target_and_postcondition():
    step = {"op": "gesture", "target": {"identifier": "canvas"}, "contacts": contacts()}
    with pytest.raises(ValueError):
        validate_mobile_plan(
            ExecutionPlan("gesture", "android", {"profile": "phone", "steps": [step]})
        )
    validate_mobile_plan(
        ExecutionPlan(
            "gesture",
            "android",
            {
                "profile": "phone",
                "steps": [
                    step,
                    {
                        "op": "expect",
                        "target": {"identifier": "result"},
                        "expected": "done",
                    },
                ],
            },
        )
    )
