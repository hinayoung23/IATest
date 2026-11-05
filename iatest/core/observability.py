from __future__ import annotations

import logging
import json
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("iatest")
_TRACE: ContextVar[dict[str, str]] = ContextVar("iatest_trace", default={})


def trace_context() -> dict[str, str]:
    return dict(_TRACE.get())


def log_event(event: str, *, level: int = logging.INFO, **fields) -> None:
    from iatest.core.redaction import redact, redact_text
    payload = {"event": event, **trace_context(), **fields}
    LOGGER.log(level, redact_text(json.dumps(redact(payload), ensure_ascii=False, default=str)))


@contextmanager
def trace_scope(operation: str, *, inherited: dict | None = None, **identifiers):
    parent = trace_context() or dict(inherited or {})
    current = {**parent, "trace_id": parent.get("trace_id") or uuid.uuid4().hex, "parent_span_id": parent.get("span_id", ""), "span_id": uuid.uuid4().hex[:16], **identifiers}
    token = _TRACE.set(current)
    started, status = time.monotonic(), "completed"
    try:
        yield current
    except BaseException:
        status = "failed"
        raise
    finally:
        log_event(operation, status=status, elapsed_ms=round((time.monotonic() - started) * 1000, 3))
        _TRACE.reset(token)


def traced(operation: str):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with trace_scope(operation):
                return function(*args, **kwargs)
        return wrapped
    return decorate


def record_operational_error(
    root: Path,
    project: str,
    *,
    component: str,
    operation: str,
    error: BaseException,
    detail: dict[str, Any] | None = None,
) -> None:
    payload = {
        **trace_context(),
        "component": component,
        "operation": operation,
        "error_type": type(error).__name__,
        "error": str(error)[:1000],
        **(detail or {}),
    }
    from iatest.core.redaction import redact, redact_text
    payload = redact(payload)
    payload["error"] = redact_text(payload["error"])
    log_event("operational_error", level=logging.WARNING, **payload)
    try:
        from iatest.context.storage import audit, context_connection
        from iatest.core.paths import safe_project_name

        safe_project = safe_project_name(project)
        with context_connection(root, safe_project) as conn:
            audit(
                conn,
                "operational_error",
                component,
                operation,
                safe_project,
                "system",
                payload,
            )
    except Exception:
        LOGGER.debug("failed to persist operational error", exc_info=True)
