"""Explicit xcresult metric bindings and bounded framework XCTest entrypoints."""

from __future__ import annotations

import hashlib
import json
import math
import os
import plistlib
import re
import subprocess
import tempfile
import uuid
from pathlib import Path

from iatest.engines.http import validate
from iatest.execution.control import RunControl
from iatest.mobile.ios_metrics import finite, parse_measurements

BINDINGS_SCHEMA = {
    "type": "object",
    "minProperties": 1,
    "maxProperties": 3,
    "additionalProperties": False,
    "properties": {
        key: {
            "type": "object",
            "additionalProperties": False,
            "required": ["identifier", "unit"],
            "properties": {
                "identifier": {"type": "string", "minLength": 1, "maxLength": 500},
                "unit": {
                    "enum": ["s", "ms", "us", "ns"]
                    if key != "hitch_ratio"
                    else ["ms/s", "ms per s", "ratio"]
                },
            },
        }
        for key in ("ios_launch_ms", "frame_duration_ms", "hitch_ratio")
    },
}


def parse_xcresult(value, spec):
    validate(spec["bindings"], BINDINGS_SCHEMA)
    tests = value if isinstance(value, list) else value.get("testMetrics", [])
    if not isinstance(tests, list) or len(tests) > 1000:
        raise ValueError("Unrecognized or oversized xcresult metrics output")
    selected = []
    for test in tests:
        if test.get("testIdentifier") != spec["test_id"]:
            continue
        for run in test.get("testRuns", []):
            if (
                run.get("device", {}).get("deviceId") == spec["device_id"]
                and run.get("testPlanConfiguration", {}).get("configurationId")
                == spec["configuration_id"]
            ):
                selected.append(run)
    if len(selected) != 1:
        raise ValueError("xcresult requires one exact test/device/configuration run")
    metrics, measurements, quality = selected[0]["metrics"], [], {}
    observations = {}
    if not isinstance(metrics, list) or len(metrics) > 100:
        raise ValueError("xcresult metric count exceeds bounds")
    for name, binding in spec["bindings"].items():
        quality[name] = {"assessed": True, "basis": "explicit_xcresult_binding"}
        matches = [m for m in metrics if m.get("identifier") == binding["identifier"]]
        if len(matches) != 1 or matches[0].get("unitOfMeasurement") != binding["unit"]:
            raise ValueError(
                "xcresult metric identifier/unit is missing, ambiguous or changed"
            )
        raw = matches[0].get("measurements")
        if not isinstance(raw, list) or not 1 <= len(raw) <= 10000:
            raise ValueError("xcresult metric sample count exceeds bounds")
        if (
            name in {"hitch_ratio", "frame_duration_ms"}
            and ".animation." in binding["identifier"]
        ):
            prefix = binding["identifier"].split(".animation.", 1)[0]
            counts = [
                m
                for m in metrics
                if m.get("identifier") == prefix + ".animation.frame.count"
            ]
            frame_counts = []
            if counts:
                if (
                    len(counts) != 1
                    or counts[0].get("unitOfMeasurement") != "frames"
                    or not isinstance(counts[0].get("measurements"), list)
                    or len(counts[0]["measurements"]) != len(matches[0]["measurements"])
                ):
                    raise ValueError(
                        "xcresult frame count schema or sample alignment changed"
                    )
                frame_counts = [finite(value) for value in counts[0]["measurements"]]
            if not frame_counts or any(value <= 0 for value in frame_counts):
                quality[name] = {
                    "assessed": False,
                    "code": "frame_coverage_unavailable",
                    "basis": "A valid scoped collector calibration is required for hitch assessment; frame metrics remain unavailable",
                }
            observations["frame_counts"] = frame_counts
            observations["frame_count_status"] = (
                "missing"
                if not frame_counts
                else "positive"
                if all(value > 0 for value in frame_counts)
                else "zero"
                if all(value == 0 for value in frame_counts)
                else "partial"
            )
            rates = [
                m
                for m in metrics
                if m.get("identifier") == prefix + ".animation.frame.rate"
            ]
            if rates:
                if (
                    len(rates) != 1
                    or rates[0].get("unitOfMeasurement") != "fps"
                    or not isinstance(rates[0].get("measurements"), list)
                    or len(rates[0]["measurements"]) != len(raw)
                ):
                    raise ValueError(
                        "xcresult frame rate schema or sample alignment changed"
                    )
                frame_rates = [finite(value) for value in rates[0]["measurements"]]
                observations["frame_rates_fps"] = frame_rates
                observations["zero_frame_count_with_nonzero_rate"] = bool(
                    frame_counts
                ) and any(
                    count == 0 and rate > 0
                    for count, rate in zip(frame_counts, frame_rates)
                )
        if name == "hitch_ratio":
            identifier = binding["identifier"]
            if "XCTMetric_Hitch-" in identifier:
                quality[name] = {
                    "assessed": False,
                    "code": "collector_calibration_required",
                    "basis": "Application hitch collector requires a positive/negative control",
                }
                family = identifier.removesuffix("time.ratio")
                companions = {
                    "hitch_counts": (family + "number", "hitches", 1),
                    "hitch_totals_ms": (family + "total.duration", "s", 1000),
                }
            elif ".animation.hitch.time.ratio" in identifier:
                family = identifier.removesuffix("hitch.time.ratio")
                companions = {
                    "hitch_counts": (family + "hitch.number", "hitches", 1),
                    "hitch_totals_ms": (family + "hitches.total.duration", "ms", 1),
                }
            else:
                companions = {}
            for key, (identifier, unit, scale) in companions.items():
                found = [m for m in metrics if m.get("identifier") == identifier]
                if found:
                    if (
                        len(found) != 1
                        or found[0].get("unitOfMeasurement") != unit
                        or not isinstance(found[0].get("measurements"), list)
                        or len(found[0]["measurements"])
                        != len(matches[0]["measurements"])
                    ):
                        raise ValueError(
                            "xcresult hitch companion schema or alignment changed"
                        )
                    observations[key] = [
                        finite(v) * scale for v in found[0]["measurements"]
                    ]
        factor = {
            "s": 1000,
            "ms": 1,
            "us": 0.001,
            "ns": 0.000001,
            "ms/s": 0.001,
            "ms per s": 0.001,
            "ratio": 1,
        }[binding["unit"]]
        raw = matches[0]["measurements"]
        if not isinstance(raw, list) or not 1 <= len(raw) <= 10000:
            raise ValueError("xcresult metric sample count exceeds bounds")
        measurements.append(
            {
                "metric": name,
                "unit": "ratio" if name == "hitch_ratio" else "ms",
                "values": [finite(v) * factor for v in raw],
            }
        )
    interchange = {
        "contract_version": 1,
        "platform": "ios",
        "app_id": spec["app_id"],
        "source": {
            "provider": "xctest",
            "device_id": spec["device_id"],
            "test_id": spec["test_id"],
        },
        "measurements": measurements,
    }
    parsed = parse_measurements(interchange)
    parsed["metric_quality"] = quality
    parsed["collector_observation"] = observations
    parsed["schema"] = "xcresult-test-metrics/0.4"
    parsed["notes"] = [
        "Explicit metric identifiers and units; wall clock, FPS and hitch ratios are never silently interchanged",
        "App identity is supplied by the build owner for imported result bundles",
        *(
            [
                "Apple output contains zero frame counts alongside nonzero frame rates; original counts are retained and cannot establish frame coverage. Use explicit isolated_copy attach with animation_hitches for the separate frame_lifetime_ms metric"
            ]
            if observations.get("zero_frame_count_with_nonzero_rate")
            else []
        ),
    ]
    return parsed, interchange["source"]


def bounded_command(args, output, control, timeout):
    """Redirect to owned files, enforce byte/time caps, never expose raw test logs."""
    with tempfile.TemporaryFile() as errors, output.open("wb") as stream:
        from iatest.execution.isolation import stop_worker, worker_environment

        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=errors,
            start_new_session=os.name == "posix",
            env=worker_environment({}, output.parent),
        )
        import time

        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                control.check()
                if (
                    time.monotonic() >= deadline
                    or output.stat().st_size + os.fstat(errors.fileno()).st_size
                    > 16777216
                ):
                    raise ValueError("XCTest host command exceeded time/output bounds")
                control.wait(0.05)
            if process.returncode or output.stat().st_size > 16777216:
                raise ValueError(
                    "XCTest host command failed; raw logs are not included in reports"
                )
        finally:
            stop_worker(process, 0.5)


def read_xcresult(path, spec, control=None):
    path = Path(path).expanduser().resolve()
    if not path.is_dir() or path.suffix != ".xcresult":
        raise ValueError("Expected a local xcresult bundle")
    with tempfile.TemporaryDirectory(prefix="iatest-xcresult-") as temporary:
        output = Path(temporary) / "metrics.json"
        bounded_command(
            [
                "xcrun",
                "xcresulttool",
                "get",
                "test-results",
                "metrics",
                "--path",
                str(path),
                "--test-id",
                spec["test_id"],
                "--compact",
                "--schema-version",
                "0.4.0",
            ],
            output,
            control or RunControl(timeout_seconds=30),
            30,
        )
        content = output.read_bytes()
        parsed, source = parse_xcresult(json.loads(content), spec)
        return parsed, source, hashlib.sha256(content).hexdigest()


MEASUREMENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["profile", "mode", "configuration_id", "bindings"],
    "properties": {
        "profile": {"type": "string", "minLength": 1},
        "mode": {"enum": ["launch", "scroll"]},
        "configuration_id": {"type": "string", "minLength": 1},
        "test_id": {"type": "string", "minLength": 1, "maxLength": 500},
        "bindings": BINDINGS_SCHEMA,
        **{
            key: {"type": "string", "minLength": 1, "maxLength": 500}
            for key in ("target_identifier", "origin_identifier", "reset_identifier")
        },
        "iterations": {"type": "integer", "minimum": 1, "maximum": 10},
        "max_reset_swipes": {"type": "integer", "minimum": 1, "maximum": 5},
        "hitch_collector": {"enum": ["signpost", "application"]},
        "calibration_id": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,80}$"},
    },
}


def scroll_evidence(content, measurement_id, iterations, *, calibration_positive=None):
    rows = []
    for value in re.findall(r"IATestScrollEvidence (\{[^\n]*\})", content):
        row = json.loads(value)
        if row.get("measurement_id") != measurement_id:
            continue
        if row.get("origin_restored") is not True or row.get("moved") is not True:
            raise ValueError("The measured scroll workload was not verified")
        for key in ("start_x", "start_y", "end_x", "end_y"):
            number = row.get(key)
            if number is None and key.startswith("end_"):
                continue
            if type(number) not in (int, float) or not math.isfinite(number):
                raise ValueError("Invalid scroll geometry evidence")
        if calibration_positive is not None and row.get("injected_stalls") != (
            3 if calibration_positive else 0
        ):
            raise ValueError(
                "The owned Debug Lab did not prove the calibration injection"
            )
        rows.append(row)
    if len(rows) != iterations + 1 or [r.get("invocation") for r in rows] != list(
        range(iterations + 1)
    ):
        raise ValueError("Scroll evidence is incomplete; rebuild the current Runner")
    if any(
        abs(r[axis] - rows[0][axis]) > 1
        for r in rows
        for axis in ("start_x", "start_y")
    ):
        raise ValueError("Scroll iterations did not restore the same origin")
    return rows


def execute_measurement(
    root, project, spec, control=None, *, calibration_positive=None
):
    from iatest.mobile.ios_measurement_scope import digest, measurement_scope
    from iatest.mobile.lease import device_scope
    from iatest.mobile.store import MobileStore

    validate(spec, MEASUREMENT_SCHEMA)
    if spec["mode"] == "scroll" and "target_identifier" not in spec:
        raise ValueError(
            "Scroll measurement requires an explicit accessibility identifier"
        )
    if spec["mode"] != "scroll" and spec.get("calibration_id"):
        raise ValueError("Collector calibration applies only to scroll measurements")
    profile = MobileStore(root, project).get("profile", spec["profile"])
    if profile["platform"] != "ios":
        raise ValueError("XCTest measurement requires an iOS profile")
    if calibration_positive is not None and (
        profile["app_id"] != "dev.iatest.mobile.lab" or spec["mode"] != "scroll"
    ):
        raise ValueError("Injected calibration is restricted to the owned Debug Lab")
    source = Path(profile.get("xctestrun", "")).expanduser().resolve()
    if (
        not source.is_file()
        or source.suffix != ".xctestrun"
        or source.stat().st_size > 1048576
    ):
        raise ValueError("A built and signed framework xctestrun is required")
    method = "testMeasureLaunch" if spec["mode"] == "launch" else "testMeasureScroll"
    test_id = spec.get("test_id", "MobileRunner/" + method)
    measurement_id = uuid.uuid4().hex
    env = {
        "IATEST_PERF_APP": profile["app_id"],
        "IATEST_PERF_TARGET": spec.get("target_identifier", ""),
        "IATEST_PERF_ORIGIN": spec.get("origin_identifier", ""),
        "IATEST_PERF_RESET": spec.get("reset_identifier", ""),
        "IATEST_PERF_MAX_RESETS": str(spec.get("max_reset_swipes", 3)),
        "IATEST_PERF_COLLECTOR": spec.get("hitch_collector", "signpost"),
        "IATEST_PERF_ITERATIONS": str(spec.get("iterations", 3)),
        "IATEST_PERF_MEASUREMENT_ID": measurement_id,
    }
    if calibration_positive is not None:
        env["IATEST_PERF_CALIBRATION"] = "1" if calibration_positive else "0"

    def rewrite(item):
        if isinstance(item, dict):
            value = {k: rewrite(v) for k, v in item.items()}
            if "TestBundlePath" in value:
                for key in ("EnvironmentVariables", "TestingEnvironmentVariables"):
                    value[key] = {**value.get(key, {}), **env}
                value["UITargetAppPerformanceAntipatternCheckerEnabled"] = False
            return value
        if isinstance(item, list):
            return [rewrite(v) for v in item]
        return (
            item.replace("__TESTROOT__", str(source.parent))
            if isinstance(item, str)
            else item
        )

    control = control or RunControl(timeout_seconds=240)
    with (
        device_scope(profile),
        tempfile.TemporaryDirectory(prefix="iatest-xctest-perf-") as temporary,
    ):
        scope = (
            measurement_scope(profile, spec, control)
            if spec["mode"] == "scroll"
            else None
        )
        if spec.get("calibration_id"):
            from iatest.mobile.ios_calibration import matching_calibration

            matching_calibration(root, project, spec["calibration_id"], scope)
        directory = Path(temporary)
        run_file, bundle = (
            directory / "performance.xctestrun",
            directory / "result.xcresult",
        )
        run_file.write_bytes(
            plistlib.dumps(rewrite(plistlib.loads(source.read_bytes())))
        )
        destination = (
            "platform=iOS Simulator,id="
            if profile.get("simulator")
            else "platform=iOS,id="
        ) + profile["device_id"]
        bounded_command(
            [
                "xcodebuild",
                "test-without-building",
                "-xctestrun",
                str(run_file),
                "-destination",
                destination,
                "-only-testing:IATestMobileTests/MobileRunner/" + method,
                "-resultBundlePath",
                str(bundle),
            ],
            directory / "build.log",
            control,
            200,
        )
        parsed, metadata, fingerprint = read_xcresult(
            bundle,
            {
                "app_id": profile["app_id"],
                "device_id": profile["device_id"],
                "test_id": test_id,
                "configuration_id": spec["configuration_id"],
                "bindings": spec["bindings"],
            },
            control,
        )
        metadata.update(
            platform="ios",
            app_id=profile["app_id"],
            trust="xcresult device/test/configuration matched; framework Runner executed",
        )
        if scope is not None:
            evidence = scroll_evidence(
                (directory / "build.log").read_text(errors="replace"),
                measurement_id,
                spec.get("iterations", 3),
                calibration_positive=calibration_positive,
            )
            if measurement_scope(profile, spec, control) != scope:
                raise ValueError(
                    "App/device/toolchain/Runner changed during measurement"
                )
            metadata.update(calibration_scope=scope, scroll_workload=evidence)
            fingerprint = digest(
                {"metrics": fingerprint, "scope": scope, "workload": evidence}
            )
        return parsed, metadata, fingerprint


def run_measurement(root, project, spec, control=None):
    from iatest.application.ios_measurements import save_measurement

    parsed, metadata, fingerprint = execute_measurement(root, project, spec, control)
    if spec.get("calibration_id"):
        from iatest.mobile.ios_calibration import apply_calibration

        apply_calibration(root, project, parsed, metadata, spec["calibration_id"])
    return save_measurement(root, project, parsed, metadata, source_digest=fingerprint)
