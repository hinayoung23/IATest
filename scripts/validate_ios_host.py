"""Exercise the real Instruments host lifecycle against an owned macOS process."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from iatest.application.ios_measurements import summaries
from iatest.execution.control import RunControl
from iatest.mobile.ios_capture import XctraceRecorder


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/private/tmp/iatest-ios-host-acceptance.json"),
    )
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise ValueError(
            "This acceptance script requires macOS/Xcode, and does not operate an iOS device"
        )
    # Instruments may record the target environment. Never attach this test to
    # the caller process carrying provider credentials; create a minimal child.
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONNOUSERSITE": "1"},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    recorder = XctraceRecorder(
        child.pid,
        RunControl(timeout_seconds=45),
        duration_seconds=20,
        ready_timeout_seconds=20,
    )
    try:
        recorder.start()
        time.sleep(1.5)
        data = recorder.finish()
        assert child.poll() is None, (
            "Stopping the recorder must not stop the target process"
        )
        assert data["metadata"]["platform"] == "macOS"
        report = {
            "passed": True,
            "platform": "macOS",
            "schema": data["schema"],
            "target_survived_recorder_stop": True,
            "measurements": summaries(data["samples"]),
            "ios_device_actions": 0,
        }
        assert report["measurements"].get("memory_footprint_kb")
        args.output.write_text(json.dumps(report, indent=2))
        print(json.dumps(report))
    finally:
        recorder.close()
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)


if __name__ == "__main__":
    main()
