from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ai_apitest.ai.workflows import build_ai_failure_review, build_ai_report_analysis
from ai_apitest.core.config import load_config
from ai_apitest.execution.runner import run_cases
from ai_apitest.generator.cases import load_cases_from_markdown
from ai_apitest.report.markdown import write_report


def run_schedule_forever(root: Path, project: str, run_once: bool = False, console: Any = None) -> None:
    config = load_config(root)
    schedules = config.get("schedules", [])
    if not schedules:
        emit(console, "未配置 schedules。")
        return
    scheduler = BlockingScheduler()
    for item in schedules:
        if not item.get("enabled", True):
            continue
        scheduler.add_job(
            lambda schedule=item: execute_scheduled_run(root, project, schedule, console),
            trigger=build_trigger(item),
            id=str(item.get("name") or item.get("id") or f"schedule-{len(scheduler.get_jobs()) + 1}"),
            replace_existing=True,
        )
    if run_once:
        for item in schedules:
            if item.get("enabled", True):
                execute_scheduled_run(root, project, item, console)
        return
    emit(console, f"已启动定时任务 {len(scheduler.get_jobs())} 个。")
    scheduler.start()


def execute_scheduled_run(root: Path, project: str, schedule: dict, console: Any = None) -> Path:
    case_ids = resolve_schedule_case_ids(root, project, schedule)
    results = run_cases(root, project=project, case_ids=case_ids, run_scope=str(schedule.get("scope", "full")))
    report = write_report(
        root,
        results,
        project=project,
        ai_review=build_ai_failure_review(root, project, results),
        analysis=build_ai_report_analysis(root, project, results),
    )
    emit(console, f"定时任务执行完成：{report}")
    return report


def resolve_schedule_case_ids(root: Path, project: str, schedule: dict) -> set[str] | None:
    scope = str(schedule.get("scope", "full"))
    if scope == "full":
        return None
    cases = load_cases_from_markdown(root, project)
    if scope == "smoke":
        return {case.id for case in cases if case.runnable and ("happy_path" in case.tags or "dependency_chain" in case.tags)}
    if scope == "specific":
        ids = schedule.get("case_ids", [])
        return {str(item) for item in ids}
    if scope == "custom":
        tags = {str(item) for item in schedule.get("tags", [])}
        priority = {str(item) for item in schedule.get("priority", [])}
        return {
            case.id
            for case in cases
            if (not tags or tags.intersection(case.tags)) and (not priority or case.priority in priority)
        }
    return None


def build_trigger(schedule: dict):
    trigger = str(schedule.get("trigger", "cron"))
    if trigger == "interval":
        values = {
            key: int(schedule.get(key, 0))
            for key in ("seconds", "minutes", "hours")
            if int(schedule.get(key, 0))
        }
        return IntervalTrigger(**(values or {"hours": 1}))
    if trigger == "date":
        return DateTrigger(run_date=datetime.fromisoformat(str(schedule["run_at"])))
    cron = schedule.get("cron") or natural_language_to_cron(str(schedule.get("natural", ""))) or "0 0 * * *"
    return CronTrigger.from_crontab(cron)


def natural_language_to_cron(text: str) -> str:
    normalized = text.strip().lower()
    if not normalized:
        return ""
    if "每小时" in normalized or "hourly" in normalized:
        return "0 * * * *"
    if "每天" in normalized or "daily" in normalized:
        return "0 9 * * *"
    if "每周" in normalized or "weekly" in normalized:
        return "0 9 * * 1"
    if "每月" in normalized or "monthly" in normalized:
        return "0 9 1 * *"
    return ""


def emit(console: Any, message: str) -> None:
    if console:
        console.print(message)
    else:
        print(message)
