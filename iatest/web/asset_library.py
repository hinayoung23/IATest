"""A single, type-aware asset library with readable details and existing actions."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict
from urllib.parse import parse_qs

from iatest.application.asset_catalog import catalog_assets, filter_assets
from iatest.web.workspace import area_links, esc, feature_href, href, label, text

CATEGORY_COPY = {
    "ui": (
        "UI 测试",
        "UI tests",
        "AI 目标用例、业务目标、浏览器/桌面/手机计划及其关联。",
        "AI goal cases, business goals, browser/desktop/mobile plans and their links.",
    ),
    "api": (
        "接口测试",
        "API tests",
        "接口契约、API 用例及 HTTP 请求计划。",
        "API contracts, cases and HTTP request plans.",
    ),
    "performance": (
        "性能测试",
        "Performance tests",
        "服务端负载配置、客户端采样计划、指标与采集校准。",
        "Service load plans, client measurement plans, metrics and collector calibrations.",
    ),
    "security": (
        "安全测试",
        "Security tests",
        "授权验证计划、扫描配置与已导入的版本化规则库。",
        "Authorized security checks, scan plans and imported versioned rule libraries.",
    ),
    "data": (
        "数据与协议",
        "Data & protocols",
        "数据质量检查、消息交互计划和 Protobuf 描述符。",
        "Data quality checks, protocol plans and Protobuf descriptors.",
    ),
    "accessibility": (
        "无障碍测试",
        "Accessibility tests",
        "自动检查计划与明确来源的人工复核资料。",
        "Automated checks and attributed human review records.",
    ),
    "automation": (
        "组合与版本任务",
        "Workflows & releases",
        "跨类型流程、远程计划与构建验证任务。",
        "Cross-type workflows, remote plans and build verification suites.",
    ),
}
ROLE_COPY = {
    "contract": ("接口契约", "API contract"),
    "case": ("API 用例", "API case"),
    "plan": ("测试计划", "Test plan"),
    "goal_case": ("AI 目标用例", "AI goal case"),
    "business_case": ("业务目标用例", "Business goal case"),
    "rule_library": ("安全规则库", "Security rule library"),
    "measurement": ("性能指标资料", "Measurement asset"),
    "calibration": ("采集校准", "Collector calibration"),
    "protocol_schema": ("协议描述符", "Protocol descriptor"),
    "manual_review": ("人工复核资料", "Human review"),
    "release_suite": ("版本验证任务", "Release suite"),
}
CATEGORY_ROLES = {
    "ui": {"plan", "goal_case", "business_case"},
    "api": {"plan", "contract", "case"},
    "performance": {"plan", "measurement", "calibration"},
    "security": {"plan", "rule_library"},
    "data": {"plan", "protocol_schema"},
    "accessibility": {"plan", "manual_review"},
    "automation": {"plan", "release_suite"},
}
STATE_COPY = {
    "ready": ("待运行", "Ready to run"),
    "saved": ("已保存", "Saved"),
    "blocked": ("待补充条件", "Blocked"),
    "needs_clarification": ("待明确目标", "Needs clarification"),
    "needs_review": ("待审阅", "Needs review"),
    "inconclusive": ("复验未定", "Inconclusive"),
    "candidate": ("候选用例", "Candidate"),
    "ready_for_replay": ("待独立回放", "Ready for replay"),
    "reproducible_passed": ("已复现通过", "Reproducibly passed"),
    "reproducible_failed": ("已复现业务失败", "Reproducible business failure"),
}
THRESHOLD_COPY = {
    "p95_ms": ("P95 响应时间上限（毫秒）", "Maximum P95 latency (ms)"),
    "max_error_ratio": ("最大错误比例（0–1）", "Maximum error ratio (0–1)"),
    "max_dropped_ratio": ("最大丢弃比例（0–1）", "Maximum dropped ratio (0–1)"),
    "max_navigation_ms": (
        "浏览器导航耗时上限（毫秒）",
        "Maximum navigation duration (ms)",
    ),
    "max_cpu_percent": ("App CPU 上限（%）", "Maximum App CPU (%)"),
    "max_memory_pss_kb": ("App 内存 PSS 上限（KiB）", "Maximum App PSS memory (KiB)"),
}


def category_title(key):
    return text(*CATEGORY_COPY[key][:2])


def state_title(state):
    return text(*STATE_COPY[state]) if state in STATE_COPY else state


def asset_url(project, asset):
    return href(project, "/assets", category=asset["category"], asset=asset["key"])


def create_url(project, category):
    return (
        href(project, "/testing", group="interaction")
        if category == "ui"
        else feature_href(
            project,
            {
                "api": "api",
                "performance": "performance",
                "security": "security",
                "data": "data",
                "accessibility": "accessibility",
                "automation": "workflow",
            }[category],
        )
    )


def plan_owner(asset, assets):
    if asset["source"] in {"engine-plan", "mobile-plan"}:
        return asset
    plan = asset["payload"].get("plan")
    if plan and asset["state"].startswith("reproducible_"):
        return next(
            (
                a
                for a in assets
                if a["source"] == "engine-plan"
                and a["id"] == plan.get("id")
                and same_plan(plan, a["payload"])
            ),
            None,
        )
    return None


def same_plan(first, second):
    from iatest.engines.contracts import ExecutionPlan

    try:
        return asdict(ExecutionPlan.from_dict(first)) == asdict(
            ExecutionPlan.from_dict(second)
        )
    except (ValueError, TypeError):
        return False


def asset_actions(project, asset, assets):
    from iatest.web.testing import form

    owner = plan_owner(asset, assets)
    body = '<div class="asset-actions">'
    if owner:
        mobile = owner["source"] == "mobile-plan"
        if mobile:
            from iatest.web.mobile import form as mobile_form

            body += mobile_form(
                "run",
                project,
                f'<input type="hidden" name="plan_id" value="{esc(owner["id"])}"><button>{esc(text("运行此计划", "Run this plan"))}</button>',
                data_task_action="mobile-run",
            )
        else:
            body += form(
                "run",
                project,
                f'<input type="hidden" name="plan_id" value="{esc(owner["id"])}"><button>{esc(text("运行此计划", "Run this plan"))}</button>',
                task="testing-run",
            )
        url = (
            href(project, "/mobile", tab="plans", method="manual")
            if mobile
            else href(project, "/testing", tab=owner["kind"], method="manual")
        )
        body += f'<a class="button ghost" href="{esc(url + "#plan-" + owner["id"])}">{esc(text("编辑计划", "Edit plan"))}</a>'
    if asset["source"] in {"api-case", "api-contract"}:
        path = "/cases" if asset["source"] == "api-case" else "/interfaces"
        body += f'<a class="button" href="{esc(href(project, path))}">{esc(text("管理 API 用例", "Manage API cases") if path == "/cases" else text("管理接口契约", "Manage API contracts"))}</a>'
    if asset["role"] == "release_suite":
        body += form(
            "campaign-run",
            project,
            f'<input type="hidden" name="campaign_id" value="{esc(asset["id"])}"><button>{esc(text("运行版本任务", "Run release suite"))}</button>',
            task="testing-campaign-run",
        )
    body += f'<a class="text-link" href="{esc(create_url(project, asset["category"]))}">{esc(text("打开对应测试工作台", "Open test workspace"))} →</a></div>'
    return body


def readable_value(value):
    if isinstance(value, bool):
        return text("是", "Yes") if value else text("否", "No")
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def plan_steps(plan):
    from iatest.web.mobile import step_summary

    steps = plan.get("spec", {}).get("steps", [])
    if not steps:
        return ""
    if plan.get("kind") in {"android", "ios"}:
        return (
            '<ol class="mobile-steps">'
            + "".join(step_summary(s) for s in steps)
            + "</ol>"
        )
    operations = {
        "click": ("点击", "Click"),
        "fill": ("填写", "Fill"),
        "expect": ("检查结果", "Check outcome"),
        "screenshot": ("保存截图", "Capture screenshot"),
        "navigate": ("打开网址", "Navigate"),
        "reload": ("刷新页面", "Reload"),
        "press": ("按键", "Press"),
    }
    body = '<ol class="mobile-steps">'
    for step in steps:
        op = step.get("op", "")
        target = step.get("target", {})
        description = (
            target.get("identifier")
            or target.get("selector")
            or target.get("text")
            or target.get("name")
            or step.get("url", "")
        )
        if step.get("value") is not None:
            description = (
                readable_value(description) + " → " + readable_value(step["value"])
            )
        if "expected" in step:
            description = (
                readable_value(description)
                + " · "
                + text("预期：", "Expected: ")
                + readable_value(step["expected"])
            )
        body += (
            "<li><strong>"
            + esc(text(*operations[op]) if op in operations else op)
            + "</strong> "
            + esc(readable_value(description))
            + "</li>"
        )
    return body + "</ol>"


def render_detail(project, asset, assets):
    p = asset["payload"]
    spec = p.get("spec", {})
    body = f'<section class="asset-detail" data-asset-key="{esc(asset["key"])}"><a class="text-link" href="{esc(href(project, "/assets", category=asset["category"]))}">← {esc(category_title(asset["category"]))}</a><h2>{esc(asset["title"])}</h2><p><span class="badge">{esc(text(*ROLE_COPY[asset["role"]]))}</span> · {esc(state_title(asset["state"]))}</p>'
    body += asset_actions(project, asset, assets)
    if asset["role"] == "goal_case":
        body += (
            "<h3>"
            + esc(text("业务目标与冻结预期", "Goal and frozen expectations"))
            + "</h3><p>"
            + esc(p.get("goal", ""))
            + "</p>"
        )
        for check in (p.get("intent") or {}).get("checks", []):
            body += (
                "<p>"
                + esc(readable_value(check.get("expected", "")))
                + " · "
                + esc(check.get("source_quote", ""))
                + "</p>"
            )
        body += (
            "<p>"
            + esc(text("独立回放次数", "Independent replays"))
            + " · "
            + str(len(p.get("verification", [])))
            + "</p>"
        )
        if p.get("error"):
            body += '<p class="inline-warning">' + esc(p["error"]) + "</p>"
        if (
            p.get("plan")
            and asset["state"].startswith("reproducible_")
            and not plan_owner(asset, assets)
        ):
            body += (
                '<p class="inline-warning">'
                + esc(
                    text(
                        "当前保存的计划已变更或不存在。这里保留当时的复验记录；请审阅当前计划并重新验证。",
                        "The saved plan has changed or is missing. These are historical replay results; review the current plan and verify it again.",
                    )
                )
                + "</p>"
            )
    elif asset["role"] == "business_case":
        body += (
            "<p>"
            + esc(p.get("goal", ""))
            + "</p><p>"
            + esc(text("预期结果", "Expected outcome"))
            + "："
            + esc(p.get("expected", ""))
            + "</p><p>"
            + esc(text("版本", "Version"))
            + " "
            + esc(p.get("version", ""))
            + "</p>"
        )
    elif asset["role"] == "case":
        body += "<p>" + esc(p.get("method", "") + " " + p.get("path", "")) + "</p>"
        if p.get("expected_result"):
            body += (
                "<h3>"
                + esc(text("预期结果", "Expected outcome"))
                + "</h3><p>"
                + esc(p["expected_result"])
                + "</p>"
            )
        for heading, key in [
            (text("前置条件", "Preconditions"), "preconditions"),
            (text("业务步骤", "Business steps"), "test_steps"),
        ]:
            if p.get(key):
                body += (
                    "<h3>"
                    + esc(heading)
                    + "</h3><ol>"
                    + "".join("<li>" + esc(s) + "</li>" for s in p[key])
                    + "</ol>"
                )
        for assertion in p.get("assertions", []):
            body += (
                "<p>"
                + esc(assertion.get("target") or assertion.get("type", ""))
                + " · "
                + esc(readable_value(assertion.get("expected", "")))
                + "</p>"
            )
    fields = []
    for name, zh, en in [
        ("start_url", "目标网页", "Target page"),
        ("platform", "平台", "Platform"),
        ("profile", "设备配置", "Device profile"),
        ("node_id", "执行节点", "Execution node"),
        ("mode", "负载模式", "Load mode"),
        ("duration_seconds", "持续秒数", "Duration seconds"),
        ("users", "并发数", "Users"),
        ("rate", "每秒到达率", "Arrival rate"),
        ("iterations", "采样次数", "Iterations"),
        ("warmup", "预热次数", "Warmup"),
        ("allowed_origins", "允许来源", "Allowed origins"),
    ]:
        if name in spec:
            fields.append((text(zh, en), spec[name]))
    request = spec.get("request", {})
    if request.get("url"):
        fields.append((text("请求目标", "Request URL"), request["url"]))
    if asset["role"] == "contract":
        fields.extend(
            [
                (text("方法与路径", "Method and path"), p["method"] + " " + p["path"]),
                (text("说明", "Description"), p.get("description", "")),
            ]
        )
    if asset["role"] == "rule_library":
        fields.extend(
            [
                (text("规则库版本", "Library version"), p.get("version", "")),
                (text("规则数", "Rule count"), len(p.get("rules", []))),
            ]
        )
    if asset["role"] in {
        "measurement",
        "calibration",
        "protocol_schema",
        "manual_review",
    }:
        for key in (
            "schema",
            "trust",
            "status",
            "valid_until",
            "sha256",
            "source_digest",
        ):
            if key in p:
                fields.append((key, p[key]))
    if fields:
        body += (
            '<dl class="asset-facts">'
            + "".join(
                "<div><dt>"
                + esc(k)
                + "</dt><dd>"
                + esc(readable_value(v))
                + "</dd></div>"
                for k, v in fields
            )
            + "</dl>"
        )
    if spec.get("thresholds"):
        body += (
            "<h3>"
            + esc(text("性能门禁", "Performance thresholds"))
            + '</h3><dl class="asset-facts">'
            + "".join(
                "<div><dt>"
                + esc(text(*THRESHOLD_COPY[k]) if k in THRESHOLD_COPY else k)
                + "</dt><dd>"
                + esc(readable_value(v))
                + "</dd></div>"
                for k, v in spec["thresholds"].items()
            )
            + "</dl>"
        )
    for check in spec.get("checks", []):
        body += (
            "<p>"
            + esc(text("检查规则", "Check rule"))
            + " · "
            + esc(check.get("rule") or check.get("id", ""))
            + "</p>"
        )
    for rule in p.get("rules", []):
        if isinstance(rule, dict):
            body += "<p>" + esc(rule.get("title") or rule.get("id", "")) + "</p>"
    plan = p if asset["role"] == "plan" else p.get("plan") or {}
    body += plan_steps(plan)
    related = []
    if asset["source"] == "engine-plan":
        related = [
            a
            for a in assets
            if a["role"] == "goal_case"
            and (a["payload"].get("plan") or {}).get("id") == asset["id"]
        ]
    elif plan:
        related = [
            a
            for a in assets
            if a["source"] == "engine-plan" and a["id"] == plan.get("id")
        ]
    if asset["kind"] == "client_performance" and spec.get("plan"):
        child = spec["plan"]
        related.extend(
            a
            for a in assets
            if a["role"] == "plan"
            and a["kind"] == child["kind"]
            and a["id"] == child["id"]
            and same_plan(a["payload"], child)
        )
    if related:
        body += (
            "<h3>"
            + esc(text("关联资产", "Related assets"))
            + "</h3>"
            + "".join(
                f'<a class="asset-row" href="{esc(asset_url(project, a))}">{esc(a["title"])} · {esc(text(*ROLE_COPY[a["role"]]))} →</a>'
                for a in related
            )
        )
    body += (
        "<details><summary>"
        + esc(text("高级：完整资产内容", "Advanced: full asset contents"))
        + "</summary><pre data-i18n-skip>"
        + esc(json.dumps(p, ensure_ascii=False, indent=2))
        + "</pre></details></section>"
    )
    return body


def render_asset_library(root, project, parsed):
    from iatest.web.server import page

    query = parse_qs(parsed.query)
    category = (query.get("category") or [""])[0]
    if category not in CATEGORY_COPY:
        category = ""
    role = (query.get("role") or [""])[0]
    if role not in ROLE_COPY:
        role = ""
    search = (query.get("q") or [""])[0][:200]
    plans_only = query.get("view") == ["plans"]
    available_roles = (
        {"plan"} if plans_only else CATEGORY_ROLES.get(category, set(ROLE_COPY))
    )
    if role not in available_roles:
        role = ""
    assets = catalog_assets(root, project)
    body = area_links(project, "assets", "plans" if plans_only else "/assets")
    selected = (query.get("asset") or [""])[0]
    if selected:
        asset = next((a for a in assets if a["key"] == selected), None)
        body += (
            render_detail(project, asset, assets)
            if asset
            else '<section class="empty-state">'
            + esc(text("此项目中没有找到该资产。", "Asset not found in this project."))
            + "</section>"
        )
        return page(root, project, label("assets"), body, area="assets")
    body += (
        '<p class="workspace-lead">'
        + esc(
            text(
                "按测试类型管理目标、用例、计划和参考资料。工作台负责创建与探索，这里负责保存、查找、复用和运行。",
                "Manage goals, cases, plans and reference material by test type. Create and explore in the workspace; find, reuse and run saved assets here.",
            )
        )
        + "</p>"
    )
    counts = Counter(
        a["category"] for a in assets if not plans_only or a["role"] == "plan"
    )
    body += '<div class="asset-category-grid">'
    for key, copy in CATEGORY_COPY.items():
        body += f'<a class="asset-category {"active" if category == key else ""}" data-asset-category="{key}" href="{esc(href(project, "/assets", category=key, **({"view": "plans"} if plans_only else {})))}"><h2>{esc(text(*copy[:2]))}</h2><strong>{counts[key]}</strong><p>{esc(text(*copy[2:]))}</p></a>'
    body += (
        '</div><section class="asset-inventory"><div class="section-head"><h2>'
        + esc(
            category_title(category)
            if category
            else text("全部测试资产", "All test assets")
        )
        + "</h2>"
    )
    if category:
        body += f'<a class="button" href="{esc(create_url(project, category))}">{esc(text("创建此类测试", "Create this test type"))}</a>'
    body += (
        '</div><form method="get" action="/assets" class="asset-filters"><input type="hidden" name="project" value="'
        + esc(project)
        + '">'
    )
    if category:
        body += '<input type="hidden" name="category" value="' + esc(category) + '">'
    if plans_only:
        body += '<input type="hidden" name="view" value="plans">'
    body += (
        "<label>"
        + esc(text("查找资产", "Find assets"))
        + '<input name="q" value="'
        + esc(search)
        + '" placeholder="'
        + esc(text("名称、标识或平台", "Name, ID or platform"))
        + '"></label><label>'
        + esc(text("资产类型", "Asset type"))
        + '<select name="role"><option value="">'
        + esc(text("全部类型", "All asset types"))
        + "</option>"
        + "".join(
            f'<option value="{key}" {"selected" if role == key else ""}>{esc(text(*copy))}</option>'
            for key, copy in ROLE_COPY.items()
            if key in available_roles
        )
        + "</select></label><button>"
        + esc(text("筛选", "Filter"))
        + '</button><a class="text-link" href="'
        + esc(href(project, "/assets"))
        + '">'
        + esc(text("重置筛选", "Reset filters"))
        + "</a></form>"
    )
    filtered = filter_assets(
        assets, category=category, role=role, query=search, plans_only=plans_only
    )
    try:
        current = max(1, int((query.get("page") or ["1"])[0]))
    except ValueError:
        current = 1
    pages = max(1, (len(filtered) + 19) // 20)
    current = min(current, pages)
    body += (
        '<p class="field-hint">'
        + esc(
            text(
                f"共 {len(filtered)} 项。目标用例和固化计划分别保存，并在详情中关联。",
                f"{len(filtered)} assets. Goal cases and compiled plans are stored separately and linked in their details.",
            )
        )
        + "</p>"
    )
    if category == "api":
        body += (
            '<div class="button-row"><a class="button ghost" href="'
            + esc(href(project, "/interfaces"))
            + '">'
            + esc(text("管理接口文档与契约", "Manage API documents and contracts"))
            + '</a><a class="button ghost" href="'
            + esc(href(project, "/cases"))
            + '">'
            + esc(text("管理 API 用例", "Manage API cases"))
            + "</a></div>"
        )
    body += (
        '<div class="table-wrap"><table><thead><tr>'
        + "".join(
            "<th>" + esc(text(zh, en)) + "</th>"
            for zh, en in [
                ("名称", "Name"),
                ("测试类型", "Test type"),
                ("资产类型", "Asset type"),
                ("状态", "Status"),
                ("平台 / 标识", "Platform / ID"),
            ]
        )
        + "</tr></thead><tbody>"
    )
    for asset in filtered[(current - 1) * 20 : current * 20]:
        body += f'<tr data-asset-key="{esc(asset["key"])}"><td><a href="{esc(asset_url(project, asset))}"><strong>{esc(asset["title"])}</strong></a></td><td>{esc(category_title(asset["category"]))}</td><td>{esc(text(*ROLE_COPY[asset["role"]]))}</td><td>{esc(state_title(asset["state"]))}</td><td>{esc(asset["kind"])}<br><small>{esc(asset["id"])}</small></td></tr>'
    body += "</tbody></table></div>"
    if not filtered:
        body += (
            '<p class="empty-state">'
            + esc(
                text(
                    "此筛选下暂无资产。创建测试、导入资料或完成 AI 探索后会显示在这里；未完成的候选也会保留其真实状态。",
                    "No matching assets. Create a test, import reference material or complete AI exploration. Incomplete candidates keep their actual status.",
                )
            )
            + "</p>"
        )
    body += (
        '<nav class="asset-pagination" aria-label="'
        + esc(text("资产分页", "Asset pages"))
        + '">'
    )
    for number in (current - 1, current + 1):
        if 1 <= number <= pages:
            args = {"category": category, "role": role, "q": search, "page": number}
            if plans_only:
                args["view"] = "plans"
            body += f'<a class="button ghost" href="{esc(href(project, "/assets", **args))}">{esc(text("上一页", "Previous") if number < current else text("下一页", "Next"))}</a>'
    body += f"<span>{current} / {pages}</span></nav></section>"
    return page(root, project, label("assets"), body, area="assets")
