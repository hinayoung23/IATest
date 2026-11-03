"""Explicitly scoped SDK measurements; device counters never masquerade as App CPU."""

import math

METRICS = {
    "app_metal_gpu_busy_ratio": "ratio (union of explicitly registered, completed App Metal command buffers / observation window)",
    "gpu_allocated_memory_kb": "KiB (target process Metal allocations)",
    "device_battery_level_percent": "% (device)",
    "device_battery_temperature_celsius": "C (device)",
    "device_battery_power_watts": "W (device battery electrical power magnitude, snapshot)",
    "device_thermal_status": "platform severity (device)",
    "device_network_rx_bytes": "bytes (device interfaces, interval)",
    "device_network_tx_bytes": "bytes (device interfaces, interval)",
    "app_network_rx_bytes": "bytes (target UID, interval)",
    "app_network_tx_bytes": "bytes (target UID, interval)",
}
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        key: {"type": "number", "minimum": 0, "maximum": 2**64} for key in METRICS
    },
}
SCHEMA["properties"]["device_battery_temperature_celsius"].update(
    minimum=-100, maximum=200
)
SCHEMA["properties"]["device_battery_level_percent"]["maximum"] = 100
SCHEMA["properties"]["device_thermal_status"]["maximum"] = 6
SCHEMA["properties"]["app_metal_gpu_busy_ratio"]["maximum"] = 1
METAL_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["sample_uptime_ms", "retained_since_uptime_ms", "completed", "invalid", "pending", "intervals_ms"],
    "properties": {
        **{k: {"type": "number", "minimum": 0, "maximum": 2**53} for k in ("sample_uptime_ms", "retained_since_uptime_ms")},
        **{k: {"type": "integer", "minimum": 0, "maximum": 2**53} for k in ("completed", "invalid", "pending")},
        "intervals_ms": {"type": "array", "maxItems": 128, "items": {"type": "array", "minItems": 2, "maxItems": 2,
            "items": {"type": "number", "minimum": 0, "maximum": 2**53}}},
    },
}


def metal_measurements(before, after):
    from iatest.engines.http import validate

    validate(before, METAL_SCHEMA)
    validate(after, METAL_SCHEMA)
    start, end = before["sample_uptime_ms"], after["sample_uptime_ms"]
    if (end <= start or after["retained_since_uptime_ms"] > start
            or before["pending"] or after["pending"] or before["invalid"] != after["invalid"]
            or after["completed"] <= before["completed"]):
        raise ValueError("Metal GPU window is incomplete, has pending/invalid buffers or lacks completed samples")
    busy, last_end = 0.0, 0.0
    for left, right in after["intervals_ms"]:
        if not all(math.isfinite(v) for v in (left, right)) or left < last_end or right <= left or right > end:
            raise ValueError("Metal GPU intervals are unordered, overlapping or outside host uptime")
        busy += max(0, min(end, right)-max(start, left))
        last_end = right
    ratio = busy/(end-start)
    if not 0 <= ratio <= 1:
        raise ValueError("Invalid Metal GPU occupancy")
    return {"app_metal_gpu_busy_ratio": ratio}


def measurements(before, after):
    if (
        before["process_id"] != after["process_id"]
        or before["sequence"] >= after["sequence"]
        or before["timestamp_ms"] >= after["timestamp_ms"]
    ):
        raise ValueError(
            "Telemetry requires increasing observations from the same process"
        )
    result = {}
    for name, value in after.get("performance", {}).items():
        if name == "app_metal_gpu_busy_ratio":
            raise ValueError("Metal GPU ratio requires its native interval evidence")
        if (
            name not in METRICS
            or isinstance(value, bool)
            or not math.isfinite(value)
            or (value < 0 and name != "device_battery_temperature_celsius")
        ):
            raise ValueError("Invalid scoped telemetry metric")
        if name.endswith("_bytes"):
            previous = before.get("performance", {}).get(name)
            if previous is None or value < previous:
                raise ValueError("Network counter baseline absent or rolled back")
            result[name] = value - previous
        else:
            result[name] = max(value, before.get("performance", {}).get(name, value))
    if (before.get("metal_gpu") and after.get("metal_gpu")
            and after["metal_gpu"]["completed"] != before["metal_gpu"]["completed"]):
        result.update(metal_measurements(before["metal_gpu"], after["metal_gpu"]))
    return result
