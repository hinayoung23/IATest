"""Task-oriented console navigation and contextual help, separate from engines."""

from __future__ import annotations

import html
from urllib.parse import parse_qs, urlencode

from iatest.web.i18n import current_locale


def text(zh, en):
    return zh if current_locale() == "zh" else en


def esc(value):
    return html.escape(str(value), quote=True)


LABELS = {
    "overview": ("概览", "Overview", "概要", "Vue d’ensemble", "Обзор"),
    "testing": (
        "测试工作台",
        "Test workspace",
        "テスト",
        "Espace de test",
        "Тестирование",
    ),
    "devices": (
        "设备与环境",
        "Devices & environments",
        "デバイスと環境",
        "Appareils et environnements",
        "Устройства и среды",
    ),
    "assets": (
        "测试资产",
        "Test assets",
        "テスト資産",
        "Ressources de test",
        "Тестовые ресурсы",
    ),
    "results": (
        "执行与报告",
        "Runs & reports",
        "実行とレポート",
        "Exécutions et rapports",
        "Запуски и отчёты",
    ),
    "context": (
        "项目知识",
        "Project knowledge",
        "プロジェクト知識",
        "Connaissances",
        "Знания проекта",
    ),
    "config": ("设置", "Settings", "設定", "Paramètres", "Настройки"),
}
PRIMARY = (
    ("overview", "/"),
    ("testing", "/testing"),
    ("devices", "/devices"),
    ("assets", "/assets"),
    ("results", "/results"),
    ("context", "/context"),
    ("config", "/config"),
)


def label(key):
    return LABELS[key][
        {"zh": 0, "en": 1, "ja": 2, "fr": 3, "ru": 4}.get(current_locale(), 1)
    ]


# Each entry is purpose, prerequisites, outcome. Keys remain compatible plan kinds.
FEATURES = {
    "web": (
        "interaction",
        "浏览器页面",
        "Browser UI",
        "验证网页上的填写、点击、跳转与结果。",
        "Test page input, clicks, navigation and outcomes.",
        "已安装浏览器；明确网址和允许访问的来源。",
        "Installed browser; explicit URL and allowed origins.",
        "保存可重复运行的页面操作与断言。",
        "Save repeatable page actions and assertions.",
    ),
    "desktop": (
        "interaction",
        "桌面应用",
        "Desktop application",
        "验证 Mac、Windows 或 Linux 应用的原生控件。",
        "Test native Mac, Windows or Linux controls.",
        "应用已运行；进程 ID、应用身份和系统辅助权限。",
        "Running app, process ID, app identity and accessibility permissions.",
        "原生操作、结果读回和窗口证据。",
        "Native input, readback and window evidence.",
    ),
    "mobile": (
        "interaction",
        "移动 App",
        "Mobile app",
        "在 Android 或 iPhone 上执行操作并检查 App 结果。",
        "Act on Android or iPhone and check App results.",
        "先保存设备配置，再从设备调试取得控件标识。",
        "Save a device profile, then inspect control identifiers.",
        "按步骤运行的移动计划及独立业务断言。",
        "Step-by-step mobile plans with business assertions.",
    ),
    "api": (
        "services",
        "HTTP 请求",
        "HTTP request",
        "检查一个或一组 HTTP 请求的响应。",
        "Check responses from one or more HTTP requests.",
        "测试服务地址、允许来源及明确的预期响应。",
        "Test URL, allowed origins and expected responses.",
        "状态码或响应断言；完整 API 用例在测试资产中管理。",
        "Response assertions; manage complete API suites in Test assets.",
    ),
    "data": (
        "services",
        "数据质量",
        "Data quality",
        "检查表格或只读数据库中的重复、空值和数量。",
        "Check duplicates, nulls and counts in tables or read-only databases.",
        "先准备 CSV 数据，或在高级配置中声明只读数据源。",
        "Prepare CSV data, or specify a read-only source in advanced settings.",
        "失败行号、检查数量和明确的数据质量判定。",
        "Failing row indices, check counts and a quality verdict.",
    ),
    "protocol": (
        "services",
        "消息与协议",
        "Messages & protocols",
        "验证 WebSocket、MQTT、gRPC 或 HTTP 协议交互。",
        "Verify WebSocket, MQTT, gRPC or HTTP exchanges.",
        "已授权服务地址、发送内容和预期回复；gRPC 另需描述符。",
        "Authorized endpoint, message and expected reply; gRPC also needs a descriptor.",
        "消息/协议断言；不会从发送回执推断业务通过。",
        "Message assertions, not a verdict based on a send receipt.",
    ),
    "performance": (
        "quality",
        "服务端性能",
        "Service performance",
        "以有限并发或到达率检查服务时延和错误率。",
        "Check service latency and errors under bounded load.",
        "授权压测环境、并发/时长和门禁阈值。",
        "Authorized load environment, concurrency, duration and thresholds.",
        "请求样本、P95、吞吐、丢弃和门禁结果。",
        "Samples, P95, throughput, drops and threshold results.",
    ),
    "client_performance": (
        "quality",
        "客户端性能",
        "Client performance",
        "运行已有界面计划并采集浏览器或手机指标。",
        "Run an existing UI plan and collect browser or device metrics.",
        "先保存界面计划；手机指标需要对应采集环境。",
        "Save a UI plan first; mobile metrics need platform collectors.",
        "明确来源的指标、缺测状态与性能门禁。",
        "Scoped metrics, missing-data status and performance gates.",
    ),
    "security": (
        "quality",
        "安全验证",
        "Security checks",
        "检查明确授权范围内的访问控制与响应策略。",
        "Check authorized access control and response policies.",
        "授权目标；越权对照需两个独立身份和资源标记。",
        "Authorized target; access checks need two identities and a resource marker.",
        "有对照的规则发现，不等于全站安全认证。",
        "Controlled findings, not a whole-site security certification.",
    ),
    "accessibility": (
        "quality",
        "无障碍检查",
        "Accessibility",
        "检查页面标题、控件名称、图片替代文字等。",
        "Check titles, accessible names and image alternatives.",
        "可访问网页和已安装 Chromium；完整条款另需人工复核。",
        "Reachable page and Chromium; full criteria also need human review.",
        "自动问题及待人工复核项，未评估不会算通过。",
        "Findings and review requirements; unassessed items do not pass.",
    ),
    "workflow": (
        "automation",
        "组合测试流程",
        "Test workflow",
        "把已经保存的计划连接为业务流程。",
        "Connect saved plans into a business journey.",
        "先创建子计划，再选择执行顺序、并行和清理。",
        "Create child plans first, then choose order, concurrency and cleanup.",
        "各步骤和整条流程的判定及清理状态。",
        "Step and workflow verdicts with cleanup status.",
    ),
    "exploration": (
        "interaction",
        "AI 目标探索",
        "AI goal exploration",
        "描述业务目的，让 AI 观察、探索并固化操作。",
        "Describe a goal for AI to observe, explore and compile.",
        "模型已配置；明确目标、可信预期和允许操作范围。",
        "Configured model; explicit goal, trusted expectations and scope.",
        "候选或经过独立回放的计划；草稿不等于通过。",
        "Candidates or independently replayed plans, not an automatic pass.",
    ),
    "remote": (
        "automation",
        "远程运行",
        "Remote execution",
        "在已登记的局域网节点上运行已有计划。",
        "Run an existing plan on an enrolled LAN node.",
        "先在设备与环境登记节点，并保存要执行的计划。",
        "Enroll a node under Devices & environments and save a plan.",
        "节点运行状态、类型化结果和清理证据。",
        "Node status, typed results and cleanup evidence.",
    ),
    "campaigns": (
        "automation",
        "版本验证任务",
        "Release verification",
        "为指定构建组合多个测试及依赖关系。",
        "Combine tests and dependencies for a specific build.",
        "先准备计划、构建标识和测试环境。",
        "Prepare plans, build identity and test environment.",
        "一次版本任务的整体结果与子任务来源。",
        "A release verdict and traceable child results.",
    ),
}
GROUPS = {
    "interaction": ("界面与交互", "UI & interaction"),
    "services": ("服务与数据", "Services & data"),
    "quality": ("专项质量", "Quality checks"),
    "automation": ("自动化流程", "Automation"),
}
FEATURE_AREAS = {
    "assets": "assets",
    "apps": "devices",
    "nodes": "devices",
    "runs": "results",
    "analysis": "results",
    "capabilities": "devices",
}


def feature_title(key):
    if key in FEATURES:
        return text(FEATURES[key][1], FEATURES[key][2])
    return text(
        *{
            "assets": ("批量导入", "Bulk import"),
            "apps": ("应用安装包", "App packages"),
            "nodes": ("执行节点", "Execution nodes"),
            "runs": ("测试执行结果", "Test runs"),
            "analysis": ("证据分析", "Evidence analysis"),
            "capabilities": ("工具与能力", "Tools & capabilities"),
        }[key]
    )


def href(project, path, **query):
    return path + "?" + urlencode({"project": project, **query})


def feature_href(project, key):
    return (
        href(project, "/mobile", tab="plans")
        if key == "mobile"
        else href(project, "/testing", tab=key)
    )


def primary_navigation(project, area):
    return "".join(
        f'<a class="{"active" if area == key else ""}" {"aria-current=page" if area == key else ""} href="{esc(href(project, path))}">{esc(label(key))}</a>'
        for key, path in PRIMARY
    )


def area_for_title(title):
    return {
        "概览": "overview",
        "仪表盘": "overview",
        "接口信息": "assets",
        "用例信息": "assets",
        "测试报告": "results",
        "项目上下文": "context",
        "配置中心": "config",
        "移动端测试": "devices",
    }.get(title, "testing")


def area_links(project, area, active=""):
    entries = {
        "assets": (
            ("/assets", {}, "资产库", "Asset library"),
            ("/assets", {"view": "plans"}, "全部测试计划", "All test plans"),
            ("/testing", {"tab": "assets"}, "导入与模板", "Import & templates"),
        ),
        "devices": (
            ("/mobile", {"tab": "devices"}, "设备配置", "Device profiles"),
            ("/mobile", {"tab": "inspect"}, "设备调试", "Device inspector"),
            ("/testing", {"tab": "apps"}, "应用安装包", "App packages"),
            ("/testing", {"tab": "nodes"}, "执行节点", "Execution nodes"),
            ("/testing", {"tab": "capabilities"}, "工具与能力", "Tools & capabilities"),
        ),
        "results": (
            ("/results", {}, "结果总览", "Result overview"),
            ("/reports", {}, "API 用例报告", "API case reports"),
            ("/testing", {"tab": "runs"}, "类型化测试", "Typed tests"),
            ("/mobile", {"tab": "runs"}, "移动端结果", "Mobile results"),
            ("/testing", {"tab": "analysis"}, "证据分析", "Evidence analysis"),
        ),
    }.get(area, ())
    if not entries:
        return ""
    return (
        '<nav class="workspace-local-tabs" aria-label="'
        + esc(label(area))
        + '">'
        + "".join(
            f'<a href="{esc(href(project, path, **q))}" class="{"active" if active == (q.get("tab") or q.get("view") or path) else ""}">{esc(text(zh, en))}</a>'
            for path, q, zh, en in entries
        )
        + "</nav>"
    )


def testing_navigation(project, tab, group="", *, manual=False, platform=""):
    group = FEATURES[tab][0] if tab in FEATURES else group
    nav = (
        '<nav class="workspace-group-nav">'
        + f'<a class="{"active" if not group else ""}" href="{esc(href(project, "/testing"))}">{esc(text("全部场景", "All scenarios"))}</a>'
    )
    for key, titles in GROUPS.items():
        nav += f'<a class="{"active" if group == key else ""}" href="{esc(href(project, "/testing", group=key))}">{esc(text(*titles))}</a>'
    nav += "</nav>"
    if group == "interaction":
        from iatest.web.goal_workspace import manual_url

        kind = (
            platform
            if platform in {"web", "android", "ios", "desktop"}
            else {"mobile": "android", "desktop": "desktop"}.get(tab, "web")
        )
        entries = [
            (
                href(project, "/testing", tab="exploration", platform=kind),
                text("AI 目标探索", "AI goal exploration"),
                not manual,
            ),
            (
                manual_url(project, kind),
                text("手工编排步骤", "Compose steps manually"),
                manual,
            ),
            (
                href(project, "/assets", category="ui"),
                text("UI 测试资产", "UI test assets"),
                False,
            ),
        ]
        nav += (
            '<nav class="workspace-local-tabs" aria-label="'
            + esc(text("界面测试流程", "UI test workflow"))
            + '">'
            + "".join(
                f'<a class="{"active" if active else ""}" href="{esc(url)}">{esc(title)}</a>'
                for url, title, active in entries
            )
            + "</nav>"
        )
    elif tab in FEATURES:
        nav += (
            '<nav class="workspace-local-tabs">'
            + "".join(
                f'<a class="{"active" if key == tab else ""}" href="{esc(feature_href(project, key))}">{esc(feature_title(key))}</a>'
                for key in (
                    ["exploration", "web", "mobile", "desktop"]
                    if group == "interaction"
                    else FEATURES
                )
                if FEATURES[key][0] == group
            )
            + "</nav>"
        )
    return nav


def feature_guide(project, key):
    if key not in FEATURES:
        descriptions = {
            "assets": (
                "先下载目标或计划模板，填好后上传预览；确认校验结果再导入。导入不会自动执行测试。",
                "Download a goal or plan template, upload it for preview, then commit validated rows. Importing does not execute tests.",
            ),
            "apps": (
                "先下载并校验安装包，再选择已有设备配置准备应用。应用身份、摘要和安装方式必须明确；下载与安装是两个独立动作。",
                "Download and verify a package, then prepare it on a saved device profile. App identity, digest and installation mode are explicit; download and installation are separate actions.",
            ),
            "nodes": (
                "节点是实际执行测试的电脑。先登记节点授权范围，再启动本机控制器及远程 worker；登记成功不代表节点在线。单机测试无需配置共享资源协调器。",
                "A node is a computer that runs tests. Enroll its scope, then start the controller and remote worker. Enrollment does not mean it is online. Single-machine tests do not need a shared resource coordinator.",
            ),
            "runs": (
                "这里汇总通过类型化测试引擎执行的结果。查看最终判定、失败原因及截图证据；API 用例报告和独立移动会话结果在相邻页面。",
                "These results come from typed test engines. Review verdicts, errors and screenshots; API case reports and independent mobile session results are on adjacent pages.",
            ),
            "analysis": (
                "先在执行结果中选择一次运行发起分析。AI 解读引用已有证据，不修改原始测试判定。",
                "Start analysis from a saved run. AI interpretations cite existing evidence and do not change the original verdict.",
            ),
            "capabilities": (
                "这里只检查本机工具和驱动声明。工具可用不代表目标 App 已验收；实际运行仍检查环境、授权和计划要求。",
                "This checks local tools and driver declarations. Availability does not verify a target App; execution still checks environment, authorization and plan requirements.",
            ),
        }
        return (
            '<section class="feature-guide"><p>'
            + esc(text(*descriptions[key]))
            + "</p></section>"
            if key in descriptions
            else ""
        )
    f = FEATURES[key]
    fields = (
        (text("适合做什么", "Purpose"), text(f[3], f[4])),
        (text("开始前准备", "Before you start"), text(f[5], f[6])),
        (text("将得到什么", "What you get"), text(f[7], f[8])),
    )
    return (
        '<section class="feature-guide"><div class="feature-guide-grid">'
        + "".join(
            f"<div><h3>{esc(title)}</h3><p>{esc(body)}</p></div>"
            for title, body in fields
        )
        + '</div><ol class="workflow-steps"><li>'
        + esc(text("填写测试内容", "Configure the test"))
        + "</li><li>"
        + esc(text("保存为计划", "Save a plan"))
        + "</li><li>"
        + esc(text("主动运行并查看结果", "Run and inspect results"))
        + '</li></ol><p class="muted">'
        + esc(
            text(
                "保存只创建计划，不会操作目标；点击运行后才执行。",
                "Saving creates a plan. Only Run executes it.",
            )
        )
        + "</p></section>"
    )


def render_catalog(root, project, parsed):
    from iatest.web.server import page

    group = (parse_qs(parsed.query).get("group") or [""])[0]
    if group not in GROUPS:
        group = ""
    body = (
        '<div class="testing-workspace"><p class="workspace-lead">'
        + esc(
            text(
                "先选择要验证的对象，再创建测试。设备、资产和结果分别在对应工作区管理。",
                "Choose what to verify, then create a test. Manage devices, assets and results in their own workspaces.",
            )
        )
        + "</p>"
        + testing_navigation(
            project,
            "catalog",
            group,
            platform=(parse_qs(parsed.query).get("platform") or [""])[0],
        )
    )
    if group == "interaction":
        from iatest.web.goal_workspace import render_goal_workspace, selected_kind

        body += render_goal_workspace(
            root, project, selected_kind(parse_qs(parsed.query))
        )
        return page(root, project, label("testing"), body + "</div>", area="testing")
    if not group:
        body += (
            '<section class="goal-entry" data-goal-entry><div><p class="eyebrow">AI · UI TESTING</p><h2>'
            + esc(
                text(
                    "从业务目标开始创建界面测试", "Create UI tests from a business goal"
                )
            )
            + "</h2><p>"
            + esc(
                text(
                    "描述目标与预期，AI 探索浏览器、手机或桌面；独立回放后沉淀为可复用资产。",
                    "Describe a goal and outcome. AI explores the browser, mobile or desktop app, then independently replays and saves reusable assets.",
                )
            )
            + '</p></div><a class="button" href="'
            + esc(href(project, "/testing", group="interaction"))
            + '">'
            + esc(text("开始 AI 目标探索", "Start AI goal exploration"))
            + " →</a></section>"
        )
    for key, titles in GROUPS.items():
        if group and group != key:
            continue
        body += (
            '<section class="catalog-section"><h2>'
            + esc(text(*titles))
            + '</h2><div class="scenario-grid">'
        )
        for kind, f in FEATURES.items():
            if f[0] != key:
                continue
            body += f'<a class="scenario-card" href="{esc(feature_href(project, kind))}"><h3>{esc(feature_title(kind))}</h3><p>{esc(text(f[3], f[4]))}</p><span>{esc(text("配置测试", "Configure test"))} →</span></a>'
        body += "</div></section>"
    return page(root, project, label("testing"), body + "</div>", area="testing")


def render_assets(root, project, parsed):
    from iatest.web.asset_library import render_asset_library

    return render_asset_library(root, project, parsed)


def render_results(root, project, parsed):
    from iatest.engines.store import EngineStore
    from iatest.mobile.store import MobileStore
    from iatest.web.server import is_safe_report_name, page, project_reports_dir

    engines = EngineStore(root, project).runs()
    mobile = MobileStore(root, project).runs()
    reports = [
        p
        for p in project_reports_dir(root, project).glob("*.md")
        if is_safe_report_name(p.name)
    ]
    body = (
        area_links(project, "results", "/results")
        + '<p class="workspace-lead">'
        + esc(
            text(
                "按执行来源查看判定和证据。后台任务完成不等于测试通过。",
                "Review verdicts and evidence by execution source. A completed task is not necessarily a passed test.",
            )
        )
        + '</p><div class="scenario-grid">'
    )
    for path, q, zh, en, count in (
        ("/reports", {}, "API 用例报告", "API case reports", len(reports)),
        (
            "/testing",
            {"tab": "runs"},
            "类型化测试结果",
            "Typed test results",
            len(engines),
        ),
        ("/mobile", {"tab": "runs"}, "移动端结果", "Mobile results", len(mobile)),
    ):
        body += f'<a class="scenario-card" href="{esc(href(project, path, **q))}"><h3>{esc(text(zh, en))}</h3><strong class="result-count">{count}</strong><p>{esc(text("查看该来源最近保存的结果", "View recent saved results from this source"))}</p></a>'
    body += "</div>"
    return page(root, project, label("results"), body, area="results")
