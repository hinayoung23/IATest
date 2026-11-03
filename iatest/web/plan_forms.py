"""Human-oriented forms which compile into the existing validated plan contracts."""

from __future__ import annotations

import csv
import io
import json

from iatest.web.workspace import esc, text


def input_field(name, zh, en, value="", *, kind="text", hint="", required=True):
    control = (
        f'<textarea name="{esc(name)}" rows="4" {"required" if required else ""}>{esc(value)}</textarea>'
        if kind == "textarea"
        else f'<input name="{esc(name)}" type="{esc(kind)}" value="{esc(value)}" {"required" if required else ""}>'
    )
    return (
        f"<label>{esc(text(zh, en))}{control}"
        + (f'<small class="field-hint">{esc(hint)}</small>' if hint else "")
        + "</label>"
    )


def select_field(name, zh, en, options, *, required=True):
    return (
        f'<label>{esc(text(zh, en))}<select name="{esc(name)}" {"required" if required else ""}>'
        + "".join(
            f'<option value="{esc(value)}">{esc(title)}</option>'
            for value, title in options
        )
        + "</select></label>"
    )


def saved_options(store, *, kinds=None, include_mobile=False):
    options = [
        (p["id"], (p.get("title") or p["id"]) + " · " + p["kind"])
        for p in store.plans()
        if kinds is None or p["kind"] in kinds
    ]
    if include_mobile:
        from iatest.mobile.store import MobileStore

        options.extend(
            ("mobile:" + p["id"], (p.get("title") or p["id"]) + " · " + p["kind"])
            for p in MobileStore(store.root, store.project).list("plan")
            if kinds is None or p["kind"] in kinds
        )
    return options


def saved_plan(store, reference):
    if reference.startswith("mobile:"):
        from iatest.mobile.store import MobileStore

        return MobileStore(store.root, store.project).get(
            "plan", reference.removeprefix("mobile:")
        )
    return store.get_plan(reference)


def guided_plan_form(project, kind):
    from iatest.web.testing import form

    fields = f'<input type="hidden" name="kind" value="{kind}">'
    fields += input_field(
        "id",
        "计划名称（唯一标识）",
        "Plan ID",
        hint=text(
            "使用字母、数字、短横线，例如 order-data-check。",
            "Use letters, digits and hyphens, e.g. order-data-check.",
        ),
    )
    if kind == "data":
        fields += input_field(
            "csv",
            "粘贴 CSV 表格",
            "Paste a CSV table",
            kind="textarea",
            hint=text(
                "首行为列名。例如：id,name；后续每行一条记录，使用英文逗号分隔。最多 10,000 行；空单元格按 null 处理。",
                "First row: column names. Following rows: records separated by commas. Up to 10,000 records; empty cells become null.",
            ),
        )
        fields += select_field(
            "rule",
            "检查规则",
            "Rule",
            [
                ("unique", text("指定列不能重复", "Unique column")),
                ("not_null", text("指定列不能为空", "Non-empty column")),
                ("count", text("记录总数范围", "Record count range")),
            ],
        )
        fields += input_field(
            "column",
            "检查的列名",
            "Column name",
            required=False,
            hint=text(
                "重复/空值检查必填，需与首行列名完全一致；数量检查可留空。",
                "Required for unique/non-empty checks; match the header exactly. Not used for counts.",
            ),
        )
        fields += input_field(
            "min_count",
            "最少记录数（数量规则）",
            "Minimum count (count rule)",
            "1",
            kind="number",
        ) + input_field(
            "max_count",
            "最多记录数（数量规则）",
            "Maximum count (count rule)",
            "10000",
            kind="number",
        )
    elif kind in {"protocol", "accessibility"}:
        fields += input_field(
            "url",
            "目标地址",
            "Target URL",
            hint=text(
                "填写测试服务的完整地址；协议连接使用 ws://、wss://、mqtt:// 或 mqtts://。",
                "Full test URL; protocol connections use ws://, wss://, mqtt:// or mqtts://.",
            ),
        )
        fields += input_field(
            "origins",
            "允许访问的来源（每行一个）",
            "Allowed origins (one per line)",
            kind="textarea",
            hint=text(
                "填写 协议://主机:端口，不含路径。必须包含目标和所需资源的来源。",
                "Use scheme://host:port without a path. Include the target and required resource origins.",
            ),
        )
        if kind == "protocol":
            fields += select_field(
                "transport",
                "交互协议",
                "Transport",
                [("http", "HTTP"), ("websocket", "WebSocket"), ("mqtt", "MQTT 3.1.1")],
            )
            fields += input_field(
                "status",
                "HTTP 预期状态码",
                "Expected HTTP status",
                "200",
                kind="number",
            )
            fields += input_field(
                "send",
                "发送文字（WebSocket / MQTT）",
                "Message to send (WebSocket / MQTT)",
                required=False,
            )
            fields += input_field(
                "receive",
                "预期收到的文字（WebSocket / MQTT）",
                "Expected reply (WebSocket / MQTT)",
                required=False,
            )
            fields += input_field(
                "topic",
                "MQTT 主题",
                "MQTT topic",
                required=False,
                hint=text(
                    "MQTT 会先订阅、再发布并检查收到的文字。gRPC、多消息和协议负载测试见高级配置。",
                    "MQTT subscribes, publishes and checks the received text. Use advanced settings for gRPC, multiple exchanges and protocol load.",
                ),
            )
        else:
            fields += select_field(
                "rule",
                "检查内容",
                "Check",
                [
                    ("document_title", text("页面有标题", "Page title")),
                    (
                        "accessible_name",
                        text("控件有可访问名称", "Accessible control name"),
                    ),
                    ("image_alt", text("图片有替代文字", "Image alternative text")),
                ],
            )
            fields += input_field(
                "selector",
                "控件 CSS 选择器",
                "Control CSS selector",
                required=False,
                hint=text(
                    "检查页面标题时留空；其它规则必填，例如 #submit 或 img.logo。",
                    "Leave empty for page title; otherwise required, e.g. #submit or img.logo.",
                ),
            )
            fields += input_field(
                "expected",
                "预期文字（可选）",
                "Expected text (optional)",
                required=False,
            )
    elif kind == "desktop":
        fields += select_field(
            "platform",
            "所在系统",
            "Platform",
            [("macos", "macOS"), ("windows", "Windows"), ("linux", "Linux")],
        )
        fields += input_field(
            "process_id", "已运行应用的进程 ID", "Running app process ID", kind="number"
        )
        fields += input_field(
            "app_id",
            "应用身份",
            "App identity",
            hint=text(
                "macOS 为 Bundle ID；Windows/Linux 使用目标进程对应的应用身份。",
                "Bundle ID on macOS; the target process app identity on Windows/Linux.",
            ),
        )
        fields += select_field(
            "op",
            "操作",
            "Action",
            [
                ("click", text("点击控件", "Click control")),
                ("fill", text("填写文字", "Fill text")),
            ],
        )
        fields += input_field(
            "identifier", "操作控件的标识", "Action control identifier"
        ) + input_field("value", "填写的文字", "Text to fill", required=False)
        fields += input_field(
            "expected_identifier", "结果控件的标识", "Result control identifier"
        )
        fields += select_field(
            "attribute",
            "读取结果属性",
            "Result attribute",
            [
                ("value", text("控件值", "Value")),
                ("text", text("文字", "Text")),
                ("name", text("名称", "Name")),
            ],
        )
        fields += input_field("expected", "预期结果文字", "Expected result text")
    fields += f"<button>{esc(text('保存测试计划', 'Save test plan'))}</button>"
    return (
        '<section class="guided-create"><h2>'
        + esc(text("创建测试", "Create test"))
        + "</h2>"
        + form("build", project, fields)
        + "</section>"
    )


def build_guided_plan(kind, get):
    """Do not run anything here; EngineStore performs full contract validation."""
    spec = {}
    if kind == "data":
        source = get("csv")
        if len(source.encode()) > 2 * 1024 * 1024:
            raise ValueError(text("CSV 不能超过 2 MiB。", "CSV must not exceed 2 MiB."))
        try:
            reader = csv.DictReader(io.StringIO(source), strict=True)
            headers = reader.fieldnames
        except csv.Error as exc:
            raise ValueError(
                text(
                    "CSV 首行格式错误，请检查引号。",
                    "Invalid CSV header: check quoting.",
                )
            ) from exc
        if (
            not headers
            or any(not x.strip() for x in headers)
            or len(set(headers)) != len(headers)
        ):
            raise ValueError(
                text(
                    "CSV 首行需要非空且不重复的列名。",
                    "CSV needs non-empty, unique column headers.",
                )
            )
        records = []
        try:
            for row in reader:
                if None in row or any(v is None for v in row.values()):
                    raise ValueError(
                        text(
                            "CSV 每行列数必须与首行一致。",
                            "Each CSV row must match the header column count.",
                        )
                    )
                records.append({k: None if v == "" else v for k, v in row.items()})
                if len(records) > 10000:
                    raise ValueError(
                        text(
                            "CSV 最多 10,000 行记录。",
                            "CSV supports up to 10,000 records.",
                        )
                    )
        except csv.Error as exc:
            raise ValueError(
                text(
                    "CSV 格式错误，请检查引号和列分隔。",
                    "Invalid CSV: check quoting and column delimiters.",
                )
            ) from exc
        if not records:
            raise ValueError(
                text("请至少提供一行 CSV 数据。", "Provide at least one CSV record.")
            )
        rule = get("rule")
        check = {"id": "table-check", "rule": rule}
        if rule == "count":
            minimum, maximum = (
                int(get("min_count", "1")),
                int(get("max_count", "10000")),
            )
            if minimum > maximum:
                raise ValueError(
                    text(
                        "最少数量不能大于最多数量。",
                        "Minimum count must not exceed maximum count.",
                    )
                )
            check.update(min=minimum, max=maximum)
        elif rule in {"unique", "not_null"}:
            column = get("column").strip()
            if column not in headers:
                raise ValueError(
                    text(
                        "检查的列名必须存在于 CSV 首行。",
                        "The column must exist in the CSV header.",
                    )
                )
            check["path"] = "$[" + json.dumps(column, ensure_ascii=False) + "]"
        else:
            raise ValueError("Unsupported table rule")
        spec = {"records": records, "checks": [check]}
    elif kind in {"protocol", "accessibility"}:
        spec = {
            "allowed_origins": [
                s.strip() for s in get("origins").splitlines() if s.strip()
            ]
        }
        if kind == "accessibility":
            rule = get("rule")
            check = {"id": "page-check", "rule": rule}
            if rule != "document_title":
                check["target"] = {"selector": get("selector")}
            if get("expected"):
                check["expected"] = get("expected")
            spec.update(start_url=get("url"), checks=[check])
        elif get("transport", "http") == "http":
            spec["checks"] = [
                {
                    "id": "http-response",
                    "request": {
                        "url": get("url"),
                        "expected_status": [int(get("status", "200"))],
                    },
                }
            ]
        else:
            transport = get("transport")
            if transport not in {"websocket", "mqtt"}:
                raise ValueError("Unsupported guided protocol")
            from iatest.core.sandbox import origin
            from iatest.engines.wire import endpoint

            spec["allowed_origins"] = [
                origin(endpoint(s, transport)[3])
                if "://" in s and s.split(":", 1)[0] in {"ws", "wss", "mqtt", "mqtts"}
                else s
                for s in spec["allowed_origins"]
            ]
            if not get("receive"):
                raise ValueError(
                    text(
                        "请填写预期收到的文字，避免只验证发送成功。",
                        "Provide an expected reply, not just a send receipt.",
                    )
                )
            exchanges = (
                [
                    {"op": "send", "text": get("send")},
                    {"op": "receive", "expect_text": get("receive")},
                ]
                if transport == "websocket"
                else [
                    {"op": "subscribe", "topic": get("topic"), "qos": 0},
                    {
                        "op": "publish",
                        "topic": get("topic"),
                        "text": get("send"),
                        "qos": 0,
                    },
                    {
                        "op": "receive",
                        "topic": get("topic"),
                        "expect_text": get("receive"),
                    },
                ]
            )
            spec.update(transport=transport, endpoint=get("url"), exchanges=exchanges)
    elif kind == "desktop":
        step = {"op": get("op"), "target": {"identifier": get("identifier")}}
        if step["op"] == "fill":
            step["value"] = get("value")
        spec = {
            "platform": get("platform"),
            "process_id": int(get("process_id")),
            "app_id": get("app_id"),
            "steps": [
                step,
                {
                    "op": "expect",
                    "target": {"identifier": get("expected_identifier")},
                    "attribute": get("attribute", "value"),
                    "expected": get("expected"),
                },
            ],
        }
    return {
        "id": get("id"),
        "kind": kind,
        "spec": spec,
        "budget": {"timeout_seconds": 120},
    }


def operation_form(root, project, kind, store):
    """Common operations use fields; specialized contracts stay in advanced panels."""
    from iatest.mobile.store import MobileStore
    from iatest.nodes.store import ControllerStore
    from iatest.web.testing import form

    fields = ""
    action = "guided-operation"
    task = ""
    if kind == "apps":
        fields = input_field("id", "安装包标识", "Package ID") + select_field(
            "platform", "系统", "Platform", [("android", "Android"), ("ios", "iOS")]
        )
        fields += input_field(
            "app_id", "应用包名 / Bundle ID", "Package name / Bundle ID"
        ) + input_field("url", "下载地址", "Download URL", kind="url")
        fields += input_field(
            "origins",
            "允许下载的来源（每行一个）",
            "Allowed download origins (one per line)",
            kind="textarea",
        )
        fields += input_field(
            "sha256",
            "发布方提供的 SHA-256",
            "Publisher SHA-256",
            hint=text(
                "输入安装包真实摘要以校验来源，不能使用示例值。下载不会自动安装。",
                "Use the actual package digest. Downloading does not install it.",
            ),
        )
        fields += '<input type="hidden" name="operation" value="app-fetch">'
        body = form(
            action,
            project,
            fields
            + f"<button>{esc(text('下载并校验安装包', 'Download and verify'))}</button>",
            task="testing-app-fetch",
        )
        profiles = [
            (p["id"], p["id"] + " · " + p["platform"])
            for p in MobileStore(root, project).list("profile")
        ]
        apps = [(a["id"], a["id"]) for a in store.artifacts("app")]
        if profiles and apps:
            install = select_field(
                "profile", "设备配置", "Device profile", profiles
            ) + select_field("artifact_id", "已校验安装包", "Verified package", apps)
            install += select_field(
                "mode",
                "安装方式",
                "Install mode",
                [
                    ("upgrade", text("覆盖升级（保留数据）", "Upgrade (keep data)")),
                ],
            )
            install += '<input type="hidden" name="operation" value="app-prepare">'
            body += (
                "<h3>"
                + esc(text("安装到已配置设备", "Prepare on a configured device"))
                + "</h3>"
                + form(
                    action,
                    project,
                    install
                    + f"<button>{esc(text('准备应用', 'Prepare app'))}</button>",
                    task="testing-app-prepare",
                )
            )
        else:
            body += (
                '<p class="empty-state">'
                + esc(
                    text(
                        "安装前需先保存设备配置并下载校验安装包。",
                        "Save a device profile and verify a package before installation.",
                    )
                )
                + "</p>"
            )
        return (
            '<section class="guided-create"><h2>'
            + esc(text("应用交付", "App delivery"))
            + "</h2>"
            + body
            + "</section>"
        )
    if kind in {"remote", "client_performance"}:
        options = saved_options(
            store,
            kinds={"web", "android", "ios"} if kind == "client_performance" else None,
            include_mobile=True,
        )
        if not options:
            return (
                '<section class="empty-state">'
                + esc(
                    text(
                        "先在测试工作台保存一个子计划，再到此处选择并组合。",
                        "Save a child plan in the test workspace, then return to select it.",
                    )
                )
                + "</section>"
            )
        fields = input_field("id", "新计划标识", "New plan ID") + select_field(
            "plan_id", "要运行的已有计划", "Saved child plan", options
        )
        if kind == "client_performance":
            fields += (
                '<p class="field-hint">'
                + esc(
                    text(
                        "浏览器计划选择导航耗时；Android 可选择 CPU 或 PSS。iOS 专业采集与离线指标请使用下方高级工具。指标缺测时不会判为通过。",
                        "Use navigation timing for browser plans, CPU/PSS for Android. Use advanced tools for iOS recording and offline metrics. Missing measurements do not pass.",
                    )
                )
                + "</p>"
            )
        if kind == "remote":
            nodes = [(n["id"], n["id"]) for n in ControllerStore(root, project).nodes()]
            if not nodes:
                return (
                    '<section class="empty-state">'
                    + esc(
                        text(
                            "尚无执行节点。先到“设备与环境 → 执行节点”登记节点，再创建远程计划。",
                            "Enroll a node under Devices & environments before creating a remote plan.",
                        )
                    )
                    + "</section>"
                )
            fields += select_field("node_id", "执行节点", "Execution node", nodes)
        else:
            fields += input_field(
                "iterations", "采样次数", "Iterations", "3", kind="number"
            ) + input_field(
                "warmup", "预热次数", "Warmup iterations", "1", kind="number"
            )
            fields += input_field(
                "threshold", "门禁上限", "Threshold maximum", kind="number"
            )
            fields += select_field(
                "metric",
                "门禁指标",
                "Threshold metric",
                [
                    (
                        "max_navigation_ms",
                        text("浏览器导航耗时（毫秒）", "Browser navigation (ms)"),
                    ),
                    (
                        "max_cpu_percent",
                        text("手机 App CPU（%）", "Mobile App CPU (%)"),
                    ),
                    (
                        "max_memory_pss_kb",
                        text(
                            "Android App 内存 PSS（KiB）",
                            "Android App PSS memory (KiB)",
                        ),
                    ),
                ],
            )
        fields += f'<input type="hidden" name="kind" value="{kind}">'
        action = "build"
    elif kind == "campaigns":
        options = saved_options(store)
        if not options:
            return (
                '<section class="empty-state">'
                + esc(
                    text(
                        "先创建测试计划，再选择本次版本需要验证的项目。",
                        "Create test plans first, then choose which to include in this release.",
                    )
                )
                + "</section>"
            )
        fields = (
            input_field("id", "版本任务标识", "Release task ID")
            + input_field("service_version", "构建 / 版本标识", "Build / version")
            + input_field("environment", "测试环境", "Environment")
        )
        fields += (
            "<label>"
            + esc(
                text(
                    "包含的计划（按显示顺序串行运行）",
                    "Included plans (run sequentially in this order)",
                )
            )
            + '<div class="plan-checklist">'
            + "".join(
                f'<label><input type="checkbox" name="plans" value="{esc(value)}">{esc(title)}</label>'
                for value, title in options
            )
            + "</div></label>"
        )
        fields += '<input type="hidden" name="operation" value="campaign-create">'
    elif kind == "nodes":
        fields = input_field("id", "节点标识", "Node ID") + input_field(
            "origins",
            "节点允许访问的测试来源（每行一个）",
            "Allowed test origins (one per line)",
            kind="textarea",
        )
        fields += input_field(
            "token_env",
            "节点令牌的环境变量名",
            "Node token environment variable",
            hint=text(
                "仅填写环境变量名称，不在页面输入令牌值。登记后，在远程电脑启动 worker 连接本机控制器，节点才会上线。",
                "Enter the environment variable name, not a token value. After enrollment, start a worker on the remote computer to connect to this controller.",
            ),
        )
        fields += '<input type="hidden" name="operation" value="node-enroll">'
        task = ""
    else:
        return ""
    return (
        '<section class="guided-create"><h2>'
        + esc(text("基础配置", "Basic configuration"))
        + "</h2>"
        + form(
            action,
            project,
            fields + f"<button>{esc(text('保存配置', 'Save configuration'))}</button>",
            task=task,
        )
        + "</section>"
    )


def operation_payload(operation, data):
    get = lambda key, default="": (data.get(key) or [default])[0]
    if operation == "app-fetch":
        return {
            **{
                k: get(k).strip() for k in ("id", "platform", "app_id", "url", "sha256")
            },
            "allowed_origins": [
                s.strip() for s in get("origins").splitlines() if s.strip()
            ],
        }
    if operation == "app-prepare":
        return {k: get(k) for k in ("profile", "artifact_id", "mode")}
    if operation == "node-enroll":
        return {
            **{k: get(k).strip() for k in ("id", "token_env")},
            "allowed_origins": [
                s.strip() for s in get("origins").splitlines() if s.strip()
            ],
            "allowed_kinds": [
                "api",
                "web",
                "performance",
                "workflow",
                "client_performance",
            ],
        }
    if operation == "campaign-create":
        ids = data.get("plans", [])
        if not ids or len(ids) > 100 or len(ids) != len(set(ids)):
            raise ValueError(
                text("请选择 1–100 个不同计划。", "Choose 1–100 distinct plans.")
            )
        return {
            "id": get("id"),
            "build": {
                "service_version": get("service_version"),
                "environment": get("environment"),
            },
            "max_parallel": 1,
            "nodes": [
                {
                    "id": f"step{i + 1}",
                    "plan_id": p,
                    "depends_on": [] if i == 0 else [f"step{i}"],
                }
                for i, p in enumerate(ids)
            ],
            "budget": {"timeout_seconds": 600, "max_requests": 10000},
        }
    raise ValueError("Unsupported guided operation")
