import sys
from types import SimpleNamespace

import pytest

from iatest.core.config import init_project
from iatest.engines import web
from iatest.engines.browser_wait import wait_for_devtools_endpoint
from iatest.execution.control import RunControl


def test_installed_chrome_precedes_unpackaged_chromium(tmp_path, monkeypatch):
    chrome = tmp_path / "chrome"
    chromium = tmp_path / "chromium"
    chrome.touch()
    chromium.touch()
    monkeypatch.delenv("IATEST_CHROMIUM_PATH", raising=False)
    monkeypatch.setattr(
        web.shutil, "which",
        lambda name: {"google-chrome": str(chrome), "chromium": str(chromium)}.get(name),
    )
    assert web.chromium_path() == str(chrome)
    chrome.unlink()
    assert web.chromium_path() == str(chromium)
    monkeypatch.setenv("IATEST_CHROMIUM_PATH", str(chrome))
    assert web.chromium_path() == ""  # An explicit invalid override must fail closed.
    monkeypatch.setenv("IATEST_CHROMIUM_PATH", str(chromium))
    assert web.chromium_path() == str(chromium)


def test_debug_endpoint_waits_for_complete_port_file(tmp_path):
    port_file = tmp_path / "DevToolsActivePort"
    writes = iter(["", "43210\n", "43210\n/devtools/browser/test\n"])
    waits = []

    def wait(seconds):
        waits.append(seconds)
        port_file.write_text(next(writes))

    endpoint = wait_for_devtools_endpoint(
        port_file,
        SimpleNamespace(poll=lambda: None),
        SimpleNamespace(check=lambda: None, wait=wait),
    )
    assert endpoint == (43210, "/devtools/browser/test")
    assert len(waits) == 3


@pytest.mark.parametrize("contents", ["", "43210\n", "70000\n/devtools/browser/test\n"])
def test_debug_endpoint_partial_file_obeys_deadline(tmp_path, monkeypatch, contents):
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text(contents)
    clock = [0.0]
    monkeypatch.setattr("iatest.engines.browser_wait.time.monotonic", lambda: clock[0])
    control = SimpleNamespace(
        check=lambda: None,
        wait=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    with pytest.raises(RuntimeError, match="Chromium startup failed or timed out"):
        wait_for_devtools_endpoint(port_file, SimpleNamespace(poll=lambda: None), control, timeout=0.1)


def test_browser_startup_reports_child_stderr(tmp_path, monkeypatch):
    init_project(tmp_path, "suite")
    # Python rejects Chromium's first argument, exercising a real child exit
    # and log drain without depending on an installed graphical browser.
    monkeypatch.setattr(web, "chromium_path", lambda: sys.executable)
    monkeypatch.setattr(
        web, "BrowserProxy",
        lambda scope: SimpleNamespace(address="http://127.0.0.1:1", close=lambda: None),
    )
    if sys.platform == "win32":
        # The deliberately failing child can exit before Job Object attachment.
        monkeypatch.setattr(
            "iatest.execution.windows_job.attach_job", lambda *args, **kwargs: lambda: None,
        )
    with pytest.raises(RuntimeError, match="Chromium startup.*[Uu]nknown option"):
        web.BrowserSession(
            tmp_path, "suite",
            {"start_url": "http://127.0.0.1:1", "allowed_origins": ["http://127.0.0.1:1"]},
            RunControl(timeout_seconds=5),
        )
