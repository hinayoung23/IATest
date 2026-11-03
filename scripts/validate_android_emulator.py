"""Real fresh-VM UI validation and data separation using the owned Android Lab."""

import argparse
import copy
import json
import os
import time
from pathlib import Path

from iatest.application.testing_service import execute_test_plan
from iatest.core.config import init_project
from iatest.engines.store import EngineStore
from iatest.mobile.delivery import fetch_app
from iatest.mobile.store import MobileStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", required=True, type=Path)
    parser.add_argument("--apk", required=True, type=Path)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--image", default="android-36;google_apis;arm64-v8a")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--project", default="emulator-acceptance")
    parser.add_argument("--java-home", type=Path)
    parser.add_argument("--client-performance", action="store_true")
    parser.add_argument("--base-apk", type=Path, help="Owned older build for upgrade/data-preservation acceptance")
    args = parser.parse_args()
    if args.java_home:
        os.environ["JAVA_HOME"] = str(args.java_home.resolve(strict=True))
    root = Path.cwd()
    init_project(root, args.project)
    app = fetch_app(
        root,
        args.project,
        {
            "id": "emulator-lab",
            "platform": "android",
            "app_id": "dev.iatest.mobile.lab",
            "path": str(args.apk.resolve()),
        },
    )
    profile = {
        "id": "managed-lab",
        "platform": "android",
        "device_id": "managed",
        "app_id": app["metadata"]["app_id"],
        "artifact_dir": str(args.artifacts.resolve()),
        "require_os_sandbox": True,
        "managed_emulator": {
            "sdk_root": str(args.sdk.resolve()),
            "system_image": args.image,
            "artifact_id": app["id"],
        },
    }
    MobileStore(root, args.project).save_profile(profile)
    target = lambda name: {"identifier": profile["app_id"] + ":id/" + name}
    plan = {
        "id": "fresh-vm-state",
        "kind": "android",
        "budget": {"timeout_seconds": 300},
        "spec": {
            "profile": profile["id"],
            "steps": [
                {"op": "launch"},
                {
                    "op": "wait",
                    "target": target("saved_status"),
                    "attribute": "text",
                    "expected": "Not saved",
                    "timeout_ms": 10000,
                },
                {
                    "op": "wait",
                    "target": target("nickname_field"),
                    "attribute": "enabled",
                    "expected": True,
                    "timeout_ms": 10000,
                },
                {
                    "op": "text",
                    "target": target("nickname_field"),
                    "value": "owned-vm-state",
                    "mode": "semantic",
                },
                {"op": "tap", "target": target("save_button")},
                {
                    "op": "expect",
                    "target": target("saved_status"),
                    "attribute": "text",
                    "expected": "Saved: owned-vm-state",
                },
            ],
        },
    }
    if args.client_performance:
        if args.base_apk:
            base = fetch_app(root, args.project, {
                "id": "emulator-base", "platform": "android", "app_id": profile["app_id"],
                "path": str(args.base_apk.resolve()),
            })
            seed = copy.deepcopy(plan)
            seed["id"] = "upgrade-seed"
            EngineStore(root, args.project).save_plan(seed)
            plan["spec"]["steps"][1]["expected"] = "Saved: owned-vm-state"
        measured = {
            "id": "vm-client-lifecycle", "kind": "client_performance",
            "budget": {"timeout_seconds": 480},
            "preparation": {"app": {"profile": profile["id"], "artifact_id": app["id"], "mode": "fresh"}},
            "spec": {
                "platform": "android", "iterations": 2,
                "thresholds": {"max_memory_pss_kb": 1000000}, "plan": plan,
            },
        }
        if args.base_apk:
            measured["preparation"]["app"].update(mode="upgrade", base_artifact_id=base["id"], seed_plan_id=seed["id"])
        else:
            # Both iterations run in the same VM, so assert a stable final value.
            plan["spec"]["steps"] = plan["spec"]["steps"][:1] + plan["spec"]["steps"][2:]
        results = [execute_test_plan(root, args.project, measured)]
    else:
        results = [execute_test_plan(root, args.project, plan) for _ in range(2)]
    states = [r.get("metrics", {}).get("os_sandbox", {}) for r in results]
    passed = all(r["verdict"] == "passed" for r in results)
    passed &= len({s.get("name") for s in states}) == len(results)
    if args.client_performance:
        passed &= len(results[0].get("metrics", {}).get("repetitions", [])) == 2
        passed &= results[0].get("metrics", {}).get("preparation", {}).get("state") == "prepared"
    passed &= all(
        s.get("process_stopped") and s.get("userdata_removed") for s in states
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        args.output.with_name(args.output.stem + "-attempt-" + str(time.time_ns()) + args.output.suffix).write_bytes(args.output.read_bytes())
    args.output.write_text(
        json.dumps(
            {
                "passed": passed,
                "emulator": True,
                "physical_device": False,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    print(json.dumps({"passed": passed, "output": str(args.output)}))
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
