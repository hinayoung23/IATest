import hashlib
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from iatest.application.testing_service import execute_test_plan
from iatest.core.config import init_project
from iatest.engines.store import EngineStore
from iatest.engines.web import BrowserSession, chromium_path
from iatest.execution.control import RunControl

DATA = b"bounded-download-fixture\n"


def test_closed_shadow_uses_explicit_native_root_and_real_input(advanced):
    root, base, _ = advanced
    browser = BrowserSession(
        root,
        "suite",
        {"start_url": base, "allowed_origins": [base]},
        RunControl(timeout_seconds=15),
    )
    try:
        browser.evaluate(
            "(()=>{document.body.innerHTML='<section id=closed></section><p id=result>Ready</p>';const r=document.querySelector('#closed').attachShadow({mode:'closed'});r.innerHTML='<button id=save>Save</button>';r.querySelector('button').onclick=()=>document.querySelector('#result').textContent='Saved'})()"
        )
        target = {"id": "save", "shadow": ["#closed"]}
        assert browser.query(target)["unique"]
        browser.act({"op": "click", "target": target})
        assert browser.query({"id": "result"})["candidates"][0]["text"] == "Saved"
    finally:
        browser.close()


def test_rotated_and_opaque_srcdoc_frame_require_binding_and_real_input(advanced):
    import json

    root, base, _ = advanced
    source = '<button id="save" onclick="this.textContent=\'Saved\'">Save</button>'
    browser = BrowserSession(
        root,
        "suite",
        {"start_url": base, "allowed_origins": [base]},
        RunControl(timeout_seconds=15),
    )
    try:
        browser.evaluate(
            "document.body.innerHTML='<iframe id=opaque sandbox=allow-scripts style=\"position:absolute;left:150px;top:150px;transform:rotate(15deg);width:300px;height:150px\"></iframe>'"
        )
        browser.evaluate(
            f"document.querySelector('#opaque').srcdoc={json.dumps(source)}"
        )
        browser.control.wait(0.2)
        descriptor = {"selector": "#opaque", "origin": base}
        with pytest.raises(ValueError, match="digest"):
            browser.query({"id": "save", "frame": [descriptor]})
        descriptor["opaque_source_sha256"] = hashlib.sha256(source.encode()).hexdigest()
        target = {"id": "save", "frame": [descriptor]}
        browser.act({"op": "click", "target": target})
        assert browser.query(target)["candidates"][0]["text"] == "Saved"
    finally:
        browser.close()


@pytest.mark.parametrize('cross_origin', [False, True])
def test_perspective_frame_native_input_and_projected_visual_anchor(advanced, cross_origin):
    import json
    from iatest.engines.projective import project

    root, base, cross = advanced
    browser = BrowserSession(root, 'suite', {'start_url': base, 'allowed_origins': [base, cross]},
                             RunControl(timeout_seconds=30))
    try:
        origin = cross if cross_origin else base
        browser.evaluate("document.body.innerHTML='<iframe id=tilted style=\"position:absolute;left:180px;top:140px;width:400px;height:180px;border:4px solid;transform:perspective(500px) rotateY(30deg) rotateZ(8deg)\"></iframe>'")
        browser.evaluate("document.querySelector('#tilted').src=" + json.dumps(origin + '/frame'))
        context = {'frame': [{'selector': '#tilted', 'origin': origin}]}
        field, button = {'id': 'field', **context}, {'id': 'go', **context}
        browser.act({'op': 'fill', 'target': field, 'value': 'perspective native input'})
        assert browser.assertion({'assert':'value', 'target':field, 'expected':'perspective native input'})[0]
        browser.evaluate("document.querySelector('#go').style.cssText='appearance:none;background:#0088aa;color:white;border:0;outline:0;box-shadow:none;transition:none;animation:none';document.querySelector('#field').style.caretColor='transparent'")
        image = browser.visual_region(button)
        rect, clip = image['rect'], image['clip']
        x, y = project(image['matrix'], rect['x'] + rect['width']/2, rect['y'] + rect['height']/2)
        anchor = {k: image[k] for k in ('sha256', 'width', 'height', 'viewport')}
        anchor.update(x=(x-clip['x'])/clip['width']*image['width'],
                      y=(y-clip['y'])/clip['height']*image['height'])
        browser.visual_click({'target': button, 'visual_anchor': anchor})
        deadline = time.monotonic() + 2
        while browser.query({'id': 'status', **context})['candidates'][0]['text'] != 'Saved':
            assert time.monotonic() < deadline, 'Native visual click did not reach child frame'
            browser.control.wait(.05)
        # An ancestor overlay must be rejected before dispatch, even though the
        # child document still reports its own button as the hit target.
        browser.select_context({})
        browser.evaluate("document.body.insertAdjacentHTML('beforeend','<div style=\"position:fixed;inset:0;z-index:10000\"></div>')")
        with pytest.raises(ValueError, match='obscured'):
            browser.act({'op': 'click', 'target': button})
    finally:
        browser.close()


@pytest.mark.parametrize('outer_cross_origin', [False, True])
def test_nested_perspective_frames_map_session_root_quads_once(advanced, outer_cross_origin):
    import json
    root, base, cross = advanced
    browser = BrowserSession(root, 'suite', {'start_url':base, 'allowed_origins':[base,cross]},
                             RunControl(timeout_seconds=30))
    try:
        outer_origin = cross if outer_cross_origin else base
        inner_origin = base if outer_cross_origin else cross
        if outer_cross_origin:
            browser._session_call(browser.page_session, 'Emulation.setDeviceMetricsOverride',
                                  {'width':1280,'height':900,'deviceScaleFactor':2,'mobile':False})
        browser.evaluate("document.body.innerHTML='<iframe id=outer style=\"position:absolute;left:150px;top:100px;width:650px;height:350px;transform:perspective(900px) rotateY(15deg)\"></iframe>'")
        browser.evaluate("document.querySelector('#outer').src=" + json.dumps(outer_origin + '/frame'))
        outer = {'selector':'#outer', 'origin':outer_origin}
        browser.select_context({'frame':[outer]})
        browser.evaluate("document.body.innerHTML='<iframe id=inner style=\"position:absolute;left:60px;top:60px;width:400px;height:180px;transform:perspective(600px) rotateX(12deg) rotateZ(5deg)\"></iframe>'")
        browser.evaluate("document.querySelector('#inner').src=" + json.dumps(inner_origin + '/frame'))
        context = {'frame':[outer, {'selector':'#inner', 'origin':inner_origin}]}
        browser.act({'op':'fill', 'target':{'id':'field',**context}, 'value':'nested perspective'})
        assert browser.assertion({'assert':'value','target':{'id':'field',**context},'expected':'nested perspective'})[0]
        browser.evaluate("document.querySelector('#go').style.cssText='appearance:none;background:#0088aa;color:white;border:0;outline:0;box-shadow:none;transition:none;animation:none';document.querySelector('#field').style.caretColor='transparent'")
        browser.act({'op':'click','target':{'id':'go',**context}})
        assert browser.assertion({'assert':'text','target':{'id':'status',**context},'expected':'Saved'})[0]
        browser.evaluate("document.querySelector('#status').textContent='Ready'")
        target = {'id':'go',**context}
        image = browser.visual_region(target)
        from iatest.engines.projective import project
        rect, clip = image['rect'], image['clip']
        x, y = project(image['matrix'], rect['x']+rect['width']/2, rect['y']+rect['height']/2)
        anchor = {k:image[k] for k in ('sha256','width','height','viewport')}
        anchor.update(x=(x-clip['x'])/clip['width']*image['width'], y=(y-clip['y'])/clip['height']*image['height'])
        browser.visual_click({'target':target,'visual_anchor':anchor})
        assert browser.assertion({'assert':'text','target':{'id':'status',**context},'expected':'Saved'})[0]
    finally:
        browser.close()


@pytest.mark.parametrize("scheme", ["data", "blob"])
def test_local_download_is_bounded_and_requires_digest(advanced, scheme):
    import json

    root, base, _ = advanced
    browser = BrowserSession(
        root,
        "suite",
        {"start_url": base, "allowed_origins": [base]},
        RunControl(timeout_seconds=15),
    )
    try:
        browser.evaluate(
            "document.body.innerHTML='<a id=local download=fixture.txt>File</a>'"
        )
        browser.evaluate(
            "document.querySelector('#local').href="
            + (
                f"'data:text/plain,'+encodeURIComponent({json.dumps(DATA.decode())})"
                if scheme == "data"
                else f"URL.createObjectURL(new Blob([{json.dumps(DATA.decode())}]))"
            )
        )
        step = {
            "target": {"id": "local"},
            "expected_sha256": hashlib.sha256(DATA).hexdigest(),
            "expected_name": "fixture.txt",
        }
        data, meta = browser.download(step)
        assert data == DATA and meta["passed"]
        with pytest.raises(ValueError, match="digest"):
            browser.download({"target": {"id": "local"}})
        with pytest.raises(ValueError):
            browser.download({**step, "max_bytes": 2})
    finally:
        browser.close()


@contextmanager
def advanced_http(tmp_path):
    init_project(tmp_path, "suite")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/file":
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header(
                    "Content-Disposition", 'attachment; filename="fixture.txt"'
                )
                self.send_header("Content-Length", str(len(DATA)))
                self.end_headers()
                self.wfile.write(DATA)
                return
            if path == "/frame":
                if urlsplit(self.path).query == "delayed=1":
                    time.sleep(.3)
                html = '<input id="field"><button id="go" onclick="document.querySelector(\'#status\').textContent=\'Saved\'">Save</button><p id="status">Ready</p><a id="frame-download" href="/file">Download</a>'
            elif path == "/popup":
                html = '<h1 id="popup-ready">Popup ready</h1>'
            else:
                html = """<style>body{margin:20px}iframe{width:500px;height:120px;border:3px solid}
                #list{height:80px;overflow:auto;position:relative;width:300px}</style>
                <iframe id="same" src="/frame"></iframe>
                <iframe id="cross" src="http://localhost:PORT/frame"></iframe>
                <div id="widget"></div>
                <input id="upload" type="file" onchange="document.querySelector('#upload-result').textContent=this.files[0].name">
                <p id="upload-result">empty</p><a id="download" href="/file">Download</a>
                <button id="other-download" onclick="document.querySelector('#same').contentDocument.querySelector('#frame-download').click()">Download elsewhere</button>
                <button id="new-window" onclick="window.open('/popup','_blank')">Open</button>
                <div id="list"><div id="spacer" style="height:1000px"><button id="item0">Item 0</button></div></div>
                <canvas id="board" width="200" height="80"></canvas><p id="canvas-status">Ready</p>
                <script>
                const root=document.querySelector('#widget').attachShadow({mode:'open'});
                root.innerHTML='<input id="shadow-field"><button id="shadow-go">Save shadow</button><p id="shadow-status">Ready</p>';
                root.querySelector('#shadow-go').onclick=()=>root.querySelector('#shadow-status').textContent='Shadow saved';
                const list=document.querySelector('#list');
                list.onscroll=()=>{const n=Math.floor(list.scrollTop/100);document.querySelector('#spacer').innerHTML='<button id="item'+n+'" style="position:absolute;top:'+n*100+'px">Item '+n+'</button>'};
                const board=document.querySelector('#board'),paint=board.getContext('2d');paint.fillStyle='blue';paint.fillRect(20,20,80,30);paint.fillStyle='white';paint.fillText('Save',30,40);
                board.onclick=e=>{if(e.offsetX>=20&&e.offsetX<=100&&e.offsetY>=20&&e.offsetY<=50)document.querySelector('#canvas-status').textContent='Saved'};
                </script>""".replace("PORT", str(self.server.server_port))
            data = ("<!doctype html><html><body>" + html + "</body></html>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html;charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    cross = f"http://localhost:{server.server_port}"
    try:
        yield tmp_path, base, cross
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def advanced(tmp_path):
    if not chromium_path():
        pytest.skip("Requires an installed Chromium")
    with advanced_http(tmp_path) as value:
        yield value


def run(advanced, steps, **budget):
    root, base, cross = advanced
    return execute_test_plan(
        root,
        "suite",
        {
            "id": "advanced-web",
            "kind": "web",
            "budget": {"timeout_seconds": 40, **budget},
            "spec": {
                "start_url": base,
                "allowed_origins": [base, cross],
                "steps": steps,
            },
        },
    )


@pytest.mark.parametrize("frame", ["same", "cross"])
def test_native_frame_query_input_and_postcondition(advanced, frame):
    origin = advanced[1] if frame == "same" else advanced[2]
    context = {"frame": [{"selector": "#" + frame, "origin": origin}]}
    result = run(
        advanced,
        [
            {
                "op": "fill",
                "target": {**context, "id": "field"},
                "value": "native frame input",
            },
            {"op": "click", "target": {**context, "id": "go"}},
            {
                "op": "expect",
                "target": {**context, "id": "field"},
                "assert": "value",
                "expected": "native frame input",
            },
            {
                "op": "expect",
                "target": {**context, "id": "status"},
                "assert": "text",
                "expected": "Saved",
            },
            {"op": "screenshot", "target": {**context, "id": "status"}},
        ],
    )
    assert result["verdict"] == "passed", result
    assert len(result["evidence"]) == 1


def test_cross_frame_query_waits_for_committed_origin(advanced):
    root, base, cross = advanced
    browser = BrowserSession(root, "suite", {"start_url": base, "allowed_origins": [base, cross]}, RunControl(timeout_seconds=20))
    try:
        import json

        browser.evaluate("document.querySelector('#cross').src=" + json.dumps(cross + "/frame?delayed=1"))
        observed = browser.query({"id": "field", "frame": [{"selector": "#cross", "origin": cross}]})
        assert observed["unique"] and observed["match_count"] == 1
    finally:
        browser.close()


def test_open_shadow_root_actions_use_real_input(advanced):
    context = {"shadow": ["#widget"]}
    result = run(
        advanced,
        [
            {
                "op": "fill",
                "target": {**context, "id": "shadow-field"},
                "value": "shadow value",
            },
            {"op": "click", "target": {**context, "id": "shadow-go"}},
            {
                "op": "expect",
                "target": {**context, "id": "shadow-field"},
                "assert": "value",
                "expected": "shadow value",
            },
            {
                "op": "expect",
                "target": {**context, "id": "shadow-status"},
                "assert": "text",
                "expected": "Shadow saved",
            },
        ],
    )
    assert result["verdict"] == "passed", result


def test_region_cache_invalidates_mutations_and_rechecks_visibility(advanced):
    root, base, cross = advanced
    spec = {"start_url": base, "allowed_origins": [base, cross]}
    browser = BrowserSession(root, "suite", spec, RunControl(timeout_seconds=20))
    try:
        browser.evaluate(
            "document.body.insertAdjacentHTML('beforeend','<section id=region><button id=cached>First</button></section>')"
        )
        target = {"id": "cached", "scope": "#region"}
        assert not browser.query(target)["cache_hit"]
        assert browser.query(target)["cache_hit"]
        # Outside-region CSS is not observed; dynamic visibility must still be reevaluated.
        browser.evaluate(
            "document.head.insertAdjacentHTML('beforeend','<style id=hide>#cached{display:none}</style>')"
        )
        hidden = browser.query(target)
        assert hidden["cache_hit"] and hidden["match_count"] == 0
        browser.evaluate(
            "document.querySelector('#hide').remove();document.querySelector('#region').insertAdjacentHTML('beforeend','<button id=cached>Duplicate</button>')"
        )
        duplicate = browser.query(target)
        assert not duplicate["cache_hit"] and not duplicate["unique"]
        assert duplicate["match_count"] == 2
        browser.evaluate(
            "document.querySelector('#region').outerHTML='<section id=region><button id=cached>Replacement</button></section>'"
        )
        replacement = browser.query(target)
        assert not replacement["cache_hit"] and replacement["unique"]
        assert replacement["candidates"][0]["text"] == "Replacement"
        assert not browser.query(target, cache=False)["cache_hit"]
    finally:
        browser.close()


def test_outline_discovers_bounded_contexts_and_retains_locator_context(advanced):
    root, base, cross = advanced
    browser = BrowserSession(
        root,
        "suite",
        {"start_url": base, "allowed_origins": [base, cross]},
        RunControl(timeout_seconds=20),
    )
    try:
        top = browser.outline()
        contexts = [item["context"] for item in top["contexts"]]
        frame = next(
            c for c in contexts if c.get("frame", [{}])[0].get("selector") == "#cross"
        )
        assert frame["frame"][0]["origin"] == cross
        child = browser.outline(ui_context=frame)
        field = next(c for c in child["controls"] if c["target"].get("id") == "field")
        assert field["target"]["frame"] == frame["frame"]
        shadow = browser.outline(ui_context={"shadow": ["#widget"]})
        assert all(
            c["target"]["shadow"] == ["#widget"]
            for c in shadow["controls"]
            if c["target"]
        )
    finally:
        browser.close()


def test_canvas_anchor_revalidates_pixels_and_observes_deterministic_result(advanced):
    root, base, cross = advanced
    browser = BrowserSession(
        root,
        "suite",
        {"start_url": base, "allowed_origins": [base, cross]},
        RunControl(timeout_seconds=20),
    )
    try:
        image = browser.visual_region({"id": "board"})
        anchor = {k: image[k] for k in ("sha256", "width", "height", "viewport")}
        anchor.update(x=60, y=35)
        browser.visual_click(
            {"op": "visual_click", "target": {"id": "board"}, "visual_anchor": anchor}
        )
        assert browser.assertion(
            {"assert": "text", "target": {"id": "canvas-status"}, "expected": "Saved"}
        )[0]
        browser.evaluate(
            "(()=>{const c=document.querySelector('#board').getContext('2d');c.fillStyle='red';c.fillRect(0,0,5,5)})()"
        )
        with pytest.raises(ValueError, match="expired"):
            browser.visual_click(
                {
                    "op": "visual_click",
                    "target": {"id": "board"},
                    "visual_anchor": anchor,
                }
            )
    finally:
        browser.close()


def test_bounded_vision_gateway_sends_image_without_saving_base64_in_agent_trace(
    advanced, monkeypatch
):
    import json

    from iatest.ai.provider import ModelResponse, ModelToolCall, TokenUsage
    from iatest.application.ui_visual import locate_region
    from iatest.core.config import load_config, save_config

    root, base, cross = advanced
    config = load_config(root)
    config["llm"]["master"].update(
        enabled=True,
        base_url="https://fixture.invalid",
        model="fixture-vision",
        api_key_env="IATEST_FIXTURE_VISION_TOKEN",
        supports_images=True,
    )
    save_config(root, config)
    monkeypatch.setenv("IATEST_FIXTURE_VISION_TOKEN", "synthetic-only")
    captured = []

    class Gateway:
        def __init__(self, model_config):
            self.model_config = model_config

        def complete(self, **kwargs):
            captured.extend(kwargs["messages"])
            args = {
                "candidates": [
                    {"x": 20, "y": 20, "width": 80, "height": 30, "score": 0.9}
                ]
            }
            return ModelResponse(
                text="",
                tool_calls=[
                    ModelToolCall(
                        "visual-fixture",
                        "submit_visual_candidates",
                        args,
                        json.dumps(args),
                    )
                ],
                usage=TokenUsage(input_tokens=321, output_tokens=40, total_tokens=361),
            )

    browser = BrowserSession(
        root,
        "suite",
        {"start_url": base, "allowed_origins": [base, cross]},
        RunControl(timeout_seconds=20),
    )
    try:
        value = locate_region(
            root,
            "suite",
            browser,
            {"id": "board"},
            "Save button",
            gateway_factory=Gateway,
        )
        assert value["candidates"][0]["anchor"]["x"] == 60
        assert (
            value["usage"]["input_tokens"] == 321 and value["provider_usage_reported"]
        )
        assert "data:image/png;base64" in str(captured)
        assert "data:image/png;base64" not in str(value)
        from iatest.data.agent_runs import AgentRunRepository

        # Audit stores only textual prompts and image digest/size metadata.
        assert "data:image/png;base64" not in str(
            AgentRunRepository(root, "suite").get(value["agent_run_id"])
        )
    finally:
        browser.close()


def test_upload_and_bounded_download_with_hash_gate(advanced):
    root, _, _ = advanced
    source = root / "fixture.csv"
    source.write_text("id,name\n1,fixture\n")
    result = run(
        advanced,
        [
            {"op": "upload", "target": {"id": "upload"}, "files": [str(source)]},
            {
                "op": "expect",
                "target": {"id": "upload-result"},
                "assert": "text",
                "expected": "fixture.csv",
            },
            {
                "op": "download",
                "target": {"id": "download"},
                "expected_name": "fixture.txt",
                "expected_sha256": hashlib.sha256(DATA).hexdigest(),
            },
        ],
    )
    assert result["verdict"] == "passed", result
    evidence = result["evidence"][0]
    assert evidence["type"] == "download"
    assert (
        EngineStore(root, "suite")
        .evidence_path(result["run_id"], evidence["name"])
        .read_bytes()
        == DATA
    )


def test_download_from_another_frame_is_rejected(advanced):
    result = run(
        advanced,
        [
            {
                "op": "download",
                "target": {"id": "other-download"},
                "expected_name": "fixture.txt",
            }
        ],
    )
    assert result["verdict"] == "inconclusive", result
    assert "another frame" in result["error"], result
    assert not result["evidence"]


def test_download_wrong_hash_is_a_failed_assertion(advanced):
    result = run(
        advanced,
        [{"op": "download", "target": {"id": "download"}, "expected_sha256": "0" * 64}],
    )
    assert result["lifecycle"] == "completed" and result["verdict"] == "failed", result


def test_popup_and_explicit_page_lifecycle(advanced):
    base = advanced[1]
    result = run(
        advanced,
        [
            {"op": "click", "target": {"id": "new-window"}},
            {"op": "expect_popup", "page": "popup", "url": base + "/popup"},
            {
                "op": "expect",
                "target": {"page": "popup", "id": "popup-ready"},
                "assert": "text",
                "expected": "Popup ready",
            },
            {"op": "close_page", "page": "popup"},
            {"op": "open_page", "page": "second", "url": base + "/popup"},
            {"op": "expect", "target": {"id": "popup-ready"}, "assert": "exists"},
            {"op": "switch_page", "page": "main"},
            {"op": "expect", "target": {"id": "upload"}, "assert": "exists"},
        ],
    )
    assert result["verdict"] == "passed", result


def test_virtual_list_scroll_is_bounded_and_observes_business_target(advanced):
    result = run(
        advanced,
        [
            {
                "op": "scroll_find",
                "target": {"id": "item3", "scope": "#list"},
                "container": {"id": "list"},
                "delta_y": 100,
                "max_scrolls": 5,
            },
            {
                "op": "expect",
                "target": {"id": "item3", "scope": "#list"},
                "assert": "text",
                "expected": "Item 3",
            },
        ],
    )
    assert result["verdict"] == "passed", result
    assert result["metrics"]["actions"] >= 3


def test_virtual_list_budget_cannot_prove_absence(advanced):
    result = run(
        advanced,
        [
            {
                "op": "scroll_find",
                "target": {"id": "item9"},
                "container": {"id": "list"},
                "delta_y": 100,
                "max_scrolls": 1,
            },
            {"op": "expect", "target": {"id": "item9"}, "assert": "absent"},
        ],
    )
    assert result["verdict"] == "inconclusive", result


def test_frame_origin_mismatch_is_rejected_before_input(advanced):
    result = run(
        advanced,
        [
            {
                "op": "click",
                "target": {
                    "id": "go",
                    "frame": [{"selector": "#same", "origin": advanced[2]}],
                },
            },
            {
                "op": "expect",
                "target": {"id": "status"},
                "assert": "text",
                "expected": "Saved",
            },
        ],
    )
    assert result["verdict"] == "inconclusive" and "origin" in result["error"], result


def test_download_quota_and_credential_file_upload_are_rejected(advanced):
    root = advanced[0]
    secret = root / ".env"
    secret.write_text("CONTROLLED_FIXTURE_ONLY=hidden")
    result = run(
        advanced,
        [
            {"op": "upload", "target": {"id": "upload"}, "files": [str(secret)]},
            {
                "op": "expect",
                "target": {"id": "upload-result"},
                "assert": "text",
                "expected": ".env",
            },
        ],
    )
    assert result["verdict"] == "inconclusive" and not result["evidence"], result
    result = run(
        advanced,
        [
            {
                "op": "download",
                "target": {"id": "download"},
                "max_bytes": 4,
                "expected_name": "fixture.txt",
            },
        ],
    )
    assert result["verdict"] == "inconclusive" and "quota" in result["error"], result
