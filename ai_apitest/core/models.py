from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Endpoint:
    method: str
    path: str
    summary: str = ""
    description: str = ""
    parameters: list[dict[str, Any]] = field(default_factory=list)
    request_body: dict[str, Any] | None = None
    responses: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    operation_id: str = ""
    deprecated: bool = False
    security: list[dict[str, Any]] = field(default_factory=list)
    business_rules: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class Assertion:
    type: str
    target: str = ""
    operator: str = "eq"
    expected: Any = None


@dataclass(slots=True)
class TestStep:
    name: str
    method: str
    path: str
    headers: dict[str, Any] = field(default_factory=dict)
    path_params: dict[str, Any] = field(default_factory=dict)
    query: dict[str, Any] = field(default_factory=dict)
    parameter_serialization: dict[str, dict[str, Any]] = field(default_factory=dict)
    body: Any = None
    assertions: list[Assertion] = field(default_factory=list)
    extract: dict[str, str] = field(default_factory=dict)
    poll: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TestCase:
    id: str
    title: str
    method: str
    path: str
    priority: str = "P1"
    tags: list[str] = field(default_factory=list)
    design_method: str = "happy_path"
    headers: dict[str, Any] = field(default_factory=dict)
    path_params: dict[str, Any] = field(default_factory=dict)
    query: dict[str, Any] = field(default_factory=dict)
    parameter_serialization: dict[str, dict[str, Any]] = field(default_factory=dict)
    body: Any = None
    assertions: list[Assertion] = field(default_factory=list)
    source: str = ""
    runnable: bool = True
    risk: str = "none"
    negative: bool = False
    preconditions: list[str] = field(default_factory=list)
    test_steps: list[str] = field(default_factory=list)
    expected_result: str = ""
    setup_steps: list[TestStep] = field(default_factory=list)
    steps: list[TestStep] = field(default_factory=list)
    cleanup_steps: list[TestStep] = field(default_factory=list)
    scenario_id: str = ""
    rule_ids: list[str] = field(default_factory=list)
    coverage_partition_ids: list[str] = field(default_factory=list)
    fixture_requirements: list[str] = field(default_factory=list)
    variables: dict[str, Any] = field(default_factory=dict)
    dataset: Any = None
    dataset_id: str = ""
    origin_case_id: str = ""
    identity: str = ""
    resources: list[str] = field(default_factory=list)
    parallel_safe: bool = False
    retry_safe: bool = False
    poll: dict[str, Any] = field(default_factory=dict)
    data_generation: dict[str, Any] = field(default_factory=dict)
    samples: int = 1
    sample_index: int = 0
    sample_origin_id: str = ""
    sample_plan: dict[str, Any] = field(default_factory=dict)


def execution_case_options(case: TestCase) -> dict[str, Any]:
    return {name: getattr(case, name) for name in ("variables", "dataset", "identity", "resources", "parallel_safe", "retry_safe", "poll", "data_generation", "samples")}


@dataclass(slots=True)
class CaseResult:
    case_id: str
    title: str
    status: str
    method: str
    path: str
    status_code: int | None
    elapsed_ms: int
    error: str = ""
    assertion_errors: list[str] = field(default_factory=list)
    request: dict[str, Any] = field(default_factory=dict)
    response: dict[str, Any] = field(default_factory=dict)
    preconditions: list[str] = field(default_factory=list)
    test_steps: list[str] = field(default_factory=list)
    expected_result: str = ""
    case_risk: str = "none"
    design_method: str = ""
    setup: dict[str, Any] = field(default_factory=dict)
    cleanup: dict[str, Any] = field(default_factory=dict)
    run_id: int | None = None
    origin_case_id: str = ""
    dataset_id: str = ""
    flaky: bool = False
    attempt_history: list[dict[str, Any]] = field(default_factory=list)
    generated_data: dict[str, Any] = field(default_factory=dict)
    generation_meta: dict[str, Any] = field(default_factory=dict)
    execution_meta: dict[str, Any] = field(default_factory=dict)
