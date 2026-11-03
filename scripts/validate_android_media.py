"""Bounded physical video continuity/restart checks on the owned Android Lab."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from iatest.execution.control import RunControl
from iatest.mobile.devices import NativeDevice
from iatest.mobile.runtime import MobileSession
from iatest.mobile.store import MobileStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--profile", default="android-lab")
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        args.output.with_name(
            args.output.stem + "-attempt-" + str(time.time_ns()) + ".json"
        ).write_text(args.output.read_text())
    if not 5 <= args.seconds <= 60:
        parser.error("Video window must be 5–60 seconds")
    profile = MobileStore(Path.cwd(), args.project).get("profile", args.profile)
    if profile["platform"] != "android" or profile["app_id"] != "dev.iatest.mobile.lab":
        parser.error("Only the owned Android Lab is accepted")
    baseline = (
        NativeDevice(profile, "media-baseline").adb("forward", "--list").splitlines()
    )
    report = {
        "physical_device": True,
        "frames": 0,
        "idle_acknowledgements": 0,
        "bytes": 0,
        "passed": False,
    }
    session = None
    try:
        session = MobileSession(
            profile, control=RunControl(timeout_seconds=args.seconds + 45)
        )
        session.action({"op": "terminate"}, None, "reset", 5)
        session.action({"op": "launch"}, None, "start", 8)
        png = session.preview(separate=True, codec="png")
        assert png["binary"].startswith(b"\x89PNG\r\n\x1a\n")
        report["png"] = {k: png[k] for k in ("width", "height")}
        session.stop_preview(session.media_receiver.id)
        started = last = time.monotonic()
        generation, presentation, max_gap = None, None, 0
        while time.monotonic() - started < args.seconds:
            frame = session.preview(
                separate=True,
                codec="h264-native",
                request_keyframe=report["frames"] == 20,
            )
            now = time.monotonic()
            max_gap = max(max_gap, now - last)
            last = now
            if frame.get("idle"):
                assert not frame.get("binary") and frame["key_frame"] is False
                report["idle_acknowledgements"] += 1
                continue
            if generation != frame["generation"]:
                assert frame["key_frame"]
                generation, presentation = frame["generation"], None
            if presentation is not None:
                assert frame["presentation_us"] > presentation
            presentation = frame["presentation_us"]
            assert (
                frame["capture_source"] == "display-surface"
                and frame["capture_cpu_pixel_copy"] is False
            )
            report["frames"] += 1
            report["bytes"] += len(frame["binary"])
            report["geometry"] = [frame["width"], frame["height"]]
        report.update(
            seconds=time.monotonic() - started, maximum_packet_gap_ms=max_gap * 1000
        )
        assert report["frames"] > 0 and generation is not None
        session.stop_preview(session.media_receiver.id)
        restarted = session.preview(separate=True, codec="h264-native")
        assert restarted["generation"] != generation and restarted["key_frame"]
        report["new_generation_recovered"] = True
        decoded = session.preview(separate=True, codec="h264")
        assert (
            decoded["binary"].startswith(b"\x89PNG\r\n\x1a\n")
            and decoded["host_decode"] is True
        )
        report["host_decode"] = True
        report["passed"] = True
    except Exception as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        if session:
            session.close()
            report["owned_processes_stopped"] = all(
                p.poll() is not None for p in session.device.processes
            )
        report["forwards_restored"] = (
            NativeDevice(profile, "media-final").adb("forward", "--list").splitlines()
            == baseline
        )
        report["passed"] &= (
            report.get("owned_processes_stopped", False) and report["forwards_restored"]
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
