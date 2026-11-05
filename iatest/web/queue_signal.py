"""In-process wakeups with periodic polling retained for cross-process recovery."""
from __future__ import annotations

import threading
from pathlib import Path

_LOCK = threading.Lock()
_EVENTS: dict[str, threading.Event] = {}


def queue_event(root: Path) -> threading.Event:
    key = str(root.resolve())
    with _LOCK:
        return _EVENTS.setdefault(key, threading.Event())


def notify_queue(root: Path) -> None:
    queue_event(root).set()
