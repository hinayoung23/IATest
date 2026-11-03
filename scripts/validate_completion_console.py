"""Real browser acceptance for frozen protobuf assets and native plan execution."""

import argparse
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from iatest.core.config import init_project
from iatest.engines.store import EngineStore
from iatest.mobile.store import MobileStore
from iatest.web.server import make_handler
from iatest.web.task_store import list_tasks, request_cancel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="completion-console")
    parser.add_argument("--browser", required=True)
    parser.add_argument("--node-modules", required=True)
    parser.add_argument("--desktop-driver", default="")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--evidence",
        type=Path,
        action="append",
        default=[],
        help="Saved acceptance JSON containing a plan; native profiles must already exist in this project",
    )
    args = parser.parse_args()
    root = Path.cwd()
    init_project(root, args.project)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    database_directory = tempfile.TemporaryDirectory(
        prefix=".iatest-console-database-", dir=root
    )
    database = Path(database_directory.name) / "samples.sqlite"
    if args.desktop_driver:
        os.environ["IATEST_DESKTOP_MACOS_DRIVER"] = str(
            Path(args.desktop_driver).resolve(strict=True)
        )
    with sqlite3.connect(database) as db:
        db.execute("create table if not exists samples(id integer primary key)")
        db.executemany("insert or ignore into samples values(?)", [(1,), (2,)])
    plans = [
        {
            "id": "console-readonly-database",
            "kind": "data",
            "spec": {
                "database": {
                    "driver": "sqlite",
                    "path_env": "IATEST_CONSOLE_DATABASE",
                    "query": "select id from samples",
                    "allowed_tables": ["samples"],
                },
                "checks": [{"id": "unique", "rule": "unique", "path": "$.id"}],
            },
        }
    ]
    plans.extend(json.loads(path.read_text())["plan"] for path in args.evidence)
    os.environ["IATEST_CONSOLE_DATABASE"] = str(database)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(root, args.project))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        subprocess.run(
            ["node", str(Path(__file__).with_suffix(".cjs"))],
            env={
                **os.environ,
                "NODE_PATH": args.node_modules,
                "IATEST_CHROMIUM_PATH": args.browser,
                "IATEST_SMOKE_URL": f"http://127.0.0.1:{server.server_port}",
                "IATEST_CONSOLE_PROJECT": args.project,
                "IATEST_CONSOLE_PLANS": json.dumps(plans),
                "IATEST_CONSOLE_REPORT": str(args.output.resolve()),
            },
            check=True,
            timeout=600,
        )
        store = EngineStore(root, args.project)
        report = json.loads(args.output.read_text())
        report["runs"] = [
            next(
                r
                for r in (
                    MobileStore(root, args.project).runs()
                    if p["kind"] in {"ios", "android"}
                    else store.runs()
                )
                if r["plan_id"] == p["id"]
            )
            for p in plans
        ]
        assert all(r["verdict"] == "passed" for r in report["runs"])
        report["protobuf_assets"] = len(store.artifacts("protobuf"))
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(
            json.dumps({"passed": True, "runs": len(plans), "output": str(args.output)})
        )
    finally:
        deadline = time.monotonic() + 30
        while True:
            active = [
                task
                for task in list_tasks(root, args.project)
                if task["status"] in {"queued", "running"}
            ]
            for task in active:
                request_cancel(root, args.project, task["id"])
            if not active or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        server.shutdown()
        server.server_close()
        thread.join(2)
        database_directory.cleanup()
        if active:
            raise RuntimeError("Console task cleanup remains unconfirmed")


if __name__ == "__main__":
    main()
