from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Iterator

from ai_apitest.core.models import CaseResult
from ai_apitest.core.config import load_config
from ai_apitest.core.paths import project_result_db
from ai_apitest.report.markdown import normalize_snapshot


MAX_PERSISTED_SNAPSHOT_CHARS = 100_000
_SCHEMA_READY: dict[str, int] = {}
_SCHEMA_LOCK = threading.Lock()


@contextmanager
def result_connection(db: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def save_run(root: Path, results: list[CaseResult], project: str = "default", *, metadata: dict | None = None) -> int:
    db = project_result_db(root, project)
    db.parent.mkdir(parents=True, exist_ok=True)
    with result_connection(db) as conn:
        conn.execute("pragma journal_mode = wal")
        conn.execute("pragma busy_timeout = 5000")
        ensure_schema_once(conn, db)
        created_at = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute("insert into runs(created_at, metadata_json) values (?, ?)", (created_at, json.dumps(normalize_snapshot(metadata or {}), ensure_ascii=False)))
        run_id = int(cursor.lastrowid)
        snapshot_limit = max(10_000, int(load_config(root).get("retention", {}).get("snapshot_chars", MAX_PERSISTED_SNAPSHOT_CHARS)))
        for result in results:
            result.run_id = run_id
            safe = normalize_snapshot(asdict(result))
            conn.execute(
                """
                insert into case_results(
                    run_id, case_id, title, status, method, path, status_code,
                    elapsed_ms, error, assertion_errors, request_json, response_json,
                    origin_case_id, dataset_id, flaky, result_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    result.case_id,
                    safe["title"],
                    result.status,
                    result.method,
                    safe["path"],
                    result.status_code,
                    result.elapsed_ms,
                    safe["error"],
                    json.dumps(safe["assertion_errors"], ensure_ascii=False),
                    persisted_snapshot(safe["request"], snapshot_limit),
                    persisted_snapshot(safe["response"], snapshot_limit),
                    result.origin_case_id,
                    result.dataset_id,
                    int(result.flaky),
                    persisted_snapshot(safe, snapshot_limit),
                ),
            )
        retain = max(10, int(load_config(root).get("retention", {}).get("result_runs", 200)))
        prune_result_runs(conn, retain)
    return run_id


def persisted_snapshot(value, limit: int = MAX_PERSISTED_SNAPSHOT_CHARS) -> str:
    safe_value = normalize_snapshot(value)
    text = json.dumps(safe_value, ensure_ascii=False, separators=(",", ":"))
    if len(text) <= limit:
        return text
    return json.dumps(
        {
            "truncated": True,
            "original_chars": len(text),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "preview": text[:limit],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        create table if not exists runs(
            id integer primary key autoincrement,
            created_at text not null
        )
        """
    )
    conn.execute(
        """
        create table if not exists case_results(
            id integer primary key autoincrement,
            run_id integer not null,
            case_id text not null,
            title text not null,
            status text not null,
            method text not null,
            path text not null,
            status_code integer,
            elapsed_ms integer not null,
            error text,
            assertion_errors text not null,
            request_json text not null,
            response_json text not null,
            foreign key(run_id) references runs(id)
        )
        """
    )
    conn.execute("create index if not exists idx_case_results_run on case_results(run_id, id)")
    conn.execute("create index if not exists idx_case_results_case on case_results(case_id, id desc)")
    for table, name, definition in (
        ("runs", "metadata_json", "text not null default '{}'"),
        ("case_results", "origin_case_id", "text not null default ''"),
        ("case_results", "dataset_id", "text not null default ''"),
        ("case_results", "flaky", "integer not null default 0"),
        ("case_results", "result_json", "text not null default '{}'"),
    ):
        if name not in {row[1] for row in conn.execute(f"pragma table_info({table})")}:
            conn.execute(f"alter table {table} add column {name} {definition}")


def ensure_schema_once(conn: sqlite3.Connection, db: Path) -> None:
    key = str(db.resolve())
    inode = db.stat().st_ino
    if _SCHEMA_READY.get(key) == inode:
        return
    with _SCHEMA_LOCK:
        inode = db.stat().st_ino
        if _SCHEMA_READY.get(key) == inode:
            return
        ensure_schema(conn)
        _SCHEMA_READY[key] = inode


def prune_result_runs(conn: sqlite3.Connection, retain: int) -> None:
    rows = conn.execute("select id from runs order by id desc limit -1 offset ?", (retain,)).fetchall()
    ids = [int(row[0]) for row in rows]
    if not ids:
        return
    conn.executemany("delete from case_results where run_id = ?", [(item,) for item in ids])
    conn.executemany("delete from runs where id = ?", [(item,) for item in ids])


def latest_results(root: Path, project: str = "default") -> tuple[int | None, list[dict]]:
    db = project_result_db(root, project)
    if not db.exists():
        return None, []
    with result_connection(db) as conn:
        conn.row_factory = sqlite3.Row
        ensure_schema_once(conn, db)
        run = conn.execute("select id from runs order by id desc limit 1").fetchone()
        if not run:
            return None, []
        rows = conn.execute(
            "select * from case_results where run_id = ? order by id",
            (run["id"],),
        ).fetchall()
        return int(run["id"]), [dict(row) for row in rows]


def latest_case_results(root: Path, project: str = "default") -> dict[str, dict]:
    db = project_result_db(root, project)
    if not db.exists():
        return {}
    with result_connection(db) as conn:
        conn.row_factory = sqlite3.Row
        ensure_schema_once(conn, db)
        rows = conn.execute(
            """
            select cr.case_id, cr.status, cr.origin_case_id, cr.run_id, r.created_at
            from case_results cr
            join runs r on r.id = cr.run_id
            join (
                select cr2.case_id, max(cr2.id) as max_id
                from case_results cr2
                group by cr2.case_id
            ) latest on latest.max_id = cr.id
            """
        ).fetchall()
        result = {row["case_id"]: dict(row) for row in rows}
        groups: dict[str, list[dict]] = {}
        for row in rows:
            if row["origin_case_id"] and row["origin_case_id"] != row["case_id"]:
                groups.setdefault(row["origin_case_id"], []).append(dict(row))
        for origin, items in groups.items():
            newest = max(item["run_id"] for item in items)
            current = [item for item in items if item["run_id"] == newest]
            statuses = {item["status"] for item in current}
            status = next((value for value in ("failed", "cancelled", "skipped", "passed") if value in statuses), "skipped")
            result[origin] = {"case_id": origin, "status": status, "created_at": current[0]["created_at"], "dataset_rows": len(current)}
        return result


def latest_run_metadata(root: Path, project: str = "default") -> dict:
    db = project_result_db(root, project)
    if not db.exists():
        return {}
    with result_connection(db) as conn:
        ensure_schema_once(conn, db)
        row = conn.execute("select id, metadata_json from runs order by id desc limit 1").fetchone()
        return {**json.loads(row[1]), "run_id": row[0]} if row else {}


def result_to_dict(result: CaseResult) -> dict:
    return asdict(result)


def current_run_results(root: Path, project: str, cases: list) -> dict[str, dict]:
    """Give execution credit only to unchanged inputs from the latest run."""
    from ai_apitest.core.paths import project_docs_file
    from ai_apitest.core.serde import read_json
    from ai_apitest.execution.provenance import fingerprint
    from ai_apitest.execution.datasets import expand_cases
    metadata = latest_run_metadata(root, project)
    if not metadata or metadata.get("contract_hash") != fingerprint(read_json(project_docs_file(root, project), [])):
        return {}
    hashes = {item["case_id"]: item["hash"] for item in metadata.get("case_hashes", [])}
    expanded = expand_cases(cases, root, load_config(root))
    valid = {case.id: case for case in expanded if hashes.get(case.id) == fingerprint(asdict(case))}
    _, rows = latest_results(root, project)
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        if row["case_id"] in valid:
            origin = valid[row["case_id"]].origin_case_id or row["case_id"]
            grouped.setdefault(origin, []).append(row)
    return {origin: {"case_id": origin, "run_id": metadata["run_id"], "status": next(status for status in ("failed", "cancelled", "skipped", "passed") if any(row["status"] == status for row in rows))} for origin, rows in grouped.items()}
