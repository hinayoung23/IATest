from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ai_apitest.context.storage import (
    apply_known_issue_updates,
    claim_due_extraction_jobs,
    create_extraction_job,
    mark_extraction_job_completed,
    mark_extraction_job_failed,
    upsert_lesson,
)
from ai_apitest.core.paths import safe_project_name
from ai_apitest.core.observability import record_operational_error

LESSONS_BLOCK = "iatest-lessons-json"
KNOWN_ISSUES_BLOCK = "iatest-known-issues-json"


def process_report_analysis(
    root: Path,
    project: str,
    analysis: str,
    *,
    source: str,
    environment: str = "",
    record_job: bool = True,
    llm: dict | None = None,
) -> tuple[str, dict[str, Any]]:
    visible = strip_context_blocks(analysis)
    summary = {
        "lessons_created": 0,
        "lessons_merged": 0,
        "conflicts": 0,
        "known_issue_updates": 0,
        "extraction_job_id": "",
        "error": "",
    }
    try:
        lessons = _extract_block_with_repair(analysis, LESSONS_BLOCK, llm)
    except Exception as exc:
        summary["error"] = f"经验提取失败：{exc}"
        if record_job:
            retryable = bool(llm and llm.get("base_url") and llm.get("model") and llm.get("api_key_env"))
            summary["extraction_job_id"] = create_extraction_job(
                root,
                project,
                trigger="report_analysis",
                source=source,
                status="failed" if retryable else "permanent_failed",
                error=str(exc),
                max_retries=3 if retryable else 0,
                payload={"analysis": analysis, "environment": environment},
            )
        return visible, summary
    if not isinstance(lessons, list):
        summary["error"] = "iatest-lessons-json 不是数组"
        if record_job:
            retryable = bool(llm and llm.get("base_url") and llm.get("model") and llm.get("api_key_env"))
            summary["extraction_job_id"] = create_extraction_job(
                root,
                project,
                trigger="report_analysis",
                source=source,
                status="failed" if retryable else "permanent_failed",
                error=summary["error"],
                max_retries=3 if retryable else 0,
                payload={"analysis": analysis, "environment": environment},
            )
        return visible, summary
    for item in lessons:
        if not isinstance(item, dict) or not item.get("title") or not item.get("content"):
            continue
        if is_low_value_meta_lesson(item):
            continue
        lesson = normalize_extracted_lesson(project, item, source=source, environment=environment)
        result = upsert_lesson(root, project, lesson, actor="ai:analyzer")
        if result.action == "duplicate_merged":
            summary["lessons_merged"] += 1
        else:
            summary["lessons_created"] += 1
        if result.action == "conflict_created":
            summary["conflicts"] += 1
    try:
        issue_updates = _extract_block_with_repair(analysis, KNOWN_ISSUES_BLOCK, llm)
        if isinstance(issue_updates, list):
            update_summary = apply_known_issue_updates(root, project, [item for item in issue_updates if isinstance(item, dict)])
            summary["known_issue_updates"] = update_summary["updated"]
    except Exception as exc:
        record_operational_error(root, project, component="context_extraction", operation="known_issue_updates", error=exc)
    if record_job:
        create_extraction_job(root, project, trigger="report_analysis", source=source, status="completed")
    return visible, summary


def retry_due_report_extractions(root: Path, project: str, limit: int = 10) -> dict[str, int]:
    summary = {"retried": 0, "completed": 0, "failed": 0, "permanent_failed": 0, "skipped": 0}
    for job in claim_due_extraction_jobs(root, project, limit=limit):
        summary["retried"] += 1
        payload = job.get("payload") or {}
        analysis = str(payload.get("analysis") or "")
        if job.get("trigger") != "report_analysis" or not analysis:
            mark_extraction_job_failed(root, project, job["id"], "缺少可重试的报告分析 payload")
            summary["failed"] += 1
            continue
        try:
            from ai_apitest.ai.workflows import configured_llm

            _, result = process_report_analysis(
                root,
                project,
                analysis,
                source=str(job.get("source") or ""),
                environment=str(payload.get("environment") or ""),
                record_job=False,
                llm=configured_llm(root, "analyzer"),
            )
        except Exception as exc:  # pragma: no cover - defensive retry boundary
            record_operational_error(
                root,
                project,
                component="context_extraction",
                operation="retry_report_extraction",
                error=exc,
                detail={"job_id": job["id"]},
            )
            mark_extraction_job_failed(root, project, job["id"], str(exc))
            summary["failed"] += 1
            continue
        if result.get("error"):
            mark_extraction_job_failed(root, project, job["id"], str(result["error"]))
            summary["failed"] += 1
            continue
        mark_extraction_job_completed(root, project, job["id"])
        summary["completed"] += 1
    return summary


def normalize_extracted_lesson(project: str, item: dict[str, Any], *, source: str, environment: str = "") -> dict[str, Any]:
    return {
        "project": safe_project_name(project),
        "scope": str(item.get("scope") or "report"),
        "method": str(item.get("method") or "").upper(),
        "path": str(item.get("path") or ""),
        "lesson_type": str(item.get("lesson_type") or "failure_pattern"),
        "lesson_subtype": str(item.get("lesson_subtype") or ""),
        "title": str(item.get("title") or "")[:200],
        "content": str(item.get("content") or ""),
        "confidence": normalize_confidence(item.get("confidence")),
        "tags": item.get("tags") if isinstance(item.get("tags"), list) else [],
        "environment": str(item.get("environment") or environment or ""),
        "source": source,
        "source_type": "report_analysis",
        "lifecycle_status": "pending",
        "review_status": "unreviewed",
        "verification_status": "unverified",
    }


def normalize_confidence(value: Any) -> str:
    text = str(value or "medium").lower()
    return text if text in {"high", "medium", "low"} else "medium"


META_INSTRUCTION_MARKERS = (
    "后续生成",
    "后续用例生成",
    "生成用例时",
    "后续生成同接口",
    "后续生成同场景",
    "应复用",
    "需要复用",
    "避免生成",
    "不要生成不符合",
    "用例生成时应",
    "生成时应",
    "测试用例应",
)

CONCRETE_FACT_PATTERNS = (
    r"\b[1-5]\d\d\b",
    r"\$\.[A-Za-z0-9_\[\].]+",
    r"\b(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+/[^\s，。；;]+",
    r"/[A-Za-z0-9_{}?&=./:-]+",
    r"\b(?:status_code|json_path|header|body_contains)\b",
    r"\b(?:eq|ne|in|gt|lt|gte|lte|contains|not_empty|exists|is_array)\b",
    r"\b(?:true|false|null)\b",
)


def is_low_value_meta_lesson(item: dict[str, Any]) -> bool:
    """Filter generic prompt-like lessons that do not carry project/interface facts."""
    title = str(item.get("title") or "")
    content = str(item.get("content") or "")
    text = f"{title}\n{content}"
    if not any(marker in text for marker in META_INSTRUCTION_MARKERS):
        return False
    if str(item.get("method") or "").strip() and str(item.get("path") or "").strip():
        return False
    if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in CONCRETE_FACT_PATTERNS):
        return False
    return True


def _extract_block_with_repair(text: str, language: str, llm: dict | None) -> Any:
    """Extract embedded JSON block with optional self-repair.

    Tries fenced block first; on repair, accepts either fenced block or bare JSON
    (the repair model may not re-wrap in the fence).
    """
    import json as _json

    def _parse(text: str) -> Any:
        try:
            return extract_json_block(text, language)
        except Exception:
            # Fallback: try bare JSON (repair may return without fence)
            stripped = text.strip()
            if stripped.startswith("```"):
                stripped = stripped.removeprefix("```json").removeprefix("```").strip()
                if stripped.endswith("```"):
                    stripped = stripped[:-3].strip()
            if stripped.startswith("[") or stripped.startswith("{"):
                return _json.loads(stripped)
            raise

    try:
        return _parse(text)
    except Exception as exc:
        if llm and llm.get("base_url") and llm.get("model") and llm.get("api_key_env"):
            from ai_apitest.ai.provider import retry_with_json_repair
            try:
                result, _warning = retry_with_json_repair(
                    text,
                    parse_json=_parse,
                    base_url=llm["base_url"], model=llm["model"],
                    api_key_env=llm["api_key_env"], provider=llm.get("provider", "openai-compatible"),
                    description=f"（{language} 块内容）",
                )
                return result
            except ValueError:
                pass
        raise exc


def extract_json_block(text: str, language: str) -> Any:
    match = re.search(r"```" + re.escape(language) + r"\s*(.*?)```", text, re.DOTALL)
    if not match:
        raise ValueError(f"LLM 响应中没有找到 {language} 代码块")
    content = match.group(1).strip()
    if not content:
        return []
    return json.loads(content)


def strip_context_blocks(text: str) -> str:
    stripped = re.sub(r"```(?:iatest-lessons-json|iatest-known-issues-json)\s*.*?```", "", text, flags=re.DOTALL)
    return re.sub(r"\n{3,}", "\n\n", stripped).strip()
