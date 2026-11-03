"""Observed-control boundary tests; no hardware or iPhone delivery claim."""

from types import SimpleNamespace

import pytest

from iatest.execution.control import RunControl
from iatest.mobile.protocol import ProtocolError
from iatest.mobile.usb_hid_input import observed_input, pinyin_keys


class ObservedUI:
    def __init__(self):
        self.value, self.selected = "", "中文"
        self.secure, self.focused, self.keyboard, self.ambiguous = False, True, True, False
        self.rotation, self.committed = 1, "中文"
        self.candidate_identifier = "中文"
        self.nodes = {}
        self.device = SimpleNamespace(
            control=RunControl(timeout_seconds=3),
            semantic=SimpleNamespace(
                capabilities={"hid_geometry_validation": True, "hid_keyboard_candidate_validation": True},
                request=self.geometry,
            ),
        )

    def query(self, target, **options):
        assert options["cache"] is False
        candidate = "label" in target or target.get("identifier") == "中文"
        node = {
            "ref": "candidate" if candidate else "editor",
            "bounds": [10, 20, 110, 60], "enabled": True,
            "secure": self.secure, "editable": not candidate, "focused": self.focused,
            "label": self.selected if candidate else "Nickname",
            "identifier": self.candidate_identifier if candidate else "nickname",
            "value": self.value,
            "ambiguous_attributes": ["value"] if self.ambiguous and not candidate else [],
        }
        self.nodes[node["ref"]] = node
        return {"complete": True, "match_count": 1, "state_version": "fresh", "candidates": [node]}

    @staticmethod
    def unique(observed):
        return observed["candidates"][0]

    def geometry(self, op, args, **options):
        assert op == "hid_validate"  # No XCTest input operation is used.
        return {"bounds": self.nodes[args["ref"]]["bounds"], "width": 400, "height": 800,
                "orientation": self.rotation, "keyboard_candidate_verified": self.keyboard and args["keyboard_candidate"]}


class HID:
    def __init__(self, ui):
        self.ui, self.commands = ui, []
        self.capabilities = {"keyboard", "relative_mouse", "absolute_mouse"}
        self.settings = {"pointer_calibration": {"width": 400, "height": 800, "rotation": 1}}

    def execute(self, command):
        self.commands.append(command)
        if command["op"] == "point":
            self.ui.value = self.ui.committed
        return {"started_timestamp_ms": 100, "postcondition_verified": False}


def step():
    return {"target": {"identifier": "nickname"}, "hid_command": {
        "op": "ime_text", "text": "中文", "pinyin": "zhongwen", "expected_value": "中文",
    }}


def test_actual_optional_converter_and_explicit_keys():
    pytest.importorskip("pypinyin")
    assert pinyin_keys("中文") == "zhongwen"
    assert pinyin_keys("重庆") == "chongqing"
    assert pinyin_keys("女") == "nv"
    with pytest.raises(ValueError, match="supported"):
        pinyin_keys("🙂")
    assert pinyin_keys("中文", "zhong wen") == "zhong wen"


def test_explicit_candidate_target_can_use_annotated_cell_without_keyboard_claim():
    ui = ObservedUI()
    ui.keyboard = False
    ui.selected = "中文, 中国的中，文化的文"
    del ui.device.semantic.capabilities["hid_keyboard_candidate_validation"]
    channel = HID(ui)
    value = step()
    value["hid_command"].update(candidate_commit="target_click", candidate_target={"type": "cell", "identifier": "中文"})
    result = observed_input(ui, channel, value, 5, 2)
    assert result["candidate_scope"] == "explicit_target"
    assert result["candidate_commit"] == "target_click" and result["postcondition_verified"]
    assert [command["op"] for command in channel.commands] == ["text", "point"]


@pytest.mark.parametrize("fault", ["missing_target", "wrong_candidate", "wrong_final_value"])
def test_explicit_candidate_click_requires_identity_and_final_value(fault):
    ui = ObservedUI()
    channel = HID(ui)
    value = step()
    value["hid_command"].update(candidate_commit="target_click", candidate_target={"identifier": "中文"})
    if fault == "missing_target":
        del value["hid_command"]["candidate_target"]
    elif fault == "wrong_candidate":
        ui.candidate_identifier, ui.selected = "错误", "错误"
    else:
        ui.committed = "错误"
    with pytest.raises((ValueError, ProtocolError)):
        observed_input(ui, channel, value, 5, 2)
    assert len(channel.commands) == {"missing_target": 0, "wrong_candidate": 1, "wrong_final_value": 2}[fault]


@pytest.mark.parametrize("backend", ["external_usb_hid", "android_bluetooth_hid"])
def test_chinese_input_selects_observed_keyboard_candidate_and_checks_final_value(backend):
    ui = ObservedUI()
    channel = HID(ui)
    execute = channel.execute
    channel.execute = lambda command: {**execute(command), "backend": backend}
    value = observed_input(ui, channel, step(), 5, 2)
    assert value["postcondition_verified"] and value["queries"] == 5
    assert value["actions"] == 2
    assert value["input_mode"] == ("usb_hid" if backend == "external_usb_hid" else backend) + "_pinyin_observed_candidate"
    assert channel.commands == [{"op": "text", "text": "zhongwen"}, {"op": "point", "x": 4915, "y": 1638, "button": "left"}]


@pytest.mark.parametrize("mutation,expected_commands", [
    ("calibration", 0), ("focus", 0), ("secure", 0),
    ("candidate", 1), ("outside_keyboard", 1), ("wrong_value", 2), ("placeholder", 2),
])
def test_missing_or_wrong_evidence_never_claims_input_success(mutation, expected_commands):
    ui = ObservedUI()
    channel = HID(ui)
    if mutation == "calibration":
        ui.rotation = 2
    elif mutation == "focus":
        ui.focused = False
    elif mutation == "secure":
        ui.secure = True
    elif mutation == "candidate":
        ui.selected = "错字"
    elif mutation == "outside_keyboard":
        ui.keyboard = False
    elif mutation == "placeholder":
        ui.ambiguous = True
    else:
        ui.committed = "错字"
    with pytest.raises(ProtocolError):
        observed_input(ui, channel, step(), 5, 2)
    assert len(channel.commands) == expected_commands


def test_missing_capability_and_budgets_fail_before_typing():
    ui = ObservedUI()
    channel = HID(ui)
    with pytest.raises(ValueError, match="budget"):
        observed_input(ui, channel, step(), 4, 2)
    with pytest.raises(ValueError, match="budget"):
        observed_input(ui, channel, step(), 5, 1)
    channel.capabilities.remove("absolute_mouse")
    with pytest.raises(ValueError, match="endpoint"):
        observed_input(ui, channel, step(), 5, 2)
    assert not channel.commands


def test_explicit_hid_focus_click_handles_an_editor_without_focus_observation():
    ui = ObservedUI()
    ui.focused = False
    channel = HID(ui)
    request = step()
    request["hid_command"]["focus"] = "target_click"
    for queries, actions in [(6, 3), (7, 2)]:
        with pytest.raises(ValueError, match="budget"):
            observed_input(ui, channel, request, queries, actions)
    assert not channel.commands
    result = observed_input(ui, channel, request, 7, 3)
    assert result["postcondition_verified"]
    assert result["actions"] == 3 and result["queries"] == 7
    assert result["focus_method"] == "target_click"
    assert [command["op"] for command in channel.commands] == ["point", "text", "point"]


@pytest.mark.parametrize("committed", ["中文", "错字"])
def test_observed_floating_candidate_can_commit_with_space_and_must_match_editor(committed):
    ui = ObservedUI()
    ui.focused = False
    ui.keyboard = False  # Floating candidates are outside the software keyboard.
    ui.committed = committed

    class KeyboardHID(HID):
        def execute(self, command):
            self.commands.append(command)
            if command["op"] == "text":
                ui.value = command["text"]
            elif command == {"op": "key", "usage": 44}:
                ui.value = ui.committed
            return {"started_timestamp_ms": 100, "backend": "android_bluetooth_hid"}

    channel = KeyboardHID(ui)
    request = step()
    request["hid_command"].update(focus="target_click", candidate_commit="space")
    if committed == "中文":
        result = observed_input(ui, channel, request, 6, 3)
        assert result["postcondition_verified"] and result["candidate_commit"] == "space"
        assert result["queries"] == 6 and result["actions"] == 3
    else:
        with pytest.raises(ProtocolError):
            observed_input(ui, channel, request, 6, 3)
    assert [value["op"] for value in channel.commands] == ["point", "text", "key"]
    assert channel.commands[-1] == {"op": "key", "usage": 44}
