"""Kernel UID firewall acceptance in a fresh, controller-owned rootable VM only."""

from __future__ import annotations

import argparse
import json
import os
import re
import socketserver
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path

from iatest.core.config import init_project
from iatest.engines.contracts import ExecutionPlan
from iatest.execution.control import RunControl
from iatest.mobile.delivery import fetch_app
from iatest.mobile.devices import NativeDevice
from iatest.mobile.emulator import plan_vm_scope
from iatest.mobile.isolation import DeviceNetworkIsolation
from iatest.mobile.store import MobileStore


@contextmanager
def echo_server():
    nonce = uuid.uuid4().hex
    hits = []

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            hits.append(True)
            self.request.sendall(nonce.encode())

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], nonce, hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--apk", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--java-home", type=Path)
    parser.add_argument("--image", default="android-36;google_apis;arm64-v8a")
    parser.add_argument("--project", default="root-firewall-acceptance")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.java_home:
        os.environ["JAVA_HOME"] = str(args.java_home.resolve(strict=True))
    root = Path.cwd()
    init_project(root, args.project)
    app = fetch_app(root, args.project, {"id": "root-lab", "platform": "android", "app_id": "dev.iatest.mobile.lab", "path": str(args.apk.resolve())})
    profile = {
        "id": "root-vm", "platform": "android", "device_id": "managed", "app_id": app["metadata"]["app_id"],
        "artifact_dir": str(args.artifacts.resolve()), "require_os_sandbox": True,
        "managed_emulator": {"sdk_root": str(args.sdk.resolve()), "system_image": args.image, "artifact_id": app["id"]},
    }
    MobileStore(root, args.project).save_profile(profile)
    plan = ExecutionPlan.from_dict({"id": "root-kernel", "kind": "android", "spec": {"profile": profile["id"], "steps": [{"op": "expect", "target": {"text": "Ready"}, "expected": True}]}})
    control = RunControl(timeout_seconds=480)
    report = {"passed": False, "physical_device": False, "traffic_source": "owned Lab Java TCP client; UID and PID verified", "checks": {}}
    try:
        with ExitStack() as stack:
            evidence = stack.enter_context(plan_vm_scope(root, args.project, plan, control))
            report["os_sandbox"] = evidence
            owned = MobileStore(root, args.project).get("profile", profile["id"])
            device = NativeDevice(owned, "root-kernel-acceptance")
            device.control = control
            device.adb("root")
            deadline = time.monotonic() + 15
            while True:
                try:
                    if device.adb("shell", "id", "-u").strip() == "0":
                        break
                except (OSError, RuntimeError):
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError("Selected owned image does not allow ADB root")
                control.wait(0.2)
            app_uid = device.adb("shell", "run-as", profile["app_id"], "id", "-u").strip()
            if not app_uid.isdigit() or int(app_uid) < 10000:
                raise ValueError("Run-as traffic does not use a non-system App UID")
            packages = device.adb("shell", "cmd", "package", "list", "packages", "-U")
            if re.findall(r"package:" + re.escape(profile["app_id"]) + r"\s+uid:([0-9]+)", packages) != [app_uid]:
                raise ValueError("Run-as UID differs from installed Lab")
            report["uid"] = int(app_uid)
            allow = stack.enter_context(echo_server())
            deny = stack.enter_context(echo_server())
            for port, endpoint in ((18750, allow), (18751, deny)):
                device.adb("reverse", "tcp:" + str(port), "tcp:" + str(endpoint[0]))
                stack.callback(device.adb, "reverse", "--remove", "tcp:" + str(port))

            def connect(ip, port):
                operation = uuid.uuid4().hex
                device.adb("shell", "am", "force-stop", profile["app_id"])
                output = device.adb("shell", "am", "start", "-W", "-n", profile["app_id"] + "/dev.iatest.lab.MainActivity", "--es", "iatest_probe_id", operation, "--es", "iatest_probe_ip", ip, "--ei", "iatest_probe_port", str(port))
                if "Status: ok" not in output:
                    raise RuntimeError("Owned network probe App was not launched")
                end = time.monotonic() + 8
                while time.monotonic() < end:
                    try:
                        value = json.loads(device.adb("shell", "run-as", profile["app_id"], "cat", "files/iatest-network-probe.json"))
                        if value.get("id") == operation:
                            if value.get("uid") != int(app_uid) or str(value.get("pid")) != device.adb("shell", "pidof", profile["app_id"]).strip():
                                raise ValueError("Network probe does not match the owned App UID/PID")
                            return value
                    except (OSError, RuntimeError, json.JSONDecodeError):
                        pass
                    control.wait(0.1)
                raise TimeoutError("Owned App did not publish the matching network probe result")

            report["baseline"] = {ip: {"allow": connect(ip, 18750), "deny": connect(ip, 18751)} for ip in ("127.0.0.1", "::1")}
            report["baseline_server_hits"] = {"allow": len(allow[2]), "deny": len(deny[2])}
            if not all(pair["allow"].get("response") == allow[1] and pair["deny"].get("response") == deny[1] for pair in report["baseline"].values()):
                raise AssertionError("Both IPv4/IPv6 fixture endpoints must work before installing rules")
            settings = {"backend": "android_root_firewall", "root_backend": "adb_root", "allowed_endpoints": [{"ip": ip, "port": 18750} for ip in ("127.0.0.1", "::1")]}
            firewall = DeviceNetworkIsolation(device, {**owned, "isolation": settings})
            firewall.open()
            stack.callback(firewall.close)
            report["rules"] = dict(firewall.expected)
            blocked_before = len(deny[2])
            report["restricted"] = {ip: {"allow": connect(ip, 18750), "deny": connect(ip, 18751)} for ip in ("127.0.0.1", "::1")}
            report["checks"]["allowed_tcp_both_families"] = all(pair["allow"].get("response") == allow[1] for pair in report["restricted"].values())
            report["checks"]["denied_tcp_both_families"] = all("response" not in pair["deny"] or not pair["deny"]["response"] for pair in report["restricted"].values()) and len(deny[2]) == blocked_before
            firewall.close()
            report["checks"]["owned_chains_removed"] = all(firewall.chain not in device.adb("shell", tool, "-S") for tool in ("iptables", "ip6tables"))
            report["restored"] = {ip: connect(ip, 18751) for ip in ("127.0.0.1", "::1")}
            report["checks"]["denied_endpoint_restored"] = all(value.get("response") == deny[1] for value in report["restored"].values())
        report["checks"]["vm_cleaned"] = bool(evidence.get("process_stopped") and evidence.get("userdata_removed"))
        report["passed"] = all(report["checks"].values())
    except Exception as exc:  # noqa: BLE001 -- Preserve real acceptance failure after owned cleanup.
        report["error"] = f"{type(exc).__name__}: {exc}"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        args.output.with_name(args.output.stem + "-attempt-" + str(time.time_ns()) + args.output.suffix).write_bytes(args.output.read_bytes())
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"passed": report["passed"], "checks": report["checks"], "error": report.get("error")}))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
