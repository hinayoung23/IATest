"""Domain contracts and deterministic compilers."""

from iatest.domain.case_drafts import (
    CompilationResult,
    DraftIssue,
    ScenarioDraft,
    compile_scenario_drafts,
    parse_scenario_drafts,
    validate_compiled_cases,
)
from iatest.domain.coverage import fill_deterministic_coverage_gaps, normalize_coverage_plan

__all__ = [
    "CompilationResult",
    "DraftIssue",
    "ScenarioDraft",
    "compile_scenario_drafts",
    "parse_scenario_drafts",
    "validate_compiled_cases",
    "fill_deterministic_coverage_gaps",
    "normalize_coverage_plan",
]
