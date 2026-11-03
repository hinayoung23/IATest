"""Join observed frame intervals to one process; never derive frames from FPS."""

from __future__ import annotations

from iatest.mobile.ios_metrics import IOS_METRICS, xml_root

FRAME_SCHEMAS = ("hitches-updates", "hitches-frame-lifetimes")


def _rows(content, name):
    root = xml_root(content)
    nodes = [
        n
        for n in root.findall("node")
        if n.find("schema") is not None and n.find("schema").get("name") == name
    ]
    if root.tag != "trace-query-result" or len(nodes) != 1:
        raise ValueError("Frame export requires one exact supported table")
    node = nodes[0]
    columns = [
        (c.findtext("mnemonic"), c.findtext("engineering-type"))
        for c in node.findall("schema/col")
    ]
    required = {
        "start": "start-time",
        "duration": "duration",
        "display": "display-name",
        "swap-id": "uint32",
        "surface-id": "uint32",
    }
    if name == "hitches-updates":
        required.update(process="process", **{"containment-level": "uint32"})
    if (
        len(columns) > 128
        or len(dict(columns)) != len(columns)
        or any(dict(columns).get(k) != kind for k, kind in required.items())
    ):
        raise ValueError("Frame table identity/unit schema changed")
    index = {}
    for cell in node.iter():
        if cell.get("id"):
            if cell.get("id") in index:
                raise ValueError("Duplicate frame reference ID")
            index[cell.get("id")] = cell

    def resolve(cell):
        seen = set()
        while cell.get("ref"):
            ref = cell.get("ref")
            if ref in seen or len(seen) >= 32 or ref not in index:
                raise ValueError("Unresolved or cyclic frame reference")
            seen.add(ref)
            actual = index[ref]
            if actual.tag != cell.tag:
                raise ValueError("Frame reference type differs")
            cell = actual
        return cell

    rows = node.findall("row")
    if len(rows) > 10000:
        raise ValueError("Frame table exceeds 10000 rows")
    result = []
    for row in rows:
        if len(row) != len(columns):
            raise ValueError("Frame row does not match its schema")
        cells = {k: resolve(c) for (k, _), c in zip(columns, row)}
        if any(cells[k].tag != kind for k, kind in required.items()):
            raise ValueError("Frame cell type differs from its declared unit")
        values = {}
        for key in ("start", "duration", "swap-id", "surface-id", "containment-level"):
            if key not in cells:
                continue
            text = cells[key].text or ""
            if not text.isascii() or not text.isdigit() or len(text) > 20:
                raise ValueError(
                    "Frame timestamps and identities require raw nonnegative integers"
                )
            values[key] = int(text)
            if values[key] > (
                4294967295
                if key in {"swap-id", "surface-id", "containment-level"}
                else 18446744073709551615
            ):
                raise ValueError("Frame numeric value exceeds its engineering type")
        display = cells["display"].text or ""
        if not display or len(display) > 200:
            raise ValueError("Frame display identity is missing or oversized")
        values["key"] = (display, values["swap-id"], values["surface-id"])
        if name == "hitches-updates":
            pid = cells["process"].find("pid")
            text = resolve(pid).text if pid is not None else ""
            if (
                not text
                or len(text) > 10
                or not text.isascii()
                or not text.isdigit()
                or not 0 < int(text) <= 2147483647
            ):
                raise ValueError("Frame update has no exact process identity")
            values["pid"] = int(text)
        result.append(values)
    return result


def parse_frame_xml(updates, lifetimes, target_pid):
    if type(target_pid) is not int or not 0 < target_pid <= 2147483647:
        raise ValueError("An explicit positive frame process PID is required")
    owners, target = {}, {}
    for row in _rows(updates, "hitches-updates"):
        if row["containment-level"] != 0:
            continue
        key = row["key"]
        owners.setdefault(key, set()).add(row["pid"])
        if row["pid"] == target_pid:
            if key in target:
                raise ValueError("Ambiguous repeated target frame update")
            target[key] = row
    if any(len(owners[key]) != 1 for key in target):
        raise ValueError("A frame identity is shared by different processes")
    matched, incomplete, seen = [], 0, set()
    for row in _rows(lifetimes, "hitches-frame-lifetimes"):
        key = row["key"]
        if key not in target:
            continue
        if key in seen:
            raise ValueError("Ambiguous duplicate target frame lifetime")
        seen.add(key)
        update = target[key]
        if not row["duration"] or row["start"] + row["duration"] < update["start"]:
            incomplete += 1
            continue
        matched.append({"frame_lifetime_ms": row["duration"] / 1000000})
    missing = len(set(target) - seen)
    assessed = bool(matched) and not incomplete and not missing
    return {
        "schema": "xctrace-frame-lifetimes/1",
        "samples": matched,
        "missing": sorted(
            set(IOS_METRICS) - ({"frame_lifetime_ms"} if matched else set())
        ),
        "metric_quality": {
            "frame_lifetime_ms": {
                "assessed": assessed,
                "basis": "unique_process_display_swap_surface_join",
                **({} if assessed else {"code": "incomplete_frame_coverage"}),
            }
        },
        "collector_observation": {
            "target_updates": len(target),
            "matched_frame_intervals": len(matched),
            "unmatched_target_updates": missing,
            "invalid_frame_intervals": incomplete,
        },
        "notes": [
            "Observed hitches-frame-lifetimes durations, associated through exact PID/display/swap/surface; not FPS-derived frame times or an XCTest frame-count repair",
            "Frame intervals cover the recording window and are not restricted to a single UI step",
        ],
    }
