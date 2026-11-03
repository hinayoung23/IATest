"""Actual Chromium console against a signed physical Android phone and IATest Lab.

No device classes are patched. A separate validation project prevents cleanup
from cancelling other projects' tasks. Only the framework-owned App is allowed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import time
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

from iatest.core.config import init_project
from iatest.execution.control import RunControl
from iatest.mobile.runtime import SESSIONS, MobileSession
from iatest.mobile.store import MobileStore
from iatest.web.server import make_handler
from iatest.web.task_store import list_tasks, request_cancel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--project",
        required=True,
        help="Source project containing the real device profile",
    )
    parser.add_argument("--profile", default="android-lab")
    parser.add_argument(
        "--plan",
        default="android-native-save-0",
        help="Previously validated Lab plan to import and replay through the console",
    )
    parser.add_argument("--browser", type=Path, required=True)
    parser.add_argument("--node-modules", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    profile = MobileStore(root, args.project).get("profile", args.profile)
    if profile["platform"] != "android" or profile["app_id"] != "dev.iatest.mobile.lab":
        parser.error(
            "Physical console acceptance only operates the framework-owned Android Lab"
        )
    project = "android-console-" + uuid.uuid4().hex[:12]
    init_project(root, project)
    MobileStore(root, project).save_profile(profile)
    session = MobileSession(profile, control=RunControl(timeout_seconds=20))
    try:
        session.action({"op": "terminate"}, None, "console-reset", 5)
        session.action({"op": "launch"}, None, "console-start", 8)
    finally:
        session.close()
    plan = MobileStore(root, args.project).get("plan", args.plan)
    if plan["kind"] != "android" or plan["spec"]["profile"] != args.profile:
        parser.error("The source plan must use the selected Android Lab profile")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    plan_path = args.output.with_name(args.output.stem + "-plan.json")
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(root, project))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["node", str(Path(__file__).with_suffix(".cjs"))],
            env={
                **os.environ,
                "NODE_PATH": str(args.node_modules),
                "IATEST_SMOKE_BROWSER": str(args.browser),
                "IATEST_SMOKE_URL": f"http://127.0.0.1:{server.server_port}",
                "IATEST_ANDROID_CONSOLE_PROJECT": project,
                "IATEST_ANDROID_CONSOLE_PROFILE": args.profile,
                "IATEST_ANDROID_CONSOLE_REPORT": str(args.output.resolve()),
                "IATEST_ANDROID_CONSOLE_PLAN": str(plan_path.resolve()),
            },
            check=True,
            timeout=240,
        )
        run = next(
            r
            for r in MobileStore(root, project).runs()
            if r["plan_id"] == "android-console-save-relaunch"
        )
        if run["verdict"] != "passed":
            raise RuntimeError("The actual console-triggered native run did not pass")
        report = json.loads(args.output.read_text())
        report["native_run"] = {k: run[k] for k in ("run_id", "verdict", "metrics")}
        report["native_steps"] = len(run["steps"])
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    finally:
        deadline = time.monotonic() + 30
        while True:
            active = [
                t
                for t in list_tasks(root, project)
                if t["status"] in {"queued", "running"}
            ]
            for task in active:
                request_cancel(root, project, task["id"])
            if not active or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        SESSIONS.close_all()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        if active:
            raise RuntimeError("Console-owned native task cleanup remains unconfirmed")


if __name__ == "__main__":
    main()
