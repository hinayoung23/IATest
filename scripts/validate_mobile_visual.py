"""Real vision model against an explicit synthetic native crop, never a phone."""

from __future__ import annotations

import argparse
import copy
import json
import struct
import tempfile
import zlib
from pathlib import Path

from iatest.ai.llm_config import resolve_llm_role
from iatest.ai.model_gateway import ProviderModelGateway
from iatest.application.ui_visual import locate_region
from iatest.core.config import init_project, load_config, save_config
from iatest.core.env import load_dotenv
from iatest.mobile.runtime import MobileSession
from iatest.mobile.visual import MobileVisual


def image():
    def chunk(kind, value):
        return (
            struct.pack(">I", len(value))
            + kind
            + value
            + struct.pack(">I", zlib.crc32(kind + value))
        )

    rows = bytearray()
    for y in range(100):
        rows.append(0)
        for x in range(160):
            rows.extend(
                (220, 20, 20, 255)
                if 20 <= x < 80 and 30 <= y < 70
                else (255, 255, 255, 255)
            )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 160, 100, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


class NativeFixture(MobileVisual):
    unique = staticmethod(MobileSession.unique)

    def query(self, _target, **kwargs):
        assert kwargs["cache"] is False
        return {
            "complete": True,
            "match_count": 1,
            "candidates": [{"bounds": [0, 0, 160, 100]}],
        }

    @property
    def device(self):
        from types import SimpleNamespace

        return SimpleNamespace(semantic=self)

    def request(self, op, _args, **_kwargs):
        assert op == "screenshot"
        return {
            "binary": image(),
            "width": 160,
            "height": 100,
            "driver_epoch": "synthetic-only",
            "crop_rect": {"x": 0, "y": 0, "width": 160, "height": 100},
            "viewport": {"width": 160, "height": 100, "dpr": 1, "rotation": 0},
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument(
        "--model",
        required=True,
        help="Explicit vision model for this temporary acceptance only",
    )
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    root = Path.cwd()
    load_dotenv(root)
    model = copy.deepcopy(
        resolve_llm_role(load_config(root).get("llm", {}), "case_generator")
    )
    model.update(
        model=args.model,
        supports_images=True,
        default_output_tokens=2048,
        thinking={"type": "disabled"},
        tool_choice={
            "type": "function",
            "function": {"name": "submit_visual_candidates"},
        },
    )
    with tempfile.TemporaryDirectory(prefix="iatest-real-native-vision-") as temporary:
        work = Path(temporary)
        config = init_project(work, "vision")
        config = load_config(work)
        config["llm"] = {"master": model, "case_generator": {}, "analyzer": {}}
        save_config(work, config)
        attempts = []
        class Capture(ProviderModelGateway):
            def complete(self, **kwargs):
                response = super().complete(**kwargs)
                attempts.append({"model": self.model_config.get("model"), "tools_sent": len(kwargs["tools"]), "supports_tools": self.model_config.get("supports_tools"), "finish_reason": response.finish_reason, "text": response.text[:3000], "tool_names": [t.name for t in response.tool_calls], "usage": response.usage.total_tokens})
                return response
        try:
            result = locate_region(work, "vision", NativeFixture(), {"identifier": "synthetic-canvas"}, "Locate the single solid red rectangle. Return its box in image pixel coordinates.", gateway_factory=Capture)
        except Exception:
            args.report.write_text(json.dumps({"passed": False, "physical_device_actions": 0, "attempts": attempts}, ensure_ascii=False, indent=2))
            raise
        args.report.write_text(json.dumps({"passed": False, "physical_device_actions": 0, "model": args.model, "attempts": attempts, "result": result}, ensure_ascii=False, indent=2))
        assert len(result["candidates"]) == 1, result
        point = result["candidates"][0]["anchor"]
        assert 20 <= point["x"] < 80 and 30 <= point["y"] < 70, point
        report = {
            "passed": True,
            "model": args.model,
            "image": "explicit synthetic native crop; no phone",
            "physical_device_actions": 0,
            "production_config_changed": False,
            "result": result,
        }
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "passed": True,
                    "model": args.model,
                    "usage": result["usage"],
                    "report": str(args.report),
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
