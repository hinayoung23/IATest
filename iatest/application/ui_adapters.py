"""Small native UI adapters shared by exploration, without full-tree snapshots."""

from __future__ import annotations

import time
import uuid

from iatest.engines.web import BrowserSession


class WebExplorer:
    def __init__(self, root, project, context, control, max_queries):
        self.root, self.project, self.context, self.control = (
            root,
            project,
            context,
            control,
        )
        self.max_queries, self.used = max_queries, 0
        self.ui_context = {}
        self.visual_anchors = set()
        self.charge_actions = lambda count: None
        self.browser = BrowserSession(
            root, project, context, control, max_queries=max_queries
        )
        self.browser.action_charge = lambda count: self.charge_actions(count)

    def outline(self, region="", ui_context=None):
        if ui_context is not None:
            self.ui_context = ui_context
        return self.browser.outline(region, self.ui_context)

    def find(self, target):
        self.ui_context = {
            k: target[k] for k in ("page", "frame", "shadow") if k in target
        }
        return self.browser.query(target, limit=3)

    def do(self, step):
        if "target" in step:
            self.ui_context = {
                k: step["target"][k]
                for k in ("page", "frame", "shadow")
                if k in step["target"]
            }
        if step["op"] == "restart_session":
            self.used += self.browser.queries
            self.browser.close()
            self.browser = None
            self.browser = BrowserSession(
                self.root,
                self.project,
                self.context,
                self.control,
                max_queries=self.max_queries - self.used,
            )
            self.ui_context = {}
            self.browser.action_charge = lambda count: self.charge_actions(count)
            return {"fresh_profile": True}
        if step["op"] in {"navigate", "reload"}:
            self.ui_context = {}
            self.browser.navigate(
                step["url"]
                if step["op"] == "navigate"
                else self.browser.evaluate("location.href")
            )
            return {"dispatch": "acknowledged"}
        if step["op"] in {"open_page", "switch_page", "close_page", "expect_popup"}:
            value = self.browser.page_action(step)
            self.ui_context = {"page": self.browser.page_alias}
            return value
        if step["op"] in {"scroll", "scroll_find"}:
            return self.browser.scroll(step)
        if step["op"] == "visual_click":
            import json

            if (
                json.dumps([step["target"], step["visual_anchor"]], sort_keys=True)
                not in self.visual_anchors
            ):
                raise ValueError("Visual action must use an observed candidate anchor")
            return self.browser.visual_click(step)
        return self.browser.act(step)

    def visual_find(self, target, goal, gateway_factory=None):
        import json

        from iatest.application.ui_visual import locate_region

        value = locate_region(
            self.root,
            self.project,
            self.browser,
            target,
            goal,
            gateway_factory=gateway_factory,
        )
        self.visual_anchors = (
            {
                json.dumps([target, c["anchor"]], sort_keys=True)
                for c in value["candidates"]
            }
            if len(value["candidates"]) == 1
            else set()
        )
        value["unique"] = len(value["candidates"]) == 1
        return value

    def check(self, step):
        deadline = time.monotonic() + step.get("timeout_seconds", 2)
        while True:
            passed, observation = self.browser.assertion(step)
            if passed or time.monotonic() >= deadline:
                return passed, observation
            self.control.wait(0.1)

    def close(self):
        if self.browser:
            self.browser.close()


class MobileExplorer:
    def __init__(self, root, project, context, control, max_queries):
        from iatest.mobile.runtime import MobileSession
        from iatest.mobile.store import MobileStore

        self.root, self.project, self.context = root, project, context

        self.profile = MobileStore(root, project).get("profile", context["profile"])
        if self.profile["platform"] != context["kind"]:
            raise ValueError("Profile platform differs from requested exploration kind")
        self.control, self.max_queries, self.queries = control, max_queries, 0
        self.session = MobileSession(
            self.profile, control=control, root=root, project=project
        )
        self.system = None
        self.visual_anchors = set()

    def visual_find(self, target, goal, gateway_factory=None, visual_crop=None):
        import json

        from iatest.application.ui_visual import locate_region

        self.queries += 1
        if self.queries > self.max_queries:
            raise ValueError("Mobile visual query budget exhausted")
        value = locate_region(
            self.root,
            self.project,
            self.session,
            target,
            goal,
            gateway_factory=gateway_factory,
            visual_crop=visual_crop,
        )
        self.visual_anchors = (
            {
                json.dumps([target, visual_crop, c["anchor"]], sort_keys=True)
                for c in value["candidates"]
            }
            if len(value["candidates"]) == 1
            else set()
        )
        value["unique"] = len(value["candidates"]) == 1
        return value

    def find(self, target, *, fresh=False):
        self.control.check()
        self.queries += 1
        if self.queries > self.max_queries:
            raise ValueError("Mobile query budget exhausted")
        return self.session.query(
            target,
            timeout=min(
                getattr(self.session, "query_timeout", 2), self.control.remaining() or 2
            ),
            max_nodes=512,
            limit=3,
            cache=not fresh,
        )

    def outline(self, region=""):
        capabilities = getattr(
            getattr(getattr(self.session, "device", None), "semantic", None),
            "capabilities",
            {},
        )
        if not region and "outline" in capabilities.get("actions", []):
            self.control.check()
            self.queries += 1
            if self.queries > self.max_queries:
                raise ValueError("Mobile query budget exhausted")
            return self.session.outline(
                timeout=min(
                    5
                    if getattr(self.session, "profile", {}).get("platform") == "ios"
                    else 2,
                    self.control.remaining() or 5,
                ),
                max_nodes=512,
                limit=12,
            )
        # Older drivers and iOS retain the explicit type-query path.
        return self.find(
            {
                "type": region
                or (
                    "android.widget.Button"
                    if self.profile["platform"] == "android"
                    else "button"
                ),
                "require_unique": False,
            }
        )

    def do(self, step):
        from iatest.mobile.system import SYSTEM_OPS, preflight

        if step["op"] == "text":
            supported = set(
                self.session.device.semantic.capabilities.get("input_modes", [])
            ) | set(self.session.device.input.capabilities.get("input_modes", []))
            if step.get("mode") not in supported:
                raise ValueError(
                    "Text mode is unavailable; select from: "
                    + ", ".join(sorted(supported))
                )
        if step["op"] in SYSTEM_OPS:
            if self.system is None:
                from iatest.mobile.system import MobileSystem

                self.system = MobileSystem(
                    self.session, self.root, self.project, self.context.get("webview")
                )
            preflight(
                self.profile, {"steps": [step], "webview": self.context.get("webview")}
            )
            value = self.system.execute(step, self.max_queries - self.queries)
            self.queries += value.get("queries", 0)
            return value
        if step["op"] == "visual_click":
            import json

            if (
                json.dumps(
                    [step["target"], step.get("visual_crop"), step["visual_anchor"]],
                    sort_keys=True,
                )
                not in self.visual_anchors
            ):
                raise ValueError(
                    "Native visual action requires an observed unique anchor"
                )
            self.queries += 2
            if self.queries > self.max_queries:
                raise ValueError("Mobile visual query budget exhausted")
            return self.session.visual_click(
                step, uuid.uuid4().hex, min(5, self.control.remaining() or 5)
            )
        observation = (
            self.find(step["target"], fresh=True) if "target" in step else None
        )
        self.control.check()
        return self.session.action(
            step,
            observation,
            operation_id=uuid.uuid4().hex,
            timeout=min(
                self.session.action_timeout,
                self.control.remaining() or self.session.action_timeout,
            ),
        )

    def bridge_hints(self):
        from iatest.mobile.bridge import read_bridge

        return read_bridge(
            self.root,
            self.project,
            self.context["profile"],
            self.context["bridge_artifact_id"],
            control=self.control,
            session=self.session,
        )

    def check(self, step):
        deadline = time.monotonic() + step.get("timeout_ms", 2000) / 1000
        while True:
            observation = self.find(step["target"], fresh=True)
            if (
                not observation.get("complete")
                or observation.get("match_count_is_lower_bound")
                or observation.get("scope_error")
            ):
                raise ValueError(
                    "Incomplete mobile observation cannot prove an assertion"
                )
            count = observation["match_count"]
            attribute = step.get("attribute", "text")
            if attribute == "exists":
                passed = (count == 1) == step["expected"]
                if count > 1:
                    raise ValueError("Ambiguous mobile expectation")
            else:
                candidate = self.session.unique(observation)
                if (
                    candidate.get("secure")
                    or candidate.get("truncated")
                    or attribute in candidate.get("truncated_attributes", [])
                    or attribute in candidate.get("ambiguous_attributes", [])
                ):
                    raise ValueError(
                        "Secure, truncated or ambiguous value cannot prove an assertion"
                    )
                passed = candidate.get(attribute) == step["expected"]
            if passed or time.monotonic() >= deadline:
                return passed, observation
            self.control.wait(0.1)

    def close(self):
        try:
            if self.system:
                self.system.close()
        finally:
            self.session.close()


def create_explorer(root, project, context, control, max_queries):
    backend = {"web": WebExplorer, "desktop": DesktopExplorer}.get(
        context["kind"], MobileExplorer
    )
    return backend(root, project, context, control, max_queries)


class DesktopExplorer:
    def __init__(self, root, project, context, control, max_queries):
        from iatest.core.resources import resource_scope
        from iatest.engines.desktop import DesktopSession

        self.root, self.project = root, project
        self.context, self.control = context, control
        self.visual_anchors = set()
        self.queries, self.max_queries = 0, max_queries
        self.lease = resource_scope(
            [f"desktop:{context['platform']}:{context['process_id']}"], control
        )
        self.lease.__enter__()
        try:
            self.session = DesktopSession(context, control)
        except BaseException:
            self.lease.__exit__(None, None, None)
            raise

    def find(self, target):
        from iatest.engines.desktop import TARGET
        from iatest.engines.http import validate

        validate(target, TARGET)
        self.queries += 1
        if self.queries > self.max_queries:
            raise ValueError("Desktop query budget exhausted")
        value = self.session.call("query", target)
        value["unique"] = value.get("complete") and value.get("match_count") == 1
        return value

    def outline(self, region=""):
        roles = (
            [region]
            if region
            else (
                ["AXButton", "AXTextField", "AXCheckBox", "AXStaticText"]
                if self.context["platform"] == "macos"
                else ["push button", "text", "check box", "label"]
                if self.context["platform"] == "linux"
                else [
                    "ControlType.Button",
                    "ControlType.Edit",
                    "ControlType.CheckBox",
                    "ControlType.Text",
                ]
            )
        )
        candidates = []
        for role in roles:
            candidates.extend(self.find({"role": role}).get("candidates", []))
        controls = [
            {
                **candidate,
                "target": {
                    k: candidate[k]
                    for k in ("identifier", "name", "role")
                    if candidate.get(k)
                },
            }
            for candidate in candidates[:12]
        ]
        return {
            "candidates": candidates[:12],
            "controls": controls,
            "complete": False,
            "unique": False,
            "match_count_is_lower_bound": True,
        }

    def do(self, step):
        from iatest.engines.desktop import ACTION_FIELDS
        if step["op"] == "visual_click":
            import json
            if json.dumps([step["target"], step["visual_anchor"]], sort_keys=True) not in self.visual_anchors:
                raise ValueError("Visual action must use an observed candidate anchor")
            return self.session.visual_click(step)
        return self.session.call(
            step["op"],
            step["target"],
            **{k: step[k] for k in ACTION_FIELDS if k in step},
        )

    def visual_find(self, target, goal, gateway_factory=None):
        import json

        from iatest.application.ui_visual import locate_region
        value = locate_region(self.root, self.project, self.session, target, goal, gateway_factory=gateway_factory)
        self.visual_anchors = {json.dumps([target, c["anchor"]], sort_keys=True) for c in value["candidates"]} if len(value["candidates"]) == 1 else set()
        value["unique"] = len(value["candidates"]) == 1
        return value

    def check(self, step):
        deadline = time.monotonic() + step.get("timeout_seconds", 2)
        while True:
            value = self.find(step["target"])
            if not value.get("complete"):
                raise ValueError(
                    "Incomplete desktop observation cannot prove an assertion"
                )
            count, field = value["match_count"], step["attribute"]
            if any(
                field in c.get("truncated_attributes", [])
                for c in value.get("candidates", [])
            ):
                raise ValueError(
                    "Truncated desktop observation cannot prove an assertion"
                )
            if count > 1:
                raise ValueError("Desktop assertion target is ambiguous")
            passed = (
                (count == 1) == step["expected"]
                if field == "exists"
                else (
                    count == 1 and value["candidates"][0].get(field) == step["expected"]
                )
            )
            if passed or time.monotonic() >= deadline:
                return passed, value
            self.control.wait(0.1)

    def close(self):
        try:
            self.session.close()
        finally:
            self.lease.__exit__(None, None, None)
