"""Transport-independent bounded polling shared by native browser backends."""
from __future__ import annotations

import time
from typing import Callable, TypeVar

T = TypeVar("T")


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
