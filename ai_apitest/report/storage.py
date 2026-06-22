from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from ai_apitest.core.models import CaseResult
from ai_apitest.core.paths import project_result_db


def save_run(root: Path, results: list[CaseResult], project: str = "default") -> int:
    db = project_result_db(root, project)
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as conn:
        ensure_schema(conn)
        created_at = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute("insert into runs(created_at) values (?)", (created_at,))
        run_id = int(cursor.lastrowid)
        for result in results:
            conn.execute(
                """
                insert into case_results(
                    run_id, case_id, title, status, method, path, status_code,
                    elapsed_ms, error, assertion_errors, request_json, response_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    result.case_id,
                    result.title,
                    result.status,
                    result.method,
                    result.path,
                    result.status_code,
                    result.elapsed_ms,
                    result.error,
                    json.dumps(result.assertion_errors, ensure_ascii=False),
                    json.dumps(result.request, ensure_ascii=False),
                    json.dumps(result.response, ensure_ascii=False),
                ),
            )
    return run_id


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


def latest_results(root: Path, project: str = "default") -> tuple[int | None, list[dict]]:
    db = project_result_db(root, project)
    if not db.exists():
        return None, []
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        ensure_schema(conn)
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
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        ensure_schema(conn)
        rows = conn.execute(
            """
            select cr.case_id, cr.status, r.created_at
            from case_results cr
            join runs r on r.id = cr.run_id
            join (
                select cr2.case_id, max(cr2.id) as max_id
                from case_results cr2
                group by cr2.case_id
            ) latest on latest.max_id = cr.id
            """
        ).fetchall()
        return {row["case_id"]: dict(row) for row in rows}


def result_to_dict(result: CaseResult) -> dict:
    return asdict(result)
