"""Process CPU counters: one logical core is 100%; never substitute system load."""

from __future__ import annotations

import math


def chromium_counters(value):
    result = {}
    for item in value["processInfo"]:
        pid, seconds = item["id"], item["cpuTime"]
        if (
            type(pid) is not int
            or pid <= 0
            or pid in result
            or not math.isfinite(seconds)
            or seconds < 0
        ):
            raise ValueError("Invalid Chromium process CPU counter")
        result[pid] = (item["type"], float(seconds))
    if not result:
        raise ValueError("Chromium returned no process counters")
    return result


def chromium_cpu(before, after, elapsed):
    if elapsed <= 0 or not math.isfinite(elapsed) or set(before) - set(after):
        raise ValueError("CPU interval lost a process; its final counter is unknown")
    total = 0.0
    for pid, (kind, seconds) in after.items():
        previous_kind, previous = before.get(pid, (kind, 0.0))
        if previous_kind != kind or seconds < previous:
            raise ValueError("CPU process identity/counter changed")
        total += seconds - previous
    return total / elapsed * 100


def android_stat(value):
    # comm can contain whitespace and ')'; fields after the final ') ' are stable.
    head, fields = value.strip().rsplit(") ", 1)
    pid = int(head.split(" (", 1)[0])
    parts = fields.split()
    ticks, born = int(parts[11]) + int(parts[12]), int(parts[19])
    if pid <= 0 or ticks < 0 or born <= 0:
        raise ValueError("Invalid Android process counters")
    return pid, born, ticks


def android_cpu(before, after, hz, elapsed):
    if before[:2] != after[:2] or after[2] < before[2] or hz <= 0 or elapsed <= 0:
        raise ValueError("Android process restarted or CPU counters are unavailable")
    return (after[2] - before[2]) / hz / elapsed * 100
