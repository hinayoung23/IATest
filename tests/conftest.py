"""Optional native test tools from the selected host, without machine-specific paths."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def java_bin():
    executable = shutil.which("javac")
    candidates = []
    if os.environ.get("JAVA_HOME"):
        candidates.append(Path(os.environ["JAVA_HOME"]) / "bin")
    if executable:
        candidates.append(Path(executable).resolve().parent)
    candidates.append(Path("/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home/bin"))
    for directory in candidates:
        if shutil.which("javac", path=str(directory)) and shutil.which("java", path=str(directory)):
            # macOS can expose launch stubs even when no system JDK is selected.
            try:
                check = subprocess.run([str(directory / "javac"), "-version"], check=False, capture_output=True, timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if check.returncode == 0:
                return directory
    pytest.skip("Requires installed JDK selected by JAVA_HOME or PATH")
