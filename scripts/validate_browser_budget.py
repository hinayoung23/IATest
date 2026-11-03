"""Probe Linux Chromium startup with bounded process memory, without weakening its sandbox."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from iatest.engines.web import chromium_path


def main():
    if not sys.platform.startswith("linux"):
        raise SystemExit("This resource-limit probe requires Linux")
    import resource

    executable = chromium_path()
    if not executable:
        raise SystemExit("Chromium is required for Linux browser acceptance")
    results = []
    for memory in (512, 2048):
        def limits():
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            resource.setrlimit(resource.RLIMIT_DATA, (memory * 1048576,) * 2)

        with tempfile.TemporaryDirectory(prefix="iatest-browser-budget-") as profile:
            process = subprocess.Popen(
                [executable, "--headless=new", "--no-first-run", "--no-default-browser-check",
                 "--disable-background-networking", "--user-data-dir=" + profile,
                 "--dump-dom", "about:blank"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, preexec_fn=limits,
                start_new_session=True,
            )
            try:
                stdout, stderr = process.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                import os
                import signal

                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout, stderr = process.communicate(timeout=5)
            results.append({
                "memory_mb": memory, "returncode": process.returncode,
                "passed": process.returncode == 0 and b"<html" in stdout,
                "stderr": stderr.decode("utf-8", "replace")[-6000:],
            })
    print(json.dumps({"browser": executable, "probes": results}, indent=2), flush=True)
    if not results[-1]["passed"]:
        raise SystemExit("Chromium failed within the explicit 2048 MiB acceptance budget")


if __name__ == "__main__":
    main()
