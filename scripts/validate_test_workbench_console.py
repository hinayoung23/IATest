"""Real browser console acceptance against native engines and a local HTTP lab."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from iatest.core.config import init_project
from iatest.web.server import make_handler
from examples.automation_lab import create_server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", default=os.environ.get("IATEST_CHROMIUM_PATH", ""))
    args = parser.parse_args()
    os.environ["IATEST_CONSOLE_RESOURCE"] = "synthetic-console-resource-authority"
    if args.browser:
        os.environ["IATEST_CHROMIUM_PATH"] = args.browser
    with tempfile.TemporaryDirectory(prefix="iatest-console-") as directory:
        root = Path(directory)
        init_project(root, "console-smoke")
        lab = create_server()
        console = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(root, "console-smoke")
        )
        threads = [
            threading.Thread(target=s.serve_forever, daemon=True)
            for s in (lab, console)
        ]
        for thread in threads:
            thread.start()
        try:
            environment = dict(
                os.environ,
                IATEST_SMOKE_URL=f"http://127.0.0.1:{console.server_port}",
                IATEST_LAB_URL=f"http://127.0.0.1:{lab.server_port}",
                IATEST_SMOKE_BROWSER=args.browser,
            )
            node = shutil.which("node")
            if not node:
                raise ValueError(
                    "Node and a development-only Playwright install are required for this acceptance script"
                )
            environment.setdefault(
                "NODE_PATH", str(Path(node).resolve().parents[1] / "lib/node_modules")
            )
            try:
                subprocess.run(
                    [node, str(Path(__file__).with_suffix(".cjs"))],
                    check=True,
                    env=environment,
                    timeout=180,
                )
            except subprocess.CalledProcessError:
                import json

                from iatest.engines.store import EngineStore
                from iatest.web.task_store import list_tasks

                print(
                    json.dumps(
                        {
                            "plans": EngineStore(root, "console-smoke").plans(),
                            "runs": EngineStore(root, "console-smoke").runs(),
                            "tasks": list_tasks(root, "console-smoke"),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    flush=True,
                )
                raise
        finally:
            for server in (lab, console):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=2)


if __name__ == "__main__":
    main()
