# ruff: noqa: F811 -- Pytest fixture import is intentionally a parameter name.
from pathlib import Path
from types import SimpleNamespace

from test_web_advanced import advanced  # noqa: F401 -- live Chromium fixture

from iatest.engines.web import BrowserSession
from iatest.execution.control import RunControl
from iatest.mobile.system import WebViewBrowser


def test_borrowed_webview_controls_real_page_and_only_removes_owned_forward(advanced):
    root, base, _ = advanced
    owner = BrowserSession(
        root,
        "suite",
        {"start_url": base, "allowed_origins": [base]},
        RunControl(timeout_seconds=25),
    )
    port = (Path(owner.temp.name) / "DevToolsActivePort").read_text().splitlines()[0]
    calls = []

    def adb(*args, **kwargs):
        calls.append(args)
        if args == ("shell", "pidof", "com.example.app"):
            return "12345"
        if args == ("shell", "cat", "/proc/net/unix"):
            return "@webview_devtools_remote_12345"
        if args[:2] == ("forward", "tcp:0"):
            return port
        return ""

    borrowed = None
    try:
        device = SimpleNamespace(
            profile={
                "platform": "android",
                "app_id": "com.example.app",
                "allow_webview": True,
            },
            adb=adb,
        )
        target = owner.pages["main"]["target"]
        borrowed = WebViewBrowser(
            device,
            root,
            "suite",
            {"allowed_origins": [base], "page_id": target},
            owner.control,
            20,
        )
        selector = {"id": "field", "frame": [{"selector": "#same", "origin": base}]}
        borrowed.act({"op": "fill", "target": selector, "value": "hybrid"})
        assert borrowed.assertion(
            {"target": selector, "assert": "value", "expected": "hybrid"}
        )[0]
        borrowed.close()
        borrowed = None
        assert owner.assertion(
            {"target": selector, "assert": "value", "expected": "hybrid"}
        )[0]
        assert calls[-1] == ("forward", "--remove", "tcp:" + port)
    finally:
        if borrowed:
            borrowed.close()
        owner.close()


def test_wk_input_uses_geometry_after_native_container_resolution(advanced, monkeypatch):
    """Move a real page at the native boundary; this is not an iPhone test."""
    from iatest.mobile.webkit import WKWebViewBrowser

    root, base, _ = advanced
    browser = BrowserSession(root, "suite", {"start_url": base, "allowed_origins": [base]}, RunControl(timeout_seconds=25))
    try:
        browser.evaluate("""(()=>{document.body.innerHTML='<button id="moving" style="position:absolute;left:20px;top:20px;width:90px;height:40px">Save</button><p id="result">Waiting</p>';document.querySelector('#moving').onclick=()=>document.querySelector('#result').textContent='Saved';return true})()""")
        handle = browser.evaluate("document.querySelector('#moving')", object_result=True)["objectId"]

        class NativeBoundary:
            def request(self, op, arguments, **kwargs):
                if op == "query":
                    # Model the layout change while XCTest resolves its container.
                    browser.evaluate("document.querySelector('#moving').style.top='140px'")
                    return {"complete": True, "match_count": 1, "candidates": [{"ref": "owned-container"}]}
                assert op == "web_tap"
                for kind in ("mousePressed", "mouseReleased"):
                    browser.call("Input.dispatchMouseEvent", {"type": kind, "x": arguments["x"], "y": arguments["y"], "button": "left", "clickCount": 1})
                return {"accepted": True}

        def function(reference, source, **kwargs):
            reply = browser.call("Runtime.callFunctionOn", {"objectId": reference, "functionDeclaration": source, "returnByValue": True})
            assert "exceptionDetails" not in reply
            return reply["result"]["value"]

        wk = WKWebViewBrowser.__new__(WKWebViewBrowser)
        wk.device = SimpleNamespace(profile={}, semantic=NativeBoundary())
        wk.pid, wk.control, wk.scope = 17, browser.control, browser.scope
        wk.channel = SimpleNamespace(target="owned-root")
        wk.handles = [handle]
        wk.native_target = lambda: {"identifier": "owned-container"}
        wk.query = lambda target: {"unique": True, "candidates": [{"secure": False}]}
        wk.function = function
        wk.root_point = lambda point: point
        wk.evaluate = lambda expression, **kwargs: browser.evaluate(expression)
        monkeypatch.setattr("iatest.mobile.webkit.verify_pid", lambda *args: None)
        wk.act({"op": "click", "target": {"id": "moving"}})
        assert browser.evaluate("document.querySelector('#result').textContent") == "Saved"
    finally:
        browser.close()
