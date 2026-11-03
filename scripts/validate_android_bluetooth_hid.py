"""Real Android-to-iPhone HID acceptance, restricted to the owned IATest Lab."""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

from iatest.application.mobile_service import execute_mobile_plan
from iatest.core.config import init_project
from iatest.execution.control import RunControl
from iatest.mobile.bluetooth_hid import validate_config
from iatest.mobile.runtime import MobileSession
from iatest.mobile.store import MobileStore, validate_profile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection", type=Path, required=True)
    parser.add_argument("--ios-profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--project", default="bluetooth-hid-acceptance")
    parser.add_argument(
        "--pinyin",
        action="store_true",
        help="Require a prepared Chinese Pinyin keyboard; observe the candidate and commit with HID Space",
    )
    parser.add_argument("--candidate-commit", choices=["space", "target_click"], default="space",
                        help="Pinyin candidate submission; target_click binds the owned Lab's exact candidate cell")
    args = parser.parse_args()
    if not args.pinyin and args.candidate_commit != "space":
        parser.error("--candidate-commit requires --pinyin")
    connection = json.loads(args.connection.read_text())
    validate_config(connection)
    profile = json.loads(args.ios_profile.read_text())
    if (
        profile.get("app_id") != "dev.iatest.mobile.lab"
        or profile.get("platform") != "ios"
        or profile.get("simulator")
    ):
        parser.error("Only the owned physical iPhone IATest Lab can be exercised")
    profile.update(
        id="ios-bluetooth-hid",
        ios_input_backend="android_bluetooth_hid",
        bluetooth_hid=connection,
    )
    validate_profile(profile)
    root, output = args.root.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    init_project(root, args.project)
    report = {
        "passed": False,
        "model_calls": 0,
        "all_test_input_backend": "android_bluetooth_hid",
        "xctest_input_used": False,
        "input_mode": "pinyin_" + args.candidate_commit if args.pinyin else "ascii",
        "runs": [],
    }
    # A failed new attempt must not leave a previous successful report in place.
    (output / "acceptance.json").write_text(json.dumps(report, indent=2))
    session = None
    try:
        session = MobileSession(
            profile,
            root=root,
            project=args.project,
            control=RunControl(timeout_seconds=120),
        )
        session.action({"op": "launch"}, None, "bluetooth-lab-start", 20)
        observed = session.query(
            {"identifier": "nickname_field"}, timeout=20, cache=False
        )
        node = session.unique(observed)
        geometry = session.device.semantic.request(
            "hid_validate",
            {"ref": node["ref"], "state_version": observed["state_version"]},
            timeout=5,
        )
        calibration = {
            "width": geometry["width"],
            "height": geometry["height"],
            "rotation": geometry["orientation"],
        }
        if (
            connection.get("pointer_calibration")
            and connection["pointer_calibration"] != calibration
        ):
            raise ValueError(
                "Configured pointer calibration differs from the current iPhone screen"
            )
        connection["pointer_calibration"] = calibration
        report["calibration"] = {**calibration, "business_verified": False}
        (output / "acceptance.json").write_text(json.dumps(report, indent=2))
    finally:
        if session:
            session.close()
    MobileStore(root, args.project).save_profile(profile)

    def hid(command, target=None):
        return {
            "op": "hid",
            "hid_command": command,
            **({"target": {"identifier": target}} if target else {}),
        }

    def click(identifier):
        return hid({"op": "target_click"}, identifier)

    def expect(identifier, expected):
        return {
            "op": "expect",
            "target": {"identifier": identifier},
            "attribute": "text",
            "expected": expected,
            "timeout_ms": 10000,
        }

    for index in range(2):
        suffix = str(uuid.uuid4().int % 10**12).zfill(12)
        marker = (
            "中文" + suffix if args.pinyin else "android-bt-" + uuid.uuid4().hex[:16]
        )
        text_steps = [hid({"op": "text", "text": marker})]
        if args.pinyin:
            text_steps = [
                hid(
                    {
                        "op": "ime_text",
                        "text": "中文",
                        "pinyin": "zhongwen",
                        "focus": "target_click",
                        "candidate_commit": args.candidate_commit,
                        **({"candidate_target": {"type": "cell", "identifier": "中文"}}
                           if args.candidate_commit == "target_click" else {}),
                        "expected_value": "中文",
                    },
                    "nickname_field",
                ),
                expect("nickname_field", "中文"),
                hid({"op": "text", "text": suffix}),
            ]
        plan = {
            "id": "bluetooth-hid-"
            + ("pinyin" if args.pinyin else "ascii")
            + "-save-"
            + str(index),
            "kind": "ios",
            "budget": {"timeout_seconds": 180, "max_actions": 20, "max_queries": 100},
            "spec": {
                "profile": profile["id"],
                "steps": [
                    {"op": "launch"},
                    click("reset_button"),
                    expect("saved_status", "Not saved"),
                    click("nickname_field"),
                    *text_steps,
                    expect("nickname_field", marker),
                    click("save_button"),
                    expect("saved_status", "Saved: " + marker),
                    {"op": "screenshot"},
                    {"op": "terminate"},
                    {"op": "launch"},
                    expect("nickname_field", marker),
                    expect("saved_status", "Saved: " + marker),
                ],
            },
        }
        MobileStore(root, args.project).save_plan(plan)
        result = execute_mobile_plan(root, args.project, plan["id"])
        report["runs"].append({"plan": plan, "result": result})
        (output / "acceptance.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2)
        )
        if result["verdict"] != "passed":
            raise RuntimeError(
                "Bluetooth HID real-device replay failed; inspect acceptance.json; no input retry"
            )
    report["passed"] = report["calibration"]["business_verified"] = True
    (output / "acceptance.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2)
    )
    print(
        json.dumps(
            {
                "passed": True,
                "runs": 2,
                "xctest_input_used": False,
                "output": str(output / "acceptance.json"),
            }
        )
    )


if __name__ == "__main__":
    main()
