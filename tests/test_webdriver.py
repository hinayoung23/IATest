import base64
import hashlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from test_web_advanced import advanced_http

from iatest.application.testing_service import execute_test_plan
from iatest.engines.web import BrowserSession
from iatest.execution.control import RunControl


@pytest.fixture
def advanced(tmp_path):
    # W3C transport tests and Firefox must not require a separate Chromium binary.
    with advanced_http(tmp_path) as value:
        yield value


@pytest.mark.parametrize(
    "backend,network_policy",
    [("firefox", "strict"), ("webkit", "strict"), ("webkit", "navigation_only")],
)
@pytest.mark.parametrize("proxy_confirmed", [True, False])
def test_w3c_wire_contract_proxy_refusal_and_engine_evidence(
    advanced, backend, network_policy, proxy_confirmed
):
    """Native W3C boundary fixture, separate from real browser acceptance above."""
    root, base, _ = advanced
    state = {"url": base, "commands": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_DELETE(self):
            self.respond(None)

        def do_GET(self):
            value = (
                True
                if self.path == "/status"
                else state["url"]
                if self.path.endswith("/url")
                else "main"
            )
            if self.path.endswith("/screenshot"):
                value = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            self.respond(value)

        def do_POST(self):
            raw = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["commands"].append(self.path)
            value = None
            if self.path == "/session":
                caps = raw["capabilities"]["alwaysMatch"]
                state["capabilities"] = caps
                value = {
                    "sessionId": "owned",
                    "capabilities": {**caps, "browserVersion": "wire-fixture"},
                }
                if not proxy_confirmed:
                    value["capabilities"]["proxy"] = {}
            elif self.path.endswith("/url"):
                state["url"] = raw["url"]
            elif self.path.endswith("/execute/sync"):
                script = raw["script"]
                if script == "return (globalThis.__iatestNodes[0]);":
                    value = {"element-6066-11e4-a52e-4f735466cecf": "native-ref"}
                elif script == "return (location.href);":
                    value = state["url"]
                else:
                    value = {
                        "search_complete": True,
                        "match_count": 1,
                        "references": [
                            {"element-6066-11e4-a52e-4f735466cecf": "native-ref"}
                        ],
                        "candidates": [
                            {
                                "index": 0,
                                "text": "Saved",
                                "enabled": True,
                                "truncated": False,
                                "rect": {"x": 0, "y": 0, "width": 100, "height": 50},
                            }
                        ],
                    }
            self.respond(value)

        def respond(self, value):
            data = json.dumps({"value": value}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    spec = {
        "start_url": base,
        "allowed_origins": [base],
        "browser": backend,
        "network_policy": network_policy,
        "webdriver_url": f"http://127.0.0.1:{server.server_port}",
        "steps": [
            {
                "op": "expect",
                "target": {"id": "status"},
                "assert": "text",
                "expected": "Saved",
            },
            {"op": "screenshot"},
        ],
    }
    try:
        result = execute_test_plan(
            root, "suite", {"id": "w3c-boundary", "kind": "web", "spec": spec}
        )
        if proxy_confirmed or network_policy == "navigation_only":
            assert result["verdict"] == "passed", result
            assert result["metrics"]["cdp_calls"] > 0
            assert result["evidence"][0]["type"] == "screenshot"
            assert result["metrics"]["network_policy"] == network_policy
            assert result["metrics"]["background_requests_restricted"] is (
                network_policy == "strict"
            )
            if network_policy == "navigation_only":
                assert "proxy" not in state["capabilities"]
                assert result["metrics"]["blocked_proxy_connections"] is None
        else:
            assert result["verdict"] == "inconclusive"
            assert not any(p.endswith("/url") for p in state["commands"])
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("backend", ["firefox", "webkit"])
def test_real_w3c_rotated_delayed_http_frame_uses_native_input(advanced, backend):
    if backend == "webkit" and os.environ.get("IATEST_SAFARI_ACCEPTANCE") != "1":
        pytest.skip("Requires explicit Safari acceptance opt-in on macOS")
    if backend == "firefox" and (not os.environ.get("IATEST_FIREFOX_DRIVER_PATH") or not os.environ.get("IATEST_FIREFOX_PATH")):
        pytest.skip("Requires explicit native Firefox and geckodriver paths")
    root, base, _ = advanced
    browser = BrowserSession(root, "suite", {
        "start_url": base, "allowed_origins": [base], "browser": backend,
        "network_policy": "navigation_only" if backend == "webkit" else "strict",
    }, RunControl(timeout_seconds=45))
    try:
        browser.evaluate("(()=>{document.body.innerHTML='<iframe id=rotated style=\"position:absolute;left:120px;top:140px;width:500px;height:160px;transform:rotate(12deg)\"></iframe>';document.querySelector('iframe').src='/frame?delayed=1'})()")
        target = {"id": "field", "frame": [{"selector": "#rotated", "origin": base}]}
        browser.act({"op": "fill", "target": target, "value": "Rotated 用户"})
        assert browser.assertion({"assert": "value", "target": target, "expected": "Rotated 用户"})[0]
        browser.act({"op": "click", "target": {"id": "go", "frame": target["frame"]}})
        assert browser.assertion({"assert": "text", "target": {"id": "status", "frame": target["frame"]}, "expected": "Saved"})[0]
        with pytest.raises(ValueError, match="origin|scope|allow"):
            browser.query({"id": "field", "frame": [{"selector": "#rotated", "origin": "http://localhost:1"}]})
        if os.name == "nt" and backend == "firefox":
            browser.select_context(target)
            browser.evaluate("document.querySelector('#status').textContent='Ready'")
            browser.select_context({})
            browser.evaluate("(()=>{const e=document.createElement('div');e.id='cover';Object.assign(e.style,{position:'fixed',inset:'0',zIndex:'99999'});document.body.append(e)})()")
            with pytest.raises(ValueError, match="obscured"):
                browser.act({"op": "click", "target": {"id": "go", "frame": target["frame"]}})
            assert not browser.pending
            browser.select_context({})
            browser.evaluate("document.querySelector('#cover').remove()")
            assert browser.assertion({"assert": "text", "target": {"id": "status", "frame": target["frame"]}, "expected": "Ready"})[0]
    finally:
        browser.close()


@pytest.mark.parametrize("style", [
    "transform:perspective(700px) rotateY(24deg)",
    "rotate:9deg;scale:.85",
    "transform:rotate(8deg);zoom:1.1",
])
@pytest.mark.parametrize("nested", [False, True])
def test_real_firefox_projective_frame_root_pointer_and_overlay(advanced, style, nested):
    if not os.environ.get("IATEST_FIREFOX_DRIVER_PATH") or not os.environ.get("IATEST_FIREFOX_PATH"):
        pytest.skip("Requires explicit native Firefox and geckodriver paths")
    root, base, _ = advanced
    browser = BrowserSession(root, "suite", {
        "browser": "firefox", "start_url": base, "allowed_origins": [base],
    }, RunControl(timeout_seconds=45))
    try:
        browser.evaluate("""(()=>{document.body.innerHTML='<section style="transform:translate(20px,10px);perspective:900px"><iframe id="quad" src="/frame" style="position:relative;left:120px;top:120px;width:450px;height:180px;border:7px solid blue;padding:4px"></iframe></section>';return true})()""")
        browser.evaluate("document.querySelector('#quad').style.cssText+=" + json.dumps(";" + style))
        target = {"id": "go", "frame": [{"selector": "#quad", "origin": base}]}
        if nested:
            browser.select_context(target)
            browser.evaluate("document.body.innerHTML='<iframe id=\"nested\" src=\"/frame\" style=\"width:370px;height:120px;border:5px solid green;transform:perspective(600px) rotateY(-12deg)\"></iframe>'")
            target = {**target, "frame": [*target["frame"], {"selector": "#nested", "origin": base}]}
        observed = browser.query(target, cache=False)
        browser._click_frame_from_root(observed["candidates"][0]["webdriver_ref"])
        assert browser.assertion({"target": {**target, "id": "status"}, "assert": "text", "expected": "Saved"})[0]
        browser.select_context({})
        browser.evaluate("document.body.insertAdjacentHTML('beforeend','<div style=\"position:fixed;inset:0;z-index:99999\"></div>')")
        observed = browser.query(target, cache=False)
        with pytest.raises(ValueError, match="obscured"):
            browser._click_frame_from_root(observed["candidates"][0]["webdriver_ref"])
        assert not browser.pending
    finally:
        browser.close()


@pytest.mark.parametrize("backend", ["firefox", "webkit"])
def test_real_native_w3c_transport_and_interactions(advanced, backend):
    if backend == "webkit" and os.environ.get("IATEST_SAFARI_ACCEPTANCE") != "1":
        pytest.skip("Requires explicit Safari acceptance opt-in on macOS")
    if backend == "firefox" and (
        not os.environ.get("IATEST_FIREFOX_DRIVER_PATH")
        or not os.environ.get("IATEST_FIREFOX_PATH")
    ):
        pytest.skip("Requires explicit native Firefox and geckodriver paths")
    root, base, _ = advanced
    browser = BrowserSession(
        root,
        "suite",
        {
            "start_url": base,
            "allowed_origins": [base],
            "browser": backend,
            "network_policy": "navigation_only" if backend == "webkit" else "strict",
        },
        RunControl(timeout_seconds=90),
    )
    temporary = Path(browser.driver_temp.name)
    try:
        assert all(
            Path(value).is_relative_to(temporary)
            for value in browser.firefox_environment.values()
        )
        target = {"id": "field", "frame": [{"selector": "#same", "origin": base}]}
        browser.act({"op": "fill", "target": target, "value": "Firefox"})
        assert browser.assertion(
            {"assert": "value", "target": target, "expected": "Firefox"}
        )[0]
        browser.act({"op": "click", "target": {"id": "go", "frame": target["frame"]}})
        assert browser.assertion(
            {
                "assert": "text",
                "target": {"id": "status", "frame": target["frame"]},
                "expected": "Saved",
            }
        )[0]
        assert (
            "safari" if backend == "webkit" else "firefox"
        ) in browser.version.lower()
        assert browser.query(
            {"role": "button", "name": "Save", "frame": target["frame"]}
        )["unique"]
        browser.evaluate(
            "document.querySelector('#status').textContent='Before visual'"
        )
        button = {"id": "go", "frame": target["frame"]}
        region = browser.visual_region(button)
        anchor = {
            **{k: region[k] for k in ("sha256", "width", "height", "viewport")},
            "x": region["width"] / 2,
            "y": region["height"] / 2,
        }
        browser.visual_click({"target": button, "visual_anchor": anchor})
        assert browser.assertion(
            {
                "assert": "text",
                "target": {"id": "status", "frame": target["frame"]},
                "expected": "Saved",
            }
        )[0]
        browser.query(button)
        assert (
            len(
                browser._session_call(
                    browser.sid, "Page.captureScreenshot", {"clip": region["rect"]}
                )["data"]
            )
            > 100
        )
        payload = b"Firefox bounded local download"
        href = "data:text/plain;base64," + base64.b64encode(payload).decode()
        browser.evaluate(
            "(()=>{const a=document.createElement('a');a.id='owned-download';a.href="
            + json.dumps(href)
            + ";a.download='owned.txt';a.textContent='Download';document.body.append(a);})()"
        )
        data, result = browser.download(
            {
                "target": {"id": "owned-download", "frame": target["frame"]},
                "expected_sha256": hashlib.sha256(payload).hexdigest(),
                "expected_name": "owned.txt",
            }
        )
        assert data == payload and result["passed"]
        assert (
            len(browser._session_call(browser.sid, "Page.captureScreenshot")["data"])
            > 100
        )
    finally:
        browser.close()
    assert not temporary.exists()


def test_webdriver_rejects_remote_unauthenticated_driver_before_dispatch(advanced):
    root, base, _ = advanced
    with pytest.raises(ValueError, match="loopback"):
        BrowserSession(
            root,
            "suite",
            {
                "start_url": base,
                "allowed_origins": [base],
                "browser": "webkit",
                "webdriver_url": "http://example.invalid:4444",
            },
            RunControl(timeout_seconds=3),
        )


@pytest.mark.parametrize(
    "restriction",
    ["project_origins", "project_networks", "parent_origins", "parent_networks"],
)
@pytest.mark.parametrize("isolation", ["policy", "process"])
def test_safari_navigation_policy_cannot_weaken_network_restrictions(
    advanced, restriction, isolation
):
    from iatest.core.config import load_config, save_config
    from iatest.core.sandbox import SandboxPolicy, policy_scope

    root, base, _ = advanced
    config = load_config(root)
    config["sandbox"]["mode"] = isolation
    save_config(root, config)
    narrowed = dict(config["sandbox"])
    narrowed["allowed_origins"] = [base]
    if restriction.endswith("networks"):
        narrowed["allowed_networks"] = ["127.0.0.0/8"]
        if restriction.startswith("project"):
            narrowed["allowed_origins"] = []
    parent = None
    if restriction.startswith("parent"):
        parent = SandboxPolicy.from_config(
            root, {**config, "sandbox": narrowed}, project="suite"
        )
    else:
        save_config(root, {**config, "sandbox": narrowed})
    with policy_scope(parent), pytest.raises(ValueError, match="cannot enforce"):
        BrowserSession(
            root,
            "suite",
            {
                "start_url": base,
                "allowed_origins": [base],
                "browser": "webkit",
                "network_policy": "navigation_only",
            },
            RunControl(timeout_seconds=3),
        )


@pytest.mark.parametrize("backend", ["chromium", "firefox"])
def test_navigation_policy_requires_explicit_safari_backend(advanced, backend):
    root, base, _ = advanced
    with pytest.raises(ValueError, match="requires the Safari"):
        BrowserSession(
            root,
            "suite",
            {
                "start_url": base,
                "allowed_origins": [base],
                "browser": backend,
                "network_policy": "navigation_only",
            },
            RunControl(timeout_seconds=3),
        )


def test_real_safari_frames_shadow_pages_and_scope(advanced):
    if os.environ.get("IATEST_SAFARI_ACCEPTANCE") != "1":
        pytest.skip("Requires explicit Safari acceptance opt-in on macOS")
    root, base, cross = advanced
    browser = BrowserSession(
        root,
        "suite",
        {
            "start_url": base,
            "allowed_origins": [base, cross],
            "browser": "webkit",
            "network_policy": "navigation_only",
        },
        RunControl(timeout_seconds=90),
    )
    try:
        for selector, origin in [("#same", base), ("#cross", cross)]:
            frame = [{"selector": selector, "origin": origin}]
            target = {"id": "field", "frame": frame}
            browser.act({"op": "fill", "target": target, "value": "Safari 中文"})
            assert browser.assertion(
                {"assert": "value", "target": target, "expected": "Safari 中文"}
            )[0], browser.element_script(
                browser.last_reference, "return arguments[0].value;"
            )
            browser.act({"op": "click", "target": {"id": "go", "frame": frame}})
            assert browser.assertion(
                {
                    "assert": "text",
                    "target": {"id": "status", "frame": frame},
                    "expected": "Saved",
                }
            )[0]
        target = {"id": "shadow-field", "shadow": ["#widget"]}
        browser.act({"op": "fill", "target": target, "value": "shadow input"})
        assert browser.assertion(
            {"assert": "value", "target": target, "expected": "shadow input"}
        )[0]
        browser.evaluate(
            "(()=>{const e=document.querySelector('#widget').shadowRoot.querySelector('#shadow-go'),r=e.getBoundingClientRect(),cover=document.createElement('div');cover.id='cover';Object.assign(cover.style,{position:'fixed',left:r.x+'px',top:r.y+'px',width:r.width+'px',height:r.height+'px',zIndex:99999});document.body.append(cover)})()"
        )
        with pytest.raises(ValueError, match="obscured"):
            browser.act(
                {"op": "click", "target": {"id": "shadow-go", "shadow": ["#widget"]}}
            )
        assert browser.pending is False
        assert browser.assertion(
            {
                "assert": "text",
                "target": {"id": "shadow-status", "shadow": ["#widget"]},
                "expected": "Ready",
            }
        )[0]
        browser.evaluate("document.querySelector('#cover').remove()")
        browser.act(
            {"op": "click", "target": {"id": "shadow-go", "shadow": ["#widget"]}}
        )
        assert browser.assertion(
            {
                "assert": "text",
                "target": {"id": "shadow-status", "shadow": ["#widget"]},
                "expected": "Shadow saved",
            }
        )[0]
        browser.page_action(
            {"op": "open_page", "page": "second", "url": base + "/popup"}
        )
        assert browser.query({"id": "popup-ready"})["unique"]
        browser.page_action({"op": "close_page", "page": "second"})
        browser.act({"op": "click", "target": {"id": "new-window"}})
        browser.page_action(
            {"op": "adopt_popup", "page": "popup", "url": base + "/popup"}
        )
        assert browser.query({"id": "popup-ready"})["unique"]
        browser.page_action({"op": "close_page", "page": "popup"})
        upload = root / "safari-upload.txt"
        upload.write_text("Owned Safari upload fixture")
        browser.upload({"target": {"id": "upload"}, "files": [str(upload)]})
        assert browser.assertion(
            {
                "assert": "text",
                "target": {"id": "upload-result"},
                "expected": upload.name,
            }
        )[0]
        browser.scroll({"op": "scroll", "target": {"id": "list"}, "delta_y": 300})
        browser.control.wait(0.2)
        assert browser.evaluate("document.querySelector('#list').scrollTop") > 0
        calls = browser.cdp.calls
        with pytest.raises(PermissionError, match="outside plan"):
            browser.navigate("https://unapproved.invalid/")
        with pytest.raises(PermissionError, match="outside plan"):
            browser.page_action(
                {
                    "op": "open_page",
                    "page": "unapproved",
                    "url": "https://unapproved.invalid/",
                }
            )
        assert browser.cdp.calls == calls
        assert "unapproved" not in browser.pages
        # A page may initiate requests in this explicit mode; reject observation
        # after leaving the allowed origins rather than claiming blocked traffic.
        with advanced_http(root / "outside") as (_, outside, _):
            browser.evaluate("location.href=" + json.dumps(outside + "/popup"))
            browser.control.wait(0.2)
            assert browser.command("GET", "/url").startswith(outside)
            with pytest.raises(PermissionError, match="outside plan"):
                browser.query({"id": "popup-ready"})
    finally:
        browser.close()


def test_real_safari_independent_randomized_plan_replay(advanced):
    import uuid

    if os.environ.get("IATEST_SAFARI_ACCEPTANCE") != "1":
        pytest.skip("Requires explicit Safari acceptance opt-in on macOS")
    root, base, _ = advanced
    for _ in range(2):
        marker = "Safari 中文 😀 " + uuid.uuid4().hex
        plan = {
            "id": "safari-replay",
            "kind": "web",
            "budget": {"timeout_seconds": 60},
            "spec": {
                "browser": "webkit",
                "network_policy": "navigation_only",
                "allowed_origins": [base],
                "start_url": base + "/frame",
                "steps": [
                    {"op": "fill", "target": {"id": "field"}, "value": marker},
                    {
                        "op": "expect",
                        "assert": "value",
                        "target": {"id": "field"},
                        "expected": marker,
                    },
                    {"op": "click", "target": {"role": "button", "name": "Save"}},
                    {
                        "op": "expect",
                        "assert": "text",
                        "target": {"id": "status"},
                        "expected": "Saved",
                    },
                    {"op": "screenshot", "target": {"id": "status"}},
                    {"op": "screenshot"},
                ],
            },
        }
        result = execute_test_plan(root, "suite", plan)
        assert result["verdict"] == "passed", result
        assert result["metrics"]["network_policy"] == "navigation_only"
        assert result["metrics"]["background_requests_restricted"] is False
        assert result["metrics"]["blocked_proxy_connections"] is None
        assert len(result["evidence"]) == 2
