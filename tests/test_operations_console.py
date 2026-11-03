import base64
from urllib.parse import urlparse

import pytest

from iatest.application.test_assets import template_bytes
from iatest.core.config import init_project
from iatest.engines.store import EngineStore
from iatest.web.i18n import set_current_locale
from iatest.web.operations import TABS, handle_operation
from iatest.web.testing import render_testing


@pytest.mark.parametrize("locale", ["zh", "en", "ja", "fr", "ru"])
def test_new_console_tabs_render_and_templates_preview_without_execution(
    tmp_path, locale
):
    init_project(tmp_path, "suite")
    set_current_locale(locale)

    class Handler:
        location = ""

        def redirect(self, location):
            self.location = location

    handler = Handler()
    handle_operation(
        tmp_path,
        "suite",
        "import-preview",
        {
            "filename": ["goals.xlsx"],
            "encoded": [base64.b64encode(template_bytes("xlsx")).decode()],
        },
        handler,
    )
    batch = EngineStore(tmp_path, "suite").artifacts("import_batch")[0]
    assert batch["model_calls"] == 0
    for tab in TABS:
        html = render_testing(tmp_path, "suite", urlparse("/testing?tab=" + tab))
        assert '<nav class="workspace-' in html
        assert "/actions/testing-" in html
    assert "import-commit" in render_testing(
        tmp_path, "suite", urlparse("/testing?tab=assets")
    )
    performance_html = render_testing(tmp_path, "suite", urlparse("/testing?tab=client_performance"))
    assert "data-ios-isolated-recording" in performance_html
    assert "isolated_copy" in performance_html
    handle_operation(
        tmp_path, "suite", "import-commit", {"batch_id": [batch["id"]]}, handler
    )
    html = render_testing(tmp_path, "suite", urlparse("/testing?tab=assets"))
    assert "testing-import-process" in html and "testing-import-commit" not in html


def test_ios_calibration_console_submits_scoped_spec_and_preserves_rejection(
    tmp_path, monkeypatch
):
    import json

    from iatest.mobile import ios_calibration
    from iatest.web import server
    from iatest.web.operations import execute_operation

    init_project(tmp_path, "suite")
    payloads = []
    monkeypatch.setattr(
        server, "start_task", lambda *args, **kwargs: payloads.append(kwargs) or True
    )
    handler = type("Handler", (), {"redirect": lambda self, location: None})()
    spec = {"profile": "lab", "mode": "scroll"}
    handle_operation(
        tmp_path, "suite", "ios-calibrate", {"content": [json.dumps(spec)]}, handler
    )
    assert payloads[0]["kind"] == "testing-ios-calibrate"
    assert payloads[0]["payload"] == {"spec": spec}
    calls = []

    def rejected(root, project, actual, control):
        calls.append((project, actual))
        return {"id": "ios-cal-rejected", "status": "rejected"}

    monkeypatch.setattr(ios_calibration, "run_calibration", rejected)
    result = execute_operation(
        tmp_path,
        "suite",
        "testing-ios-calibrate",
        {"spec": spec},
        lambda *args, **kwargs: None,
    )
    assert calls == [("suite", spec)]
    assert json.loads(result)["status"] == "rejected"
