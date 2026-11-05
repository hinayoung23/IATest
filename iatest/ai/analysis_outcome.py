"""AI explanation status is independent of deterministic execution verdicts."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class AnalysisOutcome:
    status: Literal["completed", "skipped", "degraded", "failed"]
    markdown: str = ""
    error_code: str = ""
    error: str = ""
    run_id: str = ""
    retryable: bool = False


class AnalysisMarkdown(str):
    """Compatible report text carrying an explicit, separately rendered status."""
    outcome: AnalysisOutcome

    def __new__(cls, outcome: AnalysisOutcome):
        value = super().__new__(cls, outcome.markdown)
        value.outcome = outcome
        return value
