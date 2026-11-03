"""Timed physical Android Lab soak, using finite native/media sessions."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
import uuid
from pathlib import Path

from iatest.core.files import atomic_write_text
from iatest.execution.control import RunControl
from iatest.mobile.devices import NativeDevice
from iatest.mobile.protocol import ObservationRejected
from iatest.mobile.runtime import MobileSession, ObservationIncomplete
from iatest.mobile.store import MobileStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--profile", default="android-lab")
    parser.add_argument("--minutes", type=int, default=60, choices=range(1, 121))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    profile = MobileStore(Path.cwd(), args.project).get("profile", args.profile)
    if profile["platform"] != "android" or profile["app_id"] != "dev.iatest.mobile.lab":
        parser.error("Only the owned physical Android Lab is accepted")
    if profile.get("managed_emulator"):
        parser.error("Physical-device soak must not substitute an emulator")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        parser.error("Use a new output file to retain previous evidence")
    device = NativeDevice(profile, "soak-baseline")
    baseline = {
        "ime": device.adb("shell", "settings", "get", "secure", "default_input_method").strip(),
        "enabled": device.adb("shell", "settings", "get", "secure", "enabled_input_methods").strip(),
        "forwards": device.adb("forward", "--list").splitlines(),
    }
    report = {"passed": False, "physical_device": True, "minutes": args.minutes,
              "cycles": [], "actions_verified": 0, "saves_verified": 0,
              "encoded_frames": 0, "idle_packets": 0, "encoded_bytes": 0,
              "max_roundtrip_ms": 0, "unexpected_replays": 0, "before_dispatch_refreshes": 0,
              "planned_session_seconds": 90, "model_calls": 0}
    report.update(save_backend="pointer", pad_backend="pointer")
    began = time.monotonic()
    session, process_id = None, None

    def save():
        report["elapsed_seconds"] = round(time.monotonic() - began, 3)
        atomic_write_text(args.output, json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    def query(name, timeout=3):
        value = session.query({"identifier": profile["app_id"] + ":id/" + name}, cache=False, timeout=timeout)
        report["last_query"] = {"target": name, "observation": value}
        return value

    def unique(name):
        end = time.monotonic() + 5
        while True:
            observed = query(name, max(.001, min(3, end-time.monotonic())))
            try:
                return observed, session.unique(observed)
            except ObservationIncomplete:
                if time.monotonic() >= end:
                    raise
                time.sleep(.05)

    def expect(name, text):
        end = time.monotonic() + 5
        while True:
            if time.monotonic() >= end:
                raise AssertionError(f"{name}: business postcondition differs")
            observed = query(name, max(.001, min(3, end-time.monotonic())))
            try:
                node = session.unique(observed)
            except ObservationIncomplete:
                if time.monotonic() >= end:
                    raise
                time.sleep(.05)
                continue
            if node.get("text") == text or node.get("label") == text:
                return node
            if time.monotonic() >= end:
                raise AssertionError(f"{name}: business postcondition differs")
            time.sleep(.05)

    def action(step, name):
        end = time.monotonic() + 5
        for _ in range(5):
            observed, _node = unique(name)
            report["last_action"] = {"step": step, "observation": observed}
            try:
                return session.action(step, observed, uuid.uuid4().hex, max(.001, end-time.monotonic()))
            except ObservationRejected:
                report["before_dispatch_refreshes"] += 1
                if time.monotonic() >= end:
                    raise
        raise ObservationIncomplete("Target remained unstable before dispatch")

    try:
        while time.monotonic() - began < args.minutes * 60:
            cycle_start = time.monotonic()
            session = MobileSession(profile, control=RunControl(timeout_seconds=125))
            if not report["cycles"]:
                session.action({"op": "terminate"}, None, uuid.uuid4().hex, 5)
                session.action({"op": "launch"}, None, uuid.uuid4().hex, 8)
            current_pid = session.device.adb("shell", "pidof", profile["app_id"]).strip()
            if not current_pid.isdigit() or process_id is not None and current_pid != process_id:
                raise AssertionError("Lab process unexpectedly exited or changed")
            process_id = current_pid
            _, pad = unique("gesture_pad")
            matched = re.fullmatch(r"contacts=\d+ completed=(\d+)", pad.get("label", ""))
            if not matched and len(report["cycles"]) > 0:
                raise AssertionError("Gesture state unexpectedly disappeared")
            completed = int(matched.group(1)) if matched else 0
            cycle = {"index": len(report["cycles"]), "pid": int(process_id), "verified_taps": 0,
                     "frames": 0, "first_keyframe": False}
            report["cycles"].append(cycle)
            generation, presentation = None, None
            while time.monotonic() - cycle_start < 90 and time.monotonic() - began < args.minutes * 60:
                tick = time.monotonic()
                action({"op": "tap"}, "gesture_pad")
                completed += 1
                expect("gesture_pad", f"contacts=1 completed={completed}")
                cycle["verified_taps"] += 1
                report["actions_verified"] += 1
                if cycle["verified_taps"] % 10 == 1:
                    value = "soak-" + uuid.uuid4().hex[:16]
                    action({"op": "text", "mode": "semantic", "value": value}, "nickname_field")
                    expect("nickname_field", value)
                    action({"op": "tap"}, "save_button")
                    expect("saved_status", "Saved: " + value)
                    report["saves_verified"] += 1
                packet = session.preview(separate=True, codec="h264-native")
                if packet.get("idle"):
                    assert not packet.get("binary")
                    report["idle_packets"] += 1
                else:
                    if generation is None:
                        assert packet["key_frame"]
                        generation = packet["generation"]
                        cycle["first_keyframe"] = True
                    assert packet["generation"] == generation
                    assert presentation is None or packet["presentation_us"] > presentation
                    assert packet["capture_source"] == "display-surface" and packet["capture_cpu_pixel_copy"] is False
                    presentation = packet["presentation_us"]
                    report["encoded_frames"] += 1
                    report["encoded_bytes"] += len(packet["binary"])
                    cycle["frames"] += 1
                report["max_roundtrip_ms"] = max(report["max_roundtrip_ms"], (time.monotonic() - tick) * 1000)
                save()
                time.sleep(max(0, 2 - (time.monotonic() - tick)))
            session.close()
            assert all(p.poll() is not None for p in session.device.processes)
            session = None
            cycle["seconds"] = round(time.monotonic() - cycle_start, 3)
            memory = device.adb("shell", "dumpsys", "meminfo", profile["app_id"])
            pss = re.search(r"TOTAL PSS:\s*(\d+)", memory)
            if not pss:
                raise AssertionError("Lab memory sample unavailable")
            cycle["memory_pss_kb"] = int(pss.group(1))
            assert cycle["memory_pss_kb"] <= 500000
            assert cycle["verified_taps"] > 0 and cycle["frames"] > 0 and cycle["first_keyframe"]
            save()
            print(json.dumps({"cycle": cycle["index"], "seconds": report["elapsed_seconds"],
                              "actions": report["actions_verified"], "frames": report["encoded_frames"]}), flush=True)
        report["passed"] = True
    except BaseException as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        focus = device.adb("shell", "dumpsys", "window", "displays")
        if any(profile["app_id"] + "/" in line for line in focus.splitlines() if "mCurrentFocus" in line):
            png = subprocess.run(["adb", "-s", profile["device_id"], "exec-out", "screencap", "-p"], capture_output=True, check=True, timeout=10).stdout
            args.output.with_suffix(".png").write_bytes(png)
        raise
    finally:
        if session:
            session.close()
        device.adb("shell", "am", "force-stop", profile["app_id"])
        report["cleanup"] = {
            "ime_restored": device.adb("shell", "settings", "get", "secure", "default_input_method").strip() == baseline["ime"],
            "enabled_imes_restored": device.adb("shell", "settings", "get", "secure", "enabled_input_methods").strip() == baseline["enabled"],
            "forwards_restored": device.adb("forward", "--list").splitlines() == baseline["forwards"],
        }
        processes = device.adb("shell", "ps", "-A", "-o", "NAME").splitlines()
        assert processes and processes[0].strip() == "NAME"
        report["cleanup"]["lab_stopped"] = not any(
            name.strip() == profile["app_id"] or name.strip().startswith(profile["app_id"] + ":")
            for name in processes[1:]
        )
        report["passed"] &= all(report["cleanup"].values())
        save()


if __name__ == "__main__":
    main()
