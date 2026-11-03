from types import SimpleNamespace

import pytest

from iatest.application.testing_service import execute_test_plan
from iatest.core.config import init_project
from iatest.engines.contracts import EngineResult
from iatest.mobile.emulator import BOUND_PROFILES, OWNED
from iatest.mobile.store import MobileStore


@pytest.fixture
def managed(tmp_path, monkeypatch):
    init_project(tmp_path, "suite")
    profile = {
        "id": "vm", "platform": "android", "device_id": "managed",
        "app_id": "dev.example.lab", "require_os_sandbox": True,
        "managed_emulator": {"sdk_root": "/sdk", "system_image": "android-36;google_apis;arm64-v8a", "artifact_id": "build"},
    }
    store = MobileStore(tmp_path, "suite")
    store.save_profile(profile)
    events = []

    class VM:
        def __init__(self, settings, control):
            self.settings, self.control = settings, control
            self.process = SimpleNamespace(poll=lambda: None)
            self.serial = "emulator-5678"
            self.evidence = {"device_id": self.serial, "name": "owned-vm"}

        def prepare_profile(self, original, root, project, *, install=True):
            events.append(("start", install))
            OWNED[self.serial] = self
            return {**original, "device_id": self.serial}

        def close(self):
            events.append(("close",))
            OWNED.pop(self.serial, None)
            self.evidence.update(process_stopped=True, userdata_removed=True)

    monkeypatch.setattr("iatest.mobile.emulator.AndroidEmulator", VM)
    yield tmp_path, store, events
    OWNED.pop("emulator-5678", None)
    assert not BOUND_PROFILES.get()


def journey():
    return {
        "id": "journey", "kind": "android",
        "spec": {"profile": "vm", "steps": [{"op": "expect", "target": {"text": "Ready"}, "attribute": "exists", "expected": True}]},
    }


def test_preparation_and_repeated_measurements_share_one_vm(managed, monkeypatch):
    root, store, events = managed

    def prepare(*args):
        assert store.get("profile", "vm")["device_id"] == "emulator-5678"
        events.append(("prepare",))
        return {"state": "prepared"}

    class Capture:
        def __init__(self, profile, *args, **kwargs):
            assert profile["device_id"] == "emulator-5678"
            self.values, self.notes = {}, []

        def before(self):
            assert "emulator-5678" in OWNED
            events.append(("before",))

        def after(self):
            assert "emulator-5678" in OWNED
            events.append(("after",))
            self.values = {"memory_pss_kb": 16}

    def execute(_engine, plan, _context):
        assert store.get("profile", "vm")["device_id"] == "emulator-5678"
        events.append(("ui",))
        return EngineResult(plan.id, "android", verdict="passed")

    monkeypatch.setattr("iatest.mobile.delivery.prepare_app", prepare)
    monkeypatch.setattr("iatest.mobile.runtime.MobileEngine.execute", execute)
    monkeypatch.setattr("iatest.engines.client_performance.AndroidCapture", Capture)
    result = execute_test_plan(root, "suite", {
        "id": "measured", "kind": "client_performance",
        "preparation": {"app": {"profile": "vm", "artifact_id": "build", "mode": "upgrade"}},
        "spec": {"platform": "android", "iterations": 2, "thresholds": {"max_memory_pss_kb": 32}, "plan": journey()},
    })
    assert result["verdict"] == "passed", result
    assert events == [("start", False), ("prepare",), ("before",), ("ui",), ("after",), ("before",), ("ui",), ("after",), ("close",)]
    assert result["metrics"]["os_sandbox"]["process_stopped"]
    assert store.get("profile", "vm")["device_id"] == "managed"


@pytest.mark.parametrize("failure", ["prepare", "ui", "cancel"])
def test_owned_vm_and_profile_scope_are_released_on_failure(managed, monkeypatch, failure):
    root, store, events = managed

    def prepare(*args):
        if failure == "prepare":
            raise RuntimeError("installation failed")
        return {"state": "prepared"}

    def execute(_engine, _plan, context):
        assert store.get("profile", "vm")["device_id"] == "emulator-5678"
        if failure == "cancel":
            context.control.cancelled.set()
            context.control.check()
        raise RuntimeError("UI failed")

    monkeypatch.setattr("iatest.mobile.delivery.prepare_app", prepare)
    monkeypatch.setattr("iatest.mobile.runtime.MobileEngine.execute", execute)
    plan = journey()
    plan["preparation"] = {"app": {"profile": "vm", "artifact_id": "build", "mode": "fresh"}}
    result = execute_test_plan(root, "suite", plan)
    assert result["verdict"] == "inconclusive"
    assert events == [("start", False), ("close",)]
    assert store.get("profile", "vm")["device_id"] == "managed"
    assert "emulator-5678" not in OWNED


def test_unauthorized_measured_journey_does_not_start_vm(managed):
    root, _store, events = managed
    plan = journey()
    plan["spec"]["steps"].insert(0, {"op": "clipboard_set", "value": "unapproved"})
    result = execute_test_plan(root, "suite", {
        "id": "unauthorized", "kind": "client_performance",
        "spec": {"platform": "android", "iterations": 1, "thresholds": {"max_memory_pss_kb": 32}, "plan": plan},
    })
    assert result["verdict"] == "inconclusive" and not events


def test_vm_read_budget_is_bounded_without_changing_the_phone_default():
    from iatest.mobile.runtime import MobileSession

    session = object.__new__(MobileSession)
    calls = []
    session.profile = {"platform": "android", "managed_emulator": {"system_image": "owned"}}
    session.device = SimpleNamespace(semantic=SimpleNamespace(request=lambda *a, **kw: calls.append((a, kw)) or {}))
    session.query({"identifier": "field"})
    assert calls[-1][1]["timeout"] == 2
    assert calls[-1][0][1]["snapshot_budget_ms"] == 1000
    session.query({"identifier": "field"}, timeout=0.3)
    assert calls[-1][0][1]["snapshot_budget_ms"] == 100
    session.profile = {"platform": "android"}
    session.query({"identifier": "field"})
    assert calls[-1][1]["timeout"] == 0.5
    assert "snapshot_budget_ms" not in calls[-1][0][1]
