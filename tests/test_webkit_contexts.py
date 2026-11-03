import json

import pytest

from iatest.mobile.webkit import Inspector


def inspector():
    value = Inspector.__new__(Inspector)
    value.target, value.routed, value.call_target = "page", True, None
    value.targets = {"page": {"type": "page"}}
    value.contexts, value.nodes = {}, {}
    return value


def test_frame_targets_do_not_change_page_identity_and_responses_stay_bound():
    value = inspector()
    value.receive(
        {
            "method": "Target.targetCreated",
            "params": {"targetInfo": {"targetId": "frame", "type": "frame"}},
        }
    )
    assert value.target == "page"
    packet = {
        "method": "Target.dispatchMessageFromTarget",
        "params": {
            "targetId": "frame",
            "message": json.dumps({"id": 1000002, "result": {"value": 1}}),
        },
    }
    with pytest.raises(ValueError, match="another target"):
        value.receive(packet)
    value.call_target = "frame"
    assert value.receive(packet)["id"] == 2
    packet["params"]["targetId"] = "unregistered"
    with pytest.raises(ValueError, match="another target"):
        value.receive(packet)


def test_closed_shadow_metadata_is_bounded_without_retaining_dom_text():
    value = inspector()
    value.remember(
        "page",
        {
            "nodeId": 1,
            "nodeValue": "untrusted page text",
            "shadowRoots": [{"nodeId": 2, "shadowRootType": "closed", "children": []}],
        },
    )
    assert value.nodes[("page", 1)]["shadowRoots"] == [2]
    assert "nodeValue" not in value.nodes[("page", 1)]
    value.observe(
        "page", {"method": "DOM.shadowRootPopped", "params": {"hostId": 1, "rootId": 2}}
    )
    assert value.nodes[("page", 1)]["shadowRoots"] == []
    with pytest.raises(ValueError, match="quota"):
        for index in range(3, 2052):
            value.remember("page", {"nodeId": index})


def test_deep_dom_metadata_reaches_its_quota_without_python_recursion():
    root = node = {"nodeId": 1}
    for index in range(2, 2200):
        child = {"nodeId": index}
        node["children"] = [child]
        node = child
    with pytest.raises(ValueError, match="quota"):
        inspector().remember("page", root)


def test_context_creation_is_target_scoped_and_unbound_page_swap_is_rejected():
    value = inspector()
    context = {"id": 7, "type": "normal", "frameId": "child"}
    value.observe(
        "frame",
        {"method": "Runtime.executionContextCreated", "params": {"context": context}},
    )
    assert value.contexts[("frame", 7)] == context
    with pytest.raises(ValueError, match="Unbound"):
        value.receive(
            {
                "method": "Target.didCommitProvisionalTarget",
                "params": {"oldTargetId": "page", "newTargetId": "unknown"},
            }
        )
