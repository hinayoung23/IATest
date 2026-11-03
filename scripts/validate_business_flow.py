"""Actual Chromium, two worker processes, native APK inspection and optional real AI."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from iatest.application.engine_analysis import analyze_engine_run
from iatest.application.testing_service import execute_test_plan
from iatest.core.config import init_project, load_config, save_config
from iatest.core.env import load_dotenv
from iatest.engines.store import EngineStore
from iatest.mobile.delivery import fetch_app, file_hash
from iatest.nodes.protocol import make_controller
from iatest.nodes.services import close_all
from iatest.nodes.store import ControllerStore
from iatest.web.server import make_handler
from examples.ui_goal_lab import create_server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--real-model", action="store_true")
    parser.add_argument("--apk", type=Path)
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    load_dotenv(repository)
    configured = load_config(repository)
    report = {"mobile_device_actions": 0, "production_dependencies_added": 0}
    with tempfile.TemporaryDirectory(prefix="iatest-business-") as directory:
        root, project = Path(directory) / "controller", "business-smoke"
        init_project(root, project)
        config = load_config(root)
        config["llm"], config["agents"] = (
            configured["llm"],
            configured.get("agents", {}),
        )
        save_config(root, config)
        lab = create_server()
        console = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(root, project))
        controller = make_controller(root, project)
        servers = [lab, console, controller]
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        workers = []
        try:
            base = f"http://127.0.0.1:{lab.server_port}"
            control_url = f"http://127.0.0.1:{controller.server_port}"
            nodes = ControllerStore(root, project)
            for node_id in ("a", "b"):
                local = Path(directory) / node_id
                init_project(local, project)
                token_env = "IATEST_ACCEPTANCE_NODE_" + node_id.upper()
                # Test-process-only credentials; never write project secrets or print values.
                os.environ[token_env] = "controlled-smoke-" + os.urandom(20).hex()
                nodes.enroll(
                    {
                        "id": node_id,
                        "token_env": token_env,
                        "allowed_origins": [base],
                        "allowed_kinds": ["api", "performance"],
                    }
                )
                env = {**os.environ, "PYTHONPATH": str(repository)}
                workers.append(
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "iatest",
                            "node",
                            "worker",
                            control_url,
                            node_id,
                            "--project",
                            project,
                            "--token-env",
                            token_env,
                            "--origin",
                            base,
                            "--kinds",
                            "api,performance",
                        ],
                        cwd=local,
                        env=env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                    )
                )
            deadline = time.monotonic() + 15
            while not all(n["online"] for n in nodes.nodes()):
                if time.monotonic() > deadline or any(
                    p.poll() is not None for p in workers
                ):
                    raise RuntimeError("Acceptance worker failed to register")
                time.sleep(0.05)
            distributed = {
                "id": "lan-transactions",
                "kind": "performance",
                "budget": {"timeout_seconds": 20, "max_requests": 100},
                "spec": {
                    "allowed_origins": [base],
                    "mode": "arrival_rate",
                    "rate": 10,
                    "rate_unit": "transactions",
                    "users": 2,
                    "duration_seconds": 1,
                    "scenarios": [
                        {
                            "id": "read-profile",
                            "steps": [
                                {
                                    "id": "read",
                                    "request": {"url": base + "/state"},
                                    "extract": {"nickname": "$.nickname"},
                                },
                                {
                                    "id": "verify",
                                    "request": {"url": base + "/state"},
                                    "assertions": [
                                        {
                                            "path": "$.nickname",
                                            "operator": "eq",
                                            "expected": "{{nickname}}",
                                        }
                                    ],
                                },
                            ],
                        }
                    ],
                    "thresholds": {
                        "max_error_ratio": 0,
                        "max_transaction_error_ratio": 0,
                        "max_dropped_ratio": 0,
                    },
                    "distribution": {"nodes": ["a", "b"]},
                },
            }
            cluster = execute_test_plan(root, project, distributed)
            assert cluster["verdict"] == "passed", cluster
            assert (
                cluster["metrics"]["transactions"] == 10
                and cluster["metrics"]["completed"] == 20
            ), cluster
            report["two_process_cluster"] = {
                "verdict": cluster["verdict"],
                "worker_pids": [p.pid for p in workers],
                "transactions": 10,
                "requests": 20,
                "p95_ms": cluster["metrics"]["p95_ms"],
                "coverage_complete": cluster["metrics"]["coverage_complete"],
            }
            child = {
                "id": "heading",
                "kind": "web",
                "spec": {
                    "start_url": base + "/ui",
                    "allowed_origins": [base],
                    "steps": [
                        {
                            "op": "expect",
                            "target": {"selector": "h1"},
                            "assert": "text",
                            "expected": "Account",
                        },
                        {"op": "screenshot"},
                    ],
                },
            }
            store = EngineStore(root, project)
            store.save_plan(
                {
                    "id": "api-smoke",
                    "kind": "api",
                    "spec": {
                        "allowed_origins": [base],
                        "requests": [{"url": base + "/state"}],
                    },
                }
            )
            client = {
                "id": "client-smoke",
                "kind": "client_performance",
                "spec": {
                    "platform": "web",
                    "plan": child,
                    "iterations": 2,
                    "warmup": 1,
                    "thresholds": {
                        "max_navigation_ms": 5000,
                        "max_action_max_ms": 5000,
                    },
                },
            }
            store.save_plan(client)
            result = execute_test_plan(root, project, client)
            assert result["verdict"] == "passed", result
            report["client"] = {
                "verdict": result["verdict"],
                "measurements": result["metrics"]["measurements"],
                "evidence_count": len(result["evidence"]),
            }

            def checkpoint():
                Path("/private/tmp/iatest-business-acceptance.json").write_text(
                    json.dumps(report, indent=2, ensure_ascii=False)
                )

            checkpoint()
            if args.real_model:
                try:
                    analysis = analyze_engine_run(root, project, result["run_id"])
                    assert store.get_result(result["run_id"])["verdict"] == "passed"
                    report["real_model"] = {
                        "artifact_id": analysis["id"],
                        "usage": analysis["usage"],
                        "recommendations": len(analysis["analysis"]["recommendations"]),
                        "original_verdict_preserved": True,
                    }
                except Exception as exc:  # noqa: BLE001 -- retain acceptance diagnostics before temporary cleanup
                    from iatest.data.agent_runs import AgentRunRepository

                    with AgentRunRepository(root, project).connection() as conn:
                        calls = [
                            dict(row)
                            for row in conn.execute(
                                "select name,status,error from agent_tool_calls"
                            )
                        ]
                        runs = [
                            dict(row)
                            for row in conn.execute(
                                "select role_id,total_tokens,turn_count,error from agent_runs"
                            )
                        ]
                    report["real_model"] = {
                        "error": str(exc),
                        "calls": calls,
                        "runs": runs,
                    }
                checkpoint()
                Path("/private/tmp/iatest-business-model.json").write_text(
                    json.dumps(report["real_model"], ensure_ascii=False, indent=2)
                )
            apk_spec = None
            if args.apk:
                apk = args.apk.resolve()

                class APKHandler(SimpleHTTPRequestHandler):
                    def __init__(self, *values, **kwargs):
                        super().__init__(*values, directory=str(apk.parent), **kwargs)

                    def log_message(self, *values):
                        pass

                packages = ThreadingHTTPServer(("127.0.0.1", 0), APKHandler)
                threading.Thread(target=packages.serve_forever, daemon=True).start()
                servers.append(packages)
                origin = f"http://127.0.0.1:{packages.server_port}"
                apk_spec = {
                    "id": "actual-probe",
                    "platform": "android",
                    "app_id": "dev.iatest.mobile",
                    "url": origin + "/" + apk.name,
                    "sha256": file_hash(apk),
                    "allowed_origins": [origin],
                }
                app = fetch_app(root, project, apk_spec)
                report["actual_apk"] = {
                    "sha256": app["sha256"],
                    "metadata": app["metadata"],
                    "bytes": app["bytes"],
                    "device_installation": "deferred",
                }
            environment = {
                **os.environ,
                "IATEST_SMOKE_URL": f"http://127.0.0.1:{console.server_port}",
                "IATEST_CASE_PLAN": json.dumps(child),
                "IATEST_APP_SPEC": json.dumps(apk_spec),
                "IATEST_CLIENT_PLAN": json.dumps(client),
            }
            node = shutil.which("node")
            environment.setdefault(
                "NODE_PATH", str(Path(node).resolve().parents[1] / "lib/node_modules")
            )
            subprocess.run(
                [node, str(Path(__file__).with_suffix(".cjs"))],
                env=environment,
                check=True,
                timeout=180,
            )
            report["console"] = json.loads(
                Path("/private/tmp/iatest-business-console.json").read_text()
            )
            report["passed"] = not report.get("real_model", {}).get("error")
            Path("/private/tmp/iatest-business-acceptance.json").write_text(
                json.dumps(report, indent=2, ensure_ascii=False)
            )
            print(json.dumps(report, ensure_ascii=False))
            if not report["passed"]:
                raise RuntimeError(
                    "Real model acceptance failed; diagnostic evidence was retained"
                )
        finally:
            for process in workers:
                process.terminate()
            for process in workers:
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
            close_all()
            for server in servers:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    main()
