from __future__ import annotations

from pathlib import Path

from ai_apitest.core.paths import project_docs_file
from ai_apitest.core.serde import endpoint_from_dict, read_json
from ai_apitest.generator.cases import load_cases_from_markdown
from ai_apitest.core.config import load_config
from ai_apitest.domain.contracts import build_coverage_manifest
from ai_apitest.report.storage import current_run_results


def coverage_summary(root: Path, project: str) -> dict:
    endpoints = [endpoint_from_dict(item) for item in read_json(project_docs_file(root, project), [])]
    cases = load_cases_from_markdown(root, project)
    endpoint_keys = {f"{item.method.upper()} {item.path}" for item in endpoints}
    covered = {f"{case.method.upper()} {case.path}" for case in cases if case.method.upper() != "CHAIN"}
    covered.update(f"{step.method.upper()} {step.path}" for case in cases for step in case.steps)
    manifest = build_coverage_manifest(endpoints, cases, config=load_config(root), latest_results=current_run_results(root, project, cases))
    design_methods = sorted({case.design_method for case in cases if case.design_method})
    priorities = {}
    for case in cases:
        priorities[case.priority] = priorities.get(case.priority, 0) + 1
    return {
        "project": project,
        "endpoint_total": len(endpoint_keys),
        "endpoint_covered": len(endpoint_keys.intersection(covered)),
        "endpoint_missing": sorted(endpoint_keys - covered),
        "case_total": len(cases),
        "design_methods": design_methods,
        "priorities": priorities,
        "rule_metrics": manifest["stats"],
    }
