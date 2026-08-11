from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

import pytest

from ai_apitest.core.config import init_project
from ai_apitest.web.i18n import (
    locale_from_request,
    localize_html,
    missing_translation_keys,
    normalize_locale,
    set_current_locale,
)
from ai_apitest.web.server import (
    render_agent_runtime_config,
    render_conflict_pair,
    render_lesson_cards,
    render_mock_config,
    render_quality_audit_config,
    render_report_detail,
    render_similar_cards,
    render_trend_overview,
    route_get,
)


@pytest.fixture
def web_root(tmp_path: Path) -> Path:
    init_project(tmp_path, "demo")
    return tmp_path


def test_request_locale_defaults_to_english_and_supports_aliases() -> None:
    assert locale_from_request() == "en"
    assert locale_from_request(cookie_header="iatest_lang=fr") == "fr"
    assert locale_from_request("lang=ja", "iatest_lang=ru") == "ja"
    assert normalize_locale("zh-CN") == "zh"
    assert normalize_locale("ru_RU") == "ru"
    assert normalize_locale("unsupported") == "en"


def test_non_english_language_catalogs_cover_all_fixed_copy() -> None:
    assert missing_translation_keys("ja") == []
    assert missing_translation_keys("fr") == []
    assert missing_translation_keys("ru") == []


@pytest.mark.parametrize(
    ("locale", "html_lang", "dashboard", "settings"),
    [
        ("en", "en", "Dashboard", "Settings"),
        ("zh", "zh-CN", "仪表盘", "配置中心"),
        ("ja", "ja", "ダッシュボード", "設定"),
        ("fr", "fr", "Tableau de bord", "Paramètres"),
        ("ru", "ru", "Панель управления", "Настройки"),
    ],
)
def test_main_pages_render_in_selected_locale(
    web_root: Path,
    locale: str,
    html_lang: str,
    dashboard: str,
    settings: str,
) -> None:
    try:
        set_current_locale(locale)
        dashboard_html = route_get(web_root, "demo", urlparse("/?project=demo"))
        settings_html = route_get(web_root, "demo", urlparse("/config?project=demo"))
        assert f'<html lang="{html_lang}" data-locale="{locale}">' in dashboard_html
        assert f'<option value="{locale}" selected>' in dashboard_html
        assert f"<h1>{dashboard}</h1>" in dashboard_html
        assert f"<h1>{settings}</h1>" in settings_html
    finally:
        set_current_locale("zh")


def test_english_pages_do_not_leave_fixed_chinese_copy(web_root: Path) -> None:
    try:
        set_current_locale("en")
        content = "\n".join(
            route_get(web_root, "demo", urlparse(f"{path}?project=demo"))
            for path in ("/", "/interfaces", "/cases", "/reports", "/context", "/config")
        )
        content = content.replace("中文", "").replace("日本語", "")
        assert re.search(r"[\u3400-\u9fff]", content) is None
    finally:
        set_current_locale("zh")


def test_report_content_is_not_translated(web_root: Path) -> None:
    report = web_root / "projects" / "demo" / "reports" / "report-20260810-120000.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("# 原始中文报告\n\n用户生成内容保持原样。", encoding="utf-8")
    try:
        set_current_locale("fr")
        content = render_report_detail(web_root, "demo", report)
        assert "原始中文报告" in content
        assert 'class="markdown-report" data-i18n-skip' in content
        assert "Détails du rapport" in content
    finally:
        set_current_locale("zh")


def test_language_switch_and_responsive_rules_are_shipped() -> None:
    web_dir = Path(__file__).parents[1] / "ai_apitest" / "web"
    script = (web_dir / "static" / "app.js").read_text(encoding="utf-8")
    styles = (web_dir / "static" / "app.css").read_text(encoding="utf-8")
    template = (web_dir / "templates" / "base.html").read_text(encoding="utf-8")

    assert "data-language-select" in template
    assert "iatest_lang=" in script
    for locale in ("en", "zh", "ja", "fr", "ru"):
        assert re.search(rf"\b{locale}: \{{", script)
    assert "@media (max-width: 820px)" in styles
    assert "@media (max-width: 560px)" in styles
    assert 'html[lang="fr"]' in styles
    assert ".language-switch" in styles


@pytest.mark.parametrize(
    ("locale", "role", "status", "decision", "details"),
    [
        ("en", "Quality review", "Completed", "Review passed", "View details"),
        ("ja", "品質監査", "完了", "監査合格", "詳細を見る"),
        ("fr", "Audit qualité", "Terminé", "Audit réussi", "Voir les détails"),
        ("ru", "Аудит качества", "Завершено", "Аудит пройден", "Подробнее"),
    ],
)
def test_runtime_agent_and_quality_values_are_localized(
    locale: str,
    role: str,
    status: str,
    decision: str,
    details: str,
) -> None:
    try:
        set_current_locale(locale)
        agent_html = render_agent_runtime_config(
            {},
            [{"role_id": "quality_reviewer", "role_version": "1", "status": "completed"}],
        )
        quality_html = render_quality_audit_config(
            "demo",
            {},
            [{"id": "audit-1", "artifact_type": "test_cases", "decision": "accepted"}],
        )
        assert role in agent_html
        assert status in agent_html
        assert decision in quality_html
        assert f">{details}</a>" in quality_html
    finally:
        set_current_locale("zh")


@pytest.mark.parametrize(
    ("locale", "stopped", "configured"),
    [
        ("en", "Stopped", "Configured routes"),
        ("ja", "停止中", "設定済みルート"),
        ("fr", "Arrêté", "Routes configurées"),
        ("ru", "Остановлен", "Настроенные маршруты"),
    ],
)
def test_mock_runtime_status_is_localized(
    web_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    locale: str,
    stopped: str,
    configured: str,
) -> None:
    monkeypatch.setattr(
        "ai_apitest.web.server.web_mock_status",
        lambda _root: {"running": False},
    )
    try:
        set_current_locale(locale)
        content = render_mock_config(web_root, "demo", {"routes": []})
        assert f"<strong>{stopped}</strong>" in content
        assert configured in content
    finally:
        set_current_locale("zh")


@pytest.mark.parametrize(
    ("locale", "confirm", "ignore", "tag", "keep", "merge"),
    [
        ("en", "Confirm", "Ignore", "Happy path", "Keep A", "Merge into A"),
        ("ja", "確認", "無視", "正常系", "A を保持", "A に統合"),
        ("fr", "Confirmer", "Ignorer", "Scénario nominal", "Conserver A", "Fusionner dans A"),
        ("ru", "Подтвердить", "Игнорировать", "Позитивный сценарий", "Оставить A", "Объединить с A"),
    ],
)
def test_knowledge_actions_and_tags_are_localized(
    locale: str,
    confirm: str,
    ignore: str,
    tag: str,
    keep: str,
    merge: str,
) -> None:
    lesson = {
        "id": "lesson-1",
        "scope": "endpoint",
        "lesson_type": "business_rule",
        "lifecycle_status": "pending",
        "review_status": "unreviewed",
        "confidence": "high",
        "title": "Rule",
        "content": "Content",
        "method": "GET",
        "path": "/users",
        "tags": ["happy_path"],
        "occurrence_count": 2,
    }
    pair = {
        "relation_id": "rel-1",
        "a_title": "A",
        "a_content": "A content",
        "b_title": "B",
        "b_content": "B content",
    }
    try:
        set_current_locale(locale)
        lesson_html = render_lesson_cards("demo", [lesson])
        conflict_html = render_conflict_pair("demo", pair)
        similar_html = render_similar_cards("demo", [pair])
        assert f">{confirm}</button>" in lesson_html
        assert f">{ignore}</button>" in lesson_html
        assert tag in lesson_html
        assert f">{keep}</button>" in conflict_html
        assert f">{merge}</button>" in similar_html
    finally:
        set_current_locale("zh")


@pytest.mark.parametrize(
    ("locale", "heading", "trend", "new_failure"),
    [
        ("en", "Latest 2 runs", "Not directly comparable", "Newly failing APIs"),
        ("ja", "直近 2 回の実行", "直接比較不可", "新規失敗 API"),
        ("fr", "2 dernières exécutions", "Non directement comparable", "API nouvellement en échec"),
        ("ru", "Последние запуски: 2", "Нельзя сравнивать напрямую", "Новые ошибки API"),
    ],
)
def test_trend_runtime_findings_are_localized(
    locale: str,
    heading: str,
    trend: str,
    new_failure: str,
) -> None:
    latest = {
        "run_id": 2,
        "created_at": "2026-08-10",
        "total_cases": 3,
        "passed_cases": 1,
        "failed_cases": 2,
        "pass_rate": 1 / 3,
        "run_scope": "full",
        "endpoint_metrics": [
            {"method": "GET", "path": "/new", "passed": 0, "failed": 1},
            {"method": "GET", "path": "/persistent", "passed": 0, "failed": 1},
            {"method": "GET", "path": "/recovered", "passed": 1, "failed": 0},
        ],
    }
    previous = {
        "run_id": 1,
        "created_at": "2026-08-09",
        "total_cases": 3,
        "passed_cases": 1,
        "failed_cases": 2,
        "pass_rate": 1 / 3,
        "run_scope": "smoke",
        "endpoint_metrics": [
            {"method": "GET", "path": "/persistent", "passed": 0, "failed": 1},
            {"method": "GET", "path": "/recovered", "passed": 0, "failed": 1},
        ],
    }
    try:
        set_current_locale(locale)
        content = render_trend_overview({"runs": [latest, previous]})
        assert heading in content
        assert trend in content
        assert new_failure in content
        assert "本次 full vs 上次 smoke" not in content
        assert "新增失败接口" not in content
        assert "建议动作" not in content
        if locale == "en":
            assert re.search(r"[\u3400-\u9fff]", localize_html(content, "en")) is None
    finally:
        set_current_locale("zh")
