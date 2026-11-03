"""Real Chromium + HTTP + optional configured-model end-to-end acceptance."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from iatest.application.testing_service import draft_test_plan, execute_test_plan
from iatest.core.config import init_project, load_config
from iatest.core.env import load_dotenv
from iatest.engines.web import BrowserSession
from iatest.execution.control import RunControl
from examples.automation_lab import create_server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Also call the configured real planning model",
    )
    parser.add_argument("--browser", default=os.environ.get("IATEST_CHROMIUM_PATH", ""))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/private/tmp/iatest-multitype-acceptance.json"),
    )
    args = parser.parse_args()
    if args.browser:
        os.environ["IATEST_CHROMIUM_PATH"] = args.browser
    load_dotenv(Path.cwd())
    config = load_config(Path.cwd())
    server = create_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    report = {
        "environment": "Actual local Chromium and HTTP lab; no phone",
        "results": {},
        "model": {},
    }
    try:
        with tempfile.TemporaryDirectory(prefix="iatest-multitype-") as directory:
            root = Path(directory)
            init_project(root, "acceptance")

            def run(kind, spec):
                result = execute_test_plan(
                    root,
                    "acceptance",
                    {
                        "id": "lab-" + kind,
                        "kind": kind,
                        "budget": {"timeout_seconds": 60, "max_requests": 200},
                        "spec": spec,
                    },
                )
                report["results"][kind] = result
                assert result["verdict"] == "passed", result
                return result

            web_spec = {
                "allowed_origins": [base],
                "start_url": base + "/ui",
                "steps": [
                    {"op": "fill", "target": {"label": "Username"}, "value": "Tester"},
                    {"op": "click", "target": {"role": "button", "name": "Sign in"}},
                    {
                        "op": "expect",
                        "target": {"id": "status"},
                        "assert": "text",
                        "expected": "Hello Tester",
                    },
                    {"op": "screenshot"},
                ],
            }
            run("web", web_spec)
            run(
                "performance",
                {
                    "allowed_origins": [base],
                    "mode": "arrival_rate",
                    "duration_seconds": 1,
                    "users": 4,
                    "rate": 10,
                    "request": {"url": base + "/health"},
                    "thresholds": {
                        "p95_ms": 500,
                        "max_error_ratio": 0,
                        "max_dropped_ratio": 0,
                    },
                },
            )
            security = {
                "allowed_origins": [base],
                "identities": {
                    "owner": {"Authorization": "Bearer lab-owner"},
                    "other": {"Authorization": "Bearer lab-other"},
                },
                "checks": [
                    {
                        "id": "authorization",
                        "rule": "authorization",
                        "owner": "owner",
                        "challenger": "other",
                        "request": {"url": base + "/secure-order"},
                        "marker": {"path": "$.id", "expected": "order-1"},
                    }
                ],
            }
            run("security", security)
            security["checks"][0]["request"]["url"] = base + "/vulnerable-order"
            vulnerable = execute_test_plan(
                root,
                "acceptance",
                {"id": "known-vulnerable", "kind": "security", "spec": security},
            )
            assert (
                vulnerable["verdict"] == "failed"
                and vulnerable["metrics"]["findings"] == 1
            )
            report["results"]["vulnerable_control"] = vulnerable
            prepare = {
                "id": "prepare",
                "kind": "api",
                "spec": {
                    "allowed_origins": [base],
                    "requests": [{"url": base + "/order"}],
                    "exports": {
                        "order": {"path": "$.id", "type": "string"},
                        "url": {"path": "$.url", "type": "string"},
                    },
                },
            }
            verify = {
                "id": "verify",
                "kind": "web",
                "spec": {
                    "allowed_origins": [base],
                    "start_url": {
                        "$from": "prepare.url",
                        "type": "string",
                        "sensitive": False,
                    },
                    "steps": [
                        {
                            "op": "expect",
                            "assert": "text",
                            "target": {"id": "order-detail"},
                            "expected": {
                                "$from": "prepare.order",
                                "type": "string",
                                "sensitive": False,
                            },
                        }
                    ],
                },
            }
            cleanup = {
                "id": "cleanup",
                "kind": "api",
                "spec": {
                    "allowed_origins": [base],
                    "requests": [{"url": base + "/cleanup", "method": "POST"}],
                },
            }
            run(
                "workflow",
                {
                    "nodes": [
                        {"id": "prepare", "plan": prepare},
                        {"id": "verify", "plan": verify, "depends_on": ["prepare"]},
                        {
                            "id": "cleanup",
                            "plan": cleanup,
                            "depends_on": ["verify"],
                            "always": True,
                            "resource_owner": "prepare",
                        },
                    ]
                },
            )
            # Forty bounded native query tasks; not a token/resource comparison baseline.
            with_queries = BrowserSession(
                root, "acceptance", web_spec, RunControl(timeout_seconds=60)
            )
            tasks = []
            try:
                for i in range(40):
                    targets = [
                        {"id": "login"},
                        {"test_id": "username"},
                        {"label": "Username"},
                        {"role": "button", "name": "Sign in"},
                        {
                            "selector": "button",
                            "scope": "#orders",
                            "anchor": {"selector": ".order-id", "text": "order-2"},
                        },
                        {"id": "missing-" + str(i)},
                        {"text": "Details"},
                        {"selector": "#status"},
                    ]
                    target = targets[i % len(targets)]
                    result = with_queries.query(target)
                    expected = i % 8 not in {5, 6}
                    assert result["search_complete"] and result["unique"] == expected
                    tasks.append(
                        {
                            "target": target,
                            "unique": result["unique"],
                            "query_bytes": len(json.dumps(result).encode()),
                            "duration_ms": result["duration_ms"],
                        }
                    )
            finally:
                with_queries.close()
            report["query_tasks"] = tasks
            if args.live:
                cases = [
                    (
                        "web",
                        "输入 Username 为 Tester，点击 Sign in，验证 #status 文本精确等于 Hello Tester。定位前只观察必要目标。",
                        {"allowed_origins": [base], "start_url": base + "/ui"},
                    ),
                    (
                        "performance",
                        "对测试地址进行单机固定到达率性能测试：1 秒，10 req/s，最多 4 并发，请求预算 100。P95 不超过 500 ms，错误率与丢弃率均为 0。",
                        {"allowed_origins": [base], "request_url": base + "/health"},
                    ),
                    (
                        "security",
                        "验证资源所有者成功访问订单，另一身份应被拒绝。必须使用返回 id=order-1 作为同一资源的对照证据，只做 GET，不增加其他检查。",
                        {
                            "allowed_origins": [base],
                            "request_url": base + "/secure-order",
                            "owner_env": "IATEST_LAB_OWNER",
                            "challenger_env": "IATEST_LAB_OTHER",
                            "marker": {"path": "$.id", "expected": "order-1"},
                        },
                    ),
                ]
                with patch.dict(
                    os.environ,
                    {
                        "IATEST_LAB_OWNER": "Bearer lab-owner",
                        "IATEST_LAB_OTHER": "Bearer lab-other",
                    },
                ):
                    for kind, goal, context in cases:
                        print(
                            "Calling actual configured planning model: " + kind,
                            flush=True,
                        )
                        with patch(
                            "iatest.core.config.load_config", return_value=config
                        ):
                            drafted = draft_test_plan(
                                root, "acceptance", kind, goal, context
                            )
                        executed = execute_test_plan(
                            root, "acceptance", drafted["plan"]
                        )
                        report["model"][kind] = {**drafted, "execution": executed}
                        args.output.write_text(
                            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
                        )
                        assert executed["verdict"] == "passed", executed
                        if kind == "web":
                            assert any(
                                s.get("expected") == "Hello Tester"
                                for s in drafted["plan"]["spec"]["steps"]
                            )
            report["passed"] = True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "passed": report.get("passed", False),
                "native_query_tasks": len(report.get("query_tasks", [])),
                "results": {k: v["verdict"] for k, v in report["results"].items()},
                "model_usage": {k: v["usage"] for k, v in report["model"].items()},
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
