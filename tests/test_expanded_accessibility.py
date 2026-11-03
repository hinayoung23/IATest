import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from iatest.application.testing_service import execute_test_plan
from iatest.core.config import init_project, load_config, save_config
from iatest.engines.accessibility import criteria, import_review
from iatest.engines.web import chromium_path


@pytest.mark.parametrize("mode", ["policy", "process"])
def test_wcag_checklist_keeps_manual_gaps_and_binds_review_to_actual_build(
    tmp_path, mode
):
    if not chromium_path():
        pytest.skip("Requires local Chromium")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = b'<!doctype html><html lang="en"><head><title>Audit fixture</title></head><body><h1>Test</h1><button>Save</button></body></html>'
            if self.path == "/bad":
                body = (
                    b'<html><body><img src="data:,test"><button></button></body></html>'
                )
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    init_project(tmp_path, "suite")
    config = load_config(tmp_path)
    config["sandbox"]["mode"] = mode
    save_config(tmp_path, config)
    base = f"http://127.0.0.1:{server.server_port}"
    plan = {
        "id": "wcag",
        "kind": "accessibility",
        "spec": {
            "allowed_origins": [base],
            "start_url": base + "/",
            "audit": {"level": "AA"},
        },
    }
    try:
        initial = execute_test_plan(tmp_path, "suite", plan)
        assert initial["lifecycle"] == "completed", initial
        assert initial["verdict"] == "inconclusive", initial
        assert initial["metrics"]["needs_review"] > 40
        assert (
            len(criteria()) == 87 and sum(not c["obsolete"] for c in criteria()) == 86
        )
        frozen = import_review(
            tmp_path,
            "suite",
            {
                "id": "review",
                "start_url": base + "/",
                "build_sha256": initial["metrics"]["build_sha256"],
                "reviews": [
                    {
                        "criterion": "1.1.1",
                        "status": "passed",
                        "reviewer": "human fixture reviewer",
                        "evidence_sha256": "a" * 64,
                        "notes": "Manually checked all non-text content in this fixture.",
                    }
                ],
            },
        )
        plan["spec"]["audit"].update(
            review_artifact="review", review_digest=frozen["sha256"]
        )
        reviewed = execute_test_plan(tmp_path, "suite", plan)
        assert (
            reviewed["metrics"]["needs_review"]
            == initial["metrics"]["needs_review"] - 1
        ), reviewed
        assert reviewed["verdict"] == "inconclusive"
        plan["spec"]["start_url"] = base + "/bad"
        assert execute_test_plan(tmp_path, "suite", plan)["verdict"] == "inconclusive"
        plan["spec"]["audit"] = {"level": "AA"}
        broken = execute_test_plan(tmp_path, "suite", plan)
        assert broken["verdict"] == "failed", broken
        assert {"1.1.1", "4.1.2"} <= {
            f["criterion"] for f in broken["metrics"]["findings"]
        }
    finally:
        server.shutdown()
        server.server_close()
