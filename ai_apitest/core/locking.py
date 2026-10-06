from __future__ import annotations

import errno
import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt

from ai_apitest.core.paths import project_runtime_dir, safe_project_name


_LOCAL = threading.local()


def _lock(handle) -> None:
    handle.seek(0)
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)


def _unlock(handle) -> None:
    handle.seek(0)
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    else:
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


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
    if path.stat().st_size == 0:
        handle.write(" ")
        handle.flush()
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    while True:
        try:
            _lock(handle)
            break
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                handle.close()
                raise
            if time.monotonic() >= deadline:
                handle.close()
                raise TimeoutError(f"项目 {safe_project} 正在执行其它操作，等待 {timeout_seconds:g} 秒后仍未获得锁")
            from ai_apitest.execution.control import check_control
            try:
                check_control()
            except BaseException:
                handle.close()
                raise
            time.sleep(0.1)
    handle.seek(1)
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
            handle.seek(1)
            handle.truncate()
            handle.flush()
        finally:
            _unlock(handle)
            handle.close()
