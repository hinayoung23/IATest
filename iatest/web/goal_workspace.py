"""Goal-first UI creation for browser, mobile and desktop, using existing exploration."""

from __future__ import annotations

import json

from iatest.web.plan_forms import input_field, select_field
from iatest.web.workspace import esc, href, text

PLATFORMS = {
    "web": ("浏览器", "Browser"),
    "android": ("Android", "Android"),
    "ios": ("iPhone / iPad", "iPhone / iPad"),
    "desktop": ("桌面应用", "Desktop app"),
}


def manual_url(project, kind):
    return (
        href(project, "/mobile", tab="plans", method="manual", platform=kind)
        if kind in {"android", "ios"}
        else href(project, "/testing", tab=kind, method="manual")
    )


def creation_modes(project, kind, *, manual=False):
    if not manual:
        return ""
    return (
        '<nav class="creation-modes" aria-label="'
        + esc(text("手工编排的平台", "Manual test platform"))
        + '">'
        + "".join(
            f'<a class="{"active" if key == kind else ""}" href="{esc(manual_url(project, key))}">{esc(text(*copy))}</a>'
            for key, copy in PLATFORMS.items()
        )
        + "</nav>"
    )


def selected_kind(query, default="web", *, mobile_only=False):
    kind = (query.get("platform") or [default])[0]
    allowed = {"android", "ios"} if mobile_only else PLATFORMS
    return kind if kind in allowed else default


def render_goal_workspace(root, project, kind="web"):
    from iatest.engines.store import EngineStore
    from iatest.mobile.store import MobileStore
    from iatest.web.asset_library import asset_url, state_title
    from iatest.web.testing import browser_fields, form

    kind = kind if kind in PLATFORMS else "web"
    body = (
        '<section class="goal-studio" data-goal-workspace><p class="eyebrow">'
        + esc(text("界面测试 · AI 目标探索", "UI TESTING · AI GOAL EXPLORATION"))
        + "</p><h2>"
        + esc(
            text(
                "说清目标，让 AI 探索怎么测。",
                "Describe the goal. Let AI explore the steps.",
            )
        )
        + '</h2><p class="goal-intro">'
        + esc(
            text(
                "选择目标应用，描述业务目的和预期结果。AI 会观察真实界面、执行探索，再用新会话独立回放；稳定复现后保存为可重复运行的用例与计划。",
                "Choose the target, describe a business goal and expected outcome. AI observes the real UI, explores actions and replays independently in fresh sessions. Stable cases become reusable plans.",
            )
        )
        + '</p><ol class="goal-journey">'
        + "".join(
            "<li><span>" + str(i + 1) + "</span>" + esc(text(zh, en)) + "</li>"
            for i, (zh, en) in enumerate(
                [
                    ("选择目标", "Choose target"),
                    ("描述目标与预期", "State goal & outcome"),
                    ("AI 观察与探索", "AI observes & explores"),
                    ("独立回放并保存", "Replay & save"),
                ]
            )
        )
        + '</ol><nav class="goal-platforms" aria-label="'
        + esc(text("目标平台", "Target platform"))
        + '">'
    )
    for key, copy in PLATFORMS.items():
        body += f'<a class="{"active" if key == kind else ""}" {"aria-current=page" if key == kind else ""} href="{esc(href(project, "/testing", tab="exploration", platform=key))}">{esc(text(*copy))}</a>'
    body += "</nav>"
    inputs = f'<input type="hidden" name="kind" value="{kind}"><input type="hidden" name="guided_goal" value="1">'
    ready = True
    if kind == "web":
        inputs += input_field(
            "url",
            "要测试的网址",
            "Target page URL",
            kind="url",
            hint=text(
                "填写开始测试的完整页面地址。默认只允许访问该网址所属来源。",
                "Enter the full starting page URL. Only its origin is allowed by default.",
            ),
        )
    elif kind in {"android", "ios"}:
        profiles = [
            p
            for p in MobileStore(root, project).list("profile")
            if p["platform"] == kind
        ]
        if not profiles:
            ready = False
            body += (
                '<div class="empty-state"><h3>'
                + esc(text("先连接目标 App", "Connect the target App first"))
                + "</h3><p>"
                + esc(
                    text(
                        "此平台还没有设备配置。先检测设备、填写目标 App 并准备原生组件，然后回来描述测试目标。",
                        "No device profile for this platform. Detect the device, specify the App and prepare native components, then return to describe a goal.",
                    )
                )
                + '</p><a class="button" href="'
                + esc(href(project, "/mobile", tab="devices"))
                + '">'
                + esc(text("配置设备与 App", "Configure device & App"))
                + "</a></div>"
            )
        else:
            inputs += select_field(
                "profile",
                "选择设备与 App",
                "Choose device & App",
                [(p["id"], p["id"] + " · " + p["app_id"]) for p in profiles],
            )
    else:
        inputs += select_field(
            "platform",
            "应用所在系统",
            "App platform",
            [("macos", "macOS"), ("windows", "Windows"), ("linux", "Linux")],
        )
        inputs += input_field(
            "process_id",
            "目标应用进程 ID",
            "App process ID",
            kind="number",
            hint=text(
                "应用需先运行，并授予当前系统所需的辅助功能权限。",
                "Run the app first and grant the required system accessibility permissions.",
            ),
        )
        inputs += input_field(
            "app_id",
            "目标应用身份",
            "App identity",
            hint=text(
                "macOS 填写 Bundle ID；其它系统填写该目标进程对应的应用身份。",
                "Bundle ID on macOS; the target process app identity on other systems.",
            ),
        )
    inputs += '<div class="goal-intent-fields">' + input_field(
        "goal",
        "希望验证什么业务目标",
        "What business goal should be tested?",
        kind="textarea",
        hint=text(
            "例如：把昵称改为“测试昵称”并保存，再刷新页面。无需提供控件标识或完整点击步骤。",
            "Example: set the nickname to “Test User”, save and reload. No control identifiers or complete click script required.",
        ),
    )
    inputs += input_field(
        "expected_result",
        "怎样才算通过",
        "What outcome counts as success?",
        kind="textarea",
        hint=text(
            "写出可信的具体预期，例如“刷新后昵称仍为测试昵称”。页面当前显示的内容不会自动成为预期。",
            "State a trusted, concrete outcome, e.g. the nickname remains “Test User” after reload. Current UI content does not become the expected outcome.",
        ),
    )
    inputs += "</div>"
    advanced = (
        '<details class="goal-settings"><summary>'
        + esc(
            text(
                "范围、规则与探索预算（可选）",
                "Scope, rules & exploration budget (optional)",
            )
        )
        + "</summary>"
    )
    if kind == "web":
        advanced += (
            input_field(
                "origins",
                "允许访问的来源（每行一个）",
                "Allowed origins (one per line)",
                kind="textarea",
                required=False,
                hint=text(
                    "留空仅允许开始网址的来源；填写时请列全目标与资源来源，格式为 https://主机:端口。",
                    "Empty means only the starting origin. If specified, list all target and resource origins as https://host:port.",
                ),
            )
            + browser_fields()
        )
    advanced += input_field(
        "rules",
        "其它可信业务规则（可选）",
        "Additional trusted business rules",
        kind="textarea",
        required=False,
    )
    advanced += input_field(
        "path_hints",
        "入口或路径提示（可选）",
        "Entry or path hints (optional)",
        kind="textarea",
        required=False,
    )
    advanced += input_field(
        "timeout_seconds",
        "最多探索秒数（默认 240）",
        "Time limit in seconds (default 240)",
        kind="number",
        required=False,
    ) + input_field(
        "token_budget",
        "模型 Token 预算（默认 48000）",
        "Model token budget (default 48000)",
        kind="number",
        required=False,
    )
    advanced += (
        "<label>"
        + esc(
            text(
                "高级上下文 JSON（已有配置可粘贴，表单显式填写的字段优先）",
                "Advanced context JSON (explicit form values take priority)",
            )
        )
        + '<textarea name="context" rows="6" data-i18n-skip>{}</textarea></label><label><input type="checkbox" name="force">'
        + esc(
            text(
                "重新探索，跳过已验证用例的复用",
                "Explore again instead of reusing a verified case",
            )
        )
        + "</label></details>"
    )
    inputs += (
        advanced
        + '<p class="goal-execution-note">'
        + esc(
            text(
                "开始后会调用已配置模型并实际操作目标，独立回放至少两次；无法稳定复现时保存候选和原因，不会标为通过。",
                "Starting uses the configured model and acts on the target, then replays at least twice. Unstable results remain candidates with reasons, not passes.",
            )
        )
        + "</p>"
    )
    inputs += f'<button {"" if ready else "disabled"} class="goal-start">{esc(text("开始 AI 目标探索", "Start AI goal exploration"))} →</button>'
    body += form("explore", project, inputs, task="testing-explore")
    body += (
        '<div class="goal-alternatives"><span>'
        + esc(text("已有明确步骤？", "Already know the steps?"))
        + '</span><a class="text-link" href="'
        + esc(manual_url(project, kind))
        + '">'
        + esc(text("手工编排步骤", "Compose steps manually"))
        + ' →</a><a class="text-link" href="'
        + esc(href(project, "/assets", category="ui"))
        + '">'
        + esc(text("查找已有 UI 资产", "Find saved UI assets"))
        + " →</a></div></section>"
    )
    cases = [
        c
        for c in EngineStore(root, project).artifacts("ui_case", limit=200)
        if c.get("kind") == kind
    ]
    body += (
        '<section class="goal-cases"><div class="section-head"><h2>'
        + esc(text("已沉淀的目标用例", "Saved goal cases"))
        + '</h2><a class="text-link" href="'
        + esc(href(project, "/assets", category="ui", role="goal_case"))
        + '">'
        + esc(text("到资产库管理全部", "Manage all in the asset library"))
        + " →</a></div>"
    )
    if not cases:
        body += (
            '<p class="empty-state">'
            + esc(
                text(
                    "此平台暂无目标用例。探索后会显示状态和资产入口；候选、复现通过及复现业务失败会分别标注。",
                    "No goal cases for this platform yet. Exploration creates a case with an explicit candidate or reproducible verdict and an asset link.",
                )
            )
            + "</p>"
        )
    for case in cases[:8]:
        record = {"key": "artifact-ui_case:" + case["id"], "category": "ui"}
        body += f'<article data-case-id="{esc(case["id"])}"><span class="badge" data-case-state="{esc(case.get("status", "candidate"))}">{esc(state_title(case.get("status", "candidate")))}</span><h3>{esc(case["goal"])}</h3><a class="text-link" href="{esc(asset_url(project, record))}">{esc(text("查看目标、预期与固化计划", "View goal, expectations and compiled plan"))} →</a></article>'
    return body + "</section>"


def exploration_payload(root, project, data):
    """Translate fields into the unchanged exploration contract, without device I/O."""
    from iatest.application.ui_exploration import _context
    from iatest.core.sandbox import origin
    from iatest.mobile.store import MobileStore

    def get(key, default=""):
        return (data.get(key) or [default])[0]

    kind = get("kind")
    goal = get("goal").strip()
    if not goal:
        raise ValueError(
            text("请填写要验证的业务目标。", "Describe the business goal.")
        )
    if kind not in PLATFORMS:
        raise ValueError(
            text("请选择支持的界面测试平台。", "Select a supported UI platform.")
        )
    context = json.loads(get("context", "{}") or "{}")
    if not isinstance(context, dict):
        raise TypeError(
            text(
                "高级上下文需要 JSON 对象。", "Advanced context must be a JSON object."
            )
        )
    if "rules" in context and not isinstance(context["rules"], dict):
        raise ValueError(
            text(
                "业务规则需要名称到文字的映射。",
                "Business rules must map names to text.",
            )
        )
    expected = get("expected_result").strip()
    if get("guided_goal") and not expected:
        raise ValueError(text("请明确填写通过条件。", "Specify an expected outcome."))
    if expected:
        if len(expected) > 2000:
            raise ValueError(
                text(
                    "预期结果不能超过 2000 字符。",
                    "Expected outcome exceeds 2000 characters.",
                )
            )
        # Keep the submitted contract identical across request/worker locales.
        goal += "\n" + expected
        context.setdefault("rules", {})["expected_result"] = expected
    if not goal or len(goal) > 8000:
        raise ValueError(
            text(
                "目标与预期合计需为 1–8000 字符。",
                "Goal and outcome must total 1–8000 characters.",
            )
        )
    if get("rules").strip():
        context.setdefault("rules", {})["business_rules"] = get("rules").strip()
    if get("path_hints").strip():
        context["path_hints"] = get("path_hints").strip()
    for key in ("timeout_seconds", "token_budget"):
        if get(key).strip():
            context[key] = int(get(key))
    if kind == "web":
        if get("url").strip():
            context["start_url"] = get("url").strip()
        if "origins" in data:
            context["allowed_origins"] = [
                s.strip() for s in get("origins").splitlines() if s.strip()
            ] or [origin(context.get("start_url", ""))]
        else:
            context.setdefault(
                "allowed_origins", [origin(context.get("start_url", ""))]
            )
        for key in ("browser", "network_policy"):
            if get(key):
                context[key] = get(key)
    elif kind in {"android", "ios"}:
        if get("profile"):
            context["profile"] = get("profile")
        profile = MobileStore(root, project).get("profile", context.get("profile", ""))
        if profile["platform"] != kind:
            raise ValueError(
                text(
                    "设备配置与选择的平台不一致。",
                    "Device profile does not match the selected platform.",
                )
            )
    else:
        for key in ("platform", "app_id"):
            if get(key).strip():
                context[key] = get(key).strip()
        if get("process_id").strip():
            context["process_id"] = int(get("process_id"))
    _context(kind, context)
    return {
        "kind": kind,
        "goal": goal,
        "context": context,
        "force": get("force") == "on",
    }
