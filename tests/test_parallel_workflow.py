import threading
from contextvars import ContextVar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from iatest.application.testing_service import execute_test_plan
from iatest.core.config import init_project
from iatest.core.resources import resource_scope
from iatest.engines.contracts import (
    EngineContext,
    EngineResult,
    ExecutionPlan,
    PlanControl,
)
from iatest.engines.workflow import WorkflowEngine
from iatest.execution.control import RunControl


def test_parallel_dag_uses_real_overlapping_requests_and_serializes_resources(tmp_path):
    lock = threading.Lock()
    active, peak, account_active, account_peak = 0, 0, 0, 0
    gate = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            nonlocal active, peak, account_active, account_peak
            with lock:
                active += 1
                peak = max(peak, active)
                if self.path.startswith("/account"):
                    account_active += 1
                    account_peak = max(account_peak, account_active)
                if active == 2:
                    gate.set()
            gate.wait(2)
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            with lock:
                active -= 1
                if self.path.startswith("/account"):
                    account_active -= 1

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        init_project(tmp_path, "suite")
        base = f"http://127.0.0.1:{server.server_port}"
        nodes = []
        for name in ("account-a", "account-b", "free"):
            nodes.append(
                {
                    "id": name,
                    "resources": ["account"] if name != "free" else [],
                    "plan": {
                        "id": name,
                        "kind": "api",
                        "spec": {
                            "allowed_origins": [base],
                            "requests": [{"url": base + "/" + name}],
                        },
                    },
                }
            )
        result = execute_test_plan(
            tmp_path,
            "suite",
            {
                "id": "parallel",
                "kind": "workflow",
                "spec": {"max_parallel": 3, "nodes": nodes},
            },
        )
        assert result["verdict"] == "passed", result
        assert peak == 2 and account_peak == 1
        assert result["metrics"]["requests"] == 3
    finally:
        server.shutdown()
        server.server_close()


def test_parallel_copies_context_and_cleanup_waits_for_cancelled_branches(tmp_path):
    marker = ContextVar("parallel-test-marker", default="missing")
    token = marker.set("inherited")
    control = RunControl(timeout_seconds=5, max_requests=20)
    barrier = threading.Barrier(2)
    completed, cleanup_observation = [], []

    class Registry:
        def execute(self, child, context):
            assert marker.get() == "inherited"
            if child.id == "cleanup":
                cleanup_observation.append(set(completed))
                context.control.request()
            else:
                barrier.wait(timeout=2)
                control.cancelled.set()
                completed.append(child.id)
            return EngineResult(child.id, child.kind, verdict="passed")

    nodes = [
        {"id": name, "plan": {"id": name, "kind": "api", "spec": {"case_ids": []}}}
        for name in ("a", "b", "cleanup")
    ]
    nodes[-1].update(always=True, depends_on=["a", "b"])
    try:
        result = WorkflowEngine(tmp_path, "suite", Registry()).execute(
            ExecutionPlan.from_dict(
                {
                    "id": "cancel",
                    "kind": "workflow",
                    "spec": {"max_parallel": 2, "nodes": nodes},
                }
            ),
            EngineContext(control),
        )
        assert cleanup_observation == [{"a", "b"}]
        assert result.lifecycle == "cancelled"
        assert result.metrics["cleanup_requests"] == 1
    finally:
        marker.reset(token)


def test_atomic_child_budgets_wait_for_remote_reservation_without_deadlock():
    root = RunControl(timeout_seconds=3, max_requests=2)
    shared = PlanControl(root, max_requests=2)
    local = PlanControl(shared, max_requests=2)
    with root.request_lock:
        root.requests_started = root.requests_reserved = 2
    with shared.request_lock:
        shared.requests_started = shared.requests_reserved = 2
    result = []
    thread = threading.Thread(
        target=lambda: (local.request(), result.append("sent")), daemon=True
    )
    thread.start()
    # An acknowledgement returns unused reserved capacity while a sibling is waiting.
    for item in (shared, root):
        with item.request_lock:
            item.requests_started -= 2
            item.requests_reserved -= 2
    thread.join(timeout=2)
    assert not thread.is_alive() and result == ["sent"]
    assert root.request_count() == shared.request_count() == local.request_count() == 1


def test_resource_reservation_children_wait_and_cancel(tmp_path):
    owner = RunControl(timeout_seconds=3)
    waiter = RunControl(timeout_seconds=3)
    errors = []
    with resource_scope(["parallel-test-resource"], owner, reserve=True):

        def attempt():
            try:
                with resource_scope(["parallel-test-resource"], waiter, wait_seconds=2):
                    errors.append("unexpected acquisition")
            except (RuntimeError, TimeoutError) as exc:
                errors.append(str(exc))

        worker = threading.Thread(target=attempt, daemon=True)
        worker.start()
        waiter.cancelled.set()
        worker.join(timeout=2)
        assert not worker.is_alive() and len(errors) == 1 and "取消" in errors[0]
    with resource_scope(["parallel-test-resource"], owner):
        pass


def test_parallel_cleanup_has_one_shared_request_limit(tmp_path):
    class Registry:
        def execute(self, child, context):
            context.control.request()
            return EngineResult(child.id, child.kind, verdict="passed")

    nodes = [
        {
            "id": str(i),
            "always": True,
            "plan": {"id": str(i), "kind": "api", "spec": {"case_ids": []}},
        }
        for i in range(3)
    ]
    result = WorkflowEngine(tmp_path, "suite", Registry()).execute(
        ExecutionPlan.from_dict(
            {
                "id": "cleanup-cap",
                "kind": "workflow",
                "spec": {"nodes": nodes, "cleanup_max_requests": 2},
            }
        ),
        EngineContext(RunControl(timeout_seconds=3)),
    )
    assert result.metrics["cleanup_requests"] == 2
    assert result.verdict == "inconclusive"
    assert result.steps[-1]["result"]["lifecycle"] == "cancelled"
