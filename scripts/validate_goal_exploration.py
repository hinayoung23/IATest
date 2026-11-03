"""Actual Chromium, HTTP and configured-model acceptance of exploration and analysis."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from iatest.application.engine_analysis import analyze_engine_run
from iatest.application.testing_service import execute_test_plan
from iatest.application.ui_exploration import explore_ui_goal
from iatest.core.config import init_project, load_config, save_config
from iatest.core.env import load_dotenv
from examples.ui_goal_lab import create_server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", default=os.environ.get("IATEST_CHROMIUM_PATH", ""))
    parser.add_argument(
        "--output", type=Path, default=Path("/private/tmp/iatest-goal-acceptance.json")
    )
    parser.add_argument(
        "--only", choices=("all", "analysis", "exploration", "broken"), default="all"
    )
    args = parser.parse_args()
    if args.browser:
        os.environ["IATEST_CHROMIUM_PATH"] = args.browser
    load_dotenv(Path.cwd())
    configured = load_config(Path.cwd())
    server = create_server()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    report = (
        json.loads(args.output.read_text())
        if args.only != "all" and args.output.exists()
        else {
            "environment": "Actual Chromium/HTTP/configured model; no phone",
            "analyses": [],
            "cases": [],
        }
    )
    if args.only == "exploration":
        report["cases"] = []
    elif args.only == "broken":
        report["cases"] = [
            r for r in report["cases"] if r["status"] == "reproducible_passed"
        ]
    try:
        with tempfile.TemporaryDirectory(prefix="iatest-goal-") as directory:
            root = Path(directory)
            init_project(root, "acceptance")
            config = load_config(root)
            config["llm"] = configured["llm"]
            config["agents"] = configured.get("agents", {})
            save_config(root, config)
            for kind, spec in (
                (
                    "performance",
                    {
                        "allowed_origins": [base],
                        "mode": "arrival_rate",
                        "duration_seconds": 1,
                        "users": 2,
                        "rate": 5,
                        "request": {"url": base + "/state"},
                        "thresholds": {
                            "p95_ms": 500,
                            "max_error_ratio": 0,
                            "max_dropped_ratio": 0,
                        },
                    },
                ),
                (
                    "security",
                    {
                        "allowed_origins": [base],
                        "checks": [
                            {
                                "id": "headers",
                                "rule": "response_headers",
                                "request": {"url": base + "/state"},
                                "required_headers": {
                                    "x-content-type-options": "nosniff"
                                },
                            }
                        ],
                    },
                ),
            ):
                if args.only in {"exploration", "broken"}:
                    continue
                run = execute_test_plan(
                    root,
                    "acceptance",
                    {"id": "goal-" + kind, "kind": kind, "spec": spec},
                )
                print(kind + " executed: " + run["verdict"], flush=True)
                analysis = analyze_engine_run(root, "acceptance", run["run_id"])
                report["analyses"].append(analysis)
                assert (
                    analysis["analysis"]["recommendations"]
                    and analysis["original_verdict"] == run["verdict"]
                )
                print(kind + " real-model analysis accepted", flush=True)
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
            goal = "验证昵称修改后持久化：把昵称改为随机 nickname 并保存，刷新页面后昵称仍然等于 nickname。请自行探索操作路径。"
            context = {
                "allowed_origins": [base],
                "start_url": base + "/ui",
                "timeout_seconds": 360,
                "data_generation": {
                    "nickname": {
                        "kind": "string",
                        "minLength": 16,
                        "maxLength": 20,
                        "unique": True,
                        "prefix": "test-",
                    }
                },
                "cleanup_plan": {
                    "id": "cleanup",
                    "kind": "api",
                    "spec": {
                        "allowed_origins": [base],
                        "requests": [{"url": base + "/cleanup", "method": "POST"}],
                    },
                },
            }
            for broken in (False, True):
                if args.only == "analysis":
                    continue
                if args.only == "broken" and not broken:
                    continue
                active_context = {
                    **context,
                    "start_url": base + ("/ui?broken=1" if broken else "/ui"),
                }
                case = explore_ui_goal(root, "acceptance", "web", goal, active_context)
                report["cases"].append(case)
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
                print(
                    "Actual goal exploration: "
                    + case["status"]
                    + " "
                    + case.get("error", ""),
                    flush=True,
                )
                assert case["status"] == (
                    "reproducible_failed" if broken else "reproducible_passed"
                ), case.get("error")
                namespaces = [
                    r["runtime_data"]["namespace"] for r in case["verification"]
                ]
                assert len(set(namespaces)) == 2
                assert any(
                    r["event"] == "action" and r["step"]["op"] == "reload"
                    for r in case["trace"]
                )
                if not broken:
                    reused = explore_ui_goal(
                        root, "acceptance", "web", goal, active_context
                    )
                    assert (
                        reused["model_calls"] == 0
                        and reused["status"] == "reproducible_passed"
                    )
                    report["reuse"] = {
                        k: reused[k]
                        for k in (
                            "status",
                            "model_calls",
                            "reused_from",
                            "verification",
                        )
                    }
                    print("Exact-context reuse: zero model calls", flush=True)
    finally:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        server.shutdown()
        server.server_close()
    print("Acceptance report: " + str(args.output), flush=True)


if __name__ == "__main__":
    main()
