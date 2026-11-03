"""Portable Windows bootstrap bundle without embedded pairing credentials."""

from __future__ import annotations

import hashlib
import json
import re
import ssl
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from iatest.core.sandbox import origin
from iatest.engines.store import identifier


def build_bundle(
    output,
    wheel,
    ca_file,
    *,
    controller,
    allowed_origins,
    node_id,
    token_env="IATEST_NODE_TOKEN",
    project="default",
    allowed_kinds=("api", "performance"),
):
    parsed = urlsplit(controller)
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("Windows LAN bundle requires an HTTPS controller origin")
    if not allowed_origins or len(allowed_origins) > 32:
        raise ValueError("Provide 1–32 authorized test origins")
    allowed = sorted({origin(value) for value in allowed_origins})
    supported = {
        "api",
        "performance",
        "security",
        "web",
        "android",
        "ios",
        "workflow",
        "client_performance",
        "data",
        "protocol",
        "accessibility",
        "desktop",
    }
    if (
        not isinstance(allowed_kinds, (tuple, list))
        or not allowed_kinds
        or len(allowed_kinds) > len(supported)
        or not set(allowed_kinds) <= supported
        or len(set(allowed_kinds)) != len(allowed_kinds)
    ):
        raise ValueError("Provide distinct supported worker engine kinds")
    identifier(node_id)
    identifier(project)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env):
        raise ValueError("Invalid node token environment variable name")
    wheel, ca_file, output = Path(wheel), Path(ca_file), Path(output)
    if wheel.stat().st_size > 16777216 or not wheel.name.endswith("-py3-none-any.whl"):
        raise ValueError("Expected a portable framework wheel of at most 16 MiB")
    with zipfile.ZipFile(wheel) as package:
        if "iatest/nodes/worker.py" not in package.namelist():
            raise ValueError("Wheel does not contain execution-node support")
    certificate = ca_file.read_bytes()
    if len(certificate) > 65536 or b"PRIVATE KEY" in certificate:
        raise ValueError(
            "Bundle must contain only a bounded public CA certificate, never a private key"
        )
    ssl.create_default_context(cadata=certificate.decode("ascii"))
    config = {
        "controller": controller.rstrip("/"),
        "allowed_origins": allowed,
        "node_id": node_id,
        "token_env": token_env,
        "project": project,
        "wheel": wheel.name,
        "ca": "controller-ca.pem",
        "kinds": list(allowed_kinds),
    }
    files = {
        wheel.name: wheel.read_bytes(),
        "controller-ca.pem": certificate,
        "node.json": json.dumps(config, indent=2).encode(),
        "start-node.ps1": Path(__file__).with_name("start-node.ps1").read_bytes(),
        "start-node.py": Path(__file__).with_name("bootstrap.py").read_bytes(),
    }
    files["sha256.json"] = json.dumps(
        {name: hashlib.sha256(content).hexdigest() for name, content in files.items()},
        indent=2,
    ).encode()
    with zipfile.ZipFile(output, "x", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return {
        "output": str(output),
        "node_id": node_id,
        "controller": config["controller"],
        "files": sorted(files),
        "credentials_embedded": False,
    }
