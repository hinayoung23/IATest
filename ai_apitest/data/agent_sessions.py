"""Durable checkpoints and call ledger outside execution workers."""
from __future__ import annotations

from dataclasses import asdict
import json
import os
import re
import time

from ai_apitest.core.redaction import redact
from ai_apitest.tools.registry import ToolExecutionResult


class SessionRecoveryError(RuntimeError):
    pass


class AgentSession:
    def __init__(self, repository, session_id: str, fingerprint: str, *, resume: bool = False):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", session_id):
            raise ValueError("会话标识非法")
        self.repository, self.id = repository, session_id
        self.snapshot = {}
        with repository.connection() as conn:
            conn.execute("create table if not exists durable_agent_sessions(id text primary key, fingerprint text not null, status text not null, owner integer, snapshot text not null default '{}', resumable integer not null default 1, updated_at real not null)")
            conn.execute("create table if not exists durable_agent_calls(session_id text not null, call_key text not null, status text not null, idempotent integer not null, result text not null default '{}', state text not null default '{}', primary key(session_id, call_key))")
            conn.execute("begin immediate")
            row = conn.execute("select * from durable_agent_sessions where id = ?", (self.id,)).fetchone()
            if row:
                if not resume or row["fingerprint"] != fingerprint:
                    raise SessionRecoveryError("会话已存在或任务、角色、工具、模型及输入版本发生变化")
                if not row["resumable"]:
                    raise SessionRecoveryError("会话包含不可安全恢复的敏感或过大状态")
                if row["status"] == "running" and self._alive(row["owner"]):
                    raise SessionRecoveryError("该会话仍由存活进程持有")
                unknown = conn.execute("select 1 from durable_agent_calls where session_id = ? and status = 'running' and idempotent = 0", (self.id,)).fetchone()
                if unknown:
                    raise SessionRecoveryError("非幂等工具的副作用未知，不能自动恢复")
                self.snapshot = json.loads(row["snapshot"])
                conn.execute("update durable_agent_sessions set status = 'running', owner = ?, updated_at = ? where id = ?", (os.getpid(), time.time(), self.id))
            else:
                if resume:
                    raise SessionRecoveryError("恢复会话不存在")
                conn.execute("insert into durable_agent_sessions(id, fingerprint, status, owner, updated_at) values (?, ?, 'running', ?, ?)", (self.id, fingerprint, os.getpid(), time.time()))

    @staticmethod
    def _alive(pid):
        if not pid:
            return False
        if type(pid) is not int or pid <= 0:
            return True
        if pid == os.getpid():
            return True
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x00100000, False, pid)
            if not handle:
                return ctypes.get_last_error() != 87
            try:
                return kernel.WaitForSingleObject(handle, 0) != 0
            finally:
                kernel.CloseHandle(handle)
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def save(self, snapshot: dict, *, status: str = "running"):
        safe = redact(snapshot)
        encoded = json.dumps(safe, ensure_ascii=False)
        # JSON arrays and Python tuples have identical durable semantics. A
        # read-range tuple must not be mistaken for a redacted secret.
        resumable = encoded == json.dumps(snapshot, ensure_ascii=False) and len(encoded.encode()) <= 2_000_000
        if not resumable and len(encoded.encode()) > 2_000_000:
            encoded = "{}"
        with self.repository.connection() as conn:
            conn.execute("update durable_agent_sessions set snapshot = ?, resumable = resumable and ?, status = ?, updated_at = ? where id = ?", (encoded, int(resumable), status, time.time(), self.id))
        self.snapshot = safe

    def begin_call(self, key: str, spec):
        with self.repository.connection() as conn:
            row = conn.execute("select * from durable_agent_calls where session_id = ? and call_key = ?", (self.id, key)).fetchone()
            if row and row["status"] == "completed":
                return ToolExecutionResult(**json.loads(row["result"])), json.loads(row["state"])
            if row and not row["idempotent"]:
                raise SessionRecoveryError("工具副作用未知，不能重复执行")
            conn.execute("insert or replace into durable_agent_calls(session_id, call_key, status, idempotent) values (?, ?, 'running', ?)", (self.id, key, int(spec.side_effect == "read" or spec.idempotent)))
        return None

    def finish_call(self, key: str, result: ToolExecutionResult, state: dict):
        raw = {"result": asdict(result), "state": state}
        safe = redact(raw)
        with self.repository.connection() as conn:
            if json.dumps(safe, ensure_ascii=False) != json.dumps(raw, ensure_ascii=False) or len(json.dumps(safe, ensure_ascii=False).encode()) > 2_000_000:
                conn.execute("update durable_agent_sessions set resumable = 0 where id = ?", (self.id,))
                return
            conn.execute("update durable_agent_calls set status = 'completed', result = ?, state = ? where session_id = ? and call_key = ?", (json.dumps(safe["result"], ensure_ascii=False), json.dumps(safe["state"], ensure_ascii=False), self.id, key))

    def fail(self):
        with self.repository.connection() as conn:
            conn.execute("update durable_agent_sessions set status = 'failed', updated_at = ? where id = ?", (time.time(), self.id))

    def update_budget(self, budget: dict):
        with self.repository.connection() as conn:
            row = conn.execute("select snapshot from durable_agent_sessions where id = ?", (self.id,)).fetchone()
            saved = json.loads(row[0])
            saved["budget"] = budget
            conn.execute("update durable_agent_sessions set snapshot = ? where id = ?", (json.dumps(saved, ensure_ascii=False), self.id))
