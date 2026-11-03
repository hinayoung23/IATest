"""Browser policy survives console creation and model-authored drafts."""

import copy

import pytest

from iatest.agents.runtime import AgentRuntimeError
from iatest.ai.provider import ModelResponse, ModelToolCall
from iatest.application.testing_service import draft_test_plan
from iatest.core.config import init_project
from iatest.engines.store import EngineStore
from iatest.web.testing import build_plan


@pytest.mark.parametrize(
    "authored", [{}, {"browser": "webkit", "network_policy": "navigation_only"}]
)
def test_safari_draft_binds_authorized_mode_when_model_omits_fields(tmp_path, authored, monkeypatch):
    def unexpected_browser(*args, **kwargs):
        pytest.fail("Saving a draft without ui_find must not open a browser")

    monkeypatch.setattr("iatest.engines.web.BrowserSession", unexpected_browser)
    init_project(tmp_path, "suite")
    context = {
        "allowed_origins": ["http://127.0.0.1:8766"],
        "start_url": "http://127.0.0.1:8766/ui",
        "browser": "webkit",
        "network_policy": "navigation_only",
    }
    proposed = {
        "id": "safari-draft",
        "kind": "web",
        "spec": {
            "allowed_origins": context["allowed_origins"],
            "start_url": context["start_url"],
            "steps": [{"op": "expect", "assert": "exists", "target": {"id": "login"}}],
            **authored,
        },
    }

    class Gateway:
        def __init__(self, config):
            self.model_config = config

        def complete(self, **kwargs):
            return ModelResponse(
                tool_calls=[
                    ModelToolCall(
                        "submit", "submit_test_plan", {"plan": copy.deepcopy(proposed)}
                    )
                ]
            )

    result = draft_test_plan(
        tmp_path, "suite", "web", "Check login exists", context, gateway_factory=Gateway
    )
    saved = EngineStore(tmp_path, "suite").get_plan(result["plan"]["id"])
    assert saved["spec"]["browser"] == "webkit"
    assert saved["spec"]["network_policy"] == "navigation_only"
    assert EngineStore(tmp_path, "suite").runs() == []


def test_draft_observation_opens_one_authorized_browser_and_closes_it(tmp_path, monkeypatch):
    init_project(tmp_path, "suite")
    context = {"allowed_origins": ["http://127.0.0.1:8766"],
               "start_url": "http://127.0.0.1:8766/ui", "browser": "webkit",
               "network_policy": "navigation_only"}
    opened, queries, closed = [], [], []

    class Browser:
        def __init__(self, root, project, spec, control):
            opened.append(spec)

        def query(self, target, limit):
            queries.append((target, limit))
            return {"complete": True, "match_count": 1, "candidates": []}

        def close(self):
            closed.append(True)

    monkeypatch.setattr("iatest.engines.web.BrowserSession", Browser)

    class Gateway:
        def __init__(self, config):
            self.model_config, self.calls = config, 0

        def complete(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(tool_calls=[
                    ModelToolCall("find-one", "ui_find", {"target": {"id": "login"}}),
                    ModelToolCall("find-two", "ui_find", {"target": {"id": "status"}, "limit": 2}),
                ])
            return ModelResponse(tool_calls=[ModelToolCall("submit", "submit_test_plan", {
                "plan": {"id": "observed-draft", "kind": "web", "spec": {
                    **context, "steps": [{"op": "expect", "assert": "exists", "target": {"id": "login"}}],
                }},
            })])

    draft_test_plan(tmp_path, "suite", "web", "Check login", context, gateway_factory=Gateway)
    assert opened == [context]
    assert queries == [({"id": "login"}, 3), ({"id": "status"}, 2)]
    assert closed == [True]


@pytest.mark.parametrize(
    "changed",
    [
        {"browser": "firefox"},
        {"network_policy": "navigation_only"},
        {"webdriver_url": "http://127.0.0.1:9999"},
    ],
)
def test_model_cannot_change_browser_driver_or_weaken_default_policy(tmp_path, changed):
    init_project(tmp_path, "suite")
    context = {
        "allowed_origins": ["http://127.0.0.1:8766"],
        "start_url": "http://127.0.0.1:8766/ui",
        "browser": "webkit",
    }
    proposed = {
        "id": "unauthorized-mode",
        "kind": "web",
        "spec": {
            **context,
            "steps": [{"op": "expect", "assert": "exists", "target": {"id": "login"}}],
            **changed,
        },
    }

    class Gateway:
        def __init__(self, config):
            self.model_config = config
            self.calls = 0

        def complete(self, **kwargs):
            self.calls += 1
            if self.calls > 1:
                assert any(
                    "Generated plan changed the authorized browser/network policy" in str(message.get("content"))
                    for message in kwargs["messages"] if message["role"] == "tool"
                )
                return ModelResponse(text="Policy rejected")
            return ModelResponse(
                tool_calls=[
                    ModelToolCall("submit", "submit_test_plan", {"plan": proposed})
                ]
            )

    with pytest.raises(AgentRuntimeError, match="submit_test_plan"):
        draft_test_plan(
            tmp_path,
            "suite",
            "web",
            "Check login exists",
            context,
            gateway_factory=Gateway,
        )
    assert EngineStore(tmp_path, "suite").plans() == []


def test_console_plan_persists_safari_mode(tmp_path):
    init_project(tmp_path, "suite")
    data = {
        k: [v]
        for k, v in {
            "kind": "web",
            "id": "safari-console",
            "url": "http://127.0.0.1:8766/ui",
            "origins": "http://127.0.0.1:8766",
            "browser": "webkit",
            "network_policy": "navigation_only",
            "op": "click",
            "selector": "#login",
            "expected_selector": "#status",
            "expected": "Hello",
            "max_requests": "100",
        }.items()
    }
    store = EngineStore(tmp_path, "suite")
    plan = build_plan(store, data)
    assert plan["spec"]["browser"] == "webkit"
    assert plan["spec"]["network_policy"] == "navigation_only"
    del data["network_policy"]
    assert build_plan(store, data)["spec"]["network_policy"] == "strict"
