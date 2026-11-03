import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from iatest.application.testing_service import execute_test_plan
from iatest.core.config import init_project
from iatest.engines.contracts import ExecutionPlan
from iatest.execution.control import RunControl
from iatest.nodes.distributed import aggregate, allocate, execute_remote
from iatest.nodes.protocol import make_controller
from iatest.nodes.store import ControllerStore
from iatest.nodes.worker import NodeWorker


@pytest.fixture
def cluster(tmp_path, monkeypatch):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            calls.append(self.path)
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    lab = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=lab.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{lab.server_port}"
    root = tmp_path / "controller"
    init_project(root, "suite")
    controller = make_controller(root, "suite")
    threading.Thread(target=controller.serve_forever, daemon=True).start()
    workers, threads = [], []
    store = ControllerStore(root, "suite")
    for node_id in ("a", "b"):
        token_env = "IATEST_TEST_NODE_" + node_id.upper()
        monkeypatch.setenv(token_env, "test-node-credential-" + node_id)
        store.enroll(
            {
                "id": node_id,
                "token_env": token_env,
                "allowed_origins": [base],
                "allowed_kinds": ["performance", "api", "client_performance"],
            }
        )
        local = tmp_path / node_id
        init_project(local, "suite")
        worker = NodeWorker(
            local,
            "suite",
            f"http://127.0.0.1:{controller.server_port}",
            node_id,
            token_env,
            [base],
            allowed_kinds=("performance", "api", "client_performance"),
            lease_seconds=1,
        )
        thread = threading.Thread(target=worker.run, daemon=True)
        thread.start()
        workers.append(worker)
        threads.append(thread)
    deadline = time.monotonic() + 3
    while not all(n["online"] for n in store.nodes()) and time.monotonic() < deadline:
        time.sleep(0.02)
    try:
        yield root, base, store, workers, calls
    finally:
        for worker in workers:
            worker.stop.set()
        for thread in threads:
            thread.join(timeout=5)
        for worker in workers:
            worker.close()
        controller.shutdown()
        controller.server_close()
        lab.shutdown()
        lab.server_close()


def plan(base):
    return {
        "id": "distributed",
        "kind": "performance",
        "budget": {"timeout_seconds": 12, "max_requests": 100},
        "spec": {
            "allowed_origins": [base],
            "mode": "arrival_rate",
            "duration_seconds": 0.6,
            "rate": 10,
            "users": 2,
            "request": {"url": base + "/?namespace={{namespace}}&user={{user}}"},
            "thresholds": {"max_error_ratio": 0, "max_dropped_ratio": 0},
            "distribution": {"nodes": ["a", "b"]},
        },
    }


def test_two_real_workers_share_global_load_and_merge_raw_samples(cluster):
    root, base, _store, _workers, calls = cluster
    result = execute_test_plan(root, "suite", plan(base))
    assert result["verdict"] == "passed", result
    assert result["metrics"]["completed"] == 6
    assert result["metrics"]["coverage_complete"] is True
    assert {j["metrics"]["node"] for j in result["metrics"]["node_results"]} == {
        "a",
        "b",
    }
    assert len({path.split("namespace=")[1].split("&")[0] for path in calls}) == 2
    assert sorted(
        j["metrics"]["scheduled"] for j in result["metrics"]["node_results"]
    ) == [3, 3]


def test_remote_api_and_local_scope_are_enforced(cluster):
    root, base, _store, _workers, _ = cluster
    value = {
        "id": "remote-api",
        "kind": "api",
        "spec": {"allowed_origins": [base], "requests": [{"url": base}]},
    }
    result = execute_remote(root, "suite", "a", value, RunControl(timeout_seconds=8))
    assert result.verdict == "passed", result
    value["spec"]["allowed_origins"] = ["http://example.invalid"]
    value["spec"]["requests"][0]["url"] = "http://example.invalid"
    result = execute_remote(root, "suite", "a", value, RunControl(timeout_seconds=8))
    assert result.verdict == "inconclusive"


def test_remote_worker_uses_controller_shared_authority(cluster, monkeypatch, tmp_path):
    from iatest.core.config import load_config, save_config
    from iatest.nodes.coordinator import make_coordinator

    root, base, store, _workers, calls = cluster
    monkeypatch.setenv("IATEST_TEST_GLOBAL", "synthetic-global-authority")
    authority = make_coordinator(
        tmp_path / "global.sqlite",
        [
            {
                "id": "controller",
                "token_env": "IATEST_TEST_GLOBAL",
                "resources": ["lab:shared"],
            }
        ],
        port=0,
    )
    thread = threading.Thread(target=authority.serve_forever, daemon=True)
    thread.start()
    config = load_config(root)
    config["resource_coordinator"] = {
        "url": f"http://127.0.0.1:{authority.server_port}",
        "id": "controller",
        "token_env": "IATEST_TEST_GLOBAL",
        "ttl": 3,
    }
    save_config(root, config)
    try:
        value = {
            "id": "shared-remote",
            "kind": "api",
            "resources": ["lab:shared"],
            "spec": {"allowed_origins": [base], "requests": [{"url": base}]},
        }
        result = execute_remote(
            root, "suite", "a", value, RunControl(timeout_seconds=8)
        )
        assert result.verdict == "passed", result
        assert result.metrics["global_resources"]["state"] == "released"
        with store.connection() as conn:
            job_id = conn.execute("select id from jobs").fetchone()[0]
        bindings = store.job(job_id)["global_leases"]
        assert bindings[0]["fences"] == {"lab:shared": 1}
        assert calls == ["/"]
    finally:
        authority.shutdown()
        authority.server_close()
        thread.join(timeout=2)


def test_worker_refuses_mobile_preparation_without_global_device_binding(cluster):
    from iatest.mobile.store import MobileStore

    _root, base, _store, workers, _calls = cluster
    worker = workers[0]
    MobileStore(worker.root, "suite").save_profile(
        {
            "id": "phone",
            "platform": "android",
            "device_id": "synthetic-phone",
            "app_id": "com.example.app",
        }
    )
    value = {
        "id": "prepare-phone",
        "kind": "api",
        "preparation": {
            "app": {
                "profile": "phone",
                "artifact_id": "fixture-package",
                "mode": "fresh",
            }
        },
        "spec": {"allowed_origins": [base], "requests": [{"url": base}]},
    }
    job = {
        "id": "job-fixture",
        "plan": value,
        "assets": [],
        "global_leases": [
            {
                "url": "http://127.0.0.1:1",
                "controller_id": "fixture",
                "id": "policy",
                "fences": {},
                "ttl": 3,
            }
        ],
    }
    with pytest.raises(ValueError, match="global lease"):
        worker.prepare(job)


def test_distributed_transactions_do_not_schedule_the_window_endpoint(cluster):
    root, base, _, _, _ = cluster
    value = plan(base)
    value["spec"].update(
        duration_seconds=1,
        rate_unit="transactions",
        scenarios=[
            {
                "id": "journey",
                "steps": [
                    {"id": "first", "request": {"url": base}},
                    {
                        "id": "second",
                        "request": {"url": base},
                        "assertions": [
                            {"path": "$.ok", "operator": "eq", "expected": True}
                        ],
                    },
                ],
            }
        ],
    )
    del value["spec"]["request"]
    result = execute_test_plan(root, "suite", value)
    assert result["verdict"] == "passed", result
    assert result["metrics"]["transactions"] == 10
    assert result["metrics"]["completed"] == 20


def test_global_percentiles_are_not_averaged_and_partial_nodes_cannot_pass():
    value = ExecutionPlan.from_dict(plan("http://localhost:8000"))
    samples = [
        {
            "kind": "request",
            "node_id": "a",
            "duration_ms": 1,
            "ok": True,
            "legacy_transaction": True,
        }
    ] * 99 + [
        {
            "kind": "request",
            "node_id": "b",
            "duration_ms": 1000,
            "ok": True,
            "legacy_transaction": True,
        }
    ]
    jobs = [
        {
            "node_id": "a",
            "id": "a",
            "state": "completed",
            "error": "",
            "result": {
                "lifecycle": "completed",
                "metrics": {"transport_sample_count": 99, "completed": 99},
            },
        },
        {
            "node_id": "b",
            "id": "b",
            "state": "completed",
            "error": "",
            "result": {
                "lifecycle": "completed",
                "metrics": {"transport_sample_count": 1, "completed": 1},
            },
        },
    ]
    assert aggregate(value, jobs, samples).metrics["p95_ms"] == 1
    jobs[1]["state"] = "error"
    assert aggregate(value, jobs, samples).verdict == "inconclusive"


def test_global_quotas_are_partitioned():
    value = ExecutionPlan.from_dict(plan("http://localhost:8000"))
    allocated = allocate(value)
    assert sum(p["spec"]["rate"] for p in allocated.values()) == 10
    assert sum(p["spec"]["users"] for p in allocated.values()) == 2
    assert sum(p["budget"]["max_requests"] for p in allocated.values()) == 100


def test_controller_rejects_missing_lan_tls_and_bad_cert_without_leaking_port(tmp_path):
    with pytest.raises(ValueError, match="TLS"):
        make_controller(tmp_path, "suite", "0.0.0.0", 0)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    with pytest.raises(OSError):
        make_controller(
            tmp_path, "suite", "127.0.0.1", port, certfile=str(tmp_path / "missing.pem")
        )
    with socket.socket() as available:
        available.bind(("127.0.0.1", port))


def test_remote_credentials_stay_references_in_controller_storage(cluster, monkeypatch):
    root, base, store, _, _ = cluster
    credential = "controlled-remote-secret-credential"
    monkeypatch.setenv("IATEST_TEST_AUTH", credential)
    child = {
        "id": "credential-api",
        "kind": "api",
        "input_env": {"auth": "IATEST_TEST_AUTH"},
        "spec": {
            "allowed_origins": [base],
            "requests": [
                {"url": base, "headers": {"Authorization": {"$value": "auth"}}}
            ],
        },
    }
    result = execute_test_plan(
        root,
        "suite",
        {
            "id": "remote-auth",
            "kind": "remote",
            "spec": {"node_id": "a", "plan": child},
        },
    )
    assert result["verdict"] == "passed", result
    with store.connection() as conn:
        encoded = conn.execute(
            "select plan from jobs order by created desc limit 1"
        ).fetchone()[0]
    assert credential not in encoded and '"$value": "auth"' in encoded
    assert credential not in json.dumps(result)


def test_shared_workflow_request_budget_also_limits_remote_jobs(cluster):
    root, base, _, _, calls = cluster
    child = {
        "id": "two-requests",
        "kind": "api",
        "spec": {"allowed_origins": [base], "requests": [{"url": base}, {"url": base}]},
    }
    plan = {
        "id": "remote-budget",
        "kind": "workflow",
        "budget": {"timeout_seconds": 15, "max_requests": 3},
        "spec": {
            "nodes": [
                {
                    "id": node,
                    "plan": {
                        "id": "remote-" + node,
                        "kind": "remote",
                        "spec": {"node_id": node, "plan": child},
                    },
                }
                for node in ("a", "b")
            ]
        },
    }
    result = execute_test_plan(root, "suite", plan)
    assert result["verdict"] == "inconclusive", result
    assert len(calls) == 3
    assert result["metrics"]["requests"] == 3


def test_worker_stops_load_after_controller_lease_is_lost(cluster):
    _root, base, store, workers, calls = cluster
    value = plan(base)
    value["spec"].pop("distribution")
    value["spec"].update(duration_seconds=5, rate=20, users=1)
    job_id = store.enqueue("lease-test", "a", value)
    deadline = time.monotonic() + 4
    while store.job(job_id)["state"] != "ready" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert store.job(job_id)["state"] == "ready"
    store.start([job_id], delay=0)
    while len(calls) < 3 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(calls) >= 3
    # Close transport on this worker rather than changing or terminating any device.
    workers[0].client.close()
    time.sleep(1.8)
    count = len(calls)
    time.sleep(0.3)
    assert len(calls) == count < 80


def test_revoked_node_identity_stops_active_load_and_restoration_never_replays(cluster, monkeypatch):
    _root, base, store, _workers, calls = cluster
    value = plan(base)
    value["spec"].pop("distribution")
    value["spec"].update(duration_seconds=5, rate=20, users=1)
    job_id = store.enqueue("identity-revocation", "a", value)
    deadline = time.monotonic() + 4
    while store.job(job_id)["state"] != "ready" and time.monotonic() < deadline:
        time.sleep(.02)
    assert store.job(job_id)["state"] == "ready"
    store.start([job_id], delay=0)
    while len(calls) < 3 and time.monotonic() < deadline:
        time.sleep(.02)
    assert len(calls) >= 3
    # Rotate only this fixture's synthetic server credential. The worker still
    # presents its original credential; no transport or clock is mocked.
    monkeypatch.setenv("IATEST_TEST_NODE_A", "revoked-fixture-node-credential-a")
    time.sleep(1.8)
    stopped_count = len(calls)
    time.sleep(.4)
    assert len(calls) == stopped_count < 80
    monkeypatch.setenv("IATEST_TEST_NODE_A", "test-node-credential-a")
    deadline = time.monotonic() + 3
    while store.job(job_id)["state"] not in {"error", "cancelled"} and time.monotonic() < deadline:
        time.sleep(.05)
    assert store.job(job_id)["state"] in {"error", "cancelled"}
    assert len(calls) == stopped_count


def test_parallel_remote_jobs_wait_for_reserved_quota_and_return_unused_capacity(
    cluster,
):
    root, base, _, _, calls = cluster
    child = {
        "id": "one-request",
        "kind": "api",
        "budget": {"max_requests": 10},
        "spec": {"allowed_origins": [base], "requests": [{"url": base}]},
    }
    value = {
        "id": "parallel-remote",
        "kind": "workflow",
        "budget": {"timeout_seconds": 12, "max_requests": 2},
        "spec": {
            "max_parallel": 2,
            "nodes": [
                {
                    "id": name,
                    "plan": {
                        "id": "remote-" + name,
                        "kind": "remote",
                        "spec": {"node_id": name, "plan": child},
                    },
                }
                for name in ("a", "b")
            ],
        },
    }
    result = execute_test_plan(root, "suite", value)
    assert result["verdict"] == "passed", result
    assert result["metrics"]["requests"] == len(calls) == 2


def test_remote_ios_offline_measurement_freezes_source_and_never_executes_a_phone(
    cluster,
):
    from iatest.application.ios_measurements import import_measurement

    root, _base, _store, workers, calls = cluster
    local = workers[0].root

    def asset(directory, name, value):
        source = directory / (name + ".json")
        source.write_text(
            json.dumps(
                {
                    "contract_version": 1,
                    "platform": "ios",
                    "app_id": "com.example.fixture",
                    "source": {"provider": "custom", "test_id": "offline-fixture"},
                    "measurements": [
                        {"metric": "ios_launch_ms", "unit": "ms", "values": [value]}
                    ],
                }
            )
        )
        return import_measurement(
            directory,
            "suite",
            {"id": name, "format": "measurements_json", "path": str(source)},
        )

    asset(root, "matching", 100)
    child = {
        "id": "offline",
        "kind": "client_performance",
        "spec": {
            "platform": "ios",
            "measurement": {"artifact_id": "matching"},
            "thresholds": {"max_ios_launch_ms": 200},
        },
    }
    good = execute_remote(root, "suite", "a", child, RunControl(timeout_seconds=8))
    assert good.verdict == "passed", good
    from iatest.engines.store import EngineStore

    assert (
        EngineStore(local, "suite").get_artifact("ios_measurement", "matching")[
            "measurements"
        ]["ios_launch_ms"]["max"]
        == 100
    )
    asset(root, "different", 100)
    asset(local, "different", 900)
    child["spec"]["measurement"]["artifact_id"] = "different"
    bad = execute_remote(root, "suite", "a", child, RunControl(timeout_seconds=8))
    assert bad.verdict == "inconclusive" and "source" in bad.error
    assert calls == []


def test_distributed_account_pool_is_disjoint_and_saved_as_environment_references(
    cluster, monkeypatch
):
    root, base, _, _, calls = cluster
    value = plan(base)
    value["spec"].update(
        mode="users",
        duration_seconds=0.5,
        users=3,
        think_seconds=0.1,
        request={"url": base + "/?account={{account}}&user={{user}}"},
        account_pool=[
            {
                "id": "account-" + str(i),
                "input_env": {"account": "IATEST_ACCOUNT_" + str(i)},
            }
            for i in range(3)
        ],
    )
    for i in range(3):
        monkeypatch.setenv("IATEST_ACCOUNT_" + str(i), "identity-" + str(i))
    allocations = allocate(ExecutionPlan.from_dict(value))
    assert [len(p["spec"]["account_pool"]) for p in allocations.values()] == [2, 1]
    result = execute_test_plan(root, "suite", value)
    assert result["verdict"] == "passed", result
    from urllib.parse import parse_qs, urlsplit

    observed = {
        (
            parse_qs(urlsplit(p).query)["account"][0],
            parse_qs(urlsplit(p).query)["user"][0],
        )
        for p in calls
    }
    assert observed == {("identity-" + str(i), str(i)) for i in range(3)}
    assert all("identity-" + str(i) not in json.dumps(result) for i in range(3))
