from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any

from ai_apitest.core.env import load_dotenv
from ai_apitest.core.paths import PROJECT_CONFIG, ensure_project_dirs
from ai_apitest.core.serde import read_json, write_json


DEFAULT_CONFIG: dict[str, Any] = {
    "artifacts": {
        "language": "en",
    },
    "environments": {
        "local": {
            "base_url": "http://127.0.0.1:8000",
            "headers": {},
        }
    },
    "default_environment": "local",
    "execution": {
        "parallel": 4,
        "timeout_seconds": 20,
        "fail_on_http_5xx": True,
        "fail_on_cleanup_error": True,
        "rate_limit_max_retries": 1,
        "rate_limit_fallback_wait_seconds": 1,
        "max_rate_limit_wait_seconds": 120,
        "max_request_bytes": 20000000,
        "max_response_bytes": 5000000,
    },
    "fixtures": {
        "timeout_seconds": 10,
        "providers": {},
    },
    "mock": {
        "host": "127.0.0.1",
        "port": 8780,
        "max_request_bytes": 1000000,
        "max_history": 200,
        "routes": [],
    },
    "web": {
        "max_request_bytes": 20000000,
    },
    "retention": {
        "result_runs": 200,
        "agent_runs": 500,
        "reports": 200,
    },
    "llm": {
        "master": {
            "enabled": False,
            "provider": "openai-compatible",
            "base_url": "",
            "model": "",
            "api_key_env": "",
            "context_window": 128000,
            "default_output_tokens": 8000,
            "supports_stream": True,
            "supports_tools": True,
            "supports_json_schema": False,
        },
        "case_generator": {
            "enabled": False,
            "provider": "openai-compatible",
            "base_url": "",
            "model": "",
            "api_key_env": "",
            "context_window": 128000,
            "default_output_tokens": 8000,
            "supports_stream": True,
            "supports_tools": True,
            "supports_json_schema": False,
        },
        "analyzer": {
            "enabled": False,
            "provider": "openai-compatible",
            "base_url": "",
            "model": "",
            "api_key_env": "",
            "context_window": 128000,
            "default_output_tokens": 8000,
            "supports_stream": True,
            "supports_tools": True,
            "supports_json_schema": False,
        },
    },
    "context": {
        "schema_version": 2,
        "auto_migrate": True,
        "backup_before_migrate": True,
        "injection": {
            "max_candidates": 2000,
            "budgets": {
                "api_discovery": {"max_context_ratio": 0.15, "max_context_tokens": 8000, "reserve_output_tokens": 4000},
                "case_generation": {"max_context_ratio": 0.25, "max_context_tokens": 12000, "reserve_output_tokens": 8000},
                "report_analysis": {"max_context_ratio": 0.30, "max_context_tokens": 16000, "reserve_output_tokens": 4000},
                "case_maintenance": {"max_context_ratio": 0.20, "max_context_tokens": 10000, "reserve_output_tokens": 6000},
            },
        },
        "retention": {
            "context_packs": {
                "max_records": 500,
                "max_age_days": 90,
                "remove_orphan_files": True,
                "repair_missing_files": True,
            },
            "audit_logs": {"max_records": 5000},
            "extraction_jobs": {"max_records": 1000},
            "execution_trends": {"max_records": 500},
            "failure_patterns": {"max_records": 2000},
        },
        "dependency_verification": {
            "allow_methods": ["GET", "POST"],
            "deny_methods": ["DELETE"],
            "allowed_paths": [],
            "blocked_paths": [],
            "requires_confirmation": True,
            "max_verification_attempts": 3,
        },
    },
    "case_generation": {
        "default_strategies": {
            "happy_path": True,
            "optional_field": True,
            "type_error": True,
            "boundary_value": True,
            "path_parameter_error": True,
            "dependency_chain": True,
            "auth_error": True,
            "equivalence_class": True,
            "status_code_coverage": True,
        },
        "custom_strategies": [],
    },
    "execution_strategies": {
        "custom_strategies": [],
    },
    "agents": {
        "case_generation_mode": "agent",
        "api_discovery_mode": "agent",
        "report_analysis_mode": "agent",
        "governance_mode": "agent",
        "maintenance_mode": "agent",
        "planner_batch_endpoints": 25,
        "designer_batch_endpoints": 8,
        "designer_batch_scenarios": 8,
        "semantic_repair_attempts": 2,
        "history_max_chars": 60000,
        "context_recovery": {
            "enabled": True,
            "soft_ratio": 0.55,
            "rebase_ratio": 0.70,
            "circuit_breaker_ratio": 0.80,
            "target_ratio": 0.35,
            "max_rebases": 3,
            "safety_margin_tokens": 2000,
            "evidence_preview_chars": 500,
            "max_state_chars": 16000,
        },
        "reuse_generation_cache": True,
    },
    "schedules": [],
    "governance": {
        "enabled": True,
        "inline_after_report": False,
        "review_reports": True,
        "review_cases": True,
        "review_experience_candidates": True,
        "max_candidates_per_batch": 10,
        "low_confidence_requires_manual": True,
    },
    "quality_audit": {
        "enabled": True,
        "threshold": 85,
        "reliability_min": 90,
        "completeness_min": 85,
        "max_score_spread": 15,
        "max_rounds": 3,
        "judge_max_attempts": 2,
        "require_all_models": True,
        "critical_veto": True,
        "parallel_judges": True,
        "weights": {
            "reliability": 0.40,
            "completeness": 0.30,
            "reasonableness": 0.20,
            "non_redundancy": 0.10,
        },
        "artifacts": {
            "endpoint_catalog": True,
            "test_cases": True,
            "test_report": True,
            "experiences": True,
        },
    },
}


def init_project(root: Path, project: str = "default") -> Path:
    ensure_project_dirs(root, project)
    path = root / PROJECT_CONFIG
    if not path.exists():
        write_json(path, DEFAULT_CONFIG)
    return path


def load_config(root: Path) -> dict[str, Any]:
    load_dotenv(root)
    # Callers legitimately mutate the returned nested configuration before
    # saving it. A shallow copy would leak those mutations into process-wide
    # defaults and contaminate other projects/tests.
    config = copy.deepcopy(DEFAULT_CONFIG)
    loaded = read_json(root / PROJECT_CONFIG, {})
    return deep_merge(config, loaded)


def save_config(root: Path, config: dict[str, Any]) -> None:
    write_json(root / PROJECT_CONFIG, config)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def current_environment(config: dict[str, Any]) -> dict[str, Any]:
    name = config.get("default_environment", "local")
    env = config.get("environments", {}).get(name)
    if not env:
        raise ValueError(f"配置中不存在环境: {name}")
    resolved = resolve_env_vars(env)
    return {**resolved, "name": name, "headers": resolved.get("headers", {})}


def resolve_env_vars(value: Any) -> Any:
    if isinstance(value, str):
        def replace(match) -> str:
            name = match.group(1)
            resolved = os.getenv(name)
            if resolved is None or resolved == "":
                raise ValueError(f"环境变量未配置或为空：{name}")
            return resolved

        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, value)
    if isinstance(value, dict):
        return {key: resolve_env_vars(item) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_env_vars(item) for item in value]
    return value
