"""Bounded real Android diagnostics; no target-App input or image/model upload.

The framework-owned probe is installed when opening the session. The target App
must already be foreground. Optional metrics reset its platform gfxinfo counters.
Images are checked in memory and are not saved in this metadata-only report.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from iatest.core.files import atomic_write_text
from iatest.engines.client_performance import AndroidCapture
from iatest.execution.control import RunControl
from iatest.mobile.devices import NativeDevice
from iatest.mobile.runtime import MobileSession


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--app", required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--media", action="store_true")
    parser.add_argument(
        "--video-capture", choices=["screenshot", "mirror"], default="screenshot"
    )
    parser.add_argument("--client-metrics", action="store_true")
    args = parser.parse_args()
    profile = {
        "id": "android-device-validation",
        "platform": "android",
        "device_id": args.serial,
        "app_id": args.app,
        "artifact_dir": str(args.artifacts.resolve()),
        "video_capture": args.video_capture,
    }
    device = NativeDevice(profile, "validation-baseline")

    def forwards():
        return sorted(
            line
            for line in device.adb("forward", "--list").splitlines()
            if line.split() and line.split()[0] == args.serial
        )

    def ime():
        return device.adb(
            "shell", "settings", "get", "secure", "default_input_method"
        ).strip()

    baseline = {"forwards": forwards(), "ime": ime()}
    report = {"physical_device": True, "app": args.app, "checks": {}, "media": []}
    session = None
    failure = None
    try:
        session = MobileSession(profile, control=RunControl(timeout_seconds=120))
        report["checks"]["native_handshake"] = True
        started = time.monotonic()
        overview = session.outline()
        report["outline"] = {
            "ms": round((time.monotonic() - started) * 1000, 2),
            "visited_nodes": overview.get("visited_nodes"),
            "controls": len(overview["controls"]),
            "complete": overview["complete"],
            "proves_uniqueness": overview["proves_uniqueness"],
        }
        assert overview["proves_uniqueness"] is False
        report["checks"]["bounded_discovery"] = len(overview["controls"]) <= 12
        candidates = overview.get("regions", []) + overview["controls"]
        assert candidates, "No discoverable target; specify a different foreground page"
        target = candidates[0]["target"]
        observed = session.query(target, cache=False, timeout=2)
        session.unique(observed)
        report["checks"]["fresh_unique_query"] = True
        spatial_targets = [
            c["target"] for c in overview["controls"] if "bounds" in c["target"]
        ]
        # Overlapping full-page View wrappers may be ambiguous; prefer a small leaf.
        spatial = min(
            spatial_targets,
            key=lambda t: (
                (t["bounds"][2] - t["bounds"][0]) * (t["bounds"][3] - t["bounds"][1])
            ),
            default=None,
        )
        if spatial:
            session.unique(session.query(spatial, cache=False, timeout=2))
            report["checks"]["fresh_spatial_query"] = True
        # Small center crop verifies the native region contract without exporting pixels.
        image = session.visual_region(
            target, crop={"x": 0.4, "y": 0.4, "width": 0.2, "height": 0.2}
        )
        assert image["data"].startswith(b"\x89PNG\r\n\x1a\n")
        report["crop"] = {k: image[k] for k in ("width", "height", "rect")}
        report["checks"]["native_local_crop"] = True
        if args.media:
            for codec in ("png", "h264-native", "h264"):
                started = time.monotonic()
                frame = session.preview(separate=True, codec=codec)
                data = frame["binary"]
                assert data, "Empty preview payload"
                if codec != "h264-native":
                    assert data.startswith(b"\x89PNG\r\n\x1a\n")
                else:
                    assert frame["codec"] == "h264-annexb" and frame["key_frame"]
                report["media"].append(
                    {
                        "requested_codec": codec,
                        "first_frame_ms": round((time.monotonic() - started) * 1000, 2),
                        "bytes": len(data),
                        **{
                            k: frame[k]
                            for k in (
                                "width",
                                "height",
                                "capture_source",
                                "capture_cpu_pixel_copy",
                                "host_decode",
                            )
                            if k in frame
                        },
                    }
                )
                session.stop_preview(session.media_receiver.id)
            report["checks"]["media_first_frames"] = True
        if args.client_metrics:
            capture = AndroidCapture(profile, RunControl(timeout_seconds=15))
            capture.before()
            time.sleep(1)
            capture.after()
            report["client_metrics"] = {
                "values": capture.values,
                "notes": capture.notes,
            }
            report["checks"]["client_memory_snapshot"] = (
                "memory_pss_kb" in capture.values
            )
        report["scope"] = (
            "Read-only native diagnostics; no business journey, input, installation delivery or model acceptance"
        )
    except Exception as error:  # noqa: BLE001 -- Persist diagnostic failure and still verify teardown.
        failure = error
        report["error"] = {"type": type(error).__name__, "message": str(error)}
    finally:
        try:
            if session:
                session.close()
                report["checks"]["owned_processes_stopped"] = all(
                    process.poll() is not None for process in session.device.processes
                )
        except Exception as error:  # noqa: BLE001 -- A cleanup failure must never be reported as passed.
            failure = failure or error
            report["cleanup_error"] = {
                "type": type(error).__name__,
                "message": str(error),
            }
        finally:
            report["checks"]["forwards_restored"] = forwards() == baseline["forwards"]
            report["checks"]["ime_unchanged"] = ime() == baseline["ime"]
            report["passed"] = failure is None and all(report["checks"].values())
            atomic_write_text(
                args.output, json.dumps(report, ensure_ascii=False, indent=2) + "\n"
            )
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "checks": report["checks"],
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    if failure:
        raise failure
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
