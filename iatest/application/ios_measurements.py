"""Immutable iOS measurements, independent of fresh UI execution."""

from __future__ import annotations

import hashlib
import json
import statistics
import uuid
from pathlib import Path

from iatest.engines.contracts import EngineResult
from iatest.engines.http import validate
from iatest.engines.store import EngineStore
from iatest.engines.transactions import percentile
from iatest.mobile.ios_metrics import (
    IOS_METRICS,
    parse_measurements,
    parse_process_xml,
)

IMPORT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["format", "path"],
    "properties": {
        "id": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,80}$"},
        "format": {"enum": ["trace", "xctrace_xml", "measurements_json", "xcresult"]},
        "path": {"type": "string", "minLength": 1, "maxLength": 4096},
        "platform": {"enum": ["ios", "macos"]},
        "app_id": {"type": "string", "maxLength": 200},
        "device_id": {"type": "string", "maxLength": 200},
        "target_pid": {"type": "integer", "minimum": 1, "maximum": 2147483647},
        "trace_scope": {"enum": ["process", "device_process_statistics"]},
        "run_number": {"type": "integer", "minimum": 1, "maximum": 100},
        "test_id": {"type": "string", "minLength": 1, "maxLength": 500},
        "configuration_id": {"type": "string", "minLength": 1, "maxLength": 500},
        "bindings": {"type": "object"},
    },
}


def summaries(samples):
    values = {}
    for row in samples:
        for metric, value in row.items():
            values.setdefault(metric, []).append(value)
    return {
        k: {
            "samples": len(v),
            "min": min(v),
            "median": statistics.median(v),
            "p95": percentile(v, 0.95),
            "max": max(v),
            "unit": IOS_METRICS[k],
        }
        for k, v in values.items()
    }


def save_measurement(
    root,
    project,
    parsed,
    metadata,
    *,
    artifact_id="",
    source_digest="",
    coverage_complete=True,
):
    value = {
        "contract_version": 1,
        "source": metadata,
        "input_digest": source_digest,
        "schema": parsed["schema"],
        "measurements": summaries(parsed["samples"]),
        "missing": parsed["missing"],
        "notes": parsed["notes"],
        "coverage_complete": coverage_complete,
        "metric_quality": parsed.get("metric_quality", {}),
        "collector_observation": parsed.get("collector_observation", {}),
        "trust": metadata.get("trust", "caller-declared"),
        "raw_source_retained": False,
    }
    value["source_digest"] = hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()
    value["id"] = EngineStore(root, project).save_artifact(
        "ios_measurement",
        value,
        artifact_id=artifact_id or "ios-metrics-" + uuid.uuid4().hex[:20],
    )
    return value


def import_measurement(root, project, spec, *, control=None):
    validate(spec, IMPORT_SCHEMA)
    if "trace_scope" in spec and spec["format"] != "trace":
        raise ValueError("trace_scope applies only to an existing trace bundle")
    source = Path(spec["path"]).expanduser()
    metadata = {
        k: spec[k]
        for k in ("platform", "app_id", "device_id", "target_pid", "run_number")
        if k in spec
    }
    if spec["format"] == "xcresult":
        from iatest.mobile.ios_xctest import read_xcresult

        if not all(
            spec.get(k)
            for k in ("app_id", "device_id", "test_id", "configuration_id", "bindings")
        ):
            raise ValueError(
                "xcresult requires explicit App/device/test/configuration and metric bindings"
            )
        parsed, source_meta, fingerprint = read_xcresult(source, spec, control)
        metadata.update(
            source_meta,
            platform="ios",
            app_id=spec["app_id"],
            trust="xcresult test/device/configuration matched; App supplied by build owner",
        )
    elif spec["format"] == "trace":
        from iatest.mobile.ios_capture import read_trace

        if "target_pid" not in spec:
            raise ValueError("Trace imports require target_pid")
        parsed = read_trace(
            source,
            target_pid=spec["target_pid"],
            run_number=spec.get("run_number", 1),
            control=control,
            allow_device_scope=spec.get("trace_scope") == "device_process_statistics",
        )
        actual = parsed["metadata"]
        platform = {
            "iOS": "ios",
            "iPhoneOS": "ios",
            "iPadOS": "ios",
            "macOS": "macos",
        }.get(actual["platform"])
        if (
            not platform
            or (spec.get("platform") and spec["platform"] != platform)
            or (spec.get("device_id") and spec["device_id"] != actual["device_id"])
        ):
            raise ValueError("Trace device/platform differs from the declaration")
        metadata.update(
            actual,
            platform=platform,
            provider="xctrace",
            trust="trace-metadata; app_id remains caller-declared",
        )
        fingerprint = hashlib.sha256(
            json.dumps(
                {"metadata": actual, "samples": parsed["samples"]}, sort_keys=True
            ).encode()
        ).hexdigest()
    else:
        if not source.is_file() or source.stat().st_size > 16777216:
            raise ValueError(
                "Measurement source must be a local file of at most 16 MiB"
            )
        with source.open("rb") as stream:
            content = stream.read(16777217)
        if len(content) > 16777216:
            raise ValueError("Measurement source exceeded 16 MiB while reading")
        fingerprint = hashlib.sha256(content).hexdigest()
        if spec["format"] == "xctrace_xml":
            if "target_pid" not in spec or "platform" not in spec:
                raise ValueError("XML imports require target_pid and explicit platform")
            parsed = parse_process_xml(content, spec["target_pid"])
            metadata.update(
                provider="xctrace-xml",
                trust="caller-declared platform/app; process-scoped XML",
            )
        else:
            value = json.loads(content)
            parsed = parse_measurements(value)
            if spec.get("platform", "ios") != "ios" or (
                spec.get("app_id") and spec["app_id"] != value["app_id"]
            ):
                raise ValueError(
                    "Measurement App/platform differs from the declaration"
                )
            metadata.update(
                value["source"],
                app_id=value["app_id"],
                platform="ios",
                trust="caller-declared measurement export",
            )
    return save_measurement(
        root,
        project,
        parsed,
        metadata,
        artifact_id=spec.get("id", ""),
        source_digest=fingerprint,
    )


def evaluate_measurement(root, project, plan):
    value = EngineStore(root, project).get_artifact(
        "ios_measurement", plan.spec["measurement"]["artifact_id"]
    )
    if (
        plan.spec["measurement"].get("source_digest", value["source_digest"])
        != value["source_digest"]
    ):
        raise ValueError("Measurement source digest differs from the frozen plan")
    if value["source"].get("platform") != "ios":
        raise ValueError("A macOS measurement cannot satisfy an iOS performance gate")
    result = EngineResult(
        plan.id,
        "client_performance",
        limitations=[
            "Offline imported evidence; this run did not execute or measure a new UI journey",
            *value["notes"],
        ],
    )
    missing = []
    for gate, threshold in plan.spec["thresholds"].items():
        metric = gate.removeprefix("max_")
        measured = value["measurements"].get(metric)
        quality = value.get("metric_quality", {}).get(metric, {"assessed": True})
        assessed = bool(
            measured and value["coverage_complete"] and quality.get("assessed")
        )
        actual = measured["max"] if assessed else None
        if not assessed:
            missing.append(metric)
        result.steps.append(
            {
                "gate": gate,
                "actual": actual,
                "expected": threshold,
                "assessed": assessed,
                "passed": assessed and actual <= threshold,
                "quality": quality,
            }
        )
    result.verdict = (
        "inconclusive"
        if missing
        else "passed"
        if all(s["passed"] for s in result.steps)
        else "failed"
    )
    result.metrics = {
        "platform": "ios",
        "measurements": value["measurements"],
        "not_assessed": missing,
        "measurement_id": value["id"],
        "method": "offline imported iOS measurements",
        "source": value["source"],
        "source_digest": value["source_digest"],
        "fresh_ui_execution": False,
        "unconfigured_metrics": sorted(
            set(value["measurements"])
            - {key.removeprefix("max_") for key in plan.spec["thresholds"]}
        ),
        "model_calls": 0,
        "metric_quality": value.get("metric_quality", {}),
    }
    from iatest.engines.verdicts import assessment_metrics
    result.metrics.update(assessment_metrics(len(plan.spec["thresholds"]), sum(bool(step["assessed"]) for step in result.steps), sum(bool(step["assessed"]) and not step["passed"] for step in result.steps)))
    return result
