"""Cancellable local resource reservations shared across tasks and processes."""

from __future__ import annotations

import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar

from iatest.mobile.lease import DeviceLease

RESERVATIONS = ContextVar("iatest_resource_reservations", default=None)
_guard = threading.Lock()
_locks = {}


@contextmanager
def resource_scope(names, control, *, wait_seconds=0, reserve=False):
    """A reservation holds the kernel lock; its children serialize actual use."""
    started = time.monotonic()
    inherited = RESERVATIONS.get() or {}
    current = dict(inherited)
    with ExitStack() as stack:
        for name in sorted(set(names)):
            control.check()
            if name not in current:
                lease = DeviceLease("resource", name)
                while True:
                    try:
                        lease.acquire()
                        break
                    except RuntimeError:
                        if time.monotonic() - started >= wait_seconds:
                            raise TimeoutError(
                                f"Resource unavailable: {name}"
                            ) from None
                        control.wait(
                            min(
                                0.05,
                                max(0.001, wait_seconds - (time.monotonic() - started)),
                            )
                        )
                stack.callback(lease.close)
                current[name] = uuid.uuid4().hex
            if not reserve:
                key = (name, current[name])
                with _guard:
                    entry = _locks.setdefault(key, [threading.RLock(), 0])
                    entry[1] += 1

                def release_reference(key=key, entry=entry):
                    with _guard:
                        entry[1] -= 1
                        if entry[1] == 0:
                            _locks.pop(key, None)

                stack.callback(release_reference)
                while not entry[0].acquire(blocking=False):
                    if time.monotonic() - started >= wait_seconds:
                        raise TimeoutError(f"Resource busy in this task: {name}")
                    control.wait(0.02)
                stack.callback(entry[0].release)
        token = RESERVATIONS.set(current)
        stack.callback(RESERVATIONS.reset, token)
        yield {
            "wait_seconds": time.monotonic() - started,
            "resources": sorted(set(names)),
        }
