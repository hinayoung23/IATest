from __future__ import annotations

import re
from contextvars import ContextVar
from typing import Any


DEFAULT_ARTIFACT_LANGUAGE = "en"
SUPPORTED_ARTIFACT_LANGUAGES = ("en", "zh", "ja", "fr", "ru")

_CURRENT_ARTIFACT_LANGUAGE: ContextVar[str] = ContextVar(
    "iatest_artifact_language",
    default=DEFAULT_ARTIFACT_LANGUAGE,
)

_LANGUAGE_NAMES = {
    "en": "English",
    "zh": "Simplified Chinese",
    "ja": "Japanese",
    "fr": "French",
    "ru": "Russian",
}

_TEXT: dict[str, dict[str, str]] = {
    "run_metadata": {"en": "Run metadata", "zh": "运行版本信息", "ja": "実行メタデータ", "fr": "Métadonnées d’exécution", "ru": "Метаданные запуска"},
    "retry_evidence": {"en": "Evidence before retry", "zh": "重试前失败证据", "ja": "再試行前の証拠", "fr": "Preuves avant relance", "ru": "Данные до повтора"},
    "total_cases": {"en": "Total cases", "zh": "用例总数", "ja": "ケース総数", "fr": "Total des cas", "ru": "Всего сценариев"},
    "cancelled": {"en": "Cancelled", "zh": "已取消", "ja": "キャンセル済み", "fr": "Annulés", "ru": "Отменено"},
    "flaky": {"en": "Passed after retry", "zh": "重试后通过", "ja": "再試行後成功", "fr": "Réussis après relance", "ru": "Успешно после повтора"},
    "execution_pass_rate": {"en": "Pass rate of executed cases", "zh": "实际执行通过率", "ja": "実行ケース成功率", "fr": "Taux de réussite des cas exécutés", "ru": "Успешность выполненных сценариев"},
    "test_case_collection": {"en": "API Test Case Collection", "zh": "接口测试用例集", "ja": "API テストケース集", "fr": "Collection de cas de test API", "ru": "Набор тестовых сценариев API"},
    "case_source_note": {"en": "This file is the source of truth for project test cases. The Web console reads and saves this file directly.", "zh": "这个文件是该项目的用例源文件。浏览器页面会直接读取和保存本文件。", "ja": "このファイルはプロジェクトのテストケースの正本です。Web コンソールが直接読み書きします。", "fr": "Ce fichier est la source de référence des cas de test du projet. La console Web le lit et l’enregistre directement.", "ru": "Этот файл — источник тестовых сценариев проекта. Веб-консоль читает и сохраняет его напрямую."},
    "structured_case_data": {"en": "Structured case data", "zh": "结构化用例数据", "ja": "構造化ケースデータ", "fr": "Données structurées des cas", "ru": "Структурированные данные сценариев"},
    "case_details": {"en": "Case details", "zh": "用例详情", "ja": "ケース詳細", "fr": "Détails des cas", "ru": "Подробности сценариев"},
    "basic_information": {"en": "Basic information", "zh": "基本信息", "ja": "基本情報", "fr": "Informations générales", "ru": "Основная информация"},
    "attribute": {"en": "Attribute", "zh": "属性", "ja": "属性", "fr": "Attribut", "ru": "Атрибут"},
    "value": {"en": "Value", "zh": "值", "ja": "値", "fr": "Valeur", "ru": "Значение"},
    "case_id": {"en": "Case ID", "zh": "用例ID", "ja": "ケース ID", "fr": "ID du cas", "ru": "ID сценария"},
    "endpoint_path": {"en": "API path", "zh": "接口路径", "ja": "API パス", "fr": "Chemin API", "ru": "Путь API"},
    "priority": {"en": "Priority", "zh": "用例等级", "ja": "優先度", "fr": "Priorité", "ru": "Приоритет"},
    "design_method": {"en": "Design method", "zh": "设计方法", "ja": "設計手法", "fr": "Méthode de conception", "ru": "Метод проектирования"},
    "negative": {"en": "Negative case", "zh": "是否负向", "ja": "ネガティブケース", "fr": "Cas négatif", "ru": "Негативный сценарий"},
    "runnable": {"en": "Runnable", "zh": "可执行", "ja": "実行可能", "fr": "Exécutable", "ru": "Выполнимый"},
    "risk": {"en": "Risk", "zh": "风险", "ja": "リスク", "fr": "Risque", "ru": "Риск"},
    "tags": {"en": "Tags", "zh": "标签", "ja": "タグ", "fr": "Tags", "ru": "Теги"},
    "business_rules": {"en": "Business rules", "zh": "业务规则", "ja": "ビジネスルール", "fr": "Règles métier", "ru": "Бизнес-правила"},
    "coverage_partitions": {"en": "Coverage partitions", "zh": "覆盖分区", "ja": "カバレッジ区分", "fr": "Partitions de couverture", "ru": "Разделы покрытия"},
    "project_must_provide": {"en": "Project must provide", "zh": "项目方需提供", "ja": "プロジェクト側の提供事項", "fr": "À fournir par le projet", "ru": "Требуется от проекта"},
    "expected_result": {"en": "Expected result", "zh": "预期结果", "ja": "期待結果", "fr": "Résultat attendu", "ru": "Ожидаемый результат"},
    "preconditions": {"en": "Preconditions", "zh": "前置条件", "ja": "前提条件", "fr": "Préconditions", "ru": "Предусловия"},
    "case_steps": {"en": "Case steps", "zh": "用例步骤", "ja": "ケース手順", "fr": "Étapes du cas", "ru": "Шаги сценария"},
    "request_information": {"en": "Request information", "zh": "请求信息", "ja": "リクエスト情報", "fr": "Informations de requête", "ru": "Данные запроса"},
    "method": {"en": "Method", "zh": "方法", "ja": "メソッド", "fr": "Méthode", "ru": "Метод"},
    "path": {"en": "Path", "zh": "路径", "ja": "パス", "fr": "Chemin", "ru": "Путь"},
    "path_parameters": {"en": "Path parameters", "zh": "路径参数", "ja": "パスパラメータ", "fr": "Paramètres de chemin", "ru": "Параметры пути"},
    "assertions": {"en": "Assertions", "zh": "断言列表", "ja": "アサーション", "fr": "Assertions", "ru": "Проверки"},
    "number": {"en": "No.", "zh": "序号", "ja": "番号", "fr": "N°", "ru": "№"},
    "assertion_type": {"en": "Assertion type", "zh": "断言类型", "ja": "アサーション種別", "fr": "Type d’assertion", "ru": "Тип проверки"},
    "assertion_target": {"en": "Target", "zh": "断言目标", "ja": "対象", "fr": "Cible", "ru": "Цель"},
    "operator": {"en": "Operator", "zh": "操作符", "ja": "演算子", "fr": "Opérateur", "ru": "Оператор"},
    "expected_value": {"en": "Expected value", "zh": "期望值", "ja": "期待値", "fr": "Valeur attendue", "ru": "Ожидаемое значение"},
    "generation_notes": {"en": "Generation notes", "zh": "生成说明", "ja": "生成メモ", "fr": "Notes de génération", "ru": "Примечания о генерации"},
    "generation_basis": {"en": "Generation basis", "zh": "生成依据", "ja": "生成根拠", "fr": "Base de génération", "ru": "Основание генерации"},
    "rule_generation_note": {"en": "The current version uses rule-based generation across multiple design methods; AI generation provides semantic extensions.", "zh": "当前版本使用规则生成多设计方法用例；AI 生成可作为后续增强。", "ja": "現在のバージョンは複数の設計手法をルールで生成し、AI が意味的な拡張を提供します。", "fr": "La version actuelle génère plusieurs méthodes par règles ; l’IA fournit des extensions sémantiques.", "ru": "Текущая версия генерирует разные методы по правилам; ИИ добавляет семантическое покрытие."},
    "none": {"en": "None", "zh": "无", "ja": "なし", "fr": "Aucun", "ru": "Нет"},
    "no_special_preconditions": {"en": "No special preconditions.", "zh": "无特殊前置条件。", "ja": "特別な前提条件はありません。", "fr": "Aucune précondition particulière.", "ru": "Особых предусловий нет."},
    "assertions_define_expected": {"en": "See the assertion list.", "zh": "以断言列表为准。", "ja": "アサーション一覧を参照してください。", "fr": "Voir la liste des assertions.", "ru": "См. список проверок."},
    "execution_steps": {"en": "Execution steps", "zh": "执行步骤", "ja": "実行手順", "fr": "Étapes d’exécution", "ru": "Шаги выполнения"},
    "setup_steps": {"en": "Setup steps", "zh": "前置准备步骤", "ja": "準備手順", "fr": "Étapes de préparation", "ru": "Шаги подготовки"},
    "cleanup_steps": {"en": "Cleanup steps", "zh": "清理步骤", "ja": "クリーンアップ手順", "fr": "Étapes de nettoyage", "ru": "Шаги очистки"},
    "request": {"en": "Request", "zh": "请求", "ja": "リクエスト", "fr": "Requête", "ru": "Запрос"},
    "extract": {"en": "Extract", "zh": "提取", "ja": "抽出", "fr": "Extraction", "ru": "Извлечение"},
    "test_execution_report": {"en": "Test Execution Report", "zh": "测试执行报告", "ja": "テスト実行レポート", "fr": "Rapport d’exécution des tests", "ru": "Отчёт о выполнении тестов"},
    "overview": {"en": "Overview", "zh": "总览", "ja": "概要", "fr": "Vue d’ensemble", "ru": "Обзор"},
    "metric": {"en": "Metric", "zh": "指标", "ja": "指標", "fr": "Indicateur", "ru": "Метрика"},
    "executed_cases": {"en": "Executed cases", "zh": "执行用例数", "ja": "実行ケース数", "fr": "Cas exécutés", "ru": "Выполнено сценариев"},
    "passed": {"en": "Passed", "zh": "通过", "ja": "合格", "fr": "Réussis", "ru": "Успешно"},
    "failed": {"en": "Failed", "zh": "失败", "ja": "失敗", "fr": "Échecs", "ru": "Ошибки"},
    "skipped": {"en": "Skipped", "zh": "跳过", "ja": "スキップ", "fr": "Ignorés", "ru": "Пропущено"},
    "setup_failed": {"en": "Setup failed", "zh": "准备失败", "ja": "準備失敗", "fr": "Échec de préparation", "ru": "Ошибка подготовки"},
    "cleanup_failed": {"en": "Cleanup failed", "zh": "清理失败", "ja": "クリーンアップ失敗", "fr": "Échec du nettoyage", "ru": "Ошибка очистки"},
    "cleanup_skipped": {"en": "Cleanup skipped", "zh": "清理跳过", "ja": "クリーンアップをスキップ", "fr": "Nettoyage ignoré", "ru": "Очистка пропущена"},
    "details": {"en": "Details", "zh": "明细", "ja": "詳細", "fr": "Détails", "ru": "Подробности"},
    "case": {"en": "Case", "zh": "用例", "ja": "ケース", "fr": "Cas", "ru": "Сценарий"},
    "api": {"en": "API", "zh": "接口", "ja": "API", "fr": "API", "ru": "API"},
    "status": {"en": "Status", "zh": "状态", "ja": "ステータス", "fr": "État", "ru": "Статус"},
    "status_code": {"en": "Status code", "zh": "状态码", "ja": "ステータスコード", "fr": "Code d’état", "ru": "Код состояния"},
    "duration": {"en": "Duration", "zh": "耗时", "ja": "所要時間", "fr": "Durée", "ru": "Длительность"},
    "failure_details": {"en": "Failure details", "zh": "失败详情", "ja": "失敗詳細", "fr": "Détails des échecs", "ru": "Подробности ошибок"},
    "error": {"en": "Error", "zh": "错误", "ja": "エラー", "fr": "Erreur", "ru": "Ошибка"},
    "attribution_hint": {"en": "Attribution note", "zh": "归因提示", "ja": "原因分析の注意", "fr": "Note d’attribution", "ru": "Примечание о причине"},
    "failure_not_always_defect": {"en": "An assertion failure does not necessarily indicate an API defect. Use the preconditions, steps, and test data below to determine whether the cause is test data or an inaccurate expectation.", "zh": "断言失败不一定代表接口缺陷，请结合下方前置条件、用例步骤和测试数据判断是否为测试数据或用例预期问题。", "ja": "アサーション失敗が必ずしも API の不具合を意味するわけではありません。以下の前提条件、手順、テストデータから原因を判断してください。", "fr": "Un échec d’assertion n’indique pas nécessairement un défaut API. Utilisez les préconditions, étapes et données ci-dessous pour identifier la cause.", "ru": "Ошибка проверки не обязательно означает дефект API. Используйте предусловия, шаги и тестовые данные ниже для определения причины."},
    "test_data": {"en": "Test data", "zh": "测试数据", "ja": "テストデータ", "fr": "Données de test", "ru": "Тестовые данные"},
    "test_data_note": {"en": "The following contains the actual requests, responses, and assertion results from this run for determining whether failures come from data, assertions, or the API implementation.", "zh": "以下内容为本次执行时实际发出的请求、收到的响应和断言结果，用于判断失败是否由测试数据、断言规则或接口实现造成。", "ja": "以下は今回の実際のリクエスト、レスポンス、アサーション結果です。失敗原因の判定に使用します。", "fr": "Les éléments suivants contiennent les requêtes, réponses et résultats réels afin d’identifier l’origine des échecs.", "ru": "Ниже приведены фактические запросы, ответы и результаты проверок для определения причин ошибок."},
    "ai_failure_review": {"en": "AI failure review", "zh": "AI 失败复核", "ja": "AI 失敗レビュー", "fr": "Revue IA des échecs", "ru": "ИИ-анализ ошибок"},
    "ai_review_note": {"en": "AI review assists attribution and never overrides the original assertion result.", "zh": "AI 复核仅用于辅助归因，不会覆盖自动化断言的原始执行结果。", "ja": "AI レビューは原因分析を補助するだけで、元のアサーション結果を上書きしません。", "fr": "La revue IA aide à l’attribution sans remplacer le résultat original des assertions.", "ru": "ИИ-анализ помогает определить причину и не заменяет исходный результат проверок."},
    "ai_test_analysis": {"en": "AI test analysis", "zh": "AI 测试解读", "ja": "AI テスト分析", "fr": "Analyse IA des tests", "ru": "ИИ-анализ тестов"},
    "request_data": {"en": "Request data", "zh": "请求数据", "ja": "リクエストデータ", "fr": "Données de requête", "ru": "Данные запроса"},
    "setup_result": {"en": "Setup result", "zh": "前置准备结果", "ja": "準備結果", "fr": "Résultat de préparation", "ru": "Результат подготовки"},
    "response_data": {"en": "Response data", "zh": "响应数据", "ja": "レスポンスデータ", "fr": "Données de réponse", "ru": "Данные ответа"},
    "cleanup_result": {"en": "Cleanup result", "zh": "清理结果", "ja": "クリーンアップ結果", "fr": "Résultat du nettoyage", "ru": "Результат очистки"},
    "assertion_results": {"en": "Assertion results", "zh": "断言结果", "ja": "アサーション結果", "fr": "Résultats des assertions", "ru": "Результаты проверок"},
    "one_line_conclusion": {"en": "One-line conclusion", "zh": "一句话结论", "ja": "一文の結論", "fr": "Conclusion en une phrase", "ru": "Краткий вывод"},
    "result_overview": {"en": "Result overview", "zh": "结果概览", "ja": "結果概要", "fr": "Vue d’ensemble des résultats", "ru": "Обзор результатов"},
    "main_issues": {"en": "Main issues", "zh": "主要问题", "ja": "主な問題", "fr": "Principaux problèmes", "ru": "Основные проблемы"},
    "recommended_actions": {"en": "Recommended actions", "zh": "建议处理", "ja": "推奨対応", "fr": "Actions recommandées", "ru": "Рекомендуемые действия"},
    "notes": {"en": "Notes", "zh": "备注", "ja": "備考", "fr": "Remarques", "ru": "Примечания"},
    "failure_review": {"en": "Failure review", "zh": "失败复核", "ja": "失敗レビュー", "fr": "Revue des échecs", "ru": "Анализ ошибок"},
    "assertion_knowledge_title": {"en": "Assertion knowledge for {title}", "zh": "{title} 的断言经验", "ja": "{title} のアサーション知識", "fr": "Connaissance d’assertion pour {title}", "ru": "Знание о проверках для {title}"},
    "no_structured_assertions": {"en": "no structured assertions", "zh": "无结构化断言", "ja": "構造化アサーションなし", "fr": "aucune assertion structurée", "ru": "нет структурированных проверок"},
    "no_explicit_input": {"en": "no explicit input data", "zh": "无显式输入数据", "ja": "明示的な入力データなし", "fr": "aucune donnée d’entrée explicite", "ru": "нет явных входных данных"},
    "case_assertion_knowledge": {"en": "The expected result for the {design_method} case on {method} {path} is: {expected}. Assertion pattern: {assertions}. Test-data pattern: {inputs}. Reuse these concrete assertions and business expectations for the same API and scenario; do not generate status-code or field assertions that contradict the API documentation.", "zh": "{method} {path} 的 {design_method} 用例预期：{expected}。具体断言模式：{assertions}。测试数据模式：{inputs}。后续生成同接口同场景用例时应复用这些具体断言和业务预期，避免生成不符合接口文档的状态码或字段断言。", "ja": "{method} {path} の {design_method} ケースの期待結果：{expected}。アサーションパターン：{assertions}。テストデータパターン：{inputs}。同じ API とシナリオでは、この具体的なアサーションと業務期待を再利用し、API ドキュメントに反するステータスコードやフィールドのアサーションを生成しないでください。", "fr": "Résultat attendu du cas {design_method} sur {method} {path} : {expected}. Modèle d’assertion : {assertions}. Modèle de données : {inputs}. Réutilisez ces assertions et attentes pour la même API et le même scénario sans contredire la documentation.", "ru": "Ожидаемый результат сценария {design_method} для {method} {path}: {expected}. Шаблон проверок: {assertions}. Шаблон данных: {inputs}. Повторно используйте эти проверки и бизнес-ожидания для того же API и сценария, не создавая противоречащих документации проверок кодов или полей."},
    "dependency_candidate_title": {"en": "{consumer} may depend on {producer}", "zh": "{consumer} 可能依赖 {producer}", "ja": "{consumer} は {producer} に依存する可能性があります", "fr": "{consumer} peut dépendre de {producer}", "ru": "{consumer} может зависеть от {producer}"},
    "dependency_candidate_content": {"en": "Data source: response fields {fields} from {producer}. Usage: {usage} on {consumer}. This is a candidate dependency; it must be confirmed and successfully verified before it can be used in default chain cases.", "zh": "数据来源：{producer} 的响应字段 {fields}。使用位置：{consumer} 的{usage}。当前状态为候选依赖，需人工确认并执行验证通过后，才会用于默认链路用例。", "ja": "データソース：{producer} のレスポンス項目 {fields}。使用箇所：{consumer} の {usage}。これは依存候補であり、確認と検証に成功するまで既定のチェーンケースには使用されません。", "fr": "Source des données : champs de réponse {fields} de {producer}. Utilisation : {usage} sur {consumer}. Cette dépendance candidate doit être confirmée et vérifiée avant d’être utilisée dans les cas chaînés par défaut.", "ru": "Источник данных: поля ответа {fields} из {producer}. Использование: {usage} в {consumer}. Это кандидат зависимости; до подтверждения и успешной проверки он не используется в цепочках по умолчанию."},
    "dependency_path_parameter": {"en": "path parameter {name}", "zh": "路径参数 {name}", "ja": "パスパラメータ {name}", "fr": "paramètre de chemin {name}", "ru": "параметр пути {name}"},
    "dependency_declared_parameter": {"en": "{location} parameter {name}", "zh": "{location} 参数 {name}", "ja": "{location} パラメータ {name}", "fr": "paramètre {name} dans {location}", "ru": "параметр {name} в {location}"},
    "dependency_request_parameter": {"en": "request parameter {name}", "zh": "请求参数 {name}", "ja": "リクエストパラメータ {name}", "fr": "paramètre de requête {name}", "ru": "параметр запроса {name}"},
}


def normalize_artifact_language(value: object, *, default: str = DEFAULT_ARTIFACT_LANGUAGE) -> str:
    raw = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "english": "en", "en-us": "en", "en-gb": "en",
        "chinese": "zh", "中文": "zh", "zh-cn": "zh", "zh-hans": "zh",
        "japanese": "ja", "日本語": "ja", "ja-jp": "ja",
        "french": "fr", "français": "fr", "fr-fr": "fr",
        "russian": "ru", "русский": "ru", "ru-ru": "ru",
    }
    normalized = aliases.get(raw, raw.split("-", 1)[0])
    return normalized if normalized in SUPPORTED_ARTIFACT_LANGUAGES else default


def validate_artifact_language(value: object) -> str:
    normalized = normalize_artifact_language(value, default="")
    if not normalized:
        supported = ", ".join(SUPPORTED_ARTIFACT_LANGUAGES)
        raise ValueError(f"Unsupported artifact language {value!r}; choose one of: {supported}")
    return normalized


def set_current_artifact_language(value: object) -> str:
    normalized = normalize_artifact_language(value)
    _CURRENT_ARTIFACT_LANGUAGE.set(normalized)
    return normalized


def current_artifact_language() -> str:
    return _CURRENT_ARTIFACT_LANGUAGE.get()


def configured_artifact_language(config: dict[str, Any], override: object = None) -> str:
    if override not in {None, ""}:
        return validate_artifact_language(override)
    artifacts = config.get("artifacts") if isinstance(config.get("artifacts"), dict) else {}
    return normalize_artifact_language(artifacts.get("language"))


def artifact_text(key: str, language: object = None, **values: object) -> str:
    locale = normalize_artifact_language(language or current_artifact_language())
    translations = _TEXT.get(key)
    if translations is None:
        raise KeyError(f"Unknown artifact text key: {key}")
    template = translations.get(locale) or translations[DEFAULT_ARTIFACT_LANGUAGE]
    return template.format(**values)


def output_language_instruction(language: object = None) -> str:
    locale = normalize_artifact_language(language or current_artifact_language())
    return (
        f"Output language: {_LANGUAGE_NAMES[locale]} ({locale}). "
        "Write every human-readable artifact field in that language, including titles, intents, "
        "preconditions, steps, expected outcomes, report prose, knowledge titles, and knowledge content. "
        "Do not translate JSON keys, enum values, IDs, tags, design_method, HTTP methods, API paths, "
        "JSONPath expressions, code, raw requests/responses, or quoted source evidence."
    )


def analysis_headings(language: object = None) -> dict[str, str]:
    return {
        key: artifact_text(key, language)
        for key in (
            "one_line_conclusion",
            "result_overview",
            "main_issues",
            "recommended_actions",
            "notes",
            "failure_review",
        )
    }


def detect_analysis_language(markdown: str, fallback: object = None) -> str:
    best_language = normalize_artifact_language(
        fallback or current_artifact_language()
    )
    best_score = 0
    for language in SUPPORTED_ARTIFACT_LANGUAGES:
        headings = analysis_headings(language)
        score = sum(
            f"### {heading}" in str(markdown or "")
            for heading in headings.values()
        )
        if score > best_score:
            best_language = language
            best_score = score
    return best_language


_GENERATED_EXACT: dict[str, dict[str, str]] = {
    "测试环境和接口基础地址已正确配置。": {"en": "The test environment and API base URL are configured.", "ja": "テスト環境と API ベース URL が設定されています。", "fr": "L’environnement de test et l’URL de base de l’API sont configurés.", "ru": "Тестовое окружение и базовый URL API настроены."},
    "请求体字段含义以接口文档和当前接口信息页配置为准。": {"en": "Request-body fields follow the API documentation and the current API configuration.", "ja": "リクエストボディの項目は API ドキュメントと現在の API 設定に従います。", "fr": "Les champs du corps suivent la documentation et la configuration API actuelle.", "ru": "Поля тела запроса соответствуют документации и текущей конфигурации API."},
    "路径参数使用用例中给出的测试值；如该值依赖真实业务数据，需要先确认数据存在或由链路步骤创建。": {"en": "Use the path-parameter values defined by the case. If a value depends on real business data, confirm it exists or create it in an earlier chain step.", "ja": "ケースで定義されたパスパラメータ値を使用します。実データに依存する場合は、存在を確認するか前段のチェーン手順で作成します。", "fr": "Utilisez les paramètres de chemin du cas. Si une valeur dépend de données métier réelles, confirmez son existence ou créez-la dans une étape antérieure.", "ru": "Используйте значения параметров пути из сценария. Если значение зависит от реальных данных, подтвердите его наличие или создайте на предыдущем шаге цепочки."},
    "发送请求并记录实际响应状态码、响应头和响应体。": {"en": "Send the request and record the actual status code, headers, and body.", "ja": "リクエストを送信し、実際のステータスコード、ヘッダー、ボディを記録します。", "fr": "Envoyer la requête et enregistrer le code d’état, les en-têtes et le corps réels.", "ru": "Отправить запрос и записать фактический код состояния, заголовки и тело."},
    "按断言列表校验实际响应是否符合预期。": {"en": "Validate the actual response against the assertion list.", "ja": "実際のレスポンスをアサーション一覧で検証します。", "fr": "Valider la réponse réelle à l’aide de la liste d’assertions.", "ru": "Проверить фактический ответ по списку проверок."},
    "接口使用合法参数时应成功返回": {"en": "The API should succeed with valid parameters", "ja": "有効なパラメータでは API が成功すること", "fr": "L’API doit réussir avec des paramètres valides", "ru": "API должен успешно отвечать с допустимыми параметрами"},
    "可选参数缺省时接口仍应成功返回": {"en": "The API should still succeed when optional parameters are omitted", "ja": "任意パラメータを省略しても API が成功すること", "fr": "L’API doit réussir même si les paramètres facultatifs sont omis", "ru": "API должен успешно отвечать без необязательных параметров"},
    "缺失必填 Query 参数时应返回客户端错误": {"en": "A missing required query parameter should return a client error", "ja": "必須 Query パラメータ欠落時はクライアントエラーを返すこと", "fr": "Un paramètre Query obligatoire manquant doit produire une erreur client", "ru": "При отсутствии обязательного Query-параметра должна возвращаться ошибка клиента"},
    "Query 参数类型错误时应返回客户端错误或业务错误": {"en": "An invalid query-parameter type should return a client or business error", "ja": "Query パラメータの型が不正な場合はクライアントまたは業務エラーを返すこと", "fr": "Un type Query invalide doit produire une erreur client ou métier", "ru": "Неверный тип Query-параметра должен приводить к клиентской или бизнес-ошибке"},
    "边界值应得到明确成功或明确错误响应": {"en": "Boundary values should produce an explicit success or error response", "ja": "境界値には明確な成功またはエラーレスポンスを返すこと", "fr": "Les valeurs limites doivent produire une réussite ou une erreur explicite", "ru": "Граничные значения должны давать однозначный успешный ответ или ошибку"},
    "非法路径参数应返回明确错误，或返回表示资源不存在的业务结果": {"en": "An invalid path parameter should return an explicit error or a documented resource-not-found result", "ja": "不正なパスパラメータには明確なエラーまたはリソース不存在の業務結果を返すこと", "fr": "Un paramètre de chemin invalide doit produire une erreur explicite ou un résultat indiquant une ressource absente", "ru": "Недопустимый параметр пути должен давать явную ошибку или результат отсутствия ресурса"},
    "请求体缺失必要字段时应返回客户端错误": {"en": "Missing required request-body fields should return a client error", "ja": "リクエストボディの必須項目欠落時はクライアントエラーを返すこと", "fr": "L’absence de champs obligatoires dans le corps doit produire une erreur client", "ru": "При отсутствии обязательных полей тела должна возвращаться ошибка клиента"},
    "请求体结构错误时应返回客户端错误": {"en": "An invalid request-body structure should return a client error", "ja": "リクエストボディ構造が不正な場合はクライアントエラーを返すこと", "fr": "Une structure de corps invalide doit produire une erreur client", "ru": "Неверная структура тела запроса должна приводить к ошибке клиента"},
    "使用各参数有效等价类代表值时，接口应按业务规则返回成功或明确业务结果": {"en": "Valid equivalence-class values should produce success or an explicit business result", "ja": "有効な同値クラス代表値では成功または明確な業務結果を返すこと", "fr": "Des valeurs de classes d’équivalence valides doivent produire une réussite ou un résultat métier explicite", "ru": "Допустимые представители классов эквивалентности должны давать успех или явный бизнес-результат"},
    "缺少鉴权信息时应返回 401 或 403": {"en": "Missing authentication should return 401 or 403", "ja": "認証情報がない場合は 401 または 403 を返すこと", "fr": "L’absence d’authentification doit produire 401 ou 403", "ru": "При отсутствии аутентификации должен возвращаться 401 или 403"},
    "接口应根据文档规则拒绝无效输入，并返回明确且非服务端异常的结果。": {"en": "The API should reject invalid input according to the documented rules and return an explicit non-server-error result.", "ja": "API は文書化されたルールに従って無効な入力を拒否し、明確な非サーバーエラー結果を返す必要があります。", "fr": "L’API doit rejeter les entrées invalides selon les règles documentées et produire un résultat explicite autre qu’une erreur serveur.", "ru": "API должен отклонить недопустимый ввод согласно документации и вернуть явный результат без серверной ошибки."},
    "接口使用符合文档的输入时，应返回文档声明的成功结果。": {"en": "With documented valid input, the API should return the documented success result.", "ja": "文書化された有効な入力では、API は文書化された成功結果を返す必要があります。", "fr": "Avec une entrée valide documentée, l’API doit retourner le résultat de réussite documenté.", "ru": "С документированным допустимым вводом API должен вернуть заявленный успешный результат."},
    "测试环境及链路中的接口均可访问。": {"en": "The test environment and every API in the chain are accessible.", "ja": "テスト環境とチェーン内のすべての API にアクセスできます。", "fr": "L’environnement de test et toutes les API de la chaîne sont accessibles.", "ru": "Тестовое окружение и все API в цепочке доступны."},
    "链路变量由前置步骤响应提取，不依赖硬编码业务数据。": {"en": "Chain variables are extracted from earlier responses instead of hard-coded business data.", "ja": "チェーン変数は前段のレスポンスから抽出し、業務データをハードコードしません。", "fr": "Les variables de chaîne sont extraites des réponses précédentes sans données métier codées en dur.", "ru": "Переменные цепочки извлекаются из предыдущих ответов, без жёстко заданных бизнес-данных."},
    "本用例依赖项目方额外提供的受控测试能力；当前框架未内置该能力，项目未接入前不会自动执行。": {"en": "This case depends on controlled test capabilities supplied by the project. IATest does not provide them, so the case will not run automatically until they are connected.", "ja": "このケースはプロジェクト側が提供する制御可能なテスト機能に依存します。IATest には内蔵されていないため、接続されるまで自動実行されません。", "fr": "Ce cas dépend de capacités de test contrôlées fournies par le projet. IATest ne les inclut pas et n’exécutera pas automatiquement le cas avant leur connexion.", "ru": "Сценарий зависит от управляемых тестовых возможностей проекта. IATest не предоставляет их и не запустит сценарий автоматически до подключения."},
    "需由被测项目方提供服务端时间控制能力，以建立用例所需的生命周期状态。": {"en": "The project must provide server-side time control to establish the lifecycle state required by the case.", "ja": "ケースに必要なライフサイクル状態を作るため、プロジェクト側でサーバー時刻制御を提供する必要があります。", "fr": "Le projet doit fournir un contrôle du temps serveur pour établir l’état de cycle de vie requis.", "ru": "Проект должен предоставить управление серверным временем для создания нужного состояния жизненного цикла."},
    "需由被测项目方投递受控邮件数据，再执行接口校验。": {"en": "The project must deliver controlled email data before the API is validated.", "ja": "API 検証前に、プロジェクト側で制御されたメールデータを投入する必要があります。", "fr": "Le projet doit injecter des données d’e-mail contrôlées avant la validation de l’API.", "ru": "Проект должен доставить управляемые почтовые данные до проверки API."},
    "需由被测项目方提供隔离的限流测试环境，避免污染共享 IP 配额。": {"en": "The project must provide an isolated rate-limit environment to avoid consuming a shared IP quota.", "ja": "共有 IP クォータを消費しないよう、プロジェクト側で分離されたレート制限環境を提供する必要があります。", "fr": "Le projet doit fournir un environnement de limitation isolé afin de ne pas consommer le quota IP partagé.", "ru": "Проект должен предоставить изолированное окружение ограничения частоты, чтобы не расходовать общий IP-квот."},
    "需由被测项目方提供真实保留前缀测试数据，并注入 query.prefix 参数。": {"en": "The project must provide a real reserved-prefix test value and inject it into query.prefix.", "ja": "プロジェクト側で実際の予約済みプレフィックスのテストデータを提供し、query.prefix に注入する必要があります。", "fr": "Le projet doit fournir une véritable valeur de préfixe réservé et l’injecter dans query.prefix.", "ru": "Проект должен предоставить реальное зарезервированное значение префикса и передать его в query.prefix."},
    "需由被测项目方提供当前受限前缀测试数据，并注入 query.prefix 参数。": {"en": "The project must provide a currently restricted-prefix test value and inject it into query.prefix.", "ja": "プロジェクト側で現在制限されているプレフィックスのテストデータを提供し、query.prefix に注入する必要があります。", "fr": "Le projet doit fournir une valeur de préfixe actuellement restreinte et l’injecter dans query.prefix.", "ru": "Проект должен предоставить текущее запрещённое значение префикса и передать его в query.prefix."},
    "需由被测项目方提供该场景所需的受控测试数据或环境能力。": {"en": "The project must provide the controlled test data or environment capability required by this scenario.", "ja": "このシナリオに必要な制御可能なテストデータまたは環境機能をプロジェクト側で提供する必要があります。", "fr": "Le projet doit fournir les données contrôlées ou la capacité d’environnement requises par ce scénario.", "ru": "Проект должен предоставить управляемые тестовые данные или возможности окружения для этого сценария."},
    "项目方提供的外部测试能力必须准备与当前 path_params 对应的同一资源；如动态分配资源，执行前须用实际值替换对应路径参数。": {"en": "The external capability must prepare the same resource referenced by path_params. If it allocates a resource dynamically, replace the corresponding path parameter with the actual value before execution.", "ja": "外部テスト機能は現在の path_params と同じリソースを準備する必要があります。動的に割り当てる場合は、実行前に対応するパスパラメータを実値に置換してください。", "fr": "La capacité externe doit préparer la même ressource que celle référencée par path_params. En cas d’allocation dynamique, remplacez le paramètre par la valeur réelle avant l’exécution.", "ru": "Внешняя возможность должна подготовить тот же ресурс, что указан в path_params. При динамическом выделении замените параметр пути фактическим значением до запуска."},
    "用例结束后执行清理步骤，释放本次测试创建的临时数据。": {"en": "Run cleanup after the case to remove temporary data created by this test.", "ja": "ケース終了後にクリーンアップを実行し、このテストで作成した一時データを削除します。", "fr": "Exécuter le nettoyage après le cas pour supprimer les données temporaires créées.", "ru": "После сценария выполнить очистку временных данных, созданных тестом."},
    "执行前置准备步骤，创建本用例需要的临时测试数据。": {"en": "Run the setup steps to create the temporary data required by this case.", "ja": "準備手順を実行して、このケースに必要な一時テストデータを作成します。", "fr": "Exécuter les étapes de préparation pour créer les données temporaires requises.", "ru": "Выполнить подготовку и создать временные данные для сценария."},
    "本用例会通过前置准备步骤创建所需测试数据，不依赖外部预置数据。": {"en": "This case creates its required data through setup steps and does not depend on externally preloaded data.", "ja": "このケースは準備手順で必要なデータを作成し、外部の事前データには依存しません。", "fr": "Ce cas crée les données requises via les étapes de préparation sans dépendre de données externes préchargées.", "ru": "Сценарий создаёт нужные данные на этапе подготовки и не зависит от заранее загруженных внешних данных."},
}

_TITLE_SUFFIXES = {
    "正常场景": {"en": "Happy path", "ja": "正常系", "fr": "Scénario nominal", "ru": "Позитивный сценарий"},
    "不传可选参数": {"en": "Optional parameters omitted", "ja": "任意パラメータを省略", "fr": "Paramètres facultatifs omis", "ru": "Необязательные параметры пропущены"},
    "请求体为空": {"en": "Empty request body", "ja": "空のリクエストボディ", "fr": "Corps de requête vide", "ru": "Пустое тело запроса"},
    "请求体类型错误": {"en": "Invalid request-body type", "ja": "リクエストボディの型エラー", "fr": "Type de corps invalide", "ru": "Неверный тип тела запроса"},
    "有效等价类代表值": {"en": "Valid equivalence-class value", "ja": "有効な同値クラス代表値", "fr": "Valeur valide de classe d’équivalence", "ru": "Допустимое значение класса эквивалентности"},
    "文档状态码覆盖": {"en": "Documented status-code coverage", "ja": "文書化済みステータスコードのカバレッジ", "fr": "Couverture des codes documentés", "ru": "Покрытие документированных кодов состояния"},
    "缺少鉴权信息": {"en": "Missing authentication", "ja": "認証情報なし", "fr": "Authentification absente", "ru": "Отсутствует аутентификация"},
    "链路主流程": {"en": "Primary dependency chain", "ja": "主要依存チェーン", "fr": "Chaîne de dépendances principale", "ru": "Основная цепочка зависимостей"},
    "接口依赖主链路": {"en": "Primary API dependency chain", "ja": "主要 API 依存チェーン", "fr": "Chaîne principale de dépendances API", "ru": "Основная цепочка зависимостей API"},
}

_BOUNDARY_LABELS = {
    "高于排他最小边界": {"en": "above exclusive minimum", "ja": "排他的最小値より大きい", "fr": "au-dessus du minimum exclusif", "ru": "выше исключающего минимума"},
    "等于排他最小边界": {"en": "equal to exclusive minimum", "ja": "排他的最小値と等しい", "fr": "égal au minimum exclusif", "ru": "равно исключающему минимуму"},
    "最小边界": {"en": "minimum", "ja": "最小値", "fr": "minimum", "ru": "минимум"},
    "低于最小边界": {"en": "below minimum", "ja": "最小値未満", "fr": "sous le minimum", "ru": "ниже минимума"},
    "低于排他最大边界": {"en": "below exclusive maximum", "ja": "排他的最大値より小さい", "fr": "sous le maximum exclusif", "ru": "ниже исключающего максимума"},
    "等于排他最大边界": {"en": "equal to exclusive maximum", "ja": "排他的最大値と等しい", "fr": "égal au maximum exclusif", "ru": "равно исключающему максимуму"},
    "最大边界": {"en": "maximum", "ja": "最大値", "fr": "maximum", "ru": "максимум"},
    "高于最大边界": {"en": "above maximum", "ja": "最大値超過", "fr": "au-dessus du maximum", "ru": "выше максимума"},
    "负数": {"en": "negative number", "ja": "負数", "fr": "nombre négatif", "ru": "отрицательное число"},
    "超大值": {"en": "very large value", "ja": "非常に大きい値", "fr": "très grande valeur", "ru": "очень большое значение"},
    "最小元素数": {"en": "minimum item count", "ja": "最小要素数", "fr": "nombre minimal d’éléments", "ru": "минимальное число элементов"},
    "低于最小元素数": {"en": "below minimum item count", "ja": "最小要素数未満", "fr": "sous le nombre minimal d’éléments", "ru": "меньше минимального числа элементов"},
    "最大元素数": {"en": "maximum item count", "ja": "最大要素数", "fr": "nombre maximal d’éléments", "ru": "максимальное число элементов"},
    "高于最大元素数": {"en": "above maximum item count", "ja": "最大要素数超過", "fr": "au-dessus du nombre maximal d’éléments", "ru": "больше максимального числа элементов"},
    "空数组": {"en": "empty array", "ja": "空配列", "fr": "tableau vide", "ru": "пустой массив"},
    "单元素": {"en": "single item", "ja": "単一要素", "fr": "un seul élément", "ru": "один элемент"},
    "最小长度": {"en": "minimum length", "ja": "最小長", "fr": "longueur minimale", "ru": "минимальная длина"},
    "低于最小长度": {"en": "below minimum length", "ja": "最小長未満", "fr": "sous la longueur minimale", "ru": "короче минимальной длины"},
    "最大长度": {"en": "maximum length", "ja": "最大長", "fr": "longueur maximale", "ru": "максимальная длина"},
    "高于最大长度": {"en": "above maximum length", "ja": "最大長超過", "fr": "au-dessus de la longueur maximale", "ru": "длиннее максимальной длины"},
    "不匹配格式": {"en": "format mismatch", "ja": "形式不一致", "fr": "format non conforme", "ru": "несоответствие формату"},
    "空字符串": {"en": "empty string", "ja": "空文字列", "fr": "chaîne vide", "ru": "пустая строка"},
    "单字符": {"en": "single character", "ja": "単一文字", "fr": "un seul caractère", "ru": "один символ"},
    "特殊字符": {"en": "special characters", "ja": "特殊文字", "fr": "caractères spéciaux", "ru": "специальные символы"},
    "超长字符串": {"en": "overlong string", "ja": "超長文字列", "fr": "chaîne trop longue", "ru": "слишком длинная строка"},
}


def translate_generated_text(value: str, language: object = None) -> str:
    locale = normalize_artifact_language(language or current_artifact_language())
    text = str(value or "")
    if locale == "zh" or not text:
        return text
    direct = _GENERATED_EXACT.get(text)
    if direct:
        return direct[locale]
    for source, translations in _TITLE_SUFFIXES.items():
        if text == source:
            return translations[locale]
        suffix = f" - {source}"
        if text.endswith(suffix):
            return text[: -len(source)] + translations[locale]
    fixture_suffix = "（依赖项目方提供的外部测试能力）"
    if text.endswith(fixture_suffix):
        suffixes = {
            "en": " (requires external test capability from the project)",
            "ja": "（プロジェクト側の外部テスト機能が必要）",
            "fr": " (nécessite une capacité de test externe du projet)",
            "ru": " (требуется внешняя тестовая возможность проекта)",
        }
        return text[: -len(fixture_suffix)] + suffixes[locale]
    patterns = (
        (r"^接口 (.+) 在当前环境可访问。$", {"en": "API {0} is accessible in the current environment.", "ja": "API {0} は現在の環境でアクセス可能です。", "fr": "L’API {0} est accessible dans l’environnement actuel.", "ru": "API {0} доступен в текущем окружении."}),
        (r"^按用例数据准备请求：(.+)。$", {"en": "Prepare the request using the case data: {0}.", "ja": "ケースデータでリクエストを準備します：{0}。", "fr": "Préparer la requête avec les données du cas : {0}.", "ru": "Подготовить запрос по данным сценария: {0}."}),
        (r"^替换路径参数：(.+)。$", {"en": "Substitute path parameters: {0}.", "ja": "パスパラメータを置換します：{0}。", "fr": "Remplacer les paramètres de chemin : {0}.", "ru": "Подставить параметры пути: {0}."}),
        (r"^设置 Query 参数：(.+)。$", {"en": "Set Query parameters: {0}.", "ja": "Query パラメータを設定します：{0}。", "fr": "Définir les paramètres Query : {0}.", "ru": "Установить Query-параметры: {0}."}),
        (r"^设置请求体：(.+)。$", {"en": "Set the request body: {0}.", "ja": "リクエストボディを設定します：{0}。", "fr": "Définir le corps de la requête : {0}.", "ru": "Установить тело запроса: {0}."}),
        (r"^接口返回文档声明的 (\d+) 响应。$", {"en": "The API returns the documented {0} response.", "ja": "API は文書化された {0} レスポンスを返します。", "fr": "L’API retourne la réponse {0} documentée.", "ru": "API возвращает документированный ответ {0}."}),
        (r"^响应状态码应落在接口文档声明的状态码集合中：(.+)$", {"en": "The response status must be one of the documented status codes: {0}", "ja": "レスポンスステータスは文書化されたコードのいずれかであること：{0}", "fr": "Le code de réponse doit appartenir aux codes documentés : {0}", "ru": "Код ответа должен входить в документированный набор: {0}"}),
        (r"^执行链路步骤 (\d+)：(.+)。$", {"en": "Execute chain step {0}: {1}.", "ja": "チェーン手順 {0} を実行：{1}。", "fr": "Exécuter l’étape de chaîne {0} : {1}.", "ru": "Выполнить шаг цепочки {0}: {1}."}),
    )
    for pattern, outputs in patterns:
        match = re.fullmatch(pattern, text)
        if match:
            return outputs[locale].format(*match.groups())
    dynamic_title_patterns = (
        (r"^(.* - )缺失必填 Query 参数 (.+)$", {"en": "{0}Missing required Query parameter {1}", "ja": "{0}必須 Query パラメータ {1} が欠落", "fr": "{0}Paramètre Query obligatoire {1} manquant", "ru": "{0}Отсутствует обязательный Query-параметр {1}"}),
        (r"^(.* - )Query 参数 (.+) 类型错误$", {"en": "{0}Invalid type for Query parameter {1}", "ja": "{0}Query パラメータ {1} の型エラー", "fr": "{0}Type invalide pour le paramètre Query {1}", "ru": "{0}Неверный тип Query-параметра {1}"}),
        (r"^(.* - )Path 参数 (.+) 空值$", {"en": "{0}Empty path parameter {1}", "ja": "{0}空のパスパラメータ {1}", "fr": "{0}Paramètre de chemin {1} vide", "ru": "{0}Пустой параметр пути {1}"}),
        (r"^(.* - )Path 参数 (.+) 非法格式$", {"en": "{0}Invalid path parameter format {1}", "ja": "{0}パスパラメータ {1} の形式不正", "fr": "{0}Format invalide du paramètre de chemin {1}", "ru": "{0}Неверный формат параметра пути {1}"}),
    )
    for pattern, outputs in dynamic_title_patterns:
        match = re.fullmatch(pattern, text)
        if match:
            return outputs[locale].format(*match.groups())
    boundary_match = re.fullmatch(r"^(.* - )Query 参数 (.+) 边界值 (.+)$", text)
    if boundary_match:
        prefix, name, label = boundary_match.groups()
        translated_label = _BOUNDARY_LABELS.get(label, {}).get(locale, label)
        templates = {
            "en": "{0}Query parameter {1} boundary: {2}",
            "ja": "{0}Query パラメータ {1} の境界値：{2}",
            "fr": "{0}Limite du paramètre Query {1} : {2}",
            "ru": "{0}Граница Query-параметра {1}: {2}",
        }
        return templates[locale].format(prefix, name, translated_label)
    return text


def localize_generated_cases(cases: list[Any], language: object = None) -> list[Any]:
    locale = normalize_artifact_language(language or current_artifact_language())
    if locale == "zh":
        return cases
    for case in cases:
        case.title = translate_generated_text(str(case.title), locale)
        case.preconditions = [translate_generated_text(str(item), locale) for item in case.preconditions]
        case.test_steps = [translate_generated_text(str(item), locale) for item in case.test_steps]
        case.expected_result = translate_generated_text(str(case.expected_result), locale)
        for sequence_name in ("setup_steps", "steps", "cleanup_steps"):
            for step in getattr(case, sequence_name, []) or []:
                step.name = translate_generated_text(str(step.name), locale)
    return cases
