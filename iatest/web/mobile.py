from __future__ import annotations

import base64
import html
import json
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from iatest.core.files import atomic_write_bytes
from iatest.mobile.devices import doctor
from iatest.mobile.runtime import SESSIONS
from iatest.mobile.store import MobileStore
from iatest.web.i18n import current_locale

_COPY = {
    "tap_backend": (
        "点击方式",
        "Tap backend",
        "タップ方式",
        "Mode de clic",
        "Способ нажатия",
    ),
    "pointer_tap": (
        "坐标触摸",
        "Pointer input",
        "ポインター入力",
        "Entrée tactile",
        "Касание",
    ),
    "accessibility_tap": (
        "辅助功能动作（不验证触摸路径）",
        "Accessibility action (does not test touch delivery)",
        "アクセシビリティ操作（タッチ経路は未検証）",
        "Action d’accessibilité (sans trajet tactile)",
        "Действие доступности (без проверки касания)",
    ),
    "target_json": (
        "高级目标 JSON（可选）",
        "Advanced target JSON (optional)",
        "詳細ターゲット JSON（省略可）",
        "Cible JSON avancée (facultatif)",
        "JSON цели (необязательно)",
    ),
    "visual_crop": (
        "局部范围 JSON（相对目标，0–1，可选）",
        "Crop JSON (relative to target, 0–1, optional)",
        "局所範囲 JSON（0–1、省略可）",
        "Zone JSON (0–1, facultative)",
        "Область JSON (0–1, необязательно)",
    ),
    "outline": (
        "页面控件概览",
        "Discover page controls",
        "ページの操作対象",
        "Contrôles de la page",
        "Элементы страницы",
    ),
    "ime_setup": (
        "会话内选择测试输入法，结束后恢复（Android）",
        "Select test IME during session and restore (Android)",
        "Android テスト IME を選択し終了後に復元",
        "IME Android temporaire puis restauration",
        "Тестовая IME Android с восстановлением",
    ),
    "private_gestures": (
        "启用 iOS 扩展手势（依赖 XCTest 内部接口）",
        "Enable extended iOS gestures (private XCTest interfaces)",
        "iOS 拡張ジェスチャー",
        "Gestes iOS étendus",
        "Расширенные жесты iOS",
    ),
    "visual_find": (
        "局部视觉定位",
        "Locate in image",
        "画像内を検索",
        "Localiser dans l’image",
        "Найти на изображении",
    ),
    "visual_click": (
        "点击唯一视觉候选",
        "Tap unique visual candidate",
        "一意の視覚候補をタップ",
        "Toucher le candidat unique",
        "Нажать единственного кандидата",
    ),
    "cache_clear": (
        "清空节点缓存",
        "Clear node cache",
        "ノードキャッシュを消去",
        "Vider le cache",
        "Очистить кеш узлов",
    ),
    "cache_advisory": (
        "允许缓存候选（不证明唯一性）",
        "Allow cached candidates (not uniqueness proof)",
        "候補キャッシュを許可",
        "Autoriser les candidats en cache",
        "Разрешить кеш кандидатов",
    ),
    "bridge": (
        "读取测试构建提示",
        "Read test-build hints",
        "テストビルドのヒント",
        "Lire les indices de test",
        "Подсказки тестовой сборки",
    ),
    "artifact_id": (
        "已验证安装包资产 ID",
        "Verified App artifact ID",
        "検証済みアプリ ID",
        "ID d’artefact vérifié",
        "ID проверенной сборки",
    ),
    "port": (
        "iOS Runner 端口",
        "iOS Runner port",
        "iOS Runner ポート",
        "Port du Runner iOS",
        "Порт Runner iOS",
    ),
    "semantic": (
        "直接设置字段值",
        "Set field value",
        "フィールド値を設定",
        "Définir la valeur",
        "Установить значение",
    ),
    "key_events": (
        "按键输入（字段须已聚焦）",
        "Key events (focused field)",
        "キー入力（フォーカス必須）",
        "Touches (champ actif)",
        "Клавиши (поле в фокусе)",
    ),
    "keyboard": (
        "系统键盘",
        "System keyboard",
        "システムキーボード",
        "Clavier système",
        "Системная клавиатура",
    ),
    "devices": ("设备", "Devices", "デバイス", "Appareils", "Устройства"),
    "build": (
        "构建原生组件",
        "Build native components",
        "ネイティブビルド",
        "Compiler les composants",
        "Собрать компоненты",
    ),
    "sdk": (
        "Android SDK 路径",
        "Android SDK path",
        "Android SDK パス",
        "Chemin Android SDK",
        "Путь Android SDK",
    ),
    "java": ("JDK 路径", "JDK home", "JDK パス", "Chemin du JDK", "Каталог JDK"),
    "team": (
        "iOS 签名 Team ID（可选）",
        "iOS signing Team ID (optional)",
        "iOS Team ID（任意）",
        "Team ID iOS (facultatif)",
        "Team ID для iOS (необязательно)",
    ),
    "device_build": (
        "构建 iOS 真机版本",
        "Build for physical iOS",
        "iOS 実機用ビルド",
        "Compiler pour iOS physique",
        "Сборка для физического iOS",
    ),
    "plans": ("测试计划", "Test plans", "テスト計画", "Plans de test", "Планы тестов"),
    "inspect": (
        "设备调试",
        "Device inspector",
        "デバイス検査",
        "Inspection",
        "Инспектор",
    ),
    "runs": ("执行结果", "Run results", "実行結果", "Résultats", "Результаты"),
    "profile": (
        "设备配置",
        "Device profile",
        "デバイス設定",
        "Profil d’appareil",
        "Профиль устройства",
    ),
    "save": ("保存", "Save", "保存", "Enregistrer", "Сохранить"),
    "delete": ("删除", "Delete", "削除", "Supprimer", "Удалить"),
    "detect": (
        "检测设备",
        "Detect devices",
        "デバイスを検出",
        "Détecter les appareils",
        "Найти устройства",
    ),
    "id": ("名称", "Name", "名前", "Nom", "Имя"),
    "platform": ("平台", "Platform", "プラットフォーム", "Plateforme", "Платформа"),
    "device_id": (
        "设备标识",
        "Device identifier",
        "デバイス識別子",
        "Identifiant de l’appareil",
        "Идентификатор устройства",
    ),
    "app_id": (
        "被测应用标识",
        "Target App identifier",
        "対象アプリ識別子",
        "Identifiant de l’application",
        "Идентификатор приложения",
    ),
    "advanced": (
        "高级设置",
        "Advanced settings",
        "詳細設定",
        "Paramètres avancés",
        "Дополнительные настройки",
    ),
    "artifacts": (
        "Android 构建产物目录",
        "Android artifact directory",
        "Android 成果物ディレクトリ",
        "Répertoire Android",
        "Каталог сборки Android",
    ),
    "runner": (
        "iOS xctestrun 文件",
        "iOS xctestrun file",
        "iOS xctestrun ファイル",
        "Fichier xctestrun iOS",
        "Файл xctestrun iOS",
    ),
    "simulator": (
        "iOS 模拟器",
        "iOS simulator",
        "iOS シミュレータ",
        "Simulateur iOS",
        "Симулятор iOS",
    ),
    "open": (
        "打开会话",
        "Open session",
        "セッション開始",
        "Ouvrir une session",
        "Открыть сеанс",
    ),
    "close": (
        "关闭会话",
        "Close session",
        "セッション終了",
        "Fermer la session",
        "Закрыть сеанс",
    ),
    "empty": (
        "暂无记录",
        "No records",
        "記録なし",
        "Aucun enregistrement",
        "Нет записей",
    ),
    "target": (
        "目标元素",
        "Target element",
        "対象要素",
        "Élément cible",
        "Целевой элемент",
    ),
    "identifier": (
        "元素标识",
        "Element identifier",
        "要素識別子",
        "Identifiant de l’élément",
        "Идентификатор элемента",
    ),
    "text": (
        "显示文本",
        "Visible text",
        "表示テキスト",
        "Texte visible",
        "Видимый текст",
    ),
    "label": (
        "无障碍标签",
        "Accessibility label",
        "アクセシビリティラベル",
        "Libellé d’accessibilité",
        "Метка доступности",
    ),
    "container": (
        "容器标识",
        "Container identifier",
        "コンテナ識別子",
        "Identifiant du conteneur",
        "Идентификатор контейнера",
    ),
    "query": (
        "查找元素",
        "Find elements",
        "要素を検索",
        "Rechercher",
        "Найти элементы",
    ),
    "manual": (
        "人工接管",
        "Manual control",
        "手動操作",
        "Contrôle manuel",
        "Ручное управление",
    ),
    "automatic": (
        "交还自动控制",
        "Return to automatic",
        "自動操作に戻す",
        "Rendre le contrôle automatique",
        "Вернуть автоматическое управление",
    ),
    "preview": (
        "获取画面",
        "Capture screen",
        "画面取得",
        "Capturer l’écran",
        "Снимок экрана",
    ),
    "live": (
        "短时预览",
        "Short preview",
        "短時間プレビュー",
        "Aperçu temporaire",
        "Краткий просмотр",
    ),
    "action": ("动作", "Action", "操作", "Action", "Действие"),
    "tap": ("点击", "Tap", "タップ", "Appuyer", "Нажать"),
    "swipe": ("滑动", "Swipe", "スワイプ", "Balayer", "Провести"),
    "input": (
        "输入文字",
        "Enter text",
        "テキスト入力",
        "Saisir du texte",
        "Ввести текст",
    ),
    "key": ("按键", "Key", "キー", "Touche", "Клавиша"),
    "mode": ("输入方式", "Input mode", "入力方式", "Mode de saisie", "Режим ввода"),
    "execute": ("执行", "Execute", "実行", "Exécuter", "Выполнить"),
    "draft": (
        "AI 生成计划",
        "Draft with AI",
        "AIで計画を作成",
        "Créer avec l’IA",
        "Создать с ИИ",
    ),
    "goal": (
        "测试目标与预期结果",
        "Test goal and expected outcome",
        "テスト目標と期待結果",
        "Objectif et résultat attendu",
        "Цель и ожидаемый результат",
    ),
    "expect": (
        "预期结果",
        "Expected outcome",
        "期待結果",
        "Résultat attendu",
        "Ожидаемый результат",
    ),
    "expected_text": (
        "预期显示文本",
        "Expected visible text",
        "期待するテキスト",
        "Texte attendu",
        "Ожидаемый текст",
    ),
    "run": ("运行测试", "Run test", "テスト実行", "Lancer le test", "Запустить тест"),
    "edit": (
        "查看与编辑",
        "View and edit",
        "表示・編集",
        "Voir et modifier",
        "Просмотр и правка",
    ),
    "details": (
        "详细证据",
        "Evidence details",
        "証拠の詳細",
        "Détails des preuves",
        "Подробности",
    ),
    "no_session": (
        "请先打开设备会话",
        "Open a device session first",
        "先にセッションを開始してください",
        "Ouvrez d’abord une session",
        "Сначала откройте сеанс",
    ),
    "notice": (
        "动作派发与测试通过分别记录；只有声明的后置条件满足才通过。",
        "Input dispatch and test verdict are separate; declared postconditions determine success.",
        "入力送信とテスト判定は別です。期待条件で成功を判定します。",
        "L’envoi et le verdict sont distincts ; les conditions attendues déterminent le succès.",
        "Отправка ввода и результат теста различаются; успех определяют постусловия.",
    ),
    "build_hint": (
        "首次使用前构建原生组件；真机需要授权，iOS 还需要已签名的 Runner。",
        "Build native components before first use. Devices require authorization; physical iOS also requires a signed Runner.",
        "初回はネイティブ成果物をビルドしてください。実機iOSには署名が必要です。",
        "Compilez les composants avant utilisation. Un Runner signé est requis sur iOS physique.",
        "Сначала соберите компоненты. Для физического iOS нужен подписанный Runner.",
    ),
    "preview_hint": (
        "PNG 最多每秒两帧，H.264 依设备能力；最多持续两分钟，不向模型发送，操作后需重新查找目标。",
        "PNG is capped at 2 FPS; H.264 depends on device capacity. Preview stops after 2 minutes and is not sent to the model. Re-query after interaction.",
        "PNG は最大2 FPS、H.264 は端末性能に依存。最大2分、モデルへの送信なし。操作後は再検索してください。",
        "PNG limité à 2 images/s ; H.264 selon l’appareil. Arrêt après 2 minutes, sans envoi au modèle. Recherchez après interaction.",
        "PNG до 2 кадров/с; H.264 зависит от устройства. До 2 минут, без передачи модели. После действий обновите поиск.",
    ),
}


def tr(key):
    return _COPY[key][
        {"zh": 0, "en": 1, "ja": 2, "fr": 3, "ru": 4}.get(current_locale(), 1)
    ]


def esc(value):
    return html.escape(str(value), quote=True)


def form(action, project, body, **attributes):
    attrs = " ".join(f'{k.replace("_", "-")}="{esc(v)}"' for k, v in attributes.items())
    return f'<form method="post" action="/actions/mobile-{action}" {attrs}><input type="hidden" name="project" value="{esc(project)}">{body}</form>'


def field(name, label, value="", kind="text", required=False):
    return f'<label>{esc(tr(label))}<input name="{name}" type="{kind}" value="{esc(value)}" {"required" if required else ""}></label>'


def targets(prefix=""):
    return (
        "".join(
            field(prefix + k, label)
            for k, label in (
                ("identifier", "identifier"),
                ("text", "text"),
                ("label", "label"),
                ("container_identifier", "container"),
            )
        )
        + f'<details><summary>{esc(tr("target_json"))}</summary><textarea name="{prefix}target_json" rows="3" data-i18n-skip></textarea></details>'
    )


def target_from_form(data, prefix=""):
    raw = (data.get(prefix + "target_json") or [""])[0].strip()
    if raw:
        target = json.loads(raw)
        if not isinstance(target, dict):
            raise ValueError("Target JSON must be an object")
        return target
    target = {
        k: (data.get(prefix + k) or [""])[0].strip()
        for k in ("identifier", "text", "label", "container_identifier")
    }
    target = {k: v for k, v in target.items() if v}
    target["require_unique"] = True
    return target


def mode_options(platform=""):
    modes = (
        ("keyboard",)
        if platform == "ios"
        else ("semantic", "key_events")
        if platform == "android"
        else ("semantic", "key_events", "keyboard")
    )
    return "".join(
        f'<option value="{m}" data-platform="{"ios" if m == "keyboard" else "android"}">{esc(tr(m))}</option>'
        for m in modes
    )


def step_summary(step):
    label = {
        "find": "query",
        "tap": "tap",
        "text": "input",
        "swipe": "swipe",
        "key": "key",
        "expect": "expect",
        "wait": "expect",
        "screenshot": "preview",
    }.get(step["op"])
    target = step.get("target", {})
    detail = (
        target.get("label")
        or target.get("text")
        or target.get("identifier")
        or step.get("ref", "")
    )
    if not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False)
    if step["op"] == "text":
        value = step.get("value", "")
        detail += " → " + (
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        )
    if step["op"] == "expect":
        from iatest.web.workspace import text
        attribute = {"text": text("文字", "text"), "value": text("值", "value"), "exists": text("存在", "exists")}.get(step.get("attribute", "exists"), step.get("attribute", "exists"))
        operator = {"eq": text("等于", "equals"), "contains": text("包含", "contains")}.get(step.get("operator", "eq"),step.get("operator", "eq"))
        detail += (
            " · "
            + attribute
            + " "
            + operator
            + " "
            + json.dumps(step.get("expected"), ensure_ascii=False)
        )
    return f"<li><strong>{esc(tr(label) if label else step['op'])}</strong> <span>{esc(detail)}</span></li>"


def render_mobile(root: Path, project: str, parsed) -> str:
    from iatest.web.server import page

    store = MobileStore(root, project)
    profiles = store.list("profile")
    plans = store.list("plan")
    tab = (parse_qs(parsed.query).get("tab") or ["devices"])[0]
    if tab not in {"devices", "plans", "inspect", "runs"}:
        tab = "devices"
    from iatest.web.goal_workspace import selected_kind
    from iatest.web.workspace import (
        area_links,
        feature_guide,
        testing_navigation,
        text,
    )
    manual = parse_qs(parsed.query).get("method") == ["manual"]
    default = profiles[0]["platform"] if profiles else "android"
    kind = selected_kind(parse_qs(parsed.query),default,mobile_only=True)
    area = "testing" if tab == "plans" else "results" if tab == "runs" else "devices"
    nav = testing_navigation(project,"mobile",manual=manual,platform=kind) if tab == "plans" else area_links(project,area,tab)
    body = '<div class="mobile-workspace">' + nav
    if tab == "plans" and manual: body += feature_guide(project,"mobile")
    if tab == "devices":
        rows = "".join(
            f"<tr><td>{esc(p['id'])}</td><td>{esc(p['platform'])}</td><td><code>{esc(p['device_id'])}</code></td><td>{esc(p['app_id'])}</td><td>"
            + form(
                "open",
                project,
                f'<input type="hidden" name="profile" value="{esc(p["id"])}"><button>{esc(tr("open"))}</button>',
                data_task_action="mobile-open",
            )
            + "</td><td>"
            + form(
                "delete-profile",
                project,
                f'<input type="hidden" name="id" value="{esc(p["id"])}"><button class="ghost">{esc(tr("delete"))}</button>',
            )
            + "</td></tr>"
            for p in profiles
        )
        body += (
            f'<section><div class="section-head"><h2>{esc(tr("devices"))}</h2>'
            + form("doctor", project, f"<button>{esc(tr('detect'))}</button>")
            + '</div><div class="table-wrap"><table><thead><tr>'
            + "".join(
                f"<th>{esc(tr(k))}</th>"
                for k in ("id", "platform", "device_id", "app_id", "open", "delete")
            )
            + f"</tr></thead><tbody>{rows}</tbody></table></div></section>"
        )
        cached = store.list("doctor")
        if cached:
            from iatest.web.mobile_views import detection_results
            body += detection_results(cached[0])
        inputs = (
            field("id", "id", required=True)
            + "<label>"
            + esc(tr("platform"))
            + '<select name="platform"><option>android</option><option>ios</option></select></label>'
            + field("device_id", "device_id", required=True)
            + field("app_id", "app_id", required=True)
        )
        inputs += (
            f"<details><summary>{esc(tr('advanced'))}</summary>"
            + field(
                "artifact_dir", "artifacts", str(root / "native/mobile/build/android")
            )
            + field("xctestrun", "runner")
            + field("runner_port", "port", "8101", kind="number")
            + f'<label><input type="checkbox" name="simulator">{esc(tr("simulator"))}</label><label><input type="checkbox" name="allow_private_ios_gestures">{esc(tr("private_gestures"))}</label></details><button>{esc(tr("save"))}</button>'
        )
        body += (
            f'<section id="device-profile"><h2>{esc(tr("profile"))}</h2><p>{esc(text("设备标识可从检测结果复制；应用标识是目标 App 的包名或 Bundle ID。配置名称由你命名，保存不会安装或启动 App。", "Copy the device ID from discovery; App ID is the target package name or Bundle ID. Choose a profile name. Saving does not install or launch an App."))}</p><p>{esc(tr("build_hint"))}</p>'
            + form(
                "profile",
                project,
                inputs
                + f'<label><input type="checkbox" name="allow_ime_setup">{esc(tr("ime_setup"))}</label>'
                + '<details><summary>Advanced profile JSON</summary><textarea name="advanced_profile" rows="8" data-i18n-skip>{"allowed_apps":[],"allowed_permissions":[],"allow_clipboard":false,"allow_webview":false,"video_capture":"screenshot"}</textarea></details>',
                **{"class": "mobile-form"},
            )
            + "</section>"
        )
        build_inputs = (
            f'<label>{esc(tr("platform"))}<select name="platform"><option>android</option><option>ios</option></select></label>'
            + field("sdk", "sdk")
            + field("java_home", "java")
            + field("team", "team")
            + f'<label><input type="checkbox" name="device">{esc(tr("device_build"))}</label><button>{esc(tr("build"))}</button>'
        )
        body += (
            f"<section><details><summary>{esc(tr('build'))}</summary>"
            + form(
                "build",
                project,
                build_inputs,
                **{"class": "mobile-form", "data_task_action": "mobile-build"},
            )
            + "</details></section>"
        )
        build_results = store.list("build")
        if build_results:
            body += f"<section><pre data-i18n-skip>{esc(json.dumps(build_results[0], ensure_ascii=False, indent=2))}</pre></section>"
    elif tab == "plans":
        from iatest.web.goal_workspace import (
            creation_modes,
            render_goal_workspace,
        )
        from iatest.web.mobile_views import render_plans
        body += creation_modes(project,kind,manual=manual)
        manual_profiles = [p for p in profiles if p["platform"] == kind] if parse_qs(parsed.query).get("platform") else profiles
        body += render_plans(project, manual_profiles, plans) if manual else render_goal_workspace(root,project,kind)
    elif tab == "inspect":
        try:
            session = SESSIONS.get(root, project)
        except ValueError:
            session = None
        if not session:
            body += f'<section class="empty-state"><p>{esc(tr("no_session"))}</p><a class="button" href="/mobile?{urlencode({"project":project,"tab":"devices"})}">{esc(text("选择设备并打开会话", "Choose a device and open a session"))}</a></section>'
        else:
            body += f'<section><div class="section-head"><h2>{esc(session.profile["id"])}</h2><span class="badge">{esc(session.mode)}</span></div><div class="mobile-toolbar">'
            for action, label in (
                ("close", "close"),
                ("manual", "manual"),
                ("automatic", "automatic"),
                ("cache-clear", "cache_clear"),
                *(
                    [("outline", "outline")]
                    if "outline"
                    in session.device.semantic.capabilities.get("actions", [])
                    else []
                ),
            ):
                body += form(
                    action, project, f'<button class="ghost">{esc(tr(label))}</button>'
                )
            body += '</div></section><div class="mobile-inspector-grid"><section>'
            body += form(
                "query",
                project,
                targets()
                + f'<label><input type="checkbox" name="allow_cache">{esc(tr("cache_advisory"))}</label><button>{esc(tr("query"))}</button>',
                **{"class": "mobile-form"},
            )
            observation = getattr(session, "last_observation", None)
            if observation:
                body += f"<p>{esc('complete' if observation.get('complete') else 'incomplete')} · {esc(observation.get('match_count', '?'))}</p>"
                for node in observation.get("candidates", []):
                    body += f'<article class="mobile-candidate"><strong>{esc(node.get("label") or node.get("text") or node.get("identifier"))}</strong><small>{esc(node.get("identifier", ""))}</small><code>{esc(node.get("bounds", []))}</code></article>'
                if observation.get("scope_error"):
                    body += f'<p role="alert">{esc(observation["scope_error"])}</p>'
            previous = getattr(session, "last_target", {})
            hidden = f'<input type="hidden" name="target" value="{esc(json.dumps(previous, ensure_ascii=False))}">'
            buttons = (
                f'<label>{esc(tr("action"))}<select name="op">'
                + "".join(
                    f'<option value="{op}">{esc(tr(label))}</option>'
                    for op, label in (
                        ("tap", "tap"),
                        ("text", "input"),
                        ("swipe", "swipe"),
                        ("key", "key"),
                    )
                )
                + "</select></label>"
                + field("value", "input")
                + f'<select name="mode" aria-label="{esc(tr("mode"))}">{mode_options(session.profile["platform"])}</select><select name="direction"><option value="up">↑</option><option value="down">↓</option><option value="left">←</option><option value="right">→</option></select><input name="keycode" type="number" min="0" max="300" value="{3 if session.profile["platform"] == "ios" else 4}">'
            )
            if "accessibility" in session.device.semantic.capabilities.get(
                "tap_backends", []
            ):
                buttons += f'<label>{esc(tr("tap_backend"))}<select name="tap_backend"><option value="pointer">{esc(tr("pointer_tap"))}</option><option value="accessibility">{esc(tr("accessibility_tap"))}</option></select></label>'
            body += form(
                "action",
                project,
                hidden
                + buttons
                + f"<button {'disabled' if session.mode != 'manual' else ''}>{esc(tr('execute'))}</button>",
                **{"class": "mobile-form"},
            )
            if getattr(session, "last_dispatch", None):
                body += f'<p role="status">{esc(session.last_dispatch.get("status", "unknown"))}</p>'
            if getattr(session, "last_bridge", None):
                body += f"<details open><summary>{esc(tr('bridge'))}</summary><pre data-i18n-skip>{esc(json.dumps(session.last_bridge, ensure_ascii=False, indent=2))}</pre></details>"
            if getattr(session, "last_target", None):
                body += form(
                    "visual-find",
                    project,
                    field("goal", "goal", required=True)
                    + field("visual_crop", "visual_crop")
                    + f"<button>{esc(tr('visual_find'))}</button>",
                )
            if getattr(session, "last_visual", None):
                body += f"<pre data-i18n-skip>{esc(json.dumps(session.last_visual['result'], ensure_ascii=False, indent=2))}</pre>"
                if len(session.last_visual["result"]["candidates"]) == 1:
                    body += form(
                        "visual-click",
                        project,
                        f"<button {'' if session.mode == 'manual' else 'disabled'}>{esc(tr('visual_click'))}</button>",
                    )
            body += (
                '</section><section><div class="mobile-toolbar">'
                + form(
                    "bridge",
                    project,
                    field("artifact_id", "artifact_id", required=True)
                    + f"<button>{esc(tr('bridge'))}</button>",
                )
                + form(
                    "preview",
                    project,
                    '<select name="media_codec" aria-label="Media format"><option value="png">PNG</option>'
                    + f'<option value="h264" {"" if session.device.semantic.capabilities.get("continuous_video") else "disabled"}>H.264 · FFmpeg</option><option value="h264-native" {"" if session.device.semantic.capabilities.get("continuous_video") else "disabled"}>H.264 · WebCodecs</option></select>'
                    + f"<button>{esc(tr('preview'))}</button>",
                    data_mobile_capture="true",
                )
                + f'<button type="button" data-mobile-preview>{esc(tr("live"))}</button></div><p>{esc(tr("preview_hint"))}</p><img class="mobile-preview" data-mobile-frame alt="Device preview" '
                + (
                    f'src="{esc(session.last_preview)}"'
                    if getattr(session, "last_preview", None)
                    else "hidden"
                )
                + '><p role="status" data-mobile-preview-status></p></section></div>'
            )
    else:
        for run in store.runs():
            body += f'<section data-mobile-run-id="{esc(run["run_id"])}"><div class="section-head"><h3>{esc(run["plan_id"])}</h3><span class="badge" data-verdict="{esc(run["verdict"])}">{esc(run["verdict"])}</span></div><p>{esc(run["created_at"])} · {esc(run["lifecycle"])}</p>'
            if run.get("error"):
                body += f'<p role="alert">{esc(run["error"])}</p>'
            body += (
                '<div class="mobile-evidence-grid">'
                + "".join(
                    f'<a href="/mobile-evidence/{esc(run["run_id"])}/{esc(e["name"])}?{urlencode({"project": project})}"><img loading="lazy" alt="Test evidence" src="/mobile-evidence/{esc(run["run_id"])}/{esc(e["name"])}?{urlencode({"project": project})}"></a>'
                    for e in run.get("evidence", [])
                )
                + "</div>"
            )
            body += f"<details><summary>{esc(tr('details'))}</summary><pre data-i18n-skip>{esc(json.dumps(run, ensure_ascii=False, indent=2))}</pre></details></section>"
        if not store.runs():
            body += f"<section><p>{esc(tr('empty'))}</p></section>"
    title = text(*{"plans": ("移动 App", "Mobile app"), "runs": ("移动端结果", "Mobile results"), "devices": ("设备配置", "Device profiles"), "inspect": ("设备调试", "Device inspector")}[tab])
    return page(root, project, title, body + "</div>", area=area)


def handle_mobile_action(
    root: Path, project: str, path: str, data: dict, handler
) -> None:
    from iatest.web.server import start_task

    def value(key, default=""):
        return (data.get(key) or [default])[0]

    action = path.removeprefix("/actions/mobile-")
    store = MobileStore(root, project)
    tab = "devices"
    if action == "doctor":
        from datetime import UTC, datetime
        store._save("doctor", "latest", {**doctor(), "checked_at": datetime.now(UTC).isoformat(timespec="seconds")})
    elif action == "profile":
        profile = {
            k: value(k).strip() for k in ("id", "platform", "device_id", "app_id")
        }
        for k in ("artifact_dir", "xctestrun"):
            if value(k).strip():
                profile[k] = value(k).strip()
        profile["simulator"] = value("simulator") == "on"
        if profile["platform"] == "android":
            profile["allow_ime_setup"] = value("allow_ime_setup") == "on"
        profile["allow_private_ios_gestures"] = (
            value("allow_private_ios_gestures") == "on"
        )
        if value("runner_port"):
            profile["runner_port"] = int(value("runner_port"))
        if value("advanced_profile").strip():
            extra = json.loads(value("advanced_profile"))
            allowed = {
                "allowed_apps",
                "allowed_permissions",
                "allow_clipboard",
                "allow_webview",
                "display_id",
                "video_capture",
                "app_media_port",
                "app_media_token_env",
                "ios_input_backend",
                "simulator_hid_driver",
                "isolation",
                "require_os_sandbox",
                "managed_emulator",
                "usb_hid",
                "bluetooth_hid",
                "ios_observation",
            }
            if not isinstance(extra, dict) or extra.keys() - allowed:
                raise ValueError("Unsupported advanced profile setting")
            profile.update(extra)
        store.save_profile(profile)
    elif action == "delete-profile":
        store.delete("profile", value("id"))
    elif action == "plan":
        store.save_plan(json.loads(value("content")))
        tab = "plans"
    elif action == "build-plan":
        profile = store.get("profile", value("profile"))
        from iatest.web.mobile_views import compile_mobile_steps
        store.save_plan({"id": value("id"), "kind": profile["platform"], "spec": {"profile": profile["id"], "steps": compile_mobile_steps(data)}})
        tab = "plans"
    elif action == "build":
        platform = value("platform")
        if platform not in {"android", "ios"}:
            raise ValueError("Unsupported build platform")
        payload = {
            k: value(k).strip() for k in ("platform", "sdk", "java_home", "team")
        }
        payload["device"] = value("device") == "on"
        if not start_task(
            root,
            project,
            "mobile-build",
            tr("build"),
            kind="mobile-build",
            payload=payload,
            resumable=False,
        ):
            raise ValueError("A native build is already running")
    elif action in {"open", "draft", "run"}:
        payload = {k: value(k) for k in ("profile", "goal", "plan_id")}
        started = start_task(
            root,
            project,
            "mobile-" + action,
            tr({"open": "open", "draft": "draft", "run": "run"}[action]),
            kind="mobile-" + action,
            payload=payload,
            resumable=False,
        )
        if not started:
            raise ValueError("A mobile task is already queued or running")
        tab = "inspect" if action == "open" else "runs" if action == "run" else "plans"
    elif action == "preview-stop":
        # Hiding/navigating the page may send a stop after session teardown.
        try:
            session = SESSIONS.get(root, project)
        except ValueError:
            session = None
        if session:
            with session.lock:
                session.stop_preview(value("receiver_id"))
        if "application/json" in handler.headers.get("Accept", ""):
            handler.respond_json({"stopped": True})
            return
        tab = "inspect"
    else:
        session = SESSIONS.get(root, project)
        tab = "inspect"
        with session.lock:
            if action == "close":
                SESSIONS.close(root, project)
            elif action in {"manual", "automatic"}:
                session.mode = action
                session.last_observation = None
                session.last_target = None
            elif action == "query":
                session.last_target = target_from_form(data)
                if value("allow_cache") == "on":
                    session.last_target["require_unique"] = False
                session.last_observation = session.query(
                    session.last_target, timeout=max(1, session.query_timeout)
                )
            elif action == "cache-clear":
                session.device.semantic.request("cache_clear", {}, timeout=1)
                session.last_observation = None
            elif action == "outline":
                overview = session.outline()
                session.last_target = {}
                session.last_observation = {
                    **overview,
                    "candidates": overview["controls"],
                    "match_count": len(overview["controls"]),
                }
            elif action == "visual-find":
                from iatest.application.ui_visual import locate_region

                target = getattr(session, "last_target", None)
                if not target:
                    raise ValueError("Query a native region before visual location")
                crop = (
                    json.loads(value("visual_crop"))
                    if value("visual_crop").strip()
                    else None
                )
                result = locate_region(
                    root, project, session, target, value("goal"), visual_crop=crop
                )
                session.last_visual = {"target": target, "result": result}
            elif action == "visual-click":
                visual = getattr(session, "last_visual", None)
                if (
                    session.mode != "manual"
                    or not visual
                    or len(visual["result"]["candidates"]) != 1
                ):
                    raise ValueError(
                        "Manual lease and unique observed visual candidate required"
                    )
                session.last_dispatch = session.visual_click(
                    {
                        "target": visual["target"],
                        "visual_anchor": visual["result"]["candidates"][0]["anchor"],
                        **(
                            {"visual_crop": visual["result"]["visual_crop"]}
                            if "visual_crop" in visual["result"]
                            else {}
                        ),
                    },
                    "manual-" + uuid.uuid4().hex,
                )
                session.last_visual = None
            elif action == "action":
                if session.mode != "manual":
                    raise ValueError(
                        "Take manual control before issuing interactive actions"
                    )
                target = json.loads(value("target", "{}"))
                step = {
                    "op": value("op"),
                    "value": value("value"),
                    "mode": value("mode"),
                    "direction": value("direction", "up"),
                    "keycode": int(value("keycode", "4")),
                }
                if step["op"] == "tap" and value("tap_backend"):
                    step["tap_backend"] = value("tap_backend")
                if step["op"] not in {"tap", "text", "swipe", "key"}:
                    raise ValueError("Unsupported manual action")
                observation = (
                    session.query(
                        target, timeout=max(1, session.query_timeout), cache=False
                    )
                    if step["op"] != "key"
                    else None
                )
                session.last_dispatch = session.action(
                    step,
                    observation,
                    "manual-" + uuid.uuid4().hex,
                    session.action_timeout,
                )
                session.last_observation = None
            elif action == "preview":
                response = session.preview(
                    separate=value("separate_media") == "on",
                    codec=value("media_codec", "png"),
                    request_keyframe=value("request_keyframe") == "on",
                )
                image = response.pop("binary", b"")
                if response.get("transport") == "h264-webcodecs":
                    handler.respond_json(
                        {
                            "encoded": base64.b64encode(image).decode(),
                            "metadata": response,
                        }
                    )
                    return
                if not image.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise ValueError("PNG screenshot unavailable")
                used = getattr(session, "preview_bytes", 0)
                if used + len(image) > 64 * 1024 * 1024:
                    raise ValueError(
                        "Preview session quota exhausted; close and reopen the session"
                    )
                name = uuid.uuid4().hex + ".png"
                target = store.evidence_path(session.id, name)
                target.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(target, image)
                session.preview_bytes = used + len(image)
                session.last_preview = f"/mobile-evidence/{session.id}/{name}?{urlencode({'project': project})}"
                if "application/json" in handler.headers.get("Accept", ""):
                    handler.respond_json(
                        {"url": session.last_preview, "metadata": response}
                    )
                    return
            elif action == "bridge":
                from iatest.mobile.bridge import read_bridge

                session.last_bridge = read_bridge(
                    root,
                    project,
                    session.profile["id"],
                    value("artifact_id"),
                    session=session,
                )
            else:
                raise ValueError("Unknown mobile action")
    handler.redirect("/mobile?" + urlencode({"project":project,"tab":tab,**({"method":"manual"} if tab=="plans" and action in {"build-plan","plan"} else {})}))


def execute_mobile_task(root, project, kind, payload, progress):
    from iatest.application.mobile_service import (
        draft_mobile_plan,
        execute_mobile_plan,
    )

    progress("Preparing native mobile session", stage="prepare")
    if kind == "mobile-build":
        import re
        import sys

        from iatest.execution.control import ACTIVE_RUNTIME
        from iatest.mobile.devices import command

        platform = payload["platform"]
        if platform not in {"android", "ios"}:
            raise ValueError("Unsupported build platform")
        args = [
            sys.executable,
            "-m",
            "iatest.mobile.build",
            platform,
            "--output",
            str(root / "native/mobile/build" / platform),
        ]
        fields = ("sdk", "java_home") if platform == "android" else ("team",)
        for key in fields:
            if payload.get(key):
                if key == "team" and not re.fullmatch(r"[A-Z0-9]{10}", payload[key]):
                    raise ValueError("Invalid signing Team ID")
                args.extend(["--" + key.replace("_", "-"), payload[key]])
        if platform == "ios" and payload.get("device"):
            args.append("--device")
        runtime = ACTIVE_RUNTIME.get()
        progress("Building with installed platform toolchain", stage="execute")
        output = command(
            args, timeout=300, control=runtime.control if runtime else None
        )
        MobileStore(root, project)._save(
            "build",
            "latest",
            {
                "platform": platform,
                "output": output[-6000:],
                "artifacts": str(root / "native/mobile/build" / platform),
            },
        )
        return "Native build completed: " + platform
    if kind == "mobile-open":
        session = SESSIONS.open(root, project, payload["profile"])
        return f"Native session opened: {session.profile['id']}"
    if kind == "mobile-draft":
        progress("Drafting from bounded observations", stage="llm")
        result = draft_mobile_plan(root, project, payload["profile"], payload["goal"])
        return "Validated mobile plan saved: " + result["plan"]["id"]
    progress("Running native mobile plan", stage="execute")
    result = execute_mobile_plan(root, project, payload["plan_id"])
    progress("Mobile result and evidence saved", stage="report")
    return f"{result['run_id']}: {result['verdict']}" + (
        ": " + result["error"] if result["error"] else ""
    )
