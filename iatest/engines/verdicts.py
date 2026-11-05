"""Explicit assessment counts; completing actions alone cannot pass a test."""
from __future__ import annotations

from enum import StrEnum


class Verdict(StrEnum):
    NOT_EVALUATED = "not_evaluated"
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"
    SKIPPED = "skipped"


def assessment_metrics(planned: int, assessed: int, failed: int) -> dict[str, int]:
    if any(type(value) is not int for value in (planned, assessed, failed)) or not 0 <= failed <= assessed <= planned:
        raise ValueError("Inconsistent assertion assessment counts")
    return {"checks_planned": planned, "checks_assessed": assessed, "checks_failed": failed}


def summarize_checks(result, planned: int, assessed: int, failed: int) -> None:
    result.metrics.update(assessment_metrics(planned, assessed, failed))
    if result.lifecycle != "completed":
        result.verdict = Verdict.INCONCLUSIVE
    elif result.verdict == Verdict.FAILED:
        # A known business failure is retained after fail-fast stops later checks.
        return
    elif not planned or assessed != planned:
        result.verdict = Verdict.INCONCLUSIVE
    else:
        result.verdict = Verdict.FAILED if failed else Verdict.PASSED


def ui_check(step: dict) -> bool:
    return step["op"] in {"expect", "wait"} or (step["op"] == "download" and ("expected_name" in step or "expected_sha256" in step))
