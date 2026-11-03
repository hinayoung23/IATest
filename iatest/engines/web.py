from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from iatest.core.files import atomic_write_bytes
from iatest.engines.cdp import CDP
from iatest.engines.contracts import EngineContext, EngineResult, ExecutionPlan
from iatest.engines.http import NetworkScope, validate
from iatest.engines.proxy import BrowserProxy
from iatest.engines.store import EngineStore
from iatest.engines.web_contexts import BrowserContexts
from iatest.engines.web_visual import ANCHOR_SCHEMA, BrowserVisual
from iatest.execution.control import RunCancelled


class LocatorUnavailable(ValueError):
    pass


TARGET_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "minProperties": 1,
    "properties": {
        "id": {"type": "string", "minLength": 1, "maxLength": 300},
        "test_id": {"type": "string", "minLength": 1, "maxLength": 300},
        "selector": {"type": "string", "minLength": 1, "maxLength": 500},
        "text": {"type": "string", "minLength": 1, "maxLength": 1000},
        "label": {"type": "string", "minLength": 1, "maxLength": 300},
        "role": {"type": "string", "minLength": 1, "maxLength": 100},
        "name": {"type": "string", "minLength": 1, "maxLength": 300},
        "scope": {"type": "string", "minLength": 1, "maxLength": 500},
        "anchor": {
            "type": "object",
            "additionalProperties": False,
            "required": ["selector", "text"],
            "properties": {
                "selector": {"type": "string", "minLength": 1, "maxLength": 500},
                "text": {"type": "string", "maxLength": 1000},
                "container": {"type": "string", "maxLength": 300},
            },
        },
        "visible": {"type": "boolean"},
        "page": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,60}$"},
        "frame": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["selector", "origin"],
                "properties": {
                    "selector": {"type": "string", "minLength": 1, "maxLength": 500},
                    "origin": {"type": "string", "minLength": 1, "maxLength": 2048},
                    "opaque_source_sha256": {
                        "type": "string",
                        "pattern": "^[a-f0-9]{64}$",
                    },
                },
            },
        },
        "shadow": {
            "type": "array",
            "maxItems": 8,
            "items": {"type": "string", "minLength": 1, "maxLength": 500},
        },
    },
    "anyOf": [
        {"required": [k]}
        for k in ("id", "test_id", "selector", "text", "label", "role")
    ],
}
WEB_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["allowed_origins", "start_url", "steps"],
    "properties": {
        "allowed_origins": {
            "type": "array",
            "minItems": 1,
            "maxItems": 32,
            "items": {"type": "string"},
        },
        "start_url": {"type": "string", "minLength": 1, "maxLength": 8192},
        "headless": {"type": "boolean"},
        "browser": {"enum": ["chromium", "firefox", "webkit"]},
        "network_policy": {"enum": ["strict", "navigation_only"]},
        "webdriver_url": {"type": "string", "maxLength": 2048},
        "query_cache": {"type": "boolean"},
        "steps": {
            "type": "array",
            "minItems": 1,
            "maxItems": 500,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["op"],
                "properties": {
                    "op": {
                        "enum": [
                            "navigate",
                            "reload",
                            "restart_session",
                            "find",
                            "click",
                            "fill",
                            "press",
                            "expect",
                            "wait",
                            "screenshot",
                            "open_page",
                            "switch_page",
                            "close_page",
                            "expect_popup",
                            "upload",
                            "download",
                            "scroll",
                            "scroll_find",
                            "visual_click",
                        ]
                    },
                    "target": TARGET_SCHEMA,
                    "visual_anchor": ANCHOR_SCHEMA,
                    "container": TARGET_SCHEMA,
                    "page": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,60}$"},
                    "files": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 8,
                        "uniqueItems": True,
                        "items": {"type": "string", "minLength": 1, "maxLength": 2048},
                    },
                    "max_bytes": {"type": "integer", "minimum": 1, "maximum": 16777216},
                    "max_scrolls": {"type": "integer", "minimum": 1, "maximum": 20},
                    "delta_y": {"type": "number", "minimum": -2000, "maximum": 2000},
                    "expected_name": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 200,
                    },
                    "expected_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                    "url": {"type": "string", "maxLength": 8192},
                    "value": {"type": "string", "maxLength": 16384},
                    "key": {"enum": ["Enter", "Tab", "Escape", "Backspace"]},
                    "assert": {
                        "enum": ["exists", "absent", "text", "value", "url", "enabled"]
                    },
                    "expected": {"type": ["string", "boolean"], "maxLength": 16384},
                    "timeout_seconds": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "maximum": 30,
                    },
                },
            },
        },
    },
}


def validate_target(target):
    validate(target, TARGET_SCHEMA)
    if "name" in target and "role" not in target:
        raise ValueError("Computed accessible name requires a role")
    if "label" in target and any(
        k in target for k in ("selector", "id", "test_id", "role")
    ):
        raise ValueError(
            "Use label as a standalone locator, optionally with scope/anchor/text"
        )
    for key in ("selector", "scope"):
        if target.get(key, "").strip() in {"*", "html", "body"}:
            raise ValueError(
                "Use a specific target/region instead of a full-page selector"
            )


def browser_network_policy(spec, *, root=None):
    policy = spec.get("network_policy", "strict")
    if policy not in {"strict", "navigation_only"}:
        raise ValueError("Unknown browser network policy")
    if policy == "navigation_only" and spec.get("browser", "chromium") != "webkit":
        raise ValueError("navigation_only requires the Safari (webkit) backend")
    if policy == "navigation_only" and root is not None:
        from iatest.core.config import load_config
        from iatest.core.sandbox import ACTIVE_POLICY

        settings = load_config(root).get("sandbox", {})
        parent = ACTIVE_POLICY.get()
        if settings.get("allowed_origins") or settings.get("allowed_networks") or (
            parent and parent.mode != "off" and (parent.allowed_origins or parent.allowed_networks)
        ):
            raise ValueError("Safari navigation_only cannot enforce configured project or parent network restrictions; use strict mode")
    return policy


def validate_web(plan: ExecutionPlan):
    validate(plan.spec, WEB_SCHEMA)
    browser_network_policy(plan.spec)
    last_change = -1
    checks = []
    for index, step in enumerate(plan.spec["steps"]):
        op = step["op"]
        if "target" in step:
            validate_target(step["target"])
        if (
            op
            in {
                "find",
                "click",
                "fill",
                "press",
                "upload",
                "download",
                "scroll",
                "scroll_find",
                "visual_click",
            }
            and "target" not in step
        ):
            raise ValueError(f"{op} requires target")
        if op == "fill" and "value" not in step:
            raise ValueError("fill requires value")
        if op == "visual_click" and "visual_anchor" not in step:
            raise ValueError("Visual click requires a reference image anchor")
        if op == "press" and "key" not in step:
            raise ValueError("press requires key")
        if op == "navigate" and "url" not in step:
            raise ValueError("navigate requires url")
        if op in {
            "open_page",
            "switch_page",
            "close_page",
            "expect_popup",
        } and not step.get("page"):
            raise ValueError("Page actions require an explicit alias")
        if op in {"open_page", "expect_popup"} and not step.get("url"):
            raise ValueError("New pages require an authorized expected URL")
        if op == "upload" and not step.get("files"):
            raise ValueError("Upload requires explicit authorized files")
        if op == "scroll_find":
            if "container" not in step:
                raise ValueError("scroll_find requires a container")
            validate_target(step["container"])
            if any(
                step["container"].get(k) != step["target"].get(k)
                for k in ("page", "frame", "shadow")
            ):
                raise ValueError(
                    "Scroll container and target must use the same context"
                )
        if op == "download" and ("expected_name" in step or "expected_sha256" in step):
            checks.append(index)
        if op in {"expect", "wait"}:
            if "assert" not in step or (
                step["assert"] != "url" and "target" not in step
            ):
                raise ValueError("Assertions require a predicate and target (or URL)")
            if (
                step["assert"] in {"text", "value", "url", "enabled"}
                and "expected" not in step
            ):
                raise ValueError("Value assertion requires expected")
            if step["assert"] == "enabled" and type(step.get("expected")) is not bool:
                raise ValueError("enabled expected must be boolean")
            checks.append(index)
        if op in {
            "navigate",
            "reload",
            "restart_session",
            "click",
            "visual_click",
            "fill",
            "press",
            "open_page",
            "switch_page",
            "close_page",
            "expect_popup",
            "upload",
            "scroll",
            "scroll_find",
            "download",
        }:
            last_change = index
    if (
        not checks
        or max(checks) < last_change
        or (
            max(checks) == last_change
            and plan.spec["steps"][last_change]["op"] != "download"
        )
    ):
        raise ValueError(
            "Web plan needs an explicit postcondition after its final action"
        )


def chromium_path() -> str:
    configured = os.environ.get("IATEST_CHROMIUM_PATH", "")
    candidates = (
        [configured]
        if configured
        else [
            # Installed Chrome also has the distribution's sandbox integration;
            # an unpackaged Chromium snapshot may lack it (e.g. Ubuntu AppArmor).
            shutil.which("google-chrome"),
            shutil.which("google-chrome-stable"),
            shutil.which("chromium"),
            shutil.which("chromium-browser"),
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    )
    return next(
        (
            str(Path(p).expanduser())
            for p in candidates
            if p and Path(p).expanduser().is_file()
        ),
        "",
    )


class BrowserSession(BrowserContexts, BrowserVisual):
    def __new__(cls, root, project, spec, control, **kwargs):
        if cls is BrowserSession:
            browser_network_policy(spec, root=root)
            from iatest.core.config import load_config

            if load_config(root).get("sandbox", {}).get("mode") in {
                "process",
                "container",
            }:
                from iatest.execution.browser_rpc import RPCBrowser

                return RPCBrowser(root, project, spec, control, **kwargs)
        if cls is BrowserSession and spec.get("browser", "chromium") != "chromium":
            from iatest.engines.webdriver import WebDriverSession

            return object.__new__(WebDriverSession)
        return object.__new__(cls)

    def __init__(self, root, project, spec, control, *, max_queries=500):
        self.control, self.spec = control, spec
        self.network_policy = browser_network_policy(spec)
        self.scope = NetworkScope(root, project, spec["allowed_origins"])
        if self.scope.mode in {"process", "container"}:
            raise ValueError(
                "Web backend currently supports policy isolation; requested OS/container isolation is unavailable"
            )
        self.scope.check_browser(spec["start_url"])
        executable = chromium_path()
        if not executable:
            raise ValueError(
                "Chromium unavailable; set IATEST_CHROMIUM_PATH to an installed browser"
            )
        self.temp = tempfile.TemporaryDirectory(prefix="iatest-chromium-")
        self.proxy = None
        self.process = None
        self.startup_logs = deque(maxlen=16)
        self.log_thread = None
        self.close_job = None
        self.cdp = None
        self.session = ""
        self.queries = 0
        self.max_queries = max_queries
        self.world = None
        self.pending = False
        self.context_worlds, self.frame_sessions, self.frame_chain = {}, {}, []
        self.page_alias, self.pages = "main", {}
        self.action_charge = lambda count: None
        from iatest.execution import native_ipc

        self.owns_process_group = native_ipc.authorize_request is None
        try:
            self.proxy = BrowserProxy(self.scope)
            args = [
                executable,
                "--remote-debugging-port=0",
                "--remote-debugging-address=127.0.0.1",
                "--user-data-dir=" + self.temp.name,
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-sync",
                "--disable-extensions",
                "--disable-quic",
                "--disable-features=MediaRouter,DnsOverHttps",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1",
                "--proxy-server=" + self.proxy.address,
                "--proxy-bypass-list=<-loopback>",
                "--window-size=1280,800",
                "about:blank",
            ]
            if spec.get("headless", True):
                args.insert(1, "--headless=new")
            self.process = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=os.name != "nt" and self.owns_process_group,
            )

            def capture():
                for line in iter(lambda: self.process.stderr.readline(4096), b""):
                    self.startup_logs.append(line.decode("utf-8", "replace")[:1000])

            self.log_thread = threading.Thread(target=capture, daemon=True)
            self.log_thread.start()
            if os.name == "nt":
                from iatest.execution.windows_job import attach_job

                self.close_job = attach_job(self.process, {}, kill_only=True)
            port_file = Path(self.temp.name) / "DevToolsActivePort"
            deadline = time.monotonic() + 15
            while not port_file.exists():
                control.check()
                if self.process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Chromium startup failed or timed out")
                control.wait(0.05)
            port, path = port_file.read_text().splitlines()[:2]
            self.cdp = CDP(f"ws://127.0.0.1:{int(port)}{path}", control)
            self.version = self.cdp.call("Browser.getVersion")["product"]
            context = self.cdp.call("Target.createBrowserContext")["browserContextId"]
            target = self.cdp.call(
                "Target.createTarget",
                {"url": "about:blank", "browserContextId": context},
            )["targetId"]
            self.session = self.cdp.call(
                "Target.attachToTarget", {"targetId": target, "flatten": True}
            )["sessionId"]
            self.browser_context, self.page_session = context, self.session
            self.pages["main"] = {"target": target, "session": self.session}
            self.call("Page.enable")
            from iatest.engines.client_performance import ACTIVE_CLIENT_CAPTURE

            self.performance_collector = ACTIVE_CLIENT_CAPTURE.get()
            if self.performance_collector:
                self.performance_collector.install(self)
            self.call("Network.enable")
            self.call("Network.setBypassServiceWorker", {"bypass": True})
            self.call(
                "Browser.setDownloadBehavior",
                {"behavior": "deny", "browserContextId": context},
                browser=True,
            )
            self.navigate(spec["start_url"])
        except Exception as exc:
            self.close()
            if str(exc) == "Chromium startup failed or timed out" and self.startup_logs:
                raise RuntimeError(
                    str(exc) + ": " + "".join(self.startup_logs)[-3000:].strip()
                ) from exc
            raise

    def call(self, method, params=None, *, browser=False, timeout=10):
        return self.cdp.call(method, params, "" if browser else self.session, timeout)

    def evaluate(self, expression, *, object_result=False, timeout=10):
        if self.world is None:
            tree = self.call("Page.getFrameTree")["frameTree"]
            # Child frames require explicit origin-checked context selection.
            self.world = self.call(
                "Page.createIsolatedWorld",
                {
                    "frameId": tree["frame"]["id"],
                    "worldName": "iatest",
                    "grantUniveralAccess": False,
                },
            )["executionContextId"]
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "contextId": self.world,
                "returnByValue": not object_result,
                "awaitPromise": True,
                "objectGroup": "iatest",
                "timeout": int(timeout * 1000),
            },
            timeout=timeout,
        )
        if "exceptionDetails" in result:
            raise ValueError(
                "Browser target query failed: "
                + result["exceptionDetails"].get("text", "JavaScript error")
            )
        return (
            result.get("result", {})
            if object_result
            else result.get("result", {}).get("value")
        )

    def navigate(self, url):
        self.select_context({})
        self.scope.check_browser(url)
        self.world = None
        result = self.call("Page.navigate", {"url": url})
        if result.get("errorText"):
            raise ValueError("Navigation failed: " + result["errorText"])
        deadline = time.monotonic() + min(10, self.control.remaining() or 10)
        while time.monotonic() < deadline:
            try:
                self.world = None
                frame = self.call("Page.getFrameTree")["frameTree"]["frame"]
                if (
                    result.get("loaderId")
                    and frame.get("loaderId") != result["loaderId"]
                ):
                    self.control.wait(0.05)
                    continue
                state = self.evaluate(
                    "({ready:document.readyState,url:location.href,body:!!document.body})"
                )
                if (
                    state
                    and state["ready"] in {"interactive", "complete"}
                    and state["url"] != "about:blank"
                    and state["body"]
                ):
                    self.scope.check_browser(state["url"])
                    return
            except RuntimeError:
                self.world = None
            self.control.wait(0.05)
        raise TimeoutError("Navigation readiness deadline exceeded")

    def query(self, target, limit=5, *, cache=True):
        validate_target(target)
        self.select_context(target)
        self.prepare_shadow(target)
        if not 1 <= limit <= 10:
            raise ValueError("Candidate limit must be between 1 and 10")
        if self.queries >= self.max_queries:
            raise ValueError("Browser query budget exhausted")
        self.queries += 1
        if not getattr(self, "opaque_context", False):
            self.scope.check_browser(self.evaluate("location.href"))
        self.call("Runtime.releaseObjectGroup", {"objectGroup": "iatest"})
        # Named world globals retain at most 32 nodes, replaced on every observation.
        script = Path(__file__).with_name("ui_query.js").read_text()
        need_ax = "role" in target
        cap = 32 if need_ax else limit
        value = self.evaluate(
            f"({script})({json.dumps(target, ensure_ascii=False)},{json.dumps({'cap': cap, 'cache': cache and self.spec.get('query_cache', True)})})"
        )
        if not isinstance(value, dict):
            raise TypeError("Invalid query result")
        candidates = value.get("candidates", [])
        if need_ax:
            refined = []
            for candidate in candidates:
                obj = self.evaluate(
                    f"globalThis.__iatestNodes[{candidate['index']}]",
                    object_result=True,
                )
                node = self.call("DOM.describeNode", {"objectId": obj["objectId"]})[
                    "node"
                ]
                nodes = self.call(
                    "Accessibility.getPartialAXTree",
                    {"backendNodeId": node["backendNodeId"], "fetchRelatives": False},
                ).get("nodes", [])
                ax = next(
                    (
                        n
                        for n in nodes
                        if n.get("backendDOMNodeId") == node["backendNodeId"]
                    ),
                    {},
                )
                candidate["role"] = ax.get("role", {}).get("value", "")
                candidate["name"] = ax.get("name", {}).get("value", "")
                if candidate["role"] == target["role"] and (
                    "name" not in target or candidate["name"] == target["name"]
                ):
                    refined.append(candidate)
                candidate["name_truncated"] = len(candidate["name"]) > 1000
                candidate["name"] = candidate["name"][:1000]
            value["search_complete"] = (
                value["search_complete"] and value.get("match_count", 0) <= cap
            )
            value["match_count"] = len(refined)
            candidates = refined
        value["candidates"] = candidates[:limit]
        value["has_more"] = value.get("has_more", False) or len(candidates) > limit
        value["unique"] = bool(
            value.get("search_complete") and value.get("match_count") == 1
        )
        return value

    def outline(self, region="", ui_context=None):
        """Bounded semantic landmarks and controls; never serialize the DOM."""
        ui_context = ui_context or {}
        if set(ui_context) - {"page", "frame", "shadow"}:
            raise ValueError("Outline context accepts page/frame/shadow only")
        validate_target({**ui_context, "id": "context-validation"})
        self.select_context(ui_context)
        if self.queries >= self.max_queries:
            raise ValueError("Browser query budget exhausted")
        self.queries += 1
        self.scope.check_browser(self.evaluate("location.href"))
        script = Path(__file__).with_name("ui_outline.js").read_text()
        return self.evaluate(
            f"({script})({json.dumps(region)},{json.dumps(ui_context)})"
        )

    def act(self, step):
        observation = self.query(step["target"], cache=False)
        if not observation["unique"]:
            raise LocatorUnavailable(
                "Action blocked: target is ambiguous, missing or search incomplete"
            )
        candidate = observation["candidates"][0]
        index = candidate["index"]

        def backend_node(position):
            handle = self.evaluate(
                f"globalThis.__iatestNodes[{position}]", object_result=True
            )
            return self.call("DOM.describeNode", {"objectId": handle["objectId"]})[
                "node"
            ]["backendNodeId"]

        identity = backend_node(index)
        script = f"""(async()=>{{const e=globalThis.__iatestNodes[{index}];
          if(!e||!e.isConnected)throw Error('Stale target');
          e.scrollIntoView({{block:'center',inline:'center'}});
          const rect=()=>{{let r=e.getBoundingClientRect();return [r.x,r.y,r.width,r.height]}};
          const a=rect();await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
          const b=rect();if(a.some((v,i)=>Math.abs(v-b[i])>0.5))throw Error('Unstable target');
          if(e.disabled||e.getAttribute('aria-disabled')==='true')throw Error('Disabled target');
          const x=b[0]+b[2]/2,y=b[1]+b[3]/2,hit=globalThis.__iatestHit(x,y);
          if(!hit||!(hit===e||e.contains(hit)))throw Error('Target is obscured');
          return {{x,y,editable:(e.tagName==='INPUT'||e.tagName==='TEXTAREA')&&!e.readOnly&&e.type!=='file'}};}})()"""
        point = self.evaluate(script)
        if step["op"] == "fill" and not point["editable"]:
            raise LocatorUnavailable("Target is not an editable text input")
        self.position_pointer(self.root_point(point, hit_test=True))
        current = self.query(step["target"], cache=False)
        if (
            not current["unique"]
            or backend_node(current["candidates"][0]["index"]) != identity
        ):
            raise LocatorUnavailable(
                "Target identity or business scope changed before dispatch"
            )
        rect = current["candidates"][0]["rect"]
        if (
            abs(point["x"] - (rect["x"] + rect["width"] / 2)) > 0.5
            or abs(point["y"] - (rect["y"] + rect["height"] / 2)) > 0.5
        ):
            raise LocatorUnavailable("Target moved before dispatch")
        position = current["candidates"][0]["index"]
        ready = self.evaluate(
            f"(()=>{{const e=globalThis.__iatestNodes[{position}],h=globalThis.__iatestHit({point['x']},{point['y']});return !!(e&&e.isConnected&&!e.disabled&&e.getAttribute('aria-disabled')!=='true'&&h&&(h===e||e.contains(h)));}})()"
        )
        if not ready:
            raise LocatorUnavailable(
                "Target became disabled, detached or obscured before dispatch"
            )
        input_point = self.root_point(point, hit_test=True)
        self.pending = True
        try:
            for kind in ("mousePressed", "mouseReleased"):
                self.input_call(
                    "Input.dispatchMouseEvent",
                    {
                        "type": kind,
                        "x": input_point["x"],
                        "y": input_point["y"],
                        "button": "left",
                        "clickCount": 1,
                    },
                )
            if step["op"] == "fill":
                self.input_call(
                    "Input.dispatchKeyEvent",
                    {
                        "type": "keyDown",
                        "key": "a",
                        "code": "KeyA",
                        "commands": ["selectAll"],
                    },
                )
                self.input_call(
                    "Input.dispatchKeyEvent",
                    {"type": "keyUp", "key": "a", "code": "KeyA"},
                )
                self.input_call("Input.insertText", {"text": step["value"]})
            elif step["op"] == "press":
                key = step["key"]
                code = {"Enter": 13, "Tab": 9, "Escape": 27, "Backspace": 8}[key]
                for kind in ("keyDown", "keyUp"):
                    self.input_call(
                        "Input.dispatchKeyEvent",
                        {
                            "type": kind,
                            "key": key,
                            "code": key,
                            "windowsVirtualKeyCode": code,
                            "text": "\r"
                            if key == "Enter" and kind == "keyDown"
                            else "",
                        },
                    )
            self.pending = False
        except Exception as exc:
            raise RuntimeError("Action delivery unknown; do not replay") from exc
        # Navigation may have replaced the isolated world.
        self.world = None
        return {
            "dispatch": "acknowledged",
            "target": {k: candidate[k] for k in ("identifier", "tag")},
            "business_postcondition": "pending",
        }

    def assertion(self, step):
        if step["assert"] == "url":
            self.select_context({})
            actual = self.evaluate("location.href")
            self.scope.check_browser(actual)
            return actual == step["expected"], {"actual": actual}
        observation = self.query(step["target"])
        if not observation.get("search_complete"):
            raise ValueError("Incomplete observation cannot prove an assertion")
        predicate = step["assert"]
        count = observation["match_count"]
        if predicate == "absent":
            return count == 0, observation
        if predicate == "exists":
            return count == 1, observation
        if count != 1:
            raise ValueError("Value assertion requires exactly one target")
        candidate = observation["candidates"][0]
        if predicate == "enabled":
            return candidate["enabled"] == step["expected"], observation
        if predicate == "value":
            value = self.evaluate(
                f"(()=>{{const e=globalThis.__iatestNodes[{candidate['index']}];return e.type==='password'?{{secure:true}}:{{secure:false,value:e.value}};}})()"
            )
            if value["secure"]:
                raise ValueError("Secure field value assertions are unavailable")
            return value["value"] == step["expected"], {
                "matched": value["value"] == step["expected"]
            }
        if candidate["truncated"]:
            raise ValueError("Truncated text cannot prove equality")
        return candidate["text"] == step["expected"], observation

    def close(self):
        if getattr(self, "performance_collector", None) and self.cdp:
            try:
                self.performance_collector.capture(self)
            except Exception as exc:  # noqa: BLE001 -- observation failure must not prevent browser teardown
                self.performance_collector.notes.append(
                    "Browser metrics unavailable: " + type(exc).__name__
                )
        if self.cdp:
            try:
                self.cdp.call("Browser.close", timeout=1)
            except Exception:  # noqa: BLE001, S110 -- disconnect before ack is normal; owned teardown must continue
                pass
            self.cdp.close()
        if getattr(self, "close_job", None):
            self.close_job()
            self.close_job = None
        if self.process and self.process.poll() is None:
            try:
                if os.name != "nt" and self.owns_process_group:
                    os.killpg(self.process.pid, signal.SIGTERM)
                else:
                    self.process.terminate()
                self.process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    if os.name != "nt" and self.owns_process_group:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    else:
                        self.process.kill()
                    self.process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        if self.log_thread:
            self.log_thread.join(0.5)
            if not self.log_thread.is_alive():
                self.process.stderr.close()
        if self.proxy:
            self.proxy.close()
        deadline = time.monotonic() + 2
        while True:
            try:
                self.temp.cleanup()
                break
            except PermissionError:
                if os.name != "nt" or time.monotonic() >= deadline:
                    raise
                time.sleep(.05)


class WebEngine:
    def __init__(self, root, project):
        self.root, self.project = root, project

    def capabilities(self):
        from iatest.engines.readiness import browser_prerequisites

        readiness = browser_prerequisites()

        return {
            "kind": "web",
            "available": any(row["prerequisites_found"] for row in readiness.values()),
            "backend": "native-cdp+w3c-webdriver",
            "backends": {name: row["prerequisites_found"] for name, row in readiness.items()},
            "network_policies": {
                "chromium": ["strict"],
                "firefox": ["strict"],
                "webkit": ["strict", "navigation_only"],
            },
            "readiness": readiness,
            "actions": [
                "navigate",
                "reload",
                "restart_session",
                "find",
                "click",
                "fill",
                "press",
                "expect",
                "wait",
                "screenshot",
                "open_page",
                "switch_page",
                "close_page",
                "expect_popup",
                "upload",
                "download",
                "scroll",
                "scroll_find",
                "visual_click",
            ],
            "limitations": [
                "Chromium CDP; Firefox/Safari use owned native WebDriver services; strict proxy is the default, Safari navigation_only is explicit",
                "Explicit closed Shadow and planar perspective frames supported by CDP; opaque content and blob/data downloads require source/content digests",
                "Virtual-list searches are bounded; unmounted items cannot be proved absent",
                "Bounded local queries; internal native selector/AX cost unknown",
                "Origin proxy and fresh browser profile are application controls, not an OS sandbox",
                "No automatic replay of uncertain input",
            ],
        }

    def execute(self, plan: ExecutionPlan, context: EngineContext) -> EngineResult:
        validate_web(plan)
        result = EngineResult(
            plan.id,
            "web",
            run_id="test-" + uuid.uuid4().hex[:20],
            limitations=self.capabilities()["limitations"],
        )
        browser = None
        actions, evidence_bytes, previous_queries = 0, 0, 0

        def charge(count):
            nonlocal actions
            actions += count
            if actions > plan.budget.get("max_actions", 100):
                raise ValueError("Browser action budget exhausted")

        try:
            browser = BrowserSession(
                self.root,
                self.project,
                plan.spec,
                context.control,
                max_queries=plan.budget.get("max_queries", 500),
            )
            browser.action_charge = charge
            for index, step in enumerate(plan.spec["steps"]):
                step_started = time.monotonic()
                context.control.check()
                op = step["op"]
                row = {"index": index, "op": op}
                if op in {
                    "click",
                    "visual_click",
                    "fill",
                    "press",
                    "navigate",
                    "reload",
                    "restart_session",
                    "open_page",
                    "switch_page",
                    "close_page",
                    "expect_popup",
                    "upload",
                    "download",
                    "scroll",
                    "scroll_find",
                }:
                    if op != "scroll_find":
                        charge(1)
                    if op == "restart_session":
                        previous_queries += browser.queries
                        browser.close()
                        browser = None
                        browser = BrowserSession(
                            self.root,
                            self.project,
                            plan.spec,
                            context.control,
                            max_queries=plan.budget.get("max_queries", 500)
                            - previous_queries,
                        )
                        browser.action_charge = charge
                        row["result"] = {"fresh_profile": True}
                    elif op in {"navigate", "reload"}:
                        row["result"] = browser.navigate(
                            step["url"]
                            if op == "navigate"
                            else browser.evaluate("location.href")
                        )
                    elif op in {
                        "open_page",
                        "switch_page",
                        "close_page",
                        "expect_popup",
                    }:
                        row["result"] = browser.page_action(step)
                    elif op == "upload":
                        row["result"] = browser.upload(step)
                    elif op == "visual_click":
                        row["result"] = browser.visual_click(step)
                    elif op in {"scroll", "scroll_find"}:
                        row["result"] = browser.scroll(step)
                    elif op == "download":
                        data, metadata = browser.download(step)
                        evidence_bytes += len(data)
                        if evidence_bytes > plan.budget.get(
                            "max_evidence_bytes", 8388608
                        ):
                            raise ValueError("Evidence quota exceeded")
                        name = uuid.uuid4().hex + ".bin"
                        path = EngineStore(self.root, self.project).evidence_path(
                            result.run_id, name
                        )
                        path.parent.mkdir(parents=True, exist_ok=True)
                        atomic_write_bytes(path, data)
                        row["result"] = {**metadata, "type": "download", "name": name}
                        if "expected_name" in step or "expected_sha256" in step:
                            row["passed"] = metadata["passed"]
                        result.evidence.append(row["result"])
                        if not metadata["passed"]:
                            result.verdict = "failed"
                            result.steps.append(row)
                            break
                    else:
                        row["result"] = browser.act(step)
                elif op == "find":
                    row["result"] = browser.query(step["target"])
                elif op in {"expect", "wait"}:
                    deadline = time.monotonic() + step.get("timeout_seconds", 5)
                    while True:
                        passed, observation = browser.assertion(step)
                        if passed or time.monotonic() >= deadline:
                            break
                        context.control.wait(0.05)
                    row.update(passed=passed, result=observation)
                    if not passed:
                        result.steps.append(row)
                        result.verdict = "failed"
                        break
                else:
                    capture = {"format": "png", "captureBeyondViewport": False}
                    if "target" in step:
                        region = browser.query(step["target"])
                        if not region["unique"]:
                            raise ValueError(
                                "Screenshot region must be unique and complete"
                            )
                        rect = region["candidates"][0]["rect"]
                        rect, _ = browser.root_geometry(rect)
                        viewport = browser.root_viewport()
                        left, top = max(0, rect["x"]), max(0, rect["y"])
                        width = min(viewport["width"], rect["x"] + rect["width"]) - left
                        height = (
                            min(viewport["height"], rect["y"] + rect["height"]) - top
                        )
                        if width <= 0 or height <= 0:
                            raise ValueError(
                                "Screenshot region is outside the viewport"
                            )
                        capture["clip"] = {
                            "x": left + viewport["x"],
                            "y": top + viewport["y"],
                            "width": width,
                            "height": height,
                            "scale": 1,
                        }
                    data = base64.b64decode(
                        browser._session_call(
                            browser.page_session,
                            "Page.captureScreenshot",
                            capture,
                        )["data"],
                        validate=True,
                    )
                    evidence_bytes += len(data)
                    if evidence_bytes > plan.budget.get("max_evidence_bytes", 8388608):
                        raise ValueError("Screenshot quota exceeded")
                    name = uuid.uuid4().hex + ".png"
                    path = EngineStore(self.root, self.project).evidence_path(
                        result.run_id, name
                    )
                    path.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_bytes(path, data)
                    evidence = {
                        "name": name,
                        "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "type": "screenshot",
                        "clip": capture.get("clip"),
                    }
                    result.evidence.append(evidence)
                    row["result"] = evidence
                result.steps.append(row)
                if getattr(browser, "performance_collector", None):
                    browser.performance_collector.action(
                        (time.monotonic() - step_started) * 1000
                    )
                if browser.queries > plan.budget.get("max_queries", 500):
                    raise ValueError("Browser query budget exhausted")
            if result.verdict == "not_evaluated":
                result.verdict = "passed"
        except RunCancelled as exc:
            result.lifecycle = (
                "timed_out" if "总时限" in context.control.reason() else "cancelled"
            )
            result.verdict, result.error = "inconclusive", str(exc)
        except Exception as exc:  # noqa: BLE001 -- report execution failure while retaining cleanup
            if isinstance(exc, LocatorUnavailable):
                result.metrics["failure_category"] = "locator_unavailable"
            result.lifecycle, result.verdict, result.error = (
                "error",
                "inconclusive",
                str(exc),
            )
        finally:
            if browser:
                result.metrics.update(
                    {
                        "actions": actions,
                        "queries": previous_queries + browser.queries,
                        "cdp_calls": browser.cdp.calls,
                        "protocol_bytes_sent": browser.cdp.sent,
                        "protocol_bytes_received": browser.cdp.received,
                        "evidence_bytes": evidence_bytes,
                        "browser": browser.version,
                        "network_policy": getattr(browser, "network_policy", "strict"),
                        "background_requests_restricted": getattr(browser, "network_policy", "strict") == "strict",
                        "blocked_proxy_connections": browser.proxy.blocked if browser.proxy else None,
                        "model_calls": 0,
                    }
                )
                browser.close()
        return result
