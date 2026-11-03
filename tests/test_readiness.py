import os

import pytest

from iatest.core.config import init_project
from iatest.engines.readiness import browser_prerequisites, check_browser
from iatest.engines.web import WebEngine
from iatest.mobile.runtime import MobileEngine


def test_installed_driver_does_not_claim_verified_runtime(tmp_path, monkeypatch):
    driver = tmp_path / "native-driver"
    driver.write_text("not a runnable browser")
    monkeypatch.setenv("IATEST_FIREFOX_DRIVER_PATH", str(driver))
    init_project(tmp_path, "suite")
    capability = WebEngine(tmp_path, "suite").capabilities()
    assert capability["backends"]["firefox"] is True
    assert capability["readiness"]["firefox"]["state"] == "requires_verification"
    assert capability["readiness"]["firefox"]["runtime_verified"] is False


def test_missing_driver_and_unconfigured_device_are_not_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("IATEST_FIREFOX_DRIVER_PATH", str(tmp_path / "missing"))
    assert browser_prerequisites()["firefox"]["state"] == "missing_prerequisites"
    result = check_browser("firefox")
    assert result["runtime_verified"] is False
    assert result["state"] == "missing_prerequisites"
    readiness = MobileEngine(tmp_path, "suite", "android").capabilities()["readiness"]
    assert readiness["state"] == "requires_configuration"
    assert readiness["runtime_verified"] is False
    assert not (tmp_path / ".iatest").exists()


@pytest.mark.parametrize("browser", ["chromium", "firefox"])
def test_real_browser_readiness_requires_business_readback(browser):
    variable = (
        "IATEST_CHROMIUM_PATH" if browser == "chromium" else "IATEST_FIREFOX_PATH"
    )
    if not os.environ.get(variable):
        pytest.skip("Requires an explicit real browser installation")
    result = check_browser(browser)
    assert result["state"] == "ready", result
    assert result["runtime_verified"] is True
    assert result["verdict"] == "passed"
    assert result["scope"] == "owned_loopback_fixture"
    assert result["model_calls"] == 0
