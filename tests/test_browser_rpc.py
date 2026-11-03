# ruff: noqa: F811 -- Imported pytest fixtures are intentionally parameter names.
import os

import pytest
from test_web_advanced import advanced  # noqa: F401 -- shared live browser fixture

from iatest.core.config import load_config, save_config
from iatest.engines.web import BrowserSession
from iatest.execution.control import RunControl


def test_persistent_browser_process_excludes_secrets_and_blocks_arbitrary_scripts(
    advanced,
):
    root, base, _ = advanced
    config = load_config(root)
    config["sandbox"].update(mode="process", memory_mb=2048)
    save_config(root, config)
    session = BrowserSession(
        root,
        "suite",
        {"start_url": base, "allowed_origins": [base]},
        RunControl(timeout_seconds=25),
        max_queries=15,
    )
    process = session.process
    try:
        assert process.pid != os.getpid()
        assert session.query({"selector": "#same"})["unique"]
        target = {"id": "field", "frame": [{"selector": "#same", "origin": base}]}
        session.act({"op": "fill", "target": target, "value": "RPC"})
        passed, _ = session.assertion(
            {"target": target, "assert": "value", "expected": "RPC"}
        )
        assert passed
        assert session.queries >= 3
        with pytest.raises(ValueError, match="arbitrary"):
            session.evaluate("document.cookie")
    finally:
        session.close()
    assert process.poll() is not None


def test_browser_process_cancel_reaps_owned_worker(advanced):
    root, base, _ = advanced
    config = load_config(root)
    config["sandbox"].update(mode="process", memory_mb=2048)
    save_config(root, config)
    control = RunControl(timeout_seconds=25)
    session = BrowserSession(
        root, "suite", {"start_url": base, "allowed_origins": [base]}, control
    )
    try:
        control.cancelled.set()
        with pytest.raises(RuntimeError):
            session.query({"id": "same"})
    finally:
        session.close()
    assert session.process.poll() is not None
