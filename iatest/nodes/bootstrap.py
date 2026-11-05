"""Standalone stdlib-only entry point, also shipped as start-node.py in node ZIPs."""

from __future__ import annotations
from iatest.core.env import environment_values

import argparse
import getpass
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def start(bundle, *, python=sys.executable, workspace=None):
    bundle = Path(bundle).resolve()
    hashes = json.loads((bundle / "sha256.json").read_text(encoding="utf-8"))
    config = json.loads((bundle / "node.json").read_text(encoding="utf-8"))
    required = {"node.json", config["wheel"], config["ca"], "start-node.py"}
    if not required <= hashes.keys():
        raise ValueError("Bundle manifest is missing required files")
    for name, expected in hashes.items():
        path = bundle / name
        if (
            Path(name).name != name
            or "/" in name
            or "\\" in name
            or path.resolve().parent != bundle
            or path.stat().st_size > 16777216
            or hashlib.sha256(path.read_bytes()).hexdigest() != expected
        ):
            raise ValueError("Invalid bundle path or content hash mismatch")
    token_name = config["token_env"]
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_name):
        raise ValueError("Invalid pairing token environment variable name")
    token = environment_values().get(token_name) or getpass.getpass(
        "Node pairing token (not your Windows password): "
    )
    if len(token) < 16:
        raise ValueError("Pairing token must contain at least 16 characters")
    subprocess.run(
        [
            str(python),
            "-c",
            "import sys; assert sys.version_info >= (3,11), 'Python 3.11+ required'",
        ],
        check=True,
    )
    if workspace is None:
        workspace = Path(tempfile.mkdtemp(prefix="iatest-node-"))
    else:
        workspace = Path(workspace).resolve()
        workspace.mkdir(parents=True, exist_ok=False)
    venv = workspace / ".venv"
    subprocess.run([str(python), "-m", "venv", str(venv)], check=True)
    executable = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(
        [
            str(executable),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            str(bundle / config["wheel"]),
        ],
        check=True,
    )
    environment = {**environment_values(), token_name: token}
    subprocess.run(
        [str(executable), "-m", "iatest", "init", "--project", config["project"]],
        cwd=workspace,
        env=environment,
        check=True,
    )
    command = [
        str(executable),
        "-m",
        "iatest",
        "node",
        "worker",
        config["controller"],
        config["node_id"],
        "--project",
        config["project"],
        "--token-env",
        token_name,
        "--kinds",
        ",".join(config["kinds"]),
        "--ca",
        str(bundle / config["ca"]),
    ]
    for origin in config["allowed_origins"]:
        command.extend(["--origin", origin])
    print(f"Starting isolated node in {workspace}; Ctrl-C stops it.", flush=True)
    subprocess.run(command, cwd=workspace, env=environment, check=True)
    return workspace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-path", default=sys.executable)
    parser.add_argument("--workspace", type=Path)
    args = parser.parse_args()
    start(Path(__file__).parent, python=args.python_path, workspace=args.workspace)


if __name__ == "__main__":
    main()
