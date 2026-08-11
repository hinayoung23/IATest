from __future__ import annotations

import logging
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("ai_apitest")


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
        "component": component,
        "operation": operation,
        "error_type": type(error).__name__,
        "error": str(error)[:1000],
        **(detail or {}),
    }
    LOGGER.warning(
        "%s.%s failed: %s: %s",
        component,
        operation,
        type(error).__name__,
        str(error),
    )
    LOGGER.debug("operational error traceback", exc_info=True)
    try:
        from ai_apitest.context.storage import audit, context_connection
        from ai_apitest.core.paths import safe_project_name

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
