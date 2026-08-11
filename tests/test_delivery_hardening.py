from __future__ import annotations

import json
import re
import socket
import sqlite3
import threading
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlparse

import pytest

from ai_apitest.ai import workflows
from ai_apitest.ai.workflows import _governance_feature_enabled
from ai_apitest.context.dependencies import (
    infer_dependency_candidates,
    synchronize_project_dependencies,
    update_dependency_status,
)
from ai_apitest.context.injection import retrieve_candidates
from ai_apitest.context.storage import (
    LESSON_COLUMNS,
    claim_due_extraction_jobs,
    context_connection,
    count_lessons,
    list_context_packs,
    list_lessons,
    normalize_lesson,
    prune_context_history,
    prune_context_packs,
    recent_audit_logs,
    rebuild_fts_index,
    save_context_pack,
    update_lesson_review,
    upsert_lesson,
)
from ai_apitest.core.config import current_environment, load_config, save_config
from ai_apitest.core.case_schema import normalize_cases
from ai_apitest.core.models import CaseResult, Endpoint, TestCase as ApiTestCaseModel, TestStep as ApiTestStepModel
from ai_apitest.core.observability import record_operational_error
from ai_apitest.core.paths import project_context_packs_dir, project_docs_file, project_metadata_file, project_task_db
from ai_apitest.core.serde import read_json, to_jsonable, write_json
from ai_apitest.discovery.document import detect_base_url, parse_api_document
from ai_apitest.domain.contracts import normalize_business_rules
from ai_apitest.execution.assertions import compare, json_path, normalize_json_path
from ai_apitest.execution.runner import apply_path_params, encode_query, encode_request_body, initial_context
from ai_apitest.application.result_analysis import (
    documented_contract_evidence,
    documented_endpoints_for_results,
    passed_case_summary,
)
from ai_apitest.generator.cases import generate_cases, parameter_serialization, success_assertions
from ai_apitest.orchestrator.maintainer.auto_updater import impacted_cases
from ai_apitest.web.server import load_endpoints, make_handler, render_config, run_task_by_id
from ai_apitest.web.task_store import (
    active_task,
    claim_task,
    create_task,
    list_tasks,
    recover_stale_tasks,
    task_connection,
    update_task,
)


def active_lesson(index: int, *, path: str | None = None, title: str | None = None) -> dict:
    return {
        "id": f"les-delivery-{index:05d}",
        "scope": "endpoint",
        "method": "GET",
        "path": path or f"/api/noise/{index}",
        "lesson_type": "business_rule",
        "title": title or f"noise lesson {index}",
        "content": f"delivery content {index}",
        "source": "delivery-test",
        "source_type": "manual_edit",
        "lifecycle_status": "active",
        "review_status": "confirmed",
        "verification_status": "unverified",
        "confidence": "high",
    }


def test_markdown_base_url_prefers_current_operational_address_over_recommendation() -> None:
    markdown = """
- 当前 HTTP 基础地址：`http://mail.tempbox.cn`
- 推荐 HTTPS 基础地址：`https://mail.tempbox.cn`
"""

    assert detect_base_url(markdown) == "http://mail.tempbox.cn"


def test_business_rule_normalizer_accepts_scalar_fixture_fields() -> None:
    rules = normalize_business_rules(
        [
            {
                "title": "需要时间夹具",
                "description": "需要固定时间执行",
                "preconditions": "服务可用",
                "fixture_requirements": "time_control",
            }
        ],
        method="GET",
        path="/clock",
    )

    assert rules[0]["preconditions"] == ["服务可用"]
    assert rules[0]["fixture_requirements"] == ["time_control"]


def test_report_analysis_does_not_run_inline_governance_by_default(tmp_path, monkeypatch) -> None:
    config = load_config(tmp_path)
    assert config["governance"]["inline_after_report"] is False
    monkeypatch.setattr(
        workflows,
        "configured_llm",
        lambda root, role: {
            "base_url": "https://unused.invalid/v1",
            "model": "fake",
            "api_key_env": "UNUSED",
        },
    )
    monkeypatch.setattr(
        "ai_apitest.application.result_analysis.analyze_results_with_agent_runtime",
        lambda *args, **kwargs: (
            "### One-line conclusion\nThe run passed.\n"
            "### Result overview\n1 of 1 passed.\n"
            "### Main issues\nNone.\n"
            "### Recommended actions\nNo action required.\n"
            "### Notes\nThis conclusion uses only the current execution evidence."
        ),
    )
    monkeypatch.setattr(workflows, "process_report_analysis", lambda *args, **kwargs: ("visible", {}))
    monkeypatch.setattr(
        workflows,
        "review_context_conflicts",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("inline governance must not run")),
    )
    result = CaseResult(
        case_id="TC-1",
        title="健康检查",
        status="passed",
        method="GET",
        path="/health",
        status_code=200,
        elapsed_ms=1,
    )
    assert workflows.build_ai_report_analysis(tmp_path, "demo", [result]) == "visible"


def test_report_analysis_preserves_passed_artifact_when_later_revision_fails(tmp_path, monkeypatch) -> None:
    instances = {}
    analyzer_rounds = []

    class FakeRound:
        def __init__(self, artifact, round_index: int, passed: bool) -> None:
            self.artifact = artifact
            self.round_index = round_index
            self.passed = passed
            self.hard_gate_pass = True
            self.audit_complete = True
            self.safe_for_fallback = True
            self.average_score = 92.0 if passed else 80.0 + round_index
            self.candidate_id = f"candidate-{round_index}"
            self.rank = (self.average_score, round_index)

        def repair_feedback(self):
            return {"round": self.round_index, "passed": self.passed}

    class FakeAuditService:
        def __init__(self, root, project, artifact_type, **kwargs) -> None:
            self.artifact_type = artifact_type
            self.policy = type("Policy", (), {"max_rounds": 3})()
            self.rounds = []
            self.finalized = None
            instances[artifact_type] = self

        def evaluate(self, artifact, *, round_index, **kwargs):
            passed = self.artifact_type == "test_report" and round_index == 2
            result = FakeRound(artifact, round_index, passed)
            self.rounds.append(result)
            return result

        def best_safe_round(self):
            return max(self.rounds, key=lambda item: item.rank) if self.rounds else None

        def finalize(self, selected, *, decision, reason) -> None:
            self.finalized = (selected, decision, reason)

    def analyze(*args, quality_round=1, **kwargs):
        analyzer_rounds.append(quality_round)
        if quality_round == 3:
            raise RuntimeError("third round failed")
        return (
            "### 一句话结论\n执行存在一项差异。\n"
            "### 结果概览\n共 1 条，失败 1 条。\n"
            "### 主要问题\n响应与契约不一致。\n"
            "### 建议处理\n确认接口契约。\n"
            "### 失败复核\n断言失败。\n"
            "### 备注\n仅依据执行证据。"
        )

    monkeypatch.setattr(
        workflows,
        "configured_llm",
        lambda root, role: {
            "base_url": "https://unused.invalid/v1",
            "model": "fake",
            "api_key_env": "UNUSED",
        },
    )
    monkeypatch.setattr(
        "ai_apitest.application.result_analysis.analyze_results_with_agent_runtime",
        analyze,
    )
    monkeypatch.setattr(
        "ai_apitest.application.quality_audit.QualityAuditService",
        FakeAuditService,
    )
    monkeypatch.setattr(workflows, "process_report_analysis", lambda *args, **kwargs: ("visible", {}))
    result = CaseResult(
        case_id="TC-1",
        title="契约差异",
        status="failed",
        method="GET",
        path="/health",
        status_code=422,
        elapsed_ms=1,
        assertion_errors=["detail 不一致"],
    )

    assert workflows.build_ai_report_analysis(tmp_path, "demo", [result]) == "visible"
    assert analyzer_rounds == [1, 2, 3]
    assert [item.round_index for item in instances["test_report"].rounds] == [1, 2]
    assert [item.round_index for item in instances["experiences"].rounds] == [1, 2]
    report_selected, report_decision, _ = instances["test_report"].finalized
    experience_selected, experience_decision, _ = instances["experiences"].finalized
    assert report_selected.passed is True
    assert report_decision == "accepted"
    assert experience_selected.round_index == 2
    assert experience_decision == "pending_highest_safe"


def test_parallel_case_contexts_receive_unique_timestamp_values() -> None:
    first = initial_context()
    second = initial_context()
    assert first["timestamp"] != second["timestamp"]
    assert first["uuid"] != second["uuid"]
    assert first["random"] != second["random"]
    assert re.fullmatch(r"[0-9a-f]{32}", first["uuid"])
    assert re.fullmatch(r"[0-9a-f]{32}", first["random"])


def test_report_analysis_uses_documented_contracts_and_separates_rate_limit_wait(tmp_path) -> None:
    documented = Endpoint(
        method="GET",
        path="/api/generate",
        responses={
            "422": {
                "content": {
                    "application/json": {
                        "example": {"detail": "请重新使用别的自定义前缀。"}
                    }
                }
            }
        },
        business_rules=[
            {
                "rule_id": "GEN-004",
                "title": "自定义前缀超过长度限制。",
                "source_evidence": "超长前缀返回固定 detail 字符串。",
                "expected": {
                    "assertions": [
                        {
                            "type": "json_path",
                            "target": "$.detail",
                            "operator": "eq",
                            "expected": "请重新使用别的自定义前缀。",
                        }
                    ]
                },
            }
        ],
    )
    write_json(project_docs_file(tmp_path, "demo"), [to_jsonable(documented)])
    failed = CaseResult(
        case_id="TC-CONTRACT",
        title="生成临时邮箱 - 自定义前缀超过长度限制。",
        status="failed",
        method="GET",
        path="/api/generate",
        status_code=422,
        elapsed_ms=20,
        assertion_errors=[
            "json_path $.detail eq '请重新使用别的自定义前缀。' 失败，实际值为数组"
        ],
    )
    rate_limited = CaseResult(
        case_id="TC-RATE",
        title="生成并检查邮箱",
        status="passed",
        method="CHAIN",
        path="GET /api/generate -> GET /api/check/{email}",
        status_code=200,
        elapsed_ms=59902,
        request={"steps": [{"rate_limit_wait_ms": 59817}, {}]},
    )

    contracts = documented_endpoints_for_results(tmp_path, "demo", [failed, rate_limited])
    assert contracts == [documented]
    contract_evidence = documented_contract_evidence([failed, rate_limited], contracts)
    assert contract_evidence == [
        {
            "method": "GET",
            "path": "/api/generate",
            "responses": documented.responses,
            "business_rules": documented.business_rules,
        }
    ]
    assert passed_case_summary(rate_limited)["rate_limit_wait_ms"] == 59817
    assert passed_case_summary(rate_limited)["active_elapsed_ms"] == 85

    misleading = (
        "### 一句话结论\n存在一项失败。\n"
        "### 结果概览\n性能最慢用例：TC-RATE（59902ms）。\n"
        "### 主要问题\nTC-CONTRACT 属于断言问题。\n"
        "### 建议处理\n调整断言逻辑：更新 TC-CONTRACT 的断言。\n"
        "### 备注\n仅依据执行证据。\n"
        "### 失败复核\nTC-CONTRACT 的响应格式不同。"
    )
    issues = workflows.report_quality_gate_issues(
        misleading,
        [failed, rate_limited],
        contracts,
    )
    assert {item["reason"] for item in issues} >= {
        "把框架主动限流等待误写成接口性能风险。",
        "失败断言有可信文档契约依据，不能直接归因为用例断言错误。",
    }

    corrected = misleading.replace(
        "性能最慢用例：TC-RATE（59902ms）。",
        "TC-RATE 总耗时 59902ms，其中框架主动限流等待 59817ms，实际执行 85ms。",
    ).replace(
        "TC-CONTRACT 属于断言问题。",
        "TC-CONTRACT 的实际响应与文档契约不一致，不是断言问题。",
    ).replace(
        "调整断言逻辑：更新 TC-CONTRACT 的断言。",
        "确认 TC-CONTRACT 应修接口实现还是修文档，不得调整断言。",
    )
    assert workflows.report_quality_gate_issues(
        corrected,
        [failed, rate_limited],
        contracts,
    ) == []


def test_task_store_persists_mutex_progress_and_stale_recovery(tmp_path) -> None:
    first = create_task(tmp_path, "demo", "generate", "生成用例", kind="generate", payload={"source": "spec.json"})
    assert first is not None
    assert create_task(tmp_path, "demo", "generate", "重复生成") is None
    claimed = claim_task(tmp_path, "demo", first["id"])
    assert claimed is not None
    assert claimed["payload"] == {"source": "spec.json"}

    assert update_task(
        tmp_path,
        "demo",
        first["id"],
        stage="generate",
        detail="正在生成",
        stream_delta="第一段\n",
    )
    running = active_task(tmp_path, "demo", "generate")
    assert running is not None
    assert running["detail"] == "正在生成"
    assert running["stream_excerpt"] == "第一段\n"

    with task_connection(tmp_path, "demo") as conn:
        conn.execute(
            "update web_tasks set heartbeat_at = 0 where id = ?",
            (first["id"],),
        )
    assert recover_stale_tasks(tmp_path, "demo", stale_after_seconds=1) == 1
    queued = active_task(tmp_path, "demo", "generate")
    assert queued is not None
    assert queued["status"] == "queued"

    assert claim_task(tmp_path, "demo", first["id"]) is not None
    with task_connection(tmp_path, "demo") as conn:
        conn.execute("update web_tasks set heartbeat_at = 0 where id = ?", (first["id"],))
    assert recover_stale_tasks(tmp_path, "demo", stale_after_seconds=1) == 1
    assert active_task(tmp_path, "demo", "generate") is None
    recovered = list_tasks(tmp_path, "demo", recent_seconds=3600)
    assert recovered[0]["status"] == "failed"
    assert recovered[0]["stage"] == "interrupted"


def test_persisted_task_runner_completes_and_non_resumable_task_is_not_requeued(tmp_path) -> None:
    task = create_task(tmp_path, "demo", "discover", "接口识别", kind="discover", payload={"source": "spec.json"})
    assert task is not None

    def target(progress):
        progress("已读取", stage="read", stream_delta="chunk")
        return "完成"

    run_task_by_id(tmp_path, "demo", task["id"], target)
    completed = list_tasks(tmp_path, "demo", recent_seconds=3600)[0]
    assert completed["status"] == "completed"
    assert completed["message"] == "完成"
    assert completed["stream_excerpt"] == "chunk"

    unsafe = create_task(tmp_path, "demo", "run", "用例执行", kind="run", resumable=False)
    assert unsafe is not None
    assert claim_task(tmp_path, "demo", unsafe["id"]) is not None
    with task_connection(tmp_path, "demo") as conn:
        conn.execute("update web_tasks set heartbeat_at = 0 where id = ?", (unsafe["id"],))
    assert recover_stale_tasks(tmp_path, "demo", stale_after_seconds=1) == 1
    assert active_task(tmp_path, "demo", "run") is None


def test_failed_task_preserves_the_stage_that_actually_failed(tmp_path) -> None:
    task = create_task(tmp_path, "demo", "generate", "用例生成", kind="generate")
    assert task is not None

    def target(progress):
        progress("正在调用规划模型", stage="llm")
        raise RuntimeError("planner failed")

    run_task_by_id(tmp_path, "demo", task["id"], target)
    failed = list_tasks(tmp_path, "demo", recent_seconds=3600)[0]
    assert failed["status"] == "failed"
    assert failed["stage"] == "llm"
    assert failed["message"] == "planner failed"


def test_endpoint_changes_synchronize_graph_and_dependency_lessons(tmp_path) -> None:
    producer = Endpoint(
        method="GET",
        path="/generate",
        responses={"200": {"schema": {"type": "object", "properties": {"email": {"type": "string"}}}}},
    )
    consumer = Endpoint(
        method="GET",
        path="/messages/{email}",
        parameters=[{"name": "email", "in": "path", "required": True, "schema": {"type": "string"}}],
    )
    first = synchronize_project_dependencies(tmp_path, "demo", [], [producer, consumer], actor="test")
    assert first["added_edges"] == 1
    graph = read_json(project_metadata_file(tmp_path, "demo"), {})["dependency_graph"]
    assert graph["edges"][0]["inject"] == {"path_params.email": "{{email}}"}

    lesson = list_lessons(tmp_path, "demo", lesson_type="data_dependency")[0]
    update_lesson_review(
        tmp_path,
        "demo",
        lesson["id"],
        lifecycle_status="active",
        review_status="confirmed",
    )
    update_dependency_status(tmp_path, "demo", lesson["id"], "verified", "test", "verified")

    removed = synchronize_project_dependencies(tmp_path, "demo", [producer, consumer], [producer], actor="test")
    assert removed["removed_edges"] == 1
    assert read_json(project_metadata_file(tmp_path, "demo"), {})["dependency_graph"]["edges"] == []
    with context_connection(tmp_path, "demo") as conn:
        stale = conn.execute("select lifecycle_status, verification_status from lessons where id = ?", (lesson["id"],)).fetchone()
    assert tuple(stale) == ("ignored", "invalidated")

    new_consumer = Endpoint(
        method="POST",
        path="/notify",
        parameters=[{"name": "email", "in": "query", "required": True, "schema": {"type": "string"}}],
    )
    added = synchronize_project_dependencies(tmp_path, "demo", [producer], [producer, new_consumer], actor="test")
    assert added["added_edges"] == 1
    graph = read_json(project_metadata_file(tmp_path, "demo"), {})["dependency_graph"]
    assert graph["edges"][0]["to"] == {"method": "POST", "path": "/notify"}
    assert graph["edges"][0]["inject"] == {"query.email": "{{email}}"}


def test_legacy_task_database_is_migrated_in_place(tmp_path) -> None:
    database = project_task_db(tmp_path, "demo")
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as conn:
        conn.execute(
            """
            create table web_tasks(
                id text primary key, project text not null, action text not null,
                label text not null, status text not null, stage text not null,
                detail text not null default '', message text not null default '',
                stream_excerpt text not null default '', owner_pid integer,
                started_at real not null, heartbeat_at real not null, updated_at real not null
            )
            """
        )
        conn.execute(
            "create unique index idx_web_tasks_running on web_tasks(project, action) where status = 'running'"
        )

    task = create_task(tmp_path, "demo", "generate", "生成用例", kind="generate", payload={"version": 2})
    assert task is not None
    with task_connection(tmp_path, "demo") as conn:
        columns = {row["name"] for row in conn.execute("pragma table_info(web_tasks)").fetchall()}
        index_sql = conn.execute(
            "select sql from sqlite_master where type = 'index' and name = 'idx_web_tasks_running'"
        ).fetchone()[0]
    assert {"kind", "payload", "resumable", "attempt_count", "max_attempts"} <= columns
    assert "queued" in index_sql


def test_cold_context_database_can_start_atomic_retry_claim(tmp_path) -> None:
    assert claim_due_extraction_jobs(tmp_path, "cold-project", limit=5) == []


def test_web_delete_endpoint_survives_proxy_origin_and_rerenders_pages(tmp_path) -> None:
    endpoint = Endpoint(method="GET", path="/api/items", summary="查询项目")
    write_json(project_docs_file(tmp_path, "demo"), [to_jsonable(endpoint)])
    handler_type = make_handler(tmp_path, "demo")

    def request(method: str, path: str, body: str = "", headers: dict[str, str] | None = None):
        request_headers = {"Host": "127.0.0.1:8765", **(headers or {})}
        data = body.encode("utf-8")
        if data:
            request_headers["Content-Length"] = str(len(data))
        raw_headers = "".join(f"{key}: {value}\r\n" for key, value in request_headers.items())
        raw_request = f"{method} {path} HTTP/1.0\r\n{raw_headers}\r\n".encode("ascii") + data
        client_socket, handler_socket = socket.socketpair()
        client_socket.settimeout(5)
        client_socket.sendall(raw_request)
        client_socket.shutdown(socket.SHUT_WR)

        def handle_request() -> None:
            try:
                handler_type(handler_socket, ("127.0.0.1", 12345), object())
            finally:
                handler_socket.close()

        thread = threading.Thread(target=handle_request, daemon=True)
        thread.start()
        chunks: list[bytes] = []
        while True:
            chunk = client_socket.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        client_socket.close()
        thread.join(timeout=5)
        raw_response = b"".join(chunks)
        head, _, response_body = raw_response.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        status = int(lines[0].split()[1])
        response_headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
        return status, response_headers, response_body

    status, _headers, page_body = request("GET", "/interfaces?project=demo")
    assert status == 200
    page_text = page_body.decode("utf-8")
    token_match = re.search(r'name="_csrf" value="([^"]+)"', page_text)
    assert token_match

    rejected_body = urlencode({"project": "demo", "index": "0"})
    status, _headers, error_body = request(
        "POST",
        "/actions/delete-endpoint",
        rejected_body,
        {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://untrusted.example",
        },
    )
    assert status == 403
    assert "Cross-site request rejected" in error_body.decode("utf-8")
    assert len(load_endpoints(tmp_path, "demo")) == 1

    delete_body = urlencode({"project": "demo", "index": "0", "_csrf": token_match.group(1)})
    status, headers, _body = request(
        "POST",
        "/actions/delete-endpoint",
        delete_body,
        {
            "Content-Type": "application/x-www-form-urlencoded",
            # Embedded previews can preserve the browser origin while
            # forwarding a loopback Host header to the local service.
            "Origin": "https://preview.example",
        },
    )
    assert status == 303
    assert headers["Location"].startswith("/interfaces?project=demo")
    assert load_endpoints(tmp_path, "demo") == []
    assert request("GET", "/interfaces?project=demo")[0] == 200
    assert request("GET", "/?project=demo")[0] == 200
    assert request("GET", "/cases?project=demo")[0] == 200


def test_operational_errors_are_persisted_for_web_diagnostics(tmp_path) -> None:
    try:
        raise RuntimeError("delivery failure")
    except RuntimeError as exc:
        record_operational_error(
            tmp_path,
            "demo",
            component="delivery_test",
            operation="verify_audit",
            error=exc,
            detail={"attempt": 1},
        )
    audit = recent_audit_logs(tmp_path, "demo", limit=1)[0]
    assert audit["action"] == "operational_error"
    assert audit["detail"]["error_type"] == "RuntimeError"
    assert audit["detail"]["attempt"] == 1


def test_fts_search_count_pagination_and_rebuild(tmp_path) -> None:
    for index, title in enumerate(("payment retry policy", "payment timeout policy", "profile policy"), start=1):
        upsert_lesson(tmp_path, "demo", active_lesson(index, title=title))

    assert count_lessons(tmp_path, "demo", query_text="payment") == 2
    first_page = list_lessons(tmp_path, "demo", query_text="payment", limit=1, offset=0)
    second_page = list_lessons(tmp_path, "demo", query_text="payment", limit=1, offset=1)
    assert len(first_page) == len(second_page) == 1
    assert first_page[0]["id"] != second_page[0]["id"]
    assert list_lessons(tmp_path, "demo", query_text="payment", limit=1, offset=2) == []

    with context_connection(tmp_path, "demo") as conn:
        conn.execute("delete from lessons_fts")
    assert rebuild_fts_index(tmp_path, "demo") == 3
    assert count_lessons(tmp_path, "demo", query_text="profile") == 1


def test_context_retrieval_keeps_old_exact_match_at_ten_thousand_scale(tmp_path) -> None:
    target = normalize_lesson(
        "demo",
        {
            **active_lesson(10001, path="/api/target", title="exact target rule"),
            "updated_at": "2000-01-01T00:00:00",
        },
    )
    noise = [
        normalize_lesson(
            "demo",
            {**active_lesson(index), "updated_at": "2099-01-01T00:00:00"},
        )
        for index in range(1, 10001)
    ]
    placeholders = ",".join(["?"] * len(LESSON_COLUMNS))
    with context_connection(tmp_path, "demo") as conn:
        conn.executemany(
            f"insert into lessons({','.join(LESSON_COLUMNS)}) values ({placeholders})",
            [[item.get(column) for column in LESSON_COLUMNS] for item in [target, *noise]],
        )

    candidates = retrieve_candidates(
        tmp_path,
        "demo",
        [Endpoint(method="GET", path="/api/target")],
        limit=50,
    )
    assert candidates
    assert candidates[0]["id"] == target["id"]
    assert len(candidates) == 50


def test_dependency_requires_confirmation_and_successful_verification(tmp_path) -> None:
    producer = Endpoint(
        method="GET",
        path="/api/generate",
        responses={"200": {"schema": {"type": "object", "properties": {"email": {"type": "string"}}}}},
    )
    consumer = Endpoint(
        method="GET",
        path="/api/check/{email}",
        parameters=[{"name": "email", "in": "path"}],
    )
    infer_dependency_candidates(tmp_path, "demo", [producer, consumer])
    lesson = list_lessons(tmp_path, "demo", lesson_type="data_dependency")[0]

    assert retrieve_candidates(tmp_path, "demo", [consumer]) == []
    update_lesson_review(
        tmp_path,
        "demo",
        lesson["id"],
        lifecycle_status="active",
        review_status="confirmed",
    )
    assert retrieve_candidates(tmp_path, "demo", [consumer]) == []
    update_dependency_status(tmp_path, "demo", lesson["id"], "verified", "verified_safe", "验收验证通过")
    assert retrieve_candidates(tmp_path, "demo", [consumer])[0]["id"] == lesson["id"]


def test_context_pack_retention_dry_run_and_reconciliation(tmp_path) -> None:
    base = datetime.now()
    for index in range(3):
        save_context_pack(
            tmp_path,
            "demo",
            {
                "id": f"pack-retention-{index}",
                "task_id": f"task-{index}",
                "task_type": "case_generation",
                "model": "demo",
                "context_budget": 100,
                "used_tokens": 10,
                "created_at": (base - timedelta(minutes=index)).isoformat(timespec="seconds"),
            },
        )
    orphan = project_context_packs_dir(tmp_path, "demo") / "pack-orphan.json"
    orphan.write_text("{}\n", encoding="utf-8")

    preview = prune_context_packs(tmp_path, "demo", max_records=2, max_age_days=0, dry_run=True)
    assert preview["deleted_records"] == 1
    assert preview["orphan_files"] == 1
    assert len(list_context_packs(tmp_path, "demo")) == 3
    assert orphan.exists()

    result = prune_context_packs(tmp_path, "demo", max_records=2, max_age_days=0)
    assert result["deleted_records"] == 1
    assert result["deleted_files"] == 1
    assert result["orphan_files"] == 1
    assert len(list_context_packs(tmp_path, "demo")) == 2
    assert not orphan.exists()

    retained_id = list_context_packs(tmp_path, "demo")[0]["id"]
    retained_file = project_context_packs_dir(tmp_path, "demo") / f"{retained_id}.json"
    retained_file.unlink()
    reconcile = prune_context_packs(tmp_path, "demo", max_records=2, max_age_days=0)
    assert reconcile["missing_files"] == 1
    assert reconcile["repaired_files"] == 1
    assert retained_file.exists()

    importable = project_context_packs_dir(tmp_path, "demo") / "pack-importable.json"
    importable.write_text(
        json.dumps(
            {
                "id": "pack-importable",
                "project": "demo",
                "task_id": "legacy-task",
                "task_type": "case_generation",
                "model": "legacy",
                "context_budget": 50,
                "used_tokens": 5,
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
        ),
        encoding="utf-8",
    )
    imported = prune_context_packs(tmp_path, "demo", max_records=3, max_age_days=0)
    assert imported["orphan_files"] == 1
    assert imported["importable_files"] == 1
    assert imported["imported_files"] == 1
    assert len(list_context_packs(tmp_path, "demo")) == 3
    assert importable.exists()


def test_governance_features_are_independently_configurable_and_visible(tmp_path) -> None:
    config = load_config(tmp_path)
    config["governance"]["review_cases"] = False
    save_config(tmp_path, config)

    assert _governance_feature_enabled(tmp_path, "review_reports") is True
    assert _governance_feature_enabled(tmp_path, "review_cases") is False
    html = render_config(tmp_path, "demo", urlparse("/config"))
    assert "报告质量审查" in html
    assert "用例质量审查" in html
    assert "经验候选治理" in html
    assert "低置信度人工确认" in html
    assert "max_candidates_per_batch" in html


def test_openapi_resolves_refs_overrides_parameters_and_preserves_governance_fields() -> None:
    document = {
        "openapi": "3.1.0",
        "servers": [{"url": "https://{region}.example.com/v1", "variables": {"region": {"default": "cn"}}}],
        "security": [{"bearerAuth": []}],
        "paths": {
            "/users/{id}": {
                "parameters": [{"$ref": "#/components/parameters/UserId"}],
                "get": {
                    "operationId": "getUser",
                    "deprecated": True,
                    "parameters": [
                        {"name": "id", "in": "path", "required": True, "description": "operation override", "schema": {"type": "integer"}}
                    ],
                    "requestBody": {"$ref": "#/components/requestBodies/UserBody"},
                    "responses": {"200": {"$ref": "#/components/responses/UserResponse"}},
                },
            }
        },
        "components": {
            "parameters": {
                "UserId": {"name": "id", "in": "path", "required": True, "description": "path default", "schema": {"type": "string"}}
            },
            "requestBodies": {
                "UserBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/User"}}}}
            },
            "responses": {
                "UserResponse": {"description": "ok", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/User"}}}}
            },
            "schemas": {
                "User": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "manager": {"$ref": "#/components/schemas/User"},
                    },
                }
            },
        },
    }

    endpoints, metadata = parse_api_document(json.dumps(document))
    assert metadata["base_url"] == "https://cn.example.com/v1"
    assert metadata["spec_version"] == "3.1.0"
    endpoint = endpoints[0]
    assert endpoint.operation_id == "getUser"
    assert endpoint.deprecated is True
    assert endpoint.security == [{"bearerAuth": []}]
    assert len(endpoint.parameters) == 1
    assert endpoint.parameters[0]["description"] == "operation override"
    assert endpoint.request_body["content"]["application/json"]["schema"]["properties"]["name"]["type"] == "string"
    assert endpoint.responses["200"]["description"] == "ok"
    assert any(case.design_method == "auth_error" for case in generate_cases(endpoints))


def test_openapi_resolves_relative_external_json_refs(tmp_path) -> None:
    components = {
        "parameters": {
            "TraceId": {"name": "X-Trace-Id", "in": "header", "required": True, "schema": {"type": "string"}}
        }
    }
    external = tmp_path / "components.json"
    external.write_text(json.dumps(components), encoding="utf-8")
    source = tmp_path / "openapi.json"
    document = {
        "openapi": "3.0.3",
        "paths": {
            "/health": {
                "get": {
                    "parameters": [{"$ref": "components.json#/parameters/TraceId"}],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    source.write_text(json.dumps(document), encoding="utf-8")

    endpoints, _ = parse_api_document(source.read_text(encoding="utf-8"), source=str(source))
    assert endpoints[0].parameters[0]["name"] == "X-Trace-Id"


def test_swagger2_body_refs_base_url_and_security_are_normalized() -> None:
    document = {
        "swagger": "2.0",
        "schemes": ["https"],
        "host": "api.example.com",
        "basePath": "/v2/",
        "consumes": ["application/json"],
        "security": [{"apiKey": []}],
        "parameters": {
            "PetBody": {
                "name": "pet",
                "in": "body",
                "required": True,
                "schema": {"$ref": "#/definitions/Pet"},
            }
        },
        "definitions": {
            "Pet": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}
        },
        "paths": {
            "/pets": {
                "post": {
                    "parameters": [{"$ref": "#/parameters/PetBody"}],
                    "responses": {"201": {"description": "created"}},
                }
            }
        },
    }

    endpoints, metadata = parse_api_document(json.dumps(document))
    endpoint = endpoints[0]
    assert metadata["base_url"] == "https://api.example.com/v2"
    assert endpoint.parameters == []
    schema = endpoint.request_body["content"]["application/json"]["schema"]
    assert schema["properties"]["name"]["type"] == "string"
    assert endpoint.security == [{"apiKey": []}]


def test_openapi_reports_broken_refs() -> None:
    document = {
        "openapi": "3.0.3",
        "paths": {
            "/broken": {
                "get": {
                    "parameters": [{"$ref": "#/components/parameters/Missing"}],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    with pytest.raises(ValueError, match="指向不存在的位置"):
        parse_api_document(json.dumps(document))


def test_legacy_json_path_array_syntax_stays_compatible() -> None:
    payload = {"items": [{"id": 7}, {"id": 9}]}
    assert normalize_json_path("$.items.0.id") == "$.items[0].id"
    assert json_path(payload, "$.items.0.id") == 7
    assert json_path(payload, "$.items[5].id") is None


def test_standard_json_path_wildcards_filters_recursive_descent_and_slices() -> None:
    pytest.importorskip("jsonpath_ng")
    payload = {
        "store": {
            "books": [
                {"id": 1, "price": 8, "title": "A"},
                {"id": 2, "price": 18, "title": "B"},
                {"id": 3, "price": 28, "title": "C"},
            ]
        }
    }
    assert json_path(payload, "$.store.books[*].title") == ["A", "B", "C"]
    assert json_path(payload, "$.store.books[?(@.price > 20)].id") == 3
    assert json_path(payload, "$..id") == [1, 2, 3]
    assert json_path(payload, "$.store.books[0:2].id") == [1, 2]
    assert json_path(payload, "$.store.books[?(") is None


def test_openapi_yaml_and_external_yaml_refs(tmp_path) -> None:
    pytest.importorskip("yaml")
    components = tmp_path / "components.yaml"
    components.write_text(
        """
parameters:
  Limit:
    name: limit
    in: query
    required: false
    schema:
      type: integer
      default: 10
""".strip(),
        encoding="utf-8",
    )
    source = tmp_path / "openapi.yaml"
    text = """
openapi: 3.0.3
servers:
  - url: https://api.example.com/v1
paths:
  /items:
    get:
      operationId: listItems
      parameters:
        - $ref: components.yaml#/parameters/Limit
      responses:
        '200':
          description: ok
""".strip()
    source.write_text(text, encoding="utf-8")

    endpoints, metadata = parse_api_document(text, source=str(source))
    assert metadata["source_format"] == "yaml"
    assert metadata["base_url"] == "https://api.example.com/v1"
    assert endpoints[0].operation_id == "listItems"
    assert endpoints[0].parameters[0]["schema"]["default"] == 10


def test_unknown_success_response_uses_2xx_class_without_inventing_200() -> None:
    assertion = success_assertions(Endpoint(method="POST", path="/jobs", responses={}))[0]
    assert assertion.operator == "is_2xx"
    assert assertion.expected is None
    assert compare(201, assertion.operator, assertion.expected)
    assert not compare(400, assertion.operator, assertion.expected)


def test_environment_resolves_inline_variables_and_fails_closed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DELIVERY_TOKEN", "token-value")
    monkeypatch.setenv("DELIVERY_HOST", "api.example.test")
    environment = current_environment(
        {
            "default_environment": "local",
            "environments": {
                "local": {
                    "base_url": "https://${DELIVERY_HOST}/v1",
                    "headers": {"Authorization": "Bearer ${DELIVERY_TOKEN}"},
                }
            },
        }
    )
    assert environment["headers"]["Authorization"] == "Bearer token-value"
    monkeypatch.delenv("DELIVERY_HOST", raising=False)
    with pytest.raises(ValueError, match="DELIVERY_HOST"):
        current_environment(
            {
                "default_environment": "local",
                "environments": {"local": {"base_url": "https://${DELIVERY_HOST}"}},
            }
        )


def test_openapi_parameter_serialization_is_preserved_for_execution() -> None:
    endpoint = Endpoint(
        method="GET",
        path="/items/{coords}",
        parameters=[
            {"name": "coords", "in": "path", "style": "label", "explode": True},
            {"name": "filter", "in": "query", "style": "deepObject", "explode": True},
            {"name": "tags", "in": "query", "style": "form", "explode": True},
        ],
    )
    rules = parameter_serialization(endpoint)
    assert apply_path_params(endpoint.path, {"coords": {"x": 1, "y": 2}}, rules) == "/items/.x=1.y=2"
    assert encode_query({"filter": {"active": True}, "tags": ["a", "b"]}, rules) == (
        "filter%5Bactive%5D=true&tags=a&tags=b"
    )


def test_file_request_body_is_bounded_and_not_embedded_in_snapshot(tmp_path) -> None:
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"abc")
    headers = {"Content-Type": "application/octet-stream"}
    assert encode_request_body(headers, {"$iatest_file": str(payload)}, max_bytes=3)["content"] == b"abc"
    with pytest.raises(ValueError, match="超过"):
        encode_request_body(headers, {"$iatest_file": str(payload)}, max_bytes=2)


def test_remote_openapi_cannot_reference_local_files() -> None:
    document = {
        "openapi": "3.0.3",
        "paths": {
            "/unsafe": {
                "get": {
                    "parameters": [{"$ref": "file:///etc/passwd#/parameters/token"}],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    with pytest.raises(ValueError, match="不能引用本地文件"):
        parse_api_document(json.dumps(document), source="https://example.test/openapi.json")


def test_context_history_retention_removes_only_old_records(tmp_path) -> None:
    config = load_config(tmp_path)
    config["context"]["retention"]["audit_logs"]["max_records"] = 10
    save_config(tmp_path, config)
    for index in range(12):
        record_operational_error(
            tmp_path,
            "demo",
            component="retention",
            operation=str(index),
            error=RuntimeError(f"failure-{index}"),
        )
    preview = prune_context_history(tmp_path, "demo", dry_run=True)
    assert preview["audit_logs"] == 2
    assert len(recent_audit_logs(tmp_path, "demo", limit=20)) == 12
    removed = prune_context_history(tmp_path, "demo")
    assert removed["audit_logs"] == 2
    assert len(recent_audit_logs(tmp_path, "demo", limit=20)) == 10


def test_case_ids_remain_stable_after_retire_and_append() -> None:
    cases = [
        ApiTestCaseModel(id="TC-API-0001", title="one", method="GET", path="/api/one"),
        ApiTestCaseModel(id="TC-API-0003", title="three", method="GET", path="/api/three"),
        ApiTestCaseModel(id="TC-API-0001", title="new duplicate", method="GET", path="/api/new"),
    ]
    normalized = normalize_cases(cases)
    assert [item.id for item in normalized[:2]] == ["TC-API-0001", "TC-API-0003"]
    assert normalized[2].id == "TC-API-0004"


def test_endpoint_maintenance_includes_setup_and_cleanup_dependencies() -> None:
    case = ApiTestCaseModel(
        id="TC-USERS-0001",
        title="query user",
        method="GET",
        path="/users/{id}",
        setup_steps=[ApiTestStepModel(name="create", method="POST", path="/users")],
        cleanup_steps=[ApiTestStepModel(name="delete", method="DELETE", path="/users/{id}")],
    )
    assert impacted_cases([case], ["POST /users"]) == [case]
    assert impacted_cases([case], ["DELETE /users/{id}"]) == [case]
