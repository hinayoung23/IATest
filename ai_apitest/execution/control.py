from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
import threading
import signal
import math
import time
from typing import Any, Callable

from ai_apitest.core.sandbox import ACTIVE_POLICY, policy_scope


class RunCancelled(RuntimeError):
    pass


@dataclass(slots=True)
class RunControl:
    timeout_seconds: float | None = None
    cancelled: threading.Event = field(default_factory=threading.Event)
    cancellation_check: Callable[[], bool] | None = None
    started: float = field(default_factory=time.monotonic)

    def __post_init__(self):
        if self.timeout_seconds is not None and (not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0):
            raise ValueError("总时限必须是大于 0 的有限数值")

    def reason(self) -> str:
        if self.cancelled.is_set() or (self.cancellation_check is not None and self.cancellation_check()):
            self.cancelled.set()
            return "执行已取消"
        if self.timeout_seconds is not None and time.monotonic() - self.started >= self.timeout_seconds:
            return "执行超过总时限"
        return ""

    def check(self) -> None:
        if reason := self.reason():
            raise RunCancelled(reason)

    def remaining(self) -> float | None:
        self.check()
        return max(0.001, self.timeout_seconds - (time.monotonic() - self.started)) if self.timeout_seconds is not None else None

    def wait(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.check()
            self.cancelled.wait(min(0.1, deadline - time.monotonic()))
        self.check()


@dataclass(slots=True)
class ExecutionRuntime:
    control: RunControl
    auth: Any = None
    plugins: Any = None
    case: Any = None
    context: dict[str, Any] = field(default_factory=dict)
    policy: Any = None


ACTIVE_RUNTIME: ContextVar[ExecutionRuntime | None] = ContextVar("iatest_execution", default=None)


@contextmanager
def activate(runtime: ExecutionRuntime | None):
    token = ACTIVE_RUNTIME.set(runtime)
    try:
        with policy_scope(runtime.policy if runtime and runtime.policy is not None else ACTIVE_POLICY.get()):
            yield
    finally:
        ACTIVE_RUNTIME.reset(token)


@contextmanager
def cleanup_control(timeout: float = 10):
    runtime = ACTIVE_RUNTIME.get()
    with activate(replace(runtime, control=RunControl(timeout_seconds=timeout)) if runtime else None):
        yield


def check_control() -> None:
    if runtime := ACTIVE_RUNTIME.get():
        runtime.control.check()


@contextmanager
def handle_interrupt(control: RunControl):
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda signum, frame: control.cancelled.set())
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)
