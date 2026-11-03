from __future__ import annotations

import hashlib
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from iatest.core.locking import _lock, _unlock

HELD_DEVICES = ContextVar("iatest_delivery_devices", default=frozenset())


@contextmanager
def device_scope(profile):
    key = (profile["platform"], profile["device_id"])
    if key in HELD_DEVICES.get():
        yield
        return
    lease = DeviceLease(*key)
    lease.acquire()
    token = HELD_DEVICES.set(HELD_DEVICES.get() | {key})
    try:
        yield
    finally:
        HELD_DEVICES.reset(token)
        lease.close()


class DeviceLease:
    """Kernel-owned lock shared across projects/processes of the same local user."""

    def __init__(self, platform: str, device_id: str):
        self.key = (platform, device_id)
        digest = hashlib.sha256(f"{platform}:{device_id}".encode()).hexdigest()
        self.path = (
            Path(tempfile.gettempdir()) / "iatest-device-leases" / (digest + ".lock")
        )
        self.handle = None

    def acquire(self) -> None:
        if self.key in HELD_DEVICES.get():
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            handle.write(b" ")
            handle.flush()
        try:
            _lock(handle)
        except OSError as exc:
            handle.close()
            raise RuntimeError(
                "Device is already leased by another test or manual session"
            ) from exc
        self.handle = handle

    def close(self) -> None:
        if self.handle:
            _unlock(self.handle)
            self.handle.close()
            self.handle = None
