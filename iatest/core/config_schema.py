"""Configuration validation, including extensible named environments/providers."""
from __future__ import annotations

import json
from typing import Any

from iatest.core.schema import schema_errors


def _schema(value: Any) -> dict:
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer", "minimum": 0}
    if isinstance(value, float):
        return {"type": "number", "minimum": 0}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, list):
        return {"type": "array"}
    if isinstance(value, dict):
        return {"type": "object", "properties": {k: _schema(v) for k, v in value.items()}, "additionalProperties": not bool(value)}
    return {"type": ["integer", "null"], "minimum": 1}


def configuration_schema(defaults: dict) -> dict:
    schema = _schema(defaults)
    props = schema["properties"]
    props.update({key: {"type": "object"} for key in ("testing", "data", "datasets", "extensions", "resource_coordinator")})
    props["plugins"] = {"type": "array", "items": {"type": "string"}}
    props["environments"] = {"type": "object", "minProperties": 1, "additionalProperties": {"type": "object", "properties": {"base_url": {"type": "string"}, "headers": {"type": "object", "additionalProperties": {"type": "string"}}}}}
    execution = props["execution"]["properties"]
    execution.update({"namespace": {"type": "string"}, "fail_on_http_error": {"type": "boolean"}})
    for key in ("parallel", "timeout_seconds", "max_request_bytes", "max_response_bytes"):
        execution[key].update(exclusiveMinimum=0)
    props["sandbox"]["properties"]["mode"] = {"enum": ["off", "policy", "process", "container"]}
    for section in props.values():
        for key, value in section.get("properties", {}).items():
            if key.endswith("_seconds") and value.get("type") in {"integer", "number"}:
                value["type"] = "number"
    for key in ("hard_timeout_seconds", "termination_grace_seconds"):
        props["sandbox"]["properties"][key]["exclusiveMinimum"] = 0
    props["mock"]["properties"].update({"port": {"type": "integer", "minimum": 0, "maximum": 65535}, "allow_lan": {"type": "boolean"}, "management_token_env": {"type": "string"}})
    for key in ("allowed_origins", "trusted_proxy_origins"):
        props["web"]["properties"][key] = {"type": "array", "items": {"type": "string"}, "uniqueItems": True}
    props["web"]["properties"]["require_session"] = {"type": "boolean"}
    for role in props["llm"]["properties"].values():
        role["properties"].update({"supports_images": {"type": "boolean"}, "thinking": {"type": ["object", "null"]}, "tool_choice": {"type": ["string", "object", "null"]}, "max_tokens": {"type": "integer", "minimum": 1}, "temperature": {"type": ["number", "null"], "minimum": 0, "maximum": 2}, "timeout_seconds": {"type": "number", "exclusiveMinimum": 0}})
        for name in ("context_window", "default_output_tokens"):
            role["properties"][name]["minimum"] = 1
        # Provider extensions are preserved; security policy is checked separately.
        role["additionalProperties"] = True
    agents = props["agents"]["properties"]
    agents["planner_on_complete_contract"] = {"type": "boolean"}
    for value in agents["task_budget"]["properties"].values():
        if value.get("type") in {"integer", "number"}:
            value["minimum"] = 1
    for name in ("case_generation_mode", "api_discovery_mode", "report_analysis_mode", "governance_mode", "maintenance_mode"):
        agents[name] = {"enum": ["agent", "legacy"]}
    for section in ("sandbox", "context", "agents", "execution"):
        # Defaults describe the stable keys; documented legacy extension keys
        # are accepted explicitly instead of accepting arbitrary top-level typos.
        props[section]["additionalProperties"] = False
    return schema


def validate_config(config: dict, defaults: dict) -> None:
    try:
        json.dumps(config, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("Configuration must contain finite JSON values") from exc
    errors = schema_errors(config, configuration_schema(defaults))
    if errors:
        raise ValueError("Invalid configuration: " + "; ".join(errors))
    environments = config.get("environments", {})
    if config.get("default_environment", "local") not in environments:
        raise ValueError("$.default_environment must reference an existing environment")
    recovery = config.get("agents", {}).get("context_recovery", {})
    ratios = [recovery.get(k, defaults["agents"]["context_recovery"][k]) for k in ("target_ratio", "soft_ratio", "rebase_ratio", "circuit_breaker_ratio")]
    if not 0 < ratios[0] < ratios[1] < ratios[2] < ratios[3] < 1:
        raise ValueError("$.agents.context_recovery ratios must satisfy 0 < target < soft < rebase < circuit_breaker < 1")
