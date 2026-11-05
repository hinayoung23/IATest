"""Console form parsing and credential settings, independent of HTTP rendering."""
from __future__ import annotations

import re
from pathlib import Path

from iatest.ai.llm_config import normalize_llm_base_url, normalize_llm_provider
from iatest.ai.provider import looks_like_api_key
from iatest.core.files import atomic_write_text

def llm_role_from_form(root: Path, role: str, form: dict, existing: dict | None = None, *, enforce_trust: bool = False) -> dict:
    existing = existing or {}
    base_url = field(form, "base_url")
    model = field(form, "model")
    provider = normalize_llm_provider(field(form, "provider") or "openai-compatible")
    base_url = normalize_llm_base_url(provider, base_url)
    raw_key = field(form, "api_key_env") or existing.get("api_key_env", "")
    mode = field(form, "credential_mode")
    secret = mode == "secret" or (not mode and looks_like_api_key(raw_key))
    reference = managed_key_name(role) if secret else raw_key.strip()
    if enforce_trust and base_url and reference:
        from iatest.ai.connections import validate_connection
        validate_connection(root, base_url, reference)
    api_key_env = normalize_api_key_ref(root, role, raw_key, secret=secret, explicit=bool(mode))
    return {
        **existing,
        "enabled": bool(base_url and model and api_key_env),
        "provider": provider,
        "base_url": base_url,
        "model": model,
        "api_key_env": api_key_env,
        "supports_tools": field(form, "supports_tools") == "on",
        "supports_images": field(form, "supports_images") == "on",
    }


def bounded_form_int(value: str, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def bounded_form_float(value: str, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def managed_key_name(role: str) -> str:
    return {"master": "IATEST_MASTER_KEY", "case_generator": "IATEST_CASE_GENERATOR_KEY", "analyzer": "IATEST_ANALYZER_KEY"}.get(role, "IATEST_LLM_API_KEY")


def normalize_api_key_ref(root: Path, role: str, value: str, *, secret: bool = False, explicit: bool = False) -> str:
    stripped = value.strip()
    if not stripped:
        return ""
    if secret or (not explicit and looks_like_api_key(stripped)):
        if "\n" in stripped or "\r" in stripped:
            raise ValueError("API Key 不允许包含换行")
        env_name = managed_key_name(role)
        write_env_value(root / ".env", env_name, stripped)
        return env_name
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", stripped):
        raise ValueError("请输入合法环境变量名，或选择输入新凭据")
    return stripped


def write_env_value(path: Path, key: str, value: str) -> None:
    from iatest.core.locking import workspace_config_lock
    with workspace_config_lock(path.parent):
        _write_env_value(path, key, value)


def _write_env_value(path: Path, key: str, value: str) -> None:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or "\r" in value or "\n" in value or path.is_symlink():
        raise ValueError("Invalid secret file or environment assignment")
    lines = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    lines = [line for line in lines if not line.strip().startswith(f"{key}=")]
    lines.append(f"{key}={value}")
    atomic_write_text(path, "\n".join(lines) + "\n", private=True)


def display_api_key_ref(value: str) -> str:
    value = value.strip()
    if not value:
        return "-"
    return value if re.fullmatch(r"[A-Z_][A-Z0-9_]*", value) else mask_secret(value)


def mask_secret(value: str) -> str:
    if len(value) <= 10:
        return "******"
    return f"{value[:4]}{'*' * max(len(value) - 8, 6)}{value[-4:]}"


def api_key_placeholder(value: str) -> str:
    value = value.strip()
    if not value:
        return "未配置 API Key"
    return f"当前：{display_api_key_ref(value)}"


def field(form: dict, name: str) -> str:
    return (form.get(name) or [""])[0]

