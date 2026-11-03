"""Immutable, installation-specific positive/negative iOS collector controls."""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime, timedelta

from iatest.engines.store import EngineStore
from iatest.execution.control import RunControl
from iatest.mobile.ios_measurement_scope import digest


def calibration_digest(value):
    return digest({k: v for k, v in value.items() if k not in {"id", "source_digest"}})


def assess_controls(negative, positive):
    """These limits verify a known 3×250-ms stimulus, not a business SLO."""
    baseline = [row["hitch_ratio"] for row in negative["samples"]]
    injected = [row["hitch_ratio"] for row in positive["samples"]]
    observed = positive.get("collector_observation", {})
    counts, totals = (
        observed.get("hitch_counts", []),
        observed.get("hitch_totals_ms", []),
    )
    conditions = {
        "enough_samples": len(baseline) >= 2 and len(injected) == len(baseline),
        "negative_control_quiet": bool(baseline) and max(baseline) <= 0.05,
        "positive_control_detected": bool(injected) and min(injected) >= 0.1,
        "three_hitches_detected": len(counts) == len(injected)
        and bool(counts)
        and min(counts) >= 3,
        "known_blocking_time_detected": len(totals) == len(injected)
        and bool(totals)
        and min(totals) >= 500,
    }
    return {
        "status": "usable" if all(conditions.values()) else "rejected",
        "checks": conditions,
        "limits": {
            "negative_max_ratio": 0.05,
            "positive_min_ratio": 0.1,
            "positive_min_hitches": 3,
            "positive_min_hitch_ms": 500,
        },
        "observations": {
            "negative_ratio": baseline,
            "positive_ratio": injected,
            "positive_hitch_counts": counts,
            "positive_hitch_totals_ms": totals,
        },
    }


def run_calibration(root, project, spec, control=None):
    from iatest.application.ios_measurements import save_measurement
    from iatest.engines.http import validate
    from iatest.mobile.ios_xctest import MEASUREMENT_SCHEMA, execute_measurement
    from iatest.mobile.lease import device_scope
    from iatest.mobile.store import MobileStore

    validate(spec, MEASUREMENT_SCHEMA)
    if (
        spec["mode"] != "scroll"
        or "hitch_ratio" not in spec["bindings"]
        or spec.get("calibration_id")
        or spec.get("iterations", 3) < 2
    ):
        raise ValueError(
            "Calibration requires scroll/hitch bindings, 2–10 samples and no existing calibration ID"
        )
    profile = MobileStore(root, project).get("profile", spec["profile"])
    if profile["platform"] != "ios" or profile["app_id"] != "dev.iatest.mobile.lab":
        raise ValueError(
            "Automatic injection is restricted to the framework-owned Debug Lab"
        )
    control = control or RunControl(timeout_seconds=480)
    with device_scope(profile):
        negative, negative_source, negative_hash = execute_measurement(
            root, project, spec, control, calibration_positive=False
        )
        positive, positive_source, positive_hash = execute_measurement(
            root, project, spec, control, calibration_positive=True
        )
        scope = negative_source["calibration_scope"]
        if positive_source["calibration_scope"] != scope:
            raise ValueError(
                "Device/App/toolchain/Runner changed between calibration controls"
            )
        negative_source["purpose"], positive_source["purpose"] = (
            "negative calibration control",
            "injected positive calibration control",
        )
        negative_asset = save_measurement(
            root, project, negative, negative_source, source_digest=negative_hash
        )
        positive_asset = save_measurement(
            root, project, positive, positive_source, source_digest=positive_hash
        )
    assessment = assess_controls(negative, positive)
    value = {
        "contract_version": 1,
        **assessment,
        "scope": scope,
        "measurement_spec": copy.deepcopy(spec),
        "negative_measurement_id": negative_asset["id"],
        "positive_measurement_id": positive_asset["id"],
        "valid_until": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
        "notes": [
            "Only verifies this collector and exact device/OS/toolchain/App installation/Runner/recipe",
            "Does not calibrate time accuracy or authorize unrelated App injection",
            "Frame-count validity remains independent of hitch calibration",
        ],
    }
    value["source_digest"] = calibration_digest(value)
    value["id"] = EngineStore(root, project).save_artifact(
        "ios_calibration", value, artifact_id="ios-cal-" + uuid.uuid4().hex[:20]
    )
    return value


def matching_calibration(root, project, calibration_id, scope):
    value = EngineStore(root, project).get_artifact("ios_calibration", calibration_id)
    if value.get("source_digest") != calibration_digest(value):
        raise ValueError("Calibration evidence digest changed")
    if value.get("status") != "usable":
        raise ValueError(
            "The collector did not detect its positive control; calibration is rejected"
        )
    if value.get("scope") != scope:
        raise ValueError(
            "Calibration scope differs: device/OS/toolchain/App installation/Runner/recipe must match"
        )
    try:
        expires = datetime.fromisoformat(value["valid_until"])
        valid = expires.tzinfo is not None and expires > datetime.now(UTC)
    except (TypeError, KeyError, ValueError):
        valid = False
    if not valid:
        raise ValueError("Collector calibration has expired")
    if not all(
        value.get("checks", {}).get(key) is True
        for key in (
            "enough_samples",
            "negative_control_quiet",
            "positive_control_detected",
            "three_hitches_detected",
            "known_blocking_time_detected",
        )
    ):
        raise ValueError("Collector calibration lacks verified control checks")
    return value


def apply_calibration(root, project, parsed, metadata, calibration_id):
    scope = metadata.get("calibration_scope")
    if not scope or "hitch_ratio" not in parsed.get("metric_quality", {}):
        raise ValueError("A scoped live hitch measurement is required")
    value = matching_calibration(root, project, calibration_id, scope)
    quality = parsed["metric_quality"]["hitch_ratio"]
    if not quality.get("assessed") and quality.get("code") not in {
        "frame_coverage_unavailable",
        "collector_calibration_required",
    }:
        raise ValueError(
            "Calibration cannot override unrelated metric quality failures"
        )
    parsed["metric_quality"] = copy.deepcopy(parsed["metric_quality"])
    parsed["metric_quality"]["hitch_ratio"] = {
        "assessed": True,
        "basis": "scoped_positive_negative_control",
        "calibration_id": calibration_id,
        "calibration_digest": value["source_digest"],
    }
    metadata.update(
        calibration_id=calibration_id, calibration_digest=value["source_digest"]
    )
    parsed["notes"].append(
        "Hitch assessment uses matching positive/negative controls; frame metrics are not inferred or repaired"
    )
