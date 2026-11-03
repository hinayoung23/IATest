"""Opt-in Simulator DTUHID input; XCTest remains a read-only target validator."""

import json
import os
import subprocess
import time
from pathlib import Path

from iatest.mobile.protocol import DeliveryUnknown, ProtocolError


def inject(device, step, arguments, timeout):
    control = device.control
    control.check()
    # Re-resolve the native reference immediately before external HID delivery.
    geometry = device.semantic.request(
        "hid_validate", arguments, timeout=min(timeout, 2)
    )
    if geometry.get("orientation") != 1:
        raise ValueError(
            f"Simulator HID currently requires portrait orientation; observed {geometry.get('orientation')}"
        )
    left, top, right, bottom = geometry["bounds"]
    width, height = geometry["width"], geometry["height"]
    if not (0 <= left < right <= width and 0 <= top < bottom <= height):
        raise ValueError("Simulator target must lie inside the current App viewport")
    x, y = (left + right) / 2 / width, (top + bottom) / 2 / height
    points = [{"x": x, "y": y, "delay_ms": 0}]
    if step["op"] == "swipe":
        dx, dy = {
            "up": (0, -0.35),
            "down": (0, 0.35),
            "left": (-0.35, 0),
            "right": (0.35, 0),
        }[step.get("direction", "up")]
        duration = min(2000, max(50, step.get("duration_ms", 300)))
        for i in range(1, 21):
            points.append(
                {
                    "x": x + dx * (right - left) / width * i / 20,
                    "y": y + dy * (bottom - top) / height * i / 20,
                    "delay_ms": duration / 20,
                }
            )
    points.append({**points[-1], "delay_ms": 20})
    path = Path(device.profile["simulator_hid_driver"]).expanduser().resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError("Build the explicitly selected Simulator HID driver first")
    developer = subprocess.check_output(
        ["xcode-select", "-p"], timeout=2, text=True
    ).strip()
    process = subprocess.Popen(
        [str(path), device.profile["device_id"], developer],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    started = time.time() * 1000
    uncertain = True
    deadline = time.monotonic() + min(timeout, 8)
    try:
        raw = (json.dumps({"points": points}) + "\n").encode()
        while True:
            control.check()
            if time.monotonic() >= deadline:
                raise TimeoutError("Simulator HID delivery deadline exceeded")
            try:
                output, _ = process.communicate(input=raw, timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                raw = None
        if len(output) > 65536:
            raise ProtocolError("Simulator HID response exceeds bounds")
        response = json.loads(output)
        if response.get("status") == "unknown":
            raise DeliveryUnknown("Simulator HID delivery unknown; never replay")
        if response.get("status") != "injection_call_returned" or not response.get(
            "accepted"
        ):
            uncertain = False
            raise ProtocolError(
                str(response.get("error", "Simulator HID rejected input"))
            )
        return {**response, "started_timestamp_ms": started,
                "coordinate_space": {k: geometry[k] for k in ("bounds", "width", "height", "orientation")}}
    except BaseException:
        if process.poll() is None:
            process.kill()
            process.communicate()
        if uncertain and getattr(device, "on_unknown", None):
            device.on_unknown()
        raise
