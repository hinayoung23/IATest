"""Trusted Python registration, declarative rule options; no scripts from plans."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from iatest.core.schema import check_schema, evaluated_schema_errors
from iatest.engines.http import validate
from iatest.execution.assertions import MISSING, json_path_value


@dataclass(frozen=True)
class SecurityRule:
    name: str
    options_schema: dict
    assess: Callable
    validate_options: Callable = lambda _: None


class SecurityRuleRegistry:
    def __init__(self):
        self.rules = {}

    def register(self, rule):
        import re

        if not isinstance(rule, SecurityRule) or not re.fullmatch(
            r"[a-z][a-z0-9_]{1,59}", rule.name
        ):
            raise ValueError("Invalid trusted security rule")
        if rule.name in self.rules or rule.name in {
            "authorization",
            "contract_mutation",
            "response_headers",
        }:
            raise ValueError("Security rule is already registered")
        check_schema(rule.options_schema)
        self.rules[rule.name] = rule

    def validate(self, check):
        rule = self.rules[check["rule"]]
        validate(check.get("options", {}), rule.options_schema)
        rule.validate_options(check.get("options", {}))

    def assess(self, check, baseline):
        rule = self.rules[check["rule"]]
        if not baseline["json_valid"] or baseline["truncated"]:
            raise ValueError("Security rule requires complete JSON evidence")
        return rule.assess(check["options"], baseline["json"])


def schema_rule(options, response):
    if evaluated_schema_errors(response, options["schema"]):
        return {
            "severity": "low",
            "title": "Response violates the declared security response schema",
        }
    return None


def exposure_rule(options, response):
    paths = [
        p
        for p in options["forbidden_paths"]
        if json_path_value(response, p, missing=MISSING) is not MISSING
    ]
    return (
        {
            "severity": "high",
            "title": "Response contains fields forbidden by the declared disclosure policy",
            "paths": paths,
        }
        if paths
        else None
    )


RULES = SecurityRuleRegistry()
RULES.register(
    SecurityRule(
        "response_schema",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["schema"],
            "properties": {"schema": {"type": "object"}},
        },
        schema_rule,
        lambda options: check_schema(options["schema"]),
    )
)
RULES.register(
    SecurityRule(
        "sensitive_exposure",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["forbidden_paths"],
            "properties": {
                "forbidden_paths": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 50,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1, "maxLength": 300},
                }
            },
        },
        exposure_rule,
    )
)
