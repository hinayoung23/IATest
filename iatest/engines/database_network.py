"""Authorized SELECTs over PostgreSQL/MySQL; optional drivers never load for SQLite."""

from __future__ import annotations
from iatest.core.env import environment_values

import datetime
import decimal
import importlib
import ipaddress
import json
import math
import os
import socket
import ssl
import threading
import time
import uuid

from iatest.core.config import load_config
from iatest.core.sandbox import ACTIVE_POLICY, SandboxPolicy
from iatest.engines.http import NetworkScope


def optional(name):
    try:
        return importlib.import_module(name)
    except ImportError:
        raise ValueError(
            "Install the database extra: pip install 'iatest-framework[database]'"
        ) from None


def compile_query(source, *, include_tables=False):
    """Canonicalize a restricted SELECT AST; never forward unchecked SQL or hints."""
    sqlglot = optional("sqlglot")
    exp = sqlglot.exp
    dialect = "postgres" if source["driver"] == "postgresql" else "mysql"
    try:
        trees = sqlglot.parse(source["query"], read=dialect)
    except sqlglot.errors.SqlglotError:
        raise ValueError("Database SELECT could not be parsed") from None
    if len(trees) != 1 or not isinstance(trees[0], (exp.Select, exp.SetOperation)):
        raise ValueError("Exactly one SELECT is required")
    tree = trees[0]
    safe = {
        "Select",
        "Union",
        "Intersect",
        "Except",
        "Subquery",
        "With",
        "CTE",
        "From",
        "Join",
        "Table",
        "TableAlias",
        "Identifier",
        "Column",
        "Star",
        "Alias",
        "Literal",
        "Null",
        "Boolean",
        "Placeholder",
        "Distinct",
        "Where",
        "Group",
        "Having",
        "Order",
        "Ordered",
        "Limit",
        "Offset",
        "Paren",
        "And",
        "Or",
        "Not",
        "EQ",
        "NEQ",
        "GT",
        "GTE",
        "LT",
        "LTE",
        "Is",
        "In",
        "Between",
        "Like",
        "ILike",
        "Escape",
        "Exists",
        "Add",
        "Sub",
        "Mul",
        "Div",
        "Mod",
        "Neg",
        "Case",
        "If",
        "Tuple",
    }
    functions = {
        "COUNT",
        "SUM",
        "AVG",
        "MIN",
        "MAX",
        "LENGTH",
        "LOWER",
        "UPPER",
        "COALESCE",
        "ABS",
        "ROUND",
        "SUBSTRING",
        "TRIM",
    }
    for node in tree.walk():
        if isinstance(node, exp.Func) and not isinstance(node, (exp.Anonymous, exp.If)):
            if node.sql_name() not in functions:
                raise ValueError("Database function is outside the read-only allowlist")
        elif type(node).__name__ not in safe:
            raise ValueError(
                "Database SELECT contains an unsupported operation: "
                + type(node).__name__
            )
        if isinstance(node, exp.With) and node.args.get("recursive"):
            raise ValueError(
                "Recursive database queries are outside the bounded subset"
            )
    from sqlglot.optimizer.scope import traverse_scope

    namespace = source.get(
        "schema", "public" if dialect == "postgres" else source["database"]
    )
    if dialect == "mysql" and namespace != source["database"]:
        raise ValueError("MySQL table namespace must match the selected database")
    allowed = set(source["allowed_tables"])
    physical_tables = set()
    for scope in traverse_scope(tree):
        for table in scope.sources.values():
            if not isinstance(table, exp.Table):
                continue
            name = table.name
            if dialect == "postgres" and not table.this.args.get("quoted"):
                name = name.lower()
            if (
                table.catalog
                or table.db
                and table.db != namespace
                or name not in allowed
            ):
                raise ValueError(
                    "Database table is outside the declared namespace/allowlist"
                )
            table.set("db", exp.to_identifier(namespace, quoted=True))
            physical_tables.add(name)
    placeholders = list(tree.find_all(exp.Placeholder))
    if len(placeholders) != len(source.get("parameters", [])) or any(
        p.args.get("this") for p in placeholders
    ):
        raise ValueError(
            "Database placeholders must match the positional parameter list"
        )
    marker = "__iatest_bind_" + uuid.uuid4().hex
    for placeholder in placeholders:
        placeholder.replace(exp.Var(this=marker))
    try:
        query = tree.sql(
            dialect=dialect, comments=False, unsupported_level=sqlglot.ErrorLevel.RAISE
        )
    except sqlglot.errors.SqlglotError:
        raise ValueError(
            "Database SELECT is unsupported by the selected dialect"
        ) from None
    query = query.replace("%", "%%").replace(marker, "%s")
    return (query, namespace, sorted(physical_tables)) if include_tables else query


def normalized(value, depth=0):
    if depth > 12:
        raise ValueError("Database result nesting exceeds quota")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, decimal.Decimal) and value.is_finite():
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, list) and len(value) <= 10000:
        return [normalized(item, depth + 1) for item in value]
    if isinstance(value, dict) and len(value) <= 100:
        return {str(k): normalized(v, depth + 1) for k, v in value.items()}
    raise ValueError("Database field requires an explicit supported SQL conversion")


def network(source, root, project):
    if root is None:
        raise ValueError("Network database queries require a project network policy")
    tls = source.get("tls", "verify-full") != "disable"
    host = source["host"]
    address = ("[" + host + "]") if ":" in host else host
    scope = NetworkScope(root, project, source["allowed_origins"])
    scope.check(
        ("https" if tls else "http") + "://" + address + ":" + str(source["port"])
    )
    addresses = list(
        dict.fromkeys(
            row[4][0]
            for row in socket.getaddrinfo(host, source["port"], type=socket.SOCK_STREAM)
        )
    )
    if not addresses:
        raise ValueError("Database host has no usable address")
    for policy in (scope.policy, scope.parent):
        if (
            policy
            and policy.mode != "off"
            and policy.allowed_networks
            and any(
                not any(
                    ipaddress.ip_address(ip) in net for net in policy.allowed_networks
                )
                for ip in addresses
            )
        ):
            raise PermissionError(
                "Database resolved address escaped the allowed networks"
            )
    if not tls and any(not ipaddress.ip_address(ip).is_loopback for ip in addresses):
        raise ValueError("Unverified database transport is restricted to loopback")
    ca = None
    if source.get("tls_ca_env"):
        if not tls:
            raise ValueError("A CA reference requires verified database TLS")
        ca = SandboxPolicy.from_config(
            root, load_config(root), project=project
        ).check_path(environment_values().get(source["tls_ca_env"], ""))
        if ACTIVE_POLICY.get():
            ACTIVE_POLICY.get().check_path(ca)
        if not ca.is_file() or ca.stat().st_size > 1048576:
            raise ValueError("Database CA must be a bounded authorized file")
    return tls, addresses[0], str(ca) if ca else None


def read_network_records(source, control, *, root, project):
    query, namespace, tables = compile_query(source, include_tables=True)
    tls, address, ca = network(source, root, project)
    credentials = {}
    for key, reference in source["credentials"].items():
        if reference["$env"] not in environment_values():
            raise ValueError("Database credential environment reference is missing")
        credentials[key] = environment_values()[reference["$env"]]
        if len(credentials[key]) > (256 if key == "username" else 16384):
            raise ValueError("Database credential reference exceeds quota")
    driver = optional("psycopg" if source["driver"] == "postgresql" else "pymysql")
    deadline = time.monotonic() + min(
        source.get("timeout_seconds", 5), control.remaining() or 30
    )

    def remaining():
        control.check()
        left = deadline - time.monotonic()
        if left <= 0:
            raise ValueError("Database query exceeded time quota; no partial pass")
        return left

    control.request()
    from iatest.execution import native_ipc

    if native_ipc.authorize_request is not None:
        native_ipc.authorize_request(control)
    db = cursor = transport = watcher = None
    stopped = threading.Event()
    interrupted = threading.Event()
    try:
        if source["driver"] == "postgresql":
            db = driver.connect(
                host=source["host"],
                hostaddr=address,
                port=source["port"],
                dbname=source["database"],
                user=credentials["username"],
                password=credentials["password"],
                sslmode="verify-full" if tls else "disable",
                sslrootcert=ca or "system" if tls else None,
                connect_timeout=max(1, math.ceil(remaining())),
                options="-c search_path=pg_catalog -c statement_timeout="
                + str(max(1, int(remaining() * 1000))),
                autocommit=False,
            )
            db.read_only = True
        else:
            context = ssl.create_default_context(cafile=ca) if tls else None
            db = driver.connect(
                host=source["host"],
                port=source["port"],
                database=source["database"],
                user=credentials["username"],
                password=credentials["password"],
                ssl=context,
                ssl_disabled=not tls,
                local_infile=False,
                autocommit=False,
                defer_connect=True,
                connect_timeout=max(1, math.ceil(remaining())),
                read_timeout=remaining(),
                write_timeout=remaining(),
            )
            transport = socket.create_connection(
                (address, source["port"]), timeout=remaining()
            )
            db.connect(sock=transport)
        remaining()

        def watch():
            while not stopped.wait(0.03):
                if control.reason() or time.monotonic() >= deadline:
                    interrupted.set()
                    try:
                        if source["driver"] == "postgresql":
                            db.cancel_safe(timeout=1)
                        else:
                            if db._sock is not None:
                                db._sock.shutdown(socket.SHUT_RDWR)
                    except (OSError, driver.Error):
                        pass
                    return

        watcher = threading.Thread(
            target=watch, daemon=True, name="iatest-database-deadline"
        )
        watcher.start()
        if source["driver"] == "postgresql":
            with db.cursor() as setup:
                setup.execute("SHOW transaction_read_only")
                if setup.fetchone()[0] != "on":
                    raise ValueError("Database did not enter a read-only transaction")
                for table in tables:
                    setup.execute(
                        "SELECT c.relkind FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname=%s",
                        (namespace, table),
                    )
                    relation = setup.fetchone()
                    if relation is None or relation[0] not in {"r", "p"}:
                        raise ValueError(
                            "Database queries require base tables; views/foreign relations are not authorized"
                        )
            cursor = db.cursor(name="iatest_" + uuid.uuid4().hex)
        else:
            with db.cursor() as setup:
                setup.execute(
                    "SET SESSION MAX_EXECUTION_TIME = %s",
                    (max(1, int(remaining() * 1000)),),
                )
                setup.execute("SET SESSION TRANSACTION READ ONLY")
                setup.execute("START TRANSACTION READ ONLY")
                setup.execute("SELECT @@transaction_read_only")
                if int(setup.fetchone()[0]) != 1:
                    raise ValueError("Database did not enter a read-only transaction")
                for table in tables:
                    setup.execute(
                        "SELECT TABLE_TYPE FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
                        (namespace, table),
                    )
                    relation = setup.fetchone()
                    if relation is None or relation[0] != "BASE TABLE":
                        raise ValueError(
                            "Database queries require base tables; views are not authorized"
                        )
            cursor = db.cursor(driver.cursors.SSCursor)
        cursor.execute(query, tuple(source.get("parameters", [])))
        names = [column[0] for column in cursor.description or []]
        if (
            not names
            or len(names) > 100
            or len(set(names)) != len(names)
            or any(len(n) > 256 for n in names)
        ):
            raise ValueError("Database query requires unique bounded result columns")
        rows, size = [], 2
        while True:
            remaining()
            row = cursor.fetchone()
            if row is None:
                break
            if len(rows) >= source.get("max_rows", 10000):
                raise ValueError("Database query exceeded row quota; no partial pass")
            record = dict(zip(names, (normalized(value) for value in row), strict=True))
            size += (
                len(json.dumps(record, ensure_ascii=False, allow_nan=False).encode())
                + 1
            )
            if size > 4194304:
                raise ValueError("Database result exceeds 4 MiB")
            rows.append(record)
        remaining()
        return rows
    except driver.Error:
        control.check()
        if interrupted.is_set() or time.monotonic() >= deadline:
            raise ValueError(
                "Database query exceeded time quota; no partial pass"
            ) from None
        raise ValueError(
            "Read-only database query rejected by " + source["driver"]
        ) from None
    finally:
        stopped.set()
        if watcher:
            watcher.join(1.2)
        if db:
            # Closing the socket avoids SSCursor.close() draining an unbounded result.
            if source["driver"] == "mysql":
                for result in (
                    getattr(cursor, "_result", None),
                    getattr(db, "_result", None),
                ):
                    if result is not None:
                        result.unbuffered_active = False
                if cursor is not None:
                    cursor.connection = None
                    cursor._result = None
                db._force_close()
            else:
                db.close()
        if transport:
            transport.close()
