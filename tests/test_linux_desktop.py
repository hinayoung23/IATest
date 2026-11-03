"""Opt-in real graphical acceptance; not an emulation of AT-SPI."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(
    not sys.platform.startswith("linux")
    or not os.environ.get("DISPLAY")
    or os.environ.get("IATEST_TEST_LINUX_DESKTOP") != "1",
    reason="Requires an explicit X11/GTK3 AT-SPI acceptance session",
)
def test_real_linux_desktop_unicode_keys_ambiguity_and_process_exit(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/validate_linux_desktop.py"
    output = tmp_path / "desktop.json"
    process = subprocess.run(
        [sys.executable, str(script), "--output", str(output)],
        check=False,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    result = json.loads(output.read_text())
    assert result["passed"] and not result["synthetic_driver"]
    assert len(result["checks"]) == 5
