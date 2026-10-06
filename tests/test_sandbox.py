from __future__ import annotations

from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import io
import os
from pathlib import Path
import subprocess
import threading
import time

import httpx
import pytest
from typer.testing import CliRunner

from ai_apitest.cli import app
from ai_apitest.core.config import load_config, save_config
from ai_apitest.core.models import Assertion, TestCase as Case, TestStep as Step
from ai_apitest.core.sandbox import SandboxPolicy, SandboxViolation, policy_scope, read_file
from ai_apitest.core.serde import write_json
from ai_apitest.core.paths import project_docs_file
from ai_apitest.discovery.document import load_and_parse_source
from ai_apitest.discovery.openapi import parse_openapi
from ai_apitest.execution.auth import AuthManager
from ai_apitest.execution.control import RunControl
from ai_apitest.execution.datasets import dataset_rows
from ai_apitest.execution.datasets import expand_cases
from ai_apitest.execution.fixtures import FixtureManager
from ai_apitest.execution.isolation import container_command, prepare_container, worker_environment, collect_container_output
from ai_apitest.execution.options import RunOptions
from ai_apitest.execution.plugins import PluginRegistry
from ai_apitest.execution.runner import encode_request_body, multipart_fields, run_cases
from ai_apitest.execution.transport import client_options
from ai_apitest.generator.cases import save_cases
from ai_apitest.report.storage import latest_run_metadata


def policy(root, **settings):
    return SandboxPolicy.from_config(root, {"sandbox": settings}, project="demo", base_url="https://target.invalid")


@pytest.mark.parametrize("reader", [
    lambda path, root: encode_request_body({}, {"$iatest_file": str(path)}),
    lambda path, root: multipart_fields({"upload": {"file_path": str(path)}}),
    lambda path, root: dataset_rows({"source": str(path)}, root, {}),
])
def test_all_file_inputs_block_escape_and_allow_declared_paths(tmp_path, reader):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text('[{"id": 1}]')
    with policy_scope(policy(root)):
        with pytest.raises(SandboxViolation, match="目录之外"):
            reader(outside, root)
    with policy_scope(policy(root, read_paths=[str(outside)])):
        assert reader(outside, root)


def test_symlinks_secrets_other_projects_and_explicit_denials_are_blocked(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("synthetic")
    link = root / "link"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation unavailable")
    for name in (".env", ".env.local", "iatest.json", "private.pem", "projects/other/data.json"):
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("synthetic")
    with policy_scope(policy(root, denied_read_paths=["private.pem"])):
        for file in (link, root / ".env", root / ".env.local", root / "iatest.json", root / "private.pem", root / "projects/other/data.json"):
            with pytest.raises(SandboxViolation):
                read_file(file, 100)


def test_file_size_and_special_files_are_bounded(tmp_path):
    file = tmp_path / "data"
    file.write_bytes(b"1234")
    with policy_scope(policy(tmp_path)):
        with pytest.raises(ValueError, match="字节限制"):
            read_file(file, 3)
        if hasattr(os, "mkfifo"):
            fifo = tmp_path / "pipe"
            os.mkfifo(fifo)
            with pytest.raises(SandboxViolation, match="普通文件"):
                read_file(fifo, 100)


@pytest.mark.parametrize("url", ["https://target.invalid.evil/", "https://target.invalid:444/", "http://target.invalid/", "https://user:pass@target.invalid/", "file:///tmp/data"])
def test_network_policy_checks_exact_origin(url, tmp_path):
    with pytest.raises(SandboxViolation):
        policy(tmp_path).check_url(url)


def test_network_policy_checks_every_redirect_before_io(tmp_path):
    visited = []
    def handle(request):
        visited.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://other.invalid/private"})
    with policy_scope(policy(tmp_path)), httpx.Client(transport=httpx.MockTransport(handle), **client_options({"follow_redirects": True}, 1)) as client:
        with pytest.raises(SandboxViolation, match="未授权"):
            client.get("https://target.invalid/start")
    assert visited == ["https://target.invalid/start"]


def test_network_policy_rejects_dns_addresses_outside_declared_network(tmp_path, monkeypatch):
    monkeypatch.setattr("ai_apitest.core.sandbox.socket.getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("169.254.169.254", 443))])
    with pytest.raises(SandboxViolation, match="网段"):
        policy(tmp_path, allowed_networks=["127.0.0.0/8"]).check_url("https://target.invalid/")


def test_auth_and_fixture_requests_use_same_policy(tmp_path):
    calls = []
    with policy_scope(policy(tmp_path)), httpx.Client(transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200, json={"access_token": "synthetic"}))) as client:
        auth = AuthManager({"base_url": "https://target.invalid", "auth": {"default_identity": "user", "identities": {"user": {"type": "oauth2", "token_url": "https://other.invalid/token", "client_id": "test", "client_secret": "synthetic"}}}})
        with pytest.raises(SandboxViolation):
            auth.token(client)
        manager = FixtureManager({}, client)
        result, _ = manager._request_action("test", {"url": "https://other.invalid/prepare"}, {}, extract_values=True)
        assert result["status"] == "failed" and "未授权" in result["error"]
    assert not calls


def test_remote_openapi_reference_blocked_before_network(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("unexpected network request")
    monkeypatch.setattr(httpx.Client, "stream", unexpected)
    document = {"openapi": "3.1.0", "paths": {"/data": {"get": {"responses": {"200": {"$ref": "http://169.254.169.254/secret"}}}}}}
    with policy_scope(policy(tmp_path)):
        with pytest.raises(SandboxViolation):
            parse_openapi(document, source="https://target.invalid/openapi.json")


def test_explicit_cli_document_can_use_sibling_reference_outside_root(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "response.json").write_text('{"description":"ok"}')
    source = docs / "openapi.json"
    source.write_text(json.dumps({"openapi": "3.1.0", "paths": {"/data": {"get": {"responses": {"200": {"$ref": "response.json"}}}}}}))
    assert len(load_and_parse_source(root, "demo", str(source), allow_source_file=True)[1]) == 1
    with pytest.raises(SandboxViolation):
        load_and_parse_source(root, "demo", str(source))


def test_plugins_fail_before_import_and_env_is_minimal(tmp_path, monkeypatch):
    monkeypatch.setenv("UNRELATED_SECRET", "synthetic")
    monkeypatch.setenv("FIXTURE_CREDENTIAL", "allowed")
    monkeypatch.setenv("HTTP_PROXY", "http://unwanted.invalid")
    with policy_scope(policy(tmp_path)):
        with pytest.raises(SandboxViolation, match="禁用插件"):
            PluginRegistry.from_config({"plugins": ["untrusted"]})
        assert not client_options({}, 1)["trust_env"]
    env = worker_environment({"env_allowlist": ["FIXTURE_CREDENTIAL"]}, tmp_path)
    assert env["FIXTURE_CREDENTIAL"] == "allowed"
    assert "UNRELATED_SECRET" not in env and "HTTP_PROXY" not in env
    assert env["HOME"] == str(tmp_path)


def test_relative_tls_paths_resolve_against_project_in_worker(tmp_path, monkeypatch):
    loaded = []
    class Context:
        def load_cert_chain(self, cert, key, password):
            loaded.append((cert, key, password))
    def context(cafile=None):
        loaded.append(cafile)
        return Context()
    monkeypatch.setattr("ai_apitest.execution.transport.ssl.create_default_context", context)
    with policy_scope(policy(tmp_path, mode="process")):
        client_options({"verify_tls": "fixtures/ca.pem", "client_cert": {"certfile": "fixtures/cert.pem", "keyfile": "fixtures/key.pem"}}, 1)
    assert loaded == [str(tmp_path / "fixtures/ca.pem"), (str(tmp_path / "fixtures/cert.pem"), str(tmp_path / "fixtures/key.pem"), None)]


def test_web_document_source_is_not_implicitly_authorized(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("unexpected network request")
    monkeypatch.setattr(httpx.Client, "stream", unexpected)
    with pytest.raises(SandboxViolation, match="未授权"):
        load_and_parse_source(tmp_path, "demo", "http://169.254.169.254/secret")


@pytest.fixture
def local_api():
    entered = threading.Event()
    release = threading.Event()
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append((self.path, b""))
            if self.path == "/hang":
                entered.set()
                release.wait(8)
            data = b'{"ok":true}'
            try:
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
        def do_POST(self):
            received.append((self.path, self.rfile.read(int(self.headers.get("Content-Length", "0")))))
            self.do_GET()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", entered, received
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


def setup_run(root, url, cases, **sandbox):
    config = load_config(root)
    config["environments"]["local"]["base_url"] = url
    config["execution"]["parallel"] = 1
    config["sandbox"].update({"mode": "process", "hard_timeout_seconds": 10, **sandbox})
    save_config(root, config)
    write_json(project_docs_file(root, "demo"), [])
    save_cases(root, cases, project="demo")


def test_real_process_executes_upload_datasets_namespace_and_cli(tmp_path, local_api, monkeypatch):
    url, _, received = local_api
    data = tmp_path / "payload.txt"
    data.write_text("synthetic-upload")
    cases = [Case("C1", "upload", "POST", "/upload", body={"$iatest_file": "payload.txt"}, assertions=[Assertion("status_code", expected=200)]), Case("C2", "namespace", "GET", "/{namespace}", path_params={"namespace": "{{iatest_namespace}}"}, dataset=[{"_id": "a"}, {"_id": "b"}])]
    setup_run(tmp_path, url, cases)
    results = run_cases(tmp_path, project="demo")
    assert [result.status for result in results] == ["passed"] * 3, [result.error for result in results]
    assert results[1].origin_case_id == results[1].case_id.split("[")[0] and results[1].dataset_id == "a"
    assert any(body == b"synthetic-upload" for _, body in received)
    evidence = latest_run_metadata(tmp_path, "demo")["sandbox"]
    assert evidence["completed"] and not evidence["forced_termination"]
    assert any(path == "/" + evidence["namespace"] for path, _ in received)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["run", "--project", "demo", "--no-ai", "--isolation", "process", "--json-output", "result.json"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "result.json").is_file()


@pytest.mark.parametrize("cancel", [False, True])
def test_force_termination_keeps_completed_results_and_persists_evidence(tmp_path, local_api, cancel, monkeypatch):
    url, entered, _ = local_api
    marker = tmp_path / "plugin-entered"
    plugin_dir = tmp_path / "installed-plugins"
    plugin_dir.mkdir()
    (plugin_dir / "blocking_plugin.py").write_text('import time\nfrom pathlib import Path\ndef register(registry):\n    def before(step, headers, kwargs, context):\n        if step.path == "/hang":\n            Path(context["marker"]).touch()\n            while True:\n                time.sleep(0.1)\n    registry.before_request.append(before)\n')
    metadata = plugin_dir / "iatest_test_blocker-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Name: iatest-test-blocker\nVersion: 1.0\n")
    (metadata / "entry_points.txt").write_text("[iatest.plugins]\nblocking = blocking_plugin:register\n")
    real_popen = subprocess.Popen
    def with_test_plugin(command, **kwargs):
        command = list(command)
        index = command.index("-c") + 1
        command[index] = f"import sys; sys.path.insert(0, {str(plugin_dir)!r}); " + command[index]
        return real_popen(command, **kwargs)
    monkeypatch.setattr("ai_apitest.execution.isolation.subprocess.Popen", with_test_plugin)
    cases = [Case("C1", "completed", "GET", "/ok"), Case("C2", "blocked", "GET", "/hang", variables={"marker": str(marker)})]
    setup_run(tmp_path, url, cases, hard_timeout_seconds=2, termination_grace_seconds=0.1, allow_plugins=True)
    config = load_config(tmp_path)
    config["plugins"] = ["blocking"]
    save_config(tmp_path, config)
    control = RunControl()
    thread = None
    if cancel:
        def request_cancel():
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if marker.exists():
                    control.cancelled.set()
                    return
                time.sleep(0.01)
        thread = threading.Thread(target=request_cancel, daemon=True)
        thread.start()
    started = time.monotonic()
    results = run_cases(tmp_path, project="demo", control=control)
    assert time.monotonic() - started < 6
    assert marker.exists()
    assert [result.status for result in results] == ["passed", "cancelled"], [result.error for result in results]
    assert results[1].cleanup["status"] == "skipped"
    evidence = latest_run_metadata(tmp_path, "demo")["sandbox"]
    assert evidence["forced_termination"] and not evidence["completed"]
    if thread:
        thread.join(1)


def test_process_plugin_failure_is_actionable_and_does_not_fallback(tmp_path, local_api):
    setup_run(tmp_path, local_api[0], [Case("C1", "test", "GET", "/ok")])
    config = load_config(tmp_path)
    config["plugins"] = ["not-installed"]
    save_config(tmp_path, config)
    result = run_cases(tmp_path, project="demo")[0]
    assert result.status == "failed" and "禁用插件" in result.error
    assert not local_api[2]


def test_process_fixture_check_preserves_unused_disabled_provider_status(tmp_path, monkeypatch):
    config = load_config(tmp_path)
    config["sandbox"]["mode"] = "process"
    config["fixtures"]["providers"] = {
        "available": {"type": "static", "enabled": True, "values": {"account_id": "synthetic"}},
        "disabled": {"type": "static", "enabled": False, "values": {"secret": "${UNCONFIGURED_FIXTURE_SECRET}"}},
    }
    save_config(tmp_path, config)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["fixtures-check", "--project", "demo", "--exercise"])
    assert result.exit_code == 0, result.output
    rows = {row["name"]: row for row in json.loads(result.stdout)}
    assert rows["available"]["status"] == "passed"
    assert rows["disabled"]["status"] == "disabled" and not rows["disabled"]["required"]


def test_dataset_expansion_is_bounded_and_does_not_duplicate_source_rows(tmp_path):
    original = Case("C", "data", "GET", "/", dataset=[{"id": number} for number in range(5)])
    with pytest.raises(ValueError, match="max_cases"):
        expand_cases([original], tmp_path, {"sandbox": {"max_cases": 3}})
    with pytest.raises(ValueError, match="max_input_bytes"):
        expand_cases([original], tmp_path, {"sandbox": {"max_input_bytes": 10}})
    expanded = expand_cases([original], tmp_path, {})
    assert all(case.dataset is None for case in expanded) and len(original.dataset) == 5


def test_web_cannot_lower_configured_isolation(tmp_path, monkeypatch):
    from ai_apitest.web import server
    config = load_config(tmp_path)
    config["sandbox"]["mode"] = "container"
    save_config(tmp_path, config)
    monkeypatch.setattr(server, "run_cases", lambda *args, **kwargs: pytest.fail("should not execute"))
    with pytest.raises(ValueError, match="不能降低"):
        server.execute_web_task(tmp_path, "demo", "run", {"mode": "full", "isolation": "process"}, lambda *args, **kwargs: None)


def test_container_stages_only_actual_uploads_and_uses_restricted_mounts(tmp_path):
    file = tmp_path / "payload.txt"
    file.write_text("synthetic")
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    cases = [Case("C1", "raw", "POST", "/", body={"$iatest_file": "{{file}}"}, variables={"file": "payload.txt"}), Case("C2", "business", "POST", "/", body={"file_path": "/server/data"})]
    payload = {"cases": [asdict(case) for case in cases], "config": {"execution": {"namespace": "run"}}, "environment": {}}
    prepare_container(payload, cases, {}, policy(tmp_path, mode="container"), input_dir, 1000)
    assert payload["cases"][0]["body"]["$iatest_file"].startswith("/input/files/")
    assert payload["cases"][1]["body"] == {"file_path": "/server/data"}
    assert [path.read_text() for path in (input_dir / "files").iterdir()] == ["synthetic"]
    command = container_command({"container_image": "iatest:local"}, input_dir, output_dir, "test")
    assert "--read-only" in command and "ALL" in command and "no-new-privileges=true" in command
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--pull") + 1] == "never"
    assert not any("docker.sock" in part for part in command)
    assert len([part for part in command if "type=bind" in part]) == 1
    assert any(part.startswith("/output:rw,noexec,nosuid,size=") for part in command)
    with pytest.raises(ValueError, match="网络"):
        container_command({"container_image": "iatest:local", "container_network": "host"}, input_dir, output_dir, "test")


def test_container_missing_engine_fails_closed(tmp_path, local_api, monkeypatch):
    setup_run(tmp_path, local_api[0], [Case("C1", "test", "GET", "/ok")], mode="container", container_image="iatest:local")
    monkeypatch.setattr("ai_apitest.execution.isolation.shutil.which", lambda name: None)
    with pytest.raises(ValueError, match="不会自动降低"):
        run_cases(tmp_path, project="demo")
    assert not local_api[2]


def test_container_protocol_stores_bounded_results_and_rejects_unknown_output(tmp_path):
    errors = []
    collect_container_output(io.BytesIO(b'IATEST_RESULT 0 {"case_id":"C1"}\nIATEST_COMPLETE\n'), tmp_path, 1, 1000, errors)
    assert not errors and json.loads((tmp_path / "0.json").read_text())["case_id"] == "C1"
    assert (tmp_path / "complete").is_file()
    for data in (b'IATEST_RESULT 5 {}\n', b'arbitrary-output\n', b'x' * 66000):
        errors = []
        collect_container_output(io.BytesIO(data), tmp_path, 1, 10, errors)
        assert errors


@pytest.mark.parametrize("settings", [{"mode": "invalid"}, {"read_paths": "../"}, {"allow_plugins": "false"}, {"allowed_origins": ["file:///tmp"]}])
def test_policy_config_rejects_invalid_values(tmp_path, settings):
    with pytest.raises((ValueError, SandboxViolation)):
        policy(tmp_path, **settings)
