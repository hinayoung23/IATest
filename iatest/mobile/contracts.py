from __future__ import annotations

import json
import math

from iatest.core.schema import schema_errors
from iatest.engines.contracts import ExecutionPlan
from iatest.mobile.gestures import CONTACTS, validate_contacts
from iatest.mobile.ime import EVENTS, validate_ime
from iatest.mobile.visual import ANCHOR_SCHEMA, CROP_SCHEMA, validate_crop

TARGET_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "minProperties": 1,
    "properties": {
        "bounds": {
            "type": "array",
            "minItems": 4,
            "maxItems": 4,
            "items": {"type": "number", "minimum": -32768, "maximum": 32768},
        },
        **{
            k: {"type": "string", "maxLength": 500}
            for k in ("identifier", "text", "label", "type", "container_identifier")
        },
        "anchor": {
            "type": "object",
            "additionalProperties": False,
            "required": ["identifier", "text"],
            "properties": {
                "identifier": {"type": "string", "maxLength": 500},
                "text": {"type": "string", "maxLength": 500},
            },
        },
        "require_unique": {"type": "boolean"},
    },
    "anyOf": [{"required": [k]} for k in ("identifier", "text", "label", "type", "bounds")],
}

STEP_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["op"],
    "properties": {
        "visual_crop": CROP_SCHEMA,
        "tap_backend": {"enum": ["pointer", "accessibility"]},
        "op": {
            "enum": [
                "find",
                "tap",
                "swipe",
                "key",
                "text",
                "expect",
                "wait",
                "screenshot",
                "launch",
                "terminate",
                "pinch",
                "visual_click",
                "gesture",
                "ime",
                "switch_app",
                "permission",
                "clipboard_set",
                "clipboard_expect",
                "web",
                "input_state_expect",
                "hid",
            ]
        },
        "target": TARGET_SCHEMA,
        "app_id": {"type": "string", "maxLength": 200},
        "permission": {"type": "string", "pattern": "^android\\.permission\\.[A-Z_]+$"},
        "granted": {"type": "boolean"},
        "web_step": {"type": "object"},
        "hid_command": {"type": "object"},
        "bridge_artifact_id": {"type": "string", "maxLength": 80},
        "input_identifier": {"type": "string", "minLength": 1, "maxLength": 80},
        "marked": {"type": "boolean"},
        "marked_length": {"type": "integer", "minimum": 0, "maximum": 16384},
        "ref": {"type": "string", "maxLength": 80},
        "bind": {"type": "string", "maxLength": 80},
        "value": {"type": "string", "maxLength": 16384},
        "mode": {"enum": ["semantic", "key_events", "keyboard"]},
        "attribute": {
            "enum": ["text", "label", "value", "exists", "enabled", "selected"]
        },
        "operator": {"enum": ["eq", "ne", "contains"]},
        "expected": {"type": ["string", "boolean", "number", "null"]},
        "keycode": {"type": "integer", "minimum": 0, "maximum": 300},
        "direction": {"enum": ["up", "down", "left", "right"]},
        "duration_ms": {"type": "integer", "minimum": 50, "maximum": 2000},
        "scale": {"type": "number", "minimum": 0.1, "maximum": 5},
        "velocity": {"type": "number", "minimum": 0.1, "maximum": 5},
        "visual_anchor": ANCHOR_SCHEMA,
        "contacts": CONTACTS,
        "ime_events": EVENTS,
        "ime_backend": {"enum": ["input_connection", "keyboard_ui"]},
        "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 60000},
    },
}
FRAGMENT_STEP_SCHEMA = {
    **STEP_SCHEMA,
    "properties": {
        k: v
        for k, v in STEP_SCHEMA["properties"].items()
        if k in {"op", "target", "value", "mode", "attribute", "expected"}
    },
}
FRAGMENT_STEP_SCHEMA["properties"]["op"] = {"enum": ["tap", "text", "expect"]}
STEP_SCHEMA["properties"]["op"]["enum"].append("fragment")
STEP_SCHEMA["properties"]["steps"] = {
    "type": "array",
    "minItems": 2,
    "maxItems": 8,
    "items": FRAGMENT_STEP_SCHEMA,
}

SPEC_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["profile", "steps"],
    "properties": {
        "profile": {"type": "string", "pattern": "^[a-zA-Z0-9_.-]{1,80}$"},
        "steps": {
            "type": "array",
            "minItems": 1,
            "maxItems": 1000,
            "items": STEP_SCHEMA,
        },
        "cache": {"type": "boolean"},
        "webview": {
            "type": "object",
            "additionalProperties": False,
            "required": ["allowed_origins"],
            "properties": {
                "allowed_origins": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 32,
                    "items": {"type": "string"},
                },
                "page_id": {"type": "string", "maxLength": 200},
                "inspector_url": {"type": "string", "maxLength": 2048},
                "native_target": TARGET_SCHEMA,
                "pages": {
                    "type": "object", "maxProperties": 16,
                    "propertyNames": {"pattern": "^[A-Za-z0-9_-]{1,60}$"},
                    "additionalProperties": {
                        "type": "object", "additionalProperties": False,
                        "required": ["native_target"],
                        "anyOf": [{"required": ["page_id"]}, {"required": ["url"]}],
                        "properties": {"page_id": {"type": "string", "minLength": 1, "maxLength": 200},
                                       "url": {"type": "string", "minLength": 1, "maxLength": 2048},
                                       "native_target": TARGET_SCHEMA},
                    },
                },
            },
        },
    },
}


def validate_mobile_plan(plan: ExecutionPlan) -> None:
    if plan.kind not in {"android", "ios"}:
        raise ValueError("A mobile plan must select android or ios")
    if plan.data_generation or plan.input_env:
        from iatest.engines.data import materialize

        plan, _, _ = materialize(plan, preview=True)
    if errors := schema_errors(plan.spec, SPEC_SCHEMA):
        raise ValueError("; ".join(errors))
    bound = set()
    has_expectation = False
    last_action = -1
    last_expectation = -1
    for index, step in enumerate(plan.spec["steps"]):
        op = step["op"]
        if op == "hid":
            from iatest.engines.http import validate
            from iatest.mobile.usb_hid import UI_COMMAND_SCHEMA

            if plan.kind != "ios" or "hid_command" not in step:
                raise ValueError("USB HID step requires an iOS keyboard/mouse command")
            validate(step["hid_command"], UI_COMMAND_SCHEMA)
            if step["hid_command"]["op"] in {"target_click", "ime_text"} and not step.get("target"):
                raise ValueError("Observed HID input requires an explicit native target")
            candidate = step["hid_command"].get("candidate_target")
            if candidate is not None:
                validate(candidate, TARGET_SCHEMA)
            last_action = index
        if "tap_backend" in step and (op != "tap" or plan.kind != "android"):
            raise ValueError("tap_backend is only valid for Android tap steps")
        for spatial_step in [step, *step.get("steps", [])]:
            rectangle = spatial_step.get("target", {}).get("bounds")
            if rectangle is not None:
                if not all(math.isfinite(v) for v in rectangle) or rectangle[2] <= rectangle[0] or rectangle[3] <= rectangle[1]:
                    raise ValueError("Spatial bounds require a non-empty target rectangle")
                if plan.kind == "android" and any(int(v) != v for v in rectangle):
                    raise ValueError("Android spatial bounds require integer pixels")
        if "visual_crop" in step:
            validate_crop(step["visual_crop"])
            if op != "visual_click":
                raise ValueError(
                    "visual_crop is only valid for pixel-verified visual_click"
                )
        if op == "input_state_expect" and (
            plan.kind != "ios"
            or not {"target", "bridge_artifact_id", "input_identifier"} <= step.keys()
            or not {"marked", "marked_length"} & step.keys()
        ):
            raise ValueError(
                "iOS input state requires an explicit native target, development artifact and observable condition"
            )
        if op in {"switch_app", "permission", "clipboard_set", "web"}:
            last_action = index
        if op == "switch_app" and not step.get("app_id"):
            raise ValueError("App transition requires an allowlisted app_id")
        if op == "permission" and not {"permission", "granted"} <= step.keys():
            raise ValueError("Permission operation requires its explicit desired state")
        if op == "clipboard_set" and "value" not in step:
            raise ValueError("Clipboard set requires a value")
        if op == "clipboard_expect":
            if not isinstance(step.get("expected"), str):
                raise ValueError("Clipboard expectation requires a string")
            has_expectation = True
            last_expectation = index
        if op == "web":
            from iatest.engines.http import validate
            from iatest.engines.web import WEB_SCHEMA, validate_target

            if not plan.spec.get("webview") or "web_step" not in step:
                raise ValueError("WebView step requires explicit context")
            validate(step["web_step"], WEB_SCHEMA["properties"]["steps"]["items"])
            nested = step["web_step"]
            if (
                nested["op"] in {"find", "click", "fill", "press", "expect", "wait"}
                and "target" not in nested
            ):
                raise ValueError("WebView operation requires an explicit target")
            if nested["op"] == "fill" and "value" not in nested:
                raise ValueError("WebView fill requires a value")
            if nested["op"] == "press" and "key" not in nested:
                raise ValueError("WebView press requires a key")
            if nested["op"] == "navigate" and "url" not in nested:
                raise ValueError("WebView navigation requires a URL")
            if nested["op"] not in {
                "find",
                "click",
                "fill",
                "press",
                "navigate",
                "reload",
                "expect",
                "wait",
                "switch_page",
                "expect_popup",
            }:
                raise ValueError("Unsupported hybrid WebView operation")
            if nested["op"] in {"switch_page", "expect_popup"}:
                if plan.kind != "ios" or not nested.get("page"):
                    raise ValueError("Explicit WKWebView page actions require iOS and a page alias")
                if nested["page"] != "main" and nested["page"] not in plan.spec["webview"].get("pages", {}):
                    raise ValueError("Declare each WKWebView page's native container")
                if nested["op"] == "expect_popup" and not nested.get("url"):
                    raise ValueError("WKWebView popup requires an explicit URL")
            if "target" in nested:
                validate_target(nested["target"])
            if nested["op"] in {"expect", "wait"}:
                if (
                    "assert" not in nested
                    or "expected" not in nested
                    and nested["assert"] not in {"exists", "absent"}
                ):
                    raise ValueError(
                        "WebView expectation requires an explicit predicate"
                    )
                has_expectation = True
                last_expectation = index
                last_action = index - 1
        if op == "fragment":
            fragment = step.get("steps", [])
            if len(json.dumps(fragment, ensure_ascii=False).encode()) > 48000:
                raise ValueError("Native fragment exceeds the 48,000-byte wire budget")
            if not fragment or fragment[-1]["op"] != "expect":
                raise ValueError(
                    "A native fragment must end in an explicit expectation"
                )
            for item in fragment:
                if not item.get("target"):
                    raise ValueError("Every fragment step requires a fresh target")
                if item["op"] == "text" and (
                    "value" not in item
                    or item.get("mode")
                    != ("semantic" if plan.kind == "android" else "keyboard")
                ):
                    raise ValueError(
                        "Fragment text requires a platform-supported explicit mode"
                    )
                if item["op"] == "text" and len(item["value"].encode("utf-8")) > 16384:
                    raise ValueError("Fragment text exceeds the 16 KiB UTF-8 limit")
                if item["op"] == "expect" and "expected" not in item:
                    raise ValueError("Fragment expectation requires expected")
            has_expectation, last_action, last_expectation = True, index - 1, index
        if op in {
            "tap",
            "text",
            "swipe",
            "pinch",
            "key",
            "launch",
            "terminate",
            "visual_click",
            "gesture",
            "ime",
        }:
            last_action = index
        if op in {"find", "expect", "wait"} and not step.get("target"):
            raise ValueError(f"{op} requires target")
        if op in {"tap", "text", "swipe", "pinch", "gesture", "ime"} and not (
            step.get("target") or step.get("ref") in bound
        ):
            raise ValueError(f"{op} requires target or a previously bound ref")
        if op == "pinch" and ("scale" not in step or step["scale"] == 1):
            raise ValueError("Pinch requires a scale different from 1")
        if op == "gesture":
            validate_contacts(step.get("contacts", []))
        if op == "ime":
            validate_ime(step, plan.kind)
        if op == "visual_click" and (
            "target" not in step or "visual_anchor" not in step
        ):
            raise ValueError(
                "Native visual_click requires a target and image-bound anchor"
            )
        if op == "find" and step.get("bind"):
            bound.add(step["bind"])
        if op == "text" and ("value" not in step or "mode" not in step):
            raise ValueError("text requires value and explicit input mode")
        if op == "text" and len(step["value"].encode("utf-8")) > 16384:
            raise ValueError("Text exceeds the 16 KiB UTF-8 limit")
        if op == "key" and "keycode" not in step:
            raise ValueError("key requires keycode")
        if op == "expect":
            last_expectation = index
            has_expectation = True
            if "expected" not in step:
                raise ValueError("expect requires expected")
    if not has_expectation:
        raise ValueError("A test plan requires at least one explicit expectation")
    if last_expectation <= last_action:
        raise ValueError("A test plan requires an expectation after its last action")
