"""Owned W3C WebDriver transport; no Selenium or full-tree model snapshots."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, urlsplit

import httpx

from iatest.engines.http import NetworkScope
from iatest.engines.proxy import BrowserProxy
from iatest.engines.web import (
    BrowserSession,
    browser_network_policy,
    validate_target,
)

ELEMENT = "element-6066-11e4-a52e-4f735466cecf"


def driver_path(browser):
    return (
        os.environ.get("IATEST_" + browser.upper() + "_DRIVER_PATH", "")
        or shutil.which("geckodriver" if browser == "firefox" else "safaridriver")
        or ""
    )


class WebDriverSession(BrowserSession):
    def __init__(self, root, project, spec, control, *, max_queries=500):
        self.root, self.project, self.spec, self.control = root, project, spec, control
        self.network_policy = browser_network_policy(spec, root=root)
        self.scope = NetworkScope(root, project, spec["allowed_origins"])
        self.scope.check_browser(spec["start_url"])
        self.queries, self.max_queries, self.pending = 0, max_queries, False
        self.process = self.proxy = None
        self.firefox_environment = {}
        self.sid = ""
        self.last_reference = ""
        self.logs = deque(maxlen=16)
        self.cdp = SimpleNamespace(calls=0, sent=0, received=0)
        self.pages, self.page_alias = {}, "main"
        self.action_charge = lambda count: None
        self.http = httpx.Client(timeout=10, trust_env=False, follow_redirects=False)
        try:
            if self.network_policy == "strict":
                self.proxy = BrowserProxy(self.scope)
            endpoint = spec.get("webdriver_url")
            if endpoint:
                parsed = urlsplit(endpoint)
                if (
                    parsed.scheme != "http"
                    or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                    or parsed.username
                    or parsed.password
                    or parsed.query
                    or parsed.fragment
                ):
                    raise ValueError(
                        "WebDriver endpoint must be an explicitly trusted loopback service"
                    )
            else:
                executable = driver_path(spec["browser"])
                if not executable:
                    raise ValueError(
                        "Install the selected browser's native WebDriver or configure its loopback endpoint"
                    )
                with socket.socket() as port:
                    port.bind(("127.0.0.1", 0))
                    number = port.getsockname()[1]
                self.driver_temp = tempfile.TemporaryDirectory(
                    prefix="iatest-webdriver-",
                    dir="/private/tmp"
                    if os.name == "posix" and Path("/private/tmp").exists()
                    else None,
                )
                command = [executable, "--port", str(number)]
                if spec["browser"] == "firefox":
                    command.extend(["--profile-root", self.driver_temp.name])
                    # macOS 27 protects the user's Firefox application data even
                    # when a distinct -profile is supplied. Keep all browser
                    # application data under this session's owned temporary root.
                    for variable, folder in (
                        ("MOZ_APP_DATA", "AppData"),
                        ("MOZ_LOCAL_APP_DATA", "LocalAppData"),
                    ):
                        directory = Path(self.driver_temp.name) / folder
                        directory.mkdir(mode=0o700)
                        self.firefox_environment[variable] = str(directory)
                self.process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=os.name == "posix",
                    env={**os.environ, **self.firefox_environment},
                )

                def capture():
                    for line in iter(lambda: self.process.stdout.readline(4096), b""):
                        self.logs.append(line.decode("utf-8", "replace")[:1000])

                self.log_thread = threading.Thread(target=capture, daemon=True)
                self.log_thread.start()
                endpoint = f"http://127.0.0.1:{number}"
            self.endpoint = endpoint.rstrip("/")
            deadline = time.monotonic() + 10
            while True:
                control.check()
                try:
                    if self.http.get(self.endpoint + "/status").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if (
                    time.monotonic() >= deadline
                    or self.process
                    and self.process.poll() is not None
                ):
                    raise ValueError("Native WebDriver did not become ready")
                control.wait(0.05)
            address = self.proxy.address.removeprefix("http://") if self.proxy else None
            caps = {
                "browserName": "firefox" if spec["browser"] == "firefox" else "safari",
            }
            if address:
                caps["proxy"] = {
                    "proxyType": "manual",
                    "httpProxy": address,
                    "sslProxy": address,
                    "noProxy": [],
                }
            if spec["browser"] == "firefox":
                caps["moz:firefoxOptions"] = {
                    "args": ["-headless"] if spec.get("headless", True) else [],
                    "prefs": {
                        "network.proxy.allow_hijacking_localhost": True,
                        "network.http.http3.enable": False,
                        "media.peerconnection.enabled": False,
                        # GeometryUtils is opt-in in release Firefox. These
                        # preferences apply only to our fresh session profile.
                        "layout.css.getBoxQuads.enabled": True,
                        "layout.css.convertFromNode.enabled": True,
                    },
                }
                if self.firefox_environment:
                    caps["moz:firefoxOptions"]["env"] = self.firefox_environment
                if os.environ.get("IATEST_FIREFOX_PATH"):
                    caps["moz:firefoxOptions"]["binary"] = os.environ[
                        "IATEST_FIREFOX_PATH"
                    ]
            created = self.command(
                "POST",
                "/session",
                {"capabilities": {"alwaysMatch": caps}},
                session=False,
            )
            self.sid = created["sessionId"]
            actual = created.get("capabilities", {})
            if self.firefox_environment:
                profile = Path(actual.get("moz:profile", "")).resolve(strict=True)
                if not profile.is_dir() or not profile.is_relative_to(
                    Path(self.driver_temp.name).resolve()
                ):
                    raise ValueError(
                        "Firefox did not confirm the session-owned profile"
                    )
            proxy = actual.get("proxy", {})
            if self.network_policy == "strict" and (
                proxy.get("proxyType") != "manual"
                or proxy.get("httpProxy") != address
                or proxy.get("sslProxy") != address
            ):
                raise ValueError(
                    "Selected WebDriver did not confirm the required network proxy; no downgrade"
                )
            self.version = (
                str(actual.get("browserName"))
                + "/"
                + str(actual.get("browserVersion", "unknown"))
            )
            self.command(
                "POST", "/timeouts", {"implicit": 0, "pageLoad": 10000, "script": 5000}
            )
            self.pages["main"] = self.command("GET", "/window")
            self.page_session = self.sid
            self.navigate(spec["start_url"])
        except BaseException as exc:
            self.close()
            if self.logs:
                exc.add_note("Native WebDriver startup: " + "".join(self.logs)[-3000:])
            raise

    def command(self, method, path, value=None, *, session=True):
        self.control.check()
        url = (
            self.endpoint
            + ("/session/" + quote(self.sid, safe="") if session else "")
            + path
        )
        self.cdp.calls += 1
        self.cdp.sent += len(json.dumps(value).encode())
        content = bytearray()
        with self.http.stream(
            method,
            url,
            json=value,
            timeout=min(
                30 if path == "/session" else 10, self.control.remaining() or 30
            ),
        ) as response:
            status = response.status_code
            for chunk in response.iter_bytes():
                self.control.check()
                content.extend(chunk)
                if len(content) > 16777216:
                    raise ValueError("WebDriver response exceeds 16 MiB")
        self.cdp.received += len(content)
        body = json.loads(content).get("value")
        if status >= 400 or isinstance(body, dict) and body.get("error"):
            body = body if isinstance(body, dict) else {}
            raise ValueError(
                "WebDriver rejected command: "
                + str(body.get("error", "transport error"))[:120]
                + ": "
                + str(body.get("message", ""))[:300]
            )
        return body

    def evaluate(self, expression, *, object_result=False, timeout=10):
        if object_result:
            raise ValueError("Use native element references on WebDriver")
        return self.command(
            "POST",
            "/execute/sync",
            {"script": "return (" + expression + ");", "args": []},
        )

    def element_script(self, reference, script, *arguments):
        return self.command(
            "POST",
            "/execute/sync",
            {"script": script, "args": [{ELEMENT: reference}, *arguments]},
        )

    def assertion(self, step):
        if step["assert"] != "value":
            return super().assertion(step)
        observed = self.query(step["target"], cache=False)
        if not observed["unique"]:
            raise ValueError(
                "Value assertion requires exactly one fresh complete target"
            )
        reference = observed["candidates"][0]["webdriver_ref"]
        value = self.element_script(
            reference,
            "const e=arguments[0];return e.type==='password'?{secure:true}:{secure:false,value:e.value};",
        )
        if value["secure"]:
            raise ValueError("Secure field value assertions are unavailable")
        matched = value["value"] == step["expected"]
        return matched, {"matched": matched}

    def select_context(self, target):
        from iatest.engines.web_contexts import FrameNotReady

        deadline = time.monotonic() + min(5, self.control.remaining() or 5)
        while True:
            try:
                return self._select_context(target)
            except FrameNotReady:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise ValueError("Selected HTTP frame did not commit its declared origin within the query budget") from None
                self.control.wait(min(.05, left))

    def _select_context(self, target):
        alias = target.get("page", self.page_alias)
        if alias not in self.pages:
            raise ValueError("Unknown browser page alias")
        self.page_alias = alias
        self.command("POST", "/window", {"handle": self.pages[alias]})
        self.command("POST", "/frame", {"id": None})
        parent_url = self.command("GET", "/url")
        self.scope.check_browser(parent_url)
        self.frame_owners = []
        for descriptor in target.get("frame", []):
            found = self.command(
                "POST",
                "/elements",
                {"using": "css selector", "value": descriptor["selector"]},
            )
            if len(found) != 1:
                raise ValueError("Frame must be unique")
            source = self.element_script(found[0][ELEMENT], "return arguments[0].getAttribute('src') || '';")
            self.command("POST", "/frame", {"id": found[0]})
            actual = self.evaluate("location.href")
            if actual in {"", "about:blank"} and isinstance(source, str) and source.startswith(("http://", "https://", "/")):
                from iatest.engines.web_contexts import FrameNotReady

                raise FrameNotReady("HTTP frame has not committed yet")
            self.scope.check_browser(actual)
            from iatest.core.sandbox import origin

            if origin(actual) != origin(descriptor["origin"]):
                raise ValueError("Frame origin mismatch")
            self.frame_owners.append((found[0][ELEMENT], origin(parent_url)))
            parent_url = actual

    def navigate(self, url):
        self.scope.check_browser(url)
        self.command("POST", "/url", {"url": url})
        self.scope.check_browser(self.command("GET", "/url"))
        return {"url": self.command("GET", "/url")}

    def query(self, target, limit=5, *, cache=True):
        validate_target(target)
        if not 1 <= limit <= 10 or self.queries >= self.max_queries:
            raise ValueError("Bounded query budget exhausted")
        self.queries += 1
        self.last_reference = ""
        self.select_context(target)
        script = Path(__file__).with_name("ui_query.js").read_text()
        value = self.evaluate(
            "(async()=>{globalThis.__iatestNodes=[];const observation=await("
            + script
            + ")("
            + json.dumps(target)
            + ","
            + json.dumps({"cap": 32 if "role" in target else limit, "cache": False})
            + ");return {...observation,references:globalThis.__iatestNodes};})()"
        )
        candidates = value.get("candidates", [])
        references = value.pop("references", None)
        if (
            not isinstance(references, list)
            or len(references) != len(candidates)
            or len(references) > (32 if "role" in target else limit)
        ):
            raise ValueError(
                "WebDriver did not return bounded native element references"
            )
        for candidate in candidates:
            index = candidate.get("index")
            if type(index) is not int or not 0 <= index < len(references):
                raise ValueError("WebDriver returned an invalid candidate index")
            element = references[index]
            if (
                not isinstance(element, dict)
                or not isinstance(element.get(ELEMENT), str)
                or not element[ELEMENT]
            ):
                raise ValueError(
                    "WebDriver did not serialize a native element reference"
                )
            candidate["webdriver_ref"] = element[ELEMENT]
            if "role" in target:
                candidate["role"] = self.command(
                    "GET",
                    "/element/" + quote(element[ELEMENT], safe="") + "/computedrole",
                )
                candidate["name"] = self.command(
                    "GET",
                    "/element/" + quote(element[ELEMENT], safe="") + "/computedlabel",
                )
        if "role" in target:
            value["search_complete"] = (
                value.get("search_complete", False)
                and value.get("match_count", 0) <= 32
            )
            candidates = [
                c
                for c in candidates
                if c["role"] == target["role"]
                and ("name" not in target or c["name"] == target["name"])
            ]
            value["match_count"] = len(candidates)
        value["candidates"] = candidates[:limit]
        value["unique"] = (
            value.get("search_complete", False) and value.get("match_count") == 1
        )
        self.last_reference = candidates[0]["webdriver_ref"] if value["unique"] else ""
        return value

    def outline(self, region="", ui_context=None):
        self.select_context(ui_context or {})
        if self.queries >= self.max_queries:
            raise ValueError("Query budget exhausted")
        self.queries += 1
        script = Path(__file__).with_name("ui_outline.js").read_text()
        return self.evaluate(
            f"({script})({json.dumps(region)},{json.dumps(ui_context or {})})"
        )

    def _click_frame_from_root(self, reference):
        # Gecko on Windows can offset transformed frame clicks incorrectly.
        # Resolve each parent afresh and use one native top-level pointer action.
        self.pending = False
        point = self.element_script(reference, """
const e=arguments[0],r=e.getBoundingClientRect(),x=Math.floor((Math.max(0,r.left)+Math.min(innerWidth,r.right))/2),y=Math.floor((Math.max(0,r.top)+Math.min(innerHeight,r.bottom))/2);
let h=document.elementFromPoint(x,y);for(let n=0;h&&h.shadowRoot&&n<8;n++){const next=h.shadowRoot.elementFromPoint(x,y);if(!next||next===h)break;h=next;}
return e.isConnected&&!e.matches(':disabled')&&h&&(h===e||e.contains(h))?{x,y,w:innerWidth,h:innerHeight}:null;
""")
        if not point:
            raise ValueError("Native frame click target is obscured")
        from iatest.core.sandbox import origin

        for owner, expected_origin in reversed(self.frame_owners):
            self.command("POST", "/frame/parent", {})
            actual = self.evaluate("location.href")
            self.scope.check_browser(actual)
            if origin(actual) != expected_origin:
                raise ValueError("Frame parent origin changed before input")
            geometry = self.element_script(owner, """
const e=arguments[0];if(!e.isConnected||typeof e.getBoxQuads!=='function')return null;
const content=e.getBoxQuads({box:'content'}),border=e.getBoxQuads({box:'border'}),r=e.getBoundingClientRect();
if(content.length!==1||border.length!==1)return null;
const points=q=>[q.p1,q.p2,q.p3,q.p4],b=points(border[0]);
const dx=Math.min(...b.map(p=>p.x))-r.left,dy=Math.min(...b.map(p=>p.y))-r.top;
return {quad:points(content[0]).flatMap(p=>[p.x-dx,p.y-dy]),w:innerWidth,h:innerHeight};
""")
            if not geometry:
                raise ValueError("Firefox frame quad is unavailable")
            from iatest.engines.projective import frame_matrix, project

            matrix = frame_matrix(point["w"], point["h"], geometry["quad"])
            x, y = project(matrix, point["x"], point["y"])
            ready = self.element_script(owner, """
const e=arguments[0],x=arguments[1],y=arguments[2];
if(!e.isConnected||x<0||y<0||x>=innerWidth||y>=innerHeight)return false;
for(let n=e;n;){const root=n.getRootNode(),hit=root.elementFromPoint(Math.round(x),Math.round(y));if(hit!==n&&!n.contains(hit))return false;n=root.host||null;}return true;
""", x, y)
            if not ready:
                raise ValueError("Frame geometry changed or is obscured")
            point = {"x": x, "y": y, "w": geometry["w"], "h": geometry["h"]}
        self.pending = True
        return self.command("POST", "/actions", {"actions": [{
            "type": "pointer", "id": "frame-click", "parameters": {"pointerType": "mouse"},
            "actions": [{"type": "pointerMove", "origin": "viewport", "x": round(point["x"]), "y": round(point["y"]), "duration": 0},
                        {"type": "pointerDown", "button": 0}, {"type": "pointerUp", "button": 0}],
        }]})

    def click_reference(self, reference, target):
        if os.name == "nt" and self.spec["browser"] == "firefox" and target.get("frame"):
            return self._click_frame_from_root(reference)
        if self.spec["browser"] != "webkit" or not target.get("shadow"):
            return self.command(
                "POST", "/element/" + quote(reference, safe="") + "/click", {}
            )
        # Safari's element-click hit test can treat the shadow host as an
        # interceptor. Use native pointer input after checking the composed hit.
        ready = self.element_script(
            reference,
            "const e=arguments[0],r=e.getBoundingClientRect();"
            "const l=Math.max(0,r.left),t=Math.max(0,r.top),"
            "right=Math.min(innerWidth,r.right),bottom=Math.min(innerHeight,r.bottom);"
            "if(right<=l||bottom<=t||!e.isConnected||e.matches(':disabled')||e.getAttribute('aria-disabled')==='true')return false;"
            "const x=Math.floor((l+right)/2),y=Math.floor((t+bottom)/2);"
            "let h=document.elementFromPoint(x,y);"
            "for(let n=0;h&&h.shadowRoot&&n<8;n++){const next=h.shadowRoot.elementFromPoint(x,y);if(!next||next===h)break;h=next;}"
            "return !!h&&(h===e||e.contains(h));",
        )
        if not ready:
            self.pending = False
            raise ValueError("Native click target is obscured or outside the viewport")
        return self.command(
            "POST",
            "/actions",
            {
                "actions": [
                    {
                        "type": "pointer",
                        "id": "shadow-click",
                        "parameters": {"pointerType": "mouse"},
                        "actions": [
                            {
                                "type": "pointerMove",
                                "origin": {ELEMENT: reference},
                                "x": 0,
                                "y": 0,
                                "duration": 0,
                            },
                            {"type": "pointerDown", "button": 0},
                            {"type": "pointerUp", "button": 0},
                        ],
                    }
                ]
            },
        )

    def act(self, step):
        value = self.query(step["target"], cache=False)
        if not value["unique"]:
            raise ValueError("Action requires one fresh complete target")
        candidate = value["candidates"][0]
        path = "/element/" + quote(candidate["webdriver_ref"], safe="")
        self.pending = True
        try:
            if step["op"] == "click":
                self.click_reference(candidate["webdriver_ref"], step["target"])
            elif step["op"] == "fill":
                self.command("POST", path + "/clear", {})
                if self.spec["browser"] == "webkit":
                    # Safari may acknowledge send-keys without focusing an
                    # input in a cross-origin frame. Focus through real input.
                    self.click_reference(candidate["webdriver_ref"], step["target"])
                self.command("POST", path + "/value", {"text": step["value"]})
            elif step["op"] == "press":
                self.command(
                    "POST",
                    path + "/value",
                    {
                        "text": {
                            "Enter": "\ue007",
                            "Tab": "\ue004",
                            "Escape": "\ue00c",
                            "Backspace": "\ue003",
                        }[step["key"]]
                    },
                )
            else:
                raise ValueError("Unsupported native WebDriver action")
        except (httpx.HTTPError, TimeoutError) as exc:
            raise RuntimeError("Input delivery unknown; do not replay") from exc
        self.pending = False
        if self.network_policy == "navigation_only":
            self.scope.check_browser(self.command("GET", "/url"))
            self.scope.check_browser(self.evaluate("location.href"))
        return {"dispatch": "acknowledged", "business_postcondition": "pending"}

    def page_action(self, step):
        op, alias = step["op"], step["page"]
        if op == "open_page":
            if alias in self.pages:
                raise ValueError("Duplicate page alias")
            self.scope.check_browser(step["url"])
            self.pages[alias] = self.command("POST", "/window/new", {"type": "tab"})[
                "handle"
            ]
            self.page_alias = alias
            # A new owned tab is about:blank until navigation. No observation
            # or input is allowed in that transient document.
            self.command("POST", "/window", {"handle": self.pages[alias]})
            self.command("POST", "/frame", {"id": None})
            self.navigate(step["url"])
        elif op == "switch_page":
            self.select_context({"page": alias})
        elif op == "close_page":
            self.select_context({"page": alias})
            self.command("DELETE", "/window")
            del self.pages[alias]
            if self.pages:
                self.select_context({"page": next(iter(self.pages))})
        else:
            deadline = time.monotonic() + step.get("timeout_seconds", 5)
            while True:
                handles = set(self.command("GET", "/window/handles")) - set(
                    self.pages.values()
                )
                if len(handles) > 1:
                    raise ValueError("Ambiguous popup")
                if handles:
                    handle = handles.pop()
                    self.command("POST", "/window", {"handle": handle})
                    url = self.command("GET", "/url")
                    self.scope.check_browser(url)
                    if url == step["url"]:
                        self.pages[alias] = handle
                        self.page_alias = alias
                        break
                if time.monotonic() > deadline:
                    raise TimeoutError("Popup not observed")
                self.control.wait(0.05)
        return {"page": alias}

    def scroll(self, step):
        rounds = step.get("max_scrolls", 1) if step["op"] == "scroll_find" else 1
        for index in range(rounds + 1):
            if step["op"] == "scroll_find":
                found = self.query(step["target"])
                if found["unique"]:
                    return {"found": True, "scrolls": index}
            if index == rounds:
                raise ValueError("Scroll budget exhausted; absence remains unknown")
            observed = self.query(step.get("container", step["target"]))
            if not observed["unique"]:
                raise ValueError("Scroll container must be unique")
            element = {ELEMENT: observed["candidates"][0]["webdriver_ref"]}
            self.pending = True
            self.command(
                "POST",
                "/actions",
                {
                    "actions": [
                        {
                            "type": "wheel",
                            "id": "scroll",
                            "actions": [
                                {
                                    "type": "scroll",
                                    "origin": element,
                                    "x": 0,
                                    "y": 0,
                                    "deltaX": 0,
                                    "deltaY": int(step.get("delta_y", 400)),
                                    "duration": 100,
                                }
                            ],
                        }
                    ]
                },
            )
            self.pending = False
            if step["op"] == "scroll":
                return {"dispatch": "acknowledged"}

    def upload(self, step):
        from iatest.core.sandbox import policy_scope, read_file

        observed = self.query(step["target"], cache=False)
        if not observed["unique"]:
            raise ValueError("Upload target must be unique")
        paths = []
        total = 0
        for item in step["files"]:
            path = self.scope.policy.check_path(item)
            with policy_scope(self.scope.policy):
                total += len(read_file(path, step.get("max_bytes", 8388608)))
            if total > step.get("max_bytes", 8388608):
                raise ValueError("Upload byte quota exceeded")
            paths.append(str(path))
        self.pending = True
        self.command(
            "POST",
            "/element/"
            + quote(observed["candidates"][0]["webdriver_ref"], safe="")
            + "/value",
            {"text": "\n".join(paths)},
        )
        self.pending = False
        return {"dispatch": "acknowledged", "bytes": total}

    def _session_call(self, session, method, params=None, timeout=10):
        if method != "Page.captureScreenshot":
            raise ValueError("Unsupported cross-backend low-level operation")
        if self.network_policy == "navigation_only":
            self.scope.check_browser(self.command("GET", "/url"))
            self.scope.check_browser(self.evaluate("location.href"))
        if params and "clip" in params:
            if not self.last_reference:
                raise ValueError(
                    "Element screenshot requires one fresh native reference"
                )
            data = self.command(
                "GET", "/element/" + quote(self.last_reference, safe="") + "/screenshot"
            )
        else:
            data = self.command("GET", "/screenshot")
        if len(data) > 12 * 1024 * 1024:
            raise ValueError("Screenshot quota exceeded")
        return {"data": data}

    def root_point(self, point, *, hit_test=False):
        return {**point, "scale_x": 1, "scale_y": 1}

    def root_geometry(self, rect):
        return rect, None

    def root_viewport(self):
        return self.evaluate(
            "({x:scrollX,y:scrollY,width:innerWidth,height:innerHeight,dpr:devicePixelRatio})"
        )

    def visual_region(self, target):
        observed = self.query(target, cache=False)
        if not observed["unique"]:
            raise ValueError("Visual region must be unique")
        candidate = observed["candidates"][0]
        data = base64.b64decode(
            self.command(
                "GET",
                "/element/"
                + quote(candidate["webdriver_ref"], safe="")
                + "/screenshot",
            ),
            validate=True,
        )
        if (
            len(data) < 24
            or not data.startswith(b"\x89PNG\r\n\x1a\n")
            or len(data) > 1048576
        ):
            raise ValueError("Visual PNG exceeds bounds")
        w, h = struct.unpack(">II", data[16:24])
        if not 0 < w <= 1024 or not 0 < h <= 1024 or w * h > 524288:
            raise ValueError("Visual pixel quota exceeded")
        vp = self.root_viewport()
        return {
            "data": data,
            "sha256": hashlib.sha256(data).hexdigest(),
            "width": w,
            "height": h,
            "viewport": {k: vp[k] for k in ("width", "height", "dpr")},
            "rect": candidate["rect"],
            "webdriver_ref": candidate["webdriver_ref"],
            "captured": time.monotonic(),
            "session": self.sid,
        }

    def visual_click(self, step):
        value = self.visual_region(step["target"])
        anchor = step["visual_anchor"]
        if any(
            value[k] != anchor[k] for k in ("sha256", "width", "height", "viewport")
        ):
            raise ValueError("Visual pixels/viewport changed")
        if (
            not 0 <= anchor["x"] < value["width"]
            or not 0 <= anchor["y"] < value["height"]
        ):
            raise ValueError("Visual point outside region")
        fresh = self.query(step["target"], cache=False)
        if (
            not fresh["unique"]
            or fresh["candidates"][0]["webdriver_ref"] != value["webdriver_ref"]
            or fresh["candidates"][0]["rect"] != value["rect"]
            or time.monotonic() - value["captured"] > 2
        ):
            raise ValueError("Visual target changed")
        rect = value["rect"]
        x = rect["x"] + anchor["x"] / value["width"] * rect["width"]
        y = rect["y"] + anchor["y"] / value["height"] * rect["height"]
        ready = self.element_script(
            value["webdriver_ref"],
            "const e=arguments[0],x=arguments[1],y=arguments[2];let h=document.elementFromPoint(x,y);"
            "for(let n=0;h&&h.shadowRoot&&n<8;n++){const next=h.shadowRoot.elementFromPoint(x,y);if(!next||next===h)break;h=next;}"
            "return !!(e&&e.isConnected&&!e.disabled&&h&&(h===e||e.contains(h)));",
            x,
            y,
        )
        if not ready:
            raise ValueError("Visual target is obscured")
        self.pending = True
        try:
            self.command(
                "POST",
                "/actions",
                {
                    "actions": [
                        {
                            "type": "pointer",
                            "id": "visual",
                            "parameters": {"pointerType": "mouse"},
                            "actions": [
                                {
                                    "type": "pointerMove",
                                    "origin": {ELEMENT: value["webdriver_ref"]},
                                    "x": round(x - rect["x"] - rect["width"] / 2),
                                    "y": round(y - rect["y"] - rect["height"] / 2),
                                    "duration": 0,
                                },
                                {"type": "pointerDown", "button": 0},
                                {"type": "pointerUp", "button": 0},
                            ],
                        }
                    ]
                },
            )
        except httpx.HTTPError as exc:
            raise RuntimeError("Visual delivery unknown; never replay") from exc
        self.pending = False
        return {"dispatch": "acknowledged", "business_postcondition": "pending"}

    def download(self, step):
        import re

        observed = self.query(step["target"], cache=False)
        if not observed["unique"]:
            raise ValueError("Download target must be unique")
        reference = observed["candidates"][0]["webdriver_ref"]
        href = self.element_script(reference, "return arguments[0].href||'';")
        if href.startswith(("data:", "blob:")):
            return self.local_download(step, reference, href)
        if not step.get("expected_sha256"):
            raise ValueError(
                "WebDriver HTTP download requires an explicit content digest"
            )
        self.scope.check(href)
        self.control.request()
        content = bytearray()
        with httpx.Client(trust_env=False, follow_redirects=False) as client:
            for cookie in self.command("GET", "/cookie"):
                client.cookies.set(
                    cookie["name"],
                    cookie["value"],
                    domain=cookie.get("domain"),
                    path=cookie.get("path", "/"),
                )
            with client.stream(
                "GET", href, timeout=min(10, self.control.remaining() or 10)
            ) as response:
                if response.status_code != 200:
                    raise ValueError(
                        "Download response was not successful; redirects require explicit navigation"
                    )
                for chunk in response.iter_bytes():
                    self.control.check()
                    content.extend(chunk)
                    if len(content) > step.get("max_bytes", 8388608):
                        raise ValueError("Download quota exceeded")
                match = re.search(
                    r'filename="([^"\r\n]+)"',
                    response.headers.get("content-disposition", ""),
                )
                name = (
                    Path(match.group(1) if match else urlsplit(href).path).name[:200]
                    or "download.bin"
                )
        digest = hashlib.sha256(content).hexdigest()
        return bytes(content), {
            "filename": name,
            "bytes": len(content),
            "sha256": digest,
            "passed": digest == step["expected_sha256"]
            and (not step.get("expected_name") or name == step["expected_name"]),
        }

    def local_download(self, step, reference, href):
        if not step.get("expected_sha256"):
            raise ValueError(
                "Local blob/data download requires an explicit content digest"
            )
        if href.startswith("blob:"):
            self.scope.check_browser(href[5:])
        limit = step.get("max_bytes", 8388608)
        value = self.element_script(
            reference,
            "const e=arguments[0],href=arguments[1],limit=arguments[2];"
            "return (async()=>{if(!e.isConnected||e.href!==href)throw Error('Stale download');"
            "const response=await fetch(href),reader=response.body.getReader();let size=0,parts=[];"
            "try{while(true){const next=await reader.read();if(next.done)break;size+=next.value.length;"
            "if(size>limit)throw Error('Download quota');parts.push(next.value);}}finally{await reader.cancel();}"
            "let text='';for(const part of parts)for(const byte of part)text+=String.fromCharCode(byte);"
            "return {data:btoa(text),name:e.download||'download.bin'};})();",
            href,
            limit,
        )
        data = base64.b64decode(value["data"], validate=True)
        if len(data) > limit:
            raise ValueError("Download byte quota exceeded")
        name = Path(value["name"]).name[:200]
        digest = hashlib.sha256(data).hexdigest()
        return data, {
            "filename": name,
            "bytes": len(data),
            "sha256": digest,
            "passed": digest == step["expected_sha256"]
            and (not step.get("expected_name") or step["expected_name"] == name),
        }

    def close(self):
        if getattr(self, "sid", ""):
            try:
                self.http.delete(
                    self.endpoint + "/session/" + quote(self.sid, safe=""), timeout=2
                )
            except httpx.HTTPError:
                pass
            self.sid = ""
        if getattr(self, "process", None) and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        if getattr(self, "proxy", None):
            self.proxy.close()
        if os.name == "posix" and getattr(self, "process", None):
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if getattr(self, "http", None):
            self.http.close()
        if getattr(self, "log_thread", None):
            self.log_thread.join(0.5)
        if getattr(self, "driver_temp", None):
            self.driver_temp.cleanup()
