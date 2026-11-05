"""Transport-independent bounded polling shared by native browser backends."""
from __future__ import annotations

import time
from typing import Callable, TypeVar

T = TypeVar("T")


def wait_for_devtools_endpoint(path, process, control, timeout: float = 15) -> tuple[int, str]:
    """The port file may exist before Chromium has written both lines."""
    deadline = time.monotonic() + timeout
    while True:
        control.check()
        if process.poll() is not None or time.monotonic() >= deadline:
            raise RuntimeError("Chromium startup failed or timed out")
        try:
            lines = path.read_text().splitlines()
        except FileNotFoundError:
            lines = []
        if len(lines) >= 2:
            try:
                port = int(lines[0])
            except ValueError:
                port = 0
            if 0 < port < 65536 and lines[1].startswith("/devtools/browser/"):
                return port, lines[1]
        control.wait(0.05)


def wait_for_context(control, attempt: Callable[[], T], transient: type[Exception], message: str, timeout: float = 5) -> T:
    remaining = control.remaining()
    deadline = time.monotonic() + min(timeout, remaining if remaining is not None else timeout)
    while True:
        control.check()
        try:
            return attempt()
        except transient:
            left = deadline - time.monotonic()
            if left <= 0:
                raise ValueError(message) from None
            control.wait(min(0.05, left))
