from __future__ import annotations

import os
import re
import threading
from contextvars import ContextVar
from pathlib import Path


_ENV_LOCK = threading.RLock()
_LOADED: dict[str, tuple[str, str]] = {}
_ACTIVE_ROOT: ContextVar[Path | None] = ContextVar("iatest_environment_root", default=None)
_UNSAFE = {"PATH", "HOME", "USERPROFILE", "TMP", "TEMP", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_OPTIONS", "BASH_ENV", "ENV"}


def read_dotenv(root: Path) -> dict[str, str]:
    """Parse project values without changing process execution settings."""
    env_path = root / ".env"
    if not env_path.exists():
        return {}
    values = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError("Invalid environment variable name in .env")
        if key in _UNSAFE or key.startswith(("PYTHON", "DYLD_", "LD_", "IATEST_DEBUG_")):
            continue
        values[key] = unquote_env_value(value.strip())
    return values


def load_dotenv(root: Path) -> None:
    _ACTIVE_ROOT.set(root)
    owner = str(root.resolve())
    values = read_dotenv(root)
    with _ENV_LOCK:
        for key, (loaded_owner, loaded_value) in list(_LOADED.items()):
            if loaded_owner == owner and key not in values:
                if os.environ.get(key) == loaded_value:
                    os.environ.pop(key, None)
                _LOADED.pop(key, None)
        for key, value in values.items():
            previous = _LOADED.get(key)
            if key not in os.environ or (previous == (owner, os.environ.get(key))):
                os.environ[key] = value
                _LOADED[key] = owner, value


def environment_values(root: Path | None = None) -> dict[str, str]:
    """Project values overlay only auto-loaded values, never external settings."""
    root = root or _ACTIVE_ROOT.get()
    if root is None:
        return dict(os.environ)
    values = read_dotenv(root) if root is not None else {}
    owner = str(root.resolve()) if root is not None else None
    with _ENV_LOCK:
        process = dict(os.environ)
        imported = {key for key, (_source, value) in _LOADED.items() if process.get(key) == value}
        result = {key: value for key, value in process.items() if key not in imported or _LOADED[key][0] == owner}
        for key, value in values.items():
            if key not in process or key in imported:
                result[key] = value
        return result


def env_value(name: str, default=None, *, root: Path | None = None):
    return environment_values(root).get(name, default)


def unquote_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
