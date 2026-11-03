import copy
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from iatest.application.ios_measurements import (
    evaluate_measurement,
    save_measurement,
)
from iatest.core.config import init_project
from iatest.engines.contracts import ExecutionPlan
from iatest.execution.control import RunControl
from iatest.mobile.ios_capture import XctraceRecorder
from iatest.mobile.ios_frames import parse_frame_xml

DIRECTORY = Path(__file__).parent / "fixtures/ios"


def exports():
    return [
        ET.fromstring((DIRECTORY / f"frames-xcode27-{name}.xml").read_bytes())
        for name in ("updates", "lifetimes")
    ]


def parse(updates, lifetimes, pid=777):
    return parse_frame_xml(ET.tostring(updates), ET.tostring(lifetimes), pid)


def test_actual_frame_tables_join_exact_process_surface_and_swap_without_fps():
    updates, lifetimes = exports()
    parsed = parse(updates, lifetimes)
    assert parsed["collector_observation"]["matched_frame_intervals"] == 2
    assert parsed["metric_quality"]["frame_lifetime_ms"]["assessed"] is True
    assert parsed["samples"][0]["frame_lifetime_ms"] == 33.336916
    assert not parse(updates, lifetimes, pid=888)["samples"]
    foreign = copy.deepcopy(lifetimes.find("node/row"))
    list(foreign)[4].text = "999999"
    lifetimes.find("node").append(foreign)
    assert parse(updates, lifetimes)["samples"] == parsed["samples"]


@pytest.mark.parametrize(
    "change", ["owner", "duplicate", "unit", "reference", "negative", "timestamp"]
)
def test_ambiguous_or_malformed_frame_identity_cannot_pass(change):
    updates, lifetimes = exports()
    if change == "owner":
        other = copy.deepcopy(updates.find("node/row"))
        other.find("process/pid").text = "888"
        updates.find("node").append(other)
    elif change == "duplicate":
        lifetimes.find("node").append(copy.deepcopy(lifetimes.find("node/row")))
    elif change == "unit":
        lifetimes.find("node/schema/col[2]/engineering-type").text = "milliseconds"
    elif change == "reference":
        lifetimes.find("node/row/duration").set("ref", "missing")
    elif change == "negative":
        lifetimes.find("node/row/duration").text = "-1"
    else:
        updates.find("node/row/start-time").text = "18446744073709551616"
    with pytest.raises(ValueError):
        parse(updates, lifetimes)


def test_truncated_intervals_remain_unassessed_in_saved_offline_gate(tmp_path):
    init_project(tmp_path, "suite")
    updates, lifetimes = exports()
    lifetimes.find("node").remove(lifetimes.findall("node/row")[-1])
    parsed = parse(updates, lifetimes)
    assert parsed["collector_observation"]["unmatched_target_updates"] == 1
    assert parsed["metric_quality"]["frame_lifetime_ms"]["assessed"] is False
    asset = save_measurement(tmp_path, "suite", parsed, {"platform": "ios"})
    plan = ExecutionPlan.from_dict(
        {
            "id": "frames",
            "kind": "client_performance",
            "spec": {
                "platform": "ios",
                "measurement": {"artifact_id": asset["id"]},
                "thresholds": {"max_frame_lifetime_ms": 100},
            },
        }
    )
    result = evaluate_measurement(tmp_path, "suite", plan)
    assert result.verdict == "inconclusive"
    assert result.steps[0]["actual"] is None


def test_frame_interval_can_begin_before_its_update_but_not_end_before_it():
    updates, lifetimes = exports()
    start = int(updates.find("node/row/start-time").text)
    lifetimes.find("node/row/start-time").text = str(start - 1)
    assert (
        parse(updates, lifetimes)["metric_quality"]["frame_lifetime_ms"]["assessed"]
        is True
    )
    lifetimes.find("node/row/start-time").text = "0"
    assert (
        parse(updates, lifetimes)["metric_quality"]["frame_lifetime_ms"]["assessed"]
        is False
    )


def test_animation_template_is_explicit_and_default_remains_activity_monitor():
    for name, expected in (
        ("activity_monitor", "Activity Monitor"),
        ("animation_hitches", "Animation Hitches"),
    ):
        recorder = XctraceRecorder(777, RunControl(), template=name)
        try:
            argv = recorder.argv()
            assert argv[argv.index("--template") + 1] == expected
            assert "--launch" not in argv
        finally:
            recorder.close()
