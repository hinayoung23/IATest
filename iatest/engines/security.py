"""Explicit-scope, reproducible security checks, not a claim of full scanning."""

from __future__ import annotations

import copy
import hashlib
import json

from iatest.engines.contracts import EngineContext, EngineResult, ExecutionPlan
from iatest.engines.http import (
    REQUEST_SCHEMA,
    HTTPExecutor,
    NetworkScope,
    resolve_secrets,
    validate,
)
from iatest.engines.security_rules import RULES
from iatest.execution.assertions import MISSING, json_path_value
from iatest.execution.control import RunCancelled

SECURITY_SCHEMA = {
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
        "allow_mutation": {"type": "boolean"},
        "identities": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "additionalProperties": {
                    "oneOf": [
                        {"type": "string"},
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["$env"],
                            "properties": {"$env": {"type": "string", "minLength": 1}},
                        },
                    ]
                },
            },
        },
        "checks": {
            "type": "array",
            "minItems": 1,
            "maxItems": 100,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "rule", "request"],
                "properties": {
                    "id": {"type": "string", "minLength": 1, "maxLength": 80},
                    "rule": {
                        "enum": [
                            "authorization",
                            "contract_mutation",
                            "response_headers",
                            *RULES.rules,
                        ]
                    },
                    "request": REQUEST_SCHEMA,
                    "owner": {"type": "string"},
                    "challenger": {"type": "string"},
                    "marker": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["path", "expected"],
                        "properties": {"path": {"type": "string"}, "expected": {}},
                    },
                    "denied_status": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "integer", "minimum": 400, "maximum": 499},
                    },
                    "field": {"type": "string", "minLength": 1, "maxLength": 200},
                    "mutation": {
                        "enum": ["remove_required", "invalid_type", "extra_field"]
                    },
                    "replacement": {},
                    "options": {"type": "object", "maxProperties": 20},
                    "json_schema": {"type": "object"},
                    "required_headers": {
                        "type": "object",
                        "minProperties": 1,
                        "maxProperties": 20,
                        "additionalProperties": {"type": "string", "maxLength": 1000},
                    },
                },
            },
        },
    },
}


def validate_security(plan: ExecutionPlan):
    if "scan" in plan.spec:
        from iatest.engines.security_scan import validate_scan

        return validate_scan(plan)
    from jsonschema.validators import validator_for

    from iatest.core.schema import schema_errors

    schema = copy.deepcopy(SECURITY_SCHEMA)
    schema["properties"]["checks"]["items"]["properties"]["rule"]["enum"] = [
        "authorization",
        "contract_mutation",
        "response_headers",
        *RULES.rules,
    ]
    validate(plan.spec, schema)
    ids = set()
    for check in plan.spec["checks"]:
        if check["id"] in ids:
            raise ValueError("Duplicate security check ID")
        ids.add(check["id"])
        request = check["request"]
        if request.get("method", "GET") not in {
            "GET",
            "HEAD",
            "OPTIONS",
        } and not plan.spec.get("allow_mutation"):
            raise ValueError("Writing security requests require allow_mutation=true")
        if check["rule"] == "authorization":
            if (
                not all(k in check for k in ("owner", "challenger", "marker"))
                or check["owner"] == check["challenger"]
            ):
                raise ValueError(
                    "Authorization requires distinct identities and a resource marker"
                )
            if any(
                k not in plan.spec.get("identities", {})
                for k in (check["owner"], check["challenger"])
            ):
                raise ValueError("Authorization identity is not configured")
            if any(
                k.lower() in {"authorization", "cookie"}
                for k in request.get("headers", {})
            ):
                raise ValueError(
                    "Put authorization/cookies in the explicit identity matrix"
                )
        elif check["rule"] == "contract_mutation":
            if not all(
                k in check for k in ("json_schema", "field", "mutation")
            ) or not isinstance(request.get("json"), dict):
                raise ValueError(
                    "Contract mutation requires schema, object JSON, field and mutation"
                )
            schema = check["json_schema"]
            validator_for(schema).check_schema(schema)
            if schema_errors(request["json"], schema):
                raise ValueError(
                    "Contract mutation baseline does not satisfy its schema"
                )
            if not schema_errors(mutated_body(check), schema):
                raise ValueError("Mutation must actually violate the declared schema")
        elif check["rule"] in RULES.rules:
            RULES.validate(check)
        elif not check.get("required_headers"):
            raise ValueError("response_headers requires explicit required_headers")


def mutated_body(check):
    body = copy.deepcopy(check["request"]["json"])
    if check["mutation"] == "remove_required":
        body.pop(check["field"], None)
    elif check["mutation"] == "extra_field":
        body[check["field"]] = check.get("replacement", "iatest-unexpected")
    else:
        body[check["field"]] = check.get("replacement", {"iatest_invalid_type": True})
    return body


def identity_credentials(headers):
    normalized = {k.lower(): v for k, v in headers.items()}
    credentials = {
        k: v
        for k, v in normalized.items()
        if k in {"authorization", "cookie", "x-api-key", "api-key"}
    }
    return credentials or normalized


class SecurityEngine:
    def __init__(self, root, project):
        self.root, self.project = root, project

    def capabilities(self):
        return {
            "available": True,
            "kind": "security",
            "catalogues": {"immutable": True, "max_rules": 100, "max_chain_steps": 8},
            "crawler": {"max_pages": 100, "max_depth": 5, "javascript": False},
            "rules": [
                "authorization",
                "contract_mutation",
                "response_headers",
                *RULES.rules,
            ],
            "limitations": [
                "Only declared rules and identities are assessed; no full vulnerability coverage",
                "Contract acceptance is a validation discrepancy, not proof of an exploitable injection",
            ],
        }

    def execute(self, plan: ExecutionPlan, context: EngineContext) -> EngineResult:
        if "scan" in plan.spec:
            from iatest.engines.security_scan import SecurityScan

            return SecurityScan(self.root, self.project).execute(plan, context)
        validate_security(plan)
        result = EngineResult(
            plan.id, "security", limitations=self.capabilities()["limitations"]
        )
        scope = NetworkScope(self.root, self.project, plan.spec["allowed_origins"])
        for check in plan.spec["checks"]:
            scope.check(check["request"]["url"])
        identities = resolve_secrets(plan.spec.get("identities", {}))
        for check in plan.spec["checks"]:
            if check["rule"] == "authorization":
                owner, challenger = (
                    identities[check["owner"]],
                    identities[check["challenger"]],
                )
                if identity_credentials(owner) == identity_credentials(challenger):
                    raise ValueError("Identity credentials are equivalent")
        findings = {}
        for check in plan.spec["checks"]:
            row = {"id": check["id"], "rule": check["rule"], "verdict": "not_evaluated"}
            clients = []
            try:
                context.control.check()
                client = HTTPExecutor(scope, context.control)
                clients.append(client)
                baseline_headers = identities.get(check.get("owner"), {})
                baseline = client.request(check["request"], headers=baseline_headers)
                row["baseline_status"] = baseline["status_code"]
                if not baseline["ok"] or (
                    check["rule"] != "response_headers"
                    and not 200 <= baseline["status_code"] < 300
                ):
                    raise ValueError("Baseline not successful or response incomplete")
                finding = None
                if check["rule"] == "authorization":
                    owner, challenger = (
                        identities[check["owner"]],
                        identities[check["challenger"]],
                    )
                    marker = check["marker"]
                    if (
                        json_path_value(
                            baseline["json"], marker["path"], missing=MISSING
                        )
                        != marker["expected"]
                    ):
                        raise ValueError(
                            "Owner response does not prove the expected resource"
                        )
                    other = HTTPExecutor(scope, context.control)
                    clients.append(other)
                    challenged = other.request(check["request"], headers=challenger)
                    row["challenger_status"] = challenged["status_code"]
                    actual = json_path_value(
                        challenged["json"], marker["path"], missing=MISSING
                    )
                    if challenged["truncated"]:
                        raise ValueError("Challenger evidence incomplete")
                    if (
                        challenged["status_code"]
                        in check.get("denied_status", [401, 403, 404])
                        and actual != marker["expected"]
                    ):
                        row["verdict"] = "passed"
                    elif (
                        200 <= challenged["status_code"] < 300
                        and actual == marker["expected"]
                    ):
                        finding = {
                            "severity": "high",
                            "title": "Cross-identity access to the same protected resource",
                            "identities": [check["owner"], check["challenger"]],
                            "resource": marker,
                        }
                    else:
                        raise ValueError(
                            "Response neither proves denial nor unauthorized resource access"
                        )
                elif check["rule"] == "contract_mutation":
                    changed = {**check["request"], "json": mutated_body(check)}
                    response = client.request(changed)
                    row["mutation_status"] = response["status_code"]
                    if response["truncated"]:
                        raise ValueError("Mutation evidence incomplete")
                    if 200 <= response["status_code"] < 300:
                        finding = {
                            "severity": "medium",
                            "title": "Server accepted input violating the declared contract",
                            "mutation": check["mutation"],
                            "field": check["field"],
                        }
                    elif 400 <= response["status_code"] < 500:
                        row["verdict"] = "passed"
                    else:
                        raise ValueError(
                            "Server error or redirect does not prove contract validation"
                        )
                elif check["rule"] in RULES.rules:
                    finding = RULES.assess(check, baseline)
                    if finding is None:
                        row["verdict"] = "passed"
                else:
                    missing = []
                    for key, expected in check["required_headers"].items():
                        value = baseline["headers"].get(key.lower())
                        if value is None or (expected and expected != value.strip()):
                            missing.append(key)
                    if missing:
                        finding = {
                            "severity": "low",
                            "title": "Response violates the declared header policy",
                            "headers": missing,
                        }
                    else:
                        row["verdict"] = "passed"
                if finding:
                    key = hashlib.sha256(
                        json.dumps(
                            [check["rule"], check["request"], finding], sort_keys=True
                        ).encode()
                    ).hexdigest()[:24]
                    finding.update(
                        id=key,
                        check_id=check["id"],
                        reproduction={
                            "request": check["request"],
                            "rule": check["rule"],
                        },
                    )
                    findings[key] = finding
                    row.update(verdict="failed", finding_id=key)
            except RunCancelled as exc:
                result.lifecycle = (
                    "timed_out" if "总时限" in context.control.reason() else "cancelled"
                )
                result.error = str(exc)
                row.update(verdict="inconclusive", error=str(exc))
            except Exception as exc:  # noqa: BLE001 -- report execution failure while retaining cleanup
                row.update(verdict="inconclusive", error=str(exc))
            finally:
                for client in clients:
                    client.close()
            result.steps.append(row)
            if result.lifecycle != "completed":
                break
        assessed = len(
            [s for s in result.steps if s["verdict"] in {"passed", "failed"}]
        )
        result.metrics = {
            "requests": context.control.request_count(),
            "checks": len(plan.spec["checks"]),
            "assessed": assessed,
            "not_assessed": len(plan.spec["checks"]) - assessed,
            "findings": len(findings),
        }
        result.evidence = list(findings.values())
        result.verdict = (
            "failed"
            if findings
            else "inconclusive"
            if assessed != len(plan.spec["checks"])
            else "passed"
        )
        from iatest.engines.verdicts import assessment_metrics
        result.metrics.update(assessment_metrics(len(plan.spec["checks"]), assessed, sum(row["verdict"] == "failed" for row in result.steps)))
        return result
