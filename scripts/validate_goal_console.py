"""Console acceptance: cached UI case replay and real-model evidence analysis."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from iatest.application.testing_service import execute_test_plan
from iatest.application.ui_exploration import _context, ui_reuse_key, verify_ui_case
from iatest.core.config import init_project, load_config, save_config
from iatest.core.env import load_dotenv
from iatest.engines.store import EngineStore
from iatest.web.goal_workspace import exploration_payload
from iatest.web.server import make_handler
from examples.ui_goal_lab import create_server


def main():
    load_dotenv(Path.cwd())
    configured = load_config(Path.cwd())
    with tempfile.TemporaryDirectory(prefix="iatest-goal-console-") as directory:
        root, project = Path(directory), "console-smoke"
        init_project(root, project)
        config = load_config(root)
        if os.environ.get("IATEST_SKIP_ANALYSIS") != "1":
            config["llm"], config["agents"] = (
                configured["llm"],
                configured.get("agents", {}),
            )
        save_config(root, config)
        lab = create_server()
        console = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(root, project))
        for server in (lab, console):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{lab.server_port}"
            goal = "昵称修改并保存后，刷新页面仍等于 nickname"
            context = {
                "start_url": base + "/ui",
                "allowed_origins": [base],
                "data_generation": {
                    "nickname": {
                        "kind": "string",
                        "minLength": 16,
                        "maxLength": 20,
                        "unique": True,
                    }
                },
            }
            expected = goal
            payload = exploration_payload(
                root,
                project,
                {
                    "kind": ["web"],
                    "goal": [goal],
                    "expected_result": [expected],
                    "context": [json.dumps(context)],
                    "browser": ["chromium"],
                    "network_policy": ["strict"],
                    "origins": [""],
                },
            )
            normalized_goal, normalized_context = payload["goal"], payload["context"]
            plan = {
                "id": "console-goal",
                "kind": "web",
                "data_generation": context["data_generation"],
                "spec": {
                    "start_url": base + "/ui",
                    "allowed_origins": [base],
                    "steps": [
                        {"op": "click", "target": {"id": "settings"}},
                        {
                            "op": "fill",
                            "target": {"id": "nickname"},
                            "value": {"$value": "nickname"},
                        },
                        {"op": "click", "target": {"id": "save"}},
                        {"op": "reload"},
                        {"op": "click", "target": {"id": "settings"}},
                        {
                            "op": "expect",
                            "assert": "value",
                            "target": {"id": "nickname"},
                            "expected": {"$value": "nickname"},
                        },
                    ],
                },
            }
            status, verification = verify_ui_case(root, project, plan, context)
            assert status == "reproducible_passed", verification
            store = EngineStore(root, project)
            store.save_plan(plan)
            store.save_artifact(
                "ui_case",
                {
                    "id": "seed",
                    "goal": normalized_goal,
                    "kind": "web",
                    "status": status,
                    "plan": plan,
                    "intent": {"purpose": goal},
                    "verification": verification,
                    "reuse_key": ui_reuse_key(
                        root,
                        project,
                        normalized_goal,
                        _context("web", normalized_context),
                    ),
                },
                artifact_id="seed",
            )
            performance_plan = {
                "id": "console-perf",
                "kind": "performance",
                "spec": {
                    "allowed_origins": [base],
                    "mode": "users",
                    "duration_seconds": 0.2,
                    "users": 1,
                    "request": {"url": base + "/state"},
                    "thresholds": {"p95_ms": 500, "max_error_ratio": 0},
                },
            }
            store.save_plan(performance_plan)
            run = execute_test_plan(root, project, performance_plan)
            node = shutil.which("node")
            environment = dict(
                os.environ,
                IATEST_SMOKE_URL=f"http://127.0.0.1:{console.server_port}",
                IATEST_GOAL_CONTEXT=json.dumps(context),
                IATEST_GOAL=goal,
                IATEST_EXPECTED=expected,
                IATEST_ANALYSIS_RUN=run["run_id"],
            )
            environment.setdefault(
                "NODE_PATH", str(Path(node).resolve().parents[1] / "lib/node_modules")
            )
            subprocess.run(
                [node, str(Path(__file__).with_suffix(".cjs"))],
                check=True,
                env=environment,
                timeout=240,
            )
            cases = store.artifacts("ui_case")
            assert (
                cases[0]["model_calls"] == 0
                and cases[0]["status"] == "reproducible_passed"
            )
            if os.environ.get("IATEST_SKIP_ANALYSIS") != "1":
                assert store.artifacts("analysis")[0]["agent_run_id"]
        finally:
            for server in (lab, console):
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    main()
