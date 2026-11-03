import hashlib
import plistlib
import zipfile
from typing import ClassVar

import pytest

from iatest.core.config import init_project
from iatest.engines.store import EngineStore
from iatest.execution.control import RunControl
from iatest.mobile.delivery import fetch_app, prepare_app, safe_ipa
from iatest.mobile.lease import DeviceLease, device_scope
from iatest.mobile.store import MobileStore


@pytest.fixture
def root(tmp_path):
    init_project(tmp_path, "suite")
    return tmp_path


def profile():
    return {
        "id": "phone",
        "platform": "android",
        "device_id": "test-delivery-device",
        "app_id": "dev.example.app",
    }


def inspector(path, platform):
    return {
        "app_id": "dev.example.app",
        "version": "2",
        "version_code": "2",
        "certificate_sha256": "a" * 64,
    }


def artifact(root, app_id="build2", content=b"controlled-apk"):
    path = root / (app_id + ".apk")
    path.write_bytes(content)
    return fetch_app(
        root,
        "suite",
        {
            "id": app_id,
            "platform": "android",
            "app_id": "dev.example.app",
            "path": str(path),
        },
        inspector=inspector,
    )


def test_identity_hash_and_cache_are_verified_before_install(root):
    path = root / "app.apk"
    path.write_bytes(b"app")
    spec = {
        "platform": "android",
        "app_id": "dev.example.app",
        "path": str(path),
        "sha256": "0" * 64,
    }
    with pytest.raises(ValueError, match="SHA"):
        fetch_app(root, "suite", spec, inspector=inspector)
    spec["sha256"] = hashlib.sha256(b"app").hexdigest()
    saved = fetch_app(root, "suite", spec, inspector=inspector)
    assert saved["metadata"]["app_id"] == profile()["app_id"]
    spec["app_id"] = "dev.other.app"
    with pytest.raises(ValueError, match="identity"):
        fetch_app(root, "suite", spec, inspector=inspector)


class Installer:
    calls: ClassVar[list] = []
    fail_upgrade = False

    def __init__(self, profile, control):
        self.profile = profile

    def preflight(self, artifact, path):
        self.calls.append("verify:" + artifact["id"])

    def install(self, artifact, path, mode):
        self.calls.append(mode + ":" + artifact["id"])
        if mode == "upgrade" and self.fail_upgrade:
            raise RuntimeError("signature mismatch")
        return {"artifact_id": artifact["id"], "mode": mode}

    def launch(self):
        self.calls.append("launch")
        return {"launch": "acknowledged"}


def test_upgrade_failure_never_falls_back_to_fresh(root):
    MobileStore(root, "suite").save_profile(profile())
    artifact(root)
    Installer.calls, Installer.fail_upgrade = [], True
    result = prepare_app(
        root,
        "suite",
        {"profile": "phone", "artifact_id": "build2", "mode": "upgrade"},
        RunControl(timeout_seconds=5),
        installer_factory=Installer,
    )
    assert result["state"] == "environment_error"
    assert not any(c.startswith("fresh") for c in Installer.calls)
    assert (
        EngineStore(root, "suite").get_artifact("deployment", result["id"])["state"]
        == "environment_error"
    )
    Installer.fail_upgrade = False


def test_upgrade_workflow_installs_old_seeds_data_then_updates(root):
    MobileStore(root, "suite").save_profile(profile())
    artifact(root, "old", b"old")
    artifact(root, "new", b"new")
    EngineStore(root, "suite").save_plan(
        {
            "id": "seed",
            "kind": "api",
            "spec": {
                "allowed_origins": ["http://localhost:8000"],
                "requests": [{"url": "http://localhost:8000/seed"}],
            },
        }
    )
    Installer.calls = []

    def seed(*args, **kwargs):
        Installer.calls.append("seed")
        return {"run_id": "seed-result", "verdict": "passed"}

    result = prepare_app(
        root,
        "suite",
        {
            "profile": "phone",
            "artifact_id": "new",
            "mode": "upgrade",
            "base_artifact_id": "old",
            "seed_plan_id": "seed",
        },
        RunControl(timeout_seconds=5),
        installer_factory=Installer,
        executor=seed,
    )
    assert result["state"] == "prepared"
    assert (
        Installer.calls.index("fresh:old")
        < Installer.calls.index("seed")
        < Installer.calls.index("upgrade:new")
    )


def test_device_scope_is_borrowed_by_driver_without_unlocking_owner():
    p = profile()
    with device_scope(p):
        borrowed = DeviceLease(p["platform"], p["device_id"])
        borrowed.acquire()
        assert borrowed.handle is None
        borrowed.close()
    owner = DeviceLease(p["platform"], p["device_id"])
    owner.acquire()
    owner.close()


def test_ipa_paths_and_manifest_are_bounded(root):
    path = root / "app.ipa"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "Payload/Test.app/Info.plist",
            plistlib.dumps({"CFBundleIdentifier": "dev.example.app"}),
        )
        archive.writestr("../outside", "bad")
    with pytest.raises(ValueError, match="unsafe"):
        safe_ipa(path)


def test_mobile_plan_preparation_keeps_device_lease_through_driver_execution(
    root, monkeypatch
):
    from iatest.engines.contracts import EngineContext, EngineResult, ExecutionPlan
    from iatest.engines.registry import create_engine_registry
    from iatest.mobile.lease import HELD_DEVICES
    from iatest.mobile.runtime import MobileEngine

    MobileStore(root, "suite").save_profile(profile())
    prepared = []

    def prepare(actual_root, project, spec, control):
        assert actual_root == root and project == "suite"
        prepared.append(spec["mode"])
        return {"state": "prepared", "artifact_id": spec["artifact_id"]}

    def execute(engine, plan, context):
        assert ("android", profile()["device_id"]) in HELD_DEVICES.get()
        return EngineResult(plan.id, "android", verdict="passed")

    monkeypatch.setattr("iatest.mobile.delivery.prepare_app", prepare)
    monkeypatch.setattr(MobileEngine, "execute", execute)
    plan = ExecutionPlan.from_dict(
        {
            "id": "prepared-mobile",
            "kind": "android",
            "preparation": {
                "app": {
                    "profile": "phone",
                    "artifact_id": "fixture-build",
                    "mode": "upgrade",
                }
            },
            "spec": {
                "profile": "phone",
                "steps": [
                    {
                        "op": "expect",
                        "target": {"text": "Ready"},
                        "attribute": "exists",
                        "expected": True,
                    }
                ],
            },
        }
    )
    result = create_engine_registry(root, "suite").execute(
        plan, EngineContext(RunControl(timeout_seconds=5))
    )
    assert prepared == ["upgrade"] and result.verdict == "passed"
    assert result.metrics["preparation"]["state"] == "prepared"


@pytest.mark.parametrize("listing", ["", "package:dev.example.app.other\n"])
def test_android_absent_package_does_not_use_nonzero_pm_path(listing):
    from iatest.mobile.delivery import AppInstaller

    installer = AppInstaller(profile(), RunControl(timeout_seconds=5))
    calls = []

    def adb(*args):
        calls.append(args)
        return listing

    installer.adb = adb
    assert installer.installed() is None
    assert calls == [("shell", "pm", "list", "packages", "dev.example.app")]


def test_android_package_service_error_is_not_absence():
    from iatest.mobile.delivery import AppInstaller

    installer = AppInstaller(profile(), RunControl(timeout_seconds=5))
    installer.adb = lambda *args: "Error: package service unavailable"
    with pytest.raises(ValueError, match="list cannot be verified"):
        installer.installed()


def test_android_installed_package_requires_exact_identity_and_version():
    from iatest.mobile.delivery import AppInstaller

    installer = AppInstaller(profile(), RunControl(timeout_seconds=5))
    installer.adb = lambda *args: (
        "package:dev.example.app\n"
        if args[2] == "list"
        else "versionCode=2 minSdk=26\nversionName=2.0\n"
    )
    assert installer.installed() == {
        "app_id": "dev.example.app", "version_code": "2", "version": "2.0"
    }
