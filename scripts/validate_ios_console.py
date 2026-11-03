"""Actual browser acceptance for offline iOS measurements; never operates a phone."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from iatest import __version__
from iatest.core.config import init_project, load_config, save_config
from iatest.core.env import load_dotenv
from iatest.engines.store import EngineStore
from iatest.web.server import make_handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--real-model", action="store_true")
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    load_dotenv(repository)
    with tempfile.TemporaryDirectory(prefix="iatest-ios-console-") as directory:
        root, project = Path(directory), "ios-console"
        init_project(root, project)
        if args.real_model:
            config, original = load_config(root), load_config(repository)
            config["llm"], config["agents"] = (
                original["llm"],
                original.get("agents", {}),
            )
            save_config(root, config)
        source = root / "controlled-measurements.json"
        source.write_text(
            json.dumps(
                {
                    "contract_version": 1,
                    "platform": "ios",
                    "app_id": "com.example.fixture",
                    "source": {
                        "provider": "custom",
                        "device_id": "controlled-fixture-not-device",
                        "build": "fixture-v1",
                        "test_id": "synthetic-offline-contract-check",
                    },
                    "measurements": [
                        {"metric": "cpu_percent", "unit": "%", "values": [18, 32]},
                        {
                            "metric": "memory_footprint_kb",
                            "unit": "KiB",
                            "values": [19000, 21000],
                        },
                        {
                            "metric": "ios_launch_ms",
                            "unit": "ms",
                            "values": [900, 1200],
                        },
                        {
                            "metric": "hitch_ratio",
                            "unit": "ratio",
                            "values": [0.01, 0.02],
                        },
                    ],
                }
            )
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(root, project))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            node = shutil.which("node")
            env = {
                **os.environ,
                "IATEST_SMOKE_URL": f"http://127.0.0.1:{server.server_port}",
                "IATEST_IOS_IMPORT": json.dumps(
                    {"format": "measurements_json", "path": str(source)}
                ),
                "IATEST_REAL_MODEL": "1" if args.real_model else "0",
            }
            ca = root / "public-test-ca.pem"
            ca.write_text(
                ssl.DER_cert_to_PEM_cert(
                    ssl.create_default_context().get_ca_certs(binary_form=True)[0]
                )
            )
            env["IATEST_WINDOWS_BUNDLE"] = json.dumps(
                {
                    "wheel": str(
                        repository / f"dist/iatest_framework-{__version__}-py3-none-any.whl"
                    ),
                    "ca_file": str(ca),
                    "controller": "https://192.0.2.1:8768",
                    "allowed_origins": ["http://192.0.2.1:8767"],
                    "node_id": "bundle-fixture-not-remote",
                    "token_env": "IATEST_TEST_NODE_TOKEN",
                }
            )
            env.setdefault(
                "NODE_PATH", str(Path(node).resolve().parents[1] / "lib/node_modules")
            )
            subprocess.run(
                [node, str(Path(__file__).with_suffix(".cjs"))],
                env=env,
                check=True,
                timeout=240,
            )
            store = EngineStore(root, project)
            runs = {run["plan_id"]: run for run in store.runs()}
            assert [
                runs[key]["verdict"]
                for key in (
                    "ios-offline-pass",
                    "ios-offline-fail",
                    "ios-offline-missing",
                )
            ] == ["passed", "failed", "inconclusive"]
            report = {
                "passed": True,
                "physical_device_actions": 0,
                "measurement_source": "explicit synthetic fixture; no claim of real iOS measurements",
                "ui_flows": [
                    "import",
                    "freeze source digest",
                    "offline pass/fail/missing gates",
                    "localized metric cards",
                    "five locales",
                    "390px layout",
                    "Windows bundle generation/download; no Windows execution",
                ],
                "windows_runtime_validated": False,
            }
            if args.real_model:
                artifact = store.artifacts("analysis")[0]
                assert (
                    artifact["original_verdict"] == "passed"
                    and artifact["usage"]["total_tokens"] > 0
                )
                assert (
                    store.get_result(artifact["source_run_id"])["verdict"] == "passed"
                )
                report["real_model"] = {
                    "artifact_id": artifact["id"],
                    "usage": artifact["usage"],
                    "analysis": artifact["analysis"],
                    "original_verdict_preserved": True,
                }
            Path("/private/tmp/iatest-ios-console-acceptance.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2)
            )
            if args.real_model:
                Path("/private/tmp/iatest-ios-model-acceptance.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=2)
                )
            print(
                json.dumps(
                    {
                        k: v
                        if k != "real_model"
                        else {x: y for x, y in v.items() if x != "analysis"}
                        for k, v in report.items()
                    },
                    ensure_ascii=False,
                )
            )
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
