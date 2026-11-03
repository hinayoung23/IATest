import os
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from iatest.application.testing_service import execute_test_plan
from iatest.core.config import init_project, load_config, save_config
from iatest.core.sandbox import SandboxPolicy
from iatest.engines.store import EngineStore
from iatest.engines.web import chromium_path
from iatest.execution.control import RunControl
from iatest.execution.isolation import stop_worker, worker_environment
from iatest.execution.native_isolation import stage_databases


def test_finished_cli_cleanup_never_signals_an_unowned_process_group(monkeypatch):
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    process.wait(timeout=5)

    def refuse_group(*args):
        raise AssertionError("A reaped Docker CLI cannot prove process-group ownership")

    monkeypatch.setattr(os, "killpg", refuse_group, raising=False)
    stop_worker(process, 0.1, process_group=False)
    assert process.returncode == 0


def test_wal_backup_is_a_complete_single_readonly_input(tmp_path, monkeypatch):
    source = tmp_path / "source.sqlite"
    destination = tmp_path / "inputs"
    destination.mkdir()
    writer = sqlite3.connect(source)
    monkeypatch.setenv("IATEST_WAL_SOURCE", str(source))
    try:
        writer.execute("pragma journal_mode=WAL")
        writer.execute("create table records(id integer)")
        writer.execute("insert into records values(42)")
        writer.commit()
        policy = SandboxPolicy.from_config(tmp_path, {"sandbox": {"mode": "policy"}})
        _, overrides = stage_databases(
            {
                "kind": "data",
                "spec": {
                    "database": {"driver": "sqlite", "path_env": "IATEST_WAL_SOURCE"}
                },
            },
            policy,
            destination,
            destination,
            1048576,
            RunControl(),
        )
        path = Path(overrides["IATEST_WAL_SOURCE"])
        reader = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            assert reader.execute("pragma journal_mode").fetchone()[0] == "delete"
            assert reader.execute("select id from records").fetchall() == [(42,)]
        finally:
            reader.close()
        assert [p.name for p in destination.iterdir()] == [path.name]
    finally:
        writer.close()


@pytest.fixture
def isolated(tmp_path):
    init_project(tmp_path, "suite")
    config = load_config(tmp_path)
    config["sandbox"].update(mode="process", termination_grace_seconds=0.5)
    save_config(tmp_path, config)
    calls = []
    entered = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            calls.append(self.path)
            entered.set()
            if self.path == "/slow":
                time.sleep(3)
            body = b"<html><body><p id=ready>Ready</p></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield tmp_path, f"http://127.0.0.1:{server.server_port}", calls, entered
    finally:
        server.shutdown()
        server.server_close()


def api(base, name="child"):
    return {
        "id": name,
        "kind": "api",
        "spec": {
            "allowed_origins": [base],
            "requests": [{"url": base} for _ in range(10)],
        },
    }


def test_isolated_parallel_workers_share_actual_global_request_grants(isolated):
    root, base, calls, _ = isolated
    result = execute_test_plan(
        root,
        "suite",
        {
            "id": "parallel",
            "kind": "workflow",
            "budget": {"max_requests": 5},
            "spec": {
                "max_parallel": 2,
                "nodes": [{"id": n, "plan": api(base, n)} for n in ("a", "b")],
            },
        },
    )
    assert result["verdict"] == "inconclusive", result
    assert len(calls) <= 5
    assert result["metrics"]["requests"] <= 5
    children = [s["result"] for s in result["steps"] if "result" in s]
    assert children
    assert all(c["metrics"]["isolation"]["worker_pid"] != os.getpid() for c in children)


def test_worker_environment_does_not_inherit_model_cloud_or_proxy_secrets(
    tmp_path, monkeypatch
):
    for name in (
        "OPENAI_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "HTTPS_PROXY",
        "PYTHONPATH",
    ):
        monkeypatch.setenv(name, "synthetic-isolation-canary")
    environment = worker_environment({}, tmp_path)
    assert not any(v == "synthetic-isolation-canary" for v in environment.values())


def test_windows_worker_has_private_known_folder_layout(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from iatest.execution import isolation

    monkeypatch.setattr(isolation, "os", SimpleNamespace(name="nt", environ={
        "USERPROFILE": "foreign-profile", "APPDATA": "foreign-data",
        "LOCALAPPDATA": "foreign-local-data", "OPENAI_API_KEY": "secret-canary",
    }))
    environment = isolation.worker_environment({}, tmp_path)
    assert environment["USERPROFILE"] == str(tmp_path)
    assert Path(environment["APPDATA"]).is_relative_to(tmp_path)
    assert Path(environment["LOCALAPPDATA"]).is_relative_to(tmp_path)
    assert (tmp_path / "AppData/Local").is_dir()
    assert (tmp_path / "AppData/Roaming").is_dir()
    assert not any(v.startswith("foreign") or v == "secret-canary" for v in environment.values())


def test_process_worker_preserves_narrower_active_network_policy(isolated):
    from dataclasses import replace

    from iatest.core.sandbox import SandboxPolicy, policy_scope

    root, base, calls, _ = isolated
    policy = SandboxPolicy.from_config(root, load_config(root), project="suite")
    with policy_scope(
        replace(policy, allowed_origins=frozenset({"http://127.0.0.1:1"}))
    ):
        result = execute_test_plan(root, "suite", api(base))
    assert result["verdict"] == "inconclusive" and not calls, result


def test_process_web_evidence_is_returned_and_persisted_by_parent(isolated):
    if not chromium_path():
        pytest.skip("Requires Chromium")
    root, base, _, _ = isolated
    config = load_config(root)
    config["sandbox"]["memory_mb"] = 2048
    save_config(root, config)
    result = execute_test_plan(
        root,
        "suite",
        {
            "id": "web-worker",
            "kind": "web",
            "spec": {
                "start_url": base,
                "allowed_origins": [base],
                "steps": [
                    {
                        "op": "expect",
                        "assert": "text",
                        "target": {"id": "ready"},
                        "expected": "Ready",
                    },
                    {"op": "screenshot", "target": {"id": "ready"}},
                ],
            },
        },
    )
    assert result["verdict"] == "passed", result
    assert result["metrics"]["isolation"]["worker_pid"] != os.getpid()
    assert (
        EngineStore(root, "suite")
        .evidence_path(result["run_id"], result["evidence"][0]["name"])
        .read_bytes()
        .startswith(b"\x89PNG")
    )


def test_parent_cancellation_interrupts_inflight_native_worker(isolated):
    root, base, calls, entered = isolated
    control = RunControl(timeout_seconds=15, max_requests=100)
    plan = api(base)
    plan["spec"]["requests"] = [{"url": base + "/slow"}]
    rows = []
    runner = threading.Thread(
        target=lambda: rows.append(execute_test_plan(root, "suite", plan, control))
    )
    runner.start()
    assert entered.wait(5)
    control.cancelled.set()
    runner.join(4)
    assert not runner.is_alive()
    assert rows[0]["verdict"] == "inconclusive" and rows[0]["lifecycle"] == "cancelled"
    assert calls == ["/slow"]
    pid = rows[0]["metrics"]["isolation"]["worker_pid"]
    if os.name == "nt":
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            assert ctypes.get_last_error() == 87  # No such PID.
        else:
            try:
                code = ctypes.c_ulong()
                kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
                assert kernel.GetExitCodeProcess(handle, ctypes.byref(code))
                assert code.value != 259  # STILL_ACTIVE
            finally:
                kernel.CloseHandle.argtypes = [ctypes.c_void_p]
                kernel.CloseHandle(handle)
    else:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
