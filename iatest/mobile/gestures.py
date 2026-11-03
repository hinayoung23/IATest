"""Portable normalized contact trajectories; native drivers own dispatch/release."""

from __future__ import annotations

import json
from itertools import pairwise

POINT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["t_ms", "x", "y"],
    "properties": {
        "t_ms": {"type": "integer", "minimum": 0, "maximum": 5000},
        "x": {"type": "number", "minimum": 0, "maximum": 1},
        "y": {"type": "number", "minimum": 0, "maximum": 1},
    },
}
CONTACTS = {
    "type": "array",
    "minItems": 1,
    "maxItems": 10,
    "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "points"],
        "properties": {
            "id": {"type": "integer", "minimum": 0, "maximum": 31},
            "points": {"type": "array", "minItems": 2, "maxItems": 128, "items": POINT},
        },
    },
}


def validate_contacts(contacts):
    from iatest.core.schema import schema_errors

    if errors := schema_errors(contacts, CONTACTS):
        raise ValueError("; ".join(errors))
    if len({c["id"] for c in contacts}) != len(contacts):
        raise ValueError("Contact IDs must be unique")
    if (
        sum(len(c["points"]) for c in contacts) > 256
        or len(json.dumps(contacts).encode()) > 40000
    ):
        raise ValueError("Gesture exceeds point/wire budget")
    if min(c["points"][0]["t_ms"] for c in contacts) != 0:
        raise ValueError("The first contact must start at t=0")
    for c in contacts:
        times = [p["t_ms"] for p in c["points"]]
        if any(b <= a for a, b in pairwise(times)):
            raise ValueError("Contact timestamps must strictly increase")
    return max(c["points"][-1]["t_ms"] for c in contacts)


def contact_position(contact, t_ms):
    points = contact["points"]
    for a, b in pairwise(points):
        if t_ms <= b["t_ms"]:
            k = max(0, (t_ms - a["t_ms"]) / (b["t_ms"] - a["t_ms"]))
            return a["x"] + (b["x"] - a["x"]) * k, a["y"] + (b["y"] - a["y"]) * k
    return points[-1]["x"], points[-1]["y"]


def preview_contacts(contacts):
    """Deterministic event preview for review; never a wire injection script."""
    end = validate_contacts(contacts)
    times = sorted(
        {*range(0, end + 1, 16), *(p["t_ms"] for c in contacts for p in c["points"])}
    )
    active, events = {}, []
    for t in times:
        if active:
            events.append(
                {
                    "t_ms": t,
                    "action": "move",
                    "contacts": {i: contact_position(c, t) for i, c in active.items()},
                }
            )
        for c in contacts:
            if c["points"][-1]["t_ms"] == t:
                events.append(
                    {
                        "t_ms": t,
                        "action": "up",
                        "id": c["id"],
                        "active_ids": list(active),
                    }
                )
                active.pop(c["id"])
        for c in contacts:
            if c["points"][0]["t_ms"] == t:
                active[c["id"]] = c
                events.append(
                    {
                        "t_ms": t,
                        "action": "down",
                        "id": c["id"],
                        "active_ids": list(active),
                    }
                )
    return events
