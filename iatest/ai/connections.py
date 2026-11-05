"""Locally enrolled LLM origins and credential references.

The trust file is outside ordinary model settings and is never writable through
the console. Direct provider calls remain explicit caller-owned connections.
"""
from __future__ import annotations

import hmac
import ipaddress
import re
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import urlsplit

from iatest.core.locking import workspace_config_lock
from iatest.core.serde import read_json, write_json

ACTIVE_CONNECTION_ROOT: ContextVar[Path | None] = ContextVar("iatest_connection_root", default=None)


def endpoint_origin(value: str, *, allow_http: bool = False) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or "\\" in value:
        raise ValueError("LLM URL must be HTTP(S) without credentials, query or fragment")
    try:
        local = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        local = False
    if parsed.scheme == "http" and not local and not allow_http:
        raise ValueError("Remote HTTP LLM connections require explicit --allow-http enrollment")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    host = "[" + parsed.hostname + "]" if ":" in parsed.hostname else parsed.hostname
    return f"{parsed.scheme}://{host}:{port}"


def _path(root: Path) -> Path:
    path = root / ".iatest" / "llm-connections.json"
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("LLM trust file escaped workspace")
    return path


def enroll_connection(root: Path, base_url: str, credential: str, *, allow_http: bool = False) -> None:
    origin = endpoint_origin(base_url, allow_http=allow_http)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", credential):
        raise ValueError("Enroll an environment-variable reference, never a literal key")
    with workspace_config_lock(root):
        path = _path(root)
        connections = read_json(path, {"version": 1, "connections": []})
        item = {"origin": origin, "credential": credential}
        if item not in connections["connections"]:
            connections["connections"].append(item)
        write_json(path, connections)


def bootstrap_connections(root: Path) -> None:
    """Migrate local pre-console settings once; never enroll from an HTTP form."""
    from iatest.ai.llm_config import normalize_llm_config
    from iatest.core.config import load_config

    with workspace_config_lock(root):
        path = _path(root)
        if path.exists():
            return
        connections = []
        for role in normalize_llm_config(load_config(root).get("llm", {})).values():
            url, credential = role.get("base_url", ""), role.get("api_key_env", "")
            if url and credential:
                # Existing local config is administrator-owned. Legacy literal
                # keys must be re-entered as a managed credential in the UI.
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", credential):
                    item = {"origin": endpoint_origin(url, allow_http=True), "credential": credential}
                    if item not in connections:
                        connections.append(item)
        write_json(path, {"version": 1, "connections": connections})


def validate_connection(root: Path, base_url: str, credential: str) -> None:
    origin = endpoint_origin(base_url, allow_http=True)
    connections = read_json(_path(root), {"connections": []})["connections"]
    if not any(item.get("origin") == origin and hmac.compare_digest(str(item.get("credential", "")), credential) for item in connections):
        raise PermissionError("LLM target and credential are not enrolled; use 'iatest llm-trust' locally")


def check_active_connection(base_url: str, credential: str) -> None:
    root = ACTIVE_CONNECTION_ROOT.get()
    if root is not None:
        validate_connection(root, base_url, credential)


@contextmanager
def connection_scope(root: Path):
    token = ACTIVE_CONNECTION_ROOT.set(root)
    try:
        yield
    finally:
        ACTIVE_CONNECTION_ROOT.reset(token)
