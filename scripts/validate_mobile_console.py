"""Drive the real local console with Chromium and an explicit synthetic device boundary."""

from __future__ import annotations

import argparse
import base64
import os
import re
import subprocess
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

from iatest.core.config import init_project
from iatest.mobile.runtime import SESSIONS
from iatest.web.server import make_handler


class ConsoleChannel:
    capabilities: ClassVar[dict] = {
        "continuous_video": True,
        "actions": [
            "query",
            "outline",
            "tap",
            "text",
            "swipe",
            "key",
            "state",
            "screenshot",
        ],
        "input_modes": ["semantic", "key_events"],
        "tap_backends": ["accessibility"],
    }

    def __init__(self):
        self.closed = False
        self.tapped = False

    def request(self, op, args, **kwargs):
        if op == "outline":
            return {
                "controls": [
                    {
                        "target": {"identifier": "com.example.shop:id/login"},
                        "label": "登录",
                        "identifier": "com.example.shop:id/login",
                        "bounds": [10, 10, 100, 80],
                    }
                ],
                "complete": True,
                "proves_uniqueness": False,
            }
        if op == "query":
            target = args["target"]
            identifier = target.get("identifier", "")
            text = (
                ("成功" if self.tapped else "未登录")
                if identifier.endswith("status")
                else "登录"
            )
            return {
                "complete": True,
                "match_count": 1,
                "state_version": 1,
                "visited_nodes": 4,
                "display": {"id": 0, "width": 400, "height": 800, "rotation": 0},
                "candidates": [
                    {
                        "ref": "console-ref",
                        "identifier": identifier,
                        "text": text,
                        "label": text,
                        "enabled": True,
                        "supports_accessibility_tap": True,
                        "bounds": [10, 10, 100, 80],
                    }
                ],
            }
        if op == "screenshot":
            return {
                "binary": base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
                ),
                "content_type": "image/png",
            }
        if op == "tap":
            self.tapped = True
            return {
                "status": "injection_call_returned",
                "accepted": True,
                "tap_backend": args.get("tap_backend", "pointer"),
            }
        return {"status": "received"}

    def close(self):
        self.closed = True


def attach_fixture(device):
    device.input = device.semantic = ConsoleChannel()


class EncodedConsoleChannel:
    def __init__(self, packets):
        self.packets = packets
        self.index = 0
        self.closed = False

    def request(self, op, args, **kwargs):
        assert op == "video_next"
        if self.closed:
            raise RuntimeError("Synthetic media channel closed")
        if args.get("request_keyframe"):
            self.index = ((self.index + 4) // 5) * 5
        index = self.index
        self.index += 1
        time.sleep(1 / 15)
        return {
            "binary": self.packets[index % len(self.packets)],
            "codec": "h264-annexb",
            "no_b_frames": True,
            "key_frame": index % 5 == 0,
            "generation": str(index // 20),
            "width": 96,
            "height": 64,
            "presentation_us": round(index * 1000000 / 15),
            "capture_source": "explicit FFmpeg-encoded synthetic fixture",
        }

    def close(self):
        self.closed = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node-modules", type=Path, required=True)
    parser.add_argument("--browser", type=Path, required=True)
    args = parser.parse_args()
    encoded = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=96x64:rate=15",
            "-frames:v",
            "20",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-bf",
            "0",
            "-g",
            "5",
            "-x264-params",
            "aud=1:repeat-headers=1",
            "-f",
            "h264",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
        timeout=15,
    ).stdout
    starts = [m.start() for m in re.finditer(b"\x00\x00\x00\x01\x09", encoded)]
    packets = [
        encoded[a:b] for a, b in zip(starts, starts[1:] + [len(encoded)], strict=True)
    ]
    with tempfile.TemporaryDirectory(prefix="iatest-mobile-console-") as directory:
        root = Path(directory)
        init_project(root, "console-smoke")
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(root, "console-smoke")
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        with (
            patch("iatest.mobile.devices.NativeDevice._android", attach_fixture),
            patch(
                "iatest.mobile.devices.NativeDevice.open_media",
                lambda self, codec: EncodedConsoleChannel(packets),
            ),
        ):
            thread.start()
            try:
                env = {
                    **os.environ,
                    "NODE_PATH": str(args.node_modules),
                    "IATEST_SMOKE_URL": f"http://127.0.0.1:{server.server_port}",
                    "IATEST_SMOKE_BROWSER": str(args.browser),
                }
                subprocess.run(
                    ["node", str(Path(__file__).with_suffix(".cjs"))],
                    env=env,
                    check=True,
                )
            finally:
                SESSIONS.close_all()
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    main()
