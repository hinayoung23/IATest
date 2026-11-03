"""Physical Android Lab soak on one H264 receiver, channel and encoder generation."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import threading
import time
import uuid
from pathlib import Path

from iatest.core.files import atomic_write_text
from iatest.execution.control import RunControl
from iatest.mobile.devices import NativeDevice
from iatest.mobile.runtime import MobileSession
from iatest.mobile.store import MobileStore
from iatest.mobile.video import VideoDecoder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--seconds", type=int, default=3600)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 5 <= args.seconds <= 7140 or args.output.exists():
        parser.error("Use a new output file and a finite 5–7140 second window")
    profile = MobileStore(Path.cwd(), args.project).get("profile", args.profile)
    if (
        profile["platform"] != "android"
        or profile["app_id"] != "dev.iatest.mobile.lab"
        or profile.get("managed_emulator")
    ):
        parser.error("Only the owned physical Android Lab is accepted")
    profile = {
        **profile,
        "video_capture": "mirror",
        "media_limits": {
            "duration_seconds": args.seconds + 60,
            "byte_limit": 1073741824,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    device = NativeDevice(profile, "continuous-baseline")
    baseline = device.adb("forward", "--list").splitlines()
    report = {
        "passed": False,
        "physical_device": True,
        "single_stream": True,
        "required_seconds": args.seconds,
        "frames": 0,
        "idle_packets": 0,
        "encoded_bytes": 0,
        "verified_taps": 0,
        "decoded_samples": [],
        "samples": [],
        "maximum_packet_gap_ms": 0,
        "media_restarts": 0,
    }
    session = None
    started = time.monotonic()

    def save():
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        atomic_write_text(args.output, json.dumps(report, indent=2) + "\n")

    def memory(package):
        pid = device.adb("shell", "pidof", package).strip()
        if not pid.isdigit():
            raise AssertionError("Expected one live process: " + package)
        found = re.search(
            r"TOTAL PSS:\s*(\d+)", device.adb("shell", "dumpsys", "meminfo", package)
        )
        if not found:
            raise AssertionError("Process memory sample missing")
        return {"pid": int(pid), "pss_kb": int(found.group(1))}

    def decode(frame):
        done = threading.Event()
        values = []
        decoder = VideoDecoder(lambda data, metadata: (values.append(data), done.set()))
        try:
            decoder.feed(
                frame["binary"], {"width": frame["width"], "height": frame["height"]}
            )
            decoder.finish()
            if not done.wait(5):
                raise AssertionError(
                    "Real key-frame decode did not finish: " + decoder.error
                )
        finally:
            decoder.close()
        report["decoded_samples"].append(
            {
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "sha256": hashlib.sha256(values[-1]).hexdigest(),
                "width": frame["width"],
                "height": frame["height"],
            }
        )
        args.output.with_suffix(".png").write_bytes(values[-1])

    try:
        session = MobileSession(
            profile, control=RunControl(timeout_seconds=args.seconds + 120)
        )
        session.action({"op": "terminate"}, None, uuid.uuid4().hex, 5)
        session.action({"op": "launch"}, None, uuid.uuid4().hex, 10)
        initial = {
            name: memory(name) for name in (profile["app_id"], "dev.iatest.mobile")
        }
        started = last = time.monotonic()
        next_sample = next_decode = started
        receiver = generation = None
        presentation = None
        while time.monotonic() - started < args.seconds:
            frame = session.preview(
                separate=True,
                codec="h264-native",
                request_keyframe=time.monotonic() >= next_decode,
            )
            now = time.monotonic()
            report["maximum_packet_gap_ms"] = max(
                report["maximum_packet_gap_ms"], (now - last) * 1000
            )
            last = now
            if receiver is None:
                receiver = frame["receiver_id"]
            if receiver != frame["receiver_id"]:
                raise AssertionError("Media receiver restarted")
            if generation is not None and frame["generation"] != generation:
                raise AssertionError("Encoder generation changed")
            if (
                frame["capture_source"] != "display-surface"
                or frame["capture_cpu_pixel_copy"] is not False
            ):
                raise AssertionError("Expected the direct display surface")
            if frame.get("idle"):
                if frame.get("binary"):
                    raise AssertionError("Idle response contained pixels")
                report["idle_packets"] += 1
            else:
                if generation is None:
                    if not frame["key_frame"]:
                        raise AssertionError("First encoded frame was not a key frame")
                    generation = frame["generation"]
                if frame["generation"] != generation:
                    raise AssertionError("Encoder generation changed")
                if (
                    presentation is not None
                    and frame["presentation_us"] <= presentation
                ):
                    raise AssertionError("Frame timestamps did not increase")
                presentation = frame["presentation_us"]
                report["frames"] += 1
                report["encoded_bytes"] += len(frame["binary"])
                if now >= next_decode and frame["key_frame"]:
                    decode(frame)
                    next_decode = time.monotonic() + 60
            if now >= next_sample:
                observed = session.query(
                    {"identifier": profile["app_id"] + ":id/gesture_pad"},
                    cache=False,
                    timeout=5,
                )
                label = session.unique(observed).get("label", "")
                before = re.fullmatch(r"contacts=\d+ completed=(\d+)", label)
                count = int(before.group(1)) if before else 0
                session.action({"op": "tap"}, observed, uuid.uuid4().hex, 5)
                after = session.unique(
                    session.query(
                        {"identifier": profile["app_id"] + ":id/gesture_pad"},
                        cache=False,
                        timeout=5,
                    )
                )
                if after.get("label") != f"contacts=1 completed={count + 1}":
                    raise AssertionError("Native tap business result differs")
                report["verified_taps"] += 1
                processes = {name: memory(name) for name in initial}
                for name, value in processes.items():
                    if value["pid"] != initial[name]["pid"]:
                        raise AssertionError("Source process restarted")
                    if (
                        value["pss_kb"] > 500000
                        or value["pss_kb"] - initial[name]["pss_kb"] > 65536
                    ):
                        raise AssertionError(
                            "Native memory exceeded the declared soak gate"
                        )
                report["samples"].append(
                    {"elapsed_seconds": round(now - started, 3), "processes": processes}
                )
                report.update(receiver_id=receiver, generation=generation)
                save()
                print(
                    json.dumps(
                        {
                            "elapsed_seconds": report["elapsed_seconds"],
                            "frames": report["frames"],
                            "idle_packets": report["idle_packets"],
                            "verified_taps": report["verified_taps"],
                        }
                    ),
                    flush=True,
                )
                next_sample = time.monotonic() + 30
        if not generation or not report["decoded_samples"]:
            raise AssertionError("No actual encoded/decoded frame")
        if report["verified_taps"] < max(1, args.seconds // 40) or len(
            report["decoded_samples"]
        ) < max(1, args.seconds // 90):
            raise AssertionError(
                "Insufficient business and real decoder samples over the requested window"
            )
        report["passed"] = True
    except BaseException as error:
        report["error"] = type(error).__name__ + ": " + str(error)
        raise
    finally:
        try:
            if session:
                session.close()
                report["owned_processes_stopped"] = all(
                    p.poll() is not None for p in session.device.processes
                )
        finally:
            report["forwards_restored"] = (
                device.adb("forward", "--list").splitlines() == baseline
            )
            report["passed"] &= (
                report.get("owned_processes_stopped", False)
                and report["forwards_restored"]
            )
            save()
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "seconds": report["elapsed_seconds"],
                "frames": report["frames"],
            }
        ),
        flush=True,
    )
    if not report["passed"]:
        raise AssertionError("Media cleanup verification failed")


if __name__ == "__main__":
    main()
