from urllib.parse import urlencode, urlparse

import pytest

from iatest.application.asset_catalog import catalog_assets, filter_assets
from iatest.core.config import init_project
from iatest.core.paths import project_docs_file
from iatest.core.serde import write_json
from iatest.engines.store import EngineStore
from iatest.mobile.store import MobileStore
from iatest.web.goal_workspace import exploration_payload
from iatest.web.i18n import set_current_locale
from iatest.web.server import route_get
from iatest.web.testing import build_plan, handle_testing_action


@pytest.fixture
def root(tmp_path):
    init_project(tmp_path, "suite")
    set_current_locale("zh")
    yield tmp_path
    set_current_locale("zh")


def fields(**values):
    return {k: [str(v)] for k, v in values.items()}


def ui_plan(identifier="journey", kind="web"):
    spec = (
        {
            "start_url": "https://test.example",
            "allowed_origins": ["https://test.example"],
            "steps": [
                {
                    "op": "expect",
                    "target": {"selector": "h1"},
                    "assert": "text",
                    "expected": "Saved",
                }
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
    return {"id": identifier, "kind": kind, "spec": spec}


def seed(root):
    engine = EngineStore(root, "suite")
    engine.save_plan(ui_plan())
    engine.save_plan(
        build_plan(
            engine,
            fields(
                id="load",
                kind="performance",
                url="https://test.example",
                origins="https://test.example",
                mode="users",
                duration="1",
                users="1",
                rate="1",
                p95="1000",
                error_ratio="0",
            ),
        )
    )
    engine.save_plan(
        build_plan(
            engine,
            fields(
                id="security",
                kind="security",
                url="https://test.example",
                origins="https://test.example",
                rule="response_headers",
                headers='{"x-content-type-options":"nosniff"}',
            ),
        )
    )
    engine.save_artifact(
        "ui_case",
        {
            "kind": "web",
            "goal": "保存后应显示 Saved",
            "status": "reproducible_passed",
            "plan": ui_plan(),
            "intent": {
                "checks": [{"expected": "Saved", "source_quote": "保存后应显示 Saved"}]
            },
            "verification": [{}, {}],
        },
        artifact_id="goal",
    )
    engine.save_artifact(
        "ui_case",
        {
            "kind": "web",
            "goal": "待补充条件的候选",
            "status": "blocked",
            "plan": None,
            "intent": None,
            "error": "Missing expected outcome",
        },
        artifact_id="blocked",
    )
    engine.save_artifact(
        "security_library",
        {"version": "1", "rules": [{"id": "header-rule", "title": "响应头规则"}]},
        artifact_id="rules-v1",
    )
    engine.save_artifact(
        "ios_measurement",
        {"measurements": {"ios_launch_ms": 5}, "trust": "caller-declared"},
        artifact_id="measurement",
    )
    engine.save_artifact(
        "protobuf", {"descriptor": {"sha256": "a" * 64}}, artifact_id="schema"
    )
    engine.save_artifact(
        "analysis",
        {"summary": "analysis is a report, not an asset"},
        artifact_id="report-only",
    )
    write_json(
        project_docs_file(root, "suite"),
        [{"method": "GET", "path": "/orders", "summary": "订单契约"}],
    )
    return engine


def test_all_test_types_and_reusable_artifacts_share_one_catalog(root):
    seed(root)
    assets = catalog_assets(root, "suite")
    by_key = {a["key"]: a for a in assets}
    assert by_key["engine-plan:journey"]["category"] == "ui"
    assert by_key["engine-plan:load"]["category"] == "performance"
    assert by_key["engine-plan:security"]["category"] == "security"
    assert by_key["artifact-security_library:rules-v1"]["role"] == "rule_library"
    assert by_key["artifact-ios_measurement:measurement"]["category"] == "performance"
    assert "artifact-analysis:report-only" not in by_key
    assert by_key["artifact-ui_case:blocked"]["state"] == "blocked"
    html = route_get(root, "suite", urlparse("/assets"))
    assert all(
        'data-asset-category="' + category + '"' in html
        for category in [
            "ui",
            "api",
            "performance",
            "security",
            "data",
            "accessibility",
            "automation",
        ]
    )
    assert "engine-plan:load" in html and "artifact-security_library:rules-v1" in html
    assert (
        "/interfaces?"
        not in html.split("workspace-local-tabs", 1)[1].split("</nav>", 1)[0]
    )


def test_asset_details_link_case_and_plan_without_executing(root, monkeypatch):
    seed(root)
    from iatest.web import server

    monkeypatch.setattr(
        server,
        "start_task",
        lambda *a, **kw: pytest.fail("GET must not start execution"),
    )
    for identity in [
        "artifact-ui_case:goal",
        "engine-plan:journey",
        "artifact-ui_case:blocked",
    ]:
        html = route_get(
            root, "suite", urlparse("/assets?" + urlencode({"asset": identity}))
        )
        assert "高级：完整资产内容" in html
        if identity.endswith(":blocked"):
            assert "/actions/testing-run" not in html
            assert "待补充条件" in html and "Missing expected outcome" in html
        else:
            assert "关联资产" in html and "/actions/testing-run" in html
    assert not EngineStore(root, "suite").runs()
    init_project(root, "other")
    other = route_get(root, "other", urlparse("/assets?asset=engine-plan:journey"))
    assert "此项目中没有找到该资产" in other


def test_filters_pagination_and_old_plan_view_keep_types_separate(root):
    engine = seed(root)
    for i in range(205):
        engine.save_artifact(
            "ui_case",
            {
                "kind": "web",
                "goal": f"UI fixture {i}",
                "status": "blocked",
                "plan": None,
            },
            artifact_id=f"old-goal-{i}",
        )
    ui = filter_assets(catalog_assets(root, "suite"), category="ui", role="goal_case")
    assert len(ui) == 207
    html = route_get(
        root, "suite", urlparse("/assets?category=ui&role=goal_case&page=11")
    )
    assert "共 207 项" in html and "11 / 11" in html
    assert html.count("<tr data-asset-key=") == 7
    perf = route_get(root, "suite", urlparse("/assets?category=performance&q=load"))
    assert "engine-plan:load" in perf and "engine-plan:security" not in perf
    plans = route_get(root, "suite", urlparse("/assets?view=plans"))
    assert "engine-plan:load" in plans and "artifact-ui_case:goal" not in plans


def test_engine_and_mobile_plans_with_the_same_id_have_distinct_asset_links(root):
    engine = EngineStore(root, "suite")
    engine.save_plan(ui_plan("same"))
    mobile = MobileStore(root, "suite")
    mobile.save_profile(
        {
            "id": "phone",
            "platform": "android",
            "device_id": "test-phone",
            "app_id": "com.example.lab",
        }
    )
    mobile.save_plan(ui_plan("same", "android"))
    assets = catalog_assets(root, "suite")
    assert {a["key"] for a in assets} == {"engine-plan:same", "mobile-plan:same"}
    mobile_html = route_get(root, "suite", urlparse("/assets?asset=mobile-plan:same"))
    assert (
        "/actions/mobile-run" in mobile_html
        and "/actions/testing-run" not in mobile_html
    )


@pytest.mark.parametrize(
    "url",
    [
        "/testing?group=interaction",
        "/testing?tab=exploration",
        "/testing?tab=web",
        "/testing?tab=desktop",
        "/mobile?tab=plans",
    ],
)
def test_ui_entries_default_to_goal_exploration_not_manual_json(root, url):
    html = route_get(root, "suite", urlparse(url))
    assert "data-goal-workspace" in html
    assert "/actions/testing-explore" in html and 'name="expected_result"' in html
    assert "独立回放" in html and "method=manual" in html
    assert (
        "/actions/testing-build" not in html
        and "/actions/mobile-build-plan" not in html
    )
    assert '<textarea name="content"' not in html


def test_goal_form_compiles_browser_scope_and_trusted_outcome(root, monkeypatch):
    from iatest.web import server

    tasks = []
    monkeypatch.setattr(server, "start_task", lambda *a, **kw: tasks.append(kw) or True)

    class Handler:
        def redirect(self, location):
            self.location = location

    handler = Handler()
    handle_testing_action(
        root,
        "suite",
        "/actions/testing-explore",
        fields(
            kind="web",
            guided_goal="1",
            url="https://test.example/start",
            goal="填写昵称并保存",
            expected_result="刷新后仍显示 Saved",
            origins="",
            token_budget="12000",
        ),
        handler,
    )
    payload = tasks[0]["payload"]
    assert payload["context"]["allowed_origins"] == ["https://test.example:443"]
    assert payload["context"]["rules"]["expected_result"] == "刷新后仍显示 Saved"
    assert payload["context"]["token_budget"] == 12000
    assert "Saved" in payload["goal"]
    assert "platform=web" in handler.location
    assert not EngineStore(root, "suite").plans()


@pytest.mark.parametrize(
    "kind,values",
    [
        (
            "desktop",
            {"platform": "macos", "process_id": "123", "app_id": "com.example.lab"},
        ),
        ("android", {"profile": "android"}),
        ("ios", {"profile": "ios"}),
    ],
)
def test_native_goal_context_can_be_completed_without_json_or_device_io(
    root, kind, values
):
    mobile = MobileStore(root, "suite")
    for platform in ["android", "ios"]:
        mobile.save_profile(
            {
                "id": platform,
                "platform": platform,
                "device_id": "test-" + platform,
                "app_id": "com.example.lab",
            }
        )
    payload = exploration_payload(
        root,
        "suite",
        fields(
            kind=kind,
            goal="保存昵称",
            expected_result="Saved",
            guided_goal="1",
            **values,
        ),
    )
    assert payload["kind"] == kind
    assert payload["context"]["rules"]["expected_result"] == "Saved"
    assert payload["context"].get("process_id", 123) == 123
    assert not mobile.runs()


def test_invalid_goal_never_starts_a_background_task(root, monkeypatch):
    from iatest.web import server

    monkeypatch.setattr(
        server,
        "start_task",
        lambda *a, **kw: pytest.fail("invalid input must not queue a task"),
    )
    with pytest.raises(ValueError, match="通过条件"):
        handle_testing_action(
            root,
            "suite",
            "/actions/testing-explore",
            fields(
                kind="web", guided_goal="1", url="https://test.example", goal="保存"
            ),
            object(),
        )


def test_goal_contract_is_identical_across_request_and_worker_locales(root):
    data = fields(
        kind="web",
        url="https://test.example",
        goal="保存昵称",
        expected_result="仍显示 Saved",
        origins="",
    )
    set_current_locale("en")
    english = exploration_payload(root, "suite", data)
    set_current_locale("zh")
    chinese = exploration_payload(root, "suite", data)
    assert english == chinese


def test_historical_verified_case_does_not_run_a_changed_plan_as_if_verified(root):
    engine = seed(root)
    changed = ui_plan()
    changed["spec"]["steps"][0]["expected"] = "Changed"
    engine.save_plan(changed)
    html = route_get(root, "suite", urlparse("/assets?asset=artifact-ui_case:goal"))
    assert "当前保存的计划已变更或不存在" in html
    assert "/actions/testing-run" not in html
