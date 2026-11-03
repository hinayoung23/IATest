"""Read-only per-UID Android battery model deltas; units are estimated mAh."""

from __future__ import annotations

import csv
import io
import math
import re
from decimal import Decimal


def parse_checkin(content, uid):
    if not isinstance(content, str) or len(content.encode()) > 1048576:
        raise ValueError("Battery statistics exceed the read quota")
    epoch, values = [], []
    for index, row in enumerate(csv.reader(io.StringIO(content))):
        if index >= 50000:
            raise ValueError("Battery statistics row quota exceeded")
        if len(row) < 4 or row[0] != "9" or row[2] != "l":
            continue
        if row[1] == "0" and row[3] == "bt":
            if (
                len(row) < 10
                or not row[4].isdigit()
                or not row[9].isdigit()
                or int(row[9]) <= 0
            ):
                raise ValueError("Battery statistics reset identity is unavailable")
            epoch.append((int(row[4]), int(row[9])))
        if row[1] == str(uid) and row[3] == "pwi" and len(row) >= 6 and row[4] == "uid":
            try:
                value = float(row[5])
            except ValueError as exc:
                raise ValueError("Invalid battery model estimate") from exc
            if not math.isfinite(value) or not 0 <= value <= 1e12:
                raise ValueError("Invalid battery model estimate")
            values.append(value)
    if len(epoch) != 1 or len(values) != 1:
        raise ValueError(
            "A unique since-charged UID estimate/reset identity is unavailable"
        )
    return {"epoch": epoch[0], "estimated_mah": values[0]}


def snapshot(device):
    app = device.profile["app_id"]
    packages = re.findall(
        r"package:([^\s]+)\s+uid:([0-9]+)",
        device.adb("shell", "cmd", "package", "list", "packages", "-U"),
    )
    selected = [int(uid) for name, uid in packages if name == app]
    if (
        len(selected) != 1
        or selected[0] < 10000
        or sum(int(uid) == selected[0] for _, uid in packages) != 1
    ):
        raise ValueError("Battery estimate requires a unique non-system App UID")
    battery = device.adb("shell", "dumpsys", "battery")
    powered = re.findall(
        r"^\s*(?:AC|USB|Wireless|Dock) powered:\s*(true|false)\s*$",
        battery,
        re.MULTILINE,
    )
    if len(powered) < 3 or "true" in powered:
        raise ValueError(
            "Battery model measurement requires an actually unplugged device; no simulated unplug"
        )
    boot = device.adb("shell", "cat", "/proc/sys/kernel/random/boot_id").strip()
    if not re.fullmatch(r"[a-f0-9-]{36}", boot):
        raise ValueError("Battery boot identity unavailable")
    content = device.adb(
        "shell", "dumpsys", "batterystats", "--checkin", "--charged", app, timeout=5
    )
    return {**parse_checkin(content, selected[0]), "uid": selected[0], "boot_id": boot}


def measurements(before, after):
    if any(before[k] != after[k] for k in ("epoch", "uid", "boot_id")):
        raise ValueError(
            "Battery statistics were reset, rebooted or the App UID changed"
        )
    values = (before["estimated_mah"], after["estimated_mah"])
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        for value in values
    ):
        raise ValueError("Invalid battery model counter")
    # Checkin prints decimal estimates. Binary subtraction must not turn an
    # exact 0.30 mAh delta into 0.3000000000000007 and fail a 0.30 gate.
    delta = float(Decimal(str(values[1])) - Decimal(str(values[0])))
    if not math.isfinite(delta) or delta < 0:
        raise ValueError("Battery model counter decreased")
    return {"app_estimated_battery_mah": delta}
