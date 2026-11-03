import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from iatest.application.testing_service import execute_test_plan
from iatest.core.config import init_project
from iatest.engines.contracts import ExecutionPlan
from iatest.engines.performance import validate_performance


@pytest.fixture
def root(tmp_path):
    init_project(tmp_path, "suite")
    return tmp_path


@pytest.fixture
def server():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            calls.append((self.path, self.headers.get("Authorization")))
            if self.path == "/login":
                body = {"session": "session-123"}
            elif self.path.startswith("/order/"):
                body = {"id": self.path.split("/")[-1], "ok": True}
            else:
                body = {"id": "order-123", "ok": self.path != "/business-failure"}
            encoded = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()


def plan(base, path="/create"):
    return {
        "id": "transactions",
        "kind": "performance",
        "spec": {
            "allowed_origins": [base],
            "mode": "arrival_rate",
            "rate": 2,
            "rate_unit": "transactions",
            "users": 2,
            "duration_seconds": 2,
            "thresholds": {
                "max_error_ratio": 0,
                "max_transaction_error_ratio": 0,
                "max_dropped_ratio": 0,
            },
            "scenarios": [
                {
                    "id": "order",
                    "setup": [
                        {
                            "id": "login",
                            "request": {"url": base + "/login"},
                            "extract": {"session": "$.session"},
                        }
                    ],
                    "steps": [
                        {
                            "id": "create",
                            "request": {
                                "url": base + path,
                                "headers": {"Authorization": "Bearer {{session}}"},
                            },
                            "extract": {"order": "$.id"},
                            "assertions": [
                                {"path": "$.ok", "operator": "eq", "expected": True}
                            ],
                        },
                        {
                            "id": "query",
                            "request": {"url": base + "/order/{{order}}"},
                            "assertions": [
                                {
                                    "path": "$.id",
                                    "operator": "eq",
                                    "expected": "{{order}}",
                                }
                            ],
                        },
                    ],
                }
            ],
        },
    }


def test_transactions_correlate_outputs_and_count_business_failures(root, server):
    base, calls = server
    good = execute_test_plan(root, "suite", plan(base))
    assert good["verdict"] == "passed", json.dumps(good, indent=2)
    assert good["metrics"]["completed"] == good["metrics"]["transactions"] * 2
    assert any(p == "/create" and auth == "Bearer session-123" for p, auth in calls)
    assert any(p == "/order/order-123" for p, _ in calls)
    bad = execute_test_plan(root, "suite", plan(base, "/business-failure"))
    assert bad["verdict"] == "failed", bad
    assert bad["metrics"]["error_ratio"] == 0
    assert bad["metrics"]["transaction_error_ratio"] == 1


def test_phase_schedule_and_units_are_explicit(root, server):
    value = plan(server[0])
    value["spec"]["phases"] = [
        {"duration_seconds": 1, "start_rate": 1, "end_rate": 1},
        {"duration_seconds": 1, "start_rate": 2, "end_rate": 2},
    ]
    result = execute_test_plan(root, "suite", value)
    assert result["verdict"] == "passed", json.dumps(result, indent=2)
    assert all(n > 0 for n in result["metrics"]["phase_counts"].values())
    value["spec"]["rate_unit"] = "requests"
    with pytest.raises(ValueError, match="transaction"):
        validate_performance(ExecutionPlan.from_dict(value))
