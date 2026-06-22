from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ai_apitest.core.models import CaseResult
from ai_apitest.core.paths import project_reports_dir, safe_project_name

SENSITIVE_KEYS = ("authorization", "token", "api_key", "apikey", "secret", "password", "cookie")
MAX_SNAPSHOT_CHARS = 12000


def write_report(
    root: Path,
    results: list[CaseResult],
    project: str = "default",
    analysis: str = "",
    ai_review: str = "",
) -> Path:
    now = datetime.now()
    report_path = project_reports_dir(root, project) / f"report-{now.strftime('%Y%m%d-%H%M%S')}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    passed = sum(1 for item in results if item.status == "passed")
    failed = sum(1 for item in results if item.status == "failed")
    skipped = sum(1 for item in results if item.status == "skipped")
    setup_failed = sum(1 for item in results if lifecycle_status(item.setup) in {"failed", "skipped"})
    cleanup_failed = sum(1 for item in results if item.cleanup.get("status") == "failed")
    cleanup_skipped = sum(1 for item in results if item.cleanup.get("status") == "skipped")
    total = len(results)
    lines = [
        f"# {safe_project_name(project)} 测试执行报告 - {now.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 总览",
        "",
        "| 指标 | 值 |",
        "|------|-----|",
        f"| 执行用例数 | {total} |",
        f"| 通过 | {passed} |",
        f"| 失败 | {failed} |",
        f"| 跳过 | {skipped} |",
        f"| 准备失败 | {setup_failed} |",
        f"| 清理失败 | {cleanup_failed} |",
        f"| 清理跳过 | {cleanup_skipped} |",
        "",
        "## 明细",
        "",
        "| 用例 | 接口 | 状态 | 状态码 | 耗时 |",
        "|------|------|------|--------|------|",
    ]
    for result in results:
        lines.append(
            f"| {result.case_id} {result.title} | {result.method} {result.path} | "
            f"{result.status} | {result.status_code or '-'} | {result.elapsed_ms}ms |"
        )
    failures = [item for item in results if item.status == "failed"]
    if failures:
        lines.extend(["", "## 失败详情", ""])
        for result in failures:
            lines.extend(
                [
                    f"### {result.case_id} {result.title}",
                    "",
                    f"- 接口：`{result.method} {result.path}`",
                    f"- 设计方法：{result.design_method or '-'}",
                    f"- 用例风险：{result.case_risk or '-'}",
                    f"- 预期结果：{result.expected_result or '以断言列表为准'}",
                    f"- 错误：{result.error or '断言失败'}",
                    "- 归因提示：断言失败不一定代表接口缺陷，请结合下方前置条件、用例步骤和测试数据判断是否为测试数据或用例预期问题。",
                ]
            )
            if result.preconditions:
                lines.append("- 前置条件：")
                for item in result.preconditions:
                    lines.append(f"  - {item}")
            if result.test_steps:
                lines.append("- 用例步骤：")
                for index, item in enumerate(result.test_steps, start=1):
                    lines.append(f"  {index}. {item}")
            for error in result.assertion_errors:
                lines.append(f"- 断言：{error}")
            lines.append("")
    if results:
        lines.extend(["", "## 测试数据", "", "以下内容为本次执行时实际发出的请求、收到的响应和断言结果，用于判断失败是否由测试数据、断言规则或接口实现造成。", ""])
        for result in results:
            lines.extend(case_data_block(result))
    if ai_review:
        lines.extend(["", "## AI 失败复核", "", "> AI 复核仅用于辅助归因，不会覆盖自动化断言的原始执行结果。", "", ai_review.strip(), ""])
    if analysis:
        lines.extend(["", "## AI 测试解读", "", analysis.strip(), ""])
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def case_data_block(result: CaseResult) -> list[str]:
    status_code = result.status_code if result.status_code is not None else "-"
    summary = f"{result.case_id} {result.title}｜{result.status}｜{status_code}｜{result.elapsed_ms}ms"
    request_snapshot = normalize_snapshot(result.request)
    response_snapshot = normalize_snapshot(result.response)
    setup_snapshot = normalize_snapshot(result.setup)
    cleanup_snapshot = normalize_snapshot(result.cleanup)
    assertions = {
        "preconditions": result.preconditions,
        "test_steps": result.test_steps,
        "expected_result": result.expected_result,
        "case_risk": result.case_risk,
        "design_method": result.design_method,
        "error": result.error or "",
        "assertion_errors": result.assertion_errors,
    }
    return [
        "<details>",
        f"<summary>{summary}</summary>",
        "",
        "#### 请求数据",
        "",
        "```json",
        format_snapshot(request_snapshot),
        "```",
        "",
        "#### 前置准备结果",
        "",
        "```json",
        format_snapshot(setup_snapshot or {"status": "not_configured"}),
        "```",
        "",
        "#### 响应数据",
        "",
        "```json",
        format_snapshot(response_snapshot),
        "```",
        "",
        "#### 清理结果",
        "",
        "```json",
        format_snapshot(cleanup_snapshot or {"status": "not_configured"}),
        "```",
        "",
        "#### 断言结果",
        "",
        "```json",
        format_snapshot(assertions),
        "```",
        "",
        "</details>",
        "",
    ]


def lifecycle_status(value: dict[str, Any]) -> str:
    if not value:
        return ""
    if "environment_setup" in value and isinstance(value["environment_setup"], dict):
        return str(value["environment_setup"].get("status", ""))
    return str(value.get("status", ""))


def normalize_snapshot(value: Any) -> Any:
    if isinstance(value, dict):
        normalized = {}
        for key, item in value.items():
            key_text = str(key)
            if is_sensitive_key(key_text):
                normalized[key] = "***"
            elif key_text == "url" and isinstance(item, str):
                normalized[key] = redact_url(item)
            elif key_text == "headers" and isinstance(item, dict):
                normalized[key] = redact_headers(item)
            else:
                normalized[key] = normalize_snapshot(item)
        return normalized
    if isinstance(value, list):
        return [normalize_snapshot(item) for item in value]
    return value


def redact_headers(headers: dict[str, Any]) -> dict[str, Any]:
    return {key: "***" if is_sensitive_key(str(key)) else normalize_snapshot(value) for key, value in headers.items()}


def redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    query = []
    changed = False
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if is_sensitive_key(key):
            query.append((key, "***"))
            changed = True
        else:
            query.append((key, value))
    if not changed:
        return url
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    return any(item in lowered for item in SENSITIVE_KEYS)


def format_snapshot(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    if len(text) <= MAX_SNAPSHOT_CHARS:
        return text
    return text[:MAX_SNAPSHOT_CHARS] + "\n... 内容过长，报告中已截断 ..."
