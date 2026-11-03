"""WebKit Inspector DOM observations with XCTest input on the bound WKWebView."""

from __future__ import annotations

import json
import time
import uuid
from urllib.parse import urldefrag, urlsplit

import httpx

from iatest.engines.cdp import CDP
from iatest.engines.http import NetworkScope
from iatest.engines.web import validate_target
from iatest.mobile.ios_capture import resolve_pid, verify_pid
from iatest.mobile.webkit_contexts import WKContexts


def local_url(value, scheme="http"):
    p = urlsplit(value)
    if (
        p.scheme != scheme
        or p.hostname not in {"127.0.0.1", "localhost"}
        or not p.port
        or p.username
        or p.password
        or p.query
        or p.fragment
    ):
        raise ValueError(
            "Inspector must use an explicit loopback URL without credentials"
        )
    return value.rstrip("/")


def listing(url):
    url = local_url(url)
    endpoint = url if url.endswith("/json") else url + "/json"
    with (
        httpx.Client(trust_env=False, timeout=3, follow_redirects=False) as client,
        client.stream("GET", endpoint) as response,
    ):
        response.raise_for_status()
        raw = bytearray()
        for chunk in response.iter_bytes():
            raw.extend(chunk)
            if len(raw) > 262144:
                raise ValueError("Inspector listing exceeded quota")
    values = json.loads(raw)
    if not isinstance(values, list) or len(values) > 256:
        raise ValueError("Invalid Inspector listing")
    return values


class Inspector:
    def __init__(self, url, control):
        self.target = None
        self.routed = False
        self.targets, self.contexts, self.nodes = {}, {}, {}
        self.call_target = None
        self.channel = CDP(local_url(url, "ws"), control, message_hook=self.receive)
        try:
            self.channel.call(
                "Target.setPauseOnStart", {"pauseOnStart": False}, timeout=3
            )
        except RuntimeError:
            if self.target:
                self.channel.close()
                raise
        except BaseException:
            self.channel.close()
            raise

    def receive(self, packet):
        if packet.get("method") == "Target.targetCreated":
            info = packet["params"]["targetInfo"]
            target = info["targetId"]
            self.targets[target] = info
            if len(self.targets) > 32:
                raise ValueError("Inspector target quota exceeded")
            if info.get("type") == "frame" or info.get("isProvisional"):
                return None
            if info.get("type", "page") != "page":
                return None
            if self.target and self.target != target:
                raise ValueError("Inspector target changed; reconnect explicitly")
            self.target = target
            return None
        if packet.get("method") == "Target.dispatchMessageFromTarget":
            target = packet["params"]["targetId"]
            if target != self.target and target not in getattr(self, "targets", {}):
                raise ValueError("Inspector message belongs to another target")
            inner = json.loads(packet["params"]["message"])
            if "id" in inner:
                if target != (getattr(self, "call_target", None) or self.target):
                    raise ValueError("Inspector response belongs to another target")
                inner["id"] -= 1000000
            if self.observe(target, inner):
                return None
            return inner
        if packet.get("method") == "Target.didCommitProvisionalTarget":
            old, new = packet["params"]["oldTargetId"], packet["params"]["newTargetId"]
            if old != self.target or not self.targets.get(new, {}).get("isProvisional"):
                raise ValueError("Unbound Inspector target commit")
            self.target = new
            self.contexts.clear()
            self.nodes.clear()
            return None
        if self.observe(self.target, packet):
            return None
        if self.routed and "id" in packet:
            if "error" in packet:
                raise RuntimeError("Inspector rejected target routing")
            return None
        return packet

    def remember(self, target, node):
        pending = [node]
        while pending:
            item = pending.pop()
            key = (target, item["nodeId"])
            self.nodes[key] = {
                k: item[k]
                for k in ("nodeId", "contentFrameId", "frameId", "shadowRootType")
                if k in item
            }
            self.nodes[key]["shadowRoots"] = [
                n["nodeId"]
                for n in item.get("shadowRoots", [])
                if n.get("shadowRootType") in {"open", "closed"}
            ]
            if item.get("contentDocument"):
                self.nodes[key]["contentFrameId"] = item["contentDocument"].get(
                    "frameId"
                )
            pending.extend(item.get("children", []))
            pending.extend(item.get("shadowRoots", []))
            if item.get("contentDocument"):
                pending.append(item["contentDocument"])
            if len(self.nodes) > 2048 or len(pending) > 2048:
                raise ValueError("Inspector node metadata quota exceeded")

    def observe(self, target, packet):
        method, params = packet.get("method"), packet.get("params", {})
        if method == "Runtime.executionContextCreated":
            context = params["context"]
            self.contexts[(target, context["id"])] = context
            if len(self.contexts) > 256:
                raise ValueError("Inspector context quota exceeded")
            return True
        if method == "DOM.setChildNodes":
            for node in params.get("nodes", []):
                self.remember(target, node)
            return True
        if method == "DOM.shadowRootPushed":
            self.remember(target, params["root"])
            host = self.nodes.setdefault((target, params["hostId"]), {})
            if params["root"].get("shadowRootType") in {"open", "closed"}:
                host.setdefault("shadowRoots", []).append(params["root"]["nodeId"])
            return True
        if method == "DOM.shadowRootPopped":
            host = self.nodes.get((target, params["hostId"]), {})
            host["shadowRoots"] = [
                identifier
                for identifier in host.get("shadowRoots", [])
                if identifier != params["rootId"]
            ]
            return True
        if method == "DOM.documentUpdated":
            self.nodes = {k: v for k, v in self.nodes.items() if k[0] != target}
            self.contexts = {k: v for k, v in self.contexts.items() if k[0] != target}
        return False

    def call(self, method, params=None, timeout=5, *, target=None):
        target = target or self.target
        if not target:
            return self.channel.call(method, params, timeout=timeout)
        if (
            target != self.target
            and self.targets.get(target, {}).get("type") != "frame"
        ):
            raise ValueError("Inspector command target is outside the owned page")
        inner = {
            "id": self.channel.sequence + 1 + 1000000,
            "method": method,
            "params": params or {},
        }
        self.routed = True
        self.call_target = target
        try:
            return self.channel.call(
                "Target.sendMessageToTarget",
                {"targetId": target, "message": json.dumps(inner)},
                timeout=timeout,
            )
        except RuntimeError as exc:
            raise RuntimeError(f"WebKit {method}: {exc}") from exc
        finally:
            self.routed = False
            self.call_target = None

    def close(self):
        self.channel.close()


class WKWebViewBrowser(WKContexts):
    def __init__(self, device, root, project, spec, control, max_queries):
        self.device, self.spec, self.control = device, spec, control
        self.scope = NetworkScope(root, project, spec["allowed_origins"])
        self.queries, self.max_queries = 0, max_queries
        self.channel = None
        self.world = None
        self.frame_chain = []
        self.page_alias = "main"
        self.pages = {}
        if not getattr(device.semantic, "capabilities", {}).get("webkit_native_input"):
            raise ValueError("Rebuild the Runner for WKWebView native input")
        self.pid = resolve_pid(device.profile, control)
        devices = [
            d
            for d in listing(spec["inspector_url"])
            if d.get("deviceId") == device.profile["device_id"]
        ]
        if len(devices) != 1:
            raise ValueError("Inspector registry must identify the exact device")
        address = devices[0]["url"]
        address = address if address.startswith("http://") else "http://" + address
        self.registry_address = local_url(address)
        declared = spec.get("pages", {}).get("main", {})
        pages = [
            p for p in listing(address) if p.get("appId") == "PID:" + str(self.pid)
        ]
        if declared.get("page_id") or spec.get("page_id"):
            pages = [
                p
                for p in pages
                if p["webSocketDebuggerUrl"].rsplit("/", 1)[-1]
                == declared.get("page_id", spec.get("page_id"))
            ]
        if declared.get("url"):
            pages = [p for p in pages if p["url"] == declared["url"]]
        if len(pages) != 1:
            raise ValueError(
                "Select exactly one inspectable WKWebView of the target App PID"
            )
        page = pages[0]
        self.pages["main"] = page
        self.scope.check_browser(page["url"])
        socket_url = page["webSocketDebuggerUrl"]
        if urlsplit(socket_url).port != urlsplit(address).port:
            raise ValueError("Inspector page escaped its device port")
        try:
            self.channel = Inspector(socket_url, control)
            self.channel.call("Runtime.enable")
            self.scope.check_browser(self.evaluate("location.href"))
        except BaseException:
            self.close()
            raise

    def evaluate(self, expression, *, object_result=False, world=None):
        selected = self.world if world is None else world
        target, context = selected if selected else (None, None)
        reply = self.channel.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": not object_result,
                "objectGroup": "iatest-wk",
                **({"contextId": context} if context is not None else {}),
            },
            target=target,
        )
        if reply.get("wasThrown") or reply.get("exceptionDetails"):
            raise ValueError("WKWebView observation failed")
        return (
            reply.get("result", {})
            if object_result
            else reply.get("result", {}).get("value")
        )

    def function(self, handle, function, *, by_value=True, world=None):
        selected = self.world if world is None else world
        reply = self.channel.call(
            "Runtime.callFunctionOn",
            {
                "objectId": handle,
                "functionDeclaration": function,
                "returnByValue": by_value,
                "objectGroup": "iatest-wk",
            },
            target=selected[0] if selected else None,
        )
        if reply.get("wasThrown"):
            raise ValueError("WKWebView target became unavailable")
        return reply["result"].get("value") if by_value else reply["result"]

    def query(self, target, limit=3, **kwargs):
        validate_target(target)
        if "anchor" in target:
            raise ValueError(
                "WKWebView row anchors require a narrower semantic/selector target"
            )
        self.queries += 1
        if self.queries > self.max_queries or not 1 <= limit <= 8:
            raise ValueError("WKWebView query quota exceeded")
        self.select_context(target)
        root = self.context_root(target)
        # Only bounded observations cross to the host; opaque node handles stay here.
        script = """function(){const t=TARGET,begin=performance.now();let scope=this;
        if(t.scope){const a=scope.querySelectorAll(t.scope);if(a.length!==1)throw Error('Ambiguous scope');scope=a[0]}
        const css=t.id?'#'+CSS.escape(t.id):t.test_id?'[data-testid='+JSON.stringify(t.test_id)+']':t.selector||'*';
        const nodes=scope.querySelectorAll(css),matches=[];let complete=true,visited=0;
        for(const e of nodes){if(++visited>512||performance.now()-begin>100){complete=false;break}
          if(t.text!==undefined&&e.textContent.trim()!==t.text)continue;
          if(t.label!==undefined&&!(e.getAttribute('aria-label')===t.label||Array.from(e.labels||[]).some(l=>l.textContent.trim()===t.label)))continue;
          const role=e.getAttribute('role')||({BUTTON:'button',INPUT:'textbox',A:'link',TEXTAREA:'textbox'}[e.tagName]||'');
          if(t.role&&role!==t.role)continue;if(t.name&&e.getAttribute('aria-label')!==t.name&&e.textContent.trim()!==t.name)continue;
          const r=e.getBoundingClientRect();if(t.visible!==false&&(!r.width||!r.height||getComputedStyle(e).visibility==='hidden'))continue;
          matches.push(e);if(matches.length>8){complete=false;break}
        }return {matches,complete,visited}}""".replace("TARGET", json.dumps(target))
        wrapper = self.function(root, script, by_value=False)["objectId"]
        summary = self.function(
            wrapper,
            "function(){return {count:this.matches.length,complete:this.complete,visited:this.visited}}",
        )
        candidates = []
        self.handles = []
        for index in range(min(limit, summary["count"])):
            handle = self.function(
                wrapper, f"function(){{return this.matches[{index}]}}", by_value=False
            )["objectId"]
            row = self.function(
                handle,
                """function(){const r=this.getBoundingClientRect(),text=this.textContent.trim();return {
                text:text.slice(0,2000),truncated:text.length>2000,value:this.type==='password'?null:this.value,
                secure:this.type==='password',enabled:!this.disabled&&this.getAttribute('aria-disabled')!=='true',
                rect:{x:r.x,y:r.y,width:r.width,height:r.height},connected:this.isConnected}}""",
            )
            self.handles.append(handle)
            candidates.append({**row, "index": index})
        return {
            "candidates": candidates,
            "match_count": summary["count"],
            "search_complete": summary["complete"],
            "unique": summary["complete"] and summary["count"] == 1,
            "visited_nodes": summary["visited"],
        }

    def assertion(self, step):
        if step["assert"] == "url":
            value = self.evaluate("location.href")
            self.scope.check_browser(value)
            return value == step["expected"], {"matched": value == step["expected"]}
        observed = self.query(step["target"])
        if not observed["search_complete"]:
            raise ValueError(
                "Incomplete WKWebView observation cannot prove an assertion"
            )
        count, predicate = observed["match_count"], step["assert"]
        if predicate in {"exists", "absent"}:
            return count == (1 if predicate == "exists" else 0), observed
        if count != 1:
            raise ValueError("WKWebView assertion requires one target")
        row = observed["candidates"][0]
        if row["secure"] or row["truncated"]:
            raise ValueError("Secure/truncated WKWebView value cannot prove equality")
        return row.get(predicate) == step["expected"], {
            "matched": row.get(predicate) == step["expected"]
        }

    def act(self, step):
        verify_pid(self.device.profile, self.pid, self.control)
        observed = self.query(step["target"])
        if not observed["unique"]:
            raise ValueError("WKWebView action requires one complete target")
        row = observed["candidates"][0]
        if step["op"] == "fill" and row["secure"]:
            raise ValueError(
                "WKWebView secure fill requires an explicit native-field plan"
            )
        if step["op"] == "press" and step["key"] not in {"Enter", "Tab", "Backspace"}:
            raise ValueError("Unsupported WKWebView native key")
        # XCTest may wait for keyboard/layout animations while resolving the
        # native container. Sample DOM coordinates after that wait so a point
        # from the preceding visual viewport is never carried across it.
        native = self.device.semantic.request(
            "query", {"target": self.native_target(), "limit": 2}, timeout=5
        )
        if not native.get("complete") or native.get("match_count") != 1:
            raise ValueError("WKWebView native container is ambiguous")
        container = native["candidates"][0]
        point = self.function(
            self.handles[0],
            """function(){const r=this.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2,v=visualViewport;
            if(!this.isConnected||this.disabled||x<v.offsetLeft||y<v.offsetTop||x>=v.offsetLeft+v.width||y>=v.offsetTop+v.height)throw Error('Stale/offscreen target');
            for(let e=this;e;){let root=e.getRootNode(),h=root.elementFromPoint(x,y);if(!h||!(h===e||e.contains(h)))throw Error('Obscured target');e=root.host||null;}
            return {x,y,width:innerWidth,height:innerHeight}}""",
        )
        point = self.root_point(point)
        reply = self.device.semantic.request(
            "web_tap",
            {"ref": container["ref"], **point},
            operation_id=uuid.uuid4().hex,
            timeout=20,
        )
        if not reply.get("accepted"):
            raise RuntimeError("WKWebView tap delivery unconfirmed; never replay")
        if step["op"] in {"fill", "press"}:
            if step["op"] == "fill":
                if row["secure"]:
                    raise ValueError(
                        "WKWebView secure fill needs an explicit native-field plan"
                    )
                self.function(
                    self.handles[0],
                    "function(){if(this.getRootNode().activeElement!==this||!this.setSelectionRange)throw Error('Not editable');this.setSelectionRange(0,this.value.length);return true}",
                )
                text = step["value"]
            else:
                text = {"Enter": "\n", "Tab": "\t", "Backspace": "\b"}.get(step["key"])
                if text is None:
                    raise ValueError("Unsupported WKWebView native key")
            native = self.device.semantic.request(
                "query", {"target": self.native_target(), "limit": 2}, timeout=5
            )
            if not native.get("complete") or native.get("match_count") != 1:
                raise RuntimeError(
                    "WKWebView container changed after tap; never replay"
                )
            reply = self.device.semantic.request(
                "web_type",
                {
                    "ref": native["candidates"][0]["ref"],
                    "value": text,
                    "replace": step["op"] == "fill",
                },
                operation_id=uuid.uuid4().hex,
                timeout=20,
            )
            if not reply.get("accepted"):
                raise RuntimeError(
                    "WKWebView native input delivery unconfirmed; never replay"
                )
        self.scope.check_browser(
            self.evaluate("location.href", world=(self.channel.target, None))
        )
        return {
            "dispatch": "acknowledged",
            "business_postcondition": "pending",
            "backend": "webkit+xctest",
        }

    def navigate(self, url):
        self.world = None
        self.scope.check_browser(url)
        previous = self.evaluate("({url:location.href,epoch:performance.timeOrigin})")
        same_document = (
            previous["url"] != url
            and urldefrag(previous["url"])[0] == urldefrag(url)[0]
        )
        # WebKit has Page.reload but no Chromium Page.navigate command. This is
        # one scoped document navigation, never a call into App business logic.
        self.evaluate("location.assign(" + json.dumps(url) + ");true")
        deadline = time.monotonic() + min(10, self.control.remaining() or 10)
        while True:
            self.control.check()
            state = self.evaluate(
                "({url:location.href,ready:document.readyState,epoch:performance.timeOrigin})"
            )
            self.scope.check_browser(state["url"])
            if (
                state["url"] == url
                and state["ready"] in {"interactive", "complete"}
                and (same_document or state["epoch"] != previous["epoch"])
            ):
                return {"dispatch": "acknowledged", "url": url}
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "WKWebView navigation did not reach the declared URL"
                )
            self.control.wait(0.05)

    def close(self):
        if self.channel:
            self.channel.close()
            self.channel = None
