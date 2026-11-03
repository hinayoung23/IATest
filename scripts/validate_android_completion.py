"""Physical Android acceptance against the framework-owned Lab; never business Apps."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from iatest.application.testing_service import execute_test_plan
from iatest.execution.control import RunControl
from iatest.mobile.delivery import fetch_app, prepare_app
from iatest.mobile.devices import NativeDevice
from iatest.mobile.store import MobileStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--project", default="android-completion-20261020")
    parser.add_argument("--lab-artifact", default="lab-v2")
    parser.add_argument("--artifacts", type=Path, default=Path("native/mobile/build/android"))
    parser.add_argument(
        "--phase",
        choices=("delivery", "native", "system", "webview", "performance", "performance-negative", "stress"),
        required=True,
    )
    args = parser.parse_args()
    root, directory = Path.cwd(), args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    java = "/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home"
    os.environ.setdefault("JAVA_HOME", java)
    os.environ["PATH"] = java + "/bin:" + os.environ.get("PATH", "")
    profile = {
        "id": "android-lab",
        "platform": "android",
        "device_id": args.serial,
        "app_id": "dev.iatest.mobile.lab",
        "artifact_dir": str(args.artifacts.resolve()),
        "allowed_apps": ["dev.iatest.mobile.companion"],
        "allowed_permissions": ["android.permission.CAMERA"],
        "allow_clipboard": True,
        "allow_webview": True,
        "video_capture": "mirror",
    }
    store = MobileStore(root, args.project)
    store.save_profile(profile)
    store.save_profile({**profile, "id": "android-lab-ime", "allow_ime_setup": True})
    device = NativeDevice(profile, "baseline")
    baseline = {
        "ime": device.adb(
            "shell", "settings", "get", "secure", "default_input_method"
        ).strip(),
        "enabled_imes": device.adb(
            "shell", "settings", "get", "secure", "enabled_input_methods"
        ).strip(),
        "forwards": device.adb("forward", "--list").splitlines(),
    }
    if args.phase != "delivery":
        baseline["camera_granted"] = device.android_permission_state(profile["app_id"], "android.permission.CAMERA")
    report = {
        "physical_device": True,
        "phase": args.phase,
        "results": [],
        "passed": False,
    }
    path = directory / (args.phase + ".json")
    previous = json.loads(path.read_text()) if path.exists() else {}
    if previous:
        (directory / (args.phase + "-attempt-" + str(time.time_ns()) + ".json")).write_text(path.read_text())

    def save():
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    def target(name):
        return {"identifier": profile["app_id"] + ":id/" + name}

    def expect(name, text=None, **extra):
        return {
            "op": "expect",
            "target": target(name),
            **(
                {"attribute": "text", "expected": text}
                if text is not None
                else {"expected": True}
            ),
            **extra,
        }

    def tap(name):
        return {"op": "tap", "target": target(name)}

    def run(name, steps, *, profile_id="android-lab", **options):
        plan = {
            "id": name,
            "kind": "android",
            "budget": {"timeout_seconds": 120, "max_actions": 100, "max_queries": 400},
            "spec": {"profile": profile_id, "steps": steps, **options},
        }
        store.save_plan(plan)
        result = execute_test_plan(root, args.project, plan)
        report["results"].append(result)
        save()
        print(
            json.dumps(
                {
                    "phase": args.phase,
                    "plan": name,
                    "verdict": result["verdict"],
                    "error": result.get("error", ""),
                }
            ),
            flush=True,
        )
        assert result["verdict"] == "passed", result.get("error")
        return result

    try:
        if args.phase == "delivery":
            artifacts = {}
            for name, package in (
                ("lab-v1", profile["app_id"]),
                ("lab-v2", profile["app_id"]),
                ("companion", "dev.iatest.mobile.companion"),
            ):
                artifacts[name] = fetch_app(
                    root,
                    args.project,
                    {
                        "id": name,
                        "platform": "android",
                        "app_id": package,
                        "path": str(directory / name / "iatest-lab.apk"),
                    },
                )
            report["artifacts"] = artifacts
            installed = (
                "package:" + profile["app_id"]
                in device.adb(
                    "shell", "pm", "list", "packages", profile["app_id"]
                ).splitlines()
            )
            if installed:
                result = previous.get("fresh_install", {})
                assert result.get("state") == "prepared" and "versionCode=1 " in device.adb("shell", "dumpsys", "package", profile["app_id"]), "An existing Lab is never automatically uninstalled; continuation requires this run's successful version-1 installation"
            else:
                result = prepare_app(root, args.project, {"profile": profile["id"], "artifact_id": "lab-v1", "mode": "fresh"}, RunControl(timeout_seconds=120))
            report["fresh_install"] = result
            save()
            assert result["state"] == "prepared", result
            value = "upgrade-" + str(time.time_ns())
            report["seed_value"] = value
            run(
                "android-upgrade-seed",
                [
                    expect("ready_label", "Ready"),
                    {
                        "op": "text",
                        "target": target("nickname_field"),
                        "value": value,
                        "mode": "semantic",
                    },
                    tap("save_button"),
                    expect("saved_status", "Saved: " + value),
                ],
            )
            result = prepare_app(
                root,
                args.project,
                {"profile": profile["id"], "artifact_id": "lab-v2", "mode": "upgrade"},
                RunControl(timeout_seconds=120),
            )
            report["upgrade_install"] = result
            save()
            assert result["state"] == "prepared", result
            run(
                "android-upgrade-preserves-data",
                [
                    expect("version_label", "Build 2"),
                    expect("nickname_field", value),
                    expect("saved_status", "Saved: " + value),
                ],
            )
            store.save_profile(
                {
                    **profile,
                    "id": "android-companion",
                    "app_id": "dev.iatest.mobile.companion",
                }
            )
            result = prepare_app(
                root,
                args.project,
                {
                    "profile": "android-companion",
                    "artifact_id": "companion",
                    "mode": "upgrade"
                    if device.adb(
                        "shell", "pm", "list", "packages", "dev.iatest.mobile.companion"
                    ).strip()
                    else "fresh",
                },
                RunControl(timeout_seconds=120),
            )
            report["companion_install"] = result
            assert result["state"] == "prepared", result
        elif args.phase == "native":
            for index in range(2):
                value = "native-" + str(time.time_ns())
                run(
                    "android-native-save-" + str(index),
                    [
                        {"op": "launch"},
                        expect("ready_label", "Ready"),
                        {
                            "op": "text",
                            "target": target("nickname_field"),
                            "value": value,
                            "mode": "semantic",
                        },
                        tap("save_button"),
                        expect("saved_status", "Saved: " + value),
                        {"op": "terminate"},
                        {"op": "launch"},
                        expect("nickname_field", value),
                        expect("saved_status", "Saved: " + value),
                    ],
                )
            run(
                "android-ime-candidate",
                [
                    {"op": "launch"},
                    tap("reset_button"),
                    tap("nickname_field"),
                    {
                        "op": "ime",
                        "target": target("nickname_field"),
                        "ime_backend": "input_connection",
                        "ime_events": [
                            {
                                "op": "compose",
                                "text": "zhongwen",
                                "candidates": ["中文", "中问"],
                            },
                            {"op": "candidate", "index": 0},
                            {
                                "op": "expect",
                                "composing": "",
                                "before_cursor": "中文",
                                "marked": False,
                            },
                        ],
                    },
                    expect("nickname_field", "中文"),
                    tap("save_button"),
                    expect("saved_status", "Saved: 中文"),
                ],
                profile_id="android-lab-ime",
            )
            run(
                "android-ime-cancel",
                [
                    tap("reset_button"),
                    tap("nickname_field"),
                    {
                        "op": "ime",
                        "target": target("nickname_field"),
                        "ime_backend": "input_connection",
                        "ime_events": [
                            {"op": "compose", "text": "temporary"},
                            {"op": "cancel"},
                            {
                                "op": "expect",
                                "composing": "",
                                "before_cursor": "",
                                "marked": False,
                            },
                        ],
                    },
                    tap("save_button"),
                    expect("saved_status", "Saved: "),
                ],
                profile_id="android-lab-ime",
            )
            contacts = [
                {
                    "id": i,
                    "points": [
                        {"t_ms": i * 50, "x": 0.25 + i * 0.2, "y": 0.5},
                        {"t_ms": 400 + i * 50, "x": 0.15 + i * 0.3, "y": 0.5},
                    ],
                }
                for i in range(3)
            ]
            run(
                "android-three-finger",
                [
                    {"op": "terminate"},
                    {"op": "launch"},
                    {
                        "op": "gesture",
                        "target": target("gesture_pad"),
                        "contacts": contacts,
                    },
                    {
                        "op": "expect",
                        "target": target("gesture_pad"),
                        "attribute": "label",
                        "expected": "contacts=3 completed=1",
                    },
                ],
            )
        elif args.phase == "system":
            run(
                "android-clipboard",
                [
                    {"op": "launch"},
                    {"op": "clipboard_set", "value": "iatest-owned-clipboard"},
                    {"op": "clipboard_expect", "expected": "iatest-owned-clipboard"},
                    tap("clipboard_button"),
                    expect("clipboard_status", "Clipboard: iatest-owned-clipboard"),
                ],
            )
            run(
                "android-permission",
                [
                    {
                        "op": "permission",
                        "permission": "android.permission.CAMERA",
                        "granted": True,
                    },
                    expect("ready_label", "Ready"),
                ],
            )
            run(
                "android-app-transition",
                [
                    {"op": "switch_app", "app_id": "dev.iatest.mobile.companion"},
                    {
                        "op": "expect",
                        "target": {
                            "identifier": "dev.iatest.mobile.companion:id/ready_label"
                        },
                        "expected": True,
                    },
                    {"op": "switch_app", "app_id": profile["app_id"]},
                    expect("ready_label", "Ready"),
                ],
            )
        elif args.phase == "webview":
            for index in range(2):
                value = "web-" + str(time.time_ns())
                run(
                    "android-webview-save-" + str(index),
                    [
                        {"op": "launch"},
                        {**tap("hybrid_button"), "tap_backend": "accessibility"},
                        expect("hybrid_webview"),
                        {
                            "op": "web",
                            "web_step": {
                                "op": "fill",
                                "target": {"id": "nickname"},
                                "value": value,
                            },
                        },
                        {
                            "op": "web",
                            "web_step": {
                                "op": "expect",
                                "assert": "value",
                                "target": {"id": "nickname"},
                                "expected": value,
                            },
                        },
                        {
                            "op": "web",
                            "web_step": {"op": "click", "target": {"id": "save"}},
                        },
                        {
                            "op": "web",
                            "web_step": {
                                "op": "expect",
                                "assert": "text",
                                "target": {"id": "status"},
                                "expected": value,
                            },
                        },
                    ],
                    webview={"allowed_origins": ["http://127.0.0.1:18739"]},
                )
                device.adb("shell", "am", "force-stop", profile["app_id"])
        elif args.phase in {"performance", "performance-negative"}:
            steps = [
                tap("jank_button"),
                {"op": "wait", "target": target("ready_label"), "attribute": "text", "expected": "Ready", "timeout_ms": 10000},
                {
                    "op": "swipe",
                    "target": target("item_list"),
                    "direction": "up",
                    "duration_ms": 600,
                },
                expect("ready_label", "Ready"),
            ]
            negative = args.phase == "performance-negative"
            report["expected_verdict"] = "failed" if negative else "passed"
            for mode in (("cold",) if negative else ("cold", "warm")):
                plan = {
                    "id": "android-performance-" + mode,
                    "kind": "client_performance",
                    "budget": {"timeout_seconds": 180},
                    "spec": {
                        "platform": "android",
                        "startup": mode,
                        "iterations": 2,
                        "warmup": 0,
                        "plan": {
                            "id": "android-animated-ui",
                            "kind": "android",
                            "spec": {"profile": profile["id"], "steps": steps},
                        },
                        "thresholds": {
                            "max_android_start_total_ms": 10000,
                            "max_memory_pss_kb": 500000,
                            "max_cpu_percent": 500,
                            "max_jank_ratio": 1,
                        },
                    },
                }
                if mode == "warm":
                    del plan["spec"]["thresholds"]["max_android_start_total_ms"]
                    plan["spec"]["thresholds"]["max_android_start_wait_ms"] = 10000
                    plan["spec"]["telemetry"] = {"artifact_id": args.lab_artifact}
                    plan["spec"]["thresholds"].update(
                        max_device_battery_level_percent=100,
                        max_device_thermal_status=6,
                    )
                if negative:
                    plan["id"] += "-reject-zero-memory"
                    plan["spec"]["iterations"] = 1
                    plan["spec"]["thresholds"] = {"max_memory_pss_kb": 0}
                result = execute_test_plan(root, args.project, plan)
                report["results"].append(result)
                save()
                print(
                    json.dumps(
                        {
                            "plan": plan["id"],
                            "verdict": result["verdict"],
                            "error": result.get("error", ""),
                        }
                    ),
                    flush=True,
                )
                assert result["verdict"] == report["expected_verdict"], result.get("error")
                if negative:
                    assert result["lifecycle"] == "completed"
                    gate, = result["steps"]
                    assert gate["gate"] == "max_memory_pss_kb" and gate["assessed"] is True
                    assert gate["actual"] > 0 and gate["expected"] == 0 and gate["passed"] is False
                    assert all(r["verdict"] == "passed" for r in result["metrics"]["repetitions"])
        elif args.phase == "stress":
            plan = {
                "id": "android-300-interactions",
                "kind": "android",
                "budget": {
                    "timeout_seconds": 300,
                    "max_actions": 350,
                    "max_queries": 800,
                },
                "spec": {
                    "profile": profile["id"],
                    "steps": [
                        {"op": "terminate"},
                        {"op": "launch"},
                        *[tap("gesture_pad") for _ in range(300)],
                        {
                            "op": "expect",
                            "target": target("gesture_pad"),
                            "attribute": "label",
                            "expected": "contacts=1 completed=300",
                        },
                    ],
                },
            }
            result = execute_test_plan(root, args.project, plan)
            report["results"].append(result)
            save()
            assert result["verdict"] == "passed", result.get("error")
        report["passed"] = True
    except Exception as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        report["cleanup"] = {
            "ime_restored": device.adb(
                "shell", "settings", "get", "secure", "default_input_method"
            ).strip()
            == baseline["ime"],
            "enabled_imes_restored": device.adb(
                "shell", "settings", "get", "secure", "enabled_input_methods"
            ).strip()
            == baseline["enabled_imes"],
            "forwards_restored": device.adb("forward", "--list").splitlines()
            == baseline["forwards"],
        }
        if "camera_granted" in baseline:
            report["cleanup"]["camera_permission_restored"] = device.android_permission_state(profile["app_id"], "android.permission.CAMERA") == baseline["camera_granted"]
        report["passed"] = report["passed"] and all(report["cleanup"].values())
        save()
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
