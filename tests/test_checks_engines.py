import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest

from iatest.application.testing_service import execute_test_plan
from iatest.core.config import init_project
from iatest.engines.checks import example_plan
from iatest.engines.contracts import ExecutionPlan
from iatest.engines.validation import validate_plan
from iatest.engines.web import chromium_path
from iatest.web.i18n import current_locale, set_current_locale
from iatest.web.testing import render_testing


@pytest.fixture
def lab(tmp_path):
    previous_locale = current_locale()
    init_project(tmp_path, "suite")
    calls = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_GET(self):
            calls.append(self.path)
            data = (
                b'{"ok":true}'
                if self.path == "/api"
                else b'<html><head><title>Fixture</title></head><body><button id=good aria-label=Save></button><button id=bad></button><img id=decorative width=20 height=20 alt=""><img id=missing width=20 height=20></body></html>'
            )
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "application/json" if self.path == "/api" else "text/html",
            )
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield tmp_path, f"http://127.0.0.1:{server.server_port}", calls
    finally:
        set_current_locale(previous_locale)
        server.shutdown()
        server.server_close()


def test_data_gates_report_rows_without_persisting_sensitive_record_values(lab):
    root, _, _ = lab
    plan = {
        "id": "data",
        "kind": "data",
        "spec": {
            "records": [{"id": 1, "secret": "business-canary"}, {"id": 1}, {}],
            "checks": [
                {"id": "unique", "rule": "unique", "path": "$.id"},
                {"id": "nonnull", "rule": "not_null", "path": "$.id"},
                {"id": "foreign", "rule": "references", "path": "$.id", "allowed": [1]},
                {
                    "id": "schema",
                    "rule": "schema",
                    "schema": {"type": "object", "required": ["id"]},
                },
                {"id": "count", "rule": "count", "min": 3, "max": 3},
            ],
        },
    }
    result = execute_test_plan(root, "suite", plan)
    assert result["verdict"] == "failed", result
    assert result["steps"][0]["failure_rows"] == [1, 2]
    assert result["steps"][-1]["verdict"] == "passed"
    assert "business-canary" not in str(result)


def test_protocol_contract_checks_real_http_version_headers_and_json(lab):
    root, base, calls = lab
    plan = example_plan("protocol")
    plan["spec"].update(
        allowed_origins=[base],
        checks=[
            {
                "id": "contract",
                "request": {"url": base + "/api"},
                "http_version": "HTTP/1.1",
                "headers": {"Content-Type": "application/json"},
                "json_schema": {
                    "type": "object",
                    "properties": {"ok": {"const": True}},
                    "required": ["ok"],
                },
            }
        ],
    )
    assert execute_test_plan(root, "suite", plan)["verdict"] == "passed"
    plan["spec"]["checks"][0]["http_version"] = "HTTP/2"
    assert execute_test_plan(root, "suite", plan)["verdict"] == "failed"
    assert calls == ["/api", "/api"]


def test_security_rule_registry_checks_policy_without_reporting_response_values(lab):
    root, base, _ = lab
    plan = {
        "id": "security-extensions",
        "kind": "security",
        "spec": {
            "allowed_origins": [base],
            "checks": [
                {
                    "id": "clean",
                    "rule": "sensitive_exposure",
                    "request": {"url": base + "/api"},
                    "options": {"forbidden_paths": ["$.secret"]},
                },
                {
                    "id": "exposed",
                    "rule": "sensitive_exposure",
                    "request": {"url": base + "/api"},
                    "options": {"forbidden_paths": ["$.ok"]},
                },
                {
                    "id": "schema",
                    "rule": "response_schema",
                    "request": {"url": base + "/api"},
                    "options": {"schema": {"type": "object", "required": ["missing"]}},
                },
            ],
        },
    }
    result = execute_test_plan(root, "suite", plan)
    assert result["verdict"] == "failed", result
    assert [r["verdict"] for r in result["steps"]] == ["passed", "failed", "failed"]
    assert result["metrics"]["findings"] == 2


def test_targeted_accessibility_uses_native_accessible_name_and_alt(lab):
    if not chromium_path():
        pytest.skip("Requires Chromium")
    root, base, _ = lab
    plan = {
        "id": "a11y",
        "kind": "accessibility",
        "spec": {
            "start_url": base,
            "allowed_origins": [base],
            "checks": [
                {"id": "title", "rule": "document_title", "expected": "Fixture"},
                {
                    "id": "named",
                    "rule": "accessible_name",
                    "target": {"id": "good"},
                    "expected": "Save",
                },
                {"id": "unnamed", "rule": "accessible_name", "target": {"id": "bad"}},
                {
                    "id": "decorative",
                    "rule": "image_alt",
                    "target": {"id": "decorative"},
                },
                {"id": "missing", "rule": "image_alt", "target": {"id": "missing"}},
            ],
        },
    }
    result = execute_test_plan(root, "suite", plan)
    assert result["verdict"] == "failed", result
    assert [r["verdict"] for r in result["steps"]] == [
        "passed",
        "passed",
        "failed",
        "passed",
        "failed",
    ]


@pytest.mark.parametrize("locale", ["zh", "en", "ja", "fr", "ru"])
def test_finite_checks_have_valid_examples_and_accessible_console(lab, locale):
    root, _, _ = lab
    set_current_locale(locale)
    for kind in ("data", "protocol", "accessibility"):
        validate_plan(ExecutionPlan.from_dict(example_plan(kind)))
        html = render_testing(root, "suite", urlparse("/testing?tab=" + kind))
        assert kind + "-example" in html and 'name="content"' in html


def test_unavailable_schema_is_inconclusive_without_fetching_or_finding(lab):
    root, base, calls = lab
    unavailable = {"$ref": "https://untrusted.invalid/schema.json"}
    plans = [
        {
            "id": "unavailable-data",
            "kind": "data",
            "spec": {
                "records": [{}],
                "checks": [{"id": "schema", "rule": "schema", "schema": unavailable}],
            },
        },
        {
            "id": "unavailable-security",
            "kind": "security",
            "spec": {
                "allowed_origins": [base],
                "checks": [
                    {
                        "id": "schema",
                        "rule": "response_schema",
                        "request": {"url": base + "/api"},
                        "options": {"schema": unavailable},
                    }
                ],
            },
        },
    ]
    for plan in plans:
        result = execute_test_plan(root, "suite", plan)
        assert result["verdict"] == "inconclusive", result
        if plan["kind"] == "security":
            assert result["metrics"]["findings"] == 0
    assert calls == ["/api"]
