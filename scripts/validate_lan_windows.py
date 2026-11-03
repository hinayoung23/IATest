"""Real Mac/Windows LAN acceptance with an existing authorized SSH key.

Deploys the packaged Windows bootstrap into a new TEMP workspace. SSH transfers
files and controls process lifetime; framework traffic uses direct LAN TLS.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from iatest import __version__
from iatest.application.testing_service import execute_test_plan
from iatest.core.config import init_project
from iatest.engines.transactions import percentile
from iatest.nodes.protocol import make_controller
from iatest.nodes.store import ControllerStore, engine_fingerprint
from iatest.nodes.windows_bundle import build_bundle

REMOTE_LAUNCHER = r"""
import json, os, subprocess, sys, threading
from pathlib import Path
config = json.loads(sys.stdin.readline())
environment = dict(os.environ)
environment[config["token_env"]] = config.pop("token")
if config.get("wheelhouse"):
    environment["PIP_NO_INDEX"] = "1"
    environment["PIP_FIND_LINKS"] = config["wheelhouse"]
bundle = Path(__file__).parent / "bundle"
process = subprocess.Popen(
    [sys.executable, str(bundle / "start-node.py"),
     "--workspace", str(Path(__file__).parent / "worker")],
    cwd=bundle, env=environment, stdin=subprocess.DEVNULL,
)
del environment
def commands():
    for line in sys.stdin:
        if json.loads(line).get("action") == "stop":
            break
    if process.poll() is None:
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
threading.Thread(target=commands, daemon=True).start()
raise SystemExit(process.wait())
"""


def wait_for(check, seconds, label):
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() >= deadline:
            raise RuntimeError("Timed out waiting for " + label)
        time.sleep(0.1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--identity", required=True, type=Path)
    parser.add_argument("--known-hosts", type=Path)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument(
        "--wheelhouse",
        type=Path,
        help="Windows-compatible dependency wheels for offline installation",
    )
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    wheel = args.wheel or repository / f"dist/iatest_framework-{__version__}-py3-none-any.whl"
    workspace = Path(tempfile.mkdtemp(prefix="iatest-lan-acceptance-"))
    os.chmod(workspace, 0o700)
    report = {
        "passed": False,
        "target": args.host,
        "local_workspace": str(workspace),
        "physical_device_actions": 0,
        "new_production_dependencies": 0,
        "ssh_authentication": "existing_authorized_key",
        "ssh_forwarding_used": False,
        "new_ssh_authorization_added": False,
    }
    options = [
        "-i",
        str(args.identity),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=8",
    ]
    if args.known_hosts:
        options += ["-o", "UserKnownHostsFile=" + str(args.known_hosts)]
    destination = args.user + "@" + args.host
    ssh = ["ssh", "-T", *options, destination]

    def checkpoint(stage):
        report["stage"] = stage
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(stage, flush=True)

    def remote(arguments, timeout=45):
        arguments = [str(x) for x in arguments]
        if len(arguments) >= 3 and arguments[1] == "-c" and "\n" in arguments[2]:
            # Windows' default SSH shell is cmd.exe; keep the command on one line.
            arguments[2] = "exec(" + repr(arguments[2]) + ")"
        result = subprocess.run(
            [*ssh, subprocess.list2cmdline(arguments)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        if result.returncode:
            raise RuntimeError("Remote command failed: " + result.stderr[-1000:])
        return result.stdout.strip()

    servers, workers, streams, token_names = [], [], [], []
    controller = None
    try:
        facts = json.loads(
            remote(
                [
                    "python",
                    "-c",
                    (
                        "import sys,platform,tempfile,json; print(json.dumps({"
                        "'platform':platform.system(),'python':sys.version.split()[0],"
                        "'architecture':platform.machine(),'temp':tempfile.gettempdir()}))"
                    ),
                ]
            )
        )
        assert facts["platform"] == "Windows", facts
        report["windows_host"] = facts
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
            route.connect((args.host, 9))
            local_ip = route.getsockname()[0]
        report["local_ip"] = local_ip
        checkpoint("SSH authenticated; real Windows/Python confirmed")

        def certificate(name):
            cert, key = workspace / (name + ".pem"), workspace / (name + ".key")
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-days",
                    "1",
                    "-subj",
                    "/CN=iatest-lan-acceptance",
                    "-addext",
                    "subjectAltName=IP:" + local_ip,
                    "-keyout",
                    str(key),
                    "-out",
                    str(cert),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            os.chmod(key, 0o600)
            return cert, key

        cert, key = certificate("controller")
        wrong_ca, _ = certificate("untrusted")
        root, project = workspace / "controller", "lan-acceptance"
        init_project(root, project)
        hits, hit_lock = [], threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                with hit_lock:
                    hits.append(
                        {
                            "path": self.path,
                            "client": self.client_address[0],
                            "time": time.monotonic(),
                            "port": self.server.server_port,
                        }
                    )
                data = b'{"ok":true,"nickname":"lan-fixture"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.do_GET()

        def serve(server):
            servers.append(server)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            return server

        lab = serve(ThreadingHTTPServer((local_ip, 0), Handler))
        forbidden = serve(ThreadingHTTPServer((local_ip, 0), Handler))
        base = f"http://{local_ip}:{lab.server_port}"
        forbidden_base = f"http://{local_ip}:{forbidden.server_port}"
        controller = serve(
            make_controller(root, project, local_ip, 0, certfile=cert, keyfile=key)
        )
        controller_port = controller.server_port
        controller_url = f"https://{local_ip}:{controller_port}"
        report["controller"] = controller_url
        report["authorized_test_origin"] = base
        store = ControllerStore(root, project)
        for name in ("mac", "windows"):
            token_env = "IATEST_LAN_ACCEPTANCE_" + name.upper()
            if token_env in os.environ:
                raise RuntimeError("Acceptance token variable is already in use")
            token_names.append(token_env)
            os.environ[token_env] = os.urandom(32).hex()
            store.enroll(
                {
                    "id": name,
                    "token_env": token_env,
                    "allowed_origins": [base],
                    "allowed_kinds": ["api", "performance", "client_performance"],
                }
            )

        bundle = workspace / "windows-node.zip"
        build_bundle(
            bundle,
            wheel,
            cert,
            controller=controller_url,
            allowed_origins=[base],
            node_id="windows",
            token_env=token_names[1],
            project=project,
            allowed_kinds=["api", "performance", "client_performance"],
        )
        launcher = workspace / "remote-launcher.py"
        launcher.write_text(REMOTE_LAUNCHER, encoding="utf-8")
        transfers = [bundle, launcher, wrong_ca]
        if args.wheelhouse:
            import zipfile

            dependency_archive = workspace / "wheelhouse.zip"
            dependency_wheels = sorted(args.wheelhouse.glob("*.whl"))
            if not dependency_wheels:
                raise ValueError("Wheelhouse contains no dependency wheels")
            with zipfile.ZipFile(
                dependency_archive, "x", zipfile.ZIP_DEFLATED
            ) as archive:
                for dependency in dependency_wheels:
                    archive.write(dependency, dependency.name)
            transfers.append(dependency_archive)
            report["offline_dependency_wheels"] = len(dependency_wheels)
        remote_root = remote(
            [
                "python",
                "-c",
                (
                    "import tempfile; print(tempfile.mkdtemp(prefix='iatest-lan-acceptance-'))"
                ),
            ]
        ).replace("\\", "/")
        report["windows_workspace"] = remote_root
        copied = subprocess.run(
            [
                "scp",
                *options,
                *map(str, transfers),
                destination + ":" + remote_root + "/",
            ],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        assert copied.returncode == 0, "SFTP transfer failed"
        expected = hashlib.sha256(bundle.read_bytes()).hexdigest()
        report["deployment_bundle_sha256"] = expected
        report["tested_wheel_sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
        actual = remote(
            [
                "python",
                "-c",
                (
                    "import hashlib,pathlib; print(hashlib.sha256(pathlib.Path("
                    + repr(remote_root + "/windows-node.zip")
                    + ").read_bytes()).hexdigest())"
                ),
            ]
        )
        assert actual == expected
        remote(
            [
                "python",
                "-c",
                (
                    "import zipfile; zipfile.ZipFile("
                    + repr(remote_root + "/windows-node.zip")
                    + ").extractall("
                    + repr(remote_root + "/bundle")
                    + ")"
                ),
            ]
        )
        if args.wheelhouse:
            remote(
                [
                    "python",
                    "-c",
                    "import zipfile; zipfile.ZipFile("
                    + repr(remote_root + "/wheelhouse.zip")
                    + ").extractall("
                    + repr(remote_root + "/wheelhouse")
                    + ")",
                ]
            )
        checkpoint("Public CA/wheel bundle transferred and SHA256 verified")

        log = (workspace / "windows-bootstrap.log").open("w")
        streams.append(log)
        windows = subprocess.Popen(
            [
                *ssh,
                subprocess.list2cmdline(
                    ["python", remote_root + "/remote-launcher.py"]
                ),
            ],
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        workers.append(windows)
        windows.stdin.write(
            json.dumps(
                {
                    "token_env": token_names[1],
                    "token": os.environ[token_names[1]],
                    "wheelhouse": remote_root + "/wheelhouse"
                    if args.wheelhouse
                    else "",
                }
            )
            + "\n"
        )
        windows.stdin.flush()

        mac_root = workspace / "mac"
        init_project(mac_root, project)
        mac_log = (workspace / "mac-worker.log").open("w")
        streams.append(mac_log)
        mac = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "iatest",
                "node",
                "worker",
                controller_url,
                "mac",
                "--project",
                project,
                "--token-env",
                token_names[0],
                "--origin",
                base,
                "--kinds",
                "api,performance,client_performance",
                "--ca",
                str(cert),
            ],
            cwd=mac_root,
            env={**os.environ, "PYTHONPATH": str(repository)},
            stdout=mac_log,
            stderr=subprocess.STDOUT,
        )
        workers.append(mac)

        def registered():
            if windows.poll() is not None or mac.poll() is not None:
                raise RuntimeError("A worker exited; inspect isolated bootstrap logs")
            return len(store.nodes()) == 2 and all(n["online"] for n in store.nodes())

        wait_for(registered, 600, "Windows bootstrap and both node registrations")
        nodes = {n["id"]: n["capabilities"] for n in store.nodes()}
        assert nodes["windows"]["host_platform"] == "Windows"
        assert nodes["mac"]["host_platform"] == "Darwin"
        assert nodes["windows"]["host_id"] != nodes["mac"]["host_id"]
        assert all(
            n["engine_fingerprint"] == engine_fingerprint() for n in nodes.values()
        )
        report["registered_nodes"] = nodes
        report["windows_python_bootstrap"] = "passed"
        report["powershell_bootstrap_used"] = False
        report["execution_policy_changed"] = False
        checkpoint("Python bundle started real Mac/Windows TLS workers")

        python = remote_root + "/worker/.venv/Scripts/python.exe"
        trust = json.loads(
            remote(
                [
                    python,
                    "-c",
                    (
                        "import httpx,json; url="
                        + repr(controller_url)
                        + "; result={}; "
                        "client=httpx.Client(trust_env=False,verify="
                        + repr(remote_root + "/bundle/controller-ca.pem")
                        + "); "
                        "result['unauthorized_status']=client.post(url+'/v1/poll',json={}).status_code; "
                        "client.close();\ntry:\n "
                        "httpx.get(url,verify="
                        + repr(remote_root + "/untrusted.pem")
                        + ",trust_env=False,timeout=3); "
                        "result['wrong_ca_rejected']=False\nexcept httpx.ConnectError as e:\n "
                        "result['wrong_ca_rejected']='CERTIFICATE_VERIFY_FAILED' in str(e)\n"
                        "print(json.dumps(result))"
                    ),
                ]
            )
        )
        assert trust == {"unauthorized_status": 401, "wrong_ca_rejected": True}, trust
        report["transport_guards"] = trust

        api = {
            "id": "windows-api",
            "kind": "api",
            "spec": {
                "allowed_origins": [base],
                "requests": [{"url": base + "/api"}],
                "exports": {"ok": {"path": "$.ok", "type": "boolean"}},
            },
        }
        result = execute_test_plan(
            root,
            project,
            {
                "id": "remote-api",
                "kind": "remote",
                "spec": {"node_id": "windows", "plan": api},
            },
        )
        assert result["verdict"] == "passed", result
        assert result["outputs"]["ok"]["value"] is True
        assert any(h["client"] == args.host and h["path"] == "/api" for h in hits)
        report["remote_api"] = {
            "verdict": result["verdict"],
            "run_id": result["run_id"],
            "observed_client_ip": args.host,
            "typed_export": True,
        }

        # The next API/load runs exercise the real Windows Job + native subprocess.
        remote(
            [
                python,
                "-c",
                "from pathlib import Path; from iatest.core.config import load_config,save_config; r=Path("
                + repr(remote_root + "/worker")
                + "); c=load_config(r); c['sandbox']['mode']='process'; save_config(r,c)",
            ]
        )
        isolated_api = execute_test_plan(
            root,
            project,
            {
                "id": "windows-process-api",
                "kind": "remote",
                "spec": {"node_id": "windows", "plan": api},
            },
        )
        assert isolated_api["verdict"] == "passed", isolated_api
        isolation = isolated_api["steps"][0]["result"]["metrics"]["isolation"]
        assert (
            isolation["mode"] == "process"
            and isolation["worker_pid"] != isolation["controller_pid"]
        )
        report["windows_process_isolation"] = isolation

        from iatest.application.ios_measurements import import_measurement

        source = root / "normalized-measurement.json"
        source.write_text(
            json.dumps(
                {
                    "contract_version": 1,
                    "platform": "ios",
                    "app_id": "com.example.fixture",
                    "source": {
                        "provider": "custom",
                        "test_id": "controlled-offline-measurement",
                    },
                    "measurements": [
                        {"metric": "ios_launch_ms", "unit": "ms", "values": [100, 120]}
                    ],
                }
            ),
            encoding="utf-8",
        )
        asset = import_measurement(
            root,
            project,
            {
                "id": "lan-normalized-metrics",
                "format": "measurements_json",
                "path": str(source),
            },
        )
        measured = execute_test_plan(
            root,
            project,
            {
                "id": "windows-normalized-asset",
                "kind": "remote",
                "spec": {
                    "node_id": "windows",
                    "plan": {
                        "id": "offline-asset",
                        "kind": "client_performance",
                        "spec": {
                            "platform": "ios",
                            "measurement": {"artifact_id": asset["id"]},
                            "thresholds": {"max_ios_launch_ms": 200},
                        },
                    },
                },
            },
        )
        assert measured["verdict"] == "passed", measured
        report["normalized_asset_transfer"] = {
            "verdict": measured["verdict"],
            "source_digest": asset["source_digest"],
            "physical_device_actions": 0,
        }
        api["spec"]["allowed_origins"] = [forbidden_base]
        api["spec"]["requests"][0]["url"] = forbidden_base + "/forbidden"
        rejected = execute_test_plan(
            root,
            project,
            {
                "id": "reject-scope",
                "kind": "remote",
                "spec": {"node_id": "windows", "plan": api},
            },
        )
        assert rejected["verdict"] == "inconclusive", rejected
        assert not any(h["port"] == forbidden.server_port for h in hits)
        report["scope_guard"] = {
            "verdict": rejected["verdict"],
            "forbidden_target_hits": 0,
        }

        plan = {
            "id": "real-lan-transactions",
            "kind": "performance",
            "budget": {"timeout_seconds": 25, "max_requests": 100},
            "spec": {
                "allowed_origins": [base],
                "mode": "arrival_rate",
                "rate": 10,
                "rate_unit": "transactions",
                "users": 2,
                "duration_seconds": 2,
                "scenarios": [
                    {
                        "id": "read-verify",
                        "steps": [
                            {
                                "id": "read",
                                "request": {
                                    "url": base
                                    + "/transactions?namespace={{namespace}}&user={{user}}"
                                },
                                "extract": {"nickname": "$.nickname"},
                            },
                            {
                                "id": "verify",
                                "request": {
                                    "url": base
                                    + "/transactions?namespace={{namespace}}&user={{user}}"
                                },
                                "assertions": [
                                    {
                                        "path": "$.nickname",
                                        "operator": "eq",
                                        "expected": "{{nickname}}",
                                    }
                                ],
                            },
                        ],
                    }
                ],
                "thresholds": {
                    "max_error_ratio": 0,
                    "max_transaction_error_ratio": 0,
                    "max_dropped_ratio": 0,
                },
                "distribution": {"nodes": ["mac", "windows"]},
            },
        }
        result = execute_test_plan(root, project, plan)
        assert result["verdict"] == "passed", result
        metrics = result["metrics"]
        assert metrics["transactions"] == 20 and metrics["completed"] == 40, metrics
        assert metrics["coverage_complete"] is True
        samples, allocations = [], []
        for node in metrics["node_results"]:
            job = store.job(node["job_id"])
            samples.extend(store.samples(job["id"]))
            allocations.append(
                {
                    "node": job["node_id"],
                    "users": job["plan"]["spec"]["users"],
                    "rate": job["plan"]["spec"]["rate"],
                    "requests_budget": job["plan"]["budget"]["max_requests"],
                    "transactions": node["metrics"]["transactions"],
                    "requests": node["metrics"]["completed"],
                }
            )
        assert sorted(a["transactions"] for a in allocations) == [10, 10]
        assert sum(a["requests_budget"] for a in allocations) == 100
        assert metrics["p95_ms"] == percentile(
            [s["duration_ms"] for s in samples if s["kind"] == "request"], 0.95
        )
        requests = [h for h in hits if urlsplit(h["path"]).path == "/transactions"]
        namespaces = {
            parse_qs(urlsplit(h["path"]).query)["namespace"][0] for h in requests
        }
        users = {parse_qs(urlsplit(h["path"]).query)["user"][0] for h in requests}
        assert len(namespaces) == 2 and users == {"0", "1"}
        assert {h["client"] for h in requests} == {local_ip, args.host}
        report["distributed_performance"] = {
            "verdict": result["verdict"],
            "run_id": result["run_id"],
            "transactions": metrics["transactions"],
            "requests": metrics["completed"],
            "p95_ms": metrics["p95_ms"],
            "p99_ms": metrics["p99_ms"],
            "global_raw_sample_percentile_verified": True,
            "coverage_complete": True,
            "unique_namespaces": len(namespaces),
            "global_user_ids": sorted(users),
            "allocations": allocations,
            "observed_client_ips": sorted({h["client"] for h in requests}),
        }
        checkpoint(
            "Windows API/scope/TLS guards and global 20-transaction/40-request run passed"
        )

        lease = {
            "id": "windows-lease-loss",
            "kind": "performance",
            "budget": {"timeout_seconds": 25, "max_requests": 400},
            "spec": {
                "allowed_origins": [base],
                "mode": "arrival_rate",
                "rate": 10,
                "users": 1,
                "duration_seconds": 20,
                "request": {
                    "url": base + "/lease",
                    "method": "POST",
                    "json": {"controlled": True},
                },
                "thresholds": {"max_error_ratio": 0, "max_dropped_ratio": 0},
                "distribution": {"nodes": ["windows"]},
            },
        }

        def lease_hits():
            with hit_lock:
                return [h for h in hits if h["path"] == "/lease"]

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(execute_test_plan, root, project, lease)
            wait_for(
                lambda: len(lease_hits()) >= 4 or future.done(),
                12,
                "Windows load start",
            )
            assert not future.done(), future.result()
            disconnected = time.monotonic()
            controller.shutdown()
            controller.server_close()
            servers.remove(controller)
            controller = None
            time.sleep(9)
            count = len(lease_hits())
            time.sleep(1)
            assert len(lease_hits()) == count and 4 <= count < 150
            lease_result = future.result(timeout=8)
        assert lease_result["verdict"] == "inconclusive", lease_result
        controller = serve(
            make_controller(
                root, project, local_ip, controller_port, certfile=cert, keyfile=key
            )
        )
        wait_for(
            lambda: all(n["online"] for n in store.nodes()), 12, "transport recovery"
        )
        time.sleep(2)
        assert len(lease_hits()) == count, (
            "Unknown POST workload was replayed after recovery"
        )
        job_id = lease_result["metrics"]["node_results"][0]["job_id"]
        journal = json.loads(
            remote(
                [
                    python,
                    "-c",
                    (
                        "from pathlib import Path; import json; from iatest.engines.store import EngineStore; "
                        "a=EngineStore(Path("
                        + repr(remote_root + "/worker")
                        + "),"
                        + repr(project)
                        + ").get_artifact('node_attempt',"
                        + repr(job_id)
                        + "); "
                        "print(json.dumps({'state':a['state']}))"
                    ),
                ]
            )
        )
        report["lease_loss"] = {
            "fault": "actual TLS controller listener shutdown; HTTP target stayed reachable",
            "lease_seconds": 5,
            "observed_requests": count,
            "last_request_seconds_after_disconnect": round(
                max(h["time"] for h in lease_hits()) - disconnected, 3
            ),
            "stopped_before_full_window": True,
            "stable_after_transport_recovery": True,
            "verdict": lease_result["verdict"],
            "run_id": lease_result["run_id"],
            "worker_journal": journal,
        }
        report["passed"] = True
        checkpoint(
            "Real LAN acceptance passed, including lease loss/recovery without POST replay"
        )
    except BaseException as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)[:2000]
        checkpoint("Acceptance failed; partial evidence retained")
        raise
    finally:
        for process in workers:
            if process.stdin and process.poll() is None:
                try:
                    process.stdin.write(json.dumps({"action": "stop"}) + "\n")
                    process.stdin.flush()
                    process.stdin.close()
                    process.wait(timeout=15)
                except (OSError, subprocess.TimeoutExpired):
                    process.terminate()
            elif process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for stream in streams:
            stream.close()
        for server in servers:
            server.shutdown()
            server.server_close()
        for name in token_names:
            os.environ.pop(name, None)
        report["local_worker_transports_stopped"] = all(
            p.poll() is not None for p in workers
        )
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
