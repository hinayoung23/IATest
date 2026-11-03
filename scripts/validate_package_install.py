"""Smoke-test an installed base wheel outside the source checkout (no extras)."""

from __future__ import annotations

import importlib.metadata
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def main():
    import iatest
    from iatest.application.testing_service import execute_test_plan
    from iatest.core.config import init_project, load_config, save_config

    package = Path(iatest.__file__).resolve()
    checkout = Path(__file__).resolve().parents[1]
    if package.is_relative_to(checkout / "iatest"):
        raise RuntimeError(
            "Use a clean environment with the built wheel, outside the checkout"
        )
    command = Path(sys.executable).parent / (
        "iatest.exe" if os.name == "nt" else "iatest"
    )
    checks = {}
    with tempfile.TemporaryDirectory(prefix="iatest-installed-smoke-") as temporary:
        root = Path(temporary)
        subprocess.run(
            [str(command), "--help"],
            cwd=root,
            check=True,
            capture_output=True,
            timeout=20,
        )
        subprocess.run(
            [str(command), "init", "--project", "installed"],
            cwd=root,
            check=True,
            capture_output=True,
            timeout=20,
        )
        checks["installed_cli"] = True
        prepared = subprocess.run(
            [str(command), "test", "wayland-setup", "--output", str(root / "wayland")],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        helper = json.loads(prepared.stdout)
        adapter = Path(helper["directory"])
        assert not helper["enabled"]
        assert (
            json.loads((adapter / "metadata.json").read_text())["uuid"]
            == helper["uuid"]
        )
        assert "dev.iatest.Wayland" in (adapter / "extension.js").read_text()
        checks["packaged_wayland_adapter_prepared_without_activation"] = True
        init_project(root, "installed")
        config = load_config(root)
        config["sandbox"]["mode"] = "process"
        save_config(root, config)
        result = execute_test_plan(
            root,
            "installed",
            {
                "id": "installed-data",
                "kind": "data",
                "spec": {
                    "records": [{"id": 1}, {"id": 2}],
                    "checks": [{"id": "unique", "rule": "unique", "path": "$.id"}],
                },
            },
        )
        assert result["verdict"] == "passed", result
        checks["installed_process_worker"] = True
        assert not {"psycopg", "pymysql", "sqlglot", "pypinyin"}.intersection(
            sys.modules
        )
        checks["optional_dependencies_lazy"] = True
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-m",
                "iatest",
                "web",
                "--project",
                "installed",
                "--port",
                str(port),
            ],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 20
            while True:
                try:
                    with opener.open(base, timeout=0.5) as response:
                        assert response.status == 200
                        break
                except (urllib.error.URLError, TimeoutError):
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("Installed Web console failed to start")
                    time.sleep(0.1)
            for path in (
                "/testing?project=installed&tab=capabilities",
                "/mobile?project=installed",
                "/assets?project=installed",
                "/results?project=installed",
                "/testing?project=installed&group=interaction",
                "/static/app.css",
                "/static/app.js",
                "/static/site.css",
                "/static/site.js",
            ):
                with opener.open(base + path, timeout=10) as response:
                    assert response.status == 200 and response.read(), path
            checks["installed_web_templates_and_assets"] = True
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            checks["owned_web_process_stopped"] = process.poll() is not None
    print(
        json.dumps(
            {
                "passed": all(checks.values()),
                "version": importlib.metadata.version("iatest-framework"),
                "checks": checks,
            }
        )
    )


if __name__ == "__main__":
    main()
