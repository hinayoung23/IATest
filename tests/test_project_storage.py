"""Migration, source-of-truth and crash recovery contracts for project assets."""

import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import pytest

from iatest.application.test_assets import (
    AssetRepository,
    commit_import,
    preview_import,
    template_bytes,
)
from iatest.core.config import init_project
from iatest.core.locking import project_operation_lock
from iatest.core.paths import project_cases_dir, project_docs_dir, project_reports_dir
from iatest.engines.contracts import EngineResult, ExecutionPlan
from iatest.engines.store import EngineStore
from iatest.mobile.store import MobileStore


def plan(name="journey", kind="web"):
    spec = (
        {
            "start_url": "https://test.example",
            "allowed_origins": ["https://test.example"],
            "steps": [
                {"op": "expect", "target": {"selector": "h1"}, "assert": "exists"}
            ],
        }
        if kind == "web"
        else {
            "profile": "phone",
            "steps": [
                {
                    "op": "expect",
                    "target": {"identifier": "status"},
                    "attribute": "text",
                    "expected": "Saved",
                }
            ],
        }
    )
    return {"id": name, "kind": kind, "spec": spec}


def profile():
    return {
        "id": "phone",
        "platform": "android",
        "device_id": "test-device",
        "app_id": "com.example.app",
    }


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def remove_cache(store):
    for suffix in ("", "-wal", "-shm"):
        Path(str(store.path) + suffix).unlink(missing_ok=True)


def test_every_plan_type_has_readable_project_files(tmp_path):
    examples = Path(__file__).resolve().parents[1] / "examples/plans"
    store = EngineStore(tmp_path, "suite")
    for filename in (
        "web.json",
        "http-contract.json",
        "android-login.json",
        "performance.json",
        "security.json",
        "data-quality.json",
        "grpc-interactive.json",
        "accessibility.json",
        "workflow.json",
        "ios-launch-performance.json",
    ):
        value = json.loads((examples / filename).read_text())
        store.save_plan(value)
        directory = (
            "performance/client"
            if value["kind"] == "client_performance"
            else value["kind"]
        )
        path = (
            tmp_path
            / "projects/suite/assets"
            / directory
            / "plans"
            / (value["id"] + ".json")
        )
        assert json.loads(path.read_text())["spec"] == value["spec"]
    remove_cache(store)
    assert len(store.plans()) == 10
    other = EngineStore(tmp_path, "other")
    other.save_plan(plan("web-login"))
    assert other.get_plan("web-login") != store.get_plan("web-login")


def test_files_are_authoritative_after_edit_delete_and_index_rebuild(tmp_path):
    store = EngineStore(tmp_path, "suite")
    store.save_plan(plan())
    path = tmp_path / "projects/suite/assets/web/plans/journey.json"
    value = json.loads(path.read_text())
    value["title"] = "人工修改后的计划"
    write_json(path, value)
    assert store.get_plan("journey")["title"] == value["title"]
    remove_cache(store)
    assert store.get_plan("journey")["title"] == value["title"]
    path.unlink()
    assert store.plans() == []
    remove_cache(store)
    assert store.plans() == []


def test_invalid_manual_plan_does_not_fall_back_to_cached_plan(tmp_path):
    store = EngineStore(tmp_path, "suite")
    store.save_plan(plan())
    path = tmp_path / "projects/suite/assets/web/plans/journey.json"
    write_json(path, {"id": "journey", "kind": "web", "spec": {}})
    with pytest.raises(ValueError):
        store.get_plan("journey")


def test_unregistered_cache_rows_cannot_recreate_missing_asset_files(tmp_path):
    store = EngineStore(tmp_path, "suite")
    store.save_plan(plan())
    # State possible after an import committed but the process exited before
    # registration, and the user subsequently deleted the authoritative file.
    with sqlite3.connect(store.path) as conn:
        conn.execute("delete from storage_files where table_name='plans'")
    path = tmp_path / "projects/suite/assets/web/plans/journey.json"
    path.unlink()
    assert store.plans() == []
    assert not path.exists()


def test_plan_type_changes_move_file_without_leaving_a_duplicate(tmp_path):
    store = EngineStore(tmp_path, "suite")
    store.save_plan(plan())
    store.save_plan(plan(kind="android"))
    assert not (tmp_path / "projects/suite/assets/web/plans/journey.json").exists()
    assert (tmp_path / "projects/suite/assets/android/plans/journey.json").is_file()
    remove_cache(store)
    assert store.get_plan("journey")["kind"] == "android"
    write_json(tmp_path / "projects/suite/assets/web/plans/journey.json", plan())
    with pytest.raises(ValueError, match="Duplicate asset"):
        store.plans()


def test_mobile_and_engine_ids_have_separate_files_and_can_rebuild(tmp_path):
    mobile, engine = MobileStore(tmp_path, "suite"), EngineStore(tmp_path, "suite")
    mobile.save_profile(profile())
    mobile.save_plan(plan(kind="android"))
    engine.save_plan(plan())
    assets = tmp_path / "projects/suite/assets"
    assert (assets / "android/profiles/phone.json").is_file()
    assert (assets / "android/plans/native/journey.json").is_file()
    assert (assets / "web/plans/journey.json").is_file()
    remove_cache(mobile)
    assert mobile.has_assets()
    assert mobile.get("profile", "phone") == profile()
    assert mobile.get("plan", "journey")["kind"] == "android"


def test_rules_goal_cases_and_version_history_are_readable_without_changing_payloads(
    tmp_path,
):
    init_project(tmp_path, "suite")
    store = EngineStore(tmp_path, "suite")
    store.save_artifact(
        "security_library", {"rules": [{"id": "policy"}]}, artifact_id="rules"
    )
    store.save_artifact(
        "ui_case", {"kind": "web", "goal": "保存名称"}, artifact_id="goal"
    )
    rules = store.get_artifact("security_library", "rules")
    with store.connection() as conn:
        created = conn.execute(
            "select created_at from artifacts where id='rules'"
        ).fetchone()[0]
    path = tmp_path / "projects/suite/assets/security/rules/rules.json"
    assert json.loads(path.read_text()) == rules
    assert (tmp_path / "projects/suite/assets/web/cases/goal/goal.json").is_file()
    for policy in ("error", "version"):
        preview = preview_import(
            tmp_path, "suite", template_bytes("json"), "cases.json", policy=policy
        )
        commit_import(tmp_path, "suite", preview["id"])
    versions = tmp_path / "projects/suite/assets/web/cases/nickname-persistence"
    assert sorted(p.name for p in versions.iterdir()) == [
        "v000001.json",
        "v000002.json",
    ]
    remove_cache(store)
    assert store.get_artifact("security_library", "rules") == rules
    with store.connection() as conn:
        assert (
            conn.execute(
                "select created_at from artifacts where id='rules'"
            ).fetchone()[0]
            == created
        )
    assert (
        AssetRepository(tmp_path, "suite").get("nickname-persistence")["version"] == 2
    )


def legacy_databases(tmp_path):
    base = tmp_path / "projects/suite"
    engine = base / ".iatest/testing/testing.sqlite3"
    mobile = base / ".iatest/mobile/mobile.sqlite3"
    engine.parent.mkdir(parents=True)
    mobile.parent.mkdir(parents=True)
    web = asdict(ExecutionPlan.from_dict(plan()))
    android = asdict(ExecutionPlan.from_dict(plan(kind="android")))
    with sqlite3.connect(engine) as conn:
        conn.executescript("""
            create table plans(id text primary key, kind text, content text not null);
            create table artifacts(kind text,id text,created_at text,content text not null,primary key(kind,id));
            create table runs(id text primary key,created_at text,content text not null);
            create table case_assets(case_id text,version integer,content text not null,primary key(case_id,version));
        """)
        conn.execute(
            "insert into plans values(?,?,?)", ("journey", "web", json.dumps(web))
        )
        conn.execute(
            "insert into artifacts values(?,?,?,?)",
            (
                "security_library",
                "rules",
                "2026-01-01",
                json.dumps({"id": "rules", "rules": []}),
            ),
        )
        conn.execute(
            "insert into runs values(?,?,?)",
            (
                "web-run",
                "2026-01-01",
                json.dumps(asdict(EngineResult("journey", "web", run_id="web-run"))),
            ),
        )
        conn.execute(
            "insert into case_assets values(?,?,?)",
            (
                "old-case",
                1,
                json.dumps(
                    {"case_id": "old-case", "version": 1, "platform": "desktop"}
                ),
            ),
        )
    with sqlite3.connect(mobile) as conn:
        conn.executescript("""
            create table assets(kind text,id text,content text not null,primary key(kind,id));
            create table runs(id text primary key,plan_id text,created_at text,content text not null);
        """)
        conn.execute(
            "insert into assets values(?,?,?)",
            ("profile", "phone", json.dumps(profile())),
        )
        conn.execute(
            "insert into assets values(?,?,?)", ("plan", "journey", json.dumps(android))
        )
        conn.execute(
            "insert into runs values(?,?,?,?)",
            (
                "mobile-run",
                "journey",
                "2026-01-02",
                json.dumps(
                    asdict(EngineResult("journey", "android", run_id="mobile-run"))
                ),
            ),
        )
    write_json(base / "api_docs/endpoints.json", [{"method": "GET", "path": "/health"}])
    (base / "test_cases").mkdir()
    (base / "test_cases/suite.md").write_text("# 接口用例", encoding="utf-8")
    (base / "reports").mkdir()
    (base / "reports/old-report.md").write_text("# 报告", encoding="utf-8")
    return base, engine, mobile


def test_legacy_migration_retains_originals_and_never_resurrects_deleted_assets(
    tmp_path,
):
    base, old_engine, old_mobile = legacy_databases(tmp_path)
    originals = {p: p.read_bytes() for p in base.rglob("*") if p.is_file()}
    engine, mobile = EngineStore(tmp_path, "suite"), MobileStore(tmp_path, "suite")
    assert engine.get_plan("journey")["kind"] == "web"
    assert engine.get_artifact("security_library", "rules")["rules"] == []
    assert AssetRepository(tmp_path, "suite").get("old-case")["platform"] == "desktop"
    assert mobile.get("profile", "phone") == profile()
    assert mobile.get("plan", "journey")["kind"] == "android"
    assert engine.runs()[0]["created_at"] == "2026-01-01"
    assert mobile.runs()[0]["created_at"] == "2026-01-02"
    assert (base / "reports/web/web-run/result.json").is_file()
    assert (base / "reports/android/mobile-run/native/result.json").is_file()
    assert (
        project_docs_dir(tmp_path, "suite") / "endpoints.json"
    ).read_bytes() == originals[base / "api_docs/endpoints.json"]
    assert (
        project_cases_dir(tmp_path, "suite") / "suite.md"
    ).read_bytes() == originals[base / "test_cases/suite.md"]
    assert (project_reports_dir(tmp_path, "suite") / "old-report.md").is_file()
    assert all(path.read_bytes() == content for path, content in originals.items())
    engine.delete_plan("journey")
    mobile.delete("profile", "phone")
    remove_cache(engine)
    remove_cache(mobile)
    assert engine.plans() == []
    assert mobile.list("profile") == []
    assert old_engine.is_file() and old_mobile.is_file()


def test_migration_conflict_preserves_both_versions_and_can_be_retried(tmp_path):
    base, old_engine, _ = legacy_databases(tmp_path)
    target = base / "assets/web/plans/journey.json"
    write_json(target, {**plan(), "title": "用户已有文件"})
    old_bytes, file_bytes = old_engine.read_bytes(), target.read_bytes()
    with pytest.raises(ValueError, match="migration conflict"):
        EngineStore(tmp_path, "suite").plans()
    assert old_engine.read_bytes() == old_bytes and target.read_bytes() == file_bytes
    target.rename(target.with_suffix(".conflict-backup"))
    assert EngineStore(tmp_path, "suite").get_plan("journey")["kind"] == "web"


@pytest.mark.parametrize("committed", [False, True])
def test_process_exit_during_multifile_transaction_recovers_consistently(
    tmp_path, committed
):
    store = EngineStore(tmp_path, "suite")
    store.save_plan(plan("first"))
    store.save_plan(plan("second"))
    script = r"""
import json, os, sys
from pathlib import Path
from iatest.data import project_storage as storage
from iatest.engines.store import EngineStore
store = EngineStore(Path(sys.argv[1]), "suite")
committed = sys.argv[2] == "True"
real_write = storage.atomic_write_bytes
real_remove = storage.shutil.rmtree
count = 0
def write(path, data):
    global count
    if "/assets/web/plans/" in path.as_posix():
        count += 1
        if count == 2 and not committed:
            os._exit(73)
    return real_write(path, data)
def remove(path, *args, **kwargs):
    if committed and "storage-transactions" in str(path):
        os._exit(73)
    return real_remove(path, *args, **kwargs)
storage.atomic_write_bytes = write
storage.shutil.rmtree = remove
with store.connection() as conn:
    for name in ("first", "second"):
        value = json.loads(conn.execute("select content from plans where id=?", (name,)).fetchone()[0])
        value["title"] = "committed change"
        conn.execute("update plans set content=? where id=?", (json.dumps(value), name))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), str(committed)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 73, result.stderr
    assert {p["title"] for p in store.plans()} == (
        {"committed change"} if committed else {""}
    )
    transactions = tmp_path / "projects/suite/.iatest/storage-transactions/testing"
    assert not list(transactions.iterdir())


def test_storage_does_not_block_reads_during_running_operations(tmp_path):
    store = EngineStore(tmp_path, "suite")
    store.save_plan(plan())
    with (
        ThreadPoolExecutor(max_workers=1) as pool,
        project_operation_lock(tmp_path, "suite", operation="long-run"),
    ):
        assert pool.submit(store.plans).result(timeout=5)[0]["id"] == "journey"


def test_concurrent_writes_do_not_lose_assets(tmp_path):
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(
                EngineStore(tmp_path, "suite").save_plan, plan("case-" + str(i))
            )
            for i in range(12)
        ]
        for future in futures:
            future.result(timeout=20)
    assert len(EngineStore(tmp_path, "suite").plans()) == 12


def test_new_evidence_is_typed_and_legacy_evidence_remains_readable(tmp_path):
    store = EngineStore(tmp_path, "suite")
    name = "a" * 32 + ".png"
    path = store.evidence_path("web-run", name, kind="web")
    assert path == tmp_path / "projects/suite/reports/web/web-run/evidence" / name
    path.parent.mkdir(parents=True)
    path.write_bytes(b"new evidence")
    assert store.evidence_path("web-run", name).read_bytes() == b"new evidence"
    legacy = store.directory / "evidence/old-run" / name
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"old evidence")
    assert store.evidence_path("old-run", name) == legacy
    store.save_result(EngineResult("journey", "web", run_id="web-run"))
    assert (path.parent.parent / "result.json").is_file()


def test_device_diagnostics_and_build_state_do_not_become_test_assets(tmp_path):
    store = MobileStore(tmp_path, "suite")
    doctor = {
        "devices": [{"platform": "android", "id": "test-device"}],
        "checked_at": "2026-01-01",
    }
    build = {
        "platform": "android",
        "output": "completed",
        "artifacts": "native/build/android",
    }
    store._save("doctor", "latest", doctor)
    store._save("build", "latest", build)
    assert (
        tmp_path / "projects/suite/.iatest/state/mobile/doctor/latest.json"
    ).is_file()
    assert not list((tmp_path / "projects/suite/assets").rglob("latest.json"))
    remove_cache(store)
    assert store.list("doctor") == [doctor]
    assert store.list("build") == [build]


def test_readonly_mcp_supports_both_layouts_without_writing_or_reviving_old_files(
    tmp_path,
):
    from iatest.integrations.mcp_server import MCPServer

    base = tmp_path / "projects/suite"
    legacy = base / "api_docs/endpoints.json"
    write_json(legacy, [{"method": "GET", "path": "/old"}])
    server = MCPServer(tmp_path)
    assert server.contracts({"project": "suite"})["items"][0]["path"] == "/old"
    assert not (base / ".iatest").exists()
    current = project_docs_dir(tmp_path, "suite") / "endpoints.json"
    write_json(current, [{"method": "GET", "path": "/current"}])
    assert server.contracts({"project": "suite"})["items"][0]["path"] == "/current"
    current.unlink()
    assert server.contracts({"project": "suite"})["items"] == []


@pytest.mark.parametrize("component", ["project", "assets", "reports", ".iatest"])
def test_symlink_cannot_redirect_assets_to_another_project(tmp_path, component):
    base = tmp_path / "projects/suite"
    other = tmp_path / "projects/other"
    other.mkdir(parents=True)
    base.parent.mkdir(exist_ok=True)
    try:
        if component == "project":
            base.symlink_to(other, target_is_directory=True)
        else:
            base.mkdir()
            (base / component).symlink_to(other, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks require OS permission")
    with pytest.raises(ValueError):
        EngineStore(tmp_path, "suite").save_plan(plan())
    assert not list(other.iterdir())
