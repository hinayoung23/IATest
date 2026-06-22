from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ai_apitest.core.env import load_dotenv
from ai_apitest.core.paths import PROJECT_CONFIG, ensure_project_dirs
from ai_apitest.core.serde import read_json, write_json


DEFAULT_CONFIG: dict[str, Any] = {
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
        },
    },
    "context": {
        "schema_version": 2,
        "auto_migrate": True,
        "backup_before_migrate": True,
        "injection": {
            "budgets": {
                "api_discovery": {"max_context_ratio": 0.15, "max_context_tokens": 8000, "reserve_output_tokens": 4000},
                "case_generation": {"max_context_ratio": 0.25, "max_context_tokens": 12000, "reserve_output_tokens": 8000},
                "report_analysis": {"max_context_ratio": 0.30, "max_context_tokens": 16000, "reserve_output_tokens": 4000},
                "case_maintenance": {"max_context_ratio": 0.20, "max_context_tokens": 10000, "reserve_output_tokens": 6000},
            },
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
    "schedules": [],
    "governance": {
        "enabled": True,
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
    config = dict(DEFAULT_CONFIG)
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
    headers = resolve_env_vars(env.get("headers", {}))
    return {**env, "name": name, "headers": headers}


def resolve_env_vars(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return os.getenv(value[2:-1], "")
    if isinstance(value, dict):
        return {key: resolve_env_vars(item) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_env_vars(item) for item in value]
    return value
