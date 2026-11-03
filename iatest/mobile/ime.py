"""Explicit real IME operations, bounded and distinct from semantic setText."""

from __future__ import annotations

import json

EVENTS = {
    "type": "array",
    "minItems": 2,
    "maxItems": 24,
    "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["op"],
        "properties": {
            "op": {"enum": ["compose", "candidate", "commit", "cancel", "expect"]},
            "text": {"type": "string", "maxLength": 256},
            "candidates": {
                "type": "array",
                "maxItems": 16,
                "items": {"type": "string", "minLength": 1, "maxLength": 256},
            },
            "index": {"type": "integer", "minimum": 0, "maximum": 15},
            "key": {"type": "string", "minLength": 1, "maxLength": 80},
            "composing": {"type": "string", "maxLength": 256},
            "before_cursor": {"type": "string", "maxLength": 256},
            "value": {"type": "string", "maxLength": 256},
            "marked": {"type": "boolean"},
        },
    },
}


def validate_ime(step, platform):
    from iatest.core.schema import schema_errors

    events = step.get("ime_events", [])
    if errors := schema_errors(events, EVENTS):
        raise ValueError("; ".join(errors))
    if len(json.dumps(events, ensure_ascii=False).encode()) > 40000:
        raise ValueError("IME sequence exceeds wire budget")
    backend = step.get("ime_backend")
    if backend != ("input_connection" if platform == "android" else "keyboard_ui"):
        raise ValueError("Select the explicit platform IME backend")
    if events[-1]["op"] != "expect":
        raise ValueError("IME sequence must end with an explicit expectation")
    for event in events:
        op = event["op"]
        if op == "compose" and not event.get("text"):
            raise ValueError("IME compose requires text")
        if op == "candidate" and (
            "index" not in event if platform == "android" else not event.get("text")
        ):
            raise ValueError(
                "IME candidate requires index on Android or visible text on iOS"
            )
        if op == "expect" and not (
            {"composing", "before_cursor", "marked", "value"} & set(event)
        ):
            raise ValueError("IME expectation needs an observed condition")
        if platform == "ios":
            if "marked" in event or "composing" in event or "before_cursor" in event:
                raise ValueError(
                    "iOS keyboard UI cannot prove UITextInput marked ranges; use explicit native value/read-only App evidence"
                )
            if op in {"commit", "cancel"} and not event.get("key"):
                raise ValueError(
                    "iOS commit/cancel requires an explicit visible keyboard key"
                )
        elif "value" in event:
            raise ValueError(
                "Android IME uses before_cursor/composing/marked; full values require a native UI expectation"
            )
