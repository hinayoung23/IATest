"""Bounded private model diagnostics; content logging requires an explicit flag."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from pathlib import Path

from iatest.core.redaction import redact, redact_text
from iatest.core.files import private_permissions

_LOCK = threading.Lock()
MAX_BYTES = 1_000_000


def enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def log_event(event: str, payload, *, path: str | None = None) -> None:
    try:
        _write_event(event, payload, path=path)
    except (OSError, TypeError, ValueError):
        # Optional diagnostics cannot interrupt a test or model request.
        return


def _write_event(event: str, payload, *, path: str | None = None) -> None:
    if not enabled("IATEST_DEBUG_LLM"):
        return
    directory = Path.home() / ".iatest" / "logs"
    target = Path(path) if path else directory / "llm.jsonl"
    with _LOCK:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.parent.is_symlink() or target.is_symlink():
            return
        if hasattr(os, "getuid") and target.parent.stat().st_uid != os.getuid():
            return
        private_permissions(target.parent)
        serialized = json.dumps(payload, ensure_ascii=False, default=str)
        entry = {"event": event, "bytes": len(serialized.encode()), "sha256": hashlib.sha256(serialized.encode()).hexdigest()}
        if enabled("IATEST_DEBUG_LLM_CONTENT"):
            entry["content"] = redact_text(json.dumps(redact(payload), ensure_ascii=False, default=str))[:16000]
        if target.exists() and target.stat().st_size > MAX_BYTES:
            rotated = target.with_suffix(".previous.jsonl")
            if rotated.is_symlink():
                return
            os.replace(target, rotated)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(target, flags, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                details = os.fstat(handle.fileno())
                if not stat.S_ISREG(details.st_mode) or (hasattr(os, "getuid") and details.st_uid != os.getuid()):
                    return
                if hasattr(os, "fchmod"):
                    os.fchmod(handle.fileno(), 0o600)
                else:
                    private_permissions(target)
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            # Optional diagnostics cannot interrupt a test or model request.
            return
