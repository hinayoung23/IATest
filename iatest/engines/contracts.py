from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from iatest.core.schema import schema_errors
from iatest.engines.data import DATA_PROPERTIES
from iatest.execution.control import RunControl

PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "kind", "spec"],
    "properties": {
        **DATA_PROPERTIES,
        "contract_version": {"const": 1},
        "id": {"type": "string", "pattern": "^[a-zA-Z0-9_.-]{1,80}$"},
        "title": {"type": "string", "maxLength": 300},
        "kind": {
            "enum": [
                "api",
                "web",
                "desktop",
                "performance",
                "client_performance",
                "remote",
                "security",
                "workflow",
                "android",
                "ios",
                "data",
                "protocol",
                "accessibility",
            ]
        },
        "spec": {"type": "object"},
        "preparation": {"type": "object"},
        "resources": {
            "type": "array",
            "maxItems": 100,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 200},
        },
        "budget": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "timeout_seconds": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "maximum": 3600,
                },
                "max_actions": {"type": "integer", "minimum": 1, "maximum": 1000},
                "max_queries": {"type": "integer", "minimum": 1, "maximum": 5000},
                "max_requests": {"type": "integer", "minimum": 1, "maximum": 100000},
                "max_evidence_bytes": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 67108864,
                },
            },
        },
    },
}


@dataclass(slots=True)
class ExecutionPlan:
    id: str
    kind: str
    spec: dict[str, Any]
    title: str = ""
    contract_version: int = 1
    budget: dict[str, Any] = field(default_factory=dict)
    data_generation: dict[str, Any] = field(default_factory=dict)
    input_env: dict[str, str] = field(default_factory=dict)
    data_seed: int | None = None
    preparation: dict[str, Any] = field(default_factory=dict)
    resources: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ExecutionPlan:
        try:
            json.dumps(value, allow_nan=False)
        except (ValueError, TypeError, RecursionError) as exc:
            raise ValueError("Plan must contain finite JSON values") from exc
        if errors := schema_errors(value, PLAN_SCHEMA):
            raise ValueError("; ".join(errors))
        return cls(**value)


@dataclass(slots=True)
class EngineResult:
    plan_id: str
    kind: str
    lifecycle: str = "completed"
    verdict: str = "not_evaluated"
    run_id: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    limitations: list[str] = field(default_factory=list)
    contract_version: int = 1
    outputs: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class EngineContext:
    control: RunControl
    started: float = field(default_factory=time.monotonic)


class Engine(Protocol):
    def capabilities(self) -> dict[str, Any]: ...
    def execute(self, plan: ExecutionPlan, context: EngineContext) -> EngineResult: ...


class EngineRegistry:
    def __init__(self) -> None:
        self._engines: dict[str, Engine] = {}

    def register(self, kind: str, engine: Engine) -> None:
        if kind in self._engines:
            raise ValueError(f"Engine already registered: {kind}")
        self._engines[kind] = engine

    def capabilities(self) -> dict[str, dict[str, Any]]:
        return {kind: engine.capabilities() for kind, engine in self._engines.items()}

    def execute(self, plan: ExecutionPlan, context: EngineContext) -> EngineResult:
        if plan.kind not in self._engines:
            raise ValueError(f"Unsupported engine: {plan.kind}")
        from iatest.engines.validation import validate_plan

        validate_plan(plan)
        context.control.check()
        parent = context.control
        child = PlanControl(
            parent,
            timeout_seconds=plan.budget.get("timeout_seconds", parent.remaining()),
            max_requests=plan.budget.get("max_requests", 10000),
        )
        from iatest.core.redaction import secret_scope
        from iatest.engines.data import materialize

        if plan.kind == "remote" or (
            plan.kind == "performance" and plan.spec.get("distribution")
        ):
            from dataclasses import asdict

            from iatest.engines.data import declared_secrets

            concrete, data, hidden = plan, {}, list(declared_secrets(asdict(plan)))
        else:
            concrete, data, hidden = materialize(plan)
        with secret_scope(hidden):
            from contextlib import ExitStack

            with ExitStack() as stack:
                engine = self._engines[plan.kind]
                if (
                    concrete.kind in {"android", "ios"}
                    and getattr(engine, "root", None) is not None
                ):
                    from iatest.mobile.store import MobileStore
                    from iatest.mobile.system import preflight

                    preflight(
                        MobileStore(engine.root, engine.project).get(
                            "profile", concrete.spec["profile"]
                        ),
                        concrete.spec,
                    )
                from iatest.nodes.coordinator import global_scope

                ticket = stack.enter_context(
                    global_scope(
                        getattr(engine, "root", None),
                        getattr(engine, "project", ""),
                        concrete,
                        child,
                    )
                )
                from iatest.mobile.emulator import plan_vm_scope

                vm = stack.enter_context(
                    plan_vm_scope(getattr(engine, "root", None), getattr(engine, "project", ""), concrete, child)
                )
                prepared = None
                if plan.preparation.get("app"):
                    from iatest.mobile.delivery import prepare_app
                    from iatest.mobile.lease import device_scope
                    from iatest.mobile.store import MobileStore

                    engine = self._engines[plan.kind]
                    try:
                        profile = MobileStore(engine.root, engine.project).get(
                            "profile", plan.preparation["app"]["profile"]
                        )
                        stack.enter_context(device_scope(profile))
                        prepared = prepare_app(
                            engine.root, engine.project, plan.preparation["app"], child
                        )
                        if prepared["state"] != "prepared":
                            raise RuntimeError(
                                prepared.get("error", "Application preparation failed")
                            )
                    except Exception as exc:  # noqa: BLE001 -- preparation is a distinct, non-business failure
                        return EngineResult(
                            plan.id,
                            plan.kind,
                            lifecycle="environment_error",
                            verdict="inconclusive",
                            error=str(exc),
                            metrics={"preparation": prepared, **({"os_sandbox": vm} if vm else {})},
                        )
                from iatest.execution.native_isolation import (
                    execute_native_isolated,
                )

                engine = self._engines[plan.kind]
                result = execute_native_isolated(engine, concrete, EngineContext(child))
                if result is None:
                    result = engine.execute(concrete, EngineContext(child))
                if prepared:
                    result.metrics["preparation"] = prepared
                if vm is not None:
                    result.metrics["os_sandbox"] = vm
                ticket.finish(result)
            if data and (plan.data_generation or plan.input_env):
                result.metrics["runtime_data"] = data
            return result


class PlanControl(RunControl):
    """Enforce each plan's request cap as well as the shared run cap."""

    def __init__(self, parent, **kwargs):
        super().__init__(cancellation_check=lambda: bool(parent.reason()), **kwargs)
        self.parent = parent

    def request(self):
        from contextlib import ExitStack

        from iatest.execution.control import RunCancelled

        chain, current = [], self
        while current is not None:
            if not current.request_parent:
                chain.append(current)
            current = getattr(current, "parent", None) or current.request_parent
        while True:
            self.check()
            with ExitStack() as locks:
                for item in chain:
                    locks.enter_context(item.request_lock)
                full = [
                    c
                    for c in chain
                    if c.max_requests is not None
                    and c.requests_started >= c.max_requests
                ]
                if not full:
                    for item in chain:
                        item.requests_started += 1
                    return
                exhausted = [c for c in full if not c.requests_reserved]
                if exhausted:
                    for item in exhausted:
                        item.request_exhausted = True
                    raise RunCancelled("请求预算耗尽")
            self.wait(0.02)

    def reason(self):
        return self.parent.reason() or super().reason()

    def remaining(self):
        own = super().remaining()
        parent = self.parent.remaining()
        return (
            min(x for x in (own, parent) if x is not None)
            if own is not None or parent is not None
            else None
        )
