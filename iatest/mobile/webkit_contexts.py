"""Bounded WebKit frame/shadow resolution and explicitly mapped App pages."""

from __future__ import annotations

import hashlib
import json
import time
from urllib.parse import urlsplit

from iatest.core.sandbox import origin
from iatest.mobile.ios_capture import verify_pid

FRAME_POINT = """function(){const p=POINT,e=this;
if(!e.isConnected||!e.matches('iframe,frame'))throw Error('Frame detached');
if(typeof webkitConvertPointFromNodeToPage==='function'&&typeof webkitConvertPointFromPageToNode==='function'&&typeof WebKitPoint==='function'){
const s=getComputedStyle(e),pl=parseFloat(s.paddingLeft)||0,pt=parseFloat(s.paddingTop)||0,pr=parseFloat(s.paddingRight)||0,pb=parseFloat(s.paddingBottom)||0;
const left=e.clientLeft+pl,top=e.clientTop+pt,w=e.clientWidth-pl-pr,h=e.clientHeight-pt-pb;
if(w<=0||h<=0||p.width<=0||p.height<=0||p.x<0||p.y<0||p.x>=p.width||p.y>=p.height)throw Error('Empty frame geometry');
const convert=(x,y)=>webkitConvertPointFromNodeToPage(e,new WebKitPoint(x,y));
const q=[[left,top],[left+w,top],[left+w,top+h],[left,top+h]].map(([x,y])=>convert(x,y));
const cross=q.map((a,i)=>{const b=q[(i+1)%4],c=q[(i+2)%4];return(b.x-a.x)*(c.y-b.y)-(b.y-a.y)*(c.x-b.x)});
if(!q.every(a=>Number.isFinite(a.x)&&Number.isFinite(a.y)&&Math.abs(a.x)<1e7&&Math.abs(a.y)<1e7)||!(cross.every(v=>v>1e-6)||cross.every(v=>v< -1e-6)))throw Error('Invalid frame quad');
const lx=left+w*p.x/p.width,ly=top+h*p.y/p.height,page=convert(lx,ly),back=webkitConvertPointFromPageToNode(e,page);
if(Math.abs(back.x-lx)>.75||Math.abs(back.y-ly)>.75)throw Error('Frame projection is not invertible');
const X=page.x-scrollX,Y=page.y-scrollY;
for(let n=e;n;){const root=n.getRootNode(),hit=root.elementFromPoint(X,Y);if(hit!==n&&!n.contains(hit))throw Error('Frame obscured');n=root.host||null;}
return {x:X,y:Y,width:innerWidth,height:innerHeight};}
let m=new DOMMatrix(),n=e,depth=0;
for(;n;n=n.parentElement){if(++depth>48)throw Error('Frame geometry depth');const s=getComputedStyle(n);
if(s.perspective!=='none'||s.rotate&&s.rotate!=='none'||s.scale&&s.scale!=='none')throw Error('Unsupported frame transform');
let t=new DOMMatrix(s.transform==='none'?undefined:s.transform);if(!t.is2D)throw Error('Perspective frame');
const z=Number.parseFloat(s.zoom)||1;t=new DOMMatrix().scale(z).multiply(t);m=t.multiply(m);}
const r=e.getBoundingClientRect(),w=e.offsetWidth,h=e.offsetHeight,s=getComputedStyle(e);
const pl=parseFloat(s.paddingLeft)||0,pt=parseFloat(s.paddingTop)||0,pr=parseFloat(s.paddingRight)||0,pb=parseFloat(s.paddingBottom)||0;
if(!w||!h||!p.width||!p.height||!e.clientWidth||!e.clientHeight||Math.abs(m.a*m.d-m.b*m.c)<1e-8)throw Error('Empty frame geometry');
const x=(e.clientLeft+pl)+(e.clientWidth-pl-pr)*p.x/p.width,y=(e.clientTop+pt)+(e.clientHeight-pt-pb)*p.y/p.height;
const ox=r.left-Math.min(0,m.a*w,m.c*h,m.a*w+m.c*h),oy=r.top-Math.min(0,m.b*w,m.d*h,m.b*w+m.d*h);
const X=ox+m.a*x+m.c*y,Y=oy+m.b*x+m.d*y;
let q=e;for(;q;){let root=q.getRootNode(),hit=root.elementFromPoint(X,Y);if(hit!==q&&!q.contains(hit))throw Error('Frame obscured');q=root.host||null;}
return {x:X,y:Y,width:innerWidth,height:innerHeight};} """


class WKContexts:
    def page_id(self, page):
        return page["webSocketDebuggerUrl"].rsplit("/", 1)[-1]

    def owned_pages(self):
        from iatest.mobile.webkit import listing, local_url

        verify_pid(self.device.profile, self.pid, self.control)
        devices = [
            d
            for d in listing(self.spec["inspector_url"])
            if d.get("deviceId") == self.device.profile["device_id"]
        ]
        if len(devices) != 1:
            raise ValueError("WKWebView device registry changed")
        address = devices[0]["url"]
        address = local_url(
            address if address.startswith("http://") else "http://" + address
        )
        if address != self.registry_address:
            raise ValueError("WKWebView device port changed; reconnect explicitly")
        values = [
            p for p in listing(address) if p.get("appId") == "PID:" + str(self.pid)
        ]
        if len(values) > 16:
            raise ValueError("WKWebView page quota exceeded")
        return values

    def select_page(self, alias):
        from iatest.mobile.webkit import Inspector, local_url

        declaration = self.spec.get("pages", {}).get(alias, {})
        if alias not in self.pages and not declaration:
            raise ValueError("Unknown WKWebView page alias")
        identifier = (
            self.page_id(self.pages[alias])
            if alias in self.pages
            else declaration.get("page_id")
        )
        pages = self.owned_pages()
        matches = (
            [p for p in pages if self.page_id(p) == identifier]
            if identifier
            else [p for p in pages if p["url"] == declaration.get("url")]
        )
        if len(matches) != 1:
            raise ValueError("WKWebView page mapping is missing or ambiguous")
        page = matches[0]
        self.scope.check_browser(page["url"])
        socket = local_url(page["webSocketDebuggerUrl"], "ws")
        if urlsplit(socket).port != urlsplit(self.registry_address).port:
            raise ValueError("WKWebView page escaped the device port")
        if alias != self.page_alias or self.channel is None:
            if self.channel:
                self.channel.close()
            self.channel = None
            self.world = None
            channel = Inspector(socket, self.control)
            try:
                channel.call("Runtime.enable")
            except BaseException:
                channel.close()
                raise
            self.channel = channel
        self.pages[alias], self.page_alias = page, alias

    def native_target(self):
        return (
            self.spec.get("pages", {})
            .get(self.page_alias, {})
            .get("native_target", self.spec["native_target"])
        )

    def page_action(self, step):
        if step["op"] == "switch_page":
            self.select_page(step["page"])
        elif step["op"] == "expect_popup":
            alias = step["page"]
            if alias in self.pages or alias not in self.spec.get("pages", {}):
                raise ValueError(
                    "Declare a new popup alias and native WKWebView target"
                )
            self.scope.check_browser(step["url"])
            declaration = self.spec["pages"][alias]
            if declaration.get("url") != step["url"]:
                raise ValueError("Popup URL must match its explicit page declaration")
            deadline = time.monotonic() + min(
                step.get("timeout_seconds", 5), self.control.remaining() or 5
            )
            while True:
                matches = [
                    p
                    for p in self.owned_pages()
                    if p["url"] == step["url"]
                    and self.page_id(p)
                    not in {self.page_id(p) for p in self.pages.values()}
                ]
                if len(matches) > 1:
                    raise ValueError("WKWebView popup is ambiguous")
                if matches:
                    self.pages[alias] = matches[0]
                    self.select_page(alias)
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("WKWebView popup did not appear")
                self.control.wait(0.05)
        else:
            raise ValueError(
                "WKWebViews are created/closed by the App; use native UI, then bind/switch pages"
            )
        self.world = None
        return {
            "page": self.page_alias,
            "page_id": self.page_id(self.pages[self.page_alias]),
            "app_pid": self.pid,
        }

    def resource_tree(self):
        tree = self.channel.call("Page.getResourceTree")["frameTree"]
        pending, frames = [tree], {}
        while pending:
            item = pending.pop()
            frames[item["frame"]["id"]] = item
            if len(frames) > 128:
                raise ValueError("WKWebView frame metadata quota exceeded")
            pending.extend(item.get("childFrames", []))
        return tree, frames

    def node(self, handle):
        target = self.world[0] if self.world else self.channel.target
        # WebKit requires the document to be pushed before object -> node mapping.
        if not any(key[0] == target for key in self.channel.nodes):
            root = self.channel.call("DOM.getDocument", target=target)["root"]
            self.channel.remember(target, root)
        identifier = self.channel.call(
            "DOM.requestNode", {"objectId": handle}, target=target
        )["nodeId"]
        if (target, identifier) not in self.channel.nodes:
            root = self.channel.call("DOM.getDocument", target=target)["root"]
            self.channel.remember(target, root)
            self.channel.call("DOM.requestNode", {"objectId": handle}, target=target)
        return target, identifier

    def refresh_worlds(self):
        self.channel.contexts.clear()
        self.channel.call("Runtime.disable")
        self.channel.call("Runtime.enable")
        for target, info in list(self.channel.targets.items()):
            if info.get("type") == "frame":
                self.channel.call("Runtime.disable", target=target)
                self.channel.call("Runtime.enable", target=target)

    def frame_world(self, identifier):
        matches = [
            key
            for key, context in self.channel.contexts.items()
            if context.get("frameId") == identifier
            and (context.get("type") == "normal" or context.get("isPageContext"))
        ]
        if len(matches) != 1:
            raise ValueError(
                "WKWebView frame requires one current normal execution context"
            )
        return matches[0]

    def select_context(self, target):
        self.select_page(target.get("page", self.page_alias))
        self.world, self.frame_chain = None, []
        self.scope.check_browser(self.evaluate("location.href"))
        self.channel.call("Runtime.releaseObjectGroup", {"objectGroup": "iatest-wk"})
        self.refresh_worlds()
        tree, _ = self.resource_tree()
        self.frame_binding = [
            (tree["frame"]["id"], tree["frame"].get("loaderId"), tree["frame"]["url"])
        ]
        for descriptor in target.get("frame", []):
            self.scope.check_browser(descriptor["origin"])
            selector = json.dumps(descriptor["selector"])
            handle = self.evaluate(
                f"(()=>{{let n=document.querySelectorAll({selector});if(n.length!==1||!n[0].matches('iframe,frame'))throw Error('Ambiguous frame');return n[0]}})()",
                object_result=True,
            )["objectId"]
            source = self.function(
                handle,
                "function(){return this.hasAttribute('srcdoc')?this.srcdoc:this.src}",
            )
            owner_target, identifier = self.node(handle)
            metadata = self.channel.nodes.get((owner_target, identifier), {})
            child_id = metadata.get("contentFrameId")
            children = tree.get("childFrames", [])
            if not child_id:
                for child in children:
                    owner = self.channel.call(
                        "DOM.requestFrameOwner",
                        {"frameId": child["frame"]["id"]},
                        target=owner_target,
                    )
                    if owner["nodeId"] == identifier:
                        child_id = child["frame"]["id"]
                        break
            matches = [c for c in children if c["frame"]["id"] == child_id]
            if len(matches) != 1:
                raise ValueError(
                    "WKWebView frame does not belong to the explicit owner"
                )
            child = matches[0]
            url = child["frame"]["url"]
            if url.startswith(("about:", "data:")):
                if (
                    not descriptor.get("opaque_source_sha256")
                    or hashlib.sha256(source.encode()).hexdigest()
                    != descriptor["opaque_source_sha256"]
                ):
                    raise ValueError(
                        "Opaque WKWebView frame requires its exact source digest"
                    )
            else:
                self.scope.check_browser(url)
                if origin(url) != origin(descriptor["origin"]):
                    raise ValueError("WKWebView frame escaped its declared origin")
            self.frame_chain.append({"handle": handle, "world": self.world})
            self.world = self.frame_world(child_id)
            tree = child
            self.frame_binding.append((child_id, child["frame"].get("loaderId"), url))
        self.selected_document = self.evaluate("location.href")

    def context_root(self, target):
        handle = self.evaluate("document", object_result=True)["objectId"]
        for selector in target.get("shadow", []):
            host = self.function(
                handle,
                "function(){let a=this.querySelectorAll("
                + json.dumps(selector)
                + ");if(a.length!==1)throw Error('Ambiguous shadow host');return a[0]}",
                by_value=False,
            )["objectId"]
            opened = self.function(
                host, "function(){return this.shadowRoot}", by_value=False
            )
            if opened.get("objectId"):
                handle = opened["objectId"]
                continue
            target_id, identifier = self.node(host)
            self.channel.call(
                "DOM.requestChildNodes",
                {"nodeId": identifier, "depth": 1},
                target=target_id,
            )
            roots = self.channel.nodes.get((target_id, identifier), {}).get(
                "shadowRoots", []
            )
            if len(roots) != 1:
                raise ValueError(
                    "Explicit host requires one inspectable non-UA shadow root"
                )
            handle = self.channel.call(
                "DOM.resolveNode",
                {"nodeId": roots[0], "objectGroup": "iatest-wk"},
                target=target_id,
            )["object"]["objectId"]
        return handle

    def root_point(self, point):
        _, frames = self.resource_tree()
        for identifier, loader, url in self.frame_binding:
            frame = frames.get(identifier, {}).get("frame", {})
            if (frame.get("loaderId"), frame.get("url")) != (loader, url):
                raise ValueError("WKWebView frame document changed before input")
        if self.evaluate("location.href") != self.selected_document:
            raise ValueError("WKWebView selected document changed before input")
        for owner in reversed(self.frame_chain):
            # Explicit root world is represented by (root target, None), not the current frame.
            world = owner["world"] or (self.channel.target, None)
            point = self.function(
                owner["handle"],
                FRAME_POINT.replace("POINT", json.dumps(point)),
                world=world,
            )
        viewport = self.evaluate(
            "({x:visualViewport.offsetLeft,y:visualViewport.offsetTop,width:visualViewport.width,height:visualViewport.height,scale:visualViewport.scale,layoutWidth:innerWidth,layoutHeight:innerHeight})",
            world=(self.channel.target, None),
        )
        x, y = point["x"] - viewport["x"], point["y"] - viewport["y"]
        if not 0 <= x < viewport["width"] or not 0 <= y < viewport["height"]:
            raise ValueError("WKWebView root point is outside the visible viewport")
        if abs(viewport["scale"] - 1) > 0.001:
            # A zoomed visual viewport occupies the native container width.
            # Use its own CSS dimensions; the native driver checks containment.
            return {"x": x, "y": y, "width": viewport["width"], "height": viewport["height"]}
        return {
            # WKWebView's native frame follows layout viewport coordinates.
            # Keyboard panning changes visualViewport.offsetTop while WebKit's
            # client rectangles already include the content's rendered offset.
            "x": point["x"],
            "y": point["y"],
            "width": viewport["layoutWidth"],
            "height": viewport["layoutHeight"],
        }
