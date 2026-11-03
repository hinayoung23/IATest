"""Real native-worker and persistent-browser acceptance in an explicit Docker engine.

Build a framework image first; --browser also requires Chromium in that image.
TMPDIR must be within a directory shared with a remote/VM-backed Docker engine.
Only the named temporary fixture container and network created here are removed.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from iatest.application.testing_service import execute_test_plan
from iatest.core.config import init_project, load_config, save_config
from iatest.core.models import Assertion, TestCase
from iatest.core.paths import project_docs_file
from iatest.core.serde import write_json
from iatest.engines.store import EngineStore
from iatest.engines.web import BrowserSession
from iatest.execution.control import RunControl
from iatest.execution.runner import run_cases
from iatest.generator.cases import save_cases

SERVER = r"""
import json, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
calls=[]
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        if self.path == '/stats':
            body=json.dumps(calls).encode(); kind='application/json'
        else:
            calls.append(self.path)
            if self.path == '/slow': time.sleep(15)
            body=b'<html><head><title>Container Lab</title></head><body><input id="field"><button id="save" onclick="document.querySelector(\'#status\').textContent=document.querySelector(\'#field\').value">Save</button><p id="status">Ready</p></body></html>'
            kind='text/html'
        self.send_response(200); self.send_header('Content-Type',kind)
        self.send_header('Content-Length',str(len(body))); self.end_headers()
        try: self.wfile.write(body)
        except OSError: pass
ThreadingHTTPServer(('0.0.0.0',8000),Handler).serve_forever()
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--docker-context")
    parser.add_argument("--browser", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.docker_context:
        os.environ.pop("DOCKER_HOST", None)
        os.environ["DOCKER_CONTEXT"] = args.docker_context
    report = {"passed": False, "checks": {}, "results": {}}
    suffix = uuid.uuid4().hex[:12]
    network, fixture = "iatest-test-" + suffix, "iatest-fixture-" + suffix
    created_network = created_fixture = False

    def docker(*values, check=True):
        result = subprocess.run(
            ["docker", *values], capture_output=True, text=True, timeout=30, check=False
        )
        if check and result.returncode:
            raise RuntimeError("Docker command failed: " + result.stderr[-1000:])
        return result

    def workers():
        rows = docker(
            "ps",
            "-aq",
            "--filter",
            "name=iatest-native-",
            "--filter",
            "name=iatest-browser-",
        ).stdout
        return set(rows.split())

    initial_workers = workers()
    try:
        docker("image", "inspect", args.image)
        docker(
            "network",
            "create",
            "--internal",
            "--label",
            "iatest.acceptance=" + suffix,
            network,
        )
        created_network = True
        docker(
            "run",
            "-d",
            "--rm",
            "--name",
            fixture,
            "--label",
            "iatest.acceptance=" + suffix,
            "--network",
            network,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--entrypoint",
            "python",
            args.image,
            "-B",
            "-c",
            SERVER,
        )
        created_fixture = True

        def calls():
            value = docker(
                "exec",
                fixture,
                "python",
                "-c",
                "import urllib.request;c=urllib.request.build_opener(urllib.request.ProxyHandler({}));print(c.open('http://127.0.0.1:8000/stats',timeout=2).read().decode())",
            )
            return json.loads(value.stdout)

        end = time.monotonic() + 10
        while True:
            try:
                calls()
                break
            except (RuntimeError, ValueError):
                if time.monotonic() > end:
                    raise
                time.sleep(0.1)
        base = "http://" + fixture + ":8000"
        with tempfile.TemporaryDirectory(
            prefix="iatest-container-acceptance-"
        ) as temporary:
            root = Path(temporary)
            init_project(root, "suite")
            config = load_config(root)
            config["sandbox"].update(
                mode="container",
                container_image=args.image,
                container_network="none",
                memory_mb=1024,
                pids_limit=256,
                termination_grace_seconds=0.5,
            )
            save_config(root, config)

            def run(plan, control=None):
                result = execute_test_plan(root, "suite", plan, control)
                report["results"][plan["id"]] = result
                return result

            data = {
                "id": "records-none",
                "kind": "data",
                "spec": {
                    "records": [{"id": 1}, {"id": 2}],
                    "checks": [{"id": "two", "rule": "count", "min": 2, "max": 2}],
                },
            }
            result = run(data)
            report["checks"]["native_data_network_none"] = (
                result["verdict"] == "passed"
                and result["metrics"]["isolation"]["mode"] == "container"
            )
            # Business input must not live below protected .iatest/config paths.
            # Only the copied worker inputs need the Docker VM's shared TMPDIR.
            source_directory = tempfile.TemporaryDirectory(
                prefix="iatest-container-source-",
                dir="/tmp" if os.name == "posix" else None,
            )
            database = Path(source_directory.name) / "source.sqlite"
            writer = sqlite3.connect(database)
            previous = os.environ.get("IATEST_CONTAINER_DATABASE")
            os.environ["IATEST_CONTAINER_DATABASE"] = str(database)
            try:
                writer.execute("pragma journal_mode=WAL")
                writer.execute("create table records(id integer)")
                writer.executemany("insert into records values(?)", [(1,), (2,)])
                writer.commit()
                db_plan = copy.deepcopy(data)
                db_plan["id"] = "sqlite-wal-snapshot"
                db_plan["spec"].pop("records")
                db_plan["spec"]["database"] = {
                    "driver": "sqlite",
                    "path_env": "IATEST_CONTAINER_DATABASE",
                    "query": "select id from records",
                    "allowed_tables": ["records"],
                }
                config["sandbox"]["read_paths"] = [str(database)]
                save_config(root, config)
                result = run(db_plan)
                report["checks"]["committed_wal_snapshot"] = (
                    result["verdict"] == "passed"
                    and writer.execute("select count(*) from records").fetchone()[0]
                    == 2
                )
            finally:
                writer.close()
                source_directory.cleanup()
                if previous is None:
                    os.environ.pop("IATEST_CONTAINER_DATABASE", None)
                else:
                    os.environ["IATEST_CONTAINER_DATABASE"] = previous
            api = {
                "id": "api-none",
                "kind": "api",
                "spec": {"allowed_origins": [base], "requests": [{"url": base}]},
            }
            result = run(api)
            report["checks"]["network_none_blocks_fixture"] = (
                result["verdict"] == "inconclusive" and not calls()
            )
            config["sandbox"]["container_network"] = network
            save_config(root, config)
            api["id"] = "api-internal"
            result = run(api)
            report["checks"]["internal_network_api"] = result[
                "verdict"
            ] == "passed" and calls() == ["/"]
            config["environments"]["local"]["base_url"] = base
            config["execution"]["parallel"] = 1
            save_config(root, config)
            write_json(project_docs_file(root, "suite"), [])
            save_cases(
                root,
                [
                    TestCase(
                        "container-get",
                        "Container GET",
                        "GET",
                        "/",
                        assertions=[Assertion("status_code", expected=200)],
                    )
                ],
                project="suite",
            )
            legacy = run_cases(root, project="suite")
            report["legacy_cases"] = [
                {"status": r.status, "error": r.error} for r in legacy
            ]
            report["checks"]["markdown_case_container"] = (
                len(legacy) == 1
                and legacy[0].status == "passed"
                and calls() == ["/", "/"]
            )
            baseline = calls()
            api["id"] = "origin-refused"
            api["spec"]["allowed_origins"] = ["http://example.invalid"]
            result = run(api)
            report["checks"]["origin_denial_before_dispatch"] = (
                result["verdict"] == "inconclusive" and calls() == baseline
            )
            api["spec"]["allowed_origins"] = [base]
            api["id"] = "cancel-native"
            api["spec"]["requests"] = [{"url": base + "/slow"}]
            control = RunControl(timeout_seconds=30)
            rows, errors = [], []

            def execute():
                try:
                    rows.append(run(api, control))
                except Exception as exc:  # noqa: BLE001 -- Surface background acceptance errors.
                    errors.append(type(exc).__name__)

            runner = threading.Thread(target=execute)
            runner.start()
            try:
                end = time.monotonic() + 10
                while "/slow" not in calls():
                    if time.monotonic() > end:
                        raise TimeoutError(
                            "Native worker did not reach the owned fixture"
                        )
                    time.sleep(0.1)
                active = workers() - initial_workers
                if len(active) != 1:
                    raise AssertionError("Cannot identify the owned active worker")
                info = json.loads(docker("inspect", next(iter(active))).stdout)[0]
                host = info["HostConfig"]
                report["restrictions"] = {
                    "read_only": host["ReadonlyRootfs"],
                    "cap_drop": host["CapDrop"],
                    "security_opt": host["SecurityOpt"],
                    "network": host["NetworkMode"],
                    "memory": host["Memory"],
                    "pids": host["PidsLimit"],
                    "user": info["Config"]["User"],
                    "mounts": [
                        {"destination": m["Destination"], "rw": m["RW"]}
                        for m in info["Mounts"]
                    ],
                }
                report["checks"]["container_restrictions"] = (
                    host["ReadonlyRootfs"]
                    and "ALL" in host["CapDrop"]
                    and "no-new-privileges=true" in host["SecurityOpt"]
                    and host["Memory"] == 1024 * 1024 * 1024
                    and host["PidsLimit"] == 256
                    and info["Config"]["User"].split(":")[0] != "0"
                    and all(
                        m["Destination"] == "/input" and not m["RW"]
                        for m in info["Mounts"]
                    )
                )
            finally:
                control.cancelled.set()
                runner.join(10)
            report["checks"]["native_cancel_reaped"] = (
                not runner.is_alive()
                and not errors
                and bool(rows)
                and rows[0]["lifecycle"] == "cancelled"
                and rows[0]["verdict"] == "inconclusive"
                and workers() == initial_workers
            )
            if args.browser:
                web = {
                    "id": "web-evidence",
                    "kind": "web",
                    "spec": {
                        "start_url": base,
                        "allowed_origins": [base],
                        "steps": [
                            {
                                "op": "fill",
                                "target": {"id": "field"},
                                "value": "Container",
                            },
                            {"op": "click", "target": {"id": "save"}},
                            {
                                "op": "expect",
                                "target": {"id": "status"},
                                "assert": "text",
                                "expected": "Container",
                            },
                            {"op": "screenshot", "target": {"id": "status"}},
                        ],
                    },
                }
                result = run(web)
                evidence = result.get("evidence", [])
                report["checks"]["native_web_and_png_evidence"] = (
                    result["verdict"] == "passed"
                    and bool(evidence)
                    and EngineStore(root, "suite")
                    .evidence_path(result["run_id"], evidence[0]["name"])
                    .read_bytes()
                    .startswith(b"\x89PNG")
                )
                control = RunControl(timeout_seconds=40)
                browser = BrowserSession(
                    root,
                    "suite",
                    {"start_url": base, "allowed_origins": [base]},
                    control,
                )
                try:
                    browser.act(
                        {
                            "op": "fill",
                            "target": {"id": "field"},
                            "value": "Persistent RPC",
                        }
                    )
                    browser.act({"op": "click", "target": {"id": "save"}})
                    report["checks"]["persistent_browser_rpc"] = browser.assertion(
                        {
                            "assert": "text",
                            "target": {"id": "status"},
                            "expected": "Persistent RPC",
                        }
                    )[0]
                    try:
                        browser.evaluate("document.cookie")
                    except ValueError:
                        report["checks"]["rpc_arbitrary_script_refused"] = True
                    else:
                        report["checks"]["rpc_arbitrary_script_refused"] = False
                    control.cancelled.set()
                    try:
                        browser.query({"id": "status"})
                    except RuntimeError:
                        report["checks"]["rpc_cancel_refused"] = True
                finally:
                    browser.close()
                report["checks"]["rpc_container_reaped"] = workers() == initial_workers
        report["passed"] = all(report["checks"].values())
    except Exception as exc:  # noqa: BLE001 -- Preserve actual environment failures and cleanup evidence.
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if created_fixture:
            docker("rm", "-f", fixture, check=False)
        if created_network:
            docker("network", "rm", network, check=False)
        report["checks"]["owned_fixture_removed"] = (
            docker("container", "inspect", fixture, check=False).returncode != 0
            and docker("network", "inspect", network, check=False).returncode != 0
        )
        report["checks"]["workers_reaped"] = workers() == initial_workers
        report["passed"] = report["passed"] and all(report["checks"].values())
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.output.exists():
            args.output.with_name(
                args.output.stem
                + "-attempt-"
                + str(time.time_ns())
                + args.output.suffix
            ).write_bytes(args.output.read_bytes())
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({k: v for k, v in report.items() if k != "results"}))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
