from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from dataclasses import asdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ai_apitest.ai.provider import ModelResponse, TokenUsage
from ai_apitest.core.paths import project_agent_db, safe_project_name
from ai_apitest.core.redaction import redact, sensitive_key, redact_text


SCHEMA = """
create table if not exists agent_runs(
    id text primary key,
    project text not null,
    role_id text not null,
    role_version text not null,
    model_role text not null,
    provider text not null,
    model text not null,
    status text not null,
    prompt_hash text not null,
    artifact_hash text default '',
    artifact_json text default '',
    input_tokens integer not null default 0,
    output_tokens integer not null default 0,
    total_tokens integer not null default 0,
    turn_count integer not null default 0,
    tool_call_count integer not null default 0,
    error text default '',
    metadata text not null default '{}',
    started_at text not null,
    completed_at text default ''
);
create table if not exists agent_turns(
    id integer primary key autoincrement,
    run_id text not null,
    turn_index integer not null,
    finish_reason text default '',
    input_tokens integer not null default 0,
    output_tokens integer not null default 0,
    total_tokens integer not null default 0,
    response_hash text not null,
    text_excerpt text default '',
    tool_call_count integer not null default 0,
    created_at text not null,
    foreign key(run_id) references agent_runs(id)
);
create table if not exists agent_tool_calls(
    id integer primary key autoincrement,
    run_id text not null,
    turn_index integer not null,
    call_id text not null,
    name text not null,
    arguments_hash text not null,
    arguments_json text default '{}',
    result_hash text default '',
    result_excerpt text default '',
    status text not null,
    error text default '',
    duration_ms integer not null default 0,
    created_at text not null,
    foreign key(run_id) references agent_runs(id)
);
create table if not exists agent_evidence(
    id text primary key,
    run_id text not null,
    turn_index integer not null,
    call_id text not null,
    tool_name text not null,
    content_hash text not null,
    content text not null,
    content_chars integer not null default 0,
    metadata text not null default '{}',
    created_at text not null,
    foreign key(run_id) references agent_runs(id)
);
create table if not exists agent_checkpoints(
    id integer primary key autoincrement,
    run_id text not null,
    checkpoint_index integer not null,
    reason text not null,
    input_tokens_before integer not null default 0,
    input_tokens_after integer not null default 0,
    ratio_before real not null default 0,
    ratio_after real not null default 0,
    state_json text not null,
    messages_hash text not null,
    created_at text not null,
    foreign key(run_id) references agent_runs(id),
    unique(run_id, checkpoint_index)
);
create index if not exists idx_agent_runs_project_started on agent_runs(project, started_at desc);
create index if not exists idx_agent_turns_run on agent_turns(run_id, turn_index);
create index if not exists idx_agent_tools_run on agent_tool_calls(run_id, turn_index);
create index if not exists idx_agent_evidence_run on agent_evidence(run_id, turn_index);
create index if not exists idx_agent_checkpoints_run on agent_checkpoints(run_id, checkpoint_index);
"""
_SCHEMA_READY: dict[str, int] = {}
_SCHEMA_LOCK = threading.Lock()


class AgentRunRepository:
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
            ensure_agent_schema_once(conn, self.path)
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def start(
        self,
        *,
        role_id: str,
        role_version: str,
        model_role: str,
        provider: str,
        model: str,
        prompt: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        run_id = "agent-" + uuid.uuid4().hex[:20]
        with self.connection() as conn:
            conn.execute(
                """
                insert into agent_runs(
                    id, project, role_id, role_version, model_role, provider, model,
                    status, prompt_hash, metadata, started_at
                ) values (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)
                """,
                (
                    run_id,
                    self.project,
                    role_id,
                    role_version,
                    model_role,
                    provider,
                    model,
                    digest(prompt),
                    compact_json(redact_sensitive(metadata or {}), 12000),
                    now_iso(),
                ),
            )
        return run_id

    def record_turn(self, run_id: str, turn_index: int, response: ModelResponse, *, request_metrics: dict | None = None, error: str = "") -> None:
        with self.connection() as conn:
            conn.execute(
                """
                insert into agent_turns(
                    run_id, turn_index, finish_reason, input_tokens, output_tokens,
                    total_tokens, response_hash, text_excerpt, tool_call_count, created_at,
                    usage_json, request_metrics_json, error
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    turn_index,
                    response.finish_reason,
                    response.usage.input_tokens,
                    response.usage.output_tokens,
                    response.usage.total_tokens,
                    digest(response.text + compact_json([call.to_openai_dict() for call in response.tool_calls], 12000)),
                    redact_text(response.text)[:4000],
                    len(response.tool_calls),
                    now_iso(),
                    compact_json(redact_sensitive(asdict(response.usage)), 12000),
                    compact_json(redact_sensitive(request_metrics or {}), 12000),
                    redact_text(error)[:2000],
                ),
            )

    def record_tool_call(
        self,
        run_id: str,
        turn_index: int,
        *,
        call_id: str,
        name: str,
        arguments: dict[str, Any],
        result: str,
        status: str,
        error: str = "",
        duration_ms: int = 0,
    ) -> None:
        arguments_json = compact_json(redact_sensitive(arguments), 12000)
        safe_result = redact_text(result)
        with self.connection() as conn:
            conn.execute(
                """
                insert into agent_tool_calls(
                    run_id, turn_index, call_id, name, arguments_hash, arguments_json,
                    result_hash, result_excerpt, status, error, duration_ms, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    turn_index,
                    call_id,
                    name,
                    digest(arguments_json),
                    arguments_json,
                    digest(result),
                    safe_result[:4000],
                    status,
                    redact_text(error)[:2000],
                    max(0, int(duration_ms)),
                    now_iso(),
                ),
            )

    def save_evidence(
        self,
        run_id: str,
        turn_index: int,
        *,
        call_id: str,
        tool_name: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        evidence_id = "evidence-" + uuid.uuid4().hex[:20]
        safe_content = redact_text(str(content or ""))
        with self.connection() as conn:
            conn.execute(
                """
                insert into agent_evidence(
                    id, run_id, turn_index, call_id, tool_name, content_hash,
                    content, content_chars, metadata, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    run_id,
                    max(0, int(turn_index)),
                    str(call_id or ""),
                    str(tool_name or ""),
                    digest(safe_content),
                    safe_content,
                    len(safe_content),
                    compact_json(redact_sensitive(metadata or {}), 12000),
                    now_iso(),
                ),
            )
        return evidence_id

    def read_evidence(
        self,
        run_id: str,
        evidence_id: str,
        *,
        offset: int = 0,
        max_chars: int = 8000,
    ) -> dict[str, Any]:
        safe_offset = max(0, int(offset))
        safe_max = max(500, min(16000, int(max_chars)))
        with self.connection() as conn:
            row = conn.execute(
                """
                select id, turn_index, call_id, tool_name, content_hash, content, content_chars, metadata
                from agent_evidence where id = ? and run_id = ?
                """,
                (str(evidence_id), str(run_id)),
            ).fetchone()
        if row is None:
            raise KeyError(f"任务证据不存在或不属于当前运行：{evidence_id}")
        content = str(row["content"] or "")
        chunk = content[safe_offset : safe_offset + safe_max]
        next_offset = safe_offset + len(chunk)
        return {
            "evidence_id": str(row["id"]),
            "tool": str(row["tool_name"]),
            "turn_index": int(row["turn_index"]),
            "content_hash": str(row["content_hash"]),
            "offset": safe_offset,
            "next_offset": next_offset,
            "has_more": next_offset < len(content),
            "content_chars": int(row["content_chars"]),
            "content": chunk,
        }

    def list_evidence(self, run_id: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                select id, run_id, turn_index, call_id, tool_name, content_hash,
                       content_chars, metadata, created_at
                from agent_evidence where run_id = ? order by turn_index, created_at
                """,
                (str(run_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_checkpoint(
        self,
        run_id: str,
        *,
        checkpoint_index: int,
        reason: str,
        input_tokens_before: int,
        input_tokens_after: int,
        ratio_before: float,
        ratio_after: float,
        state: dict[str, Any],
        messages: list[dict[str, Any]],
    ) -> None:
        state_json = compact_json(redact_sensitive(state), 50000)
        messages_json = compact_json(redact_sensitive(messages), 50000)
        with self.connection() as conn:
            conn.execute(
                """
                insert or replace into agent_checkpoints(
                    run_id, checkpoint_index, reason, input_tokens_before,
                    input_tokens_after, ratio_before, ratio_after, state_json,
                    messages_hash, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    max(1, int(checkpoint_index)),
                    str(reason or "context_rebase"),
                    max(0, int(input_tokens_before)),
                    max(0, int(input_tokens_after)),
                    max(0.0, float(ratio_before)),
                    max(0.0, float(ratio_after)),
                    state_json,
                    digest(messages_json),
                    now_iso(),
                ),
            )

    def list_checkpoints(self, run_id: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                select * from agent_checkpoints where run_id = ?
                order by checkpoint_index
                """,
                (str(run_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def finish(
        self,
        run_id: str,
        *,
        status: str,
        usage: TokenUsage,
        turn_count: int,
        tool_call_count: int,
        artifact: Any = None,
        error: str = "",
    ) -> None:
        artifact_json = compact_json(redact_sensitive(artifact), 50000) if artifact is not None else ""
        with self.connection() as conn:
            conn.execute(
                """
                update agent_runs set status = ?, artifact_hash = ?, artifact_json = ?, input_tokens = ?,
                    output_tokens = ?, total_tokens = ?, turn_count = ?, tool_call_count = ?,
                    error = ?, completed_at = ? where id = ?
                """,
                (
                    status,
                    digest(artifact_json) if artifact_json else "",
                    artifact_json,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.total_tokens,
                    turn_count,
                    tool_call_count,
                    redact_text(error)[:4000],
                    now_iso(),
                    run_id,
                ),
            )
            conn.execute("update agent_runs set usage_json = ? where id = ?", (compact_json(redact_sensitive(asdict(usage)), 12000), run_id))
            from ai_apitest.core.config import load_config

            retain = max(10, int(load_config(self.root).get("retention", {}).get("agent_runs", 500)))
            prune_agent_runs(conn, self.project, retain)

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("select * from agent_runs where id = ?", (run_id,)).fetchone()
            return dict(row) if row else None

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                select r.*,
                       (select count(*) from agent_checkpoints c where c.run_id = r.id) as context_rebase_count,
                       (select count(*) from agent_evidence e where e.run_id = r.id) as evidence_count
                from agent_runs r where r.project = ?
                order by r.started_at desc limit ?
                """,
                (self.project, max(1, min(500, int(limit)))),
            ).fetchall()
            return [dict(row) for row in rows]


def compact_json(value: Any, limit: int) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        text = json.dumps(str(value), ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"


def ensure_agent_schema_once(conn: sqlite3.Connection, path: Path) -> None:
    key = str(path.resolve())
    inode = path.stat().st_ino
    if _SCHEMA_READY.get(key) == inode:
        return
    with _SCHEMA_LOCK:
        inode = path.stat().st_ino
        if _SCHEMA_READY.get(key) == inode:
            return
        conn.executescript(SCHEMA)
        columns = {str(row[1]) for row in conn.execute("pragma table_info(agent_runs)").fetchall()}
        if "artifact_json" not in columns:
            conn.execute("alter table agent_runs add column artifact_json text default ''")
        if "usage_json" not in columns:
            conn.execute("alter table agent_runs add column usage_json text not null default '{}'")
        turn_columns = {str(row[1]) for row in conn.execute("pragma table_info(agent_turns)")}
        for column in ("usage_json", "request_metrics_json"):
            if column not in turn_columns:
                conn.execute(f"alter table agent_turns add column {column} text not null default '{{}}'")
        if "error" not in turn_columns:
            conn.execute("alter table agent_turns add column error text not null default ''")
        _SCHEMA_READY[key] = inode


def prune_agent_runs(conn: sqlite3.Connection, project: str, retain: int) -> None:
    rows = conn.execute(
        """
        select id from agent_runs where project = ? and status != 'running'
        order by started_at desc limit -1 offset ?
        """,
        (project, retain),
    ).fetchall()
    ids = [str(row[0]) for row in rows]
    if not ids:
        return
    conn.executemany("delete from agent_tool_calls where run_id = ?", [(item,) for item in ids])
    conn.executemany("delete from agent_turns where run_id = ?", [(item,) for item in ids])
    conn.executemany("delete from agent_evidence where run_id = ?", [(item,) for item in ids])
    conn.executemany("delete from agent_checkpoints where run_id = ?", [(item,) for item in ids])
    conn.executemany("delete from agent_runs where id = ?", [(item,) for item in ids])


def digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def redact_sensitive(value: Any) -> Any:
    return redact(value)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
