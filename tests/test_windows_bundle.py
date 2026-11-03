import hashlib
import json
import ssl
import zipfile

import pytest

from iatest.nodes.bootstrap import start
from iatest.nodes.windows_bundle import build_bundle


@pytest.fixture
def inputs(tmp_path):
    wheel = tmp_path / "iatest_framework-1.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("iatest/nodes/worker.py", "# Controlled bundle fixture")
    roots = ssl.create_default_context().get_ca_certs(binary_form=True)
    if not roots:
        pytest.skip("No local public CA certificate available")
    ca = tmp_path / "ca.pem"
    ca.write_text(ssl.DER_cert_to_PEM_cert(roots[0]))
    return wheel, ca, tmp_path / "windows-node.zip"


def test_windows_bootstrap_bundle_contains_hashes_and_no_embedded_credentials(inputs):
    wheel, ca, output = inputs
    result = build_bundle(
        output,
        wheel,
        ca,
        controller="https://192.168.1.10:8768",
        allowed_origins=["http://192.168.1.10:8767"],
        node_id="windows",
        token_env="EXISTING_NODE_AUTH",
    )
    assert result["credentials_embedded"] is False
    with zipfile.ZipFile(output) as archive:
        hashes = json.loads(archive.read("sha256.json"))
        assert all(
            hashlib.sha256(archive.read(name)).hexdigest() == expected
            for name, expected in hashes.items()
        )
        config = json.loads(archive.read("node.json"))
        assert config["token_env"] == "EXISTING_NODE_AUTH"
        assert "token" not in config and "password" not in config
        script = archive.read("start-node.ps1").decode()
        assert "-AsSecureString" in script and "--token-env" in script
        assert "Set-ExecutionPolicy" not in script
        assert b"def start(" in archive.read("start-node.py")
    with pytest.raises(FileExistsError):
        build_bundle(
            output,
            wheel,
            ca,
            controller="https://192.168.1.10:8768",
            allowed_origins=["http://192.168.1.10:8767"],
            node_id="windows",
        )


def test_windows_bundle_rejects_private_keys_and_plaintext_lan(inputs):
    wheel, ca, output = inputs
    with pytest.raises(ValueError, match="HTTPS"):
        build_bundle(
            output,
            wheel,
            ca,
            controller="http://192.168.1.10:8768",
            allowed_origins=["http://192.168.1.10:8767"],
            node_id="windows",
        )
    ca.write_text(ca.read_text() + "\n-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n")
    with pytest.raises(ValueError, match="private key"):
        build_bundle(
            output,
            wheel,
            ca,
            controller="https://192.168.1.10:8768",
            allowed_origins=["http://192.168.1.10:8767"],
            node_id="windows",
        )


def unpack(inputs):
    wheel, ca, output = inputs
    build_bundle(
        output,
        wheel,
        ca,
        controller="https://192.168.1.10:8768",
        allowed_origins=["http://192.168.1.10:8767"],
        node_id="windows",
        token_env="IATEST_BOOTSTRAP_TEST",
    )
    directory = output.parent / "bundle"
    with zipfile.ZipFile(output) as archive:
        archive.extractall(directory)
    return directory


def test_python_bootstrap_checks_integrity_before_running_commands(inputs, monkeypatch):
    directory = unpack(inputs)
    path = directory / "node.json"
    config = json.loads(path.read_text())
    config["controller"] = "https://changed.example"
    path.write_text(json.dumps(config))
    calls = []
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: calls.append((a, kw)))
    with pytest.raises(ValueError, match="hash mismatch"):
        start(directory)
    assert calls == []


def test_python_bootstrap_rejects_manifest_path_traversal(inputs, monkeypatch):
    directory = unpack(inputs)
    manifest = directory / "sha256.json"
    hashes = json.loads(manifest.read_text())
    hashes["../outside"] = "0" * 64
    manifest.write_text(json.dumps(hashes))
    calls = []
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: calls.append((a, kw)))
    with pytest.raises(ValueError, match="Invalid bundle path"):
        start(directory)
    assert calls == []


def test_python_bootstrap_uses_isolated_environment_and_no_token_arguments(
    inputs, monkeypatch, capsys
):
    directory = unpack(inputs)
    token = "controlled-bootstrap-test-token"
    monkeypatch.setenv("IATEST_BOOTSTRAP_TEST", token)
    calls = []
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: calls.append((a, kw)))
    workspace = directory.parent / "isolated"
    assert start(directory, workspace=workspace) == workspace
    worker_args, worker_options = calls[-1]
    assert worker_options["cwd"] == workspace
    assert worker_options["env"]["IATEST_BOOTSTRAP_TEST"] == token
    assert "--ca" in worker_args[0] and "--origin" in worker_args[0]
    assert token not in repr([args for args, _ in calls])
    assert token not in capsys.readouterr().out
    assert all("--ExecutionPolicy" not in args[0] for args, _ in calls)
    with pytest.raises(FileExistsError):
        start(directory, workspace=workspace)
