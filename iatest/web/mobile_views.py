"""Readable device discovery and step-by-step mobile plan configuration."""

from __future__ import annotations

import json

from iatest.web.plan_forms import input_field, select_field
from iatest.web.workspace import esc, href, text


def detection_results(report):
    body = (
        '<section class="device-discovery" aria-label="'
        + esc(text("设备检测结果", "Device detection results"))
        + '"><h2>'
        + esc(text("设备检测结果", "Device detection results"))
        + "</h2><p>"
        + esc(
            text(
                "这是连接检查。发现设备不代表原生驱动已可用；下一步保存配置并打开会话。",
                "This checks connectivity. Discovery does not verify native drivers. Save a profile and open a session next.",
            )
        )
        + '</p><div class="tool-status">'
    )
    for key, title in (("adb", "Android · ADB"), ("xcodebuild", "iOS · Xcode")):
        body += (
            '<span class="badge">'
            + esc(title)
            + " · "
            + esc(
                text("已安装", "Installed")
                if report.get(key)
                else text("未找到", "Not found")
            )
            + "</span>"
        )
    body += "</div>"
    body += (
        '<p class="field-hint">'
        + esc(
            text(
                "上次检测结果；设备状态变化后请重新检测。",
                "Last discovery snapshot; detect again after device state changes.",
            )
        )
        + (" · " + esc(report["checked_at"]) if report.get("checked_at") else "")
        + "</p>"
    )
    devices = report.get("devices", [])
    if devices:
        body += (
            '<div class="table-wrap"><table><thead><tr>'
            + "".join(
                "<th>" + esc(text(zh, en)) + "</th>"
                for zh, en in [
                    ("设备", "Device"),
                    ("系统", "Platform"),
                    ("连接状态", "Connection"),
                    ("下一步", "Next step"),
                ]
            )
            + "</tr></thead><tbody>"
        )
        for device in devices:
            state = device.get("state", "")
            ready = state in {"device", ""}
            status = {
                "device": text("已授权", "Authorized"),
                "unauthorized": text("等待手机授权", "Awaiting device authorization"),
                "offline": text("离线", "Offline"),
            }.get(state, state or device.get("connection", "USB"))
            action = (
                f'<button type="button" class="ghost" data-use-device="{esc(device["device_id"])}" data-platform="{esc(device["platform"])}">'
                + esc(text("用于新配置", "Use in a profile"))
                + "</button>"
                if ready
                else esc(
                    text(
                        "解锁手机并允许 USB 调试；离线时重接数据线。",
                        "Unlock and allow USB debugging; reconnect the cable if offline.",
                    )
                )
            )
            body += (
                "<tr><td><strong>"
                + esc(device.get("model") or device["device_id"])
                + "</strong><br><code>"
                + esc(device["device_id"])
                + "</code></td><td>"
                + esc(device["platform"])
                + "</td><td>"
                + esc(status)
                + "</td><td>"
                + action
                + "</td></tr>"
            )
        body += "</tbody></table></div>"
    else:
        body += (
            '<p class="empty-state">'
            + esc(
                text(
                    "未发现设备。Android 请检查数据线、USB 调试及本机授权；iPhone 请解锁、信任此电脑，并确认 USB 或已配置的网络连接。",
                    "No devices found. Android: check the data cable, USB debugging and computer authorization. iPhone: unlock, trust this computer and check USB or a configured network connection.",
                )
            )
            + "</p>"
        )
    for error in report.get("errors", []):
        ios = error.startswith("USB iOS:")
        body += (
            '<div class="inline-warning" role="status"><strong>'
            + esc(
                text(
                    "iOS 设备服务暂不可用" if ios else "Android 连接检测未完成",
                    "iOS device service unavailable"
                    if ios
                    else "Android discovery incomplete",
                )
            )
            + "</strong><p>"
            + esc(
                text(
                    "可继续使用其它已发现设备。请重接已解锁的 iPhone、确认信任，再重新检测。"
                    if ios
                    else "请确认 Android SDK Platform-Tools 已安装，手机已授权本机，再重新检测。",
                    "Other discovered devices remain available. Reconnect an unlocked iPhone, confirm trust, then retry."
                    if ios
                    else "Check Android SDK Platform-Tools and computer authorization, then retry.",
                )
            )
            + "</p><details><summary>"
            + esc(text("诊断详情", "Diagnostic details"))
            + "</summary><pre>"
            + esc(error)
            + "</pre></details></div>"
        )
    body += (
        "<details><summary>"
        + esc(text("原始检测信息", "Raw discovery information"))
        + "</summary><pre data-i18n-skip>"
        + esc(json.dumps(report, ensure_ascii=False, indent=2))
        + "</pre></details></section>"
    )
    return body


def action_fields(prefix="", number=1):
    from iatest.web.mobile import mode_options

    body = (
        '<h3 class="step-heading">'
        + esc(text(f"操作 {number}", f"Action {number}"))
        + "</h3>"
    )
    body += select_field(
        prefix + "op",
        "执行动作",
        "Action",
        [
            ("tap", text("点击控件", "Tap control")),
            ("text", text("在控件中输入文字", "Enter text in control")),
        ],
    )
    body += input_field(
        prefix + "identifier",
        "目标控件标识",
        "Target control identifier",
        required=False,
        hint=text(
            "从“设备与环境 → 设备调试 → 页面控件概览”复制；不是坐标。",
            "Copy from Devices & environments → Device inspector → Discover page controls; this is not a coordinate.",
        ),
    )
    body += (
        "<details><summary>"
        + esc(text("使用文字定位或高级目标", "Locate by text or advanced target"))
        + "</summary>"
        + input_field(
            prefix + "text", "目标显示文字", "Target visible text", required=False
        )
        + input_field(
            prefix + "label",
            "目标可访问名称",
            "Target accessible label",
            required=False,
        )
        + input_field(
            prefix + "container", "容器标识", "Container identifier", required=False
        )
        + input_field(
            prefix + "target_json",
            "高级目标 JSON（可选）",
            "Advanced target JSON (optional)",
            required=False,
        )
        + "</details>"
    )
    body += input_field(
        prefix + "value",
        "要输入的文字（仅输入动作）",
        "Text to enter (text action only)",
        required=False,
    )
    body += (
        "<details><summary>"
        + esc(text("输入方式", "Text input method"))
        + "</summary><label>"
        + esc(text("平台输入方式", "Platform input method"))
        + '<select name="'
        + esc(prefix + "mode")
        + '">'
        + mode_options()
        + "</select></label></details>"
    )
    return body


def render_plans(project, profiles, plans):
    from iatest.web.mobile import form, step_summary, targets, tr

    body = (
        '<section class="mobile-plan-intro"><h2>'
        + esc(
            text("用操作和结果组成一个测试", "Build a test from actions and an outcome")
        )
        + "</h2><p>"
        + esc(
            text(
                "例如：输入昵称 → 点击保存 → 检查“已保存”。操作控件是要点哪里，结果控件是从哪里读取最终状态；发送成功本身不算通过。",
                "Example: enter a nickname → tap Save → check “Saved”. Action targets receive input; the result target supplies the final state. Sending input alone does not pass the test.",
            )
        )
        + '</p><ol class="workflow-steps"><li>'
        + esc(text("选择设备配置", "Choose a profile"))
        + "</li><li>"
        + esc(text("添加操作步骤", "Add actions"))
        + "</li><li>"
        + esc(text("声明最终结果", "Declare an outcome"))
        + "</li><li>"
        + esc(text("保存后主动运行", "Save, then run"))
        + "</li></ol></section>"
    )
    if not profiles:
        body += (
            '<section class="empty-state"><h3>'
            + esc(text("请先保存设备配置", "Save a device profile first"))
            + "</h3><p>"
            + esc(
                text(
                    "配置记录手机、目标 App 和驱动环境。完成后再创建移动计划。",
                    "A profile identifies the device, target App and driver environment. Create a mobile plan afterwards.",
                )
            )
            + '</p><a class="button" href="'
            + esc(href(project, "/mobile", tab="devices"))
            + '">'
            + esc(text("前往设备配置", "Go to device profiles"))
            + "</a></section>"
        )
    else:
        selector = (
            "<label>"
            + esc(text("使用哪个设备与 App", "Device and App profile"))
            + '<select name="profile" required>'
            + "".join(
                f'<option value="{esc(p["id"])}" data-platform="{esc(p["platform"])}">{esc(p["id"])} · {esc(p["platform"])} · {esc(p["app_id"])}</option>'
                for p in profiles
            )
            + "</select></label>"
        )
        fields = (
            selector
            + input_field("id", "计划标识", "Plan ID")
            + '<input type="hidden" name="step_count" value="1"><div data-mobile-plan-steps><fieldset class="plan-step">'
            + action_fields()
            + "</fieldset></div>"
        )
        fields += (
            '<button type="button" class="ghost" data-add-mobile-step>'
            + esc(text("＋ 添加操作", "＋ Add action"))
            + '</button><template data-mobile-step-template><fieldset class="plan-step">'
            + action_fields("step__INDEX___", 2)
            + '<button type="button" class="ghost" data-remove-mobile-step>'
            + esc(text("移除此操作", "Remove action"))
            + "</button></fieldset></template>"
        )
        fields += (
            '<fieldset class="plan-expectation"><legend>'
            + esc(text("最终必须满足的结果", "Required final outcome"))
            + "</legend><p>"
            + esc(
                text(
                    "填写结果控件的标识或显示文字，再填写它应显示的完整文字。框架会等待并读回，不使用操作回执代替结果。",
                    "Identify the result control by ID or visible text, then specify its full expected text. The framework waits and reads it back independently.",
                )
            )
            + "</p>"
            + targets("expected_")
            + input_field(
                "expected_value", "结果控件应显示的文字", "Expected result control text"
            )
            + '</fieldset><p class="field-hint">'
            + esc(
                text(
                    "保存不操作手机。运行时会连接设备并执行 App 操作，请先确认测试范围。",
                    "Saving does not control the phone. Running connects to the device and performs App actions; confirm your test scope first.",
                )
            )
            + "</p><button>"
            + esc(text("保存移动测试计划", "Save mobile test plan"))
            + "</button>"
        )
        body += (
            '<section class="guided-create"><h2>'
            + esc(text("创建移动测试", "Create mobile test"))
            + "</h2>"
            + form(
                "build-plan",
                project,
                fields,
                **{"class": "mobile-form mobile-plan-builder"},
            )
            + "</section>"
        )
        body += (
            "<section><details><summary>"
            + esc(text("让 AI 辅助生成草稿", "Generate a draft with AI"))
            + "</summary><p>"
            + esc(
                text(
                    "需要已配置模型和可用原生驱动。生成时会打开设备会话、观察并探索 App；草稿需审阅和独立运行。",
                    "Requires a configured model and working native driver. Generation opens a session, observes and explores the App; review and independently run the draft.",
                )
            )
            + "</p>"
            + form(
                "draft",
                project,
                selector
                + input_field(
                    "goal",
                    "业务目标与预期结果",
                    "Business goal and expected outcome",
                    kind="textarea",
                )
                + f"<button>{esc(tr('draft'))}</button>",
                data_task_action="mobile-draft",
            )
            + "</details></section>"
        )
    body += (
        "<section><h2>" + esc(text("已保存的移动计划", "Saved mobile plans")) + "</h2>"
    )
    if not plans:
        body += (
            '<p class="empty-state">'
            + esc(
                text(
                    "暂无计划。先选择设备、添加操作和最终断言。",
                    "No plans yet. Choose a profile, add actions and a final assertion.",
                )
            )
            + "</p>"
        )
    for p in plans:
        body += (
            f'<article class="testing-plan" id="plan-{esc(p["id"])}"><h3>'
            + esc(p.get("title") or p["id"])
            + "</h3><p>"
            + esc(text("设备配置", "Profile"))
            + " · "
            + esc(p["spec"]["profile"])
            + '</p><ol class="mobile-steps">'
            + "".join(step_summary(s) for s in p["spec"]["steps"])
            + "</ol>"
        )
        body += form(
            "run",
            project,
            f'<input type="hidden" name="plan_id" value="{esc(p["id"])}"><button>{esc(tr("run"))}</button>',
            data_task_action="mobile-run",
        )
        body += (
            "<details><summary>"
            + esc(text("高级编辑：完整计划 JSON", "Advanced edit: full plan JSON"))
            + "</summary>"
            + form(
                "plan",
                project,
                f'<textarea name="content" rows="16" data-i18n-skip>{esc(json.dumps(p, ensure_ascii=False, indent=2))}</textarea><button>{esc(tr("save"))}</button>',
            )
            + "</details></article>"
        )
    body += (
        "</section><section><details><summary>"
        + esc(text("导入完整移动计划 JSON", "Import a full mobile plan JSON"))
        + "</summary><p>"
        + esc(
            text(
                "用于已有计划、复杂手势、WebView 或其它扩展操作。首次使用请先填写上方表单。",
                "For existing plans, complex gestures, WebViews and extended actions. Use the form above to get started.",
            )
        )
        + "</p>"
        + form(
            "plan",
            project,
            f'<textarea name="content" rows="12" required></textarea><button>{esc(tr("save"))}</button>',
        )
        + "</details></section>"
    )
    return body


def compile_mobile_steps(data):
    from iatest.web.mobile import target_from_form

    def value(key, default=""):
        return (data.get(key) or [default])[0]

    count = int(value("step_count", "1"))
    if not 1 <= count <= 30:
        raise ValueError(text("操作步骤需为 1–30 个。", "Use 1–30 actions."))
    steps = []
    for index in range(count):
        prefix = "" if index == 0 else f"step{index}_"
        op = value(prefix + "op")
        if op not in {"tap", "text"}:
            raise ValueError(
                text("操作只能是点击或输入。", "Actions must be tap or text.")
            )
        step = {"op": op, "target": target_from_form(data, prefix)}
        if op == "text":
            step.update(value=value(prefix + "value"), mode=value(prefix + "mode"))
        steps.append(step)
    steps.append(
        {
            "op": "expect",
            "target": target_from_form(data, "expected_"),
            "attribute": "text",
            "expected": value("expected_value"),
        }
    )
    return steps
