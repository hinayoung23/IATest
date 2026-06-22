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
    headers: dict[str, str] = field(default_factory=dict)
    path_params: dict[str, Any] = field(default_factory=dict)
    query: dict[str, Any] = field(default_factory=dict)
    body: Any = None
    assertions: list[Assertion] = field(default_factory=list)
    extract: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class TestCase:
    id: str
    title: str
    method: str
    path: str
    priority: str = "P1"
    tags: list[str] = field(default_factory=list)
    design_method: str = "happy_path"
    headers: dict[str, str] = field(default_factory=dict)
    path_params: dict[str, Any] = field(default_factory=dict)
    query: dict[str, Any] = field(default_factory=dict)
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
