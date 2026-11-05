"""Explicit retention for typed runs and their binary evidence."""
from __future__ import annotations

import json
import shutil

from iatest.core.locking import project_operation_lock
from iatest.core.paths import project_reports_root, project_dir
from iatest.data.project_storage import TYPE_DIRS
from iatest.engines.store import EngineStore, identifier


def _run_references(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"run_id", "baseline_run_id"} and isinstance(child, str):
                yield child
            else:
                yield from _run_references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _run_references(child)


def prune_results(root, project, *, keep: int = 200, dry_run: bool = True) -> dict:
    if type(keep) is not int or keep < 1:
        raise ValueError("Retention must keep at least one result")
    store = EngineStore(root, project)
    with project_operation_lock(root, project, operation="result-retention"):
        with store.connection() as conn:
            rows = conn.execute("select id,content from runs order by created_at desc,id desc").fetchall()
            protected = set()
            for row in conn.execute("select content from artifacts where kind not in ('run_context', 'analysis_status')"):
                protected.update(_run_references(json.loads(row[0])))
            for row in rows:
                protected.update(reference for reference in _run_references(json.loads(row["content"])) if reference != row["id"])
            selected = [row for row in rows[keep:] if row["id"] not in protected]
            directories = []
            for row in selected:
                run_id = identifier(row["id"])
                kind = json.loads(row["content"])["kind"]
                if kind not in TYPE_DIRS:
                    raise ValueError("Unknown result kind in retention")
                directories.extend([project_reports_root(root, project) / TYPE_DIRS[kind] / run_id, store.directory / "evidence" / run_id])
            for directory in directories:
                if directory.is_symlink() or not directory.resolve().is_relative_to(project_dir(root, project).resolve()):
                    raise ValueError("Evidence retention path escaped workspace")
            if not dry_run:
                for row in selected:
                    conn.execute("delete from runs where id=?", (row["id"],))
                    conn.execute("delete from artifacts where kind='run_context' and id=?", ("ctx-" + row["id"],))
        if not dry_run:
            for directory in directories:
                if directory.is_dir():
                    shutil.rmtree(directory)
        return {"dry_run": dry_run, "keep": keep, "run_ids": [row["id"] for row in selected], "protected_runs": len(protected), "evidence_directories": sum(path.is_dir() for path in directories) if dry_run else len(directories)}
