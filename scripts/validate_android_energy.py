"""Real owned-Lab Android UID battery model and performance-gate acceptance.

Requires an actually unplugged phone; never resets statistics or fakes unplug.
Only the existing Lab is exercised, with the signature-compatible framework probe.
No Lab installation, data clear or model calls.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

from iatest.application.testing_service import execute_test_plan
from iatest.core.config import init_project
from iatest.execution.control import RunControl
from iatest.mobile.android_energy import snapshot
from iatest.mobile.devices import NativeDevice
from iatest.mobile.store import MobileStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--project", default="android-energy-acceptance")
    parser.add_argument("--cycles", type=int, default=5)
    parser.add_argument("--iterations", type=int, default=2)
    parser.add_argument("--max-mah", type=float, default=100)
    parser.add_argument("--expect-unavailable", action="store_true")
    parser.add_argument("--expect-threshold-failure", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.cycles <= 20 or not 1 <= args.iterations <= 5:
        raise ValueError("Select 1–20 animation cycles and 1–5 iterations")
    if (
        not math.isfinite(args.max_mah)
        or args.max_mah < 0
        or args.expect_unavailable
        and args.expect_threshold_failure
    ):
        raise ValueError(
            "Select a finite nonnegative threshold and one expected outcome"
        )
    root = Path.cwd()
    init_project(root, args.project)
    profile = {
        "id": "android-energy",
        "platform": "android",
        "device_id": args.serial,
        "app_id": "dev.iatest.mobile.lab",
        "artifact_dir": str(args.artifacts.resolve()),
    }
    MobileStore(root, args.project).save_profile(profile)
    device = NativeDevice(profile, "battery-validation")
    device.control = RunControl(timeout_seconds=30)
    baseline = {
        "forwards": device.adb("forward", "--list").splitlines(),
        "ime": device.adb("shell", "settings", "get", "secure", "default_input_method"),
    }
    component = "dev.iatest.mobile.lab/dev.iatest.lab.MainActivity"
    device.adb("shell", "am", "start", "-W", "-n", component, timeout=10)

    def observation():
        device.control = RunControl(timeout_seconds=30)
        try:
            return {"available": True, "value": snapshot(device)}
        except (OSError, RuntimeError, ValueError) as exc:
            return {"available": False, "reason": str(exc)}

    def target(name):
        return {"identifier": profile["app_id"] + ":id/" + name}

    steps = []
    for _ in range(args.cycles):
        steps.extend(
            [
                {"op": "tap", "target": target("jank_button")},
                {
                    "op": "wait",
                    "target": target("ready_label"),
                    "attribute": "text",
                    "expected": "Ready",
                    "timeout_ms": 10000,
                },
            ]
        )
    steps.append(
        {
            "op": "expect",
            "target": target("ready_label"),
            "attribute": "text",
            "expected": "Ready",
        }
    )
    plan = {
        "id": "android-uid-battery",
        "kind": "client_performance",
        "budget": {
            "timeout_seconds": min(1800, args.iterations * (args.cycles * 15 + 40))
        },
        "spec": {
            "platform": "android",
            "iterations": args.iterations,
            "thresholds": {"max_app_estimated_battery_mah": args.max_mah},
            "plan": {
                "id": "owned-battery-workload",
                "kind": "android",
                "budget": {
                    "timeout_seconds": args.cycles * 15 + 30,
                    "max_actions": args.cycles * 2 + 5,
                    "max_queries": 500,
                },
                "spec": {"profile": profile["id"], "steps": steps},
            },
        },
    }
    report = {
        "physical_device": True,
        "synthetic_battery_state": False,
        "scope": "Android system UID model, estimated charge in mAh",
        "plan": plan,
        "diagnostic_before": observation(),
    }
    try:
        result = execute_test_plan(root, args.project, plan)
        report["result"] = result
        metric = "app_estimated_battery_mah"
        if args.expect_unavailable:
            report["passed"] = (
                result["verdict"] == "inconclusive"
                and metric in result["metrics"].get("not_assessed", [])
                and all(
                    r["verdict"] == "passed"
                    for r in result["metrics"].get("repetitions", [])
                )
                and bool(result["metrics"].get("repetitions"))
                and all(
                    g["actual"] is None and not g["assessed"] for g in result["steps"]
                )
            )
        elif args.expect_threshold_failure:
            report["passed"] = (
                result["verdict"] == "failed"
                and bool(result["metrics"].get("repetitions"))
                and all(
                    r["verdict"] == "passed" for r in result["metrics"]["repetitions"]
                )
                and all(
                    g["assessed"] and g["actual"] > g["expected"] and not g["passed"]
                    for g in result["steps"]
                )
            )
        else:
            report["passed"] = result["verdict"] == "passed" and metric in result[
                "metrics"
            ].get("measurements", {})
    finally:
        report["diagnostic_after"] = observation()
        report["environment_restored"] = {
            "forwards": device.adb("forward", "--list").splitlines()
            == baseline["forwards"],
            "ime": device.adb(
                "shell", "settings", "get", "secure", "default_input_method"
            )
            == baseline["ime"],
        }
        report["passed"] = bool(report.get("passed")) and all(
            report["environment_restored"].values()
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.output.exists():
            args.output.with_name(
                args.output.stem
                + "-attempt-"
                + str(time.time_ns())
                + args.output.suffix
            ).write_bytes(args.output.read_bytes())
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "verdict": result["verdict"],
                "not_assessed": result["metrics"].get("not_assessed"),
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
