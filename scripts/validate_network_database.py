"""Real PostgreSQL/MySQL acceptance in owned temporary clusters; no existing data used."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from iatest.application.testing_service import execute_test_plan
from iatest.core.config import init_project, load_config, save_config
from iatest.engines.database import read_records
from iatest.execution.control import RunCancelled, RunControl


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.contextmanager
def server(command, address, log):
    with log.open("w") as stream:
        process = subprocess.Popen(
            [str(v) for v in command], stdout=stream, stderr=stream
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(
                        "Owned database exited during startup; see " + str(log)
                    )
                try:
                    with socket.create_connection(address, timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                raise RuntimeError(
                    "Owned database did not listen within startup budget"
                )
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def command(*args):
    subprocess.run([str(v) for v in args], check=True, capture_output=True, timeout=60)


def exercise(root, source, report):
    config = load_config(root)
    for isolation in ("policy", "process"):
        config["sandbox"]["mode"] = isolation
        save_config(root, config)
        result = execute_test_plan(
            root,
            "suite",
            {
                "id": source["driver"] + "-" + isolation,
                "kind": "data",
                "budget": {"timeout_seconds": 20, "max_requests": 10},
                "spec": {
                    "database": source,
                    "checks": [
                        {"id": "ids", "rule": "unique", "path": "$.id"},
                        {"id": "count", "rule": "count", "min": 2, "max": 2},
                    ],
                },
            },
        )
        report["runs"].append(result)
        assert result["verdict"] == "passed", result
    config["sandbox"]["mode"] = "policy"
    save_config(root, config)
    rows = read_records(
        source, RunControl(timeout_seconds=10), root=root, project="suite"
    )
    assert len(rows) == 2 and rows[0]["amount"] == "1.2500"
    report["decimal_and_datetime"] = isinstance(rows[0]["created"], str)
    rows = read_records(
        {
            **source,
            "query": "select count(*) as total from accounts where email like '%example%'",
            "parameters": [],
        },
        RunControl(timeout_seconds=5),
        root=root,
        project="suite",
    )
    assert rows == [{"total": 2}]
    for changes in (
        {"query": "delete from accounts", "parameters": []},
        {"query": "select * from secrets", "parameters": []},
        {"query": "select id from accounts; delete from accounts", "parameters": []},
        {"query": "select id,id from accounts", "parameters": []},
        {
            "query": "select * from readonly_view",
            "parameters": [],
            "allowed_tables": ["readonly_view"],
        },
        {"max_rows": 1},
    ):
        try:
            read_records(
                {**source, **changes},
                RunControl(timeout_seconds=5),
                root=root,
                project="suite",
            )
        except ValueError:
            report["rejected"].append(changes)
        else:
            raise AssertionError("Unsafe or partial database result was accepted")
    joins = " accounts a0 " + " ".join(
        "cross join accounts a" + str(i) for i in range(1, 30)
    )
    heavy = {
        **source,
        "query": "select sum(a0.id + a29.id) as total from " + joins,
        "parameters": [],
    }
    began = time.monotonic()
    try:
        read_records(
            {**heavy, "timeout_seconds": 0.15},
            RunControl(timeout_seconds=5),
            root=root,
            project="suite",
        )
    except ValueError as error:
        assert "quota" in str(error), str(error)
    else:
        raise AssertionError("Database query timeout was not enforced")
    report["query_timeout_ms"] = (time.monotonic() - began) * 1000
    control = RunControl(timeout_seconds=5)
    timer = threading.Timer(0.15, control.cancelled.set)
    timer.start()
    try:
        try:
            read_records(
                {**heavy, "timeout_seconds": 3}, control, root=root, project="suite"
            )
        except RunCancelled:
            report["parent_cancelled"] = True
        else:
            raise AssertionError("Parent cancellation was not propagated")
    finally:
        timer.cancel()
        timer.join()
    without_ca = {k: v for k, v in source.items() if k != "tls_ca_env"}
    try:
        read_records(
            without_ca, RunControl(timeout_seconds=5), root=root, project="suite"
        )
    except ValueError:
        report["untrusted_tls_rejected"] = True
    else:
        raise AssertionError("Untrusted database certificate was accepted")
    assert (
        len(
            read_records(
                source, RunControl(timeout_seconds=5), root=root, project="suite"
            )
        )
        == 2
    )
    report["passed"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--postgres-bin", type=Path, default=Path("/opt/homebrew/opt/postgresql@18/bin")
    )
    parser.add_argument(
        "--mysql-bin", type=Path, default=Path("/opt/homebrew/opt/mysql@8.4/bin")
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    import psycopg
    import pymysql

    args.output.parent.mkdir(parents=True, exist_ok=True)
    for previous in (
        args.output,
        args.output.with_name("postgresql-server.log"),
        args.output.with_name("mysql-server.log"),
    ):
        if previous.exists():
            previous.with_name(
                previous.stem + "-attempt-" + str(time.time_ns()) + previous.suffix
            ).write_bytes(previous.read_bytes())
    report = {
        "synthetic_database": False,
        "owned_temporary_servers": True,
        "passed": False,
        "databases": {},
    }
    references = {
        "IATEST_DATABASE_VALIDATION_USER": "iatest_reader",
        "IATEST_DATABASE_VALIDATION_PASS": secrets.token_hex(24),
    }
    previous = {
        k: os.environ.get(k) for k in (*references, "IATEST_DATABASE_VALIDATION_CA")
    }
    os.environ.update(references)
    try:
        with tempfile.TemporaryDirectory(prefix="iatest-database-") as directory:
            root = Path(directory)
            init_project(root, "suite")
            cert, key = root / "server.pem", root / "server.key"
            command(
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                key,
                "-out",
                cert,
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:localhost,IP:127.0.0.1",
            )
            key.chmod(0o600)
            os.environ["IATEST_DATABASE_VALIDATION_CA"] = str(cert)
            for driver in ("postgresql", "mysql"):
                selected = port()
                entry = {"runs": [], "rejected": [], "passed": False}
                report["databases"][driver] = entry
                data = root / driver
                if driver == "postgresql":
                    password_file = root / "postgres-password"
                    password_file.write_text(
                        references["IATEST_DATABASE_VALIDATION_PASS"]
                    )
                    password_file.chmod(0o600)
                    command(
                        args.postgres_bin / "initdb",
                        "-D",
                        data,
                        "--username=iatest_admin",
                        "--auth-host=scram-sha-256",
                        "--auth-local=trust",
                        "--pwfile=" + str(password_file),
                        "--encoding=UTF8",
                        "--locale=C",
                    )
                    run = [
                        args.postgres_bin / "postgres",
                        "-D",
                        data,
                        "-h",
                        "127.0.0.1",
                        "-p",
                        str(selected),
                        "-k",
                        root,
                        "-c",
                        "ssl=on",
                        "-c",
                        "ssl_cert_file=" + str(cert),
                        "-c",
                        "ssl_key_file=" + str(key),
                    ]
                else:
                    command(
                        args.mysql_bin / "mysqld",
                        "--no-defaults",
                        "--initialize-insecure",
                        "--datadir=" + str(data),
                    )
                    run = [
                        args.mysql_bin / "mysqld",
                        "--no-defaults",
                        "--datadir=" + str(data),
                        "--bind-address=127.0.0.1",
                        "--port=" + str(selected),
                        "--socket=" + str(root / "mysql.sock"),
                        "--pid-file=" + str(root / "mysql.pid"),
                        "--mysqlx=OFF",
                        "--skip-name-resolve",
                        "--ssl-ca=" + str(cert),
                        "--ssl-cert=" + str(cert),
                        "--ssl-key=" + str(key),
                    ]
                with server(
                    run,
                    ("127.0.0.1", selected),
                    args.output.with_name(driver + "-server.log"),
                ) as process:
                    if driver == "postgresql":
                        with psycopg.connect(
                            host="127.0.0.1",
                            port=selected,
                            dbname="postgres",
                            user="iatest_admin",
                            password=references["IATEST_DATABASE_VALIDATION_PASS"],
                            sslmode="disable",
                            autocommit=True,
                        ) as admin:
                            admin.execute("CREATE DATABASE lab")
                            admin.execute(
                                psycopg.sql.SQL(
                                    "CREATE ROLE iatest_reader LOGIN PASSWORD {}"
                                ).format(
                                    psycopg.sql.Literal(
                                        references["IATEST_DATABASE_VALIDATION_PASS"]
                                    )
                                )
                            )
                        with psycopg.connect(
                            host="127.0.0.1",
                            port=selected,
                            dbname="lab",
                            user="iatest_admin",
                            password=references["IATEST_DATABASE_VALIDATION_PASS"],
                            sslmode="disable",
                            autocommit=True,
                        ) as admin:
                            admin.execute(
                                "CREATE TABLE accounts(id integer, email text, amount numeric(20,4), created timestamptz)"
                            )
                            admin.execute(
                                "INSERT INTO accounts VALUES (1,'a@example.test',1.25,'2026-10-22T00:00:00Z'),(2,'b@example.test',2.5,'2026-10-22T00:00:00Z')"
                            )
                            admin.execute("CREATE TABLE secrets(value text)")
                            admin.execute(
                                "CREATE VIEW readonly_view AS SELECT id FROM accounts"
                            )
                            admin.execute(
                                "GRANT SELECT ON accounts,readonly_view TO iatest_reader"
                            )
                    else:
                        with (
                            pymysql.connect(
                                unix_socket=str(root / "mysql.sock"),
                                user="root",
                                autocommit=True,
                            ) as admin,
                            admin.cursor() as cursor,
                        ):
                            cursor.execute("CREATE DATABASE lab")
                            cursor.execute(
                                "CREATE TABLE lab.accounts(id integer, email varchar(128), amount decimal(20,4), created timestamp)"
                            )
                            cursor.execute(
                                "INSERT INTO lab.accounts VALUES (1,'a@example.test',1.25,'2026-10-22 00:00:00'),(2,'b@example.test',2.5,'2026-10-22 00:00:00')"
                            )
                            cursor.execute("CREATE TABLE lab.secrets(value text)")
                            cursor.execute(
                                "CREATE VIEW lab.readonly_view AS SELECT id FROM lab.accounts"
                            )
                            cursor.execute(
                                "CREATE USER 'iatest_reader'@'127.0.0.1' IDENTIFIED BY %s",
                                (references["IATEST_DATABASE_VALIDATION_PASS"],),
                            )
                            cursor.execute(
                                "GRANT SELECT ON lab.accounts TO 'iatest_reader'@'127.0.0.1'"
                            )
                            cursor.execute(
                                "GRANT SELECT ON lab.readonly_view TO 'iatest_reader'@'127.0.0.1'"
                            )
                    source = {
                        "driver": driver,
                        "host": "127.0.0.1",
                        "port": selected,
                        "database": "lab",
                        "credentials": {
                            "username": {"$env": "IATEST_DATABASE_VALIDATION_USER"},
                            "password": {"$env": "IATEST_DATABASE_VALIDATION_PASS"},
                        },
                        "tls": "verify-full",
                        "tls_ca_env": "IATEST_DATABASE_VALIDATION_CA",
                        "allowed_origins": [f"https://127.0.0.1:{selected}"],
                        "query": "select id,email,amount,created from accounts where id > ? order by id",
                        "parameters": [0],
                        "allowed_tables": ["accounts"],
                    }
                    exercise(root, source, entry)
                    entry["owned_server_pid"] = process.pid
                entry["owned_server_stopped"] = process.poll() is not None
        report["temporary_clusters_removed"] = True
        report["passed"] = all(
            v["passed"] and v["owned_server_stopped"]
            for v in report["databases"].values()
        )
    except Exception as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    assert report["passed"]
    print(json.dumps({"passed": True, "databases": list(report["databases"])}))


if __name__ == "__main__":
    main()
