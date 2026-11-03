import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from iatest.application.testing_service import execute_test_plan
from iatest.core.config import init_project
from iatest.engines.security_scan import import_library


@pytest.mark.parametrize("vulnerable", [True, False])
@pytest.mark.parametrize("mode", ["policy", "process"])
def test_scoped_crawler_and_controlled_catalogue_chain(tmp_path, vulnerable, mode):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            calls.append(self.path)
            query = parse_qs(urlsplit(self.path).query)
            status = 403 if query.get("mode") == ["control"] else 200
            body = (
                json.dumps({"exposed": vulnerable}).encode()
                if query
                else b'<a href="/lab/child">inside</a><a href="/outside">outside</a><a href="http://example.invalid/">outside origin</a>'
            )
            self.send_response(status)
            self.send_header(
                "Content-Type", "application/json" if query else "text/html"
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    init_project(tmp_path, "suite")
    from iatest.core.config import load_config, save_config

    config = load_config(tmp_path)
    config["sandbox"]["mode"] = mode
    save_config(tmp_path, config)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        library = import_library(
            tmp_path,
            "suite",
            {
                "id": "known-lab",
                "version": "1",
                "source": "reviewed deterministic lab",
                "rules": [
                    {
                        "id": "access-control",
                        "title": "Protected resource exposed after reproducible denial control",
                        "severity": "high",
                        "chain": [
                            {
                                "request": {"url": "{{url}}?mode=control"},
                                "control": {"status": [403]},
                            },
                            {
                                "request": {"url": "{{url}}?mode=probe"},
                                "finding": {
                                    "status": [200],
                                    "json_path": "$.exposed",
                                    "equals": True,
                                },
                            },
                        ],
                    }
                ],
            },
        )
        value = {
            "id": "scan",
            "kind": "security",
            "spec": {
                "allowed_origins": [base],
                "scan": {
                    "roots": [base + "/lab/"],
                    "path_prefixes": ["/lab/"],
                    "library_id": library["artifact_id"],
                    "library_digest": library["sha256"],
                    "max_depth": 1,
                },
            },
        }
        result = execute_test_plan(tmp_path, "suite", value)
        assert result["verdict"] == ("failed" if vulnerable else "passed"), result
        assert result["metrics"]["pages"] == 2
        assert not any(path.startswith("/outside") for path in calls)
        assert bool(result["metrics"]["findings"]) == vulnerable
        value["spec"]["scan"]["library_digest"] = "0" * 64
        assert execute_test_plan(tmp_path, "suite", value)["verdict"] == "inconclusive"
    finally:
        server.shutdown()
        server.server_close()
