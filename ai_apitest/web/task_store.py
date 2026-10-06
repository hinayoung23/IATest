from __future__ import annotations

import os
import json
import sqlite3
import time
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ai_apitest.core.paths import project_task_db, safe_project_name


STALE_AFTER_SECONDS = 30
RECENT_TASK_SECONDS = 120
MAX_RETAINED_TASKS = 200
_SCHEMA_READY: dict[str, int] = {}
_SCHEMA_LOCK = threading.Lock()


@contextmanager
def task_connection(root: Path, project: str) -> Iterator[sqlite3.Connection]:
    db = project_task_db(root, project)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db, timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("pragma journal_mode = wal")
        conn.execute("pragma busy_timeout = 5000")
        ensure_task_schema_once(conn, db)
        conn.commit()
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_task_schema_once(conn: sqlite3.Connection, db: Path) -> None:
    key = str(db.resolve())
    inode = db.stat().st_ino
    if _SCHEMA_READY.get(key) == inode:
        return
    with _SCHEMA_LOCK:
        inode = db.stat().st_ino
        if _SCHEMA_READY.get(key) == inode:
            return
        ensure_task_schema(conn)
        _SCHEMA_READY[key] = inode


def ensure_task_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        create table if not exists web_tasks(
            id text primary key,
            project text not null,
            action text not null,
            label text not null,
            status text not null,
            stage text not null,
            detail text not null default '',
            message text not null default '',
            stream_excerpt text not null default '',
            kind text not null default '',
            payload text not null default '{}',
            resumable integer not null default 1,
            attempt_count integer not null default 0,
            max_attempts integer not null default 2,
            owner_pid integer,
            started_at real not null,
            heartbeat_at real not null,
            updated_at real not null
        )
        """
    )
    for column, definition in (
        ("kind", "text not null default ''"),
        ("payload", "text not null default '{}'"),
        ("resumable", "integer not null default 1"),
        ("attempt_count", "integer not null default 0"),
        ("max_attempts", "integer not null default 2"),
        ("cancel_requested", "integer not null default 0"),
    ):
        ensure_task_column(conn, column, definition)
    index = conn.execute(
        "select sql from sqlite_master where type = 'index' and name = 'idx_web_tasks_running'"
    ).fetchone()
    if index and "queued" not in str(index[0] or "").lower():
        conn.execute("drop index idx_web_tasks_running")
    conn.execute(
        "create unique index if not exists idx_web_tasks_running on web_tasks(project, action) where status in ('queued', 'running')"
    )
    conn.execute(
        "create index if not exists idx_web_tasks_recent on web_tasks(project, updated_at desc)"
    )


def ensure_task_column(conn: sqlite3.Connection, column: str, definition: str) -> None:
    columns = {str(row["name"]) for row in conn.execute("pragma table_info(web_tasks)").fetchall()}
    if column not in columns:
        conn.execute(f"alter table web_tasks add column {column} {definition}")


def recover_stale_tasks(
    root: Path,
    project: str,
    *,
    stale_after_seconds: int = STALE_AFTER_SECONDS,
) -> int:
    safe_project = safe_project_name(project)
    cutoff = time.time() - stale_after_seconds
    with task_connection(root, safe_project) as conn:
        cursor = conn.execute(
            """
            update web_tasks
            set status = case when cancel_requested = 1 then 'cancelled' when resumable = 1 and attempt_count < max_attempts then 'queued' else 'failed' end,
                stage = case when cancel_requested = 1 then 'cancelled' when resumable = 1 and attempt_count < max_attempts then 'queued' else 'interrupted' end,
                detail = case
                    when cancel_requested = 1 then '任务已取消'
                    when resumable = 1 and attempt_count < max_attempts then '服务进程中断，任务等待自动恢复'
                    else '服务进程已中断，任务未能安全恢复'
                end,
                message = case
                    when resumable = 1 and attempt_count < max_attempts then '任务因服务重启或进程退出而中断，正在等待自动恢复'
                    when message = '' then '任务因服务重启或进程退出而中断，请重新发起'
                    else message
                end,
                owner_pid = null,
                updated_at = ?
            where project = ? and status = 'running' and heartbeat_at < ?
            """,
            (time.time(), safe_project, cutoff),
        )
        return int(cursor.rowcount)


def list_tasks(
    root: Path,
    project: str,
    *,
    recent_seconds: int = RECENT_TASK_SECONDS,
) -> list[dict[str, Any]]:
    safe_project = safe_project_name(project)
    recover_stale_tasks(root, safe_project)
    cutoff = time.time() - recent_seconds
    with task_connection(root, safe_project) as conn:
        rows = conn.execute(
            """
            select * from web_tasks
            where project = ? and (status in ('queued', 'running') or updated_at >= ?)
            order by case when status in ('queued', 'running') then 0 else 1 end, updated_at desc
            """,
            (safe_project, cutoff),
        ).fetchall()
    return [public_task(dict(row)) for row in rows]


def active_task(root: Path, project: str, action: str) -> dict[str, Any] | None:
    safe_project = safe_project_name(project)
    recover_stale_tasks(root, safe_project)
    with task_connection(root, safe_project) as conn:
        row = conn.execute(
            "select * from web_tasks where project = ? and action = ? and status in ('queued', 'running') limit 1",
            (safe_project, action),
        ).fetchone()
    return public_task(dict(row)) if row else None


def create_task(
    root: Path,
    project: str,
    action: str,
    label: str,
    *,
    kind: str = "",
    payload: dict[str, Any] | None = None,
    resumable: bool = True,
    max_attempts: int = 2,
) -> dict[str, Any] | None:
    safe_project = safe_project_name(project)
    now = time.time()
    task = {
        "id": uuid.uuid4().hex,
        "project": safe_project,
        "action": action,
        "label": label,
        "status": "queued",
        "stage": "queued",
        "detail": "任务已启动",
        "message": "",
        "stream_excerpt": "",
        "kind": kind or action,
        "payload": json.dumps(payload or {}, ensure_ascii=False),
        "resumable": int(resumable),
        "attempt_count": 0,
        "max_attempts": max(1, int(max_attempts)),
        "owner_pid": None,
        "started_at": now,
        "heartbeat_at": now,
        "updated_at": now,
    }
    with task_connection(root, safe_project) as conn:
        conn.execute("begin immediate")
        cutoff = now - STALE_AFTER_SECONDS
        conn.execute(
            """
            update web_tasks
            set status = case when cancel_requested = 1 then 'cancelled' when resumable = 1 and attempt_count < max_attempts then 'queued' else 'failed' end,
                stage = case when cancel_requested = 1 then 'cancelled' when resumable = 1 and attempt_count < max_attempts then 'queued' else 'interrupted' end,
                detail = case when resumable = 1 and attempt_count < max_attempts then '服务进程中断，任务等待自动恢复' else '服务进程已中断，任务未能安全恢复' end,
                message = case when resumable = 1 and attempt_count < max_attempts then '任务因服务重启或进程退出而中断，正在等待自动恢复' else '任务因服务重启或进程退出而中断，请重新发起' end,
                owner_pid = null,
                updated_at = ?
            where project = ? and status = 'running' and heartbeat_at < ?
            """,
            (now, safe_project, cutoff),
        )
        existing = conn.execute(
            "select id from web_tasks where project = ? and action = ? and status in ('queued', 'running')",
            (safe_project, action),
        ).fetchone()
        if existing:
            return None
        conn.execute(
            """
            insert into web_tasks(
                id, project, action, label, status, stage, detail, message,
                stream_excerpt, kind, payload, resumable, attempt_count, max_attempts,
                owner_pid, started_at, heartbeat_at, updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(task[key] for key in (
                "id", "project", "action", "label", "status", "stage", "detail", "message",
                "stream_excerpt", "kind", "payload", "resumable", "attempt_count", "max_attempts",
                "owner_pid", "started_at", "heartbeat_at", "updated_at",
            )),
        )
        prune_finished_tasks(conn, safe_project)
    return public_task(task)


def update_task(root: Path, project: str, task_id: str, **updates: Any) -> bool:
    safe_project = safe_project_name(project)
    allowed = {"status", "stage", "detail", "message", "heartbeat_at"}
    values = {key: value for key, value in updates.items() if key in allowed}
    stream_delta = str(updates.get("stream_delta") or "")
    now = time.time()
    with task_connection(root, safe_project) as conn:
        conn.execute("begin immediate")
        row = conn.execute(
            "select stream_excerpt from web_tasks where project = ? and id = ?",
            (safe_project, task_id),
        ).fetchone()
        if not row:
            return False
        if stream_delta:
            values["stream_excerpt"] = (str(row["stream_excerpt"] or "") + stream_delta)[-1200:]
        values["updated_at"] = now
        if "heartbeat_at" not in values and values.get("status") == "running":
            values["heartbeat_at"] = now
        assignments = ", ".join(f"{key} = ?" for key in values)
        params = [*values.values(), safe_project, task_id]
        cursor = conn.execute(
            f"update web_tasks set {assignments} where project = ? and id = ?",
            params,
        )
        return bool(cursor.rowcount)


def heartbeat_task(root: Path, project: str, task_id: str) -> bool:
    return update_task(root, project, task_id, heartbeat_at=time.time())


def request_cancel(root: Path, project: str, task_id: str) -> bool:
    with task_connection(root, project) as conn:
        cursor = conn.execute(
            """update web_tasks set cancel_requested = 1, resumable = 0,
                status = case when status = 'queued' then 'cancelled' else status end,
                detail = '已请求取消，正在停止执行并清理资源', updated_at = ?
                where project = ? and id = ? and status in ('queued', 'running')""",
            (time.time(), safe_project_name(project), task_id),
        )
        return bool(cursor.rowcount)


def cancellation_requested(root: Path, project: str, task_id: str) -> bool:
    with task_connection(root, project) as conn:
        row = conn.execute("select cancel_requested from web_tasks where project = ? and id = ?", (safe_project_name(project), task_id)).fetchone()
    return bool(row and row[0])


def queued_tasks(root: Path, project: str, *, limit: int = 20) -> list[dict[str, Any]]:
    safe_project = safe_project_name(project)
    recover_stale_tasks(root, safe_project)
    with task_connection(root, safe_project) as conn:
        rows = conn.execute(
            "select * from web_tasks where project = ? and status = 'queued' order by started_at limit ?",
            (safe_project, max(1, int(limit))),
        ).fetchall()
    return [internal_task(dict(row)) for row in rows]


def claim_task(root: Path, project: str, task_id: str) -> dict[str, Any] | None:
    safe_project = safe_project_name(project)
    now = time.time()
    with task_connection(root, safe_project) as conn:
        conn.execute("begin immediate")
        cursor = conn.execute(
            """
            update web_tasks
            set status = 'running', stage = 'running', detail = '任务已认领，正在执行',
                owner_pid = ?, attempt_count = attempt_count + 1,
                heartbeat_at = ?, updated_at = ?
            where project = ? and id = ? and status = 'queued' and attempt_count < max_attempts
            """,
            (os.getpid(), now, now, safe_project, task_id),
        )
        if not cursor.rowcount:
            return None
        row = conn.execute(
            "select * from web_tasks where project = ? and id = ?",
            (safe_project, task_id),
        ).fetchone()
    return internal_task(dict(row)) if row else None


def prune_finished_tasks(conn: sqlite3.Connection, project: str) -> None:
    rows = conn.execute(
        """
        select id from web_tasks
        where project = ? and status not in ('queued', 'running')
        order by updated_at desc
        limit -1 offset ?
        """,
        (project, MAX_RETAINED_TASKS),
    ).fetchall()
    if rows:
        conn.executemany("delete from web_tasks where id = ?", [(row["id"],) for row in rows])


def public_task(task: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in task.items()
        if key not in {"project", "owner_pid", "payload", "resumable", "max_attempts"}
    }


def internal_task(task: dict[str, Any]) -> dict[str, Any]:
    payload = task.get("payload")
    if isinstance(payload, str):
        try:
            task["payload"] = json.loads(payload)
        except json.JSONDecodeError:
            task["payload"] = {}
    task["resumable"] = bool(task.get("resumable", 0))
    return task
