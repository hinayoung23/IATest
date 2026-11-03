import pytest

from iatest.core.config import init_project
from iatest.nodes.store import (
    ControllerStore,
    engine_fingerprint,
    environment_fingerprint,
)


def test_global_resource_claim_survives_disconnect_until_explicit_reconciliation(
    tmp_path,
):
    init_project(tmp_path, "suite")
    store = ControllerStore(tmp_path, "suite")
    for node in ("a", "b"):
        store.enroll(
            {
                "id": node,
                "token_env": "TOKEN_" + node,
                "allowed_origins": ["http://localhost"],
                "allowed_kinds": ["api"],
                "resources": ["device:shared-lab-phone"],
            }
        )
        store.register(
            node,
            "agent-" + node,
            {
                "protocol": 1,
                "engine_fingerprint": engine_fingerprint(),
                "environment_fingerprint": environment_fingerprint(tmp_path),
                "kinds": ["api"],
            },
        )
    plan = {
        "id": "plan",
        "kind": "api",
        "spec": {
            "allowed_origins": ["http://localhost"],
            "requests": [{"url": "http://localhost"}],
        },
    }
    job = store.enqueue("run", "a", plan)
    with pytest.raises(ValueError, match="resource"):
        store.enqueue("run", "b", plan)
    store.poll("a", "agent-a")
    store.ready(job, store.job(job)["proof"])
    store.start([job])
    store.cancel([job])
    assert store.claims()[0]["state"] == "uncertain"
    with pytest.raises(ValueError, match="resource"):
        store.enqueue("run", "b", plan)
    with pytest.raises(ValueError, match="acknowledgement"):
        store.reconcile(job, "External process stopped and side effect checked")
    store.reconcile(
        job, "External process stopped and side effect checked", stopped=True
    )
    assert store.claims() == []
    assert store.enqueue("run", "b", plan)


def test_pre_dispatch_cancel_releases_claim_without_replaying_or_waiting(tmp_path):
    init_project(tmp_path, "suite")
    store = ControllerStore(tmp_path, "suite")
    store.enroll(
        {
            "id": "a",
            "token_env": "TOKEN_A",
            "allowed_origins": ["http://localhost"],
            "allowed_kinds": ["api"],
            "resources": ["account:pool"],
        }
    )
    store.register(
        "a",
        "agent-a",
        {
            "protocol": 1,
            "engine_fingerprint": engine_fingerprint(),
            "environment_fingerprint": environment_fingerprint(tmp_path),
            "kinds": ["api"],
        },
    )
    plan = {
        "id": "plan",
        "kind": "api",
        "spec": {
            "allowed_origins": ["http://localhost"],
            "requests": [{"url": "http://localhost"}],
        },
    }
    job = store.enqueue("run", "a", plan)
    store.cancel([job])
    assert store.claims() == []
