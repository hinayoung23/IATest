"""Read-only, bounded database sources with explicit table authorization."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["driver", "path_env", "query", "allowed_tables"],
    "properties": {
        "driver": {"const": "sqlite"},
        "path_env": {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]*$"},
        "query": {"type": "string", "minLength": 1, "maxLength": 16384},
        "parameters": {
            "type": "array",
            "maxItems": 100,
            "items": {"type": ["string", "number", "boolean", "null"]},
        },
        "allowed_tables": {
            "type": "array",
            "minItems": 1,
            "maxItems": 64,
            "items": {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]*$"},
        },
        "max_rows": {"type": "integer", "minimum": 1, "maximum": 10000},
        "timeout_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 30},
    },
}
SQLITE_SCHEMA = SCHEMA
NETWORK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "driver",
        "host",
        "port",
        "database",
        "credentials",
        "allowed_origins",
        "query",
        "allowed_tables",
    ],
    "properties": {
        **{
            k: v
            for k, v in SQLITE_SCHEMA["properties"].items()
            if k not in {"driver", "path_env"}
        },
        "driver": {"enum": ["postgresql", "mysql"]},
        "host": {
            "type": "string",
            "minLength": 1,
            "maxLength": 253,
            "pattern": "^[A-Za-z0-9_.:-]+$",
        },
        "port": {"type": "integer", "minimum": 1, "maximum": 65535},
        "database": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "pattern": "^[A-Za-z_][A-Za-z0-9_-]*$",
        },
        "schema": {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]*$"},
        "credentials": {
            "type": "object",
            "additionalProperties": False,
            "required": ["username", "password"],
            "properties": {
                key: {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["$env"],
                    "properties": {
                        "$env": {
                            "type": "string",
                            "pattern": "^[A-Za-z_][A-Za-z0-9_]*$",
                        }
                    },
                }
                for key in ("username", "password")
            },
        },
        "allowed_origins": {
            "type": "array",
            "minItems": 1,
            "maxItems": 32,
            "items": {"type": "string"},
        },
        "tls": {"enum": ["verify-full", "disable"]},
        "tls_ca_env": {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]*$"},
    },
}
SCHEMA = {"oneOf": [SQLITE_SCHEMA, NETWORK_SCHEMA]}


def read_records(source, control, *, root=None, project="default"):
    if source.get("driver") != "sqlite":
        from iatest.engines.database_network import read_network_records
        from iatest.engines.http import validate

        validate(source, NETWORK_SCHEMA)
        return read_network_records(source, control, root=root, project=project)
    path = Path(os.environ.get(source["path_env"], "")).expanduser()
    if not path.is_absolute() or not path.is_file():
        raise ValueError(
            "SQLite path environment must select an existing absolute file"
        )
    path = path.resolve(strict=True)
    deadline = time.monotonic() + min(
        source.get("timeout_seconds", 5), control.remaining() or 30
    )
    allowed = {name.lower() for name in source["allowed_tables"]}
    maximum = source.get("max_rows", 10000)
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.1)
    try:
        db.execute("PRAGMA query_only=ON")
        # Some Python builds (notably macOS) compile SQLite without extension
        # loading, so the disabling API is absent. The SQL authorizer below
        # also rejects load_extension regardless of build capabilities.
        if hasattr(db, "enable_load_extension"):
            db.enable_load_extension(False)

        def authorize(action, one, two, database, trigger):
            if action == sqlite3.SQLITE_SELECT:
                return sqlite3.SQLITE_OK
            if (
                action == sqlite3.SQLITE_READ
                # SQLite can omit the database name for a columnless read
                # (COUNT(*) or rowid optimization). ATTACH/temp writes remain denied.
                and (database == "main" or (database is None and two == ""))
                and (one or "").lower() in allowed
            ):
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_FUNCTION and (two or "").lower() in {
                "count",
                "sum",
                "avg",
                "min",
                "max",
                "length",
                "lower",
                "upper",
                "coalesce",
                "ifnull",
                "abs",
                "round",
                "substr",
                "trim",
                "typeof",
            }:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        db.set_authorizer(authorize)

        def progress():
            control.check()
            return int(time.monotonic() >= deadline)

        db.set_progress_handler(progress, 1000)
        control.check()
        cursor = db.execute(source["query"], source.get("parameters", []))
        names = [d[0] for d in cursor.description or []]
        if not names or len(set(names)) != len(names) or len(names) > 100:
            raise ValueError("Database query requires unique bounded result columns")
        rows = []
        size = 0
        for row in cursor:
            control.check()
            if time.monotonic() >= deadline or len(rows) >= maximum:
                raise ValueError(
                    "Database query exceeded time/row quota; no partial pass"
                )
            for item in row:
                if isinstance(item, bytes):
                    raise ValueError(  # noqa: TRY004 -- rejected query data, not a Python caller type
                        "Binary database fields need explicit SQL conversion"
                    )
                size += len(str(item).encode())
            if size > 4194304:
                raise ValueError("Database result exceeds 4 MiB")
            rows.append(dict(zip(names, row)))
        return rows
    except sqlite3.Error as exc:
        raise ValueError(
            "Read-only database query rejected (" + type(exc).__name__ + ")"
        ) from None
    finally:
        db.close()
