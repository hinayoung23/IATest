import copy
import json

import pytest

from iatest.execution.control import RunControl
from iatest.mobile.ios_capture import resolve_pid
from iatest.mobile.ios_xctest import parse_xcresult


@pytest.mark.parametrize("ratio_unit", ["ms/s", "ms per s"])
def test_exact_xcresult_bindings_convert_units_without_guessing_display_names(
    ratio_unit,
):
    spec = {
        "app_id": "com.example.app",
        "device_id": "fixture",
        "test_id": "MobileRunner/testMeasureLaunch()",
        "configuration_id": "default",
        "bindings": {
            "ios_launch_ms": {"identifier": "fixture.launch", "unit": "s"},
            "hitch_ratio": {"identifier": "fixture.hitch", "unit": ratio_unit},
        },
    }
    value = [
        {
            "testIdentifier": spec["test_id"],
            "testRuns": [
                {
                    "device": {"deviceId": "fixture"},
                    "testPlanConfiguration": {"configurationId": "default"},
                    "metrics": [
                        {
                            "identifier": "fixture.launch",
                            "displayName": "localized label",
                            "unitOfMeasurement": "s",
                            "measurements": [0.1, 0.2],
                        },
                        {
                            "identifier": "fixture.hitch",
                            "unitOfMeasurement": ratio_unit,
                            "measurements": [10],
                        },
                    ],
                }
            ],
        }
    ]
    parsed, source = parse_xcresult(value, spec)
    assert parsed["samples"] == [
        {"ios_launch_ms": 100.0},
        {"ios_launch_ms": 200.0},
        {"hitch_ratio": 0.01},
    ]
    assert source["device_id"] == "fixture"
    wrong = copy.deepcopy(value)
    wrong[0]["testRuns"][0]["metrics"][0]["unitOfMeasurement"] = "ms"
    with pytest.raises(ValueError, match="unit"):
        parse_xcresult(wrong, spec)
    wrong = copy.deepcopy(value)
    wrong[0]["testRuns"] *= 2
    with pytest.raises(ValueError, match="exact"):
        parse_xcresult(wrong, spec)
    spec["test_id"] = "another/test"
    with pytest.raises(ValueError, match="exact"):
        parse_xcresult(value, spec)


def test_real_scroll_metric_zero_frame_count_is_not_a_zero_hitch_pass():
    prefix = (
        "com.apple.dt.XCTMetric_OSSignpost-Scroll_DraggingAndDeceleration.animation."
    )
    value = [
        {
            "testIdentifier": "MobileRunner/testMeasureScroll",
            "testRuns": [
                {
                    "device": {"deviceId": "fixture"},
                    "testPlanConfiguration": {"configurationId": "1"},
                    "metrics": [
                        {
                            "identifier": prefix + "hitch.time.ratio",
                            "unitOfMeasurement": "ms per s",
                            "measurements": [0, 0],
                        },
                        {
                            "identifier": prefix + "frame.count",
                            "unitOfMeasurement": "frames",
                            "measurements": [0, 0],
                        },
                    ],
                }
            ],
        }
    ]
    spec = {
        "app_id": "com.example.lab",
        "device_id": "fixture",
        "configuration_id": "1",
        "test_id": value[0]["testIdentifier"],
        "bindings": {
            "hitch_ratio": {
                "identifier": prefix + "hitch.time.ratio",
                "unit": "ms per s",
            }
        },
    }
    parsed, _ = parse_xcresult(value, spec)
    assert parsed["samples"] == [{"hitch_ratio": 0.0}, {"hitch_ratio": 0.0}]
    assert parsed["metric_quality"]["hitch_ratio"]["assessed"] is False
    assert (
        parsed["metric_quality"]["hitch_ratio"]["code"] == "frame_coverage_unavailable"
    )
    assert parsed["collector_observation"]["frame_counts"] == [0, 0]
    assert parsed["collector_observation"]["frame_count_status"] == "zero"
    metrics = value[0]["testRuns"][0]["metrics"]
    metrics.append(
        {
            "identifier": prefix + "frame.rate",
            "unitOfMeasurement": "fps",
            "measurements": [85, 84.6],
        }
    )
    parsed, _ = parse_xcresult(value, spec)
    assert parsed["collector_observation"]["frame_rates_fps"] == [85, 84.6]
    assert parsed["collector_observation"]["zero_frame_count_with_nonzero_rate"] is True
    assert parsed["metric_quality"]["hitch_ratio"]["assessed"] is False
    assert any("zero frame counts" in note for note in parsed["notes"])
    metrics[-1]["unitOfMeasurement"] = "Hz"
    with pytest.raises(ValueError, match="frame rate schema"):
        parse_xcresult(value, spec)
    metrics.pop()
    value[0]["testRuns"][0]["metrics"][1]["measurements"] = [100, 100]
    parsed, _ = parse_xcresult(value, spec)
    assert parsed["samples"] == [{"hitch_ratio": 0.0}, {"hitch_ratio": 0.0}]
    assert parsed["metric_quality"]["hitch_ratio"]["assessed"] is True
    value[0]["testRuns"][0]["metrics"][1]["measurements"] = [100]
    with pytest.raises(ValueError, match="alignment"):
        parse_xcresult(value, spec)


def test_auto_pid_resolution_rejects_multiple_app_processes():
    profile = {"device_id": "fixture", "app_id": "com.example.app"}
    rows = [{"pid": 123, "bundleIdentifier": profile["app_id"]}]

    def runner(*args, **kwargs):
        return json.dumps({"result": {"runningProcesses": rows}})

    assert resolve_pid(profile, RunControl(), runner=runner) == 123
    rows.append({"pid": 124, "bundleIdentifier": profile["app_id"]})
    with pytest.raises(ValueError, match="uniquely"):
        resolve_pid(profile, RunControl(), runner=runner)
