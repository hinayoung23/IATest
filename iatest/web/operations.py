"""Business testing assets, delivery, client metrics, nodes and release tasks."""

from __future__ import annotations

import base64
import json
import uuid
from urllib.parse import urlencode

from iatest.engines.store import EngineStore
from iatest.web.i18n import current_locale

COPY = {
    "ios_isolated_capture": (
        "iOS 单进程附加兼容采集",
        "iOS isolated recorder attach",
        "iOS 単一プロセスの互換記録",
        "Attachement iOS avec lanceur isolé",
        "Совместимое подключение к процессу iOS",
    ),
    "ios_isolated_hint": (
        "目标 App 须已运行；临时复制并校验本机签名工具，执行单进程附加，不重启 App。可将 template 改为 frame_lifetimes 精简采集原始帧区间，结束后删除工具副本。",
        "Requires a running App. Attaches through a verified temporary signed recorder without restarting the App. Select frame_lifetimes for frame-only recording; the copy is removed afterward.",
        "実行中 App に再起動せず接続します。署名と同一性を検証した一時ツールは終了後に削除します。",
        "Se connecte sans redémarrage via une copie temporaire vérifiée de l’outil signé, supprimée après la capture.",
        "Подключение без перезапуска через проверенную временную копию подписанного инструмента; копия удаляется после записи.",
    ),
    "ios_existing_capture": (
        "iOS 已有进程统计",
        "Observe an existing iOS process",
        "実行中 iOS プロセス統計",
        "Observer un processus iOS actif",
        "Наблюдение процесса iOS",
    ),
    "ios_existing_hint": (
        "此模式不重启 App；临时录制设备进程资源统计，只保存核验过的目标 PID 指标。原始统计在结束后清理。",
        "Keeps the App running. Temporarily records device process resource statistics and retains only the verified target PID metrics; raw statistics are removed afterward.",
        "App を再起動せず、端末プロセス統計から検証済み PID の値だけを保存します。",
        "Ne redémarre pas l’app ; conserve uniquement les métriques du PID vérifié.",
        "Без перезапуска приложения; сохраняются метрики только проверенного PID.",
    ),
    "ios_frame_capture": (
        "iOS 原始帧区间采集",
        "Record iOS frame intervals",
        "iOS フレーム区間を記録",
        "Capturer les intervalles iOS",
        "Запись интервалов кадров iOS",
    ),
    "ios_frame_hint": (
        "示例仅采集原始帧区间，减少无关采集数据；CPU 和内存使用单独的进程统计计划。请按业务要求填写阈值。",
        "Records frame intervals with fewer unrelated observations. Use a separate process-statistics plan for CPU and memory. Set thresholds for your workload.",
        "フレーム区間のみを記録します。CPU・メモリは別の統計計画で測定し、業務に適したしきい値を設定してください。",
        "Capture les intervalles de trame. Mesurez CPU et mémoire séparément et adaptez les seuils à votre activité.",
        "Записывает интервалы кадров. CPU и память измеряются отдельным планом; задайте пороги для своей нагрузки.",
    ),
    "frame_lifetime_ms": (
        "iOS 原始帧区间时长",
        "iOS observed frame lifetime",
        "iOS フレーム区間",
        "Durée de trame observée iOS",
        "Наблюдаемая длительность кадра iOS",
    ),
    "ios_calibrate": (
        "iOS 滚动采集器校准",
        "Calibrate iOS scroll collector",
        "iOS スクロール校正",
        "Étalonner le défilement iOS",
        "Калибровка прокрутки iOS",
    ),
    "ios_launch_capture": (
        "iOS 启动并采集示例",
        "iOS launch recording example",
        "iOS 起動記録例",
        "Exemple de capture iOS",
        "Пример записи запуска iOS",
    ),
    "ios_launch_hint": (
        "启动采集会重启指定 App，采集结束后关闭本次启动的进程；不会清除 App 数据。",
        "Launch recording restarts the selected App and stops its owned process afterward; App data is retained.",
        "指定 App を再起動し、記録後にそのプロセスを終了します。",
        "La capture redémarre l’app choisie puis arrête son processus.",
        "Запись перезапускает выбранное приложение и затем завершает его процесс.",
    ),
    "stop": ("停止", "Stop", "停止", "Arrêter", "Остановить"),
    "shared_resources": (
        "跨控制端资源协调",
        "Shared resource authority",
        "共有リソース調整",
        "Coordination des ressources",
        "Общая координация ресурсов",
    ),
    "shared_unavailable": (
        "资源协调服务暂不可用",
        "Shared resource authority unavailable",
        "リソース調整サービスに接続できません",
        "Service de coordination indisponible",
        "Сервис координации недоступен",
    ),
    "ios_measure": (
        "iOS 原生性能测量",
        "iOS native measurement",
        "iOS ネイティブ計測",
        "Mesure native iOS",
        "Нативные измерения iOS",
    ),
    "windows_bundle": (
        "生成 Windows 节点启动包",
        "Build Windows node bundle",
        "Windows ノードパッケージ",
        "Créer le paquet Windows",
        "Создать пакет узла Windows",
    ),
    "memory_footprint_kb": (
        "iOS 物理内存占用",
        "iOS physical footprint",
        "iOS 物理メモリ",
        "Empreinte physique iOS",
        "Физическая память iOS",
    ),
    "resident_memory_kb": (
        "驻留内存",
        "Resident memory",
        "常駐メモリ",
        "Mémoire résidente",
        "Резидентная память",
    ),
    "ios_launch_ms": (
        "iOS 启动测量",
        "iOS launch measurement",
        "iOS 起動測定",
        "Mesure de démarrage iOS",
        "Измерение запуска iOS",
    ),
    "frame_duration_ms": (
        "帧耗时测量",
        "Frame duration",
        "フレーム時間",
        "Durée de trame",
        "Время кадра",
    ),
    "hitch_ratio": (
        "停顿比例",
        "Hitch ratio",
        "停止比率",
        "Proportion de pauses",
        "Доля задержек",
    ),
    "ios_import": (
        "导入 iOS 性能测量",
        "Import iOS measurements",
        "iOS 測定をインポート",
        "Importer les mesures iOS",
        "Импорт измерений iOS",
    ),
    "ios_doctor": (
        "检查 iOS 宿主工具链",
        "Inspect iOS host tools",
        "iOS ホストツールを検査",
        "Vérifier les outils iOS",
        "Проверить инструменты iOS",
    ),
    "ios_gate": (
        "创建离线测量门禁计划",
        "Create offline measurement gates",
        "オフライン判定を作成",
        "Créer les seuils hors ligne",
        "Создать офлайн-проверки",
    ),
    "thresholds": (
        "性能门禁 JSON",
        "Performance gates JSON",
        "性能判定 JSON",
        "Seuils JSON",
        "Пороговые значения JSON",
    ),
    "ios_hint": (
        "导入已有 trace、导出 XML 或规范测量 JSON，不操作手机。请声明实际平台；macOS 指标不能用于通过 iOS 门禁。原始 trace 可能含环境凭据，框架只保存白名单测量。",
        "Import existing trace, XML or measurement JSON without device actions. Declare the actual platform; macOS data cannot satisfy iOS gates. Raw traces may contain credentials; only allowed measurements are retained.",
        "既存 trace/XML/JSON を端末操作なしで読み込みます。macOS 測定を iOS 判定に使用しません。",
        "Importez trace/XML/JSON sans action sur l’appareil. Les données macOS ne valident pas les seuils iOS.",
        "Импорт trace/XML/JSON без действий на устройстве. Данные macOS не проходят проверки iOS.",
    ),
    "navigation_ms": (
        "页面导航耗时",
        "Navigation time",
        "ナビゲーション時間",
        "Durée de navigation",
        "Время навигации",
    ),
    "fcp_ms": (
        "首次内容绘制",
        "First contentful paint",
        "初回コンテンツ描画",
        "Premier rendu de contenu",
        "Первая отрисовка содержимого",
    ),
    "lcp_observed_ms": (
        "最大内容绘制观测",
        "Observed largest paint",
        "最大描画の観測",
        "Plus grand rendu observé",
        "Наблюдаемая крупная отрисовка",
    ),
    "long_task_max_ms": (
        "最长主线程任务",
        "Longest main-thread task",
        "最長メインタスク",
        "Tâche principale la plus longue",
        "Самая долгая задача",
    ),
    "event_max_ms": (
        "事件延迟观测",
        "Observed event latency",
        "イベント遅延の観測",
        "Latence d’événement observée",
        "Наблюдаемая задержка событий",
    ),
    "action_max_ms": (
        "宿主操作耗时",
        "Host action time",
        "ホスト操作時間",
        "Durée d’action côté hôte",
        "Время действия хоста",
    ),
    "android_start_total_ms": (
        "Android 平台启动耗时",
        "Android platform start time",
        "Android 起動時間",
        "Démarrage Android",
        "Запуск Android",
    ),
    "memory_pss_kb": (
        "PSS 内存快照",
        "PSS memory snapshot",
        "PSS メモリ",
        "Instantané mémoire PSS",
        "Снимок памяти PSS",
    ),
    "jank_ratio": (
        "卡顿帧比例",
        "Janky frame ratio",
        "遅延フレーム率",
        "Proportion de trames lentes",
        "Доля задержанных кадров",
    ),
    "cpu_percent": (
        "应用 CPU",
        "App CPU",
        "アプリ CPU",
        "CPU de l’app",
        "ЦП приложения",
    ),
    "maximum": ("最大值", "Maximum", "最大値", "Maximum", "Максимум"),
    "samples": ("测量次数", "Measurements", "測定回数", "Mesures", "Измерения"),
    "row": ("行号", "Row", "行", "Ligne", "Строка"),
    "case": ("用例", "Case", "ケース", "Cas", "Тест"),
    "state": ("状态", "State", "状態", "État", "Состояние"),
    "row_error": ("错误", "Error", "エラー", "Erreur", "Ошибка"),
    "assets": (
        "模板与批量用例",
        "Templates & bulk cases",
        "テンプレートと一括ケース",
        "Modèles et import",
        "Шаблоны и импорт",
    ),
    "apps": (
        "安装包交付",
        "App delivery",
        "アプリ配布",
        "Livraison d’app",
        "Доставка приложений",
    ),
    "client_performance": (
        "客户端性能",
        "Client performance",
        "クライアント性能",
        "Performance client",
        "Производительность клиента",
    ),
    "nodes": (
        "执行节点",
        "Execution nodes",
        "実行ノード",
        "Nœuds d’exécution",
        "Узлы выполнения",
    ),
    "campaigns": (
        "版本测试任务",
        "Version test campaigns",
        "バージョンテスト",
        "Campagnes de version",
        "Версионные кампании",
    ),
    "remote": (
        "远程测试",
        "Remote tests",
        "リモートテスト",
        "Tests distants",
        "Удалённые тесты",
    ),
    "preview": (
        "逐行预检",
        "Preview rows",
        "行を検証",
        "Vérifier les lignes",
        "Проверить строки",
    ),
    "commit": (
        "提交导入",
        "Commit import",
        "インポート確定",
        "Confirmer l’import",
        "Подтвердить импорт",
    ),
    "valid_only": (
        "只提交有效行（其余行保留错误）",
        "Commit valid rows only",
        "有効な行のみ確定",
        "Importer les lignes valides",
        "Только допустимые строки",
    ),
    "process": (
        "探索或执行",
        "Explore or execute",
        "探索・実行",
        "Explorer ou exécuter",
        "Исследовать или выполнить",
    ),
    "download": (
        "下载模板",
        "Download templates",
        "テンプレートを取得",
        "Télécharger les modèles",
        "Скачать шаблоны",
    ),
    "goals": (
        "业务目标",
        "Business goals",
        "業務目標",
        "Objectifs métier",
        "Бизнес-цели",
    ),
    "plans": (
        "执行计划",
        "Execution plans",
        "実行計画",
        "Plans d’exécution",
        "Планы выполнения",
    ),
    "defaults": (
        "公共上下文 JSON",
        "Shared context JSON",
        "共通コンテキスト JSON",
        "Contexte commun JSON",
        "Общий контекст JSON",
    ),
    "policy": (
        "同 ID 策略",
        "Same-ID policy",
        "同一 ID の処理",
        "Politique des ID",
        "Правило совпадения ID",
    ),
    "error": (
        "报告冲突",
        "Report conflicts",
        "競合を報告",
        "Signaler les conflits",
        "Сообщать конфликты",
    ),
    "skip": (
        "跳过已有项",
        "Skip existing",
        "既存をスキップ",
        "Ignorer l’existant",
        "Пропускать существующие",
    ),
    "version": (
        "创建新版本",
        "Create new versions",
        "新規バージョン",
        "Créer une version",
        "Создавать версии",
    ),
    "fetch": (
        "获取并校验安装包",
        "Fetch & verify App",
        "アプリを取得・検証",
        "Obtenir et vérifier l’app",
        "Загрузить и проверить",
    ),
    "prepare": (
        "执行安装准备",
        "Prepare installation",
        "インストール準備",
        "Préparer l’installation",
        "Подготовить установку",
    ),
    "enroll": (
        "登记节点",
        "Enroll node",
        "ノード登録",
        "Inscrire un nœud",
        "Зарегистрировать узел",
    ),
    "controller_start": (
        "启动控制服务",
        "Start controller",
        "制御サービス開始",
        "Démarrer le contrôleur",
        "Запустить контроллер",
    ),
    "controller_stop": (
        "停止控制服务",
        "Stop controller",
        "制御サービス停止",
        "Arrêter le contrôleur",
        "Остановить контроллер",
    ),
    "create": (
        "冻结任务快照",
        "Freeze campaign snapshot",
        "計画を固定",
        "Figer la campagne",
        "Зафиксировать кампанию",
    ),
    "analyze": (
        "执行后进行 AI 证据分析",
        "Analyze evidence with AI after execution",
        "実行後に AI 証拠分析",
        "Analyser les preuves après exécution",
        "ИИ-анализ после выполнения",
    ),
    "run": ("执行", "Run", "実行", "Exécuter", "Выполнить"),
    "save": (
        "校验并保存计划",
        "Validate & save plan",
        "検証して保存",
        "Valider et enregistrer",
        "Проверить и сохранить",
    ),
    "details": ("详情", "Details", "詳細", "Détails", "Подробности"),
    "asset_hint": (
        "导入不会调用模型或操作应用。缺少预期结果的用例需要补充；可执行用例直接回放，业务目标用例在主动处理时探索。",
        "Import makes no model calls or App actions. Missing expected outcomes require clarification. Processing replays plans or explores goals.",
        "インポートはモデル呼出しやアプリ操作を行いません。期待結果が必要です。",
        "L’import ne lance aucun modèle ni action. Les attentes manquantes nécessitent une clarification.",
        "Импорт не вызывает модель и не управляет приложением. Нужен ожидаемый результат.",
    ),
    "install_hint": (
        "fresh 会卸载已有应用及其数据；upgrade 保留数据且不会在失败后自动改为全新安装。安装成功后仍需用 UI 断言确认业务就绪。",
        "fresh removes the existing App/data; upgrade preserves data and never falls back to uninstall. Verify readiness through UI assertions.",
        "fresh は既存データを削除します。upgrade 失敗時に再インストールへ切り替えません。",
        "fresh supprime les données ; upgrade les conserve sans repli automatique. Vérifiez l’état par des assertions UI.",
        "fresh удаляет данные; upgrade сохраняет их без автоматической переустановки. Готовность проверяется UI.",
    ),
    "node_hint": (
        "凭据仅填写已有环境变量名。局域网监听必须提供 TLS 证书；工作节点还会独立限制可执行类型与目标范围。下面的 CLI 在对应节点运行。",
        "Use existing environment-variable names for credentials. LAN listeners require TLS. Workers independently constrain test kinds and origins. Run the CLI on each worker.",
        "資格情報は環境変数名を指定。LAN は TLS が必要です。各ワーカーで CLI を実行します。",
        "Utilisez des références d’environnement. TLS est requis sur le LAN. Exécutez le CLI sur chaque nœud.",
        "Используйте имена переменных среды. Для LAN нужен TLS. Запустите CLI на каждом узле.",
    ),
    "client_hint": (
        "浏览器采集导航与绘制；Android 采集启动、PSS 与帧统计；iOS 支持宿主侧 Instruments 采集与历史测量导入，真机兼容性待验收。缺失指标不能通过门禁。",
        "Browser navigation/paint; Android startup/PSS/frames; iOS Instruments host recording and historical imports, pending physical-device acceptance. Missing metrics cannot pass gates.",
        "ブラウザと Android 指標、iOS Instruments と履歴インポート。iOS 実機互換性は未検証です。欠測は合格にしません。",
        "Mesures navigateur/Android et capture Instruments/import iOS. La compatibilité iOS réelle reste à valider. Les mesures absentes ne valident pas un seuil.",
        "Метрики браузера/Android, запись Instruments и импорт iOS. Совместимость с реальным iOS ещё не проверена. Отсутствующие метрики не проходят пороги.",
    ),
    "campaign_hint": (
        "冻结计划、用例版本、安装包哈希与设备配置；显式执行创建独立记录。依赖失败后跳过后续业务步骤，always 清理仍会运行。中断写操作不会自动续跑。",
        "Freeze plans, case versions, App hashes and device profiles. Each explicit run is a new attempt. Failed dependencies block business steps; always cleanup still runs. Interrupted writes never auto-resume.",
        "計画とバージョンを固定。失敗した依存をスキップし、always のクリーンアップを実行します。",
        "Plans et versions figés. Les dépendances bloquées sont ignorées ; le nettoyage always est exécuté.",
        "Планы и версии фиксируются. Сбой зависимостей блокирует шаги, очистка always выполняется.",
    ),
}
TABS = ("assets", "apps", "client_performance", "nodes", "remote", "campaigns")


def tr(key):
    locale = current_locale()
    return COPY[key][{"zh": 0, "en": 1, "ja": 2, "fr": 3, "ru": 4}.get(locale, 0)]


def detail(value, title="details"):
    from iatest.web.testing import esc

    return f"<details><summary>{esc(tr(title))}</summary><pre data-i18n-skip>{esc(json.dumps(value, ensure_ascii=False, indent=2))}</pre></details>"


def json_form(action, project, sample, label, *, task=False, extra=""):
    from iatest.web.testing import esc, form

    return form(
        action,
        project,
        f'<textarea name="content" class="code-input" rows="12" required data-i18n-skip>{esc(json.dumps(sample, ensure_ascii=False, indent=2))}</textarea>{extra}<button>{esc(tr(label))}</button>',
        task="testing-" + action if task else "",
    )


def render_operations(root, project, tab):
    from iatest.application.test_assets import AssetRepository
    from iatest.nodes import services
    from iatest.nodes.store import ControllerStore
    from iatest.web.testing import esc, form

    store = EngineStore(root, project)
    body = ""
    if tab == "assets":
        links = []
        for mode in ("goals", "plans"):
            for fmt in ("csv", "json", "xlsx"):
                links.append(
                    f'<a class="button ghost" href="/testing-template?{urlencode({"mode": mode, "format": fmt, "project": project})}">{esc(tr(mode))} · {fmt.upper()}</a>'
                )
        body += f'<section><h2>{esc(tr("download"))}</h2><p>{esc(tr("asset_hint"))}</p><div class="button-row">{"".join(links)}</div></section>'
        fields = '<input type="file" accept=".csv,.json,.xlsx" data-case-upload required><input type="hidden" name="filename"><input type="hidden" name="encoded">'
        fields += f'<details><summary>{esc(tr("defaults"))}</summary><textarea name="defaults" data-i18n-skip rows="4">{{}}</textarea></details>'
        fields += (
            f'<label>{esc(tr("policy"))}<select name="policy">'
            + "".join(
                f'<option value="{key}">{esc(tr(key))}</option>'
                for key in ("error", "skip", "version")
            )
            + f"</select></label><button>{esc(tr('preview'))}</button>"
        )
        body += "<section>" + form("import-preview", project, fields) + "</section>"
        for batch in store.artifacts("import_batch", 20):
            body += f'<section data-import-id="{esc(batch["id"])}"><h2>{esc(batch["id"])}</h2>'
            body += (
                '<div class="table-wrap"><table><thead><tr>'
                + "".join(
                    f"<th>{esc(tr(key))}</th>"
                    for key in ("row", "case", "state", "row_error")
                )
                + "</tr></thead><tbody>"
            )
            for row in batch["rows"]:
                asset = row.get("asset", {})
                state, error = row["state"], row.get("error", "")
                if state == "imported":
                    latest = AssetRepository(root, project).get(
                        asset["case_id"], asset["version"]
                    )
                    state, error = latest["state"], latest.get("error", "")
                body += f"<tr><td>{esc(row['row'])}</td><td>{esc(asset.get('case_id', ''))}</td><td>{esc(state)}</td><td>{esc(error)}</td></tr>"
            body += "</tbody></table></div>"
            hidden = f'<input type="hidden" name="batch_id" value="{esc(batch["id"])}">'
            if batch["state"] != "imported":
                body += form(
                    "import-commit",
                    project,
                    hidden
                    + f'<label><input type="checkbox" name="valid_only">{esc(tr("valid_only"))}</label><button>{esc(tr("commit"))}</button>',
                )
            else:
                body += form(
                    "import-process",
                    project,
                    hidden + f"<button>{esc(tr('process'))}</button>",
                    task="testing-import-process",
                )
            body += detail(batch) + "</section>"
        for asset in AssetRepository(root, project).list():
            body += f'<section data-case-id="{esc(asset["case_id"])}"><h2>{esc(asset["title"])}</h2><p>{esc(asset["case_id"])} · v{asset["version"]} · {esc(asset["state"])}</p>'
            if asset["state"] not in {"running", "needs_clarification"}:
                body += form(
                    "asset-process",
                    project,
                    f'<input type="hidden" name="case_id" value="{esc(asset["case_id"])}"><input type="hidden" name="version" value="{asset["version"]}"><button>{esc(tr("process"))}</button>',
                    task="testing-asset-process",
                )
            body += detail(asset) + "</section>"
    elif tab == "apps":
        body += (
            f"<section><h2>{esc(tr('fetch'))}</h2>"
            + json_form(
                "app-fetch",
                project,
                {
                    "id": "app-build-2",
                    "platform": "android",
                    "app_id": "com.example.app",
                    "url": "https://build.example.com/app.apk",
                    "allowed_origins": ["https://build.example.com"],
                    "sha256": "0" * 64,
                },
                "fetch",
                task=True,
            )
            + "</section>"
        )
        body += (
            f"<section><h2>{esc(tr('prepare'))}</h2><p>{esc(tr('install_hint'))}</p>"
            + json_form(
                "app-prepare",
                project,
                {
                    "profile": "android-local",
                    "artifact_id": "app-build-2",
                    "mode": "upgrade",
                },
                "prepare",
                task=True,
            )
            + "</section>"
        )
        for kind in ("app", "deployment"):
            for item in store.artifacts(kind):
                body += f"<section><h2>{esc(item['id'])}</h2>{detail(item)}</section>"
    elif tab in {"client_performance", "remote"}:
        child = {
            "id": "web-journey",
            "kind": "web",
            "spec": {
                "start_url": "http://localhost:8000",
                "allowed_origins": ["http://localhost:8000"],
                "steps": [
                    {"op": "expect", "target": {"selector": "h1"}, "assert": "exists"}
                ],
            },
        }
        sample = (
            {
                "id": "client-journey",
                "kind": tab,
                "budget": {"timeout_seconds": 120},
                "spec": {
                    "platform": "web",
                    "plan": child,
                    "iterations": 3,
                    "warmup": 1,
                    "thresholds": {"max_navigation_ms": 2000},
                },
            }
            if tab == "client_performance"
            else {
                "id": "remote-journey",
                "kind": "remote",
                "spec": {"node_id": "worker-a", "plan": child},
                "budget": {"timeout_seconds": 120},
            }
        )
        body += f"<section><h2>{esc(tr(tab))}</h2>"
        if tab == "client_performance":
            body += (
                "<section>"
                + json_form(
                    "ios-measure",
                    project,
                    {
                        "profile": "iphone",
                        "mode": "launch",
                        "configuration_id": "replace-with-build-configuration-id",
                        "bindings": {
                            "ios_launch_ms": {
                                "identifier": "replace-with-exact-xcresult-metric-identifier",
                                "unit": "s",
                            }
                        },
                        "iterations": 3,
                    },
                    "ios_measure",
                    task=True,
                )
                + "</section>"
            )
            body += f"<p>{esc(tr('client_hint'))}</p>"
            body += json_form(
                "ios-measure",
                project,
                {
                    "profile": "iphone-lab",
                    "mode": "scroll",
                    "target_identifier": "item_list",
                    "origin_identifier": "item_1",
                    "configuration_id": "replace-with-build-configuration-id",
                    "iterations": 3,
                    "calibration_id": "replace-with-usable-calibration-id",
                    "bindings": {
                        "hitch_ratio": {
                            "identifier": "com.apple.dt.XCTMetric_OSSignpost-Scroll_DraggingAndDeceleration.animation.hitch.time.ratio",
                            "unit": "ms per s",
                        }
                    },
                },
                "ios_measure",
                task=True,
            )
        body += json_form("save", project, sample, "save") + "</section>"
        if tab == "client_performance":
            body += (
                f"<section><h2>{esc(tr('ios_launch_capture'))}</h2><p>{esc(tr('ios_launch_hint'))}</p>"
                + json_form(
                    "save",
                    project,
                    {
                        "id": "ios-recorded-journey",
                        "kind": "client_performance",
                        "budget": {"timeout_seconds": 240},
                        "spec": {
                            "platform": "ios",
                            "iterations": 1,
                            "warmup": 0,
                            "ios_capture": {
                                "mode": "launch",
                                "duration_seconds": 120,
                                "finalize_timeout_seconds": 45,
                            },
                            "thresholds": {
                                "max_cpu_percent": 100,
                                "max_memory_footprint_kb": 200000,
                            },
                            "plan": {
                                "id": "ios-journey",
                                "kind": "ios",
                                "spec": {
                                    "profile": "iphone",
                                    "steps": [
                                        {
                                            "op": "expect",
                                            "target": {
                                                "identifier": "replace-with-identifier"
                                            },
                                            "attribute": "text",
                                            "expected": "replace-with-expected",
                                        }
                                    ],
                                },
                            },
                        },
                    },
                    "save",
                )
                + "</section>"
            )
            body += (
                f"<section data-ios-frame-recording><h2>{esc(tr('ios_frame_capture'))}</h2><p>{esc(tr('ios_frame_hint'))}</p>"
                + json_form(
                    "save",
                    project,
                    {
                        "id": "ios-frame-journey",
                        "kind": "client_performance",
                        "budget": {"timeout_seconds": 240},
                        "spec": {
                            "platform": "ios",
                            "iterations": 1,
                            "warmup": 0,
                            "ios_capture": {
                                "mode": "launch",
                                "template": "frame_lifetimes",
                                "duration_seconds": 120,
                                "finalize_timeout_seconds": 45,
                            },
                            "thresholds": {"max_frame_lifetime_ms": 50},
                            "plan": {
                                "id": "ios-scroll",
                                "kind": "ios",
                                "spec": {
                                    "profile": "iphone-lab",
                                    "steps": [
                                        {
                                            "op": "expect",
                                            "target": {"identifier": "ready_label"},
                                            "attribute": "text",
                                            "expected": "Ready",
                                            "timeout_ms": 10000,
                                        },
                                        {
                                            "op": "swipe",
                                            "target": {"identifier": "item_list"},
                                            "direction": "up",
                                        },
                                        {
                                            "op": "expect",
                                            "target": {"identifier": "ready_label"},
                                            "attribute": "text",
                                            "expected": "Ready",
                                            "timeout_ms": 10000,
                                        },
                                    ],
                                },
                            },
                        },
                    },
                    "save",
                )
                + "</section>"
            )
            body += (
                f"<section data-ios-existing-recording><h2>{esc(tr('ios_existing_capture'))}</h2><p>{esc(tr('ios_existing_hint'))}</p>"
                + json_form(
                    "save",
                    project,
                    {
                        "id": "ios-existing-process-journey",
                        "kind": "client_performance",
                        "budget": {"timeout_seconds": 240},
                        "spec": {
                            "platform": "ios",
                            "iterations": 1,
                            "warmup": 0,
                            "ios_capture": {
                                "mode": "device_processes",
                                "duration_seconds": 120,
                                "finalize_timeout_seconds": 45,
                            },
                            "thresholds": {
                                "max_cpu_percent": 100,
                                "max_memory_footprint_kb": 200000,
                            },
                            "plan": {
                                "id": "ios-existing-ready",
                                "kind": "ios",
                                "spec": {
                                    "profile": "iphone-lab",
                                    "steps": [
                                        {
                                            "op": "expect",
                                            "target": {"identifier": "ready_label"},
                                            "attribute": "text",
                                            "expected": "Ready",
                                            "timeout_ms": 10000,
                                        }
                                    ],
                                },
                            },
                        },
                    },
                    "save",
                )
                + "</section>"
            )
            body += (
                f"<section data-ios-isolated-recording><h2>{esc(tr('ios_isolated_capture'))}</h2><p>{esc(tr('ios_isolated_hint'))}</p>"
                + json_form(
                    "save", project,
                    {
                        "id": "ios-isolated-attach-journey",
                        "kind": "client_performance",
                        "budget": {"timeout_seconds": 240},
                        "spec": {
                            "platform": "ios", "iterations": 1, "warmup": 0,
                            "ios_capture": {
                                "mode": "attach", "tool_launch": "isolated_copy",
                                "stop_strategy": "time_limit", "duration_seconds": 30,
                                "finalize_timeout_seconds": 45,
                            },
                            "thresholds": {"max_cpu_percent": 100, "max_memory_footprint_kb": 200000},
                            "plan": {
                                "id": "ios-isolated-ready", "kind": "ios",
                                "spec": {
                                    "profile": "iphone-lab",
                                    "steps": [{"op": "expect", "target": {"identifier": "ready_label"},
                                               "attribute": "text", "expected": "Ready", "timeout_ms": 10000}],
                                },
                            },
                        },
                    },
                    "save",
                )
                + "</section>"
            )
            body += (
                f"<section><h2>{esc(tr('ios_calibrate'))}</h2>"
                + json_form(
                    "ios-calibrate",
                    project,
                    {
                        "profile": "iphone-lab",
                        "mode": "scroll",
                        "target_identifier": "item_list",
                        "origin_identifier": "item_1",
                        "iterations": 3,
                        "configuration_id": "replace-with-build-configuration-id",
                        "hitch_collector": "signpost",
                        "bindings": {
                            "hitch_ratio": {
                                "identifier": "com.apple.dt.XCTMetric_OSSignpost-Scroll_DraggingAndDeceleration.animation.hitch.time.ratio",
                                "unit": "ms per s",
                            }
                        },
                    },
                    "ios_calibrate",
                    task=True,
                )
                + "</section>"
            )
            for item in store.artifacts("ios_calibration"):
                body += (
                    f'<section data-ios-calibration="{esc(item["id"])}"><h2>{esc(item["id"])} · {esc(item["status"])}</h2>'
                    + detail(item)
                )
                if item["status"] == "usable" and item.get("measurement_spec"):
                    body += json_form(
                        "ios-measure",
                        project,
                        {**item["measurement_spec"], "calibration_id": item["id"]},
                        "ios_measure",
                        task=True,
                    )
                body += "</section>"
            body += "<section>" + form(
                "ios-doctor",
                project,
                f"<button>{esc(tr('ios_doctor'))}</button>",
                task="testing-ios-doctor",
            )
            for item in store.artifacts("ios_host", 1):
                body += detail(item)
            body += (
                "</section><section><h2>"
                + esc(tr("ios_import"))
                + "</h2><p>"
                + esc(tr("ios_hint"))
                + "</p>"
            )
            body += (
                json_form(
                    "ios-metrics-import",
                    project,
                    {
                        "format": "trace",
                        "path": "/path/to/recording.trace",
                        "platform": "ios",
                        "target_pid": 777,
                        "app_id": "com.example.app",
                    },
                    "ios_import",
                    task=True,
                )
                + "</section>"
            )
            for item in store.artifacts("ios_measurement"):
                body += (
                    f'<section data-ios-measurement="{esc(item["id"])}"><h2>{esc(item["id"])}</h2><p>{esc(item["source"].get("platform", ""))} · {esc(item["trust"])}</p>'
                    + detail(item)
                )
                if item["source"].get("platform") == "ios":
                    placeholder = esc(json.dumps({"max_ios_launch_ms": 1500}))
                    fields = f'<input type="hidden" name="measurement_id" value="{esc(item["id"])}"><label>ID<input name="plan_id" required></label><label>{esc(tr("thresholds"))}<textarea name="thresholds" data-i18n-skip required placeholder="{placeholder}"></textarea></label><button>{esc(tr("ios_gate"))}</button>'
                    body += form("ios-gate-create", project, fields)
                body += "</section>"
        for plan in store.plans():
            if plan["kind"] == tab:
                body += (
                    f"<section><h2>{esc(plan['id'])}</h2>"
                    + form(
                        "run",
                        project,
                        f'<input type="hidden" name="plan_id" value="{esc(plan["id"])}"><button>{esc(tr("run"))}</button>',
                        task="testing-run",
                    )
                    + json_form("save", project, plan, "save")
                    + "</section>"
                )
    elif tab == "nodes":
        from iatest.nodes.coordinator import CoordinatorClient

        body += f"<section><h2>{esc(tr('shared_resources'))}</h2>" + detail(
            services.coordinator_status(root, project)
        )
        body += json_form(
            "coordinator-start",
            project,
            {
                "host": "127.0.0.1",
                "port": 8769,
                "identities": [
                    {
                        "id": "controller-a",
                        "token_env": "IATEST_RESOURCE_TOKEN",
                        "resources": ["lab:shared"],
                    }
                ],
            },
            "save",
        )
        body += form("coordinator-stop", project, f"<button>{esc(tr('stop'))}</button>")
        body += json_form(
            "coordinator-config",
            project,
            {
                "url": "http://127.0.0.1:8769",
                "id": "controller-a",
                "token_env": "IATEST_RESOURCE_TOKEN",
                "ttl": 10,
            },
            "save",
        )
        client = None
        try:
            client = CoordinatorClient.configured(root)
            if client:
                body += detail(client.call("list", {}))
        except (ValueError, RuntimeError, __import__("httpx").HTTPError):
            body += f"<p>{esc(tr('shared_unavailable'))}</p>"
        finally:
            if client:
                client.close()
        body += (
            json_form(
                "global-reconcile",
                project,
                {
                    "id": "lease-...",
                    "stopped": False,
                    "note": "Describe verified controller stop and side effects",
                },
                "save",
            )
            + "</section>"
        )
        body += (
            "<section>"
            + detail(ControllerStore(root, project).claims())
            + json_form(
                "node-reconcile",
                project,
                {
                    "job_id": "job-...",
                    "stopped": False,
                    "note": "Describe external stop and side-effect verification evidence",
                },
                "save",
            )
            + "</section>"
        )
        body += (
            f"<section><h2>{esc(tr('nodes'))}</h2><p>{esc(tr('node_hint'))}</p>"
            + detail(services.status(root, project))
        )
        body += json_form(
            "controller-start",
            project,
            {"host": "127.0.0.1", "port": 8768},
            "controller_start",
        )
        body += (
            form(
                "controller-stop",
                project,
                f'<button class="ghost">{esc(tr("controller_stop"))}</button>',
            )
            + "</section>"
        )
        body += "<section>" + json_form(
            "node-enroll",
            project,
            {
                "id": "worker-a",
                "token_env": "IATEST_NODE_A_TOKEN",
                "allowed_origins": ["http://localhost:8000"],
                "allowed_kinds": [
                    "api",
                    "performance",
                    "web",
                    "workflow",
                    "client_performance",
                ],
            },
            "enroll",
        )
        body += "<pre data-i18n-skip>iatest node worker https://controller.example:8768 worker-a --project default --origin https://test.example.com --token-env IATEST_NODE_A_TOKEN --kinds api,performance,web,workflow,client_performance --ca /path/to/ca.pem</pre></section>"
        body += (
            "<section><h2>"
            + esc(tr("windows_bundle"))
            + "</h2>"
            + json_form(
                "windows-bundle",
                project,
                {
                    "wheel": "/path/to/iatest_framework-2.0.0-py3-none-any.whl",
                    "ca_file": "/path/to/controller-ca.pem",
                    "controller": "https://192.168.1.10:8768",
                    "allowed_origins": ["http://192.168.1.10:8767"],
                    "node_id": "windows",
                    "token_env": "IATEST_NODE_TOKEN",
                },
                "windows_bundle",
            )
            + "</section>"
        )
        for item in store.artifacts("node_bundle", 10):
            href = (
                "/testing-node-bundle/"
                + item["id"]
                + "?"
                + urlencode({"project": project})
            )
            body += f'<section><h2>{esc(item["node_id"])}</h2><a class="button" href="{esc(href)}">{esc(tr("download"))}</a>{detail(item)}</section>'
        for node in ControllerStore(root, project).nodes():
            body += f'<section data-node-id="{esc(node["id"])}"><h2>{esc(node["id"])}</h2><p>{"online" if node["online"] else "offline"}</p>{detail(node)}</section>'
    elif tab == "campaigns":
        body += (
            f"<section><h2>{esc(tr('create'))}</h2><p>{esc(tr('campaign_hint'))}</p>"
            + json_form(
                "campaign-create",
                project,
                {
                    "id": "release-check",
                    "build": {"service_version": "build-2", "environment": "staging"},
                    "max_parallel": 2,
                    "resource_wait_seconds": 30,
                    "resources": ["staging:account:test-user"],
                    "nodes": [
                        {"id": "api", "plan_id": "api-smoke"},
                        {"id": "ui", "plan_id": "web-smoke", "depends_on": ["api"]},
                    ],
                    "budget": {"timeout_seconds": 600, "max_requests": 10000},
                },
                "create",
            )
            + "</section>"
        )
        for manifest in store.artifacts("campaign"):
            fields = f'<input type="hidden" name="campaign_id" value="{esc(manifest["id"])}"><label><input type="checkbox" name="analyze">{esc(tr("analyze"))}</label><button>{esc(tr("run"))}</button>'
            body += (
                f"<section><h2>{esc(manifest['id'])}</h2>"
                + form("campaign-run", project, fields, task="testing-campaign-run")
                + detail(manifest)
                + "</section>"
            )
        for run in store.artifacts("campaign_run"):
            body += (
                f'<section data-campaign-run="{esc(run["id"])}"><h2>{esc(run["campaign_id"])}</h2><p>{esc(run["state"])} · {esc(run.get("verdict", ""))}</p>'
                + detail(run)
                + "</section>"
            )
    if tab in {"apps", "client_performance", "remote", "nodes", "campaigns"}:
        from iatest.web.plan_forms import operation_form
        from iatest.web.workspace import text
        guided = operation_form(root, project, tab, store)
        if tab in {"client_performance", "remote"}:
            guided += "<section><h2>" + esc(text("已保存计划", "Saved plans")) + "</h2>"
            for plan in store.plans():
                if plan["kind"] == tab:
                    guided += "<article><h3>" + esc(plan.get("title") or plan["id"]) + "</h3>" + form("run", project, f'<input type="hidden" name="plan_id" value="{esc(plan["id"])}"><button>{esc(tr("run"))}</button>', task="testing-run") + "</article>"
            guided += "</section>"
        elif tab == "nodes":
            guided += '<section><h2>' + esc(text("节点状态", "Node status")) + '</h2>'
            for node in ControllerStore(root,project).nodes():
                guided += '<p>' + esc(node['id']) + ' · ' + esc(text("在线", "Online") if node['online'] else text("未连接：请启动远程 worker", "Disconnected: start the remote worker")) + '</p>'
            guided += '</section>'
        elif tab == "campaigns":
            for manifest in store.artifacts("campaign"):
                guided += '<section><h3>' + esc(manifest['id']) + '</h3>' + form("campaign-run",project,f'<input type="hidden" name="campaign_id" value="{esc(manifest["id"])}"><button>{esc(tr("run"))}</button>',task="testing-campaign-run") + '</section>'
        elif tab == "apps":
            for item in store.artifacts("app"):
                guided += '<section><h3>' + esc(item['id']) + '</h3>' + detail(item) + '</section>'
        body = guided + '<details class="advanced-tools"><summary>' + esc(text("高级工具与完整配置", "Advanced tools & full configuration")) + '</summary><p>' + esc(text("以下工具面向已有环境配置的使用者。展开对应配置并阅读字段后执行；不会自动运行示例。", "These tools require an existing environment. Review the fields before execution; examples do not run automatically.")) + '</p>' + body + '</details>'
    return body


ACTIONS = {
    "guided-operation",
    "security-library",
    "accessibility-review",
    "windows-bundle",
    "ios-doctor",
    "ios-metrics-import",
    "ios-measure",
    "ios-calibrate",
    "ios-gate-create",
    "import-preview",
    "import-commit",
    "import-process",
    "asset-process",
    "app-fetch",
    "app-prepare",
    "node-enroll",
    "node-reconcile",
    "coordinator-start",
    "coordinator-stop",
    "coordinator-config",
    "global-reconcile",
    "controller-start",
    "controller-stop",
    "campaign-create",
    "campaign-run",
}
TASKS = {
    "testing-" + action
    for action in (
        "ios-doctor",
        "ios-metrics-import",
        "ios-measure",
        "ios-calibrate",
        "import-process",
        "asset-process",
        "app-fetch",
        "app-prepare",
        "campaign-run",
    )
}


def handle_operation(root, project, action, data, handler):
    from iatest.application.campaigns import create_campaign
    from iatest.application.test_assets import commit_import, preview_import
    from iatest.nodes import services
    from iatest.nodes.store import ControllerStore
    from iatest.web.server import start_task

    get = lambda key, default="": data.get(key, [default])[0]
    tab = "assets"
    if action == "guided-operation":
        from iatest.web.plan_forms import operation_payload
        operation = get("operation")
        payload = operation_payload(operation, data)
        return handle_operation(root, project, operation, {"content": [json.dumps(payload, ensure_ascii=False)]}, handler)
    if action in {"security-library", "accessibility-review"}:
        if action == "security-library":
            from iatest.engines.security_scan import import_library

            import_library(root, project, json.loads(get("content")))
            tab = "security"
        else:
            from iatest.engines.accessibility import import_review

            import_review(root, project, json.loads(get("content")))
            tab = "accessibility"
    elif action == "import-preview":
        encoded = get("encoded")
        if len(encoded) > 3 * 1024 * 1024:
            raise ValueError("Import file exceeds 2 MiB")
        preview_import(
            root,
            project,
            base64.b64decode(encoded, validate=True),
            get("filename"),
            defaults=json.loads(get("defaults", "{}") or "{}"),
            policy=get("policy", "error"),
        )
    elif action == "import-commit":
        commit_import(
            root, project, get("batch_id"), valid_only=get("valid_only") == "on"
        )
    elif action == "node-enroll":
        ControllerStore(root, project).enroll(json.loads(get("content")))
        tab = "nodes"
    elif action == "node-reconcile":
        spec = json.loads(get("content"))
        ControllerStore(root, project).reconcile(
            spec["job_id"], spec["note"], spec.get("stopped", False)
        )
        tab = "nodes"
    elif action in {
        "coordinator-start",
        "coordinator-stop",
        "coordinator-config",
        "global-reconcile",
    }:
        spec = json.loads(get("content", "{}"))
        if action == "coordinator-start":
            services.coordinator_start(root, project, spec)
        elif action == "coordinator-stop":
            services.coordinator_stop(root, project)
        elif action == "coordinator-config":
            from iatest.core.config import load_config, save_config
            from iatest.nodes.coordinator import CoordinatorClient

            if spec:
                CoordinatorClient(spec).close()
            config = load_config(root)
            config["resource_coordinator"] = spec
            save_config(root, config)
        else:
            from iatest.nodes.coordinator import CoordinatorClient

            client = CoordinatorClient.configured(root)
            if client is None:
                raise ValueError("Configure resource authority first")
            try:
                client.call("reconcile", spec)
            finally:
                client.close()
        tab = "nodes"
    elif action == "windows-bundle":
        from iatest.nodes.windows_bundle import build_bundle

        store = EngineStore(root, project)
        bundle_id = "windows-" + uuid.uuid4().hex[:20]
        directory = store.directory / "bundles"
        directory.mkdir(parents=True, exist_ok=True)
        spec = json.loads(get("content"))
        spec["project"] = project
        result = build_bundle(directory / (bundle_id + ".zip"), **spec)
        store.save_artifact(
            "node_bundle",
            {k: v for k, v in result.items() if k != "output"},
            artifact_id=bundle_id,
        )
        tab = "nodes"
    elif action == "controller-start":
        services.start(root, project, json.loads(get("content")))
        tab = "nodes"
    elif action == "controller-stop":
        services.stop(root, project)
        tab = "nodes"
    elif action == "campaign-create":
        create_campaign(root, project, json.loads(get("content")))
        tab = "campaigns"
    elif action == "ios-gate-create":
        store = EngineStore(root, project)
        asset = store.get_artifact("ios_measurement", get("measurement_id"))
        if asset["source"].get("platform") != "ios":
            raise ValueError("Only an iOS measurement can create an iOS gate")
        store.save_plan(
            {
                "id": get("plan_id"),
                "kind": "client_performance",
                "spec": {
                    "platform": "ios",
                    "measurement": {
                        "artifact_id": asset["id"],
                        "source_digest": asset["source_digest"],
                    },
                    "thresholds": json.loads(get("thresholds")),
                },
            }
        )
        tab = "client_performance"
    else:
        payload = {}
        if action in {
            "ios-doctor",
            "ios-metrics-import",
            "ios-measure",
            "ios-calibrate",
        }:
            if action in {"ios-metrics-import", "ios-measure", "ios-calibrate"}:
                payload["spec"] = json.loads(get("content"))
            tab = "client_performance"
        elif action in {"app-fetch", "app-prepare"}:
            payload["spec"] = json.loads(get("content"))
            tab = "apps"
        elif action == "campaign-run":
            payload = {
                "campaign_id": get("campaign_id"),
                "analyze": get("analyze") == "on",
            }
            EngineStore(root, project).get_artifact("campaign", payload["campaign_id"])
            tab = "campaigns"
        elif action == "asset-process":
            payload = {"case_id": get("case_id"), "version": int(get("version"))}
        else:
            payload["batch_id"] = get("batch_id")
        if not start_task(
            root,
            project,
            "testing-" + action,
            tr(
                {
                    "app-fetch": "fetch",
                    "app-prepare": "prepare",
                    "campaign-run": "run",
                    "ios-doctor": "ios_doctor",
                    "ios-metrics-import": "ios_import",
                    "ios-measure": "ios_measure",
                    "ios-calibrate": "ios_calibrate",
                }.get(action, "process")
            ),
            kind="testing-" + action,
            payload=payload,
            resumable=False,
        ):
            raise ValueError("A task of this type is already running")
    handler.redirect("/testing?" + urlencode({"project": project, "tab": tab}))


def execute_operation(root, project, kind, payload, progress):
    from iatest.application.campaigns import run_campaign
    from iatest.application.test_assets import process_asset, process_import
    from iatest.execution.control import ACTIVE_RUNTIME, RunControl
    from iatest.mobile.delivery import fetch_app, prepare_app

    parent = ACTIVE_RUNTIME.get()
    control = RunControl(
        timeout_seconds=600,
        max_requests=10000,
        cancellation_check=(lambda: bool(parent.control.reason())) if parent else None,
    )
    progress("Preparing business test resources", stage="prepare")
    if kind == "testing-campaign-run":
        result = run_campaign(
            root,
            project,
            payload["campaign_id"],
            control=control,
            analyze=payload["analyze"],
            progress=progress,
        )
    elif kind == "testing-import-process":
        result = process_import(root, project, payload["batch_id"], progress=progress)
    elif kind == "testing-asset-process":
        result = process_asset(
            root, project, payload["case_id"], version=payload["version"]
        )
    elif kind == "testing-app-fetch":
        result = fetch_app(root, project, payload["spec"], control=control)
    elif kind == "testing-app-prepare":
        result = prepare_app(root, project, payload["spec"], control)
    elif kind == "testing-ios-metrics-import":
        from iatest.application.ios_measurements import import_measurement

        result = import_measurement(root, project, payload["spec"], control=control)
    elif kind == "testing-ios-measure":
        from iatest.mobile.ios_xctest import run_measurement

        result = run_measurement(root, project, payload["spec"], control)
    elif kind == "testing-ios-calibrate":
        from iatest.mobile.ios_calibration import run_calibration

        result = run_calibration(root, project, payload["spec"], control)
    elif kind == "testing-ios-doctor":
        from iatest.mobile.ios_capture import capabilities

        result = capabilities()
        result["id"] = EngineStore(root, project).save_artifact("ios_host", result)
    else:
        raise ValueError("Unknown business test task")
    progress("Resources and execution evidence saved", stage="report")
    summary = {
        key: result[key]
        for key in ("id", "batch_id", "state", "status", "verdict", "error", "sha256")
        if key in result
    }
    if "assets" in result:
        summary["assets"] = [
            {"case_id": a["case_id"], "version": a["version"], "state": a["state"]}
            for a in result["assets"]
        ]
    return json.dumps(summary, ensure_ascii=False)
