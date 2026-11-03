"""External USB HID bridge; input capabilities follow configured hardware endpoints."""

from __future__ import annotations

import json
import os
import time
import uuid
from urllib.parse import urlsplit

import httpx

from iatest.engines.http import validate
from iatest.execution.control import RunControl
from iatest.mobile.lease import DeviceLease
from iatest.mobile.protocol import DeliveryUnknown, ProtocolError

PROTOCOL = "iatest-usb-hid/1"
CONFIG_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["endpoint", "binding_id", "token_env"],
    "properties": {
        "endpoint": {"type": "string", "maxLength": 2048},
        "binding_id": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,80}$"},
        "token_env": {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]*$"},
        "pointer_calibration": {
            "type": "object", "additionalProperties": False,
            "required": ["width", "height", "rotation"],
            "properties": {
                "width": {"type": "number", "exclusiveMinimum": 0, "maximum": 10000},
                "height": {"type": "number", "exclusiveMinimum": 0, "maximum": 10000},
                "rotation": {"type": "integer", "minimum": 0, "maximum": 6},
            },
        },
    },
}
COMMAND_SCHEMA = {
    "oneOf": [
        {
            "type": "object", "additionalProperties": False,
            "required": ["op", "x", "y"],
            "properties": {
                "op": {"const": "point"},
                "x": {"type": "integer", "minimum": 0, "maximum": 32767},
                "y": {"type": "integer", "minimum": 0, "maximum": 32767},
                "button": {"enum": ["left", "right", "middle"]},
            },
        },
        {
            "type": "object", "additionalProperties": False,
            "required": ["op", "frames"],
            "properties": {
                "op": {"const": "touch"},
                "frames": {
                    "type": "array", "minItems": 2, "maxItems": 64,
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "required": ["t_ms", "contacts"],
                        "properties": {
                            "t_ms": {"type": "integer", "minimum": 0, "maximum": 5000},
                            "contacts": {
                                "type": "array", "maxItems": 10,
                                "items": {
                                    "type": "object", "additionalProperties": False,
                                    "required": ["id", "x", "y"],
                                    "properties": {
                                        "id": {"type": "integer", "minimum": 0, "maximum": 9},
                                        **{k: {"type": "integer", "minimum": 0, "maximum": 32767} for k in ("x", "y")},
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["op", "usage"],
            "properties": {
                "op": {"const": "key"},
                "usage": {"type": "integer", "minimum": 4, "maximum": 164},
                "modifiers": {"type": "integer", "minimum": 0, "maximum": 255},
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["op", "text"],
            "properties": {
                "op": {"const": "text"},
                "text": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                    "pattern": "^[ -~\\n\\t]+$",
                },
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["op", "dx", "dy"],
            "properties": {
                "op": {"const": "move"},
                **{
                    k: {"type": "integer", "minimum": -127, "maximum": 127}
                    for k in ("dx", "dy", "wheel")
                },
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["op", "button"],
            "properties": {
                "op": {"const": "click"},
                "button": {"enum": ["left", "right", "middle"]},
            },
        },
    ]
}

# These two commands need live read-only UI observations and cannot run standalone.
UI_COMMAND_SCHEMA = {"oneOf": [*COMMAND_SCHEMA["oneOf"], {
    "type": "object", "additionalProperties": False,
    "required": ["op"],
    "properties": {"op": {"const": "target_click"}, "button": {"enum": ["left", "right", "middle"]}},
}, {
    "type": "object", "additionalProperties": False,
    "required": ["op", "text", "expected_value"],
    "properties": {
        "op": {"const": "ime_text"},
        "text": {"type": "string", "minLength": 1, "maxLength": 32},
        "pinyin": {"type": "string", "pattern": "^[a-zv' ]{1,256}$"},
        "focus": {"enum": ["require_focused", "target_click"]},
        "candidate_commit": {"enum": ["pointer", "space", "target_click"]},
        "expected_value": {"type": "string", "maxLength": 512},
        "candidate_target": {"type": "object"},
    },
    "allOf": [{"if": {"properties": {"candidate_commit": {"const": "target_click"}}, "required": ["candidate_commit"]},
               "then": {"required": ["candidate_target"], "properties": {"candidate_target": {
                   "required": ["identifier"], "properties": {"identifier": {"type": "string", "minLength": 1, "maxLength": 512}},
               }}}}],
}]}


def validate_config(settings):
    validate(settings, CONFIG_SCHEMA)
    parsed = urlsplit(settings["endpoint"])
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or not parsed.port
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "HID bridge requires an explicit 127.0.0.1 HTTP port; use an owned SSH forward"
        )


def reports(command):
    """US layout ASCII and boot-HID reports. Every key/button is released."""
    validate(command, COMMAND_SCHEMA)
    kind = command["op"]
    if kind in {"point", "touch"}:
        from iatest.mobile.hid_reports import extension_schedule

        return [(name, data) for _, name, data in extension_schedule(command)]
    if kind == "key":
        return [
            (
                "keyboard",
                bytes(
                    [command.get("modifiers", 0), 0, command["usage"], 0, 0, 0, 0, 0]
                ),
            ),
            ("keyboard", bytes(8)),
        ]
    if kind == "move":
        return [
            (
                "mouse",
                bytes(
                    [
                        0,
                        command["dx"] & 255,
                        command["dy"] & 255,
                        command.get("wheel", 0) & 255,
                    ]
                ),
            )
        ]
    if kind == "click":
        return [
            (
                "mouse",
                bytes(
                    [{"left": 1, "right": 2, "middle": 4}[command["button"]], 0, 0, 0]
                ),
            ),
            ("mouse", bytes(4)),
        ]
    plain, shifted = "1234567890-= [];\\'`,./", '!@#$%^&*()_+ {}:|"~<>?'
    usages = [
        30,
        31,
        32,
        33,
        34,
        35,
        36,
        37,
        38,
        39,
        45,
        46,
        44,
        47,
        48,
        51,
        49,
        52,
        53,
        54,
        55,
        56,
    ]
    mapping = {c: (0, i + 4) for i, c in enumerate("abcdefghijklmnopqrstuvwxyz")}
    mapping.update({c: (2, i + 4) for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")})
    mapping.update({c: (0, key) for c, key in zip(plain, usages, strict=True)})
    mapping.update(
        {c: (2, key) for c, key in zip(shifted, usages, strict=True) if c != " "}
    )
    mapping.update({"\n": (0, 40), "\t": (0, 43)})
    result = []
    for char in command["text"]:
        modifier, usage = mapping[char]
        result.extend(
            [
                ("keyboard", bytes([modifier, 0, usage, 0, 0, 0, 0, 0])),
                ("keyboard", bytes(8)),
            ]
        )
    return result


def scheduled_reports(command):
    validate(command, COMMAND_SCHEMA)
    if command["op"] in {"point", "touch"}:
        from iatest.mobile.hid_reports import extension_schedule

        return extension_schedule(command)
    return [(index * 5, name, data) for index, (name, data) in enumerate(reports(command))]


class USBHIDSession:
    def __init__(self, settings, control=None):
        validate_config(settings)
        self.settings, self.control = (
            dict(settings),
            control or RunControl(timeout_seconds=60),
        )
        self.session = uuid.uuid4().hex
        self.sequence = 0
        self.epoch = ""
        self.failed = self.closed = False
        self.token = os.environ.get(settings["token_env"], "")
        if (
            not 32 <= len(self.token) <= 256
            or not self.token.isascii()
            or not self.token.isalnum()
        ):
            raise ValueError(
                "HID bridge requires an existing 32–256 character alphanumeric token env"
            )
        self.lease = DeviceLease(
            "usb-hid", settings["endpoint"] + ":" + settings["binding_id"]
        )
        self.lease.acquire()
        try:
            reply = self._request("hello", {})
            self.epoch = reply.get("epoch", "")
            capabilities = reply.get("capabilities")
            if not self.epoch or not isinstance(capabilities, list) or any(not isinstance(c, str) for c in capabilities) or not {"keyboard", "relative_mouse"} <= set(capabilities) <= {"keyboard", "relative_mouse", "absolute_mouse", "digitizer"}:
                raise ProtocolError("USB HID bridge capability handshake differs")
            self.capabilities = set(capabilities)
        except BaseException:
            self.lease.close()
            raise

    def _request(self, op, command, *, cleanup=False):
        if not cleanup:
            self.control.check()
        self.sequence += 1
        envelope = {
            "protocol": PROTOCOL,
            "binding_id": self.settings["binding_id"],
            "session": self.session,
            "epoch": self.epoch,
            "sequence": self.sequence,
            "operation_id": uuid.uuid4().hex,
            "op": op,
            "command": command,
            "deadline_ms": min(
                10000, max(1, int((self.control.remaining() or 10) * 1000))
            )
            if not cleanup
            else 1000,
        }
        uncertain = op == "execute"
        try:
            with (
                httpx.Client(
                    trust_env=False,
                    timeout=min(10, self.control.remaining() or 10)
                    if not cleanup
                    else 2,
                ) as client,
                client.stream(
                    "POST",
                    self.settings["endpoint"].rstrip("/") + "/command",
                    json=envelope,
                    headers={"Authorization": "Bearer " + self.token},
                ) as response,
            ):
                if response.status_code != 200:
                    raise ProtocolError("USB HID bridge rejected the control request")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 4096:
                        raise ProtocolError("HID response quota exceeded")
            value = json.loads(raw)
            if any(
                value.get(k) != envelope[k]
                for k in (
                    "protocol",
                    "binding_id",
                    "session",
                    "sequence",
                    "operation_id",
                )
            ) or (self.epoch and value.get("epoch") != self.epoch):
                raise ProtocolError("USB HID response binding differs")
            if value.get("status") != "acknowledged":
                raise ProtocolError("USB HID operation is unconfirmed")
            return value
        except BaseException as exc:
            if uncertain:
                self.failed = True
                raise DeliveryUnknown(
                    "USB HID delivery unconfirmed; never replay"
                ) from exc
            raise

    def execute(self, command):
        if self.failed or self.closed:
            raise ProtocolError("HID session is closed or needs manual reconciliation")
        schedule = scheduled_reports(command)
        required = {"mouse": "relative_mouse"}
        if any(required.get(name, name) not in self.capabilities for _, name, _ in schedule):
            raise ValueError("HID command requires an unconfigured hardware endpoint")
        if schedule[-1][0] / 1000 >= min(10, self.control.remaining() or 10):
            raise ValueError("HID path exceeds remaining command time")
        started = time.time() * 1000
        value = self._request("execute", command)
        return {
            "backend": "external_usb_hid",
            "dispatch": value["status"],
            "reports_written": value.get("reports_written"),
            "postcondition_verified": False,
            "started_timestamp_ms": started,
            "input_scope": "operator-bound USB cable; negotiated HID endpoints",
        }

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self._request("close", {}, cleanup=True)
        finally:
            self.lease.close()
