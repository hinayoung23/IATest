"""Real image-model -> native desktop input -> business readback acceptance.

Uses one owned Lab, randomized visual-only labels and the configured account.
The isolated acceptance project declares image input explicitly; credentials
are reused by environment reference and never copied into reports.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import secrets
import subprocess
import time
from pathlib import Path

from iatest.ai.llm_config import resolve_llm_role
from iatest.application.ui_adapters import DesktopExplorer
from iatest.core.config import init_project, load_config, save_config
from iatest.execution.control import RunControl


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--model-role",
        choices=("master", "case_generator", "analyzer"),
        default="master",
    )
    parser.add_argument("--mac-app", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "passed": False,
        "real_model": True,
        "real_native_input": True,
        "rounds": [],
    }
    app = backend = None
    try:
        model = copy.deepcopy(
            resolve_llm_role(
                load_config(args.config_root).get("llm", {}), args.model_role
            )
        )
        if not os.environ.get(model.get("api_key_env", "")):
            raise ValueError("Configured model credential is unavailable")
        model.update(enabled=True, supports_images=True, thinking={"type": "disabled"})
        model.pop("temperature", None)
        report["model"] = model["model"]
        report["provider"] = model["provider"]
        root = output.parent / ("vision-project-" + secrets.token_hex(6))
        init_project(root, "vision")
        config = load_config(root)
        config["llm"]["case_generator"] = model
        save_config(root, config)
        manifest = output.with_name("vision-manifest-" + secrets.token_hex(4) + ".json")
        app = subprocess.Popen(
            [
                str(args.mac_app.resolve() / "Contents/MacOS/DesktopVisionLab"),
                str(manifest),
            ]
        )
        control = RunControl(timeout_seconds=240)
        backend = DesktopExplorer(
            root,
            "vision",
            {
                "platform": "macos",
                "process_id": app.pid,
                "app_id": "dev.iatest.desktop.vision",
            },
            control,
            100,
        )
        deadline = time.monotonic() + 15
        while True:
            try:
                if (
                    manifest.exists()
                    and backend.find({"identifier": "vision_canvas"})["unique"]
                ):
                    break
            except ValueError:
                pass
            if time.monotonic() > deadline:
                raise TimeoutError("Owned visual Lab did not become ready")
            time.sleep(0.1)
        for n in range(3):
            if n:
                previous = manifest.read_bytes()
                backend.do({"op": "click", "target": {"identifier": "vision_reset"}})
                until = time.monotonic() + 3
                while manifest.read_bytes() == previous:
                    if time.monotonic() > until:
                        raise TimeoutError("New randomized tiles were not observed")
                    time.sleep(0.05)
            expected = secrets.choice(json.loads(manifest.read_text())["tiles"])[
                "label"
            ]
            print(
                json.dumps({"phase": "real_visual_location", "round": n + 1}),
                flush=True,
            )
            observed = backend.visual_find(
                {"identifier": "vision_canvas"},
                "Locate the one colored tile with the exact visible label "
                + expected
                + ". Return a box inside that tile. All coordinates are image pixels.",
            )
            if not observed.get("unique"):
                raise AssertionError("Image model did not return one candidate")
            backend.do(
                {
                    "op": "visual_click",
                    "target": {"identifier": "vision_canvas"},
                    "visual_anchor": observed["candidates"][0]["anchor"],
                }
            )
            for identifier, value in (
                ("vision_status", expected),
                ("vision_count", "1"),
            ):
                ok, _ = backend.check(
                    {
                        "target": {"identifier": identifier},
                        "attribute": "text",
                        "expected": value,
                        "timeout_seconds": 3,
                    }
                )
                if not ok:
                    raise AssertionError(
                        "Native business readback differs: " + identifier
                    )
            report["rounds"].append(
                {
                    "round": n + 1,
                    "passed": True,
                    "target": expected,
                    "image_sha256": observed["image_sha256"],
                    "image_bytes": observed["image_bytes"],
                    "provider_usage_reported": observed["provider_usage_reported"],
                    "usage": observed["usage"],
                }
            )
            print(
                json.dumps({"phase": "business_verified", "round": n + 1}), flush=True
            )
        report["passed"] = True
    except Exception as exc:  # noqa: BLE001 -- preserve acceptance failure evidence
        report["error_type"] = type(exc).__name__
        # Error details may contain provider URLs; never include credentials or requests.
        report["error"] = str(exc)[:1000]
    finally:
        if backend:
            backend.close()
        if app and app.poll() is None:
            app.terminate()
            app.wait(timeout=5)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
