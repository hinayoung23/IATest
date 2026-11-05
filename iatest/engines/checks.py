"""Finite data quality, HTTP protocol and targeted accessibility contracts."""

from __future__ import annotations
from iatest.core.env import environment_values

import json

from iatest.core.schema import evaluated_schema_errors
from iatest.engines.contracts import EngineResult
from iatest.engines.http import REQUEST_SCHEMA, HTTPExecutor, NetworkScope, validate
from iatest.engines.web import TARGET_SCHEMA, BrowserSession, chromium_path
from iatest.execution.assertions import MISSING, json_path_value

DATA_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["checks"],
    "oneOf": [{"required": ["records"]}, {"required": ["database"]}],
    "properties": {
        "records": {"type": "array", "maxItems": 10000},
        "checks": {
            "type": "array",
            "minItems": 1,
            "maxItems": 100,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "rule"],
                "properties": {
                    "id": {"type": "string", "minLength": 1, "maxLength": 80},
                    "rule": {
                        "enum": ["schema", "unique", "not_null", "references", "count"]
                    },
                    "path": {"type": "string", "minLength": 1, "maxLength": 300},
                    "schema": {"type": "object"},
                    "allowed": {"type": "array", "maxItems": 10000},
                    "min": {"type": "integer", "minimum": 0, "maximum": 10000},
                    "max": {"type": "integer", "minimum": 0, "maximum": 10000},
                },
            },
        },
    },
}
PROTOCOL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["allowed_origins", "checks"],
    "properties": {
        "allowed_origins": {
            "type": "array",
            "minItems": 1,
            "maxItems": 32,
            "items": {"type": "string"},
        },
        "checks": {
            "type": "array",
            "minItems": 1,
            "maxItems": 100,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "request"],
                "properties": {
                    "id": {"type": "string", "minLength": 1, "maxLength": 80},
                    "request": REQUEST_SCHEMA,
                    "http_version": {"enum": ["HTTP/1.0", "HTTP/1.1", "HTTP/2"]},
                    "headers": {
                        "type": "object",
                        "maxProperties": 30,
                        "additionalProperties": {"type": "string", "maxLength": 1000},
                    },
                    "json_schema": {"type": "object"},
                },
            },
        },
    },
}
ACCESSIBILITY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["allowed_origins", "start_url", "checks"],
    "properties": {
        "allowed_origins": PROTOCOL_SCHEMA["properties"]["allowed_origins"],
        "start_url": {"type": "string", "minLength": 1, "maxLength": 8192},
        "checks": {
            "type": "array",
            "minItems": 1,
            "maxItems": 100,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "rule"],
                "properties": {
                    "id": {"type": "string", "minLength": 1, "maxLength": 80},
                    "rule": {
                        "enum": ["accessible_name", "image_alt", "document_title"]
                    },
                    "target": TARGET_SCHEMA,
                    "expected": {"type": "string", "maxLength": 1000},
                },
            },
        },
    },
}
SCHEMAS = {
    "data": DATA_SCHEMA,
    "protocol": PROTOCOL_SCHEMA,
    "accessibility": ACCESSIBILITY_SCHEMA,
}

from iatest.engines.database import SCHEMA as DATABASE_SCHEMA

DATA_SCHEMA["properties"]["database"] = DATABASE_SCHEMA


def example_plan(kind):
    base = "http://127.0.0.1:8000"
    specs = {
        "data": {
            "records": [{"id": 1}, {"id": 2}],
            "checks": [{"id": "unique-id", "rule": "unique", "path": "$.id"}],
        },
        "protocol": {
            "allowed_origins": [base],
            "checks": [
                {
                    "id": "http-contract",
                    "request": {"url": base},
                    "http_version": "HTTP/1.1",
                }
            ],
        },
        "accessibility": {
            "allowed_origins": [base],
            "start_url": base,
            "checks": [{"id": "page-title", "rule": "document_title"}],
        },
    }
    return {"id": kind + "-example", "kind": kind, "spec": specs[kind]}


def validate_checks(plan):
    if plan.kind == "accessibility" and "audit" in plan.spec:
        from iatest.engines.accessibility import validate_audit

        return validate_audit(plan)
    if plan.kind == "protocol" and plan.spec.get("transport"):
        from iatest.engines.wire import validate_wire

        return validate_wire(plan)
    from jsonschema.validators import validator_for

    validate(plan.spec, SCHEMAS[plan.kind])
    checks = plan.spec["checks"]
    if len({c["id"] for c in checks}) != len(checks):
        raise ValueError("Check IDs must be unique")
    if len(json.dumps(plan.spec, ensure_ascii=False).encode()) > 4194304:
        raise ValueError("Check plan exceeds 4 MiB")
    for check in checks:
        for key in ("schema", "json_schema"):
            if key in check:
                validator_for(check[key]).check_schema(check[key])
        if plan.kind == "data":
            rule = check["rule"]
            if rule == "schema" and "schema" not in check:
                raise ValueError("Schema rule requires schema")
            if rule in {"unique", "not_null", "references"} and "path" not in check:
                raise ValueError("Data rule requires a JSONPath")
            if rule == "references" and "allowed" not in check:
                raise ValueError("References rule requires explicit allowed values")
            if rule == "count" and not ({"min", "max"} & set(check)):
                raise ValueError("Count requires min/max")
            if check.get("min", 0) > check.get("max", 10000):
                raise ValueError("Count min exceeds max")
        if (
            plan.kind == "accessibility"
            and check["rule"] != "document_title"
            and "target" not in check
        ):
            raise ValueError("Accessibility checks require a precise target")


def data_check(records, check, control):
    rule = check["rule"]
    failures, seen = [], set()
    allowed = {
        json.dumps(v, sort_keys=True, allow_nan=False) for v in check.get("allowed", [])
    }
    if rule == "count":
        return (
            []
            if check.get("min", 0) <= len(records) <= check.get("max", 10000)
            else [0]
        )
    for index, row in enumerate(records):
        control.check()
        if rule == "schema":
            bad = bool(evaluated_schema_errors(row, check["schema"]))
        else:
            value = json_path_value(row, check["path"], missing=MISSING)
            key = (
                json.dumps(value, sort_keys=True, allow_nan=False)
                if value is not MISSING
                else ""
            )
            bad = value is MISSING or value is None
            if rule == "unique":
                bad |= key in seen
                seen.add(key)
            elif rule == "references":
                bad |= key not in allowed
        if bad:
            failures.append(index)
    return failures


class ChecksEngine:
    def __init__(self, root, project, kind):
        self.root, self.project, self.kind = root, project, kind

    def capabilities(self):
        return {
            "kind": self.kind,
            "available": self.kind != "accessibility" or bool(chromium_path()),
            "transports": ["http", "websocket", "mqtt", "grpc"]
            if self.kind == "protocol"
            else [],
            "wcag": {
                "version": "2.2",
                "applicable_criteria": 86,
                "manual_review_required": True,
            }
            if self.kind == "accessibility"
            else None,
            "rules": SCHEMAS[self.kind]["properties"]["checks"]["items"]["properties"]
            .get("rule", {})
            .get("enum", ["http_contract"]),
            **({"database_drivers": ["sqlite", "postgresql", "mysql"]} if self.kind == "data" else {}),
            **({"grpc_dialogue": {"descriptor_required": True, "max_seconds": 600, "max_messages": 10000}} if self.kind == "protocol" else {}),
            "limitations": [
                "Finite declared checks only; database sources are read-only authorized subsets; no complete WCAG certification or full data platform scanning"
            ],
        }

    def execute(self, plan, context):
        if plan.kind == "accessibility" and "audit" in plan.spec:
            from iatest.engines.accessibility import AccessibilityAudit

            return AccessibilityAudit(self.root, self.project).execute(plan, context)
        if plan.kind == "protocol" and plan.spec.get("transport"):
            from iatest.engines.wire import WireEngine

            return WireEngine(self.root, self.project).execute(plan, context)
        validate_checks(plan)
        result = EngineResult(
            plan.id, plan.kind, limitations=self.capabilities()["limitations"]
        )
        browser = client = None
        try:
            records = plan.spec.get("records", [])
            if self.kind == "data" and "database" in plan.spec:
                import os

                from iatest.core.config import load_config
                from iatest.core.sandbox import SandboxPolicy
                from iatest.engines.database import read_records

                if plan.spec["database"]["driver"] == "sqlite":
                    SandboxPolicy.from_config(self.root, load_config(self.root), project=self.project).check_path(
                        environment_values().get(plan.spec["database"]["path_env"], ""))
                records = read_records(plan.spec["database"], context.control, root=self.root, project=self.project)
                result.metrics["database_driver"] = plan.spec["database"]["driver"]
            if self.kind == "accessibility":
                browser = BrowserSession(
                    self.root,
                    self.project,
                    plan.spec,
                    context.control,
                    max_queries=plan.budget.get("max_queries", 500),
                )
            elif self.kind == "protocol":
                client = HTTPExecutor(
                    NetworkScope(self.root, self.project, plan.spec["allowed_origins"]),
                    context.control,
                )
            for check in plan.spec["checks"]:
                context.control.check()
                row = {"id": check["id"], "verdict": "not_evaluated"}
                try:
                    if self.kind == "data":
                        failures = data_check(
                            records, check, context.control
                        )
                        row.update(
                            failure_count=len(failures),
                            failure_rows=failures[:20],
                            records=len(records),
                        )
                        if check["rule"] == "count":
                            row.pop("failure_rows", None)
                        passed = not failures
                    elif self.kind == "protocol":
                        response = client.request(check["request"])
                        errors = []
                        if not response["ok"]:
                            errors.append("status_or_incomplete_body")
                        if (
                            "http_version" in check
                            and response["http_version"] != check["http_version"]
                        ):
                            errors.append("http_version")
                        errors.extend(
                            "header:" + k
                            for k, expected in check.get("headers", {}).items()
                            if response["headers"].get(k.lower()) != expected
                        )
                        if "json_schema" in check and (
                            not response["json_valid"]
                            or evaluated_schema_errors(
                                response["json"], check["json_schema"]
                            )
                        ):
                            errors.append("json_schema")
                        row.update(
                            status_code=response["status_code"],
                            http_version=response["http_version"],
                            violations=errors,
                        )
                        passed = not errors
                    elif check["rule"] == "document_title":
                        browser.select_context({})
                        title = browser.evaluate("document.title")
                        passed = (
                            title == check["expected"]
                            if "expected" in check
                            else bool(title.strip())
                        )
                        row["matched"] = passed
                    else:
                        observation = browser.query(check["target"], cache=False)
                        if not observation["unique"]:
                            raise ValueError(
                                "Accessibility target missing, ambiguous or incomplete"
                            )
                        position = observation["candidates"][0]["index"]
                        if check["rule"] == "image_alt":
                            value = browser.evaluate(
                                f"(()=>{{const e=globalThis.__iatestNodes[{position}];if(e.tagName!=='IMG')throw Error('Expected image');return e.getAttribute('alt');}})()"
                            )
                            passed = value is not None and (
                                "expected" not in check or value == check["expected"]
                            )
                        else:
                            handle = browser.evaluate(
                                f"globalThis.__iatestNodes[{position}]",
                                object_result=True,
                            )
                            node = browser.call(
                                "DOM.describeNode", {"objectId": handle["objectId"]}
                            )["node"]
                            nodes = browser.call(
                                "Accessibility.getPartialAXTree",
                                {
                                    "backendNodeId": node["backendNodeId"],
                                    "fetchRelatives": False,
                                },
                            )["nodes"]
                            actual = next(
                                (
                                    n
                                    for n in nodes
                                    if n.get("backendDOMNodeId")
                                    == node["backendNodeId"]
                                ),
                                None,
                            )
                            if actual is None or actual.get("ignored"):
                                raise ValueError(
                                    "Accessible node is unavailable/ignored"
                                )
                            name = actual.get("name", {}).get("value", "")
                            passed = (
                                name == check["expected"]
                                if "expected" in check
                                else bool(name.strip())
                            )
                        row["matched"] = passed
                    row["verdict"] = "passed" if passed else "failed"
                except Exception as exc:  # noqa: BLE001 -- absent evidence cannot pass
                    row.update(
                        verdict="inconclusive",
                        error=type(exc).__name__ + ": " + str(exc),
                    )
                result.steps.append(row)
                if context.control.reason():
                    break
        finally:
            if browser:
                browser.close()
            if client:
                client.close()
        assessed = sum(r["verdict"] in {"passed", "failed"} for r in result.steps)
        result.metrics = {
            "checks": len(plan.spec["checks"]),
            "assessed": assessed,
            "not_assessed": len(plan.spec["checks"]) - assessed,
            "requests": context.control.request_count(),
            "model_calls": 0,
        }
        result.verdict = (
            "inconclusive"
            if assessed != len(plan.spec["checks"])
            else "failed"
            if any(r["verdict"] == "failed" for r in result.steps)
            else "passed"
        )
        if context.control.reason():
            result.lifecycle, result.error = "cancelled", context.control.reason()
        from iatest.engines.verdicts import assessment_metrics
        result.metrics.update(assessment_metrics(len(plan.spec["checks"]), assessed, sum(row["verdict"] == "failed" for row in result.steps)))
        return result
