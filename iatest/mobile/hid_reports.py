"""USB absolute pointer and parallel digitizer reports, with explicit releases."""

from __future__ import annotations

import struct

ABSOLUTE_MOUSE = bytes.fromhex(
    "05010902a1010901a10005091901290315002501750195038102"
    "750595018103050109300931150026ff7f751095028102"
    "09381581257f750895018106c0c0"
)


def touch_descriptor():
    # Report 1: ID + count + ten (tip, contact ID, X, Y) records = 62 bytes.
    value = bytes.fromhex("050d0904a101850109541500250a750895018102")
    finger = bytes.fromhex(
        "050d0922a102094215002501750195018102750795018103"
        "095115002509750895018102050109300931150026ff7f751095028102c0"
    )
    value += finger * 10
    # Report 2: maximum contact count. Linux's GET_REPORT ioctl serves its value.
    return value + bytes.fromhex("050d850209551500250a75089501b102c0")


def touch_report(active, previous):
    records = [(1, key, *position) for key, position in sorted(active.items())]
    records += [(0, key, *position) for key, position in sorted(previous.items()) if key not in active]
    if len(records) > 10:
        raise ValueError("Digitizer contact quota exceeded")
    return bytes([1, len(records)]) + b"".join(struct.pack("<BBHH", *row) for row in records).ljust(60, b"\0")


def touch_release(report):
    if len(report) != 62 or report[0] != 1 or report[1] > 10:
        raise ValueError("Invalid digitizer release state")
    active = {}
    for index in range(report[1]):
        tip, identifier, x, y = struct.unpack_from("<BBHH", report, 2 + index * 6)
        if tip:
            active[identifier] = (x, y)
    return touch_report({}, active)


def extension_schedule(command):
    if command["op"] == "point":
        x, y = command["x"], command["y"]
        button = {None: 0, "left": 1, "right": 2, "middle": 4}[command.get("button")]
        result = [(0, "absolute_mouse", struct.pack("<BHHb", button, x, y, 0))]
        if button:
            result.append((5, "absolute_mouse", struct.pack("<BHHb", 0, x, y, 0)))
        return result
    previous, result, last = {}, [], -1
    for frame in command["frames"]:
        stamp = frame["t_ms"]
        active = {c["id"]: (c["x"], c["y"]) for c in frame["contacts"]}
        if len(active) != len(frame["contacts"]) or stamp <= last:
            raise ValueError("Digitizer frames require unique contacts and increasing times")
        result.append((stamp, "digitizer", touch_report(active, previous)))
        previous, last = active, stamp
    if command["frames"][0]["t_ms"] != 0 or previous:
        raise ValueError("Digitizer path must start at zero and end with every contact released")
    return result


def feature_report_ioctl():
    """Linux usb_hidg_report ABI: persistent GET_REPORT 2 -> [2, 10]."""
    return 0x40486742, struct.pack("<BBH64s4x", 2, 0, 2, bytes([2, 10]))
