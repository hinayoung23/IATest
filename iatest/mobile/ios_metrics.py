"""Bounded, process-scoped Instruments exports and explicit measurement imports."""

from __future__ import annotations

import math
from xml.etree import ElementTree as ET

IOS_METRICS = {
    "cpu_percent": "%",
    "memory_footprint_kb": "KiB",
    "resident_memory_kb": "KiB",
    "ios_launch_ms": "ms",
    "frame_duration_ms": "ms",
    "frame_lifetime_ms": "ms",
    "hitch_ratio": "ratio",
}
SCHEMAS = (
    "sysmon-process",
    "activity-monitor-process-live",
    "activity-monitor-process-ledger",
)
FIELDS = {
    "cpu-percent": ("cpu_percent", "system-cpu-percent", 1),
    "memory-physical-footprint": ("memory_footprint_kb", "size-in-bytes", 1 / 1024),
    "memory-resident-size": ("resident_memory_kb", "size-in-bytes", 1 / 1024),
    "memory-real": ("resident_memory_kb", "size-in-bytes", 1 / 1024),
}


def xml_root(content: bytes):
    if not 0 < len(content) <= 16777216:
        raise ValueError("Instruments XML must be between 1 byte and 16 MiB")
    if b"\x00" in content:
        raise ValueError("Only UTF-8 Instruments XML is accepted")
    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        raise ValueError("XML entities and document types are not accepted")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError("Malformed Instruments XML") from exc
    if sum(1 for _ in root.iter()) > 300000:
        raise ValueError("Instruments XML exceeds the element budget")
    return root


def toc_metadata(content: bytes, run_number=1):
    from iatest.mobile.ios_frames import FRAME_SCHEMAS
    root = xml_root(content)
    if root.tag != "trace-toc":
        raise ValueError("Expected an xctrace table of contents")
    run = root.find(f"./run[@number='{int(run_number)}']")
    if run is None:
        raise ValueError("Requested trace run does not exist")
    device = run.find("./info/target/device")
    process = run.find("./info/target/process")
    # Explicit allowlist: target/environment can contain credentials and is never retained.
    return {
        "platform": device.get("platform", "") if device is not None else "",
        "device_id": device.get("uuid", "") if device is not None else "",
        "os_version": device.get("os-version", "") if device is not None else "",
        "device_model": device.get("model", "") if device is not None else "",
        "target_pid": int(process.get("pid", "0")) if process is not None else 0,
        "template": run.findtext("./info/summary/template-name", ""),
        "instruments_version": run.findtext("./info/summary/instruments-version", ""),
        "schemas": sorted(
            {
                t.get("schema", "")
                for t in run.findall("./data/table")
                if t.get("schema") in {*SCHEMAS, *FRAME_SCHEMAS}
            }
        ),
        "run_number": int(run_number),
    }


def finite(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("Measurements must be finite nonnegative numbers")
    return float(value)


def parse_process_xml(content: bytes, target_pid: int):
    if type(target_pid) is not int or not 0 < target_pid <= 2147483647:
        raise ValueError("An explicit positive target PID is required")
    root = xml_root(content)
    if root.tag != "trace-query-result":
        raise ValueError("Expected xctrace table XML")
    tables, notes = {}, []
    for node in root.findall("./node"):
        schema = node.find("./schema")
        if schema is None or schema.get("name") not in SCHEMAS:
            continue
        name = schema.get("name")
        if name in tables:
            raise ValueError(
                "Multiple matching tables require an unambiguous run/table selection"
            )
        columns = [
            (c.findtext("mnemonic"), c.findtext("engineering-type"))
            for c in schema.findall("./col")
        ]
        if (
            not columns
            or len(columns) > 128
            or len({c[0] for c in columns}) != len(columns)
        ):
            raise ValueError("Invalid Instruments column schema")
        index = {}
        for element in node.iter():
            if element.get("id"):
                if element.get("id") in index:
                    raise ValueError("Duplicate Instruments reference ID")
                index[element.get("id")] = element

        def resolve(element, index=index):
            visited = set()
            while element.get("ref"):
                ref = element.get("ref")
                if ref in visited or len(visited) >= 32 or ref not in index:
                    raise ValueError("Unresolved or cyclic Instruments reference")
                visited.add(ref)
                actual = index[ref]
                if actual.tag != element.tag:
                    raise ValueError("Instruments reference type differs")
                element = actual
            return element

        def pid(element):
            element = resolve(element)
            if element.tag == "process":
                element = element.find("./pid")
                if element is None:
                    return None
                element = resolve(element)
            if element.tag != "pid":
                return None
            return int(element.text or "0")

        rows, raw_rows = [], node.findall("./row")
        if len(raw_rows) > 10000:
            raise ValueError("Instruments export exceeds 10000 rows")
        for raw in raw_rows:
            if len(raw) != len(columns):
                raise ValueError("Instruments row does not match its column schema")
            cells = {
                key: (kind, resolve(cell)) for (key, kind), cell in zip(columns, raw)
            }
            identities = [
                pid(cells[key][1]) for key in ("pid", "process") if key in cells
            ]
            if not identities or any(p != target_pid for p in identities):
                continue
            values = {}
            for field, (metric, required_type, scale) in FIELDS.items():
                if field not in cells:
                    continue
                kind, cell = cells[field]
                if cell.tag == "sentinel":
                    continue
                if kind != required_type or cell.tag != required_type:
                    notes.append(f"Unrecognized unit/type for {name}.{field}")
                    continue
                # Numeric payload has the engineering unit; fmt is display text only.
                number = finite(float(cell.text or "nan")) * scale
                values[metric] = number
            if values:
                rows.append(values)
        tables[name] = rows
    selected = next((name for name in SCHEMAS if tables.get(name)), "")
    rows = tables.get(selected, [])
    if not rows:
        notes.append("No supported process measurements for the selected PID")
    return {
        "samples": rows,
        "schema": selected,
        "target_pid": target_pid,
        "notes": sorted(set(notes)),
        "missing": sorted(set(IOS_METRICS) - {key for row in rows for key in row}),
    }


def parse_measurements(value):
    """Explicit interchange contract for XCTest/custom exports; never guess units."""
    if (
        not isinstance(value, dict)
        or set(value)
        != {"contract_version", "platform", "app_id", "source", "measurements"}
        or value["contract_version"] != 1
        or value["platform"] != "ios"
    ):
        raise ValueError("Expected iOS measurements contract version 1")
    if not isinstance(value["app_id"], str) or not value["app_id"]:
        raise ValueError("Measurement App ID is required")
    source = value["source"]
    if (
        not isinstance(source, dict)
        or set(source) - {"provider", "device_id", "os_version", "build", "test_id"}
        or source.get("provider") not in {"xctest", "instruments", "custom"}
        or not all(isinstance(v, str) and len(v) <= 500 for v in source.values())
    ):
        raise ValueError("Explicit bounded measurement source is required")
    if (
        not isinstance(value["measurements"], list)
        or not 1 <= len(value["measurements"]) <= len(IOS_METRICS)
    ):
        raise ValueError(f"Provide between 1 and {len(IOS_METRICS)} metrics")
    samples, seen = [], set()
    for metric in value["measurements"]:
        if not isinstance(metric, dict) or set(metric) != {"metric", "unit", "values"}:
            raise ValueError("Metric requires metric, unit and values")
        key = metric["metric"]
        if key not in IOS_METRICS or metric["unit"] != IOS_METRICS[key] or key in seen:
            raise ValueError("Unknown/duplicate metric or mismatched measurement unit")
        seen.add(key)
        if (
            not isinstance(metric["values"], list)
            or not 1 <= len(metric["values"]) <= 10000
        ):
            raise ValueError("Provide 1–10000 samples per metric")
        values = [finite(v) for v in metric["values"]]
        if key == "hitch_ratio" and any(v > 1 for v in values):
            raise ValueError("Hitch ratio must be between 0 and 1")
        samples.extend({key: v} for v in values)
    return {
        "samples": samples,
        "schema": "iatest-ios-measurements/1",
        "notes": [
            "Source and measurement conditions are supplied by the caller, not independently verified"
        ],
        "missing": sorted(set(IOS_METRICS) - seen),
    }
