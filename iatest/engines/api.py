from __future__ import annotations

from pathlib import Path

from iatest.core.serde import to_jsonable
from iatest.engines.contracts import EngineContext, EngineResult, ExecutionPlan
from iatest.engines.http import REQUEST_SCHEMA, HTTPExecutor, NetworkScope, validate
from iatest.execution.assertions import MISSING, json_path_value
from iatest.execution.control import ExecutionRuntime, activate
from iatest.execution.runner import run_cases

INLINE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["allowed_origins", "requests"],
    "properties": {
        "allowed_origins": {
            "type": "array",
            "minItems": 1,
            "maxItems": 32,
            "items": {"type": "string"},
        },
        "requests": {
            "type": "array",
            "minItems": 1,
            "maxItems": 1000,
            "items": REQUEST_SCHEMA,
        },
        "exports": {
            "type": "object",
            "maxProperties": 50,
            "additionalProperties": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "type"],
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "type": {
                        "enum": [
                            "string",
                            "integer",
                            "number",
                            "boolean",
                            "object",
                            "array",
                        ]
                    },
                    "sensitive": {"type": "boolean"},
                },
            },
        },
    },
}


def validate_api(plan):
    if "requests" in plan.spec:
        validate(plan.spec, INLINE_SCHEMA)
        return
    unknown = set(plan.spec) - {"case_ids", "run_scope"}
    ids = plan.spec.get("case_ids")
    if (
        unknown
        or (
            ids is not None
            and (not isinstance(ids, list) or not all(isinstance(i, str) for i in ids))
        )
        or not isinstance(plan.spec.get("run_scope", "all"), str)
        or len(plan.spec.get("run_scope", "all")) > 80
    ):
        raise ValueError("API spec supports case_ids/run_scope or typed requests")


class APIEngine:
    def __init__(self, root: Path, project: str) -> None:
        self.root, self.project = root, project

    def capabilities(self) -> dict:
        return {
            "available": True,
            "kind": "api",
            "case_format": "existing-markdown",
            "actions": ["run_cases", "http_requests", "export_typed_json"],
            "limitations": [],
        }

    def execute(self, plan: ExecutionPlan, context: EngineContext) -> EngineResult:
        validate_api(plan)
        if "requests" in plan.spec:
            return self.execute_inline(plan, context)
        ids = plan.spec.get("case_ids")
        with activate(ExecutionRuntime(control=context.control)):
            results = run_cases(
                self.root,
                project=self.project,
                case_ids=set(ids) if ids is not None else None,
                run_scope=str(plan.spec.get("run_scope", "all")),
            )
        statuses = {r.status for r in results}
        verdict = (
            "failed"
            if statuses & {"failed", "error"}
            else "passed"
            if results and statuses == {"passed"}
            else "inconclusive"
        )
        return EngineResult(plan.id, "api", verdict=verdict, steps=to_jsonable(results))

    def execute_inline(self, plan, context):
        result = EngineResult(plan.id, "api")
        scope = NetworkScope(self.root, self.project, plan.spec["allowed_origins"])
        for request in plan.spec["requests"]:
            scope.check(request["url"])
        client = HTTPExecutor(scope, context.control)
        try:
            for index, request in enumerate(plan.spec["requests"]):
                response = client.request(request)
                result.steps.append(
                    {
                        "index": index,
                        "status_code": response["status_code"],
                        "passed": response["ok"],
                        "elapsed_ms": response["elapsed_ms"],
                    }
                )
                if not response["ok"]:
                    result.verdict = "failed"
                    break
            else:
                for name, definition in plan.spec.get("exports", {}).items():
                    value = json_path_value(
                        response["json"], definition["path"], missing=MISSING
                    )
                    if value is MISSING:
                        raise ValueError(f"Required output unavailable: {name}")
                    validate(value, {"type": definition["type"]})
                    result.outputs[name] = {
                        "value": value,
                        "type": definition["type"],
                        "sensitive": definition.get("sensitive", False),
                    }
                result.verdict = "passed"
        finally:
            client.close()
        result.metrics["requests"] = context.control.request_count()
        return result
