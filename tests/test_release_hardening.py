from __future__ import annotations

import copy
import io
import json
import os
import re
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock

import pytest

from iatest.ai import provider, workflows
from iatest.ai.connections import bootstrap_connections, connection_scope, enroll_connection, validate_connection
from iatest.core.config import DEFAULT_CONFIG, init_project, load_config, save_config, update_config
from iatest.core.env import load_dotenv
from iatest.engines.contracts import EngineResult, ExecutionPlan
from iatest.engines.validation import validate_plan
from iatest.engines.verdicts import summarize_checks
from iatest.web import server
from iatest.web.security import ConsoleSecurity


def request(root, method="GET", path="/task-status", *, headers=None, body="", security=None):
    fields = {"Host": "127.0.0.1:8765", "Content-Length": str(len(body.encode())), **(headers or {})}
    raw = f"{method} {path} HTTP/1.0\r\n" + "".join(f"{key}: {value}\r\n" for key, value in fields.items()) + "\r\n" + body
    client, peer = socket.socketpair()
    client.settimeout(5)
    handler = server.make_handler(root, "demo", security=security)
    errors = []

    def run():
        try:
            handler(peer, ("127.0.0.1", 12345), object())
        except BaseException as exc:
            errors.append(exc)
        finally:
            peer.close()

    client.sendall(raw.encode())
    client.shutdown(socket.SHUT_WR)
    worker = threading.Thread(target=run, daemon=True)
    with redirect_stderr(io.StringIO()):
        worker.start()
        chunks = []
        while value := client.recv(65536):
            chunks.append(value)
        client.close()
        worker.join(5)
    assert not errors and not worker.is_alive(), errors
    head, _, content = b"".join(chunks).partition(b"\r\n\r\n")
    return int(head.split()[1]), head.decode(), content.decode()


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST"])
@pytest.mark.parametrize("host", ["evil.example:8765", "127.0.0.1:8766", "localhost.evil.example:8765", "localhost:8765,evil.example"])
def test_console_rejects_untrusted_host_before_dispatch(tmp_path, method, host, monkeypatch):
    action = Mock()
    monkeypatch.setattr(server, "handle_action", action)
    assert request(tmp_path, method, headers={"Host": host})[0] == 403
    action.assert_not_called()


@pytest.mark.parametrize("origin,token,expected", [(None, "", 403), ("http://127.0.0.1:8765", "", 403), ("https://evil.example", "valid", 403), ("null", "valid", 403), (None, "valid", 204), ("http://localhost:8765", "valid", 204)])
def test_console_requires_token_and_valid_origin(tmp_path, monkeypatch, origin, token, expected):
    def action(_root, _project, _path, _form, handler):
        handler.send_response(204)
        handler.end_headers()
    monkeypatch.setattr(server, "handle_action", action)
    security = ConsoleSecurity(csrf="valid")
    headers = {"Origin": origin} if origin is not None else {}
    assert request(tmp_path, "POST", "/actions/cancel-task", headers=headers, body="_csrf=" + token, security=security)[0] == expected


def test_console_session_protects_reads_and_bootstrap_is_one_time(tmp_path):
    security = ConsoleSecurity(require_session=True, bootstrap="once", session="session")
    assert request(tmp_path, security=security)[0] == 401
    status, headers, _ = request(tmp_path, path="/session?token=once", security=security)
    assert status == 303 and "HttpOnly; SameSite=Strict" in headers
    assert request(tmp_path, path="/session?token=once", security=security)[0] == 403
    assert request(tmp_path, headers={"Cookie": "iatest_session=session"}, security=security)[0] == 200


def test_console_explicit_proxy_origin_remains_supported(tmp_path, monkeypatch):
    security = ConsoleSecurity(allowed_origins=("https://console.example",), csrf="valid")
    def action(_root, _project, _path, _form, handler):
        handler.send_response(204)
        handler.end_headers()
    monkeypatch.setattr(server, "handle_action", action)
    assert request(tmp_path, "POST", headers={"Host": "console.example", "Origin": "https://console.example"}, body="_csrf=valid", security=security)[0] == 204


def test_config_disjoint_updates_are_merged_and_conflicts_are_explicit(tmp_path):
    first, second = load_config(tmp_path), load_config(tmp_path)
    first["execution"]["parallel"] = 7
    second["mock"]["port"] = 9999
    save_config(tmp_path, first)
    save_config(tmp_path, second)
    assert load_config(tmp_path)["execution"]["parallel"] == 7
    assert load_config(tmp_path)["mock"]["port"] == 9999
    first, second = load_config(tmp_path), load_config(tmp_path)
    first["execution"]["parallel"] = 8
    second["execution"]["parallel"] = 9
    save_config(tmp_path, first)
    with pytest.raises(ValueError, match="concurrently.*execution.parallel"):
        save_config(tmp_path, second)


def test_config_update_transaction_is_shared_across_projects(tmp_path):
    init_project(tmp_path)
    def append(index):
        update_config(tmp_path, lambda config: config["schedules"].append({"name": str(index)}))
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(append, range(20)))
    assert len(load_config(tmp_path)["schedules"]) == 20


@pytest.mark.parametrize("value", [{"agents": "invalid"}, {"agents": {"task_budget": None}}, {"llm": None}])
def test_legacy_invalid_config_reports_schema_path(tmp_path, value):
    (tmp_path / "iatest.json").write_text(json.dumps(value))
    with pytest.raises(ValueError, match="Invalid configuration: \\$\\."):
        load_config(tmp_path)


@pytest.mark.parametrize("section,key,value", [("execution", "parallel", "4"), ("execution", "parallel", 0), ("execution", "parallel", True), ("execution", "paralell", 4), ("llm", "master", {"context_window": 0}), ("agents", "task_budget", {"max_tokens": 0}), ("web", "max_request_bytes", float("nan"))])
def test_invalid_configuration_rejected_before_write(tmp_path, section, key, value):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config[section][key] = value
    with pytest.raises(ValueError):
        save_config(tmp_path, config)
    assert not (tmp_path / "iatest.json").exists()


def test_dotenv_does_not_override_execution_environment_and_can_refresh_owned_key(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/safe/path")
    monkeypatch.setenv("EXTERNALLY_SET", "external")
    monkeypatch.delenv("OWNED_AUDIT_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("PATH=/unsafe\nEXTERNALLY_SET=project\nOWNED_AUDIT_KEY=first\n")
    load_dotenv(tmp_path)
    assert os.environ["PATH"] == "/safe/path" and os.environ["EXTERNALLY_SET"] == "external"
    env.write_text("OWNED_AUDIT_KEY=second\n")
    load_dotenv(tmp_path)
    assert os.environ["OWNED_AUDIT_KEY"] == "second"


@pytest.mark.parametrize("stream", [False, True])
def test_unenrolled_credential_never_reaches_provider(tmp_path, monkeypatch, stream):
    monkeypatch.setenv("AUDIT_CLOUD_SECRET", "canary")
    bootstrap_connections(tmp_path)
    transport = Mock(side_effect=AssertionError("network must not run"))
    monkeypatch.setattr(provider, "post_json", transport)
    monkeypatch.setattr(provider.httpx, "Client", transport)
    with connection_scope(tmp_path), pytest.raises(PermissionError, match="not enrolled"):
        provider.model_completion(base_url="https://attacker.invalid/v1", model="audit", api_key_env="AUDIT_CLOUD_SECRET", messages=[], stream_callback=(lambda _: None) if stream else None)
    transport.assert_not_called()


def test_connection_binds_scheme_host_port_and_key(tmp_path):
    enroll_connection(tmp_path, "https://model.example/v1", "MODEL_KEY")
    validate_connection(tmp_path, "https://model.example/v2", "MODEL_KEY")
    for url, key in [("http://model.example/v1", "MODEL_KEY"), ("https://model.example:444/v1", "MODEL_KEY"), ("https://model.example/v1", "CLOUD_SECRET")]:
        with pytest.raises(PermissionError):
            validate_connection(tmp_path, url, key)
    with pytest.raises(ValueError, match="allow-http"):
        enroll_connection(tmp_path, "http://lan-model.example/v1", "MODEL_KEY")
    enroll_connection(tmp_path, "http://127.0.0.1:11434/v1", "LOCAL_KEY")


def test_project_credentials_do_not_cross_workspace_boundaries(tmp_path, monkeypatch):
    from iatest.core.config import resolve_env_vars
    from iatest.engines.data import plan_values
    monkeypatch.delenv("SHARED_AUDIT_CREDENTIAL", raising=False)
    first, second = tmp_path / "first", tmp_path / "second"
    for root, secret in ((first, "first-canary"), (second, "second-canary")):
        root.mkdir()
        (root / ".env").write_text("SHARED_AUDIT_CREDENTIAL=" + secret + "\n")
        load_config(root)
    enroll_connection(second, "https://model.example/v1", "SHARED_AUDIT_CREDENTIAL")
    with connection_scope(second):
        assert provider.resolve_api_key("SHARED_AUDIT_CREDENTIAL") == "second-canary"
    assert resolve_env_vars("${SHARED_AUDIT_CREDENTIAL}") == "second-canary"
    plan = ExecutionPlan("audit", "web", {}, input_env={"value": "SHARED_AUDIT_CREDENTIAL"})
    assert plan_values(plan)[0]["value"] == "second-canary"


def test_web_cannot_create_trust_or_write_secret_before_connection_check(tmp_path):
    bootstrap_connections(tmp_path)
    form = {"base_url": ["https://attacker.invalid"], "api_key_env": ["short-secret"], "credential_mode": ["secret"]}
    with pytest.raises(PermissionError):
        server.llm_role_from_form(tmp_path, "master", form, enforce_trust=True)
    assert not (tmp_path / ".env").exists()


def test_explicit_environment_reference_is_never_guessed_as_a_literal(tmp_path):
    form = {"base_url": ["https://model.example"], "api_key_env": ["sk_named_key"], "credential_mode": ["reference"]}
    assert server.llm_role_from_form(tmp_path, "master", form)["api_key_env"] == "sk_named_key"
    assert not (tmp_path / ".env").exists()


def test_secret_file_permissions_and_explicit_short_secret(tmp_path):
    path = tmp_path / ".env"
    path.write_text("OLD=value\n")
    path.chmod(0o644)
    assert server.normalize_api_key_ref(tmp_path, "master", "secret123", secret=True) == "IATEST_MASTER_KEY"
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    assert server.display_api_key_ref("secret123") == "******"


def test_debug_zero_is_disabled_and_content_is_opt_in(tmp_path, monkeypatch):
    target = tmp_path / "private" / "llm.jsonl"
    monkeypatch.setattr(provider, "_DEBUG_LOG_PATH", str(target))
    monkeypatch.setenv("IATEST_DEBUG_LLM", "0")
    provider._debug_llm_log([{"role": "user", "content": "password=audit-canary"}], "audit", "audit")
    assert not target.exists()
    monkeypatch.setenv("IATEST_DEBUG_LLM", "1")
    monkeypatch.delenv("IATEST_DEBUG_LLM_CONTENT", raising=False)
    provider._debug_llm_log([{"role": "user", "content": "business-canary"}], "audit", "audit")
    assert "business-canary" not in target.read_text()
    monkeypatch.setenv("IATEST_DEBUG_LLM_CONTENT", "1")
    provider._debug_llm_log_response("password=audit-canary")
    assert "audit-canary" not in target.read_text()
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o600


def test_missing_knowledge_version_disables_both_cache_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(workflows, "context_connection", Mock(side_effect=RuntimeError("database unavailable")))
    fingerprint = workflows.case_generation_fingerprint(tmp_path, "demo", [], load_config(tmp_path), {})
    assert fingerprint == ""
    assert workflows.load_generation_cache(tmp_path, "demo", "", []) is None
    workflows.save_generation_cache(tmp_path, "demo", "", [object()])


def test_fts_same_count_drift_is_detected_and_repaired(tmp_path):
    from iatest.context.storage import check_fts_index, context_connection, upsert_lesson
    upsert_lesson(tmp_path, "demo", {"id": "lesson", "title": "title", "content": "original", "source": "test", "source_type": "manual_edit"})
    with context_connection(tmp_path, "demo") as conn:
        conn.execute("update lessons_fts set content='corrupted'")
    assert check_fts_index(tmp_path, "demo")["mismatched"] == 1
    assert check_fts_index(tmp_path, "demo", repair=True)["repaired"]
    assert check_fts_index(tmp_path, "demo")["ok"]


@pytest.mark.parametrize("planned,assessed,failed,verdict", [(0, 0, 0, "inconclusive"), (1, 0, 0, "inconclusive"), (1, 1, 0, "passed"), (1, 1, 1, "failed")])
def test_assessment_counts_are_explicit(planned, assessed, failed, verdict):
    result = EngineResult("audit", "web")
    summarize_checks(result, planned, assessed, failed)
    assert result.verdict == verdict
    assert result.metrics["checks_assessed"] == assessed


def test_action_only_plan_is_rejected_before_execution():
    value = ExecutionPlan.from_dict({"id": "audit", "kind": "web", "spec": {"allowed_origins": ["http://127.0.0.1"], "start_url": "http://127.0.0.1", "steps": [{"op": "reload"}]}})
    with pytest.raises(ValueError, match="postcondition"):
        validate_plan(value)


def test_browser_context_wait_shares_budget_and_cancellation():
    from iatest.engines.browser_wait import wait_for_context
    from iatest.execution.control import RunCancelled, RunControl
    attempts = iter([False, True])
    def ready():
        if not next(attempts):
            raise LookupError("not committed")
        return "context"
    assert wait_for_context(RunControl(timeout_seconds=1), ready, LookupError, "unavailable") == "context"
    control = RunControl(timeout_seconds=1)
    control.cancelled.set()
    with pytest.raises(RunCancelled):
        wait_for_context(control, lambda: "must not execute", LookupError, "unavailable")
    with pytest.raises(ValueError, match="unavailable"):
        wait_for_context(RunControl(timeout_seconds=1), lambda: (_ for _ in ()).throw(LookupError()), LookupError, "unavailable", timeout=0)


def test_analysis_error_has_structured_status_and_cannot_become_markdown(tmp_path, monkeypatch):
    monkeypatch.setattr(workflows, "_build_ai_report_analysis", Mock(side_effect=TimeoutError("password=canary")))
    result = workflows.build_ai_report_analysis_result(tmp_path, "demo", [])
    assert result.status == "failed" and result.error_code == "TimeoutError"
    assert result.markdown == "" and "canary" not in result.error
    assert workflows.build_ai_report_analysis(tmp_path, "demo", []) == ""


def test_report_keeps_ai_failure_separate_from_test_success():
    from iatest.ai.analysis_outcome import AnalysisMarkdown, AnalysisOutcome
    from iatest.core.models import CaseResult
    from iatest.report.markdown import render_report_markdown
    result = CaseResult(case_id="TC-1", title="Health", method="GET", path="/health", status="passed", status_code=200, elapsed_ms=1)
    report = render_report_markdown([result], analysis=AnalysisMarkdown(AnalysisOutcome("failed", error_code="TimeoutError")))
    assert '"ai_analysis": {' in report and '"error_code": "TimeoutError"' in report
    assert result.status == "passed"


def test_result_retention_keeps_referenced_evidence_and_previews_by_default(tmp_path):
    from iatest.application.result_retention import prune_results
    from iatest.engines.store import EngineStore
    store = EngineStore(tmp_path, "demo")
    filename = "a" * 32 + ".png"
    for run_id in ("old", "referenced", "new"):
        store.save_result(EngineResult("audit", "web", run_id=run_id))
        path = store.evidence_path(run_id, filename, kind="web")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"private evidence")
    store.save_artifact("ui_case", {"verification": [{"run_id": "referenced"}]})
    preview = prune_results(tmp_path, "demo", keep=1)
    assert preview["run_ids"] == ["old"]
    assert store.get_result("old")
    removed = prune_results(tmp_path, "demo", keep=1, dry_run=False)
    assert removed["run_ids"] == ["old"]
    assert store.evidence_path("referenced", filename).read_bytes() == b"private evidence"
    with pytest.raises(ValueError, match="not found"):
        store.get_result("old")


def test_mock_lan_mode_is_explicit_and_history_redacts_query(tmp_path):
    from iatest.mock_server import build_mock_server, request_record
    with pytest.raises(ValueError, match="allow_lan"):
        build_mock_server({"mock": {"host": "0.0.0.0", "port": 0}})
    item = request_record("GET", "/", {"password": ["canary"]}, {}, None, 200, "")
    assert "canary" not in json.dumps(item)


def test_trace_is_linked_to_task_without_exposing_error_secrets(tmp_path, caplog):
    from iatest.core.observability import record_operational_error, trace_scope
    from iatest.web.task_store import claim_task, create_task
    from iatest.web.queue_signal import queue_event
    queue_event(tmp_path).clear()
    with trace_scope("request", request_id="request-audit") as trace:
        task = create_task(tmp_path, "demo", "audit", "audit")
        record_operational_error(tmp_path, "demo", component="audit", operation="test", error=RuntimeError("password=canary"))
    assert queue_event(tmp_path).is_set()
    stored = claim_task(tmp_path, "demo", task["id"])
    assert stored["payload"]["_trace"]["trace_id"] == trace["trace_id"]
    assert "canary" not in caplog.text
