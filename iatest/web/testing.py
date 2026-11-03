from __future__ import annotations

import html
import json
from urllib.parse import parse_qs, urlencode

from iatest.engines.registry import create_engine_registry
from iatest.engines.store import EngineStore
from iatest.web.i18n import current_locale
from iatest.web.operations import COPY as OPERATIONS_COPY
from iatest.web.operations import TABS as OPERATIONS_TABS

_COPY = {
    "network_policy": (
        "网络约束", "Network scope", "ネットワーク制限", "Contrôle réseau", "Сетевые ограничения",
    ),
    "network_strict": (
        "拦截越界请求（默认）", "Block out-of-scope requests (default)", "範囲外リクエストを遮断（既定）", "Bloquer les requêtes hors périmètre (défaut)", "Блокировать запросы вне области (по умолчанию)",
    ),
    "network_navigation": (
        "Safari常规：校验导航和定位来源", "Safari standard: check navigation and target origins", "Safari標準：遷移と対象のオリジンを確認", "Safari standard : vérifier les origines de navigation et des cibles", "Safari: проверять источники навигации и элементов",
    ),
    "network_navigation_hint": (
        "Safari常规模式不拦截网页自身的网络请求；已配置全局来源或网段限制时不可用。",
        "Safari standard mode does not block page-generated network requests and cannot enforce configured global origin or network restrictions.",
        "Safari標準モードはページ自身の通信を遮断しません。全体のオリジン・ネットワーク制限を設定した場合は使用できません。",
        "Le mode standard Safari ne bloque pas les requêtes de la page et ne peut pas appliquer les restrictions globales d’origine ou de réseau.",
        "Стандартный режим Safari не блокирует запросы страницы и не применяет глобальные ограничения источников или сетей.",
    ),
    "readiness_hint": (
        "工具安装与运行验收分别显示；设备、权限或驱动变化后需重新验证。浏览器可运行 iatest test doctor --browser chromium（或 firefox/webkit）。",
        "Installed tools do not prove runtime readiness. Recheck after device, permission or driver changes. Run iatest test doctor --browser chromium (or firefox/webkit).",
        "インストールと実行確認は別です。変更後は iatest test doctor --browser chromium（firefox/webkit）で再確認してください。",
        "Installation et validation sont distinctes. Relancez iatest test doctor --browser chromium (ou firefox/webkit) après toute modification.",
        "Установка и проверка запуска различаются. После изменений выполните iatest test doctor --browser chromium (firefox/webkit).",
    ),
    "proto_sources": (
        ".proto 源文件（JSON）",
        ".proto sources (JSON)",
        ".proto ソース（JSON）",
        "Sources .proto (JSON)",
        "Исходники .proto (JSON)",
    ),
    "proto_compile": (
        "编译 .proto",
        "Compile .proto",
        ".proto をコンパイル",
        "Compiler .proto",
        "Компилировать .proto",
    ),
    "desktop": (
        "桌面 UI",
        "Desktop UI",
        "デスクトップ UI",
        "UI de bureau",
        "Настольный UI",
    ),
    "data": (
        "数据质量",
        "Data quality",
        "データ品質",
        "Qualité des données",
        "Качество данных",
    ),
    "protocol": (
        "协议契约",
        "Protocol contracts",
        "プロトコル契約",
        "Contrats de protocole",
        "Контракты протокола",
    ),
    "accessibility": (
        "无障碍检查",
        "Accessibility checks",
        "アクセシビリティ",
        "Accessibilité",
        "Доступность",
    ),
    "force_explore": (
        "重新探索并创建新版本",
        "Explore again as a new version",
        "再探索して新規バージョンを作成",
        "Explorer à nouveau et créer une version",
        "Повторить исследование и создать версию",
    ),
    "exploration": (
        "目标探索与用例",
        "Goal exploration & cases",
        "目標探索とケース",
        "Exploration et cas",
        "Исследование и тесты",
    ),
    "analysis": (
        "AI 证据分析",
        "AI evidence analysis",
        "AI 証拠分析",
        "Analyse des preuves IA",
        "ИИ-анализ фактов",
    ),
    "explore": (
        "开始探索并复验",
        "Explore and replay",
        "探索して再検証",
        "Explorer et rejouer",
        "Исследовать и повторить",
    ),
    "explore_hint": (
        "描述验证目的即可，步骤可选。探索会实际操作应用，固化前自动使用新会话和新数据复验两次。凭据请在补充上下文中使用 input_env。",
        "Describe the outcome; steps are optional. Exploration acts on the app and replays twice with fresh sessions/data. Use input_env for credentials.",
        "目的のみで開始できます。実際に操作し、新しいセッションとデータで2回再検証します。",
        "Décrivez le résultat ; les étapes sont facultatives. Actions réelles et deux nouvelles exécutions.",
        "Опишите цель; шаги необязательны. Реальные действия и два повтора с новыми данными.",
    ),
    "baseline": (
        "基线运行 ID（可选）",
        "Baseline run ID (optional)",
        "基準実行 ID（任意）",
        "ID de référence (facultatif)",
        "ID базового запуска (необязательно)",
    ),
    "telemetry": (
        "补充观测 JSON（可选）",
        "Additional observations JSON (optional)",
        "追加観測 JSON（任意）",
        "Observations JSON (facultatif)",
        "Дополнительные наблюдения JSON",
    ),
    "platform": ("平台", "Platform", "プラットフォーム", "Plateforme", "Платформа"),
    "profile": (
        "移动设备配置名称",
        "Mobile device profile",
        "端末プロファイル",
        "Profil mobile",
        "Профиль устройства",
    ),
    "oracle": (
        "冻结的验证目标",
        "Frozen oracles",
        "固定した検証目標",
        "Résultats attendus figés",
        "Зафиксированные ожидания",
    ),
    "interpretations": (
        "AI 解释与假设",
        "AI interpretations & hypotheses",
        "AI の解釈と仮説",
        "Interprétations et hypothèses IA",
        "Интерпретации и гипотезы ИИ",
    ),
    "recommendations": (
        "建议与复测方法",
        "Advice & retest steps",
        "改善案と再検証",
        "Conseils et vérifications",
        "Рекомендации и перепроверка",
    ),
    "facts": (
        "执行器事实与证据",
        "Engine facts & evidence",
        "実行結果と証拠",
        "Faits et preuves du moteur",
        "Факты и доказательства",
    ),
    "requests": (
        "实际请求",
        "HTTP requests",
        "実リクエスト",
        "Requêtes HTTP",
        "HTTP запросы",
    ),
    "completed": ("已完成", "Completed", "完了", "Terminé", "Завершено"),
    "error_ratio_metric": (
        "错误率",
        "Error ratio",
        "エラー率",
        "Taux d’erreur",
        "Доля ошибок",
    ),
    "rps": (
        "实际每秒请求",
        "Achieved requests/s",
        "実リクエスト/秒",
        "Requêtes/s atteintes",
        "Фактические запросы/с",
    ),
    "dropped": (
        "未发出的目标请求",
        "Dropped arrivals",
        "未送信リクエスト",
        "Arrivées abandonnées",
        "Пропущенные запросы",
    ),
    "p95_ms": ("P95（毫秒）", "P95 (ms)", "P95 (ms)", "P95 (ms)", "P95 (мс)"),
    "checks": (
        "计划检查数",
        "Planned checks",
        "予定チェック数",
        "Contrôles prévus",
        "Запланировано проверок",
    ),
    "assessed": (
        "已评估检查",
        "Assessed checks",
        "評価済み",
        "Contrôles évalués",
        "Оценено проверок",
    ),
    "not_assessed": (
        "未评估检查",
        "Unassessed checks",
        "未評価",
        "Contrôles non évalués",
        "Не оценено",
    ),
    "findings": ("发现数", "Findings", "検出数", "Constats", "Находки"),
    "nodes": (
        "编排步骤",
        "Workflow steps",
        "手順数",
        "Étapes du workflow",
        "Шаги сценария",
    ),
    "max_parallel": (
        "最大并发分支",
        "Maximum parallel branches",
        "最大並列数",
        "Branches parallèles",
        "Параллельные ветви",
    ),
    "independent_branches": (
        "作为独立分支执行",
        "Run as independent branches",
        "独立分岐で実行",
        "Exécuter indépendamment",
        "Независимые ветви",
    ),
    "actions": (
        "操作尝试",
        "Action attempts",
        "操作試行",
        "Tentatives d’action",
        "Попытки действий",
    ),
    "queries": (
        "目标查询",
        "Target queries",
        "対象検索",
        "Recherches ciblées",
        "Запросы целей",
    ),
    "error": (
        "执行错误",
        "Execution error",
        "実行エラー",
        "Erreur d’exécution",
        "Ошибка выполнения",
    ),
    "cancelled": ("已取消", "Cancelled", "取消済み", "Annulé", "Отменено"),
    "timed_out": (
        "执行超时",
        "Timed out",
        "タイムアウト",
        "Délai dépassé",
        "Время истекло",
    ),
    "inspect": (
        "定位调试",
        "Target inspector",
        "対象の検査",
        "Inspection de cible",
        "Проверка цели",
    ),
    "query": (
        "查找目标",
        "Find target",
        "対象を検索",
        "Rechercher la cible",
        "Найти цель",
    ),
    "not_evaluated": (
        "仅观测，未判定",
        "Observation only",
        "観測のみ",
        "Observation seule",
        "Только наблюдение",
    ),
    "title": (
        "多类型测试",
        "Multi-type testing",
        "複数種のテスト",
        "Tests multi-types",
        "Разные типы тестов",
    ),
    "intro": (
        "创建原生测试计划，统一运行并查看各类型判定和证据。",
        "Create native test plans and inspect typed results and evidence.",
        "ネイティブ計画を作成し、結果と証拠を確認します。",
        "Créer des plans natifs et consulter résultats et preuves.",
        "Создавайте планы и просматривайте результаты и доказательства.",
    ),
    "web": ("Web UI", "Web UI", "Web UI", "Web UI", "Web UI"),
    "performance": (
        "性能测试",
        "Performance",
        "性能テスト",
        "Performance",
        "Производительность",
    ),
    "security": ("安全测试", "Security", "セキュリティ", "Sécurité", "Безопасность"),
    "workflow": ("跨类型编排", "Workflows", "ワークフロー", "Workflows", "Сценарии"),
    "api": (
        "API 请求",
        "API requests",
        "API リクエスト",
        "Requêtes API",
        "Запросы API",
    ),
    "runs": ("执行结果", "Results", "実行結果", "Résultats", "Результаты"),
    "capabilities": (
        "引擎能力",
        "Capabilities",
        "エンジン機能",
        "Capacités",
        "Возможности",
    ),
    "id": ("计划名称", "Plan name", "計画名", "Nom du plan", "Имя плана"),
    "url": ("测试地址", "Target URL", "対象 URL", "URL cible", "Целевой URL"),
    "origins": (
        "授权来源（每行一个，包含协议和端口）",
        "Allowed origins (one per line, include scheme and port)",
        "許可オリジン（1 行ずつ）",
        "Origines autorisées (une par ligne)",
        "Разрешённые источники (по одному в строке)",
    ),
    "save": ("保存计划", "Save plan", "計画を保存", "Enregistrer", "Сохранить"),
    "run": ("运行测试", "Run test", "テスト実行", "Exécuter", "Выполнить"),
    "delete": ("删除计划", "Delete plan", "計画を削除", "Supprimer", "Удалить"),
    "advanced": (
        "高级 JSON 编辑",
        "Advanced JSON editor",
        "JSON 編集",
        "Éditeur JSON",
        "Редактор JSON",
    ),
    "plans": (
        "已保存计划",
        "Saved plans",
        "保存した計画",
        "Plans enregistrés",
        "Сохранённые планы",
    ),
    "empty": ("暂无记录", "No records", "記録なし", "Aucun résultat", "Нет записей"),
    "target": (
        "操作目标选择器",
        "Action target selector",
        "操作対象セレクタ",
        "Sélecteur de cible",
        "Селектор цели",
    ),
    "expected_target": (
        "断言目标选择器",
        "Assertion target selector",
        "検証対象セレクタ",
        "Sélecteur à vérifier",
        "Селектор проверки",
    ),
    "expected": (
        "预期文本",
        "Expected text",
        "期待テキスト",
        "Texte attendu",
        "Ожидаемый текст",
    ),
    "action": ("操作", "Action", "操作", "Action", "Действие"),
    "click": ("点击", "Click", "クリック", "Cliquer", "Нажать"),
    "fill": ("填写", "Fill", "入力", "Remplir", "Ввести"),
    "value": ("输入内容", "Input text", "入力内容", "Texte saisi", "Вводимый текст"),
    "mode": (
        "负载模式",
        "Load mode",
        "負荷モード",
        "Mode de charge",
        "Модель нагрузки",
    ),
    "arrival": (
        "固定到达率",
        "Arrival rate",
        "到着レート",
        "Taux d’arrivée",
        "Интенсивность запросов",
    ),
    "users_mode": (
        "并发用户",
        "Concurrent users",
        "同時ユーザー",
        "Utilisateurs simultanés",
        "Одновременные пользователи",
    ),
    "duration": (
        "持续秒数",
        "Duration in seconds",
        "実行秒数",
        "Durée en secondes",
        "Длительность, с",
    ),
    "users": (
        "用户数 / 最大发送并发",
        "Users / max in flight",
        "ユーザー数 / 最大並行数",
        "Utilisateurs / concurrence max",
        "Пользователи / параллелизм",
    ),
    "rate": (
        "目标每秒请求数",
        "Target requests per second",
        "目標リクエスト毎秒",
        "Requêtes par seconde",
        "Запросов в секунду",
    ),
    "p95": (
        "P95 上限（毫秒）",
        "P95 limit (ms)",
        "P95 上限 (ms)",
        "Limite P95 (ms)",
        "Предел P95 (мс)",
    ),
    "error_ratio": (
        "错误率上限（0–1）",
        "Max error ratio (0–1)",
        "最大エラー率 (0–1)",
        "Taux d’erreur max (0–1)",
        "Доля ошибок (0–1)",
    ),
    "max_requests": (
        "请求总预算",
        "Request budget",
        "リクエスト予算",
        "Budget de requêtes",
        "Бюджет запросов",
    ),
    "rule": ("检查规则", "Rule", "ルール", "Règle", "Правило"),
    "authorization": (
        "身份越权对照",
        "Cross-identity authorization",
        "認可の対照",
        "Contrôle inter-identités",
        "Проверка авторизации",
    ),
    "response_headers": (
        "响应头策略",
        "Response header policy",
        "ヘッダーポリシー",
        "Politique des en-têtes",
        "Политика заголовков",
    ),
    "contract_mutation": (
        "契约变异",
        "Contract mutation",
        "契約の変異",
        "Mutation du contrat",
        "Мутация контракта",
    ),
    "owner_env": (
        "资源所有者 Authorization 环境变量名",
        "Owner Authorization environment variable",
        "所有者 Authorization 環境変数",
        "Variable Authorization du propriétaire",
        "Переменная Authorization владельца",
    ),
    "other_env": (
        "对照身份 Authorization 环境变量名",
        "Challenger Authorization environment variable",
        "比較対象 Authorization 環境変数",
        "Variable Authorization du tiers",
        "Переменная Authorization проверяемого",
    ),
    "marker_path": (
        "资源标记 JSONPath",
        "Resource marker JSONPath",
        "リソース JSONPath",
        "JSONPath du marqueur",
        "JSONPath ресурса",
    ),
    "marker_value": (
        "资源标记预期值（JSON）",
        "Expected resource marker (JSON)",
        "期待マーカー (JSON)",
        "Marqueur attendu (JSON)",
        "Ожидаемый маркер (JSON)",
    ),
    "headers": (
        "必需响应头（JSON）",
        "Required response headers (JSON)",
        "必須ヘッダー (JSON)",
        "En-têtes requis (JSON)",
        "Обязательные заголовки (JSON)",
    ),
    "method": ("请求方法", "Method", "メソッド", "Méthode", "Метод"),
    "json": (
        "请求 JSON（可选）",
        "Request JSON (optional)",
        "JSON（任意）",
        "JSON de requête (facultatif)",
        "JSON запроса (необязательно)",
    ),
    "chain": (
        "依次执行的计划名称（每行一个）",
        "Plan names in order (one per line)",
        "順次実行する計画名",
        "Plans dans l’ordre (un par ligne)",
        "Планы по порядку (по одному в строке)",
    ),
    "cleanup": (
        "最终清理计划名称（可选）",
        "Final cleanup plan (optional)",
        "最終クリーンアップ（任意）",
        "Plan de nettoyage final (facultatif)",
        "Финальная очистка (необязательно)",
    ),
    "details": (
        "详细证据",
        "Evidence details",
        "証拠の詳細",
        "Détails des preuves",
        "Подробности",
    ),
    "draft": ("AI 生成草稿", "AI draft", "AI 下書き", "Brouillon IA", "Черновик ИИ"),
    "goal": (
        "测试目标与明确的预期结果",
        "Goal and explicit expected outcome",
        "目標と明確な期待結果",
        "Objectif et résultat attendu",
        "Цель и ожидаемый результат",
    ),
    "context": (
        "补充上下文（JSON，勿填明文凭据）",
        "Context (JSON, no plaintext credentials)",
        "コンテキスト（JSON、認証情報を除く）",
        "Contexte (JSON, sans identifiants)",
        "Контекст (JSON, без секретов)",
    ),
    "draft_hint": (
        "AI 仅生成经校验的草稿；保存后需单独运行。Web 只观测有限目标。",
        "AI creates a validated draft; run it separately. Web observation returns bounded targets.",
        "AI は検証済み下書きのみ作成します。実行は別操作です。",
        "L’IA crée un brouillon validé ; l’exécution reste séparée.",
        "ИИ создаёт проверенный черновик; запуск выполняется отдельно.",
    ),
    "passed": ("通过", "Passed", "成功", "Réussi", "Пройдено"),
    "failed": ("失败", "Failed", "失敗", "Échec", "Ошибка"),
    "inconclusive": (
        "无法确定",
        "Inconclusive",
        "判定不能",
        "Non concluant",
        "Неопределённо",
    ),
}


_COPY = {**OPERATIONS_COPY, **_COPY}


def tr(key):
    index = {"zh": 0, "en": 1, "ja": 2, "fr": 3, "ru": 4}.get(current_locale(), 0)
    return _COPY[key][index]


def esc(value):
    return html.escape(str(value), quote=True)


def metric_cards(run):
    if run["kind"] == "client_performance":
        from iatest.web.operations import tr as operation_tr

        cards = []
        for key, metric in run.get("metrics", {}).get("measurements", {}).items():
            name = operation_tr(key) if key in OPERATIONS_COPY else key
            value = metric["max"]
            display = (
                f"{value:.2%}"
                if key in {"jank_ratio", "hitch_ratio"}
                else f"{value:,.2f} {metric['unit']}"
            )
            cards.append(
                f"<div><dt>{esc(name)} · {esc(operation_tr('maximum'))}</dt><dd>{esc(display)}</dd><small>{esc(operation_tr('samples'))}: {metric['samples']}</small></div>"
            )
        body = '<dl class="testing-metrics">' + "".join(cards) + "</dl>"
        if run.get("metrics", {}).get("not_assessed"):
            names = [
                operation_tr(k) if k in OPERATIONS_COPY else k
                for k in run["metrics"]["not_assessed"]
            ]
            body += f"<p>{esc(tr('not_assessed'))}: {esc(', '.join(names))}</p>"
        return body
    fields = {
        "performance": [
            "requests",
            "completed",
            "rps",
            "p95_ms",
            "error_ratio",
            "dropped",
        ],
        "security": ["checks", "assessed", "not_assessed", "findings"],
        "workflow": ["nodes", "requests"],
        "api": ["requests"],
        "web": ["actions", "queries"],
        **{
            kind: ["checks", "assessed", "not_assessed"]
            for kind in ("data", "protocol", "accessibility")
        },
    }.get(run["kind"], [])
    parts = []
    for key in fields:
        value = run.get("metrics", {}).get(key)
        if not isinstance(value, (int, float)):
            continue
        display = (
            f"{value:.2%}"
            if key == "error_ratio"
            else f"{value:,.2f}".rstrip("0").rstrip(".")
            if isinstance(value, float)
            else str(value)
        )
        parts.append(
            f"<div><dt>{esc(tr('error_ratio_metric' if key == 'error_ratio' else key))}</dt><dd>{esc(display)}</dd></div>"
        )
    return '<dl class="testing-metrics">' + "".join(parts) + "</dl>"


def form(action, project, content, *, task=""):
    attrs = f' data-background-task data-task-action="{task}"' if task else ""
    return f'<form method="post" action="/actions/testing-{action}" class="mobile-form"{attrs}><input type="hidden" name="project" value="{esc(project)}">{content}</form>'


def field(name, label, value="", kind="text", required=True):
    return f'<label>{esc(tr(label))}<input name="{name}" type="{kind}" value="{esc(value)}" {"step=any" if kind == "number" else ""} {"required" if required else ""}></label>'


def browser_fields():
    return (
        '<label>Browser<select name="browser"><option value="chromium">Chromium</option><option value="firefox">Firefox</option><option value="webkit">Safari / WebKit</option></select></label>'
        + f'<label>{esc(tr("network_policy"))}<select name="network_policy"><option value="strict">{esc(tr("network_strict"))}</option><option value="navigation_only">{esc(tr("network_navigation"))}</option></select></label>'
        + f'<p class="muted">{esc(tr("network_navigation_hint"))}</p>'
    )


def render_testing(root, project, parsed):
    from iatest.web.server import page
    from iatest.web.workspace import (
        FEATURE_AREAS,
        FEATURES,
        area_links,
        feature_guide,
        feature_title,
        render_catalog,
        testing_navigation,
    )
    store = EngineStore(root, project)
    query = parse_qs(parsed.query)
    manual = query.get("method") == ["manual"]
    tab = query.get("tab", ["catalog"])[0]
    tabs = (*FEATURES, *FEATURE_AREAS)
    if tab not in tabs:
        return render_catalog(root, project, parsed)
    if tab == "mobile":
        from urllib.parse import urlparse

        from iatest.web.mobile import render_mobile
        return render_mobile(root, project, urlparse("/mobile?" + urlencode({"project":project,"tab":"plans"})))
    area = FEATURE_AREAS.get(tab, "testing")
    render_page = page
    def page(root, project, title, body):
        return render_page(root, project, feature_title(tab), body, area=area)
    navigation = testing_navigation(project,tab,manual=manual,platform=(query.get("platform") or [""])[0]) if area == "testing" else area_links(project,area,tab)
    from iatest.web.goal_workspace import (
        creation_modes,
        render_goal_workspace,
        selected_kind,
    )
    if tab in {"web", "desktop", "exploration"} and not (manual and tab != "exploration"):
        kind = selected_kind(query, "web") if tab == "exploration" else tab
        return page(root,project,feature_title(tab),'<div class="testing-workspace">'+navigation+render_goal_workspace(root,project,kind)+'</div>')
    body = '<div class="testing-workspace">' + navigation
    if tab in {"web","desktop"}: body += creation_modes(project,tab,manual=True)
    body += feature_guide(project,tab)
    if tab in OPERATIONS_TABS:
        from iatest.web.operations import render_operations

        body += render_operations(root, project, tab)
    elif tab == "exploration":
        body += render_exploration(project, store)
    elif tab == "analysis":
        body += render_analyses(store)
    elif tab == "capabilities":
        body += f'<section><p>{esc(tr("readiness_hint"))}</p></section>'
        for kind, capability in (
            create_engine_registry(root, project).capabilities().items()
        ):
            from iatest.web.workspace import text
            status = text("工具可用", "Available") if capability.get("available") is True else text("请检查环境要求", "Check environment requirements")
            title = feature_title(kind) if kind in FEATURES else kind
            body += f'<section><h2>{esc(title)}</h2><p class="badge">{esc(status)}</p><details><summary>{esc(tr("details"))}</summary><pre data-i18n-skip>{esc(json.dumps(capability, ensure_ascii=False, indent=2))}</pre></details></section>'
    elif tab == "runs":
        for run in store.runs():
            verdict = run["verdict"]
            body += f'<section data-run-id="{esc(run["run_id"])}" data-plan-id="{esc(run["plan_id"])}"><div class="section-head"><h2>{esc(run["plan_id"])}</h2><span class="testing-verdict" data-verdict="{esc(verdict)}">{esc(tr(verdict) if verdict in _COPY else verdict)}</span></div><p>{esc(tr(run["kind"]) if run["kind"] in _COPY else run["kind"])} · {esc(tr(run["lifecycle"]) if run["lifecycle"] in _COPY else run["lifecycle"])} · {esc(run["created_at"])}</p>'
            if run.get("error"):
                body += f'<p role="alert">{esc(run["error"])}</p>'
            body += metric_cards(run)
            if run["kind"] in {
                "performance",
                "security",
                "client_performance",
                "data",
                "protocol",
                "accessibility",
            }:
                fields = (
                    f'<input type="hidden" name="run_id" value="{esc(run["run_id"])}">'
                    + field("baseline", "baseline", required=False)
                )
                fields += f'<label>{esc(tr("telemetry"))}<textarea name="observations" rows="2" data-i18n-skip></textarea></label><button>{esc(tr("analysis"))}</button>'
                body += form("analyze", project, fields, task="testing-analyze")
            for evidence in run.get("evidence", []):
                if evidence.get("type") in {"screenshot", "download"}:
                    url = f"/testing-evidence/{evidence.get('run_id') or run['run_id']}/{evidence['name']}?{urlencode({'project': project})}"
                    if evidence.get("type") == "download":
                        body += f'<a class="button ghost" href="{esc(url)}" download>{esc(evidence.get("filename", "download.bin"))}</a>'
                    else:
                        body += f'<a href="{esc(url)}" target="_blank" rel="noreferrer"><img class="testing-evidence" src="{esc(url)}" alt="{esc(tr("details"))}" loading="lazy"></a>'
            body += f"<details><summary>{esc(tr('details'))}</summary><pre data-i18n-skip>{esc(json.dumps(run, ensure_ascii=False, indent=2))}</pre></details></section>"
        if not store.runs(1):
            body += f"<section>{esc(tr('empty'))}</section>"
    else:
        plans = [p for p in store.plans() if p["kind"] == tab]
        body += f"<section><h2>{esc(tr('plans'))}</h2>"
        if not plans:
            from iatest.web.workspace import text
            body += '<p class="empty-state">' + esc(text("还没有计划。使用下方表单填写测试目标与预期，再保存。", "No plans yet. Configure a target and expected outcome below, then save.")) + '</p>'
        for plan in plans:
            body += f'<article class="testing-plan" id="plan-{esc(plan["id"])}"><h3>{esc(plan.get("title") or plan["id"])}</h3><div class="button-row">'
            body += form(
                "run",
                project,
                f'<input type="hidden" name="plan_id" value="{esc(plan["id"])}"><button>{esc(tr("run"))}</button>',
                task="testing-run",
            )
            body += (
                form(
                    "delete",
                    project,
                    f'<input type="hidden" name="plan_id" value="{esc(plan["id"])}"><input type="hidden" name="kind" value="{tab}"><button class="ghost">{esc(tr("delete"))}</button>',
                )
                + "</div>"
            )
            body += (
                f"<details><summary>{esc(tr('advanced'))}</summary>"
                + form(
                    "save",
                    project,
                    f'<textarea name="content" rows="16" class="code-input" data-i18n-skip>{esc(json.dumps(plan, ensure_ascii=False, indent=2))}</textarea><button>{esc(tr("save"))}</button>',
                )
                + "</details></article>"
            )
        body += "</section>"
        if tab in {"data", "protocol", "accessibility", "desktop"}:
            from iatest.web.plan_forms import guided_plan_form
            from iatest.web.workspace import text
            body += guided_plan_form(project, tab)
            body += "<section><details><summary>" + esc(text("高级配置：完整计划、描述符与人工复核", "Advanced: full plans, descriptors and manual reviews")) + "</summary><p>" + esc(text("仅在基础表单不覆盖目标场景时使用。完整字段见使用文档；以下是可编辑示例，需替换目标和预期。", "Use when the basic form does not cover your scenario. See the guide for full fields and replace example targets and expectations.")) + "</p>"
            if tab == "protocol":
                sample = {
                    "echo.proto": 'syntax="proto3"; package demo; message Echo { string text=1; } service EchoService { rpc Call(Echo) returns(Echo); }'
                }
                body += form(
                    "proto-compile",
                    project,
                    '<input type="hidden" name="kind" value="protocol"><label>'
                    + esc(tr("proto_sources"))
                    + '<textarea name="sources" rows="5" required data-i18n-skip>'
                    + esc(json.dumps(sample, indent=2))
                    + "</textarea></label><button>"
                    + esc(tr("proto_compile"))
                    + "</button>",
                )
                for artifact in store.artifacts("protobuf"):
                    body += (
                        "<details><summary>Protobuf "
                        + esc(artifact["id"])
                        + '</summary><textarea readonly rows="6" data-i18n-skip>'
                        + esc(json.dumps(artifact["descriptor"], indent=2))
                        + "</textarea></details>"
                    )
            if tab == "accessibility":
                body += form(
                    "accessibility-review",
                    project,
                    f'<textarea name="content" rows="10" required data-i18n-skip>{{"id":"review","start_url":"https://test.example/","build_sha256":"replace-with-audit-build-digest","reviews":[]}}</textarea><button>{esc(tr("save"))}</button>',
                )
                for artifact in store.artifacts("accessibility_review"):
                    body += f"<pre data-i18n-skip>{esc(json.dumps(artifact, ensure_ascii=False))}</pre>"
            from iatest.engines.checks import example_plan

            example = (
                example_plan(tab)
                if tab != "desktop"
                else {
                    "id": "desktop-example",
                    "kind": "desktop",
                    "spec": {
                        "platform": "macos",
                        "process_id": 1234,
                        "app_id": "com.example.App",
                        "steps": [
                            {"op": "click", "target": {"identifier": "save"}},
                            {
                                "op": "expect",
                                "target": {"identifier": "status"},
                                "attribute": "value",
                                "expected": "Saved",
                            },
                        ],
                    },
                }
            )
            body += (
                f"<section><h2>{esc(tr('save'))}</h2>"
                + form(
                    "save",
                    project,
                    f'<textarea name="content" rows="20" class="code-input" required data-i18n-skip>{esc(json.dumps(example, ensure_ascii=False, indent=2))}</textarea><button>{esc(tr("save"))}</button>',
                )
                + "</section>"
            )
            return page(root, project, "多类型测试", body + "</details></section></div>")
        inputs = f'<input type="hidden" name="kind" value="{tab}">' + field("id", "id")
        if tab == "security":
            body += "<details><summary>" + esc(tr("advanced")) + "</summary>"
            body += form(
                "security-library",
                project,
                f'<textarea name="content" rows="12" required data-i18n-skip>{{"id":"catalogue","version":"1","source":"project reviewed rules","rules":[]}}</textarea><button>{esc(tr("save"))}</button>',
            )
            for artifact in store.artifacts("security_library"):
                body += f"<pre data-i18n-skip>{esc(json.dumps({k: artifact[k] for k in ('id', 'version', 'sha256')}, ensure_ascii=False))}</pre>"
            body += "</details>"
        if tab == "workflow":
            from iatest.web.plan_forms import (
                input_field,
                saved_options,
                select_field,
            )
            from iatest.web.workspace import text
            options = saved_options(store, include_mobile=True)
            inputs += '<fieldset class="plan-checklist"><legend>' + esc(text("依次执行的计划（按显示顺序）", "Plans to run in the displayed order")) + '</legend>'
            inputs += ''.join(f'<label><input type="checkbox" name="selected_plans" value="{esc(value)}">{esc(title)}</label>' for value,title in options) + '</fieldset>'
            if not options: inputs += '<p class="empty-state">' + esc(text("请先在其它测试场景中保存子计划。", "Save child plans in other scenarios first.")) + '</p>'
            inputs += select_field("cleanup", "最后执行的清理计划（可选）", "Cleanup plan (optional)", [("", text("不设置", "None")), *options], required=False)
            inputs += input_field("max_parallel", "最多并行计划数", "Maximum parallel plans", "1", kind="number")
            inputs += f'<label><input type="checkbox" name="independent_branches">{esc(tr("independent_branches"))}</label>'
            inputs += '<details><summary>' + esc(text("手动指定执行顺序", "Specify a custom order")) + '</summary><p>' + esc(text("每行一个已有计划标识。填写后优先使用此顺序，忽略上方勾选。", "One saved plan ID per line. This overrides the selected plans above.")) + '</p><textarea name="chain" rows="5"></textarea></details>'
        else:
            inputs += field("url", "url", kind="url")
            inputs += f'<label>{esc(tr("origins"))}<textarea name="origins" rows="2" required></textarea></label>'
            if tab == "web":
                inputs += browser_fields()
                inputs += field("selector", "target")
                inputs += f'<label>{esc(tr("action"))}<select name="op"><option value="click">{esc(tr("click"))}</option><option value="fill">{esc(tr("fill"))}</option></select></label>'
                inputs += (
                    field("value", "value", required=False)
                    + field("expected_selector", "expected_target")
                    + field("expected", "expected")
                )
            elif tab in {"performance", "api"}:
                inputs += (
                    "<label>"
                    + esc(tr("method"))
                    + '<select name="method"><option>GET</option><option>POST</option><option>PUT</option><option>DELETE</option></select></label>'
                )
                inputs += f'<label>{esc(tr("json"))}<textarea name="json" rows="3" data-i18n-skip></textarea></label>'
                if tab == "api":
                    from iatest.web.plan_forms import input_field
                    inputs += input_field("expected_status", "预期 HTTP 状态码", "Expected HTTP status", "200", kind="number")
                if tab == "performance":
                    inputs += f'<label>{esc(tr("mode"))}<select name="mode"><option value="arrival_rate">{esc(tr("arrival"))}</option><option value="users">{esc(tr("users_mode"))}</option></select></label>'
                    for name, key, default in (
                        ("duration", "duration", "10"),
                        ("users", "users", "4"),
                        ("rate", "rate", "5"),
                        ("p95", "p95", "1000"),
                        ("error_ratio", "error_ratio", "0.01"),
                    ):
                        inputs += field(name, key, default, "number")
            elif tab == "security":
                inputs += f'<label>{esc(tr("rule"))}<select name="rule"><option value="authorization">{esc(tr("authorization"))}</option><option value="response_headers">{esc(tr("response_headers"))}</option></select></label>'
                inputs += field("owner_env", "owner_env", required=False) + field(
                    "other_env", "other_env", required=False
                )
                inputs += field(
                    "marker_path", "marker_path", "$.id", required=False
                ) + field(
                    "marker_value", "marker_value", '"resource-1"', required=False
                )
                inputs += f'<label>{esc(tr("headers"))}<textarea name="headers" rows="3" data-i18n-skip>{{"x-content-type-options":"nosniff"}}</textarea></label>'
            inputs += field("max_requests", "max_requests", "1000", "number")
        inputs += f"<button>{esc(tr('save'))}</button>"
        body += (
            f"<section><h2>{esc(tr('save'))}</h2>"
            + form("build", project, inputs)
            + "</section>"
        )
        if tab == "web":
            observation = (
                browser_fields()
                + field("url", "url", kind="url")
                + f'<label>{esc(tr("origins"))}<textarea name="origins" required></textarea></label>'
                + field("selector", "target")
            )
            observation += f"<button>{esc(tr('query'))}</button>"
            body += (
                f"<section><h2>{esc(tr('inspect'))}</h2>"
                + form("inspect", project, observation, task="testing-inspect")
                + "</section>"
            )
        if tab != "workflow":
            context = {
                "allowed_origins": [],
                **({"start_url": ""} if tab == "web" else {}),
            }
            ai = f'<input type="hidden" name="kind" value="{tab}"><label>{esc(tr("goal"))}<textarea name="goal" rows="3" maxlength="8000" required></textarea></label><label>{esc(tr("context"))}<textarea name="context" rows="6" class="code-input" data-i18n-skip>{esc(json.dumps(context, ensure_ascii=False, indent=2))}</textarea></label><button>{esc(tr("draft"))}</button>'
            body += (
                f"<section><details><summary>{esc(tr('draft'))}</summary><p>{esc(tr('draft_hint'))}</p>"
                + form("draft", project, ai, task="testing-draft")
                + "</details></section>"
            )
        body += (
            f"<section><details><summary>{esc(tr('advanced'))}</summary>"
            + form(
                "save",
                project,
                f'<textarea name="content" rows="12" class="code-input" required data-i18n-skip></textarea><button>{esc(tr("save"))}</button>',
            )
            + "</details></section>"
        )
    return page(root, project, "多类型测试", body + "</div>")


def render_exploration(project, store):
    from iatest.web.goal_workspace import render_goal_workspace
    return render_goal_workspace(store.root,project)


def render_analyses(store):
    body = ""
    for item in store.artifacts("analysis"):
        analysis = item["analysis"]
        body += f'<section data-analysis-id="{esc(item["id"])}"><h2>{esc(item["source_run_id"])}</h2><p>{esc(item["original_verdict"])}</p><p>{esc(analysis["summary"])}</p><h3>{esc(tr("interpretations"))}</h3>'
        for row in analysis["interpretations"]:
            body += f"<p>{esc(row['uncertainty'])} · {esc(row['explanation'])} <small>{esc(', '.join(row['evidence_refs']))}</small></p>"
        body += f"<h3>{esc(tr('recommendations'))}</h3>"
        for row in analysis["recommendations"]:
            body += (
                f"<article><h4>{esc(row['priority'])} · {esc(row['action'])}</h4><p>{esc(row['expected_effect'])}</p><ol>"
                + "".join(f"<li>{esc(v)}</li>" for v in row["verification"])
                + f"</ol><small>{esc(', '.join(row['evidence_refs']))}</small></article>"
            )
        body += "".join(f"<p>{esc(v)}</p>" for v in analysis["limitations"])
        body += f"<details><summary>{esc(tr('facts'))}</summary><pre data-i18n-skip>{esc(json.dumps(item, ensure_ascii=False, indent=2))}</pre></details></section>"
    return body or f"<section>{esc(tr('empty'))}</section>"


def build_plan(store, data):
    get = lambda k, default="": data.get(k, [default])[0]
    kind = get("kind")
    from iatest.web.plan_forms import saved_plan
    if kind in {"data", "protocol", "accessibility", "desktop"}:
        from iatest.web.plan_forms import build_guided_plan
        return build_guided_plan(kind, get)
    if kind in {"remote", "client_performance"}:
        child = saved_plan(store, get("plan_id"))
        spec = {"plan": child}
        if kind == "remote":
            spec["node_id"] = get("node_id")
        else:
            spec.update(platform=child["kind"], iterations=int(get("iterations", "3")), warmup=int(get("warmup", "1")), thresholds={get("metric"): float(get("threshold"))})
        return {"id": get("id"), "kind": kind, "spec": spec, "budget": {"timeout_seconds": 120}}
    plan = {
        "id": get("id"),
        "kind": kind,
        "spec": {},
        "budget": {
            "timeout_seconds": 120,
            "max_requests": int(get("max_requests", "1000")),
        },
    }
    if kind == "workflow":
        nodes = []
        chain = get("chain").splitlines() if get("chain").strip() else data.get("selected_plans", [])
        for index, name in enumerate(chain):
            if not name.strip():
                continue
            node = {
                "id": f"step{index}",
                "plan": saved_plan(store, name.strip()),
                "depends_on": [nodes[-1]["id"]]
                if nodes and not get("independent_branches")
                else [],
            }
            nodes.append(node)
        if get("cleanup").strip():
            nodes.append(
                {
                    "id": "cleanup",
                    "plan": saved_plan(store, get("cleanup").strip()),
                    "depends_on": [n["id"] for n in nodes],
                    "always": True,
                    "resource_owner": nodes[0]["id"] if nodes else "cleanup",
                }
            )
        plan["spec"] = {"nodes": nodes, "max_parallel": int(get("max_parallel", "1"))}
        return plan
    origins = [line.strip() for line in get("origins").splitlines() if line.strip()]
    request = {"url": get("url"), "method": get("method", "GET")}
    if get("json").strip():
        request["json"] = json.loads(get("json"))
    spec = {"allowed_origins": origins}
    if kind == "web":
        spec.update(
            browser=get("browser", "chromium") or "chromium",
            network_policy=get("network_policy", "strict") or "strict",
            start_url=get("url"),
            steps=[
                {
                    "op": get("op"),
                    "target": {"selector": get("selector")},
                    **({"value": get("value")} if get("op") == "fill" else {}),
                },
                {
                    "op": "expect",
                    "target": {"selector": get("expected_selector")},
                    "assert": "text",
                    "expected": get("expected"),
                },
                {"op": "screenshot"},
            ],
        )
    elif kind == "performance":
        spec.update(
            mode=get("mode"),
            duration_seconds=float(get("duration")),
            users=int(get("users")),
            rate=float(get("rate", "1") or "1"),
            request=request,
            thresholds={
                "p95_ms": float(get("p95")),
                "max_error_ratio": float(get("error_ratio")),
                "max_dropped_ratio": 0,
            },
        )
    elif kind == "api":
        request["expected_status"] = [int(get("expected_status", "200"))]
        spec["requests"] = [request]
    elif kind == "security":
        check = {"id": "check-1", "rule": get("rule"), "request": request}
        if get("rule") == "authorization":
            spec["identities"] = {
                "owner": {"Authorization": {"$env": get("owner_env")}},
                "challenger": {"Authorization": {"$env": get("other_env")}},
            }
            check.update(
                owner="owner",
                challenger="challenger",
                marker={
                    "path": get("marker_path"),
                    "expected": json.loads(get("marker_value")),
                },
            )
        else:
            check["required_headers"] = json.loads(get("headers"))
        spec["checks"] = [check]
    plan["spec"] = spec
    return plan


def handle_testing_action(root, project, path, data, handler):
    from iatest.web.server import start_task

    store = EngineStore(root, project)
    action = path.removeprefix("/actions/testing-")
    from iatest.web.operations import ACTIONS, handle_operation

    if action in ACTIONS:
        return handle_operation(root, project, action, data, handler)
    get = lambda k, default="": data.get(k, [default])[0]
    tab = get("kind", "web")
    if action == "proto-compile":
        from iatest.engines.protobuf import compile_sources

        descriptor = compile_sources(json.loads(get("sources")))
        artifact_id = "protobuf-" + descriptor["sha256"]
        value = {"id": artifact_id, "descriptor": descriptor}
        try:
            existing = store.get_artifact("protobuf", artifact_id)
        except ValueError:
            store.save_artifact("protobuf", value, artifact_id=artifact_id)
        else:
            if existing != value:
                raise ValueError("Protobuf artifact identity collision")
        tab = "protocol"
    elif action == "save":
        value = json.loads(get("content"))
        store.save_plan(value)
        tab = value["kind"]
    elif action == "build":
        value = build_plan(store, data)
        store.save_plan(value)
    elif action == "delete":
        store.delete_plan(get("plan_id"))
    elif action in {"run", "draft", "inspect", "explore", "analyze"}:
        if action == "analyze":
            store.get_result(get("run_id"))
            payload = {
                "run_id": get("run_id"),
                "baseline_id": get("baseline"),
                "telemetry": json.loads(get("observations"))
                if get("observations").strip()
                else None,
            }
            tab = "analysis"
        elif action == "explore":
            from iatest.web.goal_workspace import exploration_payload
            payload = exploration_payload(root,project,data)
            tab = "exploration"
        elif action == "run":
            store.get_plan(get("plan_id"))
            payload = {"plan_id": get("plan_id")}
            tab = "runs"
        elif action == "draft":
            payload = {
                "kind": tab,
                "goal": get("goal"),
                "context": json.loads(get("context")),
            }
        else:
            payload = {
                "spec": {
                    "browser": get("browser", "chromium") or "chromium",
                    "network_policy": get("network_policy", "strict") or "strict",
                    "allowed_origins": [
                        v.strip() for v in get("origins").splitlines() if v.strip()
                    ],
                    "start_url": get("url"),
                },
                "target": {"selector": get("selector")},
            }
            tab = "runs"
        if not start_task(
            root,
            project,
            "testing-" + action,
            tr(
                {
                    "run": "run",
                    "draft": "draft",
                    "inspect": "inspect",
                    "explore": "explore",
                    "analyze": "analysis",
                }[action]
            ),
            kind="testing-" + action,
            payload=payload,
            resumable=False,
        ):
            raise ValueError("A testing task is already running")
    else:
        raise ValueError("Unknown testing action")
    redirect_query = {"project":project,"tab":tab}
    if action == "explore": redirect_query["platform"] = payload["kind"]
    elif tab in {"web","desktop"} and action in {"build","save","delete"}: redirect_query["method"] = "manual"
    handler.redirect("/testing?" + urlencode(redirect_query))


def execute_testing_task(root, project, kind, payload, progress):
    from iatest.application.testing_service import (
        draft_test_plan,
        execute_test_plan,
        inspect_web_target,
    )

    if kind == "testing-analyze":
        from iatest.application.engine_analysis import analyze_engine_run

        progress("Interpreting immutable execution evidence", stage="llm")
        result = analyze_engine_run(
            root,
            project,
            payload["run_id"],
            baseline_id=payload.get("baseline_id", ""),
            telemetry=payload.get("telemetry"),
        )
        return "Analysis saved: " + result["id"]
    if kind == "testing-explore":
        from iatest.application.ui_exploration import explore_ui_goal

        progress(
            "Understanding goal, exploring and independently replaying", stage="llm"
        )
        result = explore_ui_goal(
            root,
            project,
            payload["kind"],
            payload["goal"],
            payload["context"],
            force=payload.get("force", False),
        )
        progress("Case and exploration evidence saved", stage="report")
        return (
            result["id"]
            + ": "
            + result["status"]
            + (": " + result["error"] if result.get("error") else "")
        )

    if kind == "testing-inspect":
        progress("Observing a bounded web target", stage="execute")
        result = inspect_web_target(root, project, payload["spec"], payload["target"])
        return result["run_id"] + ": observation only"

    if kind == "testing-draft":
        progress("Drafting typed plan from bounded evidence", stage="llm")
        result = draft_test_plan(
            root, project, payload["kind"], payload["goal"], payload["context"]
        )
        return "Draft saved: " + result["plan"]["id"]
    progress("Executing native typed plan", stage="execute")
    result = execute_test_plan(root, project, payload["plan_id"])
    progress("Typed result and evidence saved", stage="report")
    return f"{result['run_id']}: {result['verdict']}" + (
        ": " + result["error"] if result["error"] else ""
    )
