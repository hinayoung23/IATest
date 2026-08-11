from __future__ import annotations

import re
from pathlib import Path

import pytest

from ai_apitest import cli
from ai_apitest.ai.case_generation import system_prompt
from ai_apitest.ai.workflows import generate_cases_with_config, report_quality_gate_issues
from ai_apitest.context.storage import list_lessons, record_case_generation_lessons
from ai_apitest.core.artifact_language import (
    localize_generated_cases,
    output_language_instruction,
    set_current_artifact_language,
)
from ai_apitest.core.case_schema import (
    external_test_capability_summary,
    normalize_cases,
)
from ai_apitest.core.config import init_project, load_config, save_config
from ai_apitest.core.models import Assertion, CaseResult, Endpoint, TestCase as CaseModel
from ai_apitest.core.paths import project_cases_file, project_reports_dir
from ai_apitest.generator.cases import generate_cases, render_cases_markdown
from ai_apitest.report.markdown import render_report_markdown
from ai_apitest.web import server
from ai_apitest.web.i18n import set_current_locale


def test_default_artifact_language_is_english(tmp_path: Path) -> None:
    init_project(tmp_path, "demo")
    assert load_config(tmp_path)["artifacts"]["language"] == "en"


def test_generation_reads_persisted_terminal_language(tmp_path: Path) -> None:
    init_project(tmp_path, "demo")
    endpoint = Endpoint(
        method="GET",
        path="/health",
        summary="Health",
        responses={"200": {}},
    )
    english, _warning = generate_cases_with_config(
        tmp_path,
        [endpoint],
        project="demo",
        force_refresh=True,
    )
    assert english[0].title == "Health - Happy path"

    config = load_config(tmp_path)
    config["artifacts"]["language"] = "zh"
    save_config(tmp_path, config)
    chinese, _warning = generate_cases_with_config(
        tmp_path,
        [endpoint],
        project="demo",
        force_refresh=True,
    )
    assert chinese[0].title == "Health - 正常场景"


def test_cli_can_show_and_switch_artifact_language(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "cwd_root", lambda: tmp_path)
    cli.artifact_language("fr")
    assert load_config(tmp_path)["artifacts"]["language"] == "fr"


def test_language_switch_does_not_rewrite_historical_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases_path = project_cases_file(tmp_path, "demo")
    report_path = project_reports_dir(tmp_path, "demo") / "report-20260811-120000.md"
    cases_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    cases_path.write_text("# 历史中文用例\n", encoding="utf-8")
    report_path.write_text("# 历史中文报告\n", encoding="utf-8")
    monkeypatch.setattr(cli, "cwd_root", lambda: tmp_path)

    cli.artifact_language("ja")

    assert cases_path.read_text(encoding="utf-8") == "# 历史中文用例\n"
    assert report_path.read_text(encoding="utf-8") == "# 历史中文报告\n"


def test_historical_fixture_guidance_keeps_its_original_language() -> None:
    case = CaseModel(
        id="TC-USERS-0001",
        title="用户过期校验（依赖项目方提供的外部测试能力）",
        method="GET",
        path="/users",
        preconditions=[
            "本用例依赖项目方额外提供的受控测试能力；当前框架未内置该能力，项目未接入前不会自动执行。"
        ],
        test_steps=[
            "需由被测项目方提供服务端时间控制能力，以建立用例所需的生命周期状态。"
        ],
        fixture_requirements=["time_control"],
    )
    try:
        set_current_artifact_language("en")
        normalized = normalize_cases([case])[0]
        assert normalized.title == "用户过期校验（依赖项目方提供的外部测试能力）"
        assert len(normalized.preconditions) == 1
        assert len(normalized.test_steps) == 1
        assert "This case depends" not in "\n".join(normalized.preconditions)
        assert "The project must provide" not in "\n".join(normalized.test_steps)
    finally:
        set_current_artifact_language("en")


def test_new_fixture_guidance_and_capability_labels_use_selected_language() -> None:
    case = CaseModel(
        id="TC-USERS-0001",
        title="Expired user",
        method="GET",
        path="/users",
        fixture_requirements=["time_control"],
    )
    try:
        set_current_artifact_language("fr")
        normalized = normalize_cases([case])[0]
        generated_text = "\n".join(
            [normalized.title, *normalized.preconditions, *normalized.test_steps]
        )
        assert "nécessite une capacité de test externe" in normalized.title
        assert "Le projet doit fournir" in generated_text
        assert external_test_capability_summary(["time_control"], "fr") == "Contrôle du temps serveur"
        assert re.search(r"[\u3400-\u9fff]", generated_text) is None
    finally:
        set_current_artifact_language("en")


@pytest.mark.parametrize("language", ["en", "zh", "ja", "fr", "ru"])
def test_model_prompt_declares_selected_artifact_language(language: str) -> None:
    try:
        set_current_artifact_language(language)
        assert output_language_instruction(language) in system_prompt()
    finally:
        set_current_artifact_language("en")


@pytest.mark.parametrize(
    ("language", "case_heading", "report_heading"),
    [
        ("en", "API Test Case Collection", "Test Execution Report"),
        ("zh", "接口测试用例集", "测试执行报告"),
        ("ja", "API テストケース集", "テスト実行レポート"),
        ("fr", "Collection de cas de test API", "Rapport d’exécution des tests"),
        ("ru", "Набор тестовых сценариев API", "Отчёт о выполнении тестов"),
    ],
)
def test_deterministic_cases_and_reports_use_selected_language(
    language: str,
    case_heading: str,
    report_heading: str,
) -> None:
    endpoint = Endpoint(
        method="GET",
        path="/users/{id}",
        summary="Users",
        parameters=[
            {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
            {
                "name": "q",
                "in": "query",
                "required": True,
                "schema": {"type": "string", "minLength": 1, "maxLength": 3},
            },
        ],
        responses={"200": {}, "400": {}},
    )
    cases = localize_generated_cases(generate_cases([endpoint]), language)
    case_markdown = render_cases_markdown(
        "demo",
        cases,
        artifact_language=language,
    )
    result = CaseResult(
        case_id=cases[0].id,
        title=cases[0].title,
        status="passed",
        method=cases[0].method,
        path=cases[0].path,
        status_code=200,
        elapsed_ms=1,
        preconditions=cases[0].preconditions,
        test_steps=cases[0].test_steps,
        expected_result=cases[0].expected_result,
    )
    report = render_report_markdown(
        [result],
        artifact_language=language,
    )
    assert case_heading in case_markdown
    assert report_heading in report
    if language in {"en", "fr", "ru"}:
        assert re.search(r"[\u3400-\u9fff]", case_markdown) is None
        assert re.search(r"[\u3400-\u9fff]", report) is None


@pytest.mark.parametrize(
    ("language", "heading"),
    [
        ("en", "One-line conclusion"),
        ("zh", "一句话结论"),
        ("ja", "一文の結論"),
        ("fr", "Conclusion en une phrase"),
        ("ru", "Краткий вывод"),
    ],
)
def test_report_quality_gate_accepts_locale_specific_headings(
    language: str,
    heading: str,
) -> None:
    names = {
        "en": ("Result overview", "Main issues", "Recommended actions", "Notes"),
        "zh": ("结果概览", "主要问题", "建议处理", "备注"),
        "ja": ("結果概要", "主な問題", "推奨対応", "備考"),
        "fr": ("Vue d’ensemble des résultats", "Principaux problèmes", "Actions recommandées", "Remarques"),
        "ru": ("Обзор результатов", "Основные проблемы", "Рекомендуемые действия", "Примечания"),
    }[language]
    markdown = "\n".join(f"### {name}\n-" for name in (heading, *names))
    assert report_quality_gate_issues(markdown, [], artifact_language=language) == []


def test_case_generation_lessons_use_current_artifact_language(tmp_path: Path) -> None:
    case = CaseModel(
        id="TC-USER-0001",
        title="Users - Happy path",
        method="GET",
        path="/users",
        design_method="happy_path",
        assertions=[Assertion("status_code", operator="eq", expected=200)],
        expected_result="The API returns the documented success result.",
    )
    try:
        set_current_artifact_language("fr")
        record_case_generation_lessons(tmp_path, "demo", [case])
        lesson = list_lessons(tmp_path, "demo", limit=1)[0]
        assert lesson["title"].startswith("Connaissance d’assertion")
        assert "Résultat attendu" in lesson["content"]
    finally:
        set_current_artifact_language("en")


def test_web_task_captures_console_language(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def fake_create_task(*args, **kwargs):
        captured.update(kwargs.get("payload") or {})
        return {"id": "task-1"}

    monkeypatch.setattr(server, "create_task", fake_create_task)
    monkeypatch.setattr(server, "submit_web_task", lambda *args, **kwargs: True)
    try:
        set_current_locale("ru")
        assert server.start_task(
            tmp_path,
            "demo",
            "generate",
            "Generate",
            payload={"force": True},
        )
        assert captured["artifact_language"] == "ru"
    finally:
        set_current_locale("zh")
