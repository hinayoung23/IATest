from __future__ import annotations


def normalize_llm_config(config: dict) -> dict:
    if "master" not in config:
        legacy = {
            "enabled": config.get("enabled", False),
            "provider": normalize_llm_provider(config.get("provider", "openai-compatible")),
            "base_url": config.get("base_url", ""),
            "model": config.get("model", ""),
            "api_key_env": config.get("api_key_env", ""),
        }
        config = {"master": legacy, "case_generator": {}, "analyzer": {}}
    defaults = {
        "enabled": False,
        "provider": "openai-compatible",
        "base_url": "",
        "model": "",
        "api_key_env": "",
        "context_window": 128000,
        "default_output_tokens": 8000,
        "supports_stream": True,
    }
    return {
        "master": {**defaults, **config.get("master", {})},
        "case_generator": {**defaults, **config.get("case_generator", {})},
        "analyzer": {**defaults, **config.get("analyzer", {})},
    }


def resolve_llm_role(config: dict, role: str) -> dict:
    normalized = normalize_llm_config(config)
    if role == "master":
        return normalized["master"]
    selected = normalized.get(role)
    if selected and is_llm_configured(selected):
        return selected
    return normalized["master"]


def is_llm_configured(config: dict) -> bool:
    return bool(config.get("base_url") and config.get("model") and config.get("api_key_env"))


def normalize_llm_role(role: str) -> str:
    return role if role in {"master", "case_generator", "analyzer"} else "master"


def normalize_llm_provider(provider: str) -> str:
    return provider if provider in {"openai-compatible", "anthropic"} else "openai-compatible"


def normalize_llm_base_url(provider: str, base_url: str) -> str:
    stripped = base_url.rstrip("/")
    if provider != "anthropic" or not stripped:
        return stripped
    if stripped.endswith("/v1") or stripped.endswith("/messages"):
        return stripped
    return stripped + "/v1"
