"""Physical owned-Lab discovery, SDK telemetry and goal exploration acceptance."""

import argparse
import json
import time
import zipfile
from pathlib import Path

from iatest.application.testing_service import execute_test_plan
from iatest.application.ui_exploration import explore_ui_goal
from iatest.core.config import init_project
from iatest.core.env import load_dotenv
from iatest.execution.control import RunControl
from iatest.mobile.bridge import read_bridge
from iatest.mobile.delivery import fetch_app
from iatest.mobile.runtime import MobileSession
from iatest.mobile.store import MobileStore
from iatest.mobile.telemetry import measurements


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["outline", "telemetry", "goal"])
    parser.add_argument("--device", required=True)
    parser.add_argument("--xctestrun", type=Path, required=True)
    parser.add_argument("--lab", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--project", default="ios-completion-acceptance")
    parser.add_argument("--token-budget", type=int, default=64000)
    parser.add_argument("--metal", action="store_true", help="Require registered Metal GPU busy interval evidence")
    args = parser.parse_args()
    root = Path.cwd()
    load_dotenv(root)
    init_project(root, args.project)
    profile = {
        "id": "ios-completion",
        "platform": "ios",
        "device_id": args.device,
        "app_id": "dev.iatest.mobile.lab",
        "xctestrun": str(args.xctestrun.resolve()),
    }
    MobileStore(root, args.project).save_profile(profile)
    control = RunControl(timeout_seconds=240)
    session = MobileSession(profile, root=root, project=args.project, control=control)
    before = time.monotonic()
    result = {}
    try:
        session.action(
            {"op": "launch"}, None, operation_id="prepare-completion-lab", timeout=30
        )
        if args.mode == "outline":
            start = time.monotonic()
            value = session.outline(timeout=5)
            result = {
                "physical_device": True,
                "elapsed_ms": (time.monotonic() - start) * 1000,
                "observation": value,
            }
        elif args.mode == "telemetry":
            if not args.lab or not args.lab.is_dir():
                raise ValueError(
                    "Telemetry acceptance requires the exact signed Lab .app"
                )
            if args.metal:
                button = session.query({"identifier": "metal_gpu_toggle"}, timeout=5)
                session.action({"op": "tap"}, button, operation_id="enable-owned-metal-workload", timeout=20)
            archive = args.output.with_suffix(".ipa")
            archive.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as target:
                for path in args.lab.rglob("*"):
                    if path.is_file():
                        target.write(
                            path,
                            "Payload/"
                            + args.lab.name
                            + "/"
                            + path.relative_to(args.lab).as_posix(),
                        )
            artifact = fetch_app(
                root,
                args.project,
                {
                    "platform": "ios",
                    "app_id": profile["app_id"],
                    "path": str(archive.resolve()),
                },
            )
            first = read_bridge(
                root,
                args.project,
                profile["id"],
                artifact["id"],
                session=session,
                control=control,
            )
            control.wait(0.3)
            last = read_bridge(
                root,
                args.project,
                profile["id"],
                artifact["id"],
                session=session,
                control=control,
            )
            observed = measurements(first, last)
            result = {
                "artifact_id": artifact["id"],
                "physical_device": True,
                "observations": [first, last],
                "metrics": observed,
            }
            if args.metal:
                button = session.query({"identifier": "metal_gpu_toggle"}, timeout=5)
                session.action({"op": "tap"}, button, operation_id="disable-owned-metal-workload", timeout=20)
    finally:
        session.close()
    if args.mode == "goal":
        result = explore_ui_goal(
            root,
            args.project,
            "ios",
            "重置 Lab 实验数据后，保存昵称 nickname；重启 App 后昵称字段的值应等于 nickname",
            {
                "profile": profile["id"],
                "data_generation": {
                    "nickname": {
                        "kind": "string",
                        "minLength": 16,
                        "maxLength": 20,
                        "unique": True,
                    }
                },
                "required_steps": ["terminate", "launch"],
                "max_actions": 12,
                "max_queries": 100,
                "timeout_seconds": 360,
                "token_budget": args.token_budget,
            },
            force=True,
        )
    elif args.mode == "telemetry":
        plan = {
            "id": "ios-sdk-gate",
            "kind": "client_performance",
            "spec": {
                "platform": "ios",
                "iterations": 1,
                "telemetry": {"artifact_id": result["artifact_id"]},
                "thresholds": {
                    "max_gpu_allocated_memory_kb": 1000000,
                    "max_device_thermal_status": 3,
                },
                "plan": {
                    "id": "sdk-journey",
                    "kind": "ios",
                    "spec": {
                        "profile": profile["id"],
                        "steps": [
                            {"op": "tap", "target": {"identifier": "reset_button"}},
                            {"op": "tap", "target": {"identifier": "option_toggle"}},
                            {
                                "op": "expect",
                                "target": {"identifier": "option_status"},
                                "attribute": "text",
                                "expected": "Option on",
                            },
                        ],
                    },
                },
            },
        }
        if args.metal:
            if not 0 < result["metrics"].get("app_metal_gpu_busy_ratio", 0) <= 1:
                raise ValueError("Owned GPU workload did not produce native Metal time evidence")
            plan["spec"]["thresholds"]["max_app_metal_gpu_busy_ratio"] = 1
            plan["spec"]["plan"]["spec"]["steps"].insert(0, {"op": "tap", "target": {"identifier": "metal_gpu_toggle"}})
            plan["spec"]["plan"]["spec"]["steps"].insert(-1, {"op": "tap", "target": {"identifier": "metal_gpu_toggle"}})
        result["gate"] = execute_test_plan(root, args.project, plan)
    result["acceptance_elapsed_seconds"] = time.monotonic() - before
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "mode": args.mode,
                "status": result.get("status"),
                "error": result.get("error"),
                "gate": result.get("gate", {}).get("verdict"),
                "metrics": result.get("metrics"),
                "elapsed_ms": result.get("elapsed_ms"),
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    if args.mode == "goal":
        return 0 if result["status"] == "reproducible_passed" else 1
    if args.mode == "telemetry":
        return 0 if result["gate"]["verdict"] == "passed" else 1
    return 0 if result.get("observation", {}).get("controls") else 1


if __name__ == "__main__":
    raise SystemExit(main())
