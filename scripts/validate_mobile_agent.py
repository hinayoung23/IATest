"""Real configured-model acceptance against explicit synthetic UI observations.

This validates the planning/tool/contract path, never physical-device execution.
No test double replaces the model. A synthetic device boundary is explicitly used.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from iatest.application.mobile_service import draft_mobile_plan
from iatest.core.config import load_config
from iatest.core.env import load_dotenv
from iatest.mobile.store import MobileStore


class ObservationFixture:
    def __init__(self):
        self.profile = {
            "id": "acceptance-phone",
            "platform": "android",
            "device_id": "synthetic-ui-fixture",
            "app_id": "com.example.shop",
        }
        self.mode = "automatic"
        import threading

        self.lock = threading.RLock()
        self.queries = []
        self.capabilities = {
            "actions": ["query", "tap", "text"],
            "input_modes": ["semantic"],
        }
        from types import SimpleNamespace

        self.device = SimpleNamespace(
            semantic=SimpleNamespace(capabilities=self.capabilities),
            input=SimpleNamespace(capabilities=self.capabilities),
        )

    def query(self, target, **kwargs):
        self.queries.append(target)
        nodes = [
            {
                "ref": "login-ref",
                "identifier": "com.example.shop:id/login",
                "label": "登录",
                "text": "登录",
                "type": "android.widget.Button",
                "bounds": [10, 100, 200, 160],
                "enabled": True,
            },
            {
                "ref": "status-ref",
                "identifier": "com.example.shop:id/status",
                "label": "状态",
                "text": "未登录",
                "type": "android.widget.TextView",
                "bounds": [10, 200, 200, 260],
                "enabled": True,
            },
        ]
        found = [
            n
            for n in nodes
            if all(
                n.get(k) == v
                for k, v in target.items()
                if k in {"identifier", "text", "label", "type"}
            )
        ]
        return {
            "complete": True,
            "match_count": len(found),
            "candidates": found[: kwargs.get("limit", 3)],
            "state_version": 1,
        }


def checks_for_plan(plan, profile_id):
    steps = plan["spec"]["steps"]
    last_tap = max((i for i, s in enumerate(steps) if s["op"] == "tap"), default=-1)

    def requested_outcome(step):
        if step["op"] != "expect":
            return False
        if step.get("attribute", "exists") == "exists":
            return (
                step.get("expected") is True
                and step.get("target", {}).get("text") == "登录成功"
                and step.get("operator", "eq") == "eq"
            )
        return (
            step.get("attribute") in {"text", "value", "label"}
            and step.get("expected") == "登录成功"
            and step.get("operator", "eq") == "eq"
        )

    return {
        "platform_unchanged": plan["kind"] == "android",
        "profile_unchanged": plan["spec"]["profile"] == profile_id,
        "tap_planned": last_tap >= 0,
        "requested_assertion_retained": any(
            requested_outcome(s) for s in steps[last_tap + 1 :]
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/private/tmp/iatest-mobile-agent-acceptance.json"),
    )
    parser.add_argument(
        "--recheck",
        type=Path,
        help="Recheck an existing actual-model response without another model call",
    )
    args = parser.parse_args()
    if args.recheck:
        report = json.loads(args.recheck.read_text())
        report["checks"] = checks_for_plan(report["plan"], "acceptance-phone")
        report["rechecked_without_new_model_call"] = True
        args.recheck.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(
            json.dumps(
                {"checks": report["checks"], "new_model_calls": 0}, ensure_ascii=False
            )
        )
        if not all(report["checks"].values()):
            raise SystemExit(1)
        return
    if not args.live:
        raise SystemExit("Use --live to call the configured case_generator model")
    load_dotenv(args.root)
    config = load_config(args.root)
    fixture = ObservationFixture()
    with tempfile.TemporaryDirectory(prefix="iatest-mobile-model-") as directory:
        root = Path(directory)
        store = MobileStore(root, "acceptance")
        store.save_profile(fixture.profile)
        with (
            patch("iatest.core.config.load_config", return_value=config),
            patch("iatest.mobile.runtime.SESSIONS.get", return_value=fixture),
        ):
            result = draft_mobile_plan(
                root,
                "acceptance",
                fixture.profile["id"],
                "在当前页面点击显示文本为‘登录’的按钮，然后验证状态显示‘登录成功’。只规划，不执行操作。",
            )
        checks = checks_for_plan(result["plan"], fixture.profile["id"])
        report = {
            "model": "actual configured case_generator endpoint",
            "device": "synthetic observation fixture; no phone operation",
            "checks": checks,
            "query_count": len(fixture.queries),
            **result,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "checks": checks,
                    "usage": result["usage"],
                    "query_count": len(fixture.queries),
                    "output": str(args.output),
                },
                ensure_ascii=False,
            )
        )
        if not all(checks.values()):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
