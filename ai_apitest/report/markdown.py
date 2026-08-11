from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ai_apitest.core.models import CaseResult
from ai_apitest.core.artifact_language import (
    artifact_text,
    configured_artifact_language,
    current_artifact_language,
    normalize_artifact_language,
    set_current_artifact_language,
)
from ai_apitest.core.config import load_config
from ai_apitest.core.files import atomic_write_text
from ai_apitest.core.paths import project_reports_dir, safe_project_name

SENSITIVE_KEYS = ("authorization", "token", "api_key", "apikey", "secret", "password", "cookie")
MAX_SNAPSHOT_CHARS = 12000
REPORT_FILENAME_PATTERN = re.compile(r"report-\d{8}-\d{6}(?:-\d{6})?\.md")


def report_filename(now: datetime) -> str:
    return f"report-{now.strftime('%Y%m%d-%H%M%S-%f')}.md"


def is_report_filename(name: str) -> bool:
    """Accept current microsecond names and read-only legacy names."""
    return bool(REPORT_FILENAME_PATTERN.fullmatch(str(name or "")))


def write_report(
    root: Path,
    results: list[CaseResult],
    project: str = "default",
    analysis: str = "",
    ai_review: str = "",
    artifact_language: str | None = None,
) -> Path:
    language = configured_artifact_language(load_config(root), artifact_language)
    set_current_artifact_language(language)
    now = datetime.now()
    report_path = project_reports_dir(root, project) / report_filename(now)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        report_path,
        render_report_markdown(
            results,
            project=project,
            analysis=analysis,
            ai_review=ai_review,
            now=now,
            artifact_language=language,
        ),
    )
    prune_reports(root, project)
    return report_path


def render_report_markdown(
    results: list[CaseResult],
    *,
    project: str = "default",
    analysis: str = "",
    ai_review: str = "",
    now: datetime | None = None,
    artifact_language: str | None = None,
) -> str:
    """Pure report renderer used by both quality audit and final persistence."""
    now = now or datetime.now()
    language = normalize_artifact_language(
        artifact_language or current_artifact_language()
    )
    t = lambda key: artifact_text(key, language)
    passed = sum(1 for item in results if item.status == "passed")
    failed = sum(1 for item in results if item.status == "failed")
    skipped = sum(1 for item in results if item.status == "skipped")
    setup_failed = sum(1 for item in results if lifecycle_status(item.setup) in {"failed", "skipped"})
    cleanup_failed = sum(1 for item in results if lifecycle_contains_status(item.cleanup, "failed"))
    cleanup_skipped = sum(1 for item in results if lifecycle_contains_status(item.cleanup, "skipped"))
    total = len(results)
    lines = [
        f"# {safe_project_name(project)} {t('test_execution_report')} - {now.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"## {t('overview')}",
        "",
        f"| {t('metric')} | {t('value')} |",
        "|------|-----|",
        f"| {t('executed_cases')} | {total} |",
        f"| {t('passed')} | {passed} |",
        f"| {t('failed')} | {failed} |",
        f"| {t('skipped')} | {skipped} |",
        f"| {t('setup_failed')} | {setup_failed} |",
        f"| {t('cleanup_failed')} | {cleanup_failed} |",
        f"| {t('cleanup_skipped')} | {cleanup_skipped} |",
        "",
        f"## {t('details')}",
        "",
        f"| {t('case')} | {t('api')} | {t('status')} | {t('status_code')} | {t('duration')} |",
        "|------|------|------|--------|------|",
    ]
    for result in results:
        lines.append(
            f"| {result.case_id} {result.title} | {result.method} {result.path} | "
            f"{result.status} | {result.status_code or '-'} | {result.elapsed_ms}ms |"
        )
    failures = [item for item in results if item.status == "failed"]
    if failures:
        lines.extend(["", f"## {t('failure_details')}", ""])
        for result in failures:
            lines.extend(
                [
                    f"### {result.case_id} {result.title}",
                    "",
                    f"- {t('api')}: `{result.method} {result.path}`",
                    f"- {t('design_method')}: {result.design_method or '-'}",
                    f"- {t('risk')}: {result.case_risk or '-'}",
                    f"- {t('expected_result')}: {result.expected_result or artifact_text('assertions_define_expected', language)}",
                    f"- {t('error')}: {result.error or t('failed')}",
                    f"- {t('attribution_hint')}: {t('failure_not_always_defect')}",
                ]
            )
            if result.preconditions:
                lines.append(f"- {t('preconditions')}:")
                for item in result.preconditions:
                    lines.append(f"  - {item}")
            if result.test_steps:
                lines.append(f"- {t('case_steps')}:")
                for index, item in enumerate(result.test_steps, start=1):
                    lines.append(f"  {index}. {item}")
            for error in result.assertion_errors:
                lines.append(f"- {t('assertions')}: {error}")
            lines.append("")
    if results:
        lines.extend(["", f"## {t('test_data')}", "", t('test_data_note'), ""])
        for result in results:
            lines.extend(case_data_block(result, artifact_language=language))
    if ai_review:
        lines.extend(["", f"## {t('ai_failure_review')}", "", f"> {t('ai_review_note')}", "", ai_review.strip(), ""])
    if analysis:
        lines.extend(["", f"## {t('ai_test_analysis')}", "", analysis.strip(), ""])
    return "\n".join(lines)


def prune_reports(root: Path, project: str) -> None:
    retain = max(10, int(load_config(root).get("retention", {}).get("reports", 200)))
    reports = sorted(project_reports_dir(root, project).glob("report-*.md"), key=lambda item: item.stat().st_mtime, reverse=True)
    for path in reports[retain:]:
        path.unlink(missing_ok=True)


def case_data_block(
    result: CaseResult,
    *,
    artifact_language: str | None = None,
) -> list[str]:
    language = normalize_artifact_language(
        artifact_language or current_artifact_language()
    )
    t = lambda key: artifact_text(key, language)
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
        f"#### {t('request_data')}",
        "",
        "```json",
        format_snapshot(request_snapshot),
        "```",
        "",
        f"#### {t('setup_result')}",
        "",
        "```json",
        format_snapshot(setup_snapshot or {"status": "not_configured"}),
        "```",
        "",
        f"#### {t('response_data')}",
        "",
        "```json",
        format_snapshot(response_snapshot),
        "```",
        "",
        f"#### {t('cleanup_result')}",
        "",
        "```json",
        format_snapshot(cleanup_snapshot or {"status": "not_configured"}),
        "```",
        "",
        f"#### {t('assertion_results')}",
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
    if value.get("status"):
        return str(value["status"])
    if "environment_setup" in value and isinstance(value["environment_setup"], dict):
        return str(value["environment_setup"].get("status", ""))
    for status in ("failed", "skipped", "passed", "not_needed"):
        if lifecycle_contains_status(value, status):
            return status
    return ""


def lifecycle_contains_status(value: Any, status: str) -> bool:
    if isinstance(value, dict):
        return value.get("status") == status or any(lifecycle_contains_status(item, status) for item in value.values())
    if isinstance(value, list):
        return any(lifecycle_contains_status(item, status) for item in value)
    return False


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
