from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ai_apitest.core.paths import project_runtime_dir, safe_project_name


_LOCAL = threading.local()


@contextmanager
def project_operation_lock(
    root: Path,
    project: str,
    *,
    operation: str,
    timeout_seconds: float = 300,
) -> Iterator[None]:
    """Serialize stateful operations for one project across threads and processes."""
    safe_project = safe_project_name(project)
    runtime_dir = project_runtime_dir(root, safe_project)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    path = runtime_dir / "operation.lock"
    key = str(path.resolve())
    held = getattr(_LOCAL, "held", {})
    if key in held:
        held[key]["depth"] += 1
        try:
            yield
        finally:
            held[key]["depth"] -= 1
        return

    handle = path.open("a+", encoding="utf-8")
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                handle.close()
                raise TimeoutError(f"项目 {safe_project} 正在执行其它操作，等待 {timeout_seconds:g} 秒后仍未获得锁")
            time.sleep(0.1)
    handle.seek(0)
    handle.truncate()
    handle.write(
        json.dumps(
            {"pid": os.getpid(), "thread": threading.current_thread().name, "operation": operation, "acquired_at": time.time()},
            ensure_ascii=False,
        )
    )
    handle.flush()
    held[key] = {"handle": handle, "depth": 1}
    _LOCAL.held = held
    try:
        yield
    finally:
        held.pop(key, None)
        try:
            handle.seek(0)
            handle.truncate()
            handle.flush()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
