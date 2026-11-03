from types import SimpleNamespace

import pytest

from iatest.application.ui_adapters import MobileExplorer
from iatest.application.ui_exploration import Exploration
from iatest.execution.control import RunControl
from iatest.mobile.runtime import MobileSession


def backend():
    calls = []

    def request(op, args, **kwargs):
        calls.append((op, args))
        return {
            "controls": [
                {
                    "target": {"type": "android.view.View", "label": "我的"},
                    "label": "我的",
                    "visual_required": False,
                }
            ],
            "complete": True,
            "proves_uniqueness": False,
        }

    session = MobileSession.__new__(MobileSession)
    session.closed = False
    session.device = SimpleNamespace(
        semantic=SimpleNamespace(capabilities={"actions": ["outline"]}, request=request)
    )
    explorer = MobileExplorer.__new__(MobileExplorer)
    explorer.session = session
    explorer.control = RunControl(timeout_seconds=5)
    explorer.max_queries = 2
    explorer.queries = 0
    explorer.profile = {"platform": "android"}
    return explorer, calls


def test_generic_view_controls_are_discovered_without_button_assumptions_and_share_budget():
    explorer, calls = backend()
    result = explorer.outline()
    assert result["controls"][0]["target"] == {
        "type": "android.view.View",
        "label": "我的",
    }
    assert result["proves_uniqueness"] is False
    explorer.outline()
    with pytest.raises(ValueError, match="budget"):
        explorer.outline()
    assert len(calls) == 2
    assert all(
        op == "outline" and args == {"max_nodes": 512, "limit": 12}
        for op, args in calls
    )


def test_explorer_respects_ios_snapshot_timeout_and_remaining_budget():
    calls = []
    explorer = MobileExplorer.__new__(MobileExplorer)
    explorer.session = SimpleNamespace(query_timeout=5, query=lambda target, **kwargs: calls.append(kwargs) or {})
    explorer.control = RunControl(timeout_seconds=10)
    explorer.max_queries, explorer.queries = 3, 0
    explorer.find({"identifier": "save"})
    assert calls[-1]["timeout"] == 5
    explorer.control = RunControl(timeout_seconds=1)
    explorer.find({"identifier": "save"})
    assert 0 < calls[-1]["timeout"] <= 1


def test_discovered_targets_enter_exploration_without_becoming_uniqueness_proof():
    explorer, calls = backend()
    runtime = Exploration(
        "验证个人页可达",
        {"kind": "android", "max_queries": 5},
        explorer,
        {},
        RunControl(timeout_seconds=5),
    )
    result = runtime.observe()
    target = {"type": "android.view.View", "label": "我的"}
    assert target in runtime.recipes.values()
    assert result["observation"]["proves_uniqueness"] is False
    assert calls[0][0] == "outline"


def test_native_outline_rejects_oversized_or_unsupported_requests_before_dispatch():
    explorer, calls = backend()
    with pytest.raises(ValueError, match="budget"):
        explorer.session.outline(max_nodes=1025)
    with pytest.raises(ValueError, match="budget"):
        explorer.session.outline(limit=13)
    explorer.session.device.semantic.capabilities = {"actions": ["query"]}
    with pytest.raises(ValueError, match="Rebuild"):
        explorer.session.outline()
    assert not calls


def test_spatial_target_survives_compaction_and_console_json_without_dropping_empty_label():
    from iatest.application.ui_exploration import semantic_state
    from iatest.web.mobile import target_from_form

    target = {
        "type": "android.widget.ImageView",
        "label": "",
        "bounds": [628, 2181, 812, 2365],
    }
    import json

    assert target_from_form({"target_json": [json.dumps(target)]}) == target
    assert semantic_state(
        {"controls": [{"target": target, "bounds": [1, 2, 3, 4]}]}
    ) == {"controls": [{"target": target}]}


@pytest.mark.parametrize(
    "driver_ready,node_ready", [(True, True), (False, True), (True, False)]
)
def test_accessibility_tap_is_explicit_and_never_falls_back_to_pointer(
    driver_ready, node_ready
):
    calls = []

    def semantic(op, args, **kwargs):
        calls.append((op, args))
        return {"status": "injection_call_returned", "accepted": True}

    def pointer(*args, **kwargs):
        pytest.fail("An explicit accessibility action must never inject a pointer")

    session = MobileSession.__new__(MobileSession)
    session.closed = False
    session.profile = {"platform": "android"}
    session.device = SimpleNamespace(
        input=SimpleNamespace(request=pointer),
        semantic=SimpleNamespace(
            request=semantic,
            capabilities={"tap_backends": ["accessibility"] if driver_ready else []},
        ),
    )
    observation = {
        "complete": True,
        "match_count": 1,
        "state_version": 1,
        "display": {"id": 0, "width": 400, "height": 800, "rotation": 0},
        "candidates": [
            {
                "ref": "fresh",
                "bounds": [10, 10, 80, 80],
                "supports_accessibility_tap": node_ready,
            }
        ],
    }
    step = {"op": "tap", "tap_backend": "accessibility"}
    if driver_ready and node_ready:
        session.action(step, observation, "semantic-test", 1)
        assert calls[0][0] == "tap" and calls[0][1]["tap_backend"] == "accessibility"
        assert calls[0][1]["ref"] == "fresh"
    else:
        with pytest.raises(ValueError, match="advertise accessibility tap"):
            session.action(step, observation, "unsupported-test", 1)
        assert not calls
