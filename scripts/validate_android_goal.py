"""Configured-model exploration, independent replay and reuse on the owned Android Lab."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from iatest.application.ui_exploration import explore_ui_goal
from iatest.core.env import load_dotenv
from iatest.execution.control import RunControl
from iatest.mobile.runtime import MobileSession
from iatest.mobile.store import MobileStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--profile", default="android-lab")
    parser.add_argument("--token-budget", type=int, default=48000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1000 <= args.token_budget <= 200000:
        parser.error("Token budget must be 1000–200000")
    root = Path.cwd()
    load_dotenv(root)
    profile = MobileStore(root, args.project).get("profile", args.profile)
    if profile["platform"] != "android" or profile["app_id"] != "dev.iatest.mobile.lab":
        parser.error("This validator only operates the framework-owned Android Lab")
    session = MobileSession(profile, control=RunControl(timeout_seconds=20))
    try:
        session.action({"op": "launch"}, None, "prepare", 8)
    finally:
        session.close()
    goal = "验证昵称保存后在 App 重启后持久化：把昵称改为随机 nickname 并保存，确认保存成功；终止并重新启动 App 后，昵称字段仍然等于 nickname。自行探索原生界面的操作路径，不打开 WebView，不修改权限或剪贴板。"
    context = {
        "profile": args.profile,
        "timeout_seconds": 480,
        "max_actions": 40,
        "max_queries": 200,
        "token_budget": args.token_budget,
        "required_steps": ["text", "tap", "terminate", "launch"],
        "rules": {
            "persistence": "昵称保存成功后，终止并重新启动 App，昵称字段必须仍然等于 nickname。"
        },
        "data_generation": {
            "nickname": {
                "kind": "string",
                "minLength": 16,
                "maxLength": 20,
                "prefix": "android-",
                "unique": True,
            }
        },
    }
    report = {"physical_device": True, "synthetic_device": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        case = explore_ui_goal(root, args.project, "android", goal, context, force=True)
        report["case"] = case
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "status": case["status"],
                    "error": case.get("error", ""),
                    "usage": case.get("usage", {}),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        assert case["status"] == "reproducible_passed", case.get("error")
        assert (
            len({run["runtime_data"]["namespace"] for run in case["verification"]}) == 2
        )
        reused = explore_ui_goal(root, args.project, "android", goal, context)
        report["reuse"] = reused
        assert reused["status"] == "reproducible_passed" and reused["model_calls"] == 0
        report["passed"] = True
    finally:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print("Physical Android exploration and independent reuse passed", flush=True)


if __name__ == "__main__":
    main()
