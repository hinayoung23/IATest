"""Readable project assets with rebuildable SQLite views and legacy migration.

JSON/Markdown files are the authoritative assets. SQLite content columns remain
materialized views for existing queries and atomic import/version checks.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from iatest.core.files import atomic_write_bytes, atomic_write_text
from iatest.core.locking import project_operation_lock
from iatest.core.paths import (
    project_assets_dir,
    project_dir,
    project_reports_root,
    project_runtime_dir,
    resolved_path,
)

TYPE_DIRS = {
    kind: kind
    for kind in (
        "api",
        "web",
        "desktop",
        "android",
        "ios",
        "performance",
        "security",
        "data",
        "protocol",
        "accessibility",
        "workflow",
        "remote",
        "shared",
    )
}
TYPE_DIRS["client_performance"] = "performance/client"
ARTIFACT_TYPES = {
    "security_library": "security",
    "protobuf": "protocol",
    "accessibility_review": "accessibility",
    "campaign": "workflow",
    "ios_measurement": "client_performance",
    "ios_calibration": "client_performance",
}
ARTIFACT_GROUPS = {
    "ui_case": "cases",
    "security_library": "rules",
    "protobuf": "schemas",
    "accessibility_review": "reviews",
    "campaign": "campaigns",
    "ios_measurement": "measurements",
    "ios_calibration": "calibrations",
    "app": "applications",
}
INTERNAL_ARTIFACTS = {
    "node_attempt",
    "node_bundle",
    "ios_host",
    "campaign_run",
    "analysis",
    "run_context",
    "visual_location",
    "deployment",
    "import_batch",
}
_LOCAL = threading.local()
TABLE_KEYS = {
    "plans": ("id",),
    "runs": ("id",),
    "artifacts": ("kind", "id"),
    "case_assets": ("case_id", "version"),
    "assets": ("kind", "id"),
}


def _identifier(value):
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value)
        or value in {".", ".."}
    ):
        raise ValueError("Invalid project asset identifier")
    return value


def _json(value):
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n"
    )


def _read(path):
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("Project asset exceeds 64 MiB: " + str(path))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f"Invalid JSON project asset {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("Project asset must be a JSON object: " + str(path))  # noqa: TRY004
    return value


def _validate_content(table, kind, value):
    if table == "artifacts":
        _identifier(value.get("id"))
    elif table == "case_assets":
        _identifier(value.get("case_id"))
        if type(value.get("version")) is not int or value["version"] < 0:
            raise ValueError("Invalid case asset version")
    elif table == "runs":
        _identifier(value.get("run_id"))
    if table == "plans":
        from iatest.engines.contracts import ExecutionPlan
        from iatest.engines.validation import validate_plan

        validate_plan(ExecutionPlan.from_dict(value))
    elif table == "assets" and kind in {"profile", "plan"}:
        from iatest.engines.contracts import ExecutionPlan
        from iatest.mobile.contracts import validate_mobile_plan
        from iatest.mobile.store import validate_profile

        if kind == "profile":
            validate_profile(value)
        else:
            validate_mobile_plan(ExecutionPlan.from_dict(value))


def _inside(path, base):
    if not resolved_path(path).is_relative_to(resolved_path(base)):
        raise ValueError("Asset path escaped its project: " + str(path))
    return path


def type_directory(root, project, kind):
    if kind not in TYPE_DIRS:
        raise ValueError("Unknown test asset type: " + str(kind))
    return _inside(
        project_assets_dir(root, project) / TYPE_DIRS[kind], project_dir(root, project)
    )


def _layout_marker(root, project):
    return _inside(
        project_runtime_dir(root, project) / "storage-layout.json",
        project_dir(root, project),
    )


def ensure_project_layout(root, project):
    """Copy old API folders once; retain originals for rollback and old references."""
    base = project_dir(root, project)
    _inside(base, Path(root) / "projects")
    reports = project_reports_root(root, project)
    project_assets_dir(root, project)
    marker = _layout_marker(root, project)
    if marker.is_file() and _read(marker).get("layout_version") == 2:
        return
    with project_operation_lock(
        root, project, operation="storage-layout", lock_file="storage.lock"
    ):
        if marker.is_file() and _read(marker).get("layout_version") == 2:
            return
        copies = []
        for old, new in (
            (base / "api_docs", base / "assets/api/documents"),
            (base / "test_cases", base / "assets/api/cases"),
        ):
            if old.is_dir():
                for source in old.rglob("*"):
                    _inside(source, base)
                    if source.is_file():
                        copies.append((source, new / source.relative_to(old)))
        if reports.is_dir():
            # Existing API reports are files at reports/. New typed report
            # directories must never be recursively copied into reports/api/.
            copies.extend(
                (source, reports / "api" / source.name)
                for source in reports.iterdir()
                if source.is_file()
            )
        for source, target in copies:
            _inside(source, base)
            _inside(target, base)
            if target.exists() and target.read_bytes() != source.read_bytes():
                raise ValueError(
                    "Storage migration conflict; both files retained: " + str(target)
                )
        for kind in TYPE_DIRS:
            directory = type_directory(root, project, kind)
            for group in ("plans", "cases"):
                _inside(directory / group, base).mkdir(parents=True, exist_ok=True)
        for artifact, kind in ARTIFACT_TYPES.items():
            _inside(
                type_directory(root, project, kind) / ARTIFACT_GROUPS[artifact], base
            ).mkdir(parents=True, exist_ok=True)
        _inside(base / "assets/api/documents", base).mkdir(parents=True, exist_ok=True)
        for platform in ("android", "ios"):
            _inside(base / "assets" / platform / "profiles", base).mkdir(
                parents=True, exist_ok=True
            )
        _inside(reports / "api", base).mkdir(parents=True, exist_ok=True)
        for source, target in copies:
            if not target.exists():
                atomic_write_bytes(target, source.read_bytes())
        atomic_write_text(
            marker,
            _json(
                {
                    "layout_version": 2,
                    "legacy_files_retained": True,
                    "copied_files": len(copies),
                    "migrated_databases": [],
                }
            ),
        )


def _asset_type(kind, value):
    if kind in ARTIFACT_TYPES:
        return ARTIFACT_TYPES[kind]
    if kind in {"ui_case", "app"}:
        for candidate in (value.get("kind"), value.get("platform")):
            if candidate in TYPE_DIRS:
                return candidate
    return "shared"


def evidence_path(root, project, namespace, run, name, *, kind=None):
    _identifier(run)
    if not re.fullmatch(r"(?:[a-f0-9]{32}|desktop-\d{1,6})\.(?:png|bin)", name):
        raise ValueError("Invalid evidence name")
    reports = project_reports_root(root, project)

    def candidate(test_kind):
        base = reports / TYPE_DIRS[test_kind] / run
        if namespace == "mobile":
            base /= "native"
        return _inside(base / "evidence" / name, project_dir(root, project))

    if kind is not None:
        if kind not in TYPE_DIRS:
            raise ValueError("Unknown evidence type")
        return candidate(kind)
    paths = [candidate(test_kind) for test_kind in TYPE_DIRS]
    for path in paths:
        if path.is_file():
            return path
    legacy = (
        project_runtime_dir(root, project)
        / ("testing" if namespace == "testing" else "mobile")
        / "evidence"
        / run
        / name
    )
    if legacy.exists():
        return _inside(legacy, project_dir(root, project))
    for path in paths:
        if (path.parent.parent / "result.json").is_file():
            return path
    return candidate("shared")


class ProjectAssetIndex:
    """A serialized SQL view of files, with recoverable multi-file commits."""

    def __init__(self, root, project, namespace):
        if namespace not in {"testing", "mobile"}:
            raise ValueError("Unknown asset namespace")
        self.root, self.project, self.namespace = Path(root), project, namespace
        self.base = project_dir(self.root, project)
        self.runtime = project_runtime_dir(self.root, project)
        self.path = self.runtime / "indexes" / (namespace + ".sqlite3")
        self.legacy = self.runtime / (
            "testing/testing.sqlite3"
            if namespace == "testing"
            else "mobile/mobile.sqlite3"
        )
        self.tables = (
            ("plans", "runs", "artifacts", "case_assets")
            if namespace == "testing"
            else ("assets", "runs")
        )
        # Preserve ordering metadata without changing the signed artifact payload.
        self.metadata = self.runtime / "state/testing/asset-metadata.json"

    def _schema(self, conn):
        if self.namespace == "testing":
            conn.executescript("""
                create table if not exists plans(id text primary key, kind text, content text not null);
                create table if not exists runs(id text primary key, created_at text, content text not null);
                create table if not exists artifacts(kind text, id text, created_at text, content text not null, primary key(kind,id));
                create table if not exists case_assets(case_id text,version integer,content text not null,primary key(case_id,version));
            """)
        else:
            conn.executescript("""
                create table if not exists assets(kind text,id text,content text not null,primary key(kind,id));
                create table if not exists runs(id text primary key,plan_id text,created_at text,content text not null);
            """)
        conn.executescript("""
            create table if not exists storage_files(table_name text,key_json text,path text,signature text,primary key(table_name,key_json));
            create table if not exists storage_transactions(id text primary key);
        """)

    def _key(self, table, row):
        columns = TABLE_KEYS[table]
        return tuple(row[column] for column in columns)

    def _file(self, table, row):
        value = json.loads(row["content"])
        if not isinstance(value, dict):
            raise ValueError("Stored asset must be an object")  # noqa: TRY004
        fields = ("case_id", "version") if table == "case_assets" else ("id",)
        if table == "plans":
            fields = ("id", "kind")
        if table == "runs":
            if value.get("run_id") != row["id"]:
                raise ValueError("Run identity differs from stored identity")
        elif (table != "assets" or row["kind"] in {"profile", "plan"}) and any(
            value.get(field) != row[field] for field in fields
        ):
            raise ValueError("Asset identity differs from stored identity")
        if table == "plans":
            path = (
                type_directory(self.root, self.project, row["kind"])
                / "plans"
                / (_identifier(row["id"]) + ".json")
            )
        elif table == "artifacts":
            kind, name = _identifier(row["kind"]), _identifier(row["id"])
            if kind in INTERNAL_ARTIFACTS:
                path = self.runtime / "state/testing" / kind / (name + ".json")
            else:
                base = type_directory(self.root, self.project, _asset_type(kind, value))
                group = ARTIFACT_GROUPS.get(kind)
                if kind == "ui_case":
                    path = base / "cases" / name / "goal.json"
                else:
                    path = (base / group if group else base / "artifacts" / kind) / (
                        name + ".json"
                    )
        elif table == "case_assets":
            platform = value.get("platform", "shared")
            if platform not in TYPE_DIRS:
                platform = "shared"
            version = row["version"]
            if not isinstance(version, int) or version < 0:
                raise ValueError("Invalid case asset version")
            path = (
                type_directory(self.root, self.project, platform)
                / "cases"
                / _identifier(row["case_id"])
                / f"v{version:06d}.json"
            )
        elif table == "assets" and row["kind"] not in {"profile", "plan"}:
            path = (
                self.runtime
                / "state/mobile"
                / _identifier(row["kind"])
                / (_identifier(row["id"]) + ".json")
            )
        elif table == "assets":
            platform = (
                value.get("platform") if row["kind"] == "profile" else value.get("kind")
            )
            if platform not in {"android", "ios"}:
                raise ValueError("Mobile asset requires Android or iOS")
            group = "profiles" if row["kind"] == "profile" else "plans/native"
            path = (
                type_directory(self.root, self.project, platform)
                / group
                / (_identifier(row["id"]) + ".json")
            )
        else:
            kind = value.get("kind", "shared")
            if kind not in TYPE_DIRS:
                kind = "shared"
            path = (
                project_reports_root(self.root, self.project)
                / TYPE_DIRS[kind]
                / _identifier(row["id"])
            )
            if self.namespace == "mobile":
                path /= "native"
            path /= "result.json"
            value["created_at"] = row["created_at"]
        return _inside(path, self.base), value

    def _rows(self, conn):
        return {
            (table, self._key(table, row)): dict(row)
            for table in self.tables
            for row in conn.execute("select * from " + table)
        }

    def _insert(self, conn, table, row):
        columns = tuple(row)
        conn.execute(
            f"insert or replace into {table} ({','.join(columns)}) values ({','.join('?' for _ in columns)})",
            tuple(row[c] for c in columns),
        )

    def _recover(self, conn):
        directory = _inside(
            self.runtime / "storage-transactions" / self.namespace, self.base
        )
        if not directory.exists():
            return
        for journal in sorted(directory.iterdir()):
            _inside(journal, self.base)
            if journal.name.endswith(".done"):
                shutil.rmtree(journal)
                conn.execute(
                    "delete from storage_transactions where id=?", (journal.name[:-5],)
                )
                continue
            manifest = journal / "manifest.json"
            if not manifest.is_file():
                shutil.rmtree(journal)
                continue
            data = _read(manifest)
            committed = conn.execute(
                "select 1 from storage_transactions where id=?", (journal.name,)
            ).fetchone()
            side = "after" if committed else "before"
            for index, change in enumerate(data["changes"]):
                target = _inside(self.base / change["path"], self.base)
                snapshot = journal / side / str(index)
                if change[side]:
                    atomic_write_bytes(
                        target, _inside(snapshot, self.base).read_bytes()
                    )
                elif target.exists():
                    target.unlink()
            self._discard_journal(journal)
            conn.execute("delete from storage_transactions where id=?", (journal.name,))
        conn.commit()

    def _discard_journal(self, journal):
        # Rename before recursive cleanup so an interrupted cleanup cannot leave
        # a live manifest whose snapshots have already been partially removed.
        completed = journal.with_name(journal.name + ".done")
        journal.rename(completed)
        shutil.rmtree(completed)

    def _commit(self, conn, changes):
        if self.namespace == "testing":
            timestamps = {
                kind + "/" + name: created
                for kind, name, created in conn.execute(
                    "select kind,id,created_at from artifacts"
                )
            }
            if timestamps or self.metadata.exists():
                changes[self.metadata] = _json(
                    {"artifact_created_at": timestamps}
                ).encode()
        for path in changes:
            _inside(path, self.base)
        changes = {
            path: content
            for path, content in changes.items()
            if (path.read_bytes() if path.exists() else None) != content
        }
        if not changes:
            conn.commit()
            return
        transaction = uuid.uuid4().hex
        journal = _inside(
            self.runtime / "storage-transactions" / self.namespace / transaction,
            self.base,
        )
        for side in ("before", "after"):
            (journal / side).mkdir(parents=True, exist_ok=True)
        entries = []
        for index, (path, content) in enumerate(changes.items()):
            _inside(path, self.base)
            before = path.read_bytes() if path.exists() else None
            entries.append(
                {
                    "path": str(path.relative_to(self.base)),
                    "before": before is not None,
                    "after": content is not None,
                }
            )
            if before is not None:
                atomic_write_bytes(journal / "before" / str(index), before)
            if content is not None:
                atomic_write_bytes(journal / "after" / str(index), content)
        atomic_write_text(journal / "manifest.json", _json({"changes": entries}))
        try:
            for path, content in changes.items():
                if content is None:
                    if path.exists():
                        path.unlink()
                else:
                    atomic_write_bytes(path, content)
            conn.execute("insert into storage_transactions values(?)", (transaction,))
            conn.commit()
        except BaseException:
            conn.rollback()
            self._recover(conn)
            raise
        # A crash before cleanup is resolved by the committed transaction row.
        self._discard_journal(journal)
        conn.execute("delete from storage_transactions where id=?", (transaction,))
        conn.commit()

    def _migrate(self, conn):
        marker = _layout_marker(self.root, self.project)
        state = _read(marker)
        if self.namespace in state.get("migrated_databases", []):
            return
        changes = {}
        if self.legacy.is_file():
            _inside(self.legacy, self.base)
            with sqlite3.connect(
                self.legacy.resolve().as_uri() + "?mode=ro", uri=True
            ) as old:
                old.row_factory = sqlite3.Row
                old.execute("begin")
                tables = {
                    r[0]
                    for r in old.execute(
                        "select name from sqlite_master where type='table'"
                    )
                }
                for table in self.tables:
                    if table not in tables:
                        continue
                    for raw in old.execute("select * from " + table):
                        row = dict(raw)
                        path, value = self._file(table, row)
                        _validate_content(table, row.get("kind"), value)
                        content = _json(value).encode()
                        if path.exists():
                            if _read(path) != value:
                                raise ValueError(
                                    "Storage migration conflict; original database retained: "
                                    + str(path)
                                )
                        else:
                            changes[path] = content
                        self._insert(conn, table, row)
        state.setdefault("migrated_databases", []).append(self.namespace)
        changes[marker] = _json(state).encode()
        self._commit(conn, changes)

    def _discover(self):
        assets = project_assets_dir(self.root, self.project)
        reports = project_reports_root(self.root, self.project)
        files = []
        if self.namespace == "testing":
            for kind, directory in TYPE_DIRS.items():
                base = assets / directory
                files.extend(
                    ("plans", p, kind) for p in (base / "plans").glob("*.json")
                )
                files.extend(
                    ("artifacts", p, p.parent.name)
                    for p in (base / "artifacts").glob("*/*.json")
                )
                for artifact, group in ARTIFACT_GROUPS.items():
                    pattern = "*/goal.json" if artifact == "ui_case" else "*.json"
                    files.extend(
                        ("artifacts", p, artifact) for p in (base / group).glob(pattern)
                    )
                files.extend(
                    ("case_assets", p, kind) for p in (base / "cases").glob("*/v*.json")
                )
            files.extend(
                ("artifacts", p, p.parent.name)
                for p in (self.runtime / "state/testing").glob("*/*.json")
            )
        else:
            for kind in ("android", "ios"):
                base = assets / kind
                files.extend(
                    ("assets", p, "profile") for p in (base / "profiles").glob("*.json")
                )
                files.extend(
                    ("assets", p, "plan")
                    for p in (base / "plans/native").glob("*.json")
                )
            files.extend(
                ("assets", p, p.parent.name)
                for p in (self.runtime / "state/mobile").glob("*/*.json")
            )
        for kind, directory in TYPE_DIRS.items():
            pattern = (
                "*/result.json"
                if self.namespace == "testing"
                else "*/native/result.json"
            )
            files.extend(
                ("runs", p, kind)
                for p in (reports / directory).glob(pattern)
            )
        return files

    def _sync(self, conn):
        seen = set()
        timestamps = (
            _read(_inside(self.metadata, self.base)).get("artifact_created_at", {})
            if self.namespace == "testing" and self.metadata.is_file()
            else {}
        )
        for table, path, kind in self._discover():
            _inside(path, self.base)
            stat = path.stat()
            signature = json.dumps(
                [
                    stat.st_dev,
                    stat.st_ino,
                    stat.st_size,
                    stat.st_mtime_ns,
                    stat.st_ctime_ns,
                ]
            )
            relative = str(path.relative_to(self.base))
            cached = conn.execute(
                "select key_json,signature from storage_files where table_name=? and path=?",
                (table, relative),
            ).fetchone()
            if cached and cached[1] == signature:
                seen.add((table, cached[0]))
                continue
            value = _read(path)
            _validate_content(table, kind, value)
            content = json.dumps(value, ensure_ascii=False, allow_nan=False)
            if table == "plans":
                if (
                    value["kind"] != kind
                    or path.name != _identifier(value["id"]) + ".json"
                ):
                    raise ValueError(
                        "Plan identity differs from its asset path: " + str(path)
                    )
                row = {"id": value["id"], "kind": kind, "content": content}
            elif table == "artifacts":
                existing = conn.execute(
                    "select created_at from artifacts where kind=? and id=?",
                    (kind, value["id"]),
                ).fetchone()
                created_at = (
                    timestamps.get(kind + "/" + value["id"])
                    or (existing[0] if existing else value.get("created_at"))
                    or datetime.fromtimestamp(stat.st_mtime, UTC).isoformat()
                )
                row = {
                    "kind": _identifier(kind),
                    "id": _identifier(value["id"]),
                    "created_at": created_at,
                    "content": content,
                }
            elif table == "case_assets":
                row = {
                    "case_id": _identifier(value["case_id"]),
                    "version": value["version"],
                    "content": content,
                }
            elif table == "assets":
                name = value["id"] if kind in {"profile", "plan"} else path.stem
                row = {"kind": kind, "id": _identifier(name), "content": content}
            else:
                row = {
                    "id": _identifier(value["run_id"]),
                    "created_at": str(value.get("created_at", "")),
                    "content": content,
                }
                if self.namespace == "mobile":
                    row["plan_id"] = value["plan_id"]
            expected_path, _ = self._file(table, row)
            if expected_path != path:
                raise ValueError(
                    "Asset identity differs from its file path: " + str(path)
                )
            key = (table, json.dumps(self._key(table, row)))
            duplicate = conn.execute(
                "select path from storage_files where table_name=? and key_json=?", key
            ).fetchone()
            if (
                duplicate
                and duplicate[0] != relative
                and (self.base / duplicate[0]).exists()
            ):
                raise ValueError(
                    "Duplicate asset identity in different files: " + str(path)
                )
            self._insert(conn, table, row)
            conn.execute(
                "insert or replace into storage_files values(?,?,?,?)",
                (*key, relative, signature),
            )
            if key in seen:
                raise ValueError("Duplicate asset identity: " + str(path))
            seen.add(key)
        for row in conn.execute(
            "select table_name,key_json from storage_files"
        ).fetchall():
            if tuple(row) not in seen:
                self._delete(conn, row[0], json.loads(row[1]))
                conn.execute(
                    "delete from storage_files where table_name=? and key_json=?",
                    tuple(row),
                )
        # A crash immediately after legacy import can leave SQL rows whose
        # files were never registered in storage_files. Never treat these
        # orphaned cache rows as authoritative assets.
        for table in self.tables:
            columns = ",".join(TABLE_KEYS[table])
            for key_values in conn.execute(
                "select " + columns + " from " + table
            ).fetchall():
                if (table, json.dumps(tuple(key_values))) not in seen:
                    self._delete(conn, table, key_values)
        self._commit(conn, {})

    def _delete(self, conn, table, key):
        columns = TABLE_KEYS[table]
        conn.execute(
            "delete from "
            + table
            + " where "
            + " and ".join(c + "=?" for c in columns),
            tuple(key),
        )

    @contextmanager
    def connection(self):
        ensure_project_layout(self.root, self.project)
        key = str(resolved_path(self.path))
        active = getattr(_LOCAL, "connections", {})
        if key in active:
            yield active[key]
            return
        with project_operation_lock(
            self.root, self.project, operation="asset-storage", lock_file="storage.lock"
        ):
            _inside(self.path, self.base)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=10)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("pragma journal_mode=WAL")
                self._schema(conn)
                self._recover(conn)
                self._migrate(conn)
                self._sync(conn)
                before = self._rows(conn)
                active[key] = conn
                _LOCAL.connections = active
                yield conn
                after = self._rows(conn)
                changes = {}
                for identity in before.keys() | after.keys():
                    old, new = before.get(identity), after.get(identity)
                    if old == new:
                        continue
                    table, key_values = identity
                    tracking_key = (table, json.dumps(key_values))
                    if old:
                        old_path, _ = self._file(identity[0], old)
                        changes[old_path] = None
                    if new:
                        new_path, value = self._file(identity[0], new)
                        changes[new_path] = _json(value).encode()
                        conn.execute(
                            "insert or replace into storage_files values(?,?,?,?)",
                            (*tracking_key, str(new_path.relative_to(self.base)), ""),
                        )
                    else:
                        conn.execute(
                            "delete from storage_files where table_name=? and key_json=?",
                            tracking_key,
                        )
                self._commit(conn, changes)
            except BaseException:
                conn.rollback()
                raise
            finally:
                active.pop(key, None)
                conn.close()
