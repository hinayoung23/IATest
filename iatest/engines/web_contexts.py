"""Explicit browser contexts, bounded file delivery and virtual-list traversal."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from pathlib import Path

from iatest.core.sandbox import origin, policy_scope, read_file
from iatest.engines.projective import (
    IDENTITY,
    compose,
    frame_matrix,
    inverse,
    project,
    region,
)


class FrameNotReady(ValueError):
    """The selected HTTP frame still has its initial empty document."""


class BrowserContexts:
    def prepare_shadow(self, target):
        """Resolve only explicit shadow hosts, including closed roots, without a DOM dump."""
        if not target.get("shadow"):
            return
        self.evaluate(
            "globalThis.__iatestClosedRoots=new WeakMap();globalThis.__iatestShadowScope=document"
        )
        for selector in target.get("shadow", []):
            handle = self.evaluate(
                f"(()=>{{const n=globalThis.__iatestShadowScope.querySelectorAll({json.dumps(selector)});if(n.length!==1)throw Error('Ambiguous shadow host');return n[0]}})()",
                object_result=True,
            )
            node = self.call(
                "DOM.describeNode",
                {"objectId": handle["objectId"], "depth": 1, "pierce": True},
            )["node"]
            roots = [
                n
                for n in node.get("shadowRoots", [])
                if n.get("shadowRootType") in {"open", "closed"}
            ]
            if len(roots) != 1:
                raise ValueError(
                    "Explicit host requires exactly one non-UA shadow root"
                )
            resolved = self.call(
                "DOM.resolveNode",
                {
                    "backendNodeId": roots[0]["backendNodeId"],
                    "executionContextId": self.world,
                },
            )["object"]
            self.call(
                "Runtime.callFunctionOn",
                {
                    "objectId": resolved["objectId"],
                    "functionDeclaration": "function(host){globalThis.__iatestClosedRoots.set(host,this);globalThis.__iatestShadowScope=this}",
                    "arguments": [{"objectId": handle["objectId"]}],
                    "returnByValue": True,
                },
            )

    def _session_call(self, session, method, params=None, timeout=10):
        return self.cdp.call(method, params or {}, session, timeout)

    def _world(self, session, frame):
        key = (session, frame["id"], frame.get("loaderId"))
        if key not in self.context_worlds:
            self.context_worlds[key] = self._session_call(
                session,
                "Page.createIsolatedWorld",
                {
                    "frameId": frame["id"],
                    "worldName": "iatest",
                    "grantUniveralAccess": False,
                },
            )["executionContextId"]
            if len(self.context_worlds) > 128:
                self.context_worlds.pop(next(iter(self.context_worlds)))
        return self.context_worlds[key]

    def _evaluate_in(self, session, world, expression):
        result = self._session_call(
            session,
            "Runtime.evaluate",
            {
                "expression": expression,
                "contextId": world,
                "returnByValue": True,
                "awaitPromise": True,
                "objectGroup": "iatest",
                "timeout": 5000,
            },
        )
        if result.get("exceptionDetails"):
            raise ValueError("Browser context or frame geometry is unavailable")
        return result["result"].get("value")

    def select_context(self, target):
        from iatest.engines.browser_wait import wait_for_context
        return wait_for_context(self.control, lambda: self._select_context(target), FrameNotReady, "Selected frame did not commit its declared origin within the query budget")

    def _select_context(self, target):
        alias = target.get("page", self.page_alias)
        if alias not in self.pages:
            raise ValueError("Unknown browser page alias")
        self.page_alias = alias
        page = self.pages[alias]
        self.page_session = page["session"]
        self.session = self.page_session
        tree = self._session_call(self.session, "Page.getFrameTree")["frameTree"]
        self.world = self._world(self.session, tree["frame"])
        self.frame_chain = []
        self.opaque_context = False
        for descriptor in target.get("frame", []):
            self.scope.check_browser(descriptor["origin"])
            selector = json.dumps(descriptor["selector"])
            owner = self.evaluate(
                f"(()=>{{const n=document.querySelectorAll({selector}); if(n.length!==1||!n[0].matches('iframe,frame'))throw Error('Ambiguous frame');const e=n[0];globalThis.__iatestFrame=e;return {{src:e.src,source:e.hasAttribute('srcdoc')?e.srcdoc:e.src}};}})()"
            )
            if (
                owner.get("src")
                and not owner["src"].startswith(("about:", "data:"))
                and origin(owner["src"]) != origin(descriptor["origin"])
            ):
                raise ValueError("Frame source differs from declared origin")
            handle = self.evaluate("globalThis.__iatestFrame", object_result=True)
            node = self.call("DOM.describeNode", {"objectId": handle["objectId"]})[
                "node"
            ]
            frame_id = node.get("frameId")
            children = {
                child["frame"]["id"]: child for child in tree.get("childFrames", [])
            }
            self.frame_chain.append(
                {
                    "session": self.session,
                    "world": self.world,
                    "selector": descriptor["selector"],
                    "frame_id": frame_id,
                }
            )
            if frame_id in children:
                child = children[frame_id]
            else:
                # OOPIF roots are separate targets and absent from the parent frame tree.
                deadline = time.monotonic() + min(2, self.control.remaining() or 2)
                info = None
                while info is None and time.monotonic() < deadline:
                    info = next(
                        (
                            v
                            for v in self.cdp.call("Target.getTargets")["targetInfos"]
                            if v["targetId"] == frame_id
                        ),
                        None,
                    )
                    if info is None:
                        self.control.wait(0.05)
                        frame_id = self.call(
                            "DOM.describeNode", {"objectId": handle["objectId"]}
                        )["node"].get("frameId")
                        self.frame_chain[-1]["frame_id"] = frame_id
                if info is None:
                    raise ValueError(
                        "Cross-process frame did not become ready within its query budget"
                    )
                if (
                    info.get("browserContextId") != self.browser_context
                    or info.get("type") != "iframe"
                    or info.get("parentFrameId", info.get("parentId"))
                    != tree["frame"]["id"]
                ):
                    raise ValueError(
                        "Frame is outside the owned browser context"
                    ) from None
                if frame_id not in self.frame_sessions:
                    self.frame_sessions[frame_id] = self.cdp.call(
                        "Target.attachToTarget", {"targetId": frame_id, "flatten": True}
                    )["sessionId"]
                    self._session_call(self.frame_sessions[frame_id], "Page.enable")
                self.session = self.frame_sessions[frame_id]
                child = self._session_call(self.session, "Page.getFrameTree")[
                    "frameTree"
                ]
                if child["frame"]["id"] != frame_id:
                    raise ValueError("Frame target identity changed")
            url = child["frame"]["url"]
            if url in {"", "about:blank"} and owner.get("src", "").startswith(("http://", "https://")):
                # Read-only retry from the parent rechecks owner identity and
                # handles a later process swap; never infer origin from src.
                raise FrameNotReady("HTTP frame has not committed yet")
            if url.startswith(("about:", "data:")):
                if (
                    not descriptor.get("opaque_source_sha256")
                    or hashlib.sha256(owner["source"].encode()).hexdigest()
                    != descriptor["opaque_source_sha256"]
                ):
                    raise ValueError(
                        "Opaque frame requires its exact explicitly declared source digest"
                    )
                self.opaque_context = True
            else:
                self.scope.check_browser(url)
                if origin(url) != origin(descriptor["origin"]):
                    raise ValueError("Frame navigated outside its declared origin")
                self.opaque_context = False
            self.world = self._world(self.session, child["frame"])
            self.frame_chain[-1]["child_session"] = self.session
            tree = child
        self.selected_frame_id = tree["frame"]["id"]

    def frame_transforms(self):
        transforms = []
        session_roots = {self.page_session: IDENTITY}
        parent_global = IDENTITY
        for owner in self.frame_chain:
            selector = json.dumps(owner["selector"])
            geometry = self._evaluate_in(
                owner["session"],
                owner["world"],
                f"(()=>{{const n=document.querySelectorAll({selector});if(n.length!==1||n[0]!==globalThis.__iatestFrame)throw Error('Frame replaced');return {{width:n[0].clientWidth,height:n[0].clientHeight}};}})()",
            )
            handle = self._session_call(
                owner["session"],
                "Runtime.evaluate",
                {"expression": "globalThis.__iatestFrame", "contextId": owner["world"]},
            )["result"]
            quad = self._session_call(
                owner["session"], "DOM.getBoxModel", {"objectId": handle["objectId"]}
            )["model"]["content"]
            # CDP box quads are relative to this session's root viewport,
            # including same-process ancestors. Do not apply them twice.
            child_global = compose(session_roots[owner["session"]], frame_matrix(geometry["width"], geometry["height"], quad))
            transforms.append((owner, compose(inverse(parent_global), child_global)))
            if owner["child_session"] != owner["session"]:
                session_roots[owner["child_session"]] = child_global
            parent_global = child_global
        return list(reversed(transforms))

    def root_geometry(self, rect):
        matrix = IDENTITY
        for _, transform in self.frame_transforms():
            matrix = compose(transform, matrix)
        return region(matrix, rect), matrix

    def root_point(self, point, *, hit_test=False):
        x, y = point["x"], point["y"]
        for owner, matrix in self.frame_transforms():
            x, y = project(matrix, x, y)
            if hit_test:
                selector = json.dumps(owner["selector"])
                ready = self._evaluate_in(
                    owner["session"],
                    owner["world"],
                    f"(()=>{{const e=document.querySelector({selector}),h=document.elementFromPoint({x},{y});return !!(e&&h===e);}})()",
                )
                if not ready:
                    raise ValueError("Frame is obscured or outside its viewport")
        return {"x": x, "y": y}

    def input_call(self, method, params):
        return self._session_call(self.page_session, method, params)

    def position_pointer(self, point):
        # The browser input router learns the current OOPIF through a real
        # pointer move. Never retry a failed move or synthesize DOM events.
        self.pending = True
        self.input_call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": point["x"], "y": point["y"], "button": "none"})
        tree = self._session_call(self.page_session, "Page.getFrameTree")["frameTree"]
        world = self._world(self.page_session, tree["frame"])
        self._evaluate_in(self.page_session, world, "new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))")
        self.pending = False

    def root_viewport(self):
        tree = self._session_call(self.page_session, "Page.getFrameTree")["frameTree"]
        world = self._world(self.page_session, tree["frame"])
        return self._evaluate_in(
            self.page_session,
            world,
            "({x:scrollX,y:scrollY,width:innerWidth,height:innerHeight})",
        )

    def page_action(self, step):
        op, alias = step["op"], step["page"]
        if op in {"switch_page", "close_page"}:
            if alias not in self.pages:
                raise ValueError("Unknown page alias")
            if op == "close_page":
                if len(self.pages) == 1:
                    raise ValueError("Cannot close the final browser page")
                self.cdp.call(
                    "Target.closeTarget", {"targetId": self.pages[alias]["target"]}
                )
                del self.pages[alias]
                alias = next(iter(self.pages))
            self.select_context({"page": alias})
            self.scope.check_browser(self.evaluate("location.href"))
            return {"page": alias}
        if alias in self.pages or len(self.pages) >= 16:
            raise ValueError("Page alias exists or page limit reached")
        self.scope.check_browser(step["url"])
        if op == "open_page":
            target_id = self.cdp.call(
                "Target.createTarget",
                {"url": "about:blank", "browserContextId": self.browser_context},
            )["targetId"]
        else:
            deadline = time.monotonic() + step.get("timeout_seconds", 5)
            while True:
                values = self.cdp.call("Target.getTargets")["targetInfos"]
                matches = [
                    v
                    for v in values
                    if v.get("browserContextId") == self.browser_context
                    and v.get("openerId") == self.pages[self.page_alias]["target"]
                    and v.get("url") == step["url"]
                    and v["targetId"] not in {p["target"] for p in self.pages.values()}
                ]
                if len(matches) > 1:
                    raise ValueError("Popup is ambiguous")
                if matches:
                    target_id = matches[0]["targetId"]
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("Expected popup was not observed")
                self.control.wait(0.05)
        session = self.cdp.call(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}
        )["sessionId"]
        self.pages[alias] = {"target": target_id, "session": session}
        self._session_call(session, "Page.enable")
        self.select_context({"page": alias})
        if op == "open_page":
            self.navigate(step["url"])
        else:
            self.scope.check_browser(self.evaluate("location.href"))
        return {"page": alias, "url": step["url"]}

    def upload(self, step):
        observation = self.query(step["target"])
        if not observation["unique"]:
            raise ValueError("File input must be unique and complete")
        index = observation["candidates"][0]["index"]
        file_input = self.evaluate(
            f"(()=>{{const e=globalThis.__iatestNodes[{index}];return e.tagName==='INPUT'&&e.type==='file'&&!e.disabled;}})()"
        )
        if not file_input:
            raise ValueError("Upload requires an enabled file input")
        paths, total, staged = (
            [],
            0,
            Path(self.temp.name) / "uploads" / uuid.uuid4().hex,
        )
        staged.mkdir(parents=True)
        for value in step["files"]:
            path = self.scope.policy.check_path(value)
            with policy_scope(self.scope.policy):
                data = read_file(path, step.get("max_bytes", 8388608))
            total += len(data)
            if total > step.get("max_bytes", 8388608) or any(
                c in path.name for c in "\r\n\0"
            ):
                raise ValueError("Upload size/name quota exceeded")
            target = staged / path.name
            if target.exists():
                raise ValueError("Duplicate upload basename")
            target.write_bytes(data)
            paths.append(str(target))
        handle = self.evaluate(f"globalThis.__iatestNodes[{index}]", object_result=True)
        self.pending = True
        self.call(
            "DOM.setFileInputFiles", {"files": paths, "objectId": handle["objectId"]}
        )
        self.pending = False
        return {"dispatch": "acknowledged", "files": len(paths), "bytes": total}

    def scroll(self, step):
        container = step.get("container", step.get("target"))
        rounds = step.get("max_scrolls", 1) if step["op"] == "scroll_find" else 1
        for index in range(rounds + 1):
            if step["op"] == "scroll_find":
                observation = self.query(step["target"])
                if observation["unique"]:
                    return {"found": True, "scrolls": index, "observation": observation}
                if observation.get("match_count", 0) > 1:
                    raise ValueError("Virtual-list target is ambiguous")
                if index == rounds:
                    raise ValueError(
                        "Scroll budget exhausted; absence of unmounted items is unknown"
                    )
            observed = self.query(container)
            if not observed["unique"]:
                raise ValueError("Scroll container must be unique")
            self.evaluate(
                f"globalThis.__iatestNodes[{observed['candidates'][0]['index']}].scrollIntoView({{block:'center',inline:'center'}})"
            )
            observed = self.query(container)
            if not observed["unique"]:
                raise ValueError("Scroll container changed before input")
            rect = observed["candidates"][0]["rect"]
            position = observed["candidates"][0]["index"]
            usable = self.evaluate(
                f"(()=>{{const e=globalThis.__iatestNodes[{position}],h=globalThis.__iatestHit({rect['x'] + rect['width'] / 2},{rect['y'] + rect['height'] / 2});return !!(e&&h&&(h===e||e.contains(h)));}})()"
            )
            if not usable:
                raise ValueError("Scroll container is obscured")
            point = self.root_point(
                {
                    "x": rect["x"] + rect["width"] / 2,
                    "y": rect["y"] + rect["height"] / 2,
                },
                hit_test=True,
            )
            if step["op"] == "scroll_find":
                self.action_charge(1)
            self.pending = True
            self.input_call(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseWheel",
                    "x": point["x"],
                    "y": point["y"],
                    "deltaX": 0,
                    "deltaY": step.get("delta_y", 400),
                },
            )
            self.pending = False
            self.control.wait(0.05)
            if step["op"] == "scroll":
                return {"dispatch": "acknowledged"}

    def download(self, step):
        observed = self.query(step["target"], cache=False)
        if not observed["unique"]:
            raise ValueError("Download target must be unique")
        index = observed["candidates"][0]["index"]
        href = self.evaluate(f"globalThis.__iatestNodes[{index}].href||''")
        if href.startswith(("data:", "blob:")):
            return self.local_download(step, index, href)
        directory = Path(self.temp.name) / "downloads"
        directory.mkdir(exist_ok=True)
        limit = step.get("max_bytes", 8388608)
        overflow = self.cdp.event_overflows
        self.cdp.take_events()
        self.cdp.call(
            "Browser.setDownloadBehavior",
            {
                "behavior": "allowAndName",
                "downloadPath": str(directory),
                "browserContextId": self.browser_context,
                "eventsEnabled": True,
            },
        )
        guid, completed, filename = "", False, ""
        try:
            self.act({"op": "click", "target": step["target"]})
            expected_frame = self.selected_frame_id
            deadline = time.monotonic() + step.get("timeout_seconds", 10)
            while time.monotonic() < deadline:
                self.cdp.call("Browser.getVersion")
                if self.cdp.event_overflows != overflow:
                    raise ValueError("Download event coverage incomplete")
                for event in self.cdp.take_events():
                    value = event["params"]
                    if event["method"] == "Browser.downloadWillBegin":
                        self.scope.check_browser(value["url"])
                        if value.get("frameId") != expected_frame:
                            self.cdp.call(
                                "Browser.cancelDownload",
                                {
                                    "guid": value["guid"],
                                    "browserContextId": self.browser_context,
                                },
                            )
                            raise ValueError("Download came from another frame")
                        if guid and guid != value["guid"]:
                            raise ValueError(
                                "Multiple downloads from one step are ambiguous"
                            )
                        guid, filename = value["guid"], value["suggestedFilename"]
                    elif value.get("guid") == guid:
                        if (
                            max(
                                value.get("receivedBytes", 0),
                                value.get("totalBytes", 0),
                            )
                            > limit
                        ):
                            raise ValueError("Download size quota exceeded")
                        if value["state"] == "canceled":
                            raise ValueError("Download was canceled")
                        completed |= value["state"] == "completed"
                if guid and completed:
                    if not re.fullmatch(r"[a-f0-9-]{36}", guid):
                        raise ValueError("Invalid browser download identifier")
                    path = directory / guid
                    with policy_scope(None):
                        data = read_file(path, limit)
                    fingerprint = hashlib.sha256(data).hexdigest()
                    passed = (
                        not step.get("expected_sha256")
                        or fingerprint == step["expected_sha256"]
                    ) and (
                        not step.get("expected_name")
                        or filename == step["expected_name"]
                    )
                    return data, {
                        "filename": filename[:200],
                        "bytes": len(data),
                        "sha256": fingerprint,
                        "passed": passed,
                    }
                self.control.wait(0.05)
            raise TimeoutError("Download did not complete within its deadline")
        finally:
            if guid and not completed:
                self.cdp.call(
                    "Browser.cancelDownload",
                    {"guid": guid, "browserContextId": self.browser_context},
                )
            self.cdp.call(
                "Browser.setDownloadBehavior",
                {"behavior": "deny", "browserContextId": self.browser_context},
            )

    def local_download(self, step, index, href):
        import base64

        if not step.get("expected_sha256"):
            raise ValueError(
                "Local blob/data download requires an explicit content digest"
            )
        limit = step.get("max_bytes", 8388608)
        if href.startswith("blob:"):
            self.scope.check_browser(href[5:])
        value = self.evaluate(
            f"""(async()=>{{const e=globalThis.__iatestNodes[{index}];if(!e||e.href!=={json.dumps(href)})throw Error('Stale download');const r=await fetch(e.href),reader=r.body.getReader();let n=0,parts=[];try{{while(true){{let x=await reader.read();if(x.done)break;n+=x.value.length;if(n>{limit})throw Error('Download quota');parts.push(x.value)}}}}finally{{await reader.cancel()}}let s='';for(let p of parts)for(let v of p)s+=String.fromCharCode(v);return {{data:btoa(s),name:e.download||'download.bin'}}}})()""",
            timeout=step.get("timeout_seconds", 10),
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
