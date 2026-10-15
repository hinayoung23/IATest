from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ai_apitest.core.paths import project_agent_db, safe_project_name
from ai_apitest.data.agent_runs import redact_sensitive, redact_text


SCHEMA = """
create table if not exists quality_audit_sessions(
    id text primary key,
    project text not null,
    artifact_type text not null,
    status text not null,
    threshold real not null,
    max_rounds integer not null,
    unique_model_count integer not null default 0,
    selected_candidate_id text default '',
    final_score real,
    decision text default '',
    reason text default '',
    metadata text not null default '{}',
    started_at text not null,
    completed_at text default ''
);
create table if not exists quality_artifact_candidates(
    id text primary key,
    session_id text not null,
    round_index integer not null,
    artifact_hash text not null,
    evidence_hash text not null default '',
    rubric_version text not null default '',
    audit_input_hash text not null default '',
    artifact_json text not null,
    producer_model_key text default '',
    producer_run_id text default '',
    hard_gate_pass integer not null default 0,
    hard_gate_issues text not null default '[]',
    audit_complete integer not null default 0,
    passed integer not null default 0,
    average_score real,
    dimension_averages text not null default '{}',
    score_spread real,
    critical_issue_count integer not null default 0,
    created_at text not null,
    unique(session_id, round_index),
    foreign key(session_id) references quality_audit_sessions(id)
);
create table if not exists quality_audit_scores(
    id integer primary key autoincrement,
    session_id text not null,
    candidate_id text not null,
    judge_key text not null,
    provider text not null,
    model text not null,
    status text not null,
    reliability real,
    completeness real,
    reasonableness real,
    non_redundancy real,
    total_score real,
    confidence text default '',
    summary text default '',
    issues_json text not null default '[]',
    agent_run_id text default '',
    error text default '',
    created_at text not null,
    unique(candidate_id, judge_key),
    foreign key(session_id) references quality_audit_sessions(id),
    foreign key(candidate_id) references quality_artifact_candidates(id)
);
create index if not exists idx_quality_sessions_project
    on quality_audit_sessions(project, started_at desc);
create index if not exists idx_quality_candidates_session
    on quality_artifact_candidates(session_id, round_index);
create index if not exists idx_quality_scores_candidate
    on quality_audit_scores(candidate_id, judge_key);
create table if not exists quality_audit_baselines(
    project text not null,
    judge_key text not null,
    artifact_type text not null,
    payload text not null,
    updated_at text not null,
    primary key(project, judge_key, artifact_type)
);
"""

_SCHEMA_READY: dict[str, int] = {}
_SCHEMA_LOCK = threading.Lock()


class QualityAuditRepository:
    def __init__(self, root: Path, project: str) -> None:
        self.root = root
        self.project = safe_project_name(project)
        self.path = project_agent_db(root, self.project)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("pragma journal_mode=WAL")
            conn.execute("pragma foreign_keys=ON")
            ensure_schema_once(conn, self.path)
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def start_session(
        self,
        *,
        artifact_type: str,
        threshold: float,
        max_rounds: int,
        unique_model_count: int,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        session_id = "quality-" + uuid.uuid4().hex[:20]
        with self.connection() as conn:
            conn.execute(
                """
                insert into quality_audit_sessions(
                    id, project, artifact_type, status, threshold, max_rounds,
                    unique_model_count, metadata, started_at
                ) values (?, ?, ?, 'running', ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    self.project,
                    str(artifact_type),
                    float(threshold),
                    max(1, int(max_rounds)),
                    max(0, int(unique_model_count)),
                    json_text(redact_sensitive(metadata or {})),
                    now_iso(),
                ),
            )
        return session_id

    def add_candidate(
        self,
        session_id: str,
        *,
        round_index: int,
        artifact: Any,
        evidence: Any = None,
        rubric_version: str = "",
        producer_model_key: str = "",
        producer_run_id: str = "",
        hard_gate_pass: bool,
        hard_gate_issues: list[dict[str, Any]] | None = None,
    ) -> str:
        candidate_id = "candidate-" + uuid.uuid4().hex[:20]
        artifact_json = json_text(redact_sensitive(artifact), max_chars=5_000_000)
        # Hash originals in memory; persist only their redacted representation.
        # Two different credentials must not share an audit cache entry merely
        # because both become the same redaction marker.
        artifact_hash = digest(json_text(artifact, max_chars=5_000_000))
        evidence_hash = digest(json_text(evidence, max_chars=5_000_000))
        gate_hash = digest(json_text(hard_gate_issues or []))
        audit_input_hash = digest(f"{artifact_hash}\n{evidence_hash}\n{rubric_version}\n{gate_hash}")
        with self.connection() as conn:
            conn.execute(
                """
                insert into quality_artifact_candidates(
                    id, session_id, round_index, artifact_hash, evidence_hash,
                    rubric_version, audit_input_hash, artifact_json,
                    producer_model_key, producer_run_id, hard_gate_pass,
                    hard_gate_issues, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    session_id,
                    max(1, int(round_index)),
                    artifact_hash,
                    evidence_hash,
                    str(rubric_version or ""),
                    audit_input_hash,
                    artifact_json,
                    str(producer_model_key or ""),
                    str(producer_run_id or ""),
                    int(bool(hard_gate_pass)),
                    json_text(redact_sensitive(hard_gate_issues or [])),
                    now_iso(),
                ),
            )
        return candidate_id

    def cached_score(self, audit_input_hash: str, judge_key: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                select q.* from quality_audit_scores q
                join quality_artifact_candidates c on c.id = q.candidate_id
                join quality_audit_sessions s on s.id = c.session_id
                where s.project = ? and c.audit_input_hash = ?
                  and c.hard_gate_pass = 1
                  and q.judge_key = ? and q.status = 'completed'
                order by q.created_at desc limit 1
                """,
                (self.project, str(audit_input_hash), str(judge_key)),
            ).fetchone()
        return decode_row(row) if row else None

    def get_baseline(self, judge_key: str, artifact_type: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("select payload from quality_audit_baselines where project=? and judge_key=? and artifact_type=?", (self.project, judge_key, artifact_type)).fetchone()
            try:
                baseline = json.loads(row[0]) if row else None
                if not isinstance(baseline, dict):
                    return None
                approved = conn.execute("select c.artifact_json from quality_artifact_candidates c join quality_audit_sessions s on s.id=c.session_id join quality_audit_scores q on q.candidate_id=c.id where c.id=? and s.project=? and s.artifact_type=? and c.hard_gate_pass=1 and c.audit_complete=1 and c.passed=1 and q.judge_key=? and q.status='completed' and q.confidence='high' and q.issues_json='[]'", (baseline["candidate_id"], self.project, artifact_type, judge_key)).fetchone()
                from ai_apitest.agents.token_optimization import canonical
                if not approved or canonical(json.loads(approved[0])) != canonical(baseline["bundle"]["artifact"]):
                    return None
                return baseline
            except (ValueError, TypeError, KeyError):
                return None

    def save_baseline(self, judge_key: str, artifact_type: str, payload: dict) -> None:
        safe = redact_sensitive(payload)
        text = json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
        # Never treat truncated or redacted originals as equivalence proof.
        if safe != payload or len(text.encode()) > 2_000_000:
            return
        with self.connection() as conn:
            conn.execute("insert or replace into quality_audit_baselines values(?,?,?,?,?)", (self.project, judge_key, artifact_type, text, now_iso()))
            conn.execute("delete from quality_audit_baselines where rowid in (select rowid from quality_audit_baselines where project=? order by updated_at desc, rowid desc limit -1 offset 100)", (self.project,))

    def get_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "select * from quality_artifact_candidates where id = ?",
                (str(candidate_id),),
            ).fetchone()
        return decode_row(row) if row else None

    def record_score(
        self,
        session_id: str,
        candidate_id: str,
        *,
        judge_key: str,
        provider: str,
        model: str,
        status: str,
        dimensions: dict[str, float] | None = None,
        total_score: float | None = None,
        confidence: str = "",
        summary: str = "",
        issues: list[dict[str, Any]] | None = None,
        agent_run_id: str = "",
        error: str = "",
        audit_scope: dict | None = None,
    ) -> None:
        dimensions = dimensions or {}
        with self.connection() as conn:
            conn.execute(
                """
                insert or replace into quality_audit_scores(
                    session_id, candidate_id, judge_key, provider, model, status,
                    reliability, completeness, reasonableness, non_redundancy,
                    total_score, confidence, summary, issues_json, agent_run_id,
                    error, created_at, audit_scope_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    candidate_id,
                    str(judge_key),
                    str(provider),
                    str(model),
                    str(status),
                    optional_float(dimensions.get("reliability")),
                    optional_float(dimensions.get("completeness")),
                    optional_float(dimensions.get("reasonableness")),
                    optional_float(dimensions.get("non_redundancy")),
                    optional_float(total_score),
                    str(confidence or ""),
                    redact_text(summary)[:4000],
                    json_text(redact_sensitive(issues or []), max_chars=100_000),
                    str(agent_run_id or ""),
                    redact_text(error)[:4000],
                    now_iso(),
                    json_text(audit_scope or {"mode": "full"}),
                ),
            )

    def finish_candidate(
        self,
        candidate_id: str,
        *,
        audit_complete: bool,
        passed: bool,
        average_score: float | None,
        dimension_averages: dict[str, float],
        score_spread: float | None,
        critical_issue_count: int,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                update quality_artifact_candidates
                set audit_complete = ?, passed = ?, average_score = ?,
                    dimension_averages = ?, score_spread = ?,
                    critical_issue_count = ?
                where id = ?
                """,
                (
                    int(bool(audit_complete)),
                    int(bool(passed)),
                    optional_float(average_score),
                    json_text(dimension_averages),
                    optional_float(score_spread),
                    max(0, int(critical_issue_count)),
                    candidate_id,
                ),
            )

    def finish_session(
        self,
        session_id: str,
        *,
        status: str,
        selected_candidate_id: str = "",
        final_score: float | None = None,
        decision: str,
        reason: str = "",
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                update quality_audit_sessions
                set status = ?, selected_candidate_id = ?, final_score = ?,
                    decision = ?, reason = ?, completed_at = ?
                where id = ?
                """,
                (
                    str(status),
                    str(selected_candidate_id or ""),
                    optional_float(final_score),
                    str(decision or ""),
                    redact_text(reason)[:4000],
                    now_iso(),
                    session_id,
                ),
            )
            prune_sessions(conn, self.project, 500)

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "select * from quality_audit_sessions where id = ? and project = ?",
                (str(session_id), self.project),
            ).fetchone()
        return decode_row(row) if row else None

    def list_recent(self, limit: int = 50, *, artifact_type: str = "") -> list[dict[str, Any]]:
        query = """
            select s.*,
                   (select count(*) from quality_artifact_candidates c where c.session_id = s.id) as candidate_count
            from quality_audit_sessions s
            where s.project = ?
        """
        values: list[Any] = [self.project]
        if artifact_type:
            query += " and s.artifact_type = ?"
            values.append(str(artifact_type))
        query += " order by s.started_at desc limit ?"
        values.append(max(1, min(500, int(limit))))
        with self.connection() as conn:
            rows = conn.execute(query, values).fetchall()
        return [decode_row(row) for row in rows]

    def session_detail(self, session_id: str) -> dict[str, Any] | None:
        session = self.get_session(session_id)
        if session is None:
            return None
        with self.connection() as conn:
            candidates = conn.execute(
                """
                select * from quality_artifact_candidates
                where session_id = ? order by round_index
                """,
                (str(session_id),),
            ).fetchall()
            scores = conn.execute(
                """
                select * from quality_audit_scores
                where session_id = ? order by candidate_id, judge_key
                """,
                (str(session_id),),
            ).fetchall()
        session["candidates"] = [decode_row(row) for row in candidates]
        session["scores"] = [decode_row(row) for row in scores]
        return session


def ensure_schema_once(conn: sqlite3.Connection, path: Path) -> None:
    key = str(path.resolve())
    inode = path.stat().st_ino
    if _SCHEMA_READY.get(key) == inode:
        return
    with _SCHEMA_LOCK:
        inode = path.stat().st_ino
        if _SCHEMA_READY.get(key) == inode:
            return
        conn.executescript(SCHEMA)
        columns = {
            str(row[1])
            for row in conn.execute("pragma table_info(quality_artifact_candidates)").fetchall()
        }
        for name in ("evidence_hash", "rubric_version", "audit_input_hash"):
            if name not in columns:
                conn.execute(
                    f"alter table quality_artifact_candidates add column {name} text not null default ''"
                )
        score_columns = {str(row[1]) for row in conn.execute("pragma table_info(quality_audit_scores)")}
        if "audit_scope_json" not in score_columns:
            conn.execute("alter table quality_audit_scores add column audit_scope_json text not null default '{}'")
        _SCHEMA_READY[key] = inode


def prune_sessions(conn: sqlite3.Connection, project: str, retain: int) -> None:
    rows = conn.execute(
        """
        select id from quality_audit_sessions
        where project = ? and status != 'running'
        order by started_at desc limit -1 offset ?
        """,
        (project, max(10, int(retain))),
    ).fetchall()
    ids = [str(row[0]) for row in rows]
    if not ids:
        return
    conn.executemany("delete from quality_audit_scores where session_id = ?", [(item,) for item in ids])
    conn.executemany("delete from quality_artifact_candidates where session_id = ?", [(item,) for item in ids])
    conn.executemany("delete from quality_audit_sessions where id = ?", [(item,) for item in ids])


def decode_row(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    for key in ("metadata", "artifact_json", "hard_gate_issues", "dimension_averages", "issues_json", "audit_scope_json"):
        if key in value:
            try:
                value[key] = json.loads(value[key] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
    return value


def json_text(value: Any, *, max_chars: int = 500_000) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(text) <= max_chars:
        return text
    return json.dumps(
        {
            "truncated": True,
            "content_hash": digest(text),
            "content_chars": len(text),
            "preview": text[: max(0, max_chars - 300)],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
