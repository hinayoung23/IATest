"""Persistent bounded browser RPC; models and project secrets remain in the parent."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from iatest.core.config import load_config
from iatest.execution.isolation import (
    container_command,
    stop_worker,
    worker_environment,
)

METHODS = {
    "outline",
    "query",
    "act",
    "assertion",
    "navigate",
    "page_action",
    "scroll",
    "visual_region",
    "visual_click",
    "upload",
    "download",
    "root_point",
    "root_viewport",
    "select_context",
    "evaluate",
}


def encode(value):
    if isinstance(value, bytes):
        return {
            "_binary": base64.b64encode(value).decode(),
            "_sha256": hashlib.sha256(value).hexdigest(),
        }
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    return value


def decode(value):
    if isinstance(value, dict) and set(value) == {"_binary", "_sha256"}:
        raw = base64.b64decode(value["_binary"], validate=True)
        if len(raw) > 8388608 or hashlib.sha256(raw).hexdigest() != value["_sha256"]:
            raise ValueError(
                "Browser evidence transfer exceeds bounds or digest differs"
            )
        return raw
    if isinstance(value, dict):
        return {k: decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode(v) for v in value]
    return value


class RPCBrowser:
    def __init__(self, root, project, spec, control, *, max_queries=500):
        from iatest.core.sandbox import SandboxPolicy
        from iatest.engines.web import chromium_path

        self.root, self.project, self.control = root, project, control
        config = load_config(root)
        settings = config["sandbox"]
        self.mode = settings["mode"]
        self.policy = SandboxPolicy.from_config(root, config, project=project)
        self.temp = tempfile.TemporaryDirectory(prefix="iatest-browser-rpc-")
        self.work = Path(self.temp.name)
        self.inputs, self.outputs = self.work / "input", self.work / "output"
        self.inputs.mkdir(mode=0o700)
        self.outputs.mkdir(mode=0o700)
        (self.inputs / "files").mkdir()
        self.process = None
        self.container_name = None
        self.closed = False
        self.sequence = 0
        self.pending = False
        self.queries = 0
        self.page_alias = "main"
        self.action_charge = lambda count: None
        self.responses = queue.Queue(maxsize=4)
        self.bytes = 0
        self.maximum = int(settings.get("max_output_bytes", 64000000))
        self.maximum = min(self.maximum, 64000000)
        env = worker_environment({**settings, "env_allowlist": []}, self.work)
        executable = chromium_path()
        if executable and self.mode == "process":
            env["IATEST_CHROMIUM_PATH"] = executable
        for name in (
            "IATEST_FIREFOX_PATH",
            "IATEST_FIREFOX_DRIVER_PATH",
            "IATEST_WEBKIT_DRIVER_PATH",
        ):
            if self.mode == "process" and os.environ.get(name):
                env[name] = os.environ[name]
        narrowed = {
            **settings,
            "mode": "policy",
            "read_paths": [
                "/input/files"
                if self.mode == "container"
                else str(self.inputs / "files")
            ],
            "denied_read_paths": [],
        }
        payload = {
            "root": "/tmp/browser-project"
            if self.mode == "container"
            else str(self.work / "project"),
            "project": project,
            "spec": spec,
            "config": {"sandbox": narrowed},
            "settings": settings,
            "max_queries": max_queries,
            "max_actions": spec.get("max_actions", 100),
            "timeout": min(
                settings.get("hard_timeout_seconds", 300), control.remaining() or 300
            ),
            "max_requests": max(
                0, (control.max_requests or 10000) - control.request_count()
            ),
        }
        try:
            if self.mode == "container":
                if not shutil.which("docker"):
                    raise ValueError(
                        "Browser RPC container mode requires Docker; no downgrade"
                    )
                (self.inputs / "worker.env").write_text(
                    "HOME=/tmp\nTMPDIR=/tmp\nPYTHONDONTWRITEBYTECODE=1\n",
                    encoding="utf-8",
                )
                network = settings.get("container_network", "none")
                if network != "none":
                    check = subprocess.run(
                        [
                            "docker",
                            "network",
                            "inspect",
                            "--format",
                            "{{.Internal}}",
                            network,
                        ],
                        capture_output=True,
                        check=False,
                        text=True,
                        timeout=10,
                    )
                    if check.returncode or check.stdout.strip() != "true":
                        raise ValueError(
                            "Container browser requires an internal test network"
                        )
                self.container_name = "iatest-browser-" + os.urandom(8).hex()
                args = container_command(
                    settings,
                    self.inputs,
                    self.outputs,
                    self.container_name,
                )
                args.insert(2, "-i")  # Persistent RPC keeps stdin open for requests/grants.
                index = args.index("iatest.execution.worker")
                args[index:] = ["iatest.execution.browser_rpc"]
                env = None
            else:
                bootstrap = f"import sys;sys.path.insert(0,{str(Path(__file__).resolve().parents[2])!r});from iatest.execution.browser_rpc import main;main()"
                args = [sys.executable, "-I", "-B", "-c", bootstrap]
            self.process = subprocess.Popen(
                args,
                cwd=self.work,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=os.name == "posix",
            )
            self.job = None
            if os.name == "nt" and self.mode == "process":
                from iatest.execution.windows_job import attach_job

                self.job = attach_job(self.process, settings)

            def reader():
                try:
                    for line in iter(
                        lambda: self.process.stdout.readline(12582913), b""
                    ):
                        self.bytes += len(line)
                        if len(line) > 12582912 or self.bytes > self.maximum:
                            raise ValueError(
                                "Browser RPC evidence/output budget exceeded"
                            )
                        self.responses.put(json.loads(line), timeout=1)
                except (ValueError, queue.Full):
                    pass

            self.reader = threading.Thread(target=reader, daemon=True)
            self.reader.start()
            self.send({"startup": payload})
            self.receive(20)
        except BaseException:
            self.close()
            raise

    def send(self, value):
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode() + b"\n"
        if len(raw) > 1048576:
            raise ValueError("Browser RPC request exceeds 1 MiB")
        self.process.stdin.write(raw)
        self.process.stdin.flush()

    def receive(self, timeout=30):
        deadline = time.monotonic() + min(timeout, self.control.remaining() or timeout)
        while True:
            self.control.check()
            if time.monotonic() > deadline:
                raise TimeoutError(
                    "Browser RPC response timed out; input must not be replayed"
                )
            try:
                response = self.responses.get(timeout=0.05)
            except queue.Empty:
                if self.process.poll() is not None:
                    raise RuntimeError(
                        "Browser RPC worker exited; input delivery may be unknown"
                    )
                continue
            if response.get("grant"):
                try:
                    self.control.request()
                    allowed = True
                except RuntimeError:
                    allowed = False
                self.send({"grant_reply": True, "allowed": allowed})
                continue
            if response.get("error"):
                self.pending = response.get("pending", False)
                raise ValueError(response["error"])
            self.queries = response.get("queries", self.queries)
            self.page_alias = response.get("page_alias", self.page_alias)
            self.pending = response.get("pending", False)
            return decode(response.get("result"))

    def rpc(self, method, *args, **kwargs):
        if method not in METHODS:
            raise ValueError("Unregistered browser RPC operation")
        if method == "evaluate" and args[0] not in {
            "location.href",
            "devicePixelRatio",
        }:
            raise ValueError("RPC never accepts arbitrary JavaScript")
        if method == "upload":
            import copy

            from iatest.execution.native_isolation import stage_uploads

            step = copy.deepcopy(args[0])
            step["op"] = "upload"
            staged = [p for p in (self.inputs / "files").rglob("*") if p.is_file()]
            if len(staged) > 1000:
                raise ValueError("Browser upload file-count quota exceeded")
            remaining_input = int(
                load_config(self.root)["sandbox"].get("max_input_bytes", 64000000)
            ) - sum(p.stat().st_size for p in staged)
            if remaining_input <= 0:
                raise ValueError("Browser upload input quota exhausted")
            stage_uploads(
                {"spec": {"steps": [step]}},
                self.policy,
                self.inputs / "files",
                Path("/input/files")
                if self.mode == "container"
                else self.inputs / "files",
                remaining_input,
            )
            args = (step,)
        self.sequence += 1
        self.pending = method in {
            "act",
            "navigate",
            "page_action",
            "scroll",
            "visual_click",
            "upload",
        }
        try:
            self.send(
                {
                    "sequence": self.sequence,
                    "method": method,
                    "args": args,
                    "kwargs": kwargs,
                }
            )
            return self.receive()
        except BaseException:
            if self.control.reason():
                self.close()
            raise

    def __getattr__(self, name):
        if name in METHODS:
            return lambda *args, **kwargs: self.rpc(name, *args, **kwargs)
        raise AttributeError(name)

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.process:
            try:
                if self.process.poll() is None:
                    self.send({"close": True})
                    self.process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                stop_worker(self.process, 1, process_group=self.mode != "container")
        if self.container_name:
            try:
                subprocess.run(
                    ["docker", "rm", "-f", self.container_name],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        if getattr(self, "job", None):
            self.job()
        if getattr(self, "reader", None):
            self.reader.join(1)
        if self.process:
            for stream in (self.process.stdin, self.process.stdout):
                stream.close()
        self.temp.cleanup()


def main():
    from iatest.engines.web import BrowserSession
    from iatest.execution import native_ipc
    from iatest.execution.control import RunControl
    from iatest.execution.worker import apply_resource_limits

    raw = sys.stdin.buffer.readline(1048577)
    if len(raw) > 1048576:
        raise ValueError("Oversized browser startup")
    value = json.loads(raw)["startup"]
    apply_resource_limits(value["settings"])
    root = Path(value["root"])
    root.mkdir(parents=True, exist_ok=True)
    (root / "iatest.json").write_text(json.dumps(value["config"]))
    control = RunControl(
        timeout_seconds=value["timeout"], max_requests=value["max_requests"]
    )
    requests = queue.Queue(maxsize=4)
    grant = queue.Queue(maxsize=1)
    lock = threading.Lock()
    browser = None

    def emit(packet):
        raw = json.dumps(encode(packet), allow_nan=False).encode() + b"\n"
        if len(raw) > 12582912:
            raise ValueError("Browser output frame exceeded")
        with lock:
            sys.stdout.buffer.write(raw)
            sys.stdout.buffer.flush()

    def listen():
        try:
            for line in iter(lambda: sys.stdin.buffer.readline(1048577), b""):
                if len(line) > 1048576:
                    break
                packet = json.loads(line)
                if packet.get("close"):
                    control.cancelled.set()
                    requests.put(packet)
                    break
                if packet.get("grant_reply"):
                    grant.put(packet, timeout=1)
                else:
                    requests.put(packet, timeout=1)
        except (ValueError, queue.Full):
            pass
        finally:
            control.cancelled.set()

    def authorize(_):
        emit({"grant": True})
        while not control.reason():
            try:
                reply = grant.get(timeout=0.1)
                break
            except queue.Empty:
                continue
        else:
            raise RuntimeError("Browser request grant cancelled")
        if not reply["allowed"]:
            raise RuntimeError("Parent request budget exhausted")

    native_ipc.authorize_request = authorize
    reader = threading.Thread(target=listen, daemon=True)
    reader.start()
    try:
        browser = BrowserSession(
            root,
            value["project"],
            value["spec"],
            control,
            max_queries=value["max_queries"],
        )
        count = [0]

        def charge(n):
            count[0] += n
            if count[0] > value["max_actions"]:
                raise ValueError("Browser RPC action budget exhausted")

        browser.action_charge = charge
        emit({"result": {"ready": True}})
        sequence = 0
        while not control.reason():
            try:
                packet = requests.get(timeout=0.1)
            except queue.Empty:
                continue
            if packet.get("close"):
                break
            try:
                if packet.get("sequence") != sequence + 1:
                    raise ValueError("Browser RPC sequence mismatch")
                sequence += 1
                method = packet["method"]
                if method not in METHODS:
                    raise ValueError("Unregistered browser RPC method")
                if method == "evaluate" and packet["args"][0] not in {
                    "location.href",
                    "devicePixelRatio",
                }:
                    raise ValueError("Arbitrary scripts are forbidden")
                if method in {
                    "act",
                    "navigate",
                    "page_action",
                    "visual_click",
                    "upload",
                }:
                    charge(1)
                result = getattr(browser, method)(*packet["args"], **packet["kwargs"])
                emit(
                    {
                        "result": result,
                        "queries": browser.queries,
                        "page_alias": browser.page_alias,
                        "pending": browser.pending,
                    }
                )
            except (ValueError, RuntimeError, OSError, TimeoutError) as exc:
                emit({"error": str(exc)[:500], "pending": browser.pending})
                if browser.pending:
                    break
    except (ValueError, RuntimeError, OSError) as exc:
        emit({"error": str(exc)[:500]})
    finally:
        if browser:
            browser.close()


if __name__ == "__main__":
    main()
