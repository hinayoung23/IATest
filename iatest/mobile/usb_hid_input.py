"""Observed HID target clicks and Chinese candidate selection, with no XCTest input."""

from __future__ import annotations

import math
import re
import time

from iatest.mobile.protocol import ProtocolError


def pinyin_keys(text, explicit=None):
    if explicit is not None:
        value = explicit
    else:
        try:
            from pypinyin import Style, lazy_pinyin
        except ImportError as exc:
            raise ValueError("Install iatest-framework[hid] or supply explicit pinyin") from exc
        value = "".join(lazy_pinyin(text, style=Style.NORMAL, errors=lambda _: ["?"]))
        value = value.replace("ü", "v")
    if not re.fullmatch(r"[a-zv' ]{1,256}", value):
        raise ValueError("Chinese HID input requires a supported Pinyin phrase or explicit keys")
    return value


def observed_input(session, channel, step, max_queries, max_actions):
    command = step["hid_command"]
    calibration = channel.settings.get("pointer_calibration")
    if "absolute_mouse" not in channel.capabilities or calibration is None:
        raise ValueError("Observed HID input requires an absolute endpoint and explicit pointer calibration")
    if not session.device.semantic.capabilities.get("hid_geometry_validation"):
        raise ValueError("Rebuild the Runner for read-only HID geometry validation")
    ime = command["op"] == "ime_text"
    click_focus = ime and command.get("focus") == "target_click"
    space_commit = ime and command.get("candidate_commit") == "space"
    target_commit = ime and command.get("candidate_commit") == "target_click"
    if target_commit and not command.get("candidate_target", {}).get("identifier"):
        raise ValueError("HID candidate target_click requires an explicit candidate identifier")
    if ime and not space_commit and not target_commit and not session.device.semantic.capabilities.get("hid_keyboard_candidate_validation"):
        raise ValueError("Rebuild the Runner for keyboard candidate validation")
    actions = (2 + int(click_focus)) if ime else 1
    required_queries = (5 + 2 * int(click_focus) - int(space_commit)) if ime else 2
    if max_actions < actions or max_queries < required_queries:
        raise ValueError("Observed HID input exceeds remaining action/query budget")
    keys = pinyin_keys(command["text"], command.get("pinyin")) if ime else None
    control = session.device.control
    deadline = time.monotonic() + min(15, control.remaining() or 15)
    queries = 0

    def read(target, expected=None):
        nonlocal queries
        while True:
            control.check()
            left = deadline - time.monotonic()
            if left <= 0 or queries >= max_queries:
                raise ProtocolError("HID read-only observation time/query budget exhausted")
            queries += 1
            observed = session.query(target, cache=False, timeout=min(5, left))
            if observed.get("complete") and not observed.get("match_count_is_lower_bound"):
                if observed.get("match_count", 0) > 1:
                    raise ProtocolError("HID native target is ambiguous")
                if observed.get("match_count") == 1:
                    node = session.unique(observed)
                    if node.get("secure") or not node.get("enabled", True):
                        raise ProtocolError("HID observation requires an enabled non-secure target")
                    if expected is None or node.get("value") == expected and "value" not in node.get("truncated_attributes", []) and "value" not in node.get("ambiguous_attributes", []):
                        return observed, node
            control.wait(min(0.02, max(0.001, deadline - time.monotonic())))

    def point(observed, node, *, keyboard_candidate=False):
        nonlocal queries
        if queries >= max_queries:
            raise ProtocolError("HID geometry query budget exhausted")
        started = time.monotonic()
        queries += 1
        geometry = session.device.semantic.request("hid_validate", {"ref": node["ref"], "state_version": observed.get("state_version"), "keyboard_candidate": keyboard_candidate}, timeout=min(5, max(0.001, deadline - started)))
        if keyboard_candidate and geometry.get("keyboard_candidate_verified") is not True:
            raise ProtocolError("HID candidate is not in the current keyboard")
        if time.monotonic() - started > 2 or geometry.get("bounds") != node.get("bounds"):
            raise ProtocolError("HID target geometry changed or became stale")
        if (geometry.get("width"), geometry.get("height"), geometry.get("orientation")) != (calibration["width"], calibration["height"], calibration["rotation"]):
            raise ProtocolError("HID calibration does not match current screen geometry")
        left, top, right, bottom = geometry["bounds"]
        width, height = calibration["width"], calibration["height"]
        if any(not isinstance(v, (float, int)) or not math.isfinite(v) for v in (left, top, right, bottom)) or not (0 <= left < right <= width and 0 <= top < bottom <= height):
            raise ProtocolError("HID target is outside the calibrated screen")
        return {"op": "point", "x": round((left + right) * 32767 / (2 * width)), "y": round((top + bottom) * 32767 / (2 * height)), "button": command.get("button", "left")}

    observed, node = read(step["target"])
    if not ime:
        value = channel.execute(point(observed, node))
        return {**value, "queries": queries, "actions": 1, "target_geometry_validated": True}
    if not node.get("editable"):
        raise ProtocolError("HID Chinese composition requires a non-secure editor")
    if click_focus:
        channel.execute(point(observed, node))
        observed, node = read(step["target"])
        if not node.get("editable"):
            raise ProtocolError("HID Chinese editor changed after the explicit focus click")
    elif not node.get("focused"):
        raise ProtocolError("HID Chinese composition requires the explicitly focused editor")
    point(observed, node)  # Validate calibration before the first key is sent.
    first = channel.execute({"op": "text", "text": keys})
    candidate, selected = read(command.get("candidate_target", {"label": command["text"]}))
    candidate_values = {selected.get("label"), selected.get("text")}
    if target_commit:
        candidate_values.add(selected.get("identifier"))
    if command["text"] not in candidate_values:
        raise ProtocolError("Observed candidate does not equal the requested Chinese phrase")
    committed = channel.execute({"op": "key", "usage": 44}) if space_commit else channel.execute(point(candidate, selected, keyboard_candidate=not target_commit))
    read(step["target"], command["expected_value"])
    mode = "android_bluetooth_hid" if first.get("backend") == "android_bluetooth_hid" else "usb_hid"
    return {**committed, "started_timestamp_ms": first["started_timestamp_ms"], "queries": queries, "actions": actions, "postcondition_verified": True, "focus_method": "target_click" if click_focus else "observed", "candidate_commit": "space" if space_commit else "target_click" if target_commit else "pointer", "candidate_scope": "explicit_target" if target_commit else "observed_unique" if space_commit else "current_keyboard", "input_mode": mode + "_pinyin_observed_candidate" + ("_space" if space_commit else "_target" if target_commit else "")}
