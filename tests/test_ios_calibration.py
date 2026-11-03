import copy
import json
import plistlib
from datetime import UTC, datetime, timedelta

import pytest

from iatest.application.ios_measurements import (
    evaluate_measurement,
    save_measurement,
)
from iatest.core.config import init_project
from iatest.engines.contracts import ExecutionPlan
from iatest.engines.store import EngineStore
from iatest.mobile.ios_calibration import (
    apply_calibration,
    assess_controls,
    calibration_digest,
    matching_calibration,
)
from iatest.mobile.ios_measurement_scope import measurement_scope, runner_digest
from iatest.mobile.ios_xctest import scroll_evidence


def controls():
    quiet = {"samples": [{"hitch_ratio": 0}, {"hitch_ratio": 0.01}]}
    injected = {
        "samples": [{"hitch_ratio": 0.27}, {"hitch_ratio": 0.26}],
        "collector_observation": {
            "hitch_counts": [3, 3],
            "hitch_totals_ms": [750, 750],
        },
    }
    return quiet, injected


@pytest.mark.parametrize(
    "change", ["quiet", "ratio", "counts", "duration", "alignment"]
)
def test_known_fault_must_be_detected_by_each_control_channel(change):
    quiet, positive = controls()
    assert assess_controls(quiet, positive)["status"] == "usable"
    if change == "quiet":
        quiet["samples"][0]["hitch_ratio"] = 0.2
    elif change == "ratio":
        positive["samples"][0]["hitch_ratio"] = 0
    elif change == "counts":
        positive["collector_observation"]["hitch_counts"][0] = 0
    elif change == "duration":
        positive["collector_observation"]["hitch_totals_ms"][0] = 0
    else:
        positive["collector_observation"]["hitch_totals_ms"].pop()
    assert assess_controls(quiet, positive)["status"] == "rejected"


def save_calibration(root, *, status="usable", expired=False):
    init_project(root, "suite")
    value = {
        **assess_controls(*controls()),
        "scope": {"app": "installation-1"},
        "valid_until": (
            datetime.now(UTC) + timedelta(days=-1 if expired else 1)
        ).isoformat(),
    }
    value["status"] = status
    value["source_digest"] = calibration_digest(value)
    value["id"] = EngineStore(root, "suite").save_artifact("ios_calibration", value)
    return value


@pytest.mark.parametrize("change", ["scope", "digest", "expired", "rejected"])
def test_calibration_rejects_changed_installation_expiry_or_rejected_control(
    tmp_path, change
):
    value = save_calibration(
        tmp_path,
        expired=change == "expired",
        status="rejected" if change == "rejected" else "usable",
    )
    scope = value["scope"]
    if change == "scope":
        scope = {"app": "installation-2"}
    if change == "digest":
        store = EngineStore(tmp_path, "suite")
        with store.connection() as connection:
            row = connection.execute(
                "select content from artifacts where id=?", (value["id"],)
            ).fetchone()
            changed = json.loads(row[0])
            changed["status"] = "tampered"
            connection.execute(
                "update artifacts set content=? where id=?",
                (json.dumps(changed), value["id"]),
            )
    with pytest.raises(ValueError):
        matching_calibration(tmp_path, "suite", value["id"], scope)


def test_calibration_changes_only_hitch_quality_and_offline_gate_obeys_it(tmp_path):
    calibration = save_calibration(tmp_path)
    parsed = {
        "schema": "fixture",
        "samples": [{"hitch_ratio": 0.02, "frame_duration_ms": 10}],
        "missing": [],
        "notes": [],
        "metric_quality": {
            name: {"assessed": False, "code": "frame_coverage_unavailable"}
            for name in ("hitch_ratio", "frame_duration_ms")
        },
    }
    metadata = {"platform": "ios", "calibration_scope": calibration["scope"]}
    uncalibrated = save_measurement(tmp_path, "suite", parsed, metadata)

    def gate(asset, **thresholds):
        return evaluate_measurement(
            tmp_path,
            "suite",
            ExecutionPlan.from_dict(
                {
                    "id": "gate",
                    "kind": "client_performance",
                    "spec": {
                        "platform": "ios",
                        "measurement": {"artifact_id": asset["id"]},
                        "thresholds": thresholds,
                    },
                }
            ),
        )

    assert gate(uncalibrated, max_hitch_ratio=0.05).verdict == "inconclusive"
    apply_calibration(tmp_path, "suite", parsed, metadata, calibration["id"])
    assert parsed["metric_quality"]["frame_duration_ms"]["assessed"] is False
    asset = save_measurement(tmp_path, "suite", parsed, metadata)
    assert gate(asset, max_hitch_ratio=0.05).verdict == "passed"
    assert gate(asset, max_hitch_ratio=0.01).verdict == "failed"
    result = gate(asset, max_hitch_ratio=0.05, max_frame_duration_ms=20)
    assert result.verdict == "inconclusive"
    assert result.steps[0]["assessed"] is True and result.steps[1]["assessed"] is False


@pytest.mark.parametrize("change", ["nonce", "origin", "movement", "stalls", "count"])
def test_scroll_evidence_requires_matching_nonce_restored_origin_and_injected_stalls(
    change,
):
    rows = [
        {
            "measurement_id": "known",
            "invocation": i,
            "origin_restored": True,
            "moved": True,
            "start_x": 0,
            "start_y": 430,
            "end_x": 0,
            "end_y": -300,
            "injected_stalls": 3,
        }
        for i in range(3)
    ]
    assert (
        len(
            scroll_evidence(
                "\n".join("IATestScrollEvidence " + json.dumps(r) for r in rows),
                "known",
                2,
                calibration_positive=True,
            )
        )
        == 3
    )
    if change == "nonce":
        rows[0]["measurement_id"] = "other"
    elif change == "origin":
        rows[1]["start_y"] += 2
    elif change == "movement":
        rows[1]["moved"] = False
    elif change == "stalls":
        rows[1]["injected_stalls"] = 0
    else:
        rows.pop()
    with pytest.raises(ValueError):
        scroll_evidence(
            "\n".join("IATestScrollEvidence " + json.dumps(r) for r in rows),
            "known",
            2,
            calibration_positive=True,
        )


def test_runner_fingerprint_changes_when_native_code_changes_and_rejects_external_host(
    tmp_path,
):
    host = tmp_path / "Runner.app"
    host.mkdir()
    binary = host / "Runner"
    binary.write_bytes(b"version1")
    source = tmp_path / "runner.xctestrun"
    source.write_bytes(
        plistlib.dumps(
            {
                "Tests": {
                    "TestHostPath": "__TESTROOT__/Runner.app",
                    "TestBundlePath": "__TESTHOST__/tests.xctest",
                }
            }
        )
    )
    first = runner_digest(source)
    binary.write_bytes(b"version2")
    assert runner_digest(source) != first
    (host / "link").symlink_to(binary)
    with pytest.raises(ValueError, match="symlink"):
        runner_digest(source)


def test_scope_invalidates_after_app_reinstall_or_recipe_change(tmp_path, monkeypatch):
    import iatest.mobile.ios_measurement_scope as scope

    app = {
        "bundleIdentifier": "dev.iatest.mobile.lab",
        "version": "1",
        "bundleVersion": "1",
        "url": "/installation/one",
    }
    monkeypatch.setattr(scope, "app_record", lambda *args: copy.deepcopy(app))
    monkeypatch.setattr(
        scope,
        "device_query",
        lambda *args: {"deviceProperties": {"osBuildUpdate": "fixture"}},
    )
    monkeypatch.setattr(scope, "command", lambda *args, **kwargs: "Xcode fixture")
    monkeypatch.setattr(scope, "runner_digest", lambda *args: "runner-1")
    profile = {"app_id": "dev.iatest.mobile.lab", "device_id": "fixture"}
    spec = {
        "bindings": {"hitch_ratio": {"identifier": "exact", "unit": "ratio"}},
        "target_identifier": "list",
    }
    first = measurement_scope(profile, spec, None)
    app["url"] = "/installation/two"
    assert measurement_scope(profile, spec, None) != first
    app["url"] = "/installation/one"
    assert (
        measurement_scope(profile, {**spec, "origin_identifier": "row2"}, None) != first
    )
