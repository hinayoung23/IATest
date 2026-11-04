from __future__ import annotations

import json
import re
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from iatest.core.paths import project_reports_root, project_runtime_dir
from iatest.core.redaction import redact
from iatest.data.project_storage import ProjectAssetIndex
from iatest.engines.contracts import EngineResult, ExecutionPlan


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value) or value in {".", ".."}:
        raise ValueError("Invalid test asset identifier")
    return value


class EngineStore:
    def __init__(self, root: Path, project: str):
        self.root, self.project = root, project
        self.directory = project_runtime_dir(root, project) / "testing"
        self.index = ProjectAssetIndex(root, project, "testing")
        self.path = self.index.path

    @contextmanager
    def connection(self):
        with self.index.connection() as conn:
            yield conn

    def save_plan(self, value: dict):
        from iatest.engines.validation import validate_plan

        plan = ExecutionPlan.from_dict(value)
        validate_plan(plan)
        with self.connection() as conn:
            conn.execute(
                "insert or replace into plans values(?,?,?)",
                (
                    identifier(plan.id),
                    plan.kind,
                    json.dumps(asdict(plan), ensure_ascii=False, allow_nan=False),
                ),
            )

    def plans(self) -> list[dict]:
        with self.connection() as conn:
            return [
                json.loads(r[0])
                for r in conn.execute("select content from plans order by kind,id")
            ]

    def get_plan(self, name: str) -> dict:
        with self.connection() as conn:
            row = conn.execute(
                "select content from plans where id=?", (identifier(name),)
            ).fetchone()
        if not row:
            raise ValueError("Test plan not found")
        return json.loads(row[0])

    def delete_plan(self, name: str):
        with self.connection() as conn:
            conn.execute("delete from plans where id=?", (identifier(name),))

    def save_result(self, result: EngineResult) -> str:
        result.run_id = result.run_id or "test-" + uuid.uuid4().hex[:20]
        with self.connection() as conn:
            conn.execute(
                "insert or replace into runs values(?,?,?)",
                (
                    identifier(result.run_id),
                    datetime.now(UTC).isoformat(),
                    json.dumps(
                        redact(asdict(result)), ensure_ascii=False, allow_nan=False
                    ),
                ),
            )
        return result.run_id

    def runs(self, limit: int = 50) -> list[dict]:
        with self.connection() as conn:
            return [
                {**json.loads(r[1]), "created_at": r[0]}
                for r in conn.execute(
                    "select created_at,content from runs order by created_at desc limit ?",
                    (max(1, min(200, limit)),),
                )
            ]

    def get_result(self, run_id: str) -> dict:
        with self.connection() as conn:
            row = conn.execute(
                "select created_at,content from runs where id=?", (identifier(run_id),)
            ).fetchone()
        if row is None:
            raise ValueError("Test run not found")
        return {**json.loads(row[1]), "created_at": row[0]}

    def save_artifact(
        self, kind: str, value: dict, *, artifact_id: str = "", replace=False
    ) -> str:
        artifact_id = identifier(artifact_id or kind + "-" + uuid.uuid4().hex[:20])
        identifier(kind)
        value = {**value, "id": artifact_id}
        from iatest.engines.data import redact_asset

        encoded = json.dumps(redact_asset(value), ensure_ascii=False, allow_nan=False)
        if len(encoded.encode()) > 4 * 1024 * 1024:
            raise ValueError("Artifact exceeds 4 MiB")
        with self.connection() as conn:
            sql = "insert or replace" if replace else "insert"
            conn.execute(
                sql + " into artifacts values(?,?,?,?)",
                (kind, artifact_id, datetime.now(UTC).isoformat(), encoded),
            )
        return artifact_id

    def get_artifact(self, kind: str, artifact_id: str) -> dict:
        with self.connection() as conn:
            row = conn.execute(
                "select content from artifacts where kind=? and id=?",
                (identifier(kind), identifier(artifact_id)),
            ).fetchone()
        if row is None:
            raise ValueError("Artifact not found")
        return json.loads(row[0])

    def artifacts(self, kind: str, limit=100) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "select content from artifacts where kind=? order by created_at desc limit ?",
                (identifier(kind), max(1, min(200, limit))),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    @property
    def evidence_roots(self):
        return (
            project_reports_root(self.root, self.project),
            self.directory / "evidence",
        )

    def evidence_path(self, run: str, name: str, *, kind=None) -> Path:
        from iatest.data.project_storage import evidence_path

        return evidence_path(self.root, self.project, "testing", run, name, kind=kind)
