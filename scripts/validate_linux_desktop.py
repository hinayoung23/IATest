"""Real AT-SPI acceptance in an unlocked X11 session, using one owned GTK Lab.

Run with the framework Python; --gtk-python selects OS Python with python3-gi.
Only the exact owned Lab PID receives actions. No user's App is modified.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from iatest.engines.desktop import DesktopSession
from iatest.execution.control import RunControl


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gtk-python", default="/usr/bin/python3")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not sys.platform.startswith("linux") or not os.environ.get("DISPLAY"):
        parser.error("An unlocked X11 graphical session is required")
    fixture = Path(__file__).resolve().parents[1] / "examples/desktop_controls_lab.py"
    report = {"passed": False, "synthetic_driver": False, "checks": []}
    process = session = None
    try:
        process = subprocess.Popen([args.gtk_python, str(fixture)])
        report["process_id"] = process.pid
        deadline = time.monotonic() + 10
        executable = str(Path(f"/proc/{process.pid}/exe").resolve())
        session = DesktopSession(
            {"platform": "linux", "process_id": process.pid, "app_id": executable},
            RunControl(timeout_seconds=60),
        )

        def expect_text(identifier, expected):
            until = time.monotonic() + 3
            while True:
                value = session.call("query", {"identifier": identifier})
                if (
                    value["complete"]
                    and value["match_count"] == 1
                    and value["candidates"][0]["text"] == expected
                ):
                    return
                if time.monotonic() >= until:
                    raise AssertionError(
                        "Native text readback differs for "
                        + identifier
                        + ": "
                        + repr(value)
                    )
                time.sleep(0.05)

        while True:
            try:
                found = session.call("query", {"identifier": "nickname"})
                if found["complete"] and found["match_count"] == 1:
                    break
            except ValueError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError("GTK Lab did not expose a complete AT-SPI target")
            time.sleep(0.2)
        from iatest.application.ui_adapters import DesktopExplorer

        discovery = DesktopExplorer.__new__(DesktopExplorer)
        discovery.context = {'platform': 'linux'}
        discovery.find = lambda target: session.call('query', target)
        outline = discovery.outline()
        identifiers = {c['identifier'] for c in outline['controls']}
        assert {'nickname', 'save'} <= identifiers
        assert not outline['complete'] and not outline['unique']
        report['linux_explorer_controls'] = sorted(identifiers)
        marker = "Linux 中文 " + uuid.uuid4().hex[:8]
        session.call("fill", {"identifier": "nickname"}, value=marker)
        expect_text("nickname", marker)
        session.call("click", {"identifier": "save"})
        expect_text("status", "Saved: " + marker)
        report["checks"].append("native_unicode_fill_click_business_readback")
        # Establish the caret explicitly; refocusing an already focused field
        # must not select all text before every individual key.
        session.call("press", {"identifier": "nickname"}, key="End")
        session.call("press", {"identifier": "nickname"}, key="Backspace")
        expect_text("nickname", marker[:-1])
        session.call("press", {"identifier": "nickname"}, key="Enter")
        expect_text("status", "Saved: " + marker[:-1])
        report["checks"].append("focused_native_backspace_and_enter")
        count = session.call("query", {"identifier": "count"})["candidates"][0]["text"]
        try:
            session.call("click", {"name": "Duplicate"})
        except ValueError:
            pass
        else:
            raise AssertionError("Ambiguous action was accepted")
        assert (
            session.call("query", {"identifier": "count"})["candidates"][0]["text"]
            == count
        )
        report["checks"].append("ambiguous_click_rejected_without_side_effect")
        secret = session.call("query", {"identifier": "password"})["candidates"][0]
        assert (
            secret["secure"]
            and secret["value"] is None
            and secret["text"] == "[REDACTED]"
        )
        report["checks"].append("password_redacted")
        assert (
            session.call("query", {"identifier": "absent-control"})["match_count"] == 0
        )
        process.terminate()
        process.wait(timeout=5)
        try:
            session.call("click", {"identifier": "save"})
        except ValueError:
            pass
        else:
            raise AssertionError("Exited process accepted input")
        report["checks"].append("exited_process_rejected")
        report["passed"] = True
    except Exception as exc:  # noqa: BLE001 -- Preserve failure evidence and close the owned Lab.
        report["error"] = str(exc)
    finally:
        if session:
            session.close()
        if process and process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
