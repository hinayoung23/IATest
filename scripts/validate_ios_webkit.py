"""Actual owned-Lab hybrid UI acceptance; no model or device substitutes."""

import argparse
import json
import time
from pathlib import Path

from iatest.application.testing_service import execute_test_plan
from iatest.core.config import init_project
from iatest.core.sandbox import origin
from iatest.execution.control import RunControl
from iatest.mobile.ios_capture import resolve_pid
from iatest.mobile.runtime import MobileSession
from iatest.mobile.store import MobileStore
from iatest.mobile.webkit import listing


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True)
    parser.add_argument("--xctestrun", type=Path, required=True)
    parser.add_argument("--inspector", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--project", default="ios-webkit-acceptance")
    parser.add_argument(
        "--contexts",
        action="store_true",
        help="Validate nested cross-origin frames, open/closed Shadow roots and mapped pages",
    )
    args = parser.parse_args()
    if args.output.exists():
        args.output.with_name(args.output.stem + "-attempt-" + str(time.time_ns()) + args.output.suffix).write_bytes(args.output.read_bytes())
    root = Path.cwd()
    init_project(root, args.project)
    profile = {
        "id": "ios-webkit",
        "platform": "ios",
        "device_id": args.device,
        "app_id": "dev.iatest.mobile.lab",
        "xctestrun": str(args.xctestrun.resolve()),
        "allow_webview": True,
    }
    MobileStore(root, args.project).save_profile(profile)
    control = RunControl(timeout_seconds=180)
    session = MobileSession(profile, root=root, project=args.project, control=control)
    try:
        session.action({"op": "launch"}, None, operation_id="lab-start", timeout=30)
        found = session.query({"identifier": "hybrid_button"}, timeout=5)
        session.action({"op": "tap"}, found, operation_id="lab-hybrid", timeout=20)
        pid = resolve_pid(profile, control)
        deadline = time.monotonic() + 15
        while True:
            devices = [
                d for d in listing(args.inspector) if d["deviceId"] == args.device
            ]
            pages = listing("http://" + devices[0]["url"]) if len(devices) == 1 else []
            pages = [
                p
                for p in pages
                if p.get("appId") == "PID:" + str(pid)
                and p["url"].startswith("http://127.0.0.1:")
            ]
            if len(pages) == 1:
                break
            if time.monotonic() > deadline:
                raise ValueError("Owned Lab WKWebView was not inspectable")
            control.wait(0.25)
        allowed_origin = origin(pages[0]["url"])
    finally:
        session.close()
    plan = {
        "id": "ios-hybrid-save",
        "kind": "ios",
        "data_generation": {
            "nickname": {
                "kind": "string",
                "minLength": 16,
                "maxLength": 20,
                "unique": True,
            }
        },
        "budget": {"timeout_seconds": 180, "max_actions": 10, "max_queries": 80},
        "spec": {
            "profile": profile["id"],
            "webview": {
                "allowed_origins": [allowed_origin],
                "inspector_url": args.inspector,
                "native_target": {"type": "webView", "identifier": "hybrid_webview"},
            },
            "steps": [
                {
                    "op": "web",
                    "web_step": {
                        "op": "fill",
                        "target": {"id": "nickname"},
                        "value": {"$value": "nickname"},
                    },
                },
                {
                    "op": "web",
                    "web_step": {
                        "op": "expect",
                        "assert": "value",
                        "target": {"id": "nickname"},
                        "expected": {"$value": "nickname"},
                    },
                },
                {"op": "web", "web_step": {"op": "click", "target": {"id": "save"}}},
                {
                    "op": "web",
                    "web_step": {
                        "op": "expect",
                        "assert": "text",
                        "target": {"id": "status"},
                        "expected": {"$value": "nickname"},
                    },
                },
            ],
        },
    }
    if args.contexts:
        frame = [
            {"selector": "#outer", "origin": allowed_origin},
            {
                "selector": "#inner",
                "origin": allowed_origin.replace("127.0.0.1", "localhost"),
            },
        ]
        steps = []

        def web(step):
            return {"op": "web", "web_step": step}

        def start():
            return [
                web({"op": "navigate", "url": allowed_origin + "/contexts"}),
                web(
                    {
                        "op": "wait",
                        "target": {"id": "contexts_ready"},
                        "assert": "text",
                        "expected": "Contexts Ready",
                        "timeout_seconds": 10,
                    }
                ),
            ]

        for context, field, button, status in (
            ({"frame": frame}, "frame_nickname", "frame_save", "frame_status"),
            (
                {"shadow": ["#open-host"]},
                "shadow_nickname",
                "shadow_save",
                "shadow_status",
            ),
            (
                {"shadow": ["#closed-host"]},
                "shadow_nickname",
                "shadow_save",
                "shadow_status",
            ),
        ):
            steps += start()
            steps += [
                web(
                    {
                        "op": "fill",
                        "target": {**context, "id": field},
                        "value": {"$value": "nickname"},
                    }
                ),
                web(
                    {
                        "op": "expect",
                        "target": {**context, "id": field},
                        "assert": "value",
                        "expected": {"$value": "nickname"},
                    }
                ),
                web({"op": "click", "target": {**context, "id": button}}),
                web(
                    {
                        "op": "expect",
                        "target": {**context, "id": status},
                        "assert": "text",
                        "expected": {"$value": "nickname"},
                    }
                ),
            ]
        steps += start()
        steps += [
            web({"op": "click", "target": {"id": "open_secondary"}}),
            web(
                {
                    "op": "expect_popup",
                    "page": "secondary",
                    "url": allowed_origin + "/secondary",
                    "timeout_seconds": 10,
                }
            ),
            web(
                {
                    "op": "wait",
                    "target": {"id": "secondary_ready", "page": "secondary"},
                    "assert": "text",
                    "expected": "Secondary Ready",
                }
            ),
            {"op": "tap", "target": {"identifier": "hybrid_main_button"}},
            web({"op": "switch_page", "page": "main"}),
            web(
                {
                    "op": "expect",
                    "target": {"id": "contexts_ready", "page": "main"},
                    "assert": "text",
                    "expected": "Contexts Ready",
                }
            ),
        ]
        plan["id"] = "ios-hybrid-contexts"
        plan["budget"] = {"timeout_seconds": 480, "max_actions": 50, "max_queries": 200}
        plan["spec"]["steps"] = steps
        plan["spec"]["webview"]["allowed_origins"].append(
            allowed_origin.replace("127.0.0.1", "localhost")
        )
        plan["spec"]["webview"]["pages"] = {
            "main": {
                "page_id": pages[0]["webSocketDebuggerUrl"].rsplit("/", 1)[-1],
                "native_target": {"identifier": "hybrid_webview", "type": "webView"},
            },
            "secondary": {
                "url": allowed_origin + "/secondary",
                "native_target": {
                    "identifier": "hybrid_secondary_webview",
                    "type": "webView",
                },
            },
        }
    results = [execute_test_plan(root, args.project, plan) for _ in range(2)]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps({"device": args.device, "plan": plan, "results": results}, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "verdicts": [r["verdict"] for r in results],
                "output": str(args.output),
                "physical_device": True,
            }
        )
    )
    return 0 if all(r["verdict"] == "passed" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
