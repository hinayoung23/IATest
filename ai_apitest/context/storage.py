from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from ai_apitest.core.models import Assertion, CaseResult
from ai_apitest.core.models import Endpoint, TestCase
from ai_apitest.context.signatures import compute_exact_key, compute_semantic_key, is_contradictory
from ai_apitest.core.paths import (
    project_context_dir,
    project_context_db,
    project_context_exports_dir,
    project_context_packs_dir,
    safe_project_name,
)

SCHEMA_VERSION = 2

LESSON_COLUMNS = [
    "id",
    "project",
    "schema_version",
    "revision",
    "scope",
    "method",
    "path",
    "lesson_type",
    "lesson_subtype",
    "environment",
    "base_url_hash",
    "title",
    "content",
    "exact_key",
    "semantic_key",
    "lifecycle_status",
    "review_status",
    "verification_status",
    "source",
    "source_type",
    "confidence",
    "quality_score",
    "quality_reasons",
    "occurrence_count",
    "first_seen",
    "last_seen",
    "last_seen_source",
    "confirmed_at",
    "expires_at",
    "tags",
    "related_lesson_ids",
    "merge_candidate_ids",
    "created_at",
    "updated_at",
    "created_by",
    "updated_by",
    "metadata",
]


@dataclass(slots=True)
class UpsertResult:
    action: str
    lesson_id: str
    related_lesson_ids: list[str]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


@contextmanager
def context_connection(root: Path, project: str = "default") -> Iterator[sqlite3.Connection]:
    db = project_context_db(root, project)
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("pragma foreign_keys = on")
        conn.execute("pragma journal_mode = wal")
        conn.execute("pragma busy_timeout = 5000")
        ensure_schema(conn)
        yield conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        create table if not exists lessons(
            id text primary key,
            project text not null,
            schema_version integer not null default 2,
            revision integer not null default 1,
            scope text not null,
            method text,
            path text,
            lesson_type text not null,
            lesson_subtype text,
            environment text default '',
            base_url_hash text default '',
            title text not null,
            content text not null,
            exact_key text not null,
            semantic_key text not null,
            lifecycle_status text not null default 'pending',
            review_status text not null default 'unreviewed',
            verification_status text not null default 'unverified',
            source text not null,
            source_type text not null,
            confidence text not null default 'medium',
            quality_score real default 0.0,
            quality_reasons text default '[]',
            occurrence_count integer not null default 1,
            first_seen text not null,
            last_seen text not null,
            last_seen_source text,
            confirmed_at text,
            expires_at text,
            tags text default '[]',
            related_lesson_ids text default '[]',
            merge_candidate_ids text default '[]',
            created_at text not null,
            updated_at text not null,
            created_by text default 'system',
            updated_by text default 'system',
            metadata text default '{}'
        )
        """
    )
    for statement in [
        "create index if not exists idx_lessons_project on lessons(project)",
        "create index if not exists idx_lessons_exact_key on lessons(exact_key)",
        "create index if not exists idx_lessons_semantic_key on lessons(semantic_key)",
        "create index if not exists idx_lessons_scope_endpoint on lessons(project, scope, method, path)",
        "create index if not exists idx_lessons_lifecycle on lessons(lifecycle_status)",
        "create index if not exists idx_lessons_verification on lessons(verification_status)",
    ]:
        conn.execute(statement)
    conn.execute(
        """
        create table if not exists lesson_relations(
            id integer primary key autoincrement,
            lesson_id text not null,
            related_lesson_id text not null,
            relation_type text not null,
            notes text,
            created_at text not null,
            foreign key(lesson_id) references lessons(id),
            foreign key(related_lesson_id) references lessons(id)
        )
        """
    )
    conn.execute(
        """
        create table if not exists lesson_sources(
            id integer primary key autoincrement,
            lesson_id text not null,
            source text not null,
            source_type text not null,
            extracted_at text not null,
            foreign key(lesson_id) references lessons(id)
        )
        """
    )
    conn.execute(
        """
        create table if not exists context_packs(
            id text primary key,
            task_id text not null,
            task_type text not null,
            project text not null,
            model text not null,
            model_context_window integer,
            context_budget integer not null,
            used_tokens integer not null,
            reserve_output_tokens integer,
            included_lesson_ids text default '[]',
            excluded_lesson_ids text default '[]',
            final_prompt_hash text,
            created_at text not null
        )
        """
    )
    conn.execute(
        """
        create table if not exists extraction_jobs(
            id text primary key,
            trigger text not null,
            source text not null,
            project text not null,
            status text not null default 'pending',
            error text,
            max_retries integer not null default 3,
            retry_count integer not null default 0,
            retry_backoff text not null default 'exponential',
            next_retry_at text,
            created_at text not null,
            updated_at text not null
        )
        """
    )
    ensure_column(conn, "extraction_jobs", "payload", "text default '{}'")
    conn.execute(
        """
        create table if not exists audit_logs(
            id integer primary key autoincrement,
            action text not null,
            entity_type text not null,
            entity_id text not null,
            project text not null,
            actor text default 'system',
            detail text default '{}',
            created_at text not null
        )
        """
    )
    conn.execute(
        """
        create table if not exists execution_trends(
            id integer primary key autoincrement,
            run_id integer not null,
            project text not null,
            created_at text not null,
            total_cases integer not null,
            passed_cases integer not null,
            failed_cases integer not null,
            pass_rate real not null,
            failure_distribution text not null default '{}',
            endpoint_metrics text not null default '[]',
            latency_percentiles text not null default '{}',
            known_issue_status text not null default '{}',
            delta_from_previous text default '{}'
        )
        """
    )
    # Migration: add run_scope and case_ids columns for comparable trend analysis
    for col, col_type in [("run_scope", "text not null default ''"), ("case_ids", "text not null default '[]'")]:
        try:
            conn.execute(f"alter table execution_trends add column {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    conn.execute(
        """
        create table if not exists failure_patterns(
            id integer primary key autoincrement,
            project text not null,
            method text,
            path text,
            failure_category text not null,
            occurrence_count integer not null default 1,
            first_seen text not null,
            last_seen text not null,
            trend_direction text default 'stable',
            related_lesson_ids text default '[]',
            metadata text default '{}',
            updated_at text not null
        )
        """
    )
    conn.execute("create index if not exists idx_failure_patterns_project on failure_patterns(project)")
    conn.execute("create index if not exists idx_failure_patterns_endpoint on failure_patterns(method, path)")
    ensure_column(conn, "failure_patterns", "trend_direction", "text default 'stable'")
    ensure_column(conn, "failure_patterns", "related_lesson_ids", "text default '[]'")
    ensure_column(conn, "failure_patterns", "metadata", "text default '{}'")
    ensure_column(conn, "failure_patterns", "updated_at", "text")
    conn.execute(
        """
        create table if not exists schema_version(
            version integer primary key,
            applied_at text not null,
            description text
        )
        """
    )
    conn.execute(
        "insert or ignore into schema_version(version, applied_at, description) values (?, ?, ?)",
        (SCHEMA_VERSION, now_iso(), "context system v2 initial schema"),
    )
    ensure_fts(conn)


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"pragma table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"alter table {table} add column {column} {definition}")


def ensure_fts(conn: sqlite3.Connection) -> None:
    try:
        conn.execute(
            """
            create virtual table if not exists lessons_fts using fts5(
                id unindexed,
                title,
                content
            )
            """
        )
    except sqlite3.OperationalError:
        return


def migrate_project(root: Path, project: str = "default") -> Path:
    with context_connection(root, project):
        pass
    return project_context_db(root, project)


def normalize_lesson(project: str, lesson: dict[str, Any], actor: str = "system") -> dict[str, Any]:
    timestamp = now_iso()
    normalized = dict(lesson)
    normalized.setdefault("id", "les-" + uuid.uuid4().hex[:12])
    normalized["project"] = safe_project_name(str(normalized.get("project") or project))
    normalized.setdefault("schema_version", SCHEMA_VERSION)
    normalized.setdefault("revision", 1)
    normalized["scope"] = str(normalized.get("scope") or "project")
    normalized["method"] = str(normalized.get("method") or "").upper()
    normalized["path"] = str(normalized.get("path") or "")
    normalized["lesson_type"] = str(normalized.get("lesson_type") or "business_rule")
    normalized["lesson_subtype"] = str(normalized.get("lesson_subtype") or "")
    normalized["environment"] = str(normalized.get("environment") or "")
    normalized["base_url_hash"] = str(normalized.get("base_url_hash") or "")
    normalized["title"] = str(normalized.get("title") or normalized.get("content") or "未命名经验")[:200]
    normalized["content"] = str(normalized.get("content") or normalized["title"])
    normalized["exact_key"] = str(normalized.get("exact_key") or compute_exact_key(normalized))
    normalized["semantic_key"] = str(normalized.get("semantic_key") or compute_semantic_key(normalized))
    normalized.setdefault("lifecycle_status", "pending")
    normalized.setdefault("review_status", "unreviewed")
    normalized.setdefault("verification_status", "unverified")
    normalized["source"] = str(normalized.get("source") or "manual")
    normalized["source_type"] = str(normalized.get("source_type") or "manual_edit")
    normalized.setdefault("confidence", "medium")
    normalized.setdefault("quality_score", 0.0)
    normalized["quality_reasons"] = json_field(normalized.get("quality_reasons"), [])
    normalized.setdefault("occurrence_count", 1)
    normalized.setdefault("first_seen", timestamp)
    normalized.setdefault("last_seen", timestamp)
    normalized.setdefault("last_seen_source", normalized["source"])
    normalized.setdefault("confirmed_at", None)
    normalized.setdefault("expires_at", None)
    normalized["tags"] = json_field(normalized.get("tags"), [])
    normalized["related_lesson_ids"] = json_field(normalized.get("related_lesson_ids"), [])
    normalized["merge_candidate_ids"] = json_field(normalized.get("merge_candidate_ids"), [])
    normalized.setdefault("created_at", timestamp)
    normalized.setdefault("updated_at", timestamp)
    normalized.setdefault("created_by", actor)
    normalized.setdefault("updated_by", actor)
    normalized["metadata"] = json_field(normalized.get("metadata"), {})
    return normalized


def json_field(value: Any, default: Any) -> str:
    if value is None:
        return dumps(default)
    if isinstance(value, str):
        try:
            json.loads(value)
            return value
        except json.JSONDecodeError:
            return dumps(default)
    return dumps(value)


def upsert_lesson(root: Path, project: str, lesson: dict[str, Any], actor: str = "system") -> UpsertResult:
    payload = normalize_lesson(project, lesson, actor=actor)
    with context_connection(root, project) as conn:
        existing = conn.execute(
            """
            select * from lessons
            where project = ? and exact_key = ? and lifecycle_status in ('active', 'pending', 'reopened')
            order by updated_at desc
            limit 1
            """,
            (payload["project"], payload["exact_key"]),
        ).fetchone()
        if existing:
            merge_duplicate(conn, dict(existing), payload, actor)
            return UpsertResult("duplicate_merged", str(existing["id"]), [])

        conflicts = find_conflicts(conn, payload)
        insert_lesson(conn, payload)
        add_lesson_source(conn, payload["id"], payload["source"], payload["source_type"])
        if conflicts:
            for conflict in conflicts:
                add_relation(conn, payload["id"], conflict["id"], "conflict", "结论冲突，等待人工裁决")
                conn.execute(
                    "update lessons set review_status = 'needs_review', revision = revision + 1, updated_at = ?, updated_by = ? where id = ?",
                    (now_iso(), actor, conflict["id"]),
                )
            audit(conn, "lesson_conflict_created", "lesson", payload["id"], payload["project"], actor, {"conflicts": [item["id"] for item in conflicts]})
            return UpsertResult("conflict_created", payload["id"], [item["id"] for item in conflicts])

        similar = conn.execute(
            """
            select id from lessons
            where project = ? and semantic_key = ? and id != ? and lifecycle_status in ('active', 'pending')
            limit 5
            """,
            (payload["project"], payload["semantic_key"], payload["id"]),
        ).fetchall()
        for row in similar:
            add_relation(conn, payload["id"], row["id"], "similar", "语义相似，等待合并确认")
        audit(conn, "lesson_created", "lesson", payload["id"], payload["project"], actor, {"similar": [row["id"] for row in similar]})
        return UpsertResult("created" if not similar else "similar_created", payload["id"], [row["id"] for row in similar])


def insert_lesson(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    values = [payload.get(column) for column in LESSON_COLUMNS]
    conn.execute(
        f"insert into lessons({','.join(LESSON_COLUMNS)}) values ({','.join(['?'] * len(LESSON_COLUMNS))})",
        values,
    )
    refresh_fts(conn, payload["id"], payload["title"], payload["content"])


def merge_duplicate(conn: sqlite3.Connection, existing: dict[str, Any], payload: dict[str, Any], actor: str) -> None:
    timestamp = now_iso()
    conn.execute(
        """
        update lessons
        set occurrence_count = occurrence_count + 1,
            last_seen = ?,
            last_seen_source = ?,
            revision = revision + 1,
            updated_at = ?,
            updated_by = ?
        where id = ?
        """,
        (timestamp, payload["source"], timestamp, actor, existing["id"]),
    )
    add_lesson_source(conn, existing["id"], payload["source"], payload["source_type"])
    audit(conn, "lesson_duplicate_merged", "lesson", existing["id"], existing["project"], actor, {"source": payload["source"]})


def find_conflicts(conn: sqlite3.Connection, payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        select * from lessons
        where project = ?
          and scope = ?
          and method = ?
          and path = ?
          and lesson_type = ?
          and environment = ?
          and lifecycle_status = 'active'
        """,
        (
            payload["project"],
            payload["scope"],
            payload["method"],
            payload["path"],
            payload["lesson_type"],
            payload["environment"],
        ),
    ).fetchall()
    return [dict(row) for row in rows if is_contradictory(payload["content"], row["content"])]


def refresh_fts(conn: sqlite3.Connection, lesson_id: str, title: str, content: str) -> None:
    try:
        conn.execute("delete from lessons_fts where id = ?", (lesson_id,))
        conn.execute("insert into lessons_fts(id, title, content) values (?, ?, ?)", (lesson_id, title, content))
    except sqlite3.OperationalError:
        return


def add_lesson_source(conn: sqlite3.Connection, lesson_id: str, source: str, source_type: str) -> None:
    conn.execute(
        "insert into lesson_sources(lesson_id, source, source_type, extracted_at) values (?, ?, ?, ?)",
        (lesson_id, source, source_type, now_iso()),
    )


def add_relation(conn: sqlite3.Connection, lesson_id: str, related_lesson_id: str, relation_type: str, notes: str = "") -> None:
    conn.execute(
        """
        insert into lesson_relations(lesson_id, related_lesson_id, relation_type, notes, created_at)
        values (?, ?, ?, ?, ?)
        """,
        (lesson_id, related_lesson_id, relation_type, notes, now_iso()),
    )


def audit(conn: sqlite3.Connection, action: str, entity_type: str, entity_id: str, project: str, actor: str, detail: dict[str, Any] | None = None) -> None:
    conn.execute(
        """
        insert into audit_logs(action, entity_type, entity_id, project, actor, detail, created_at)
        values (?, ?, ?, ?, ?, ?, ?)
        """,
        (action, entity_type, entity_id, project, actor, dumps(detail or {}), now_iso()),
    )


def list_lessons(
    root: Path,
    project: str,
    *,
    lifecycle_status: str = "",
    lesson_type: str = "",
    scope: str = "",
    query_text: str = "",
    limit: int = 50,
) -> list[dict[str, Any]]:
    query = "select * from lessons where project = ?"
    params: list[Any] = [safe_project_name(project)]
    if lifecycle_status:
        query += " and lifecycle_status = ?"
        params.append(lifecycle_status)
    if lesson_type:
        query += " and lesson_type = ?"
        params.append(lesson_type)
    if scope:
        query += " and scope = ?"
        params.append(scope)
    if query_text:
        query += " and (title like ? or content like ? or path like ?)"
        like = f"%{query_text}%"
        params.extend([like, like, like])
    query += " order by updated_at desc limit ?"
    params.append(limit)
    with context_connection(root, project) as conn:
        return [row_to_lesson(row) for row in conn.execute(query, params).fetchall()]


def update_lesson_review(
    root: Path,
    project: str,
    lesson_id: str,
    *,
    lifecycle_status: str,
    review_status: str,
    actor: str = "user",
) -> bool:
    timestamp = now_iso()
    with context_connection(root, project) as conn:
        row = conn.execute("select id from lessons where project = ? and id = ?", (safe_project_name(project), lesson_id)).fetchone()
        if not row:
            return False
        conn.execute(
            """
            update lessons
            set lifecycle_status = ?,
                review_status = ?,
                confirmed_at = case when ? = 'confirmed' then coalesce(confirmed_at, ?) else confirmed_at end,
                revision = revision + 1,
                updated_at = ?,
                updated_by = ?
            where id = ?
            """,
            (lifecycle_status, review_status, review_status, timestamp, timestamp, actor, lesson_id),
        )
        audit(
            conn,
            f"lesson_{review_status}",
            "lesson",
            lesson_id,
            safe_project_name(project),
            actor,
            {"lifecycle_status": lifecycle_status, "review_status": review_status},
        )
        return True


def resolve_known_issue(root: Path, project: str, lesson_id: str, actor: str = "user") -> bool:
    timestamp = now_iso()
    safe_project = safe_project_name(project)
    with context_connection(root, safe_project) as conn:
        row = conn.execute(
            "select id from lessons where project = ? and id = ? and lesson_type = 'known_issue'",
            (safe_project, lesson_id),
        ).fetchone()
        if not row:
            return False
        conn.execute(
            """
            update lessons
            set lifecycle_status = 'resolved',
                review_status = 'confirmed',
                confirmed_at = coalesce(confirmed_at, ?),
                revision = revision + 1,
                updated_at = ?,
                updated_by = ?
            where id = ?
            """,
            (timestamp, timestamp, actor, lesson_id),
        )
        audit(conn, "known_issue_resolved", "lesson", lesson_id, safe_project, actor, {})
        return True


def edit_lesson(
    root: Path,
    project: str,
    lesson_id: str,
    *,
    title: str,
    content: str,
    confidence: str,
    tags: list[str],
    actor: str = "user",
) -> bool:
    timestamp = now_iso()
    confidence = confidence if confidence in {"high", "medium", "low"} else "medium"
    with context_connection(root, project) as conn:
        row = conn.execute("select id from lessons where project = ? and id = ?", (safe_project_name(project), lesson_id)).fetchone()
        if not row:
            return False
        conn.execute(
            """
            update lessons
            set title = ?,
                content = ?,
                confidence = ?,
                tags = ?,
                revision = revision + 1,
                updated_at = ?,
                updated_by = ?
            where id = ?
            """,
            (title, content, confidence, dumps(tags), timestamp, actor, lesson_id),
        )
        refresh_fts(conn, lesson_id, title, content)
        audit(conn, "lesson_edited", "lesson", lesson_id, safe_project_name(project), actor, {"title": title})
        return True


def recent_audit_logs(root: Path, project: str, limit: int = 50) -> list[dict[str, Any]]:
    with context_connection(root, project) as conn:
        rows = conn.execute(
            "select * from audit_logs where project = ? order by id desc limit ?",
            (safe_project_name(project), limit),
        ).fetchall()
        items = []
        for row in rows:
            data = dict(row)
            data["detail"] = loads_json(data.get("detail"), {})
            items.append(data)
        return items


def row_to_lesson(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for key, default in [
        ("quality_reasons", []),
        ("tags", []),
        ("related_lesson_ids", []),
        ("merge_candidate_ids", []),
        ("metadata", {}),
    ]:
        data[key] = loads_json(data.get(key), default)
    return data


def validate_context(root: Path, project: str) -> dict[str, Any]:
    with context_connection(root, project) as conn:
        total = conn.execute("select count(*) from lessons").fetchone()[0]
        missing = conn.execute("select count(*) from lessons where title = '' or content = '' or exact_key = ''").fetchone()[0]
        conflicts = conn.execute("select count(*) from lesson_relations where relation_type = 'conflict'").fetchone()[0]
        return {"project": safe_project_name(project), "total_lessons": total, "invalid_lessons": missing, "conflicts": conflicts, "ok": missing == 0}


def context_stats(root: Path, project: str) -> dict[str, Any]:
    with context_connection(root, project) as conn:
        def grouped(column: str) -> dict[str, int]:
            rows = conn.execute(f"select {column} as value, count(*) as count from lessons group by {column}").fetchall()
            return {str(row["value"]): int(row["count"]) for row in rows}

        total = conn.execute("select count(*) from lessons").fetchone()[0]
        packs = conn.execute("select count(*) from context_packs").fetchone()[0]
        jobs = conn.execute("select status, count(*) as count from extraction_jobs group by status").fetchall()
        conflicts = conn.execute("select count(*) from lesson_relations where relation_type = 'conflict'").fetchone()[0]
        return {
            "project": safe_project_name(project),
            "total_lessons": int(total),
            "by_lifecycle": grouped("lifecycle_status"),
            "by_review": grouped("review_status"),
            "by_verification": grouped("verification_status"),
            "conflicts": int(conflicts),
            "context_packs": int(packs),
            "extraction_jobs": {row["status"]: int(row["count"]) for row in jobs},
        }


def rebuild_project_memory(root: Path, project: str) -> Path:
    safe_project = safe_project_name(project)
    with context_connection(root, safe_project) as conn:
        rows = conn.execute(
            """
            select id, scope, method, path, lesson_type, title, content, confidence, tags, updated_at
            from lessons
            where project = ?
              and scope = 'project'
              and lifecycle_status in ('active', 'reopened')
              and review_status = 'confirmed'
            order by quality_score desc, occurrence_count desc, updated_at desc
            """,
            (safe_project,),
        ).fetchall()
    memory = {
        "project": safe_project,
        "updated_at": now_iso(),
        "rules": [
            {
                **dict(row),
                "tags": loads_json(row["tags"], []),
            }
            for row in rows
        ],
    }
    path = project_context_dir(root, safe_project) / "project_memory.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(memory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def export_lessons(root: Path, project: str, output: Path | None = None) -> Path:
    safe_project = safe_project_name(project)
    if output is None:
        project_context_exports_dir(root, safe_project).mkdir(parents=True, exist_ok=True)
        output = project_context_exports_dir(root, safe_project) / f"lessons-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
    with context_connection(root, safe_project) as conn, output.open("w", encoding="utf-8") as file:
        rows = conn.execute("select * from lessons where project = ? order by created_at", (safe_project,)).fetchall()
        for row in rows:
            file.write(dumps(row_to_lesson(row)) + "\n")
    return output


def import_lessons(root: Path, project: str, source: Path, actor: str = "system") -> dict[str, int]:
    created = 0
    merged = 0
    conflicts = 0
    with source.open("r", encoding="utf-8") as file:
        for line in file:
            if not line.strip():
                continue
            lesson = json.loads(line)
            result = upsert_lesson(root, project, lesson, actor=actor)
            if result.action == "duplicate_merged":
                merged += 1
            elif result.action == "conflict_created":
                conflicts += 1
                created += 1
            else:
                created += 1
    return {"created": created, "merged": merged, "conflicts": conflicts}


def save_context_pack(root: Path, project: str, pack: dict[str, Any], actor: str = "system") -> str:
    payload = dict(pack)
    payload.setdefault("id", "pack-" + uuid.uuid4().hex[:12])
    payload.setdefault("project", safe_project_name(project))
    payload.setdefault("created_at", now_iso())
    payload.setdefault("included_lesson_ids", [])
    payload.setdefault("excluded_lesson_ids", [])
    project_context_packs_dir(root, project).mkdir(parents=True, exist_ok=True)
    with context_connection(root, project) as conn:
        conn.execute(
            """
            insert or replace into context_packs(
                id, task_id, task_type, project, model, model_context_window, context_budget,
                used_tokens, reserve_output_tokens, included_lesson_ids, excluded_lesson_ids,
                final_prompt_hash, created_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["id"],
                payload.get("task_id", ""),
                payload.get("task_type", ""),
                payload["project"],
                payload.get("model", ""),
                payload.get("model_context_window"),
                int(payload.get("context_budget", 0)),
                int(payload.get("used_tokens", 0)),
                payload.get("reserve_output_tokens"),
                dumps(payload.get("included_lesson_ids", [])),
                dumps(payload.get("excluded_lesson_ids", [])),
                payload.get("final_prompt_hash", ""),
                payload["created_at"],
            ),
        )
        audit(conn, "context_injected", "context_pack", payload["id"], payload["project"], actor, {"task_type": payload.get("task_type", "")})
    pack_file = project_context_packs_dir(root, project) / f"{payload['id']}.json"
    pack_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(payload["id"])


def list_context_packs(root: Path, project: str, limit: int = 20) -> list[dict[str, Any]]:
    with context_connection(root, project) as conn:
        rows = conn.execute(
            "select * from context_packs where project = ? order by created_at desc limit ?",
            (safe_project_name(project), limit),
        ).fetchall()
        return [pack_from_row(row) for row in rows]


def latest_context_pack(root: Path, project: str, task_type: str) -> dict[str, Any] | None:
    with context_connection(root, project) as conn:
        row = conn.execute(
            """
            select * from context_packs
            where project = ? and task_type = ?
            order by created_at desc
            limit 1
            """,
            (safe_project_name(project), task_type),
        ).fetchone()
        return pack_from_row(row) if row else None


def get_context_pack(root: Path, project: str, pack_id: str) -> dict[str, Any] | None:
    with context_connection(root, project) as conn:
        row = conn.execute("select * from context_packs where project = ? and id = ?", (safe_project_name(project), pack_id)).fetchone()
        return pack_from_row(row) if row else None


def lessons_by_ids(root: Path, project: str, lesson_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not lesson_ids:
        return {}
    placeholders = ",".join("?" for _ in lesson_ids)
    params: list[Any] = [safe_project_name(project), *lesson_ids]
    with context_connection(root, project) as conn:
        rows = conn.execute(
            f"select * from lessons where project = ? and id in ({placeholders})",
            params,
        ).fetchall()
        return {row["id"]: row_to_lesson(row) for row in rows}


def pack_from_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["included_lesson_ids"] = loads_json(data.get("included_lesson_ids"), [])
    data["excluded_lesson_ids"] = loads_json(data.get("excluded_lesson_ids"), [])
    return data


def trend_overview(root: Path, project: str, limit: int = 10) -> dict[str, Any]:
    safe_project = safe_project_name(project)
    with context_connection(root, safe_project) as conn:
        rows = conn.execute(
            """
            select run_id, created_at, total_cases, passed_cases, failed_cases, pass_rate,
                   failure_distribution, endpoint_metrics, latency_percentiles,
                   delta_from_previous, known_issue_status, run_scope, case_ids
            from execution_trends
            where project = ?
            order by id desc
            limit ?
            """,
            (safe_project, limit),
        ).fetchall()
        runs = []
        for row in rows:
            d = dict(row)
            for col, default_val in [
                ("failure_distribution", {}),
                ("endpoint_metrics", []),
                ("latency_percentiles", {}),
                ("delta_from_previous", {}),
                ("known_issue_status", {}),
                ("case_ids", []),
            ]:
                raw = d.get(col)
                d[col] = loads_json(raw, default_val) if isinstance(raw, str) else (raw if raw is not None else default_val)
            runs.append(d)
        return {"project": safe_project, "runs": runs}


def assess_trend_comparability(latest: dict, previous: dict) -> dict:
    """Shared comparability assessment used by both Web trend view and AI context builder.

    Returns a dict with keys: comparable (bool), confidence (str), note (str),
    jaccard (float|None), scope_mismatch (bool).
    """
    latest_total = int(latest.get("total_cases", 0))
    prev_total = int(previous.get("total_cases", 0))
    latest_scope = str(latest.get("run_scope", "") or "")
    prev_scope = str(previous.get("run_scope", "") or "")
    scope_mismatch = bool(prev_scope and latest_scope and prev_scope != latest_scope)

    # Primary: scope mismatch → not comparable
    if scope_mismatch:
        return {
            "comparable": False, "confidence": "low",
            "note": f"本次 {latest_scope} vs 上次 {prev_scope}，执行范围不同",
            "jaccard": None, "scope_mismatch": True,
        }

    # Secondary: case ID overlap (Jaccard)
    latest_ids = set(latest.get("case_ids", []))
    prev_ids = set(previous.get("case_ids", []))
    jaccard = None
    if latest_ids and prev_ids:
        union = latest_ids | prev_ids
        jaccard = len(latest_ids & prev_ids) / len(union) if union else 0.0
        if jaccard >= 0.7:
            return {"comparable": True, "confidence": "high", "note": f"用例重合度 {jaccard:.0%}，可以比较", "jaccard": jaccard, "scope_mismatch": False}
        elif jaccard >= 0.4:
            return {"comparable": True, "confidence": "medium", "note": f"用例重合度 {jaccard:.0%}，仅做参考", "jaccard": jaccard, "scope_mismatch": False}
        else:
            return {"comparable": False, "confidence": "low", "note": f"用例重合度仅 {jaccard:.0%}，不可直接比较", "jaccard": jaccard, "scope_mismatch": False}

    # Tertiary: case count ratio (fallback when no case_ids)
    ratio = latest_total / prev_total if prev_total > 0 else 1.0
    if ratio < 0.5 or ratio > 2.0:
        return {"comparable": False, "confidence": "low", "note": f"本次 {latest_total} 条 vs 上次 {prev_total} 条，样本量差异过大", "jaccard": None, "scope_mismatch": False}
    if ratio < 0.7 or ratio > 1.4:
        return {"comparable": True, "confidence": "medium", "note": f"本次 {latest_total} 条 vs 上次 {prev_total} 条，样本量有变化，通过率仅做参考", "jaccard": None, "scope_mismatch": False}
    return {"comparable": True, "confidence": "high", "note": f"两次执行规模接近（{latest_total} vs {prev_total}），可以比较", "jaccard": None, "scope_mismatch": False}


def save_execution_trend(root: Path, project: str, run_id: int, results: list[CaseResult], *, run_scope: str = "", case_ids: list[str] | None = None) -> None:
    total = len(results)
    passed = sum(1 for item in results if item.status == "passed")
    failed = sum(1 for item in results if item.status == "failed")
    _case_ids = case_ids or [item.case_id for item in results]
    pass_rate = passed / total if total else 0.0
    endpoint_metrics = []
    by_endpoint: dict[tuple[str, str], list[CaseResult]] = {}
    for result in results:
        by_endpoint.setdefault((result.method, result.path), []).append(result)
    for (method, path), items in sorted(by_endpoint.items()):
        endpoint_metrics.append(
            {
                "method": method,
                "path": path,
                "passed": sum(1 for item in items if item.status == "passed"),
                "failed": sum(1 for item in items if item.status == "failed"),
                "avg_ms": int(sum(item.elapsed_ms for item in items) / len(items)) if items else 0,
            }
        )
    latencies = sorted(item.elapsed_ms for item in results)
    percentiles = {
        "p50": percentile(latencies, 0.50),
        "p90": percentile(latencies, 0.90),
        "p95": percentile(latencies, 0.95),
        "p99": percentile(latencies, 0.99),
    }
    failure_distribution = {"failed": failed} if failed else {}
    safe_project = safe_project_name(project)
    with context_connection(root, safe_project) as conn:
        previous = conn.execute(
            "select pass_rate, failed_cases from execution_trends where project = ? order by id desc limit 1",
            (safe_project,),
        ).fetchone()
        delta = {}
        if previous:
            delta = {
                "pass_rate_delta": pass_rate - float(previous["pass_rate"]),
                "failed_delta": failed - int(previous["failed_cases"]),
            }
        conn.execute(
            """
            insert into execution_trends(
                run_id, project, created_at, total_cases, passed_cases, failed_cases,
                pass_rate, failure_distribution, endpoint_metrics, latency_percentiles,
                known_issue_status, delta_from_previous, run_scope, case_ids
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                safe_project,
                now_iso(),
                total,
                passed,
                failed,
                pass_rate,
                dumps(failure_distribution),
                dumps(endpoint_metrics),
                dumps(percentiles),
                dumps({}),
                dumps(delta),
                run_scope,
                dumps(_case_ids),
            ),
        )
        audit(conn, "trend_snapshot_created", "execution_trend", str(run_id), safe_project, "system", {"total": total, "passed": passed, "failed": failed})
    update_failure_patterns(root, project, results)


def mark_suspect_lessons_after_run(root: Path, project: str, results: list[CaseResult]) -> dict[str, int]:
    failed = [item for item in results if item.status == "failed"]
    if not failed:
        return {"updated": 0, "needs_review": 0}
    pack = latest_context_pack(root, project, "case_generation")
    if not pack or not pack.get("included_lesson_ids"):
        return {"updated": 0, "needs_review": 0}
    safe_project = safe_project_name(project)
    timestamp = now_iso()
    counts = {"updated": 0, "needs_review": 0}
    with context_connection(root, safe_project) as conn:
        for lesson_id in pack["included_lesson_ids"]:
            row = conn.execute("select * from lessons where project = ? and id = ?", (safe_project, lesson_id)).fetchone()
            if not row:
                continue
            lesson = row_to_lesson(row)
            metadata = dict(lesson.get("metadata") or {})
            suspect_count = int(metadata.get("suspect_count") or 0) + 1
            metadata["suspect_count"] = suspect_count
            metadata["last_suspect_source"] = f"context_pack:{pack['id']}"
            metadata["last_failed_cases"] = [item.case_id for item in failed[:20]]
            quality = max(0.0, float(lesson.get("quality_score") or 0.0) - 0.1)
            review_status = "needs_review" if suspect_count >= 3 else str(lesson.get("review_status") or "unreviewed")
            conn.execute(
                """
                update lessons
                set quality_score = ?,
                    review_status = ?,
                    metadata = ?,
                    revision = revision + 1,
                    updated_at = ?,
                    updated_by = 'system'
                where id = ?
                """,
                (quality, review_status, dumps(metadata), timestamp, lesson_id),
            )
            counts["updated"] += 1
            if review_status == "needs_review":
                counts["needs_review"] += 1
        if counts["updated"]:
            audit(conn, "context_suspect_marked", "context_pack", pack["id"], safe_project, "system", counts)
    return counts


def update_known_issue_lifecycle(root: Path, project: str, results: list[CaseResult]) -> dict[str, int]:
    safe_project = safe_project_name(project)
    by_endpoint: dict[tuple[str, str], list[CaseResult]] = {}
    for result in results:
        if result.method == "CHAIN":
            continue
        by_endpoint.setdefault((result.method, result.path), []).append(result)
    updated = {"reopened": 0, "resolved_candidate": 0, "quiet": 0}
    with context_connection(root, safe_project) as conn:
        rows = conn.execute(
            """
            select * from lessons
            where project = ?
              and lesson_type = 'known_issue'
              and lifecycle_status in ('active', 'reopened', 'resolved_candidate', 'resolved')
            """,
            (safe_project,),
        ).fetchall()
        for row in rows:
            lesson = row_to_lesson(row)
            key = (str(lesson.get("method") or ""), str(lesson.get("path") or ""))
            related = by_endpoint.get(key)
            if not related:
                continue
            metadata = dict(lesson.get("metadata") or {})
            has_failure = any(item.status == "failed" for item in related)
            timestamp = now_iso()
            if has_failure:
                new_status = "reopened" if lesson.get("lifecycle_status") in {"resolved", "resolved_candidate"} else "active"
                metadata["quiet_runs"] = 0
                conn.execute(
                    """
                    update lessons
                    set lifecycle_status = ?,
                        review_status = case when lifecycle_status in ('resolved', 'resolved_candidate') then 'needs_review' else review_status end,
                        occurrence_count = occurrence_count + 1,
                        last_seen = ?,
                        last_seen_source = ?,
                        metadata = ?,
                        revision = revision + 1,
                        updated_at = ?,
                        updated_by = 'system'
                    where id = ?
                    """,
                    (new_status, timestamp, f"run:{key[0]} {key[1]}", dumps(metadata), timestamp, lesson["id"]),
                )
                updated["reopened"] += 1
                audit(conn, "known_issue_recurred", "lesson", lesson["id"], safe_project, "system", {"endpoint": f"{key[0]} {key[1]}"})
            elif all(item.status == "passed" for item in related):
                quiet_runs = int(metadata.get("quiet_runs") or 0) + 1
                metadata["quiet_runs"] = quiet_runs
                if quiet_runs >= 5 and lesson.get("lifecycle_status") in {"active", "reopened"}:
                    conn.execute(
                        """
                        update lessons
                        set lifecycle_status = 'resolved_candidate',
                            review_status = 'needs_review',
                            metadata = ?,
                            revision = revision + 1,
                            updated_at = ?,
                            updated_by = 'system'
                        where id = ?
                        """,
                        (dumps(metadata), timestamp, lesson["id"]),
                    )
                    updated["resolved_candidate"] += 1
                    audit(conn, "known_issue_resolved_candidate", "lesson", lesson["id"], safe_project, "system", {"quiet_runs": quiet_runs})
                else:
                    conn.execute(
                        "update lessons set metadata = ?, revision = revision + 1, updated_at = ?, updated_by = 'system' where id = ?",
                        (dumps(metadata), timestamp, lesson["id"]),
                    )
                    updated["quiet"] += 1
    return updated


def update_failure_patterns(root: Path, project: str, results: list[CaseResult]) -> dict[str, int]:
    safe_project = safe_project_name(project)
    failed = [item for item in results if item.status == "failed"]
    updated = {"created": 0, "updated": 0}
    timestamp = now_iso()
    with context_connection(root, safe_project) as conn:
        touched: set[int] = set()
        for result in failed:
            category = classify_failure(result)
            row = conn.execute(
                """
                select * from failure_patterns
                where project = ? and method = ? and path = ? and failure_category = ?
                """,
                (safe_project, result.method, result.path, category),
            ).fetchone()
            metadata = {
                "last_case_id": result.case_id,
                "last_error": result.error or "; ".join(result.assertion_errors[:3]),
                "status_code": result.status_code,
            }
            if row:
                conn.execute(
                    """
                    update failure_patterns
                    set occurrence_count = occurrence_count + 1,
                        last_seen = ?,
                        trend_direction = ?,
                        metadata = ?,
                        updated_at = ?
                    where id = ?
                    """,
                    (timestamp, compute_failure_trend(conn, safe_project, result.method, result.path, category), dumps(metadata), timestamp, row["id"]),
                )
                touched.add(int(row["id"]))
                updated["updated"] += 1
            else:
                cursor = conn.execute(
                    """
                    insert into failure_patterns(
                        project, method, path, failure_category, occurrence_count, first_seen,
                        last_seen, trend_direction, related_lesson_ids, metadata, updated_at
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (safe_project, result.method, result.path, category, 1, timestamp, timestamp, "new", dumps([]), dumps(metadata), timestamp),
                )
                touched.add(int(cursor.lastrowid))
                updated["created"] += 1
        stale_rows = conn.execute(
            "select id, project, method, path, failure_category from failure_patterns where project = ?",
            (safe_project,),
        ).fetchall()
        for row in stale_rows:
            if int(row["id"]) in touched:
                continue
            trend = compute_failure_trend(conn, safe_project, row["method"], row["path"], row["failure_category"], current_seen=False)
            conn.execute("update failure_patterns set trend_direction = ?, updated_at = ? where id = ?", (trend, timestamp, row["id"]))
        if failed:
            audit(conn, "failure_patterns_updated", "failure_pattern", safe_project, safe_project, "system", updated)
    return updated


def classify_failure(result: CaseResult) -> str:
    text = " ".join([result.error or "", " ".join(result.assertion_errors), result.case_risk or "", result.design_method or ""]).lower()
    if "timeout" in text or "connect" in text or "connection" in text:
        return "env_issue"
    if "data" in text or "不存在" in text or "not found" in text or result.case_risk in {"data_dependent", "data_dependent_without_fixture"}:
        return "test_data_issue"
    if result.assertion_errors:
        return "case_assertion_error"
    if result.status_code and result.status_code >= 500:
        return "interface_bug"
    return "new_issue"


def compute_failure_trend(
    conn: sqlite3.Connection,
    project: str,
    method: str,
    path: str,
    category: str,
    current_seen: bool = True,
) -> str:
    rows = conn.execute(
        """
        select endpoint_metrics
        from execution_trends
        where project = ?
        order by id desc
        limit 10
        """,
        (project,),
    ).fetchall()
    occurrences = []
    for row in reversed(rows):
        metrics = loads_json(row["endpoint_metrics"], [])
        seen = any(item.get("method") == method and item.get("path") == path and int(item.get("failed", 0)) > 0 for item in metrics)
        occurrences.append(1 if seen else 0)
    if current_seen and (not occurrences or occurrences[-1] == 0):
        occurrences.append(1)
    if len(occurrences) < 3:
        return "new"
    mid = len(occurrences) // 2
    first = sum(occurrences[:mid])
    second = sum(occurrences[mid:])
    if second == 0 and first > 0:
        return "improving"
    if second > max(1, first) * 1.5:
        return "worsening"
    if abs(second - first) <= 1:
        return "stable"
    return "fluctuating"


def apply_known_issue_updates(root: Path, project: str, updates: list[dict[str, Any]], actor: str = "ai:analyzer") -> dict[str, int]:
    counts = {"updated": 0, "ignored": 0}
    safe_project = safe_project_name(project)
    timestamp = now_iso()
    allowed = {"active", "reopened", "resolved_candidate", "resolved", "ignored", "archived"}
    with context_connection(root, safe_project) as conn:
        for item in updates:
            lesson_id = str(item.get("id") or item.get("lesson_id") or "")
            status = str(item.get("lifecycle_status") or item.get("status") or "")
            if not lesson_id or status not in allowed:
                counts["ignored"] += 1
                continue
            review = "needs_review" if status in {"reopened", "resolved_candidate"} else ("confirmed" if status == "resolved" else "unreviewed")
            cursor = conn.execute(
                """
                update lessons
                set lifecycle_status = ?,
                    review_status = ?,
                    revision = revision + 1,
                    updated_at = ?,
                    updated_by = ?
                where project = ? and id = ? and lesson_type = 'known_issue'
                """,
                (status, review, timestamp, actor, safe_project, lesson_id),
            )
            if cursor.rowcount:
                counts["updated"] += 1
                audit(conn, "known_issue_status_updated", "lesson", lesson_id, safe_project, actor, {"status": status, "reason": item.get("reason", "")})
            else:
                counts["ignored"] += 1
    return counts


def mark_lessons_for_endpoint_changes(root: Path, project: str, changed_keys: list[str], actor: str = "system") -> int:
    safe_project = safe_project_name(project)
    parsed = [key.split(" ", 1) for key in changed_keys if " " in key]
    timestamp = now_iso()
    count = 0
    with context_connection(root, safe_project) as conn:
        for method, path in parsed:
            cursor = conn.execute(
                """
                update lessons
                set review_status = 'needs_review',
                    revision = revision + 1,
                    updated_at = ?,
                    updated_by = ?
                where project = ?
                  and method = ?
                  and path = ?
                  and lifecycle_status in ('active', 'reopened')
                """,
                (timestamp, actor, safe_project, method.upper(), path),
            )
            count += cursor.rowcount
        if count:
            audit(conn, "endpoint_lessons_need_review", "lesson", safe_project, safe_project, actor, {"changed": changed_keys, "count": count})
    return count


def record_case_generation_lessons(root: Path, project: str, cases: list[TestCase], actor: str = "system") -> dict[str, int]:
    counts = {"created": 0, "merged": 0}
    seen: set[tuple[str, str, str]] = set()
    for case in cases:
        if not case.expected_result or not case.assertions:
            continue
        key = (case.method, case.path, case.design_method)
        if key in seen:
            continue
        seen.add(key)
        result = upsert_lesson(
            root,
            project,
            {
                "scope": "endpoint" if case.method != "CHAIN" else "case",
                "method": case.method,
                "path": case.path,
                "lesson_type": "assertion_pattern",
                "lesson_subtype": case.design_method,
                "title": f"{case.title} 的断言经验",
                "content": case_generation_lesson_content(case),
                "source": case.id,
                "source_type": "case_generation",
                "confidence": "medium",
                "tags": ["case_generation", case.design_method],
                "lifecycle_status": "pending",
                "review_status": "unreviewed",
            },
            actor=actor,
        )
        if result.action == "duplicate_merged":
            counts["merged"] += 1
        else:
            counts["created"] += 1
    return counts


def case_generation_lesson_content(case: TestCase) -> str:
    assertions = "; ".join(describe_assertion(assertion) for assertion in case.assertions) or "无结构化断言"
    inputs = []
    if case.path_params:
        inputs.append(f"path_params={json.dumps(case.path_params, ensure_ascii=False, sort_keys=True)}")
    if case.query:
        inputs.append(f"query={json.dumps(case.query, ensure_ascii=False, sort_keys=True)}")
    if case.body is not None:
        inputs.append(f"body={json.dumps(case.body, ensure_ascii=False, sort_keys=True)}")
    input_text = "；".join(inputs) if inputs else "无显式输入数据"
    return (
        f"{case.method} {case.path} 的 {case.design_method} 用例预期：{case.expected_result}。"
        f"具体断言模式：{assertions}。"
        f"测试数据模式：{input_text}。"
        "后续生成同接口同场景用例时应复用这些具体断言和业务预期，避免生成不符合接口文档的状态码或字段断言。"
    )


def describe_assertion(assertion: Assertion) -> str:
    expected = json.dumps(assertion.expected, ensure_ascii=False, sort_keys=True)
    target = assertion.target or assertion.type
    return f"{assertion.type} {target} {assertion.operator} {expected}"


def percentile(values: list[int], ratio: float) -> int:
    if not values:
        return 0
    index = min(len(values) - 1, max(0, int(round((len(values) - 1) * ratio))))
    return int(values[index])


def create_extraction_job(
    root: Path,
    project: str,
    *,
    trigger: str,
    source: str,
    status: str,
    error: str = "",
    retry_count: int = 0,
    max_retries: int = 3,
    payload: dict[str, Any] | None = None,
) -> str:
    job_id = "ext-" + uuid.uuid4().hex[:12]
    timestamp = now_iso()
    next_retry_at = retry_next_at(retry_count) if status == "failed" and retry_count < max_retries else None
    with context_connection(root, project) as conn:
        conn.execute(
            """
            insert into extraction_jobs(
                id, trigger, source, project, status, error, max_retries,
                retry_count, retry_backoff, next_retry_at, created_at, updated_at, payload
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                trigger,
                source,
                safe_project_name(project),
                status,
                error,
                max_retries,
                retry_count,
                "exponential",
                next_retry_at,
                timestamp,
                timestamp,
                dumps(payload or {}),
            ),
        )
        audit(conn, "extraction_" + status, "extraction_job", job_id, safe_project_name(project), "system", {"source": source, "error": error})
    return job_id


def retry_next_at(retry_count: int) -> str:
    delay_minutes = min(60, 2 ** max(0, retry_count))
    return (datetime.now() + timedelta(minutes=delay_minutes)).isoformat(timespec="seconds")


def due_extraction_jobs(root: Path, project: str, limit: int = 10) -> list[dict[str, Any]]:
    safe_project = safe_project_name(project)
    timestamp = now_iso()
    with context_connection(root, safe_project) as conn:
        rows = conn.execute(
            """
            select *
            from extraction_jobs
            where project = ?
              and status = 'failed'
              and retry_count < max_retries
              and (next_retry_at is null or next_retry_at <= ?)
            order by created_at
            limit ?
            """,
            (safe_project, timestamp, limit),
        ).fetchall()
        jobs = []
        for row in rows:
            data = dict(row)
            data["payload"] = loads_json(data.get("payload"), {})
            jobs.append(data)
        return jobs


def mark_extraction_job_running(root: Path, project: str, job_id: str) -> bool:
    return update_extraction_job(root, project, job_id, status="running")


def mark_extraction_job_completed(root: Path, project: str, job_id: str) -> bool:
    return update_extraction_job(root, project, job_id, status="completed", error="", next_retry_at=None)


def mark_extraction_job_failed(root: Path, project: str, job_id: str, error: str) -> bool:
    safe_project = safe_project_name(project)
    timestamp = now_iso()
    with context_connection(root, safe_project) as conn:
        row = conn.execute(
            "select retry_count, max_retries from extraction_jobs where project = ? and id = ?",
            (safe_project, job_id),
        ).fetchone()
        if not row:
            return False
        retry_count = int(row["retry_count"]) + 1
        max_retries = int(row["max_retries"])
        status = "permanent_failed" if retry_count >= max_retries else "failed"
        next_retry_at = retry_next_at(retry_count) if status == "failed" else None
        conn.execute(
            """
            update extraction_jobs
            set status = ?,
                error = ?,
                retry_count = ?,
                next_retry_at = ?,
                updated_at = ?
            where project = ? and id = ?
            """,
            (status, error, retry_count, next_retry_at, timestamp, safe_project, job_id),
        )
        audit(conn, "extraction_" + status, "extraction_job", job_id, safe_project, "system", {"error": error, "retry_count": retry_count})
        return True


def update_extraction_job(
    root: Path,
    project: str,
    job_id: str,
    *,
    status: str,
    error: str | None = None,
    next_retry_at: str | None = None,
) -> bool:
    safe_project = safe_project_name(project)
    timestamp = now_iso()
    with context_connection(root, safe_project) as conn:
        cursor = conn.execute(
            """
            update extraction_jobs
            set status = ?,
                error = coalesce(?, error),
                next_retry_at = ?,
                updated_at = ?
            where project = ? and id = ?
            """,
            (status, error, next_retry_at, timestamp, safe_project, job_id),
        )
        if cursor.rowcount:
            audit(conn, "extraction_" + status, "extraction_job", job_id, safe_project, "system", {})
        return bool(cursor.rowcount)


def conflict_pairs(root: Path, project: str, limit: int = 50) -> list[dict[str, Any]]:
    safe_project = safe_project_name(project)
    with context_connection(root, safe_project) as conn:
        rows = conn.execute(
            """
            select r.id as relation_id,
                   r.notes,
                   r.created_at,
                   a.id as a_id, a.title as a_title, a.content as a_content,
                   a.lifecycle_status as a_lifecycle, a.review_status as a_review,
                   a.confidence as a_confidence, a.source as a_source,
                   b.id as b_id, b.title as b_title, b.content as b_content,
                   b.lifecycle_status as b_lifecycle, b.review_status as b_review,
                   b.confidence as b_confidence, b.source as b_source,
                   a.method as method, a.path as path, a.environment as environment
            from lesson_relations r
            join lessons a on a.id = r.lesson_id
            join lessons b on b.id = r.related_lesson_id
            where r.relation_type = 'conflict'
              and a.project = ?
            order by r.created_at desc
            limit ?
            """,
            (safe_project, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def similar_pairs(root: Path, project: str, limit: int = 50) -> list[dict[str, Any]]:
    safe_project = safe_project_name(project)
    with context_connection(root, safe_project) as conn:
        rows = conn.execute(
            """
            select r.id as relation_id,
                   r.notes,
                   r.created_at,
                   a.id as a_id, a.title as a_title, a.content as a_content,
                   a.lifecycle_status as a_lifecycle, a.review_status as a_review,
                   a.confidence as a_confidence, a.source as a_source,
                   b.id as b_id, b.title as b_title, b.content as b_content,
                   b.lifecycle_status as b_lifecycle, b.review_status as b_review,
                   b.confidence as b_confidence, b.source as b_source,
                   a.method as method, a.path as path, a.environment as environment
            from lesson_relations r
            join lessons a on a.id = r.lesson_id
            join lessons b on b.id = r.related_lesson_id
            where r.relation_type = 'similar'
              and a.project = ?
            order by r.created_at desc
            limit ?
            """,
            (safe_project, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def resolve_conflict(
    root: Path,
    project: str,
    *,
    relation_id: int,
    resolution: str,
    actor: str = "user",
) -> bool:
    safe_project = safe_project_name(project)
    timestamp = now_iso()
    with context_connection(root, safe_project) as conn:
        row = conn.execute(
            """
            select r.id, r.lesson_id, r.related_lesson_id
            from lesson_relations r
            join lessons l on l.id = r.lesson_id
            where r.id = ? and r.relation_type = 'conflict' and l.project = ?
            """,
            (relation_id, safe_project),
        ).fetchone()
        if not row:
            return False
        a_id = row["lesson_id"]
        b_id = row["related_lesson_id"]
        if resolution == "keep_a":
            confirm_lesson(conn, a_id, timestamp, actor)
            reject_lesson(conn, b_id, timestamp, actor)
        elif resolution == "keep_b":
            confirm_lesson(conn, b_id, timestamp, actor)
            reject_lesson(conn, a_id, timestamp, actor)
        elif resolution == "keep_both":
            confirm_lesson(conn, a_id, timestamp, actor, review_status="needs_review")
            confirm_lesson(conn, b_id, timestamp, actor, review_status="needs_review")
        elif resolution == "ignore_both":
            reject_lesson(conn, a_id, timestamp, actor)
            reject_lesson(conn, b_id, timestamp, actor)
        else:
            return False
        conn.execute("update lesson_relations set relation_type = 'conflict_resolved', notes = ? where id = ?", (resolution, relation_id))
        audit(conn, "conflict_resolved", "lesson_relation", str(relation_id), safe_project, actor, {"resolution": resolution, "a": a_id, "b": b_id})
        return True


def apply_conflict_review(
    root: Path,
    project: str,
    reviews: list[dict[str, Any]],
    *,
    actor: str = "ai:analyzer",
) -> dict[str, int]:
    safe_project = safe_project_name(project)
    summary = {"conflict": 0, "not_conflict": 0, "uncertain": 0, "missing": 0}
    timestamp = now_iso()
    with context_connection(root, safe_project) as conn:
        for review in reviews:
            try:
                relation_id = int(review.get("relation_id"))
            except (TypeError, ValueError):
                summary["missing"] += 1
                continue
            row = conn.execute(
                """
                select r.id
                from lesson_relations r
                join lessons l on l.id = r.lesson_id
                where r.id = ? and r.relation_type = 'conflict' and l.project = ?
                """,
                (relation_id, safe_project),
            ).fetchone()
            if not row:
                summary["missing"] += 1
                continue
            decision = str(review.get("decision") or "uncertain")
            if decision not in {"conflict", "not_conflict", "uncertain"}:
                decision = "uncertain"
            reason = str(review.get("reason") or "").strip()
            if decision == "not_conflict":
                relation_type = "conflict_dismissed"
                notes = f"AI 复核认为不是冲突：{reason}" if reason else "AI 复核认为不是冲突"
            elif decision == "conflict":
                relation_type = "conflict"
                notes = f"AI 复核确认疑似冲突：{reason}" if reason else "AI 复核确认疑似冲突"
            else:
                relation_type = "conflict"
                notes = f"AI 复核无法确定，保留人工裁决：{reason}" if reason else "AI 复核无法确定，保留人工裁决"
            conn.execute(
                "update lesson_relations set relation_type = ?, notes = ? where id = ?",
                (relation_type, notes, relation_id),
            )
            audit(
                conn,
                "conflict_ai_reviewed",
                "lesson_relation",
                str(relation_id),
                safe_project,
                actor,
                {"decision": decision, "reason": reason, "reviewed_at": timestamp},
            )
            summary[decision] += 1
    return summary


def resolve_similar(
    root: Path,
    project: str,
    *,
    relation_id: int,
    resolution: str,
    actor: str = "user",
) -> bool:
    safe_project = safe_project_name(project)
    timestamp = now_iso()
    with context_connection(root, safe_project) as conn:
        row = conn.execute(
            """
            select r.id, r.lesson_id, r.related_lesson_id
            from lesson_relations r
            join lessons l on l.id = r.lesson_id
            where r.id = ? and r.relation_type = 'similar' and l.project = ?
            """,
            (relation_id, safe_project),
        ).fetchone()
        if not row:
            return False
        a_id = row["lesson_id"]
        b_id = row["related_lesson_id"]
        if resolution == "merge_a":
            merge_lesson_into(conn, source_id=b_id, target_id=a_id, timestamp=timestamp, actor=actor)
        elif resolution == "merge_b":
            merge_lesson_into(conn, source_id=a_id, target_id=b_id, timestamp=timestamp, actor=actor)
        elif resolution == "keep_both":
            conn.execute("update lesson_relations set relation_type = 'similar_resolved', notes = ? where id = ?", (resolution, relation_id))
            audit(conn, "similar_kept_separate", "lesson_relation", str(relation_id), safe_project, actor, {"a": a_id, "b": b_id})
            return True
        else:
            return False
        conn.execute("update lesson_relations set relation_type = 'merged_into', notes = ? where id = ?", (resolution, relation_id))
        audit(conn, "similar_merged", "lesson_relation", str(relation_id), safe_project, actor, {"resolution": resolution, "a": a_id, "b": b_id})
        return True


def merge_lesson_into(conn: sqlite3.Connection, *, source_id: str, target_id: str, timestamp: str, actor: str) -> None:
    source = conn.execute("select * from lessons where id = ?", (source_id,)).fetchone()
    target = conn.execute("select * from lessons where id = ?", (target_id,)).fetchone()
    if not source or not target:
        return
    source_data = row_to_lesson(source)
    target_data = row_to_lesson(target)
    related_ids = set(target_data.get("related_lesson_ids") or [])
    related_ids.add(source_id)
    conn.execute(
        """
        update lessons
        set occurrence_count = occurrence_count + ?,
            related_lesson_ids = ?,
            quality_score = max(coalesce(quality_score, 0), ?),
            revision = revision + 1,
            updated_at = ?,
            updated_by = ?
        where id = ?
        """,
        (
            int(source_data.get("occurrence_count") or 1),
            dumps(sorted(related_ids)),
            float(source_data.get("quality_score") or 0),
            timestamp,
            actor,
            target_id,
        ),
    )
    source_related = set(source_data.get("related_lesson_ids") or [])
    source_related.add(target_id)
    conn.execute(
        """
        update lessons
        set lifecycle_status = 'merged',
            review_status = 'rejected',
            related_lesson_ids = ?,
            revision = revision + 1,
            updated_at = ?,
            updated_by = ?
        where id = ?
        """,
        (dumps(sorted(source_related)), timestamp, actor, source_id),
    )


def confirm_lesson(conn: sqlite3.Connection, lesson_id: str, timestamp: str, actor: str, review_status: str = "confirmed") -> None:
    conn.execute(
        """
        update lessons
        set lifecycle_status = 'active',
            review_status = ?,
            confirmed_at = coalesce(confirmed_at, ?),
            revision = revision + 1,
            updated_at = ?,
            updated_by = ?
        where id = ?
        """,
        (review_status, timestamp, timestamp, actor, lesson_id),
    )


def reject_lesson(conn: sqlite3.Connection, lesson_id: str, timestamp: str, actor: str) -> None:
    conn.execute(
        """
        update lessons
        set lifecycle_status = 'ignored',
            review_status = 'rejected',
            revision = revision + 1,
            updated_at = ?,
            updated_by = ?
        where id = ?
        """,
        (timestamp, actor, lesson_id),
    )
