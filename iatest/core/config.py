from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any

from iatest.core.env import load_dotenv, env_value
from iatest.core.paths import PROJECT_CONFIG, ensure_project_dirs
from iatest.core.serde import read_json, write_json


DEFAULT_CONFIG: dict[str, Any] = {
    "config_version": 1,
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
    "sandbox": {
        "mode": "policy",
        "read_paths": [],
        "denied_read_paths": [],
        "allowed_origins": [],
        "allowed_networks": [],
        "allow_plugins": False,
        "env_allowlist": [],
        "hard_timeout_seconds": 300,
        "termination_grace_seconds": 5,
        "max_output_bytes": 64000000,
        "max_input_bytes": 64000000,
        "max_cases": 10000,
        "container_image": "",
        "container_network": "none",
        "memory_mb": 512,
        "cpus": 1,
        "pids_limit": 64,
    },
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
        "max_requests": None,
        "sampling": {"prioritize_failures": True},
    },
    "fixtures": {
        "timeout_seconds": 10,
        "providers": {},
    },
    "mock": {
        "host": "127.0.0.1",
        "allow_lan": False,
        "management_token_env": "",
        "port": 8780,
        "max_request_bytes": 1000000,
        "max_history": 200,
        "routes": [],
    },
    "web": {
        "max_request_bytes": 20000000,
        "allowed_origins": [],
        "trusted_proxy_origins": [],
        "require_session": True,
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
        "task_budget": {"enabled": True, "max_tokens": 256000, "max_tool_calls": 500, "timeout_seconds": 1800},
        "model_routing": {"rules": []},
        "recipes": {"paths": []},
        "token_optimization": {
            "enabled": True,
            "evidence_dedup": True,
            "prefetch": True,
            "incremental_audit": True,
            "adaptive_batches": True,
            "prefetch_max_tokens": 18000,
            "batch_input_tokens": 18000,
            "audit_output_tokens": 6000,
        },
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
    from iatest.core.locking import workspace_config_lock
    with workspace_config_lock(root):
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
    if not isinstance(loaded, dict):
        raise ValueError("Configuration must be a JSON object")
    if loaded and "config_version" not in loaded:
        loaded = copy.deepcopy(loaded)
        # Existing projects retain their explicitly chosen cost policy.
        agents = loaded.setdefault("agents", {})
        if isinstance(agents, dict):
            budget = agents.setdefault("task_budget", {})
            if isinstance(budget, dict):
                budget.setdefault("enabled", False)
        if isinstance(loaded.get("llm"), dict) and "master" not in loaded["llm"] and loaded["llm"]:
            from iatest.ai.llm_config import normalize_llm_config
            loaded["llm"] = normalize_llm_config(loaded["llm"])
    config = deep_merge(config, loaded)
    from iatest.core.config_schema import validate_config
    validate_config(config, DEFAULT_CONFIG)
    return ConfigSnapshot(config, root)


def save_config(root: Path, config: dict[str, Any]) -> None:
    from iatest.core.config_schema import validate_config
    from iatest.core.locking import workspace_config_lock
    with workspace_config_lock(root):
        if isinstance(config, ConfigSnapshot) and config.root == root.resolve():
            current = dict(load_config(root))
            merged = _merge_changes(current, config.baseline, config, "$")
        else:
            merged = deep_merge(copy.deepcopy(DEFAULT_CONFIG), config)
        validate_config(merged, DEFAULT_CONFIG)
        write_json(root / PROJECT_CONFIG, merged)
        if isinstance(config, ConfigSnapshot):
            config.clear()
            config.update(merged)
            config.baseline = copy.deepcopy(merged)


class ConfigSnapshot(dict):
    """A mutable configuration with a baseline for conflict-safe saves."""

    def __init__(self, value: dict, root: Path):
        super().__init__(value)
        self.root = root.resolve()
        self.baseline = copy.deepcopy(value)


def _merge_changes(current: dict, before: dict, after: dict, path: str) -> dict:
    merged = copy.deepcopy(current)
    absent = object()
    for key in before.keys() | after.keys():
        old, new = before.get(key, absent), after.get(key, absent)
        if old == new:
            continue
        present = current.get(key, absent)
        if isinstance(old, dict) and isinstance(new, dict) and isinstance(present, dict):
            merged[key] = _merge_changes(present, old, new, path + "." + key)
        elif present != old and present != new:
            raise ValueError(f"Configuration changed concurrently at {path}.{key}; reload before saving")
        elif new is absent:
            merged.pop(key, None)
        else:
            merged[key] = copy.deepcopy(new)
    return merged


def update_config(root: Path, change) -> dict[str, Any]:
    from iatest.core.locking import workspace_config_lock
    with workspace_config_lock(root):
        config = load_config(root)
        change(config)
        save_config(root, config)
        return config


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
            resolved = env_value(name)
            if resolved is None or resolved == "":
                raise ValueError(f"环境变量未配置或为空：{name}")
            return resolved

        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, value)
    if isinstance(value, dict):
        return {key: resolve_env_vars(item) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_env_vars(item) for item in value]
    return value
