"""Shared task budgets and explicit routing across configured model roles."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
import inspect
import math
import threading
import time

from ai_apitest.core.config import load_config


class TaskBudgetExceeded(RuntimeError):
    pass


@dataclass
class TaskBudget:
    max_tokens: int = 256000
    max_tool_calls: int = 500
    timeout_seconds: float = 1800
    tokens: int = 0
    tool_calls: int = 0
    started_at: float = field(default_factory=time.time)
    reserved: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self):
        if type(self.max_tokens) is not int or self.max_tokens < 1 or type(self.max_tool_calls) is not int or self.max_tool_calls < 1 or type(self.timeout_seconds) not in (int, float) or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("任务预算必须为有限正数")

    def check(self):
        if time.time() - self.started_at >= self.timeout_seconds:
            raise TaskBudgetExceeded("任务总时限耗尽")

    def remaining(self):
        self.check()
        return max(0.001, self.timeout_seconds - (time.time() - self.started_at))

    def reserve(self, input_tokens: int, output_tokens: int) -> int:
        with self.lock:
            self.check()
            allowed = min(output_tokens, self.max_tokens - self.tokens - self.reserved - input_tokens)
            if allowed < 128:
                raise TaskBudgetExceeded("任务 Token 预算不足以发起下一次模型调用")
            self.reserved += input_tokens + allowed
            return allowed

    def settle(self, reserved: int, actual: int):
        with self.lock:
            self.reserved -= reserved
            self.tokens += actual
            self.check()
            if self.tokens > self.max_tokens:
                raise TaskBudgetExceeded("任务累计 Token 超出预算")

    def tool(self):
        with self.lock:
            self.check()
            if self.tool_calls >= self.max_tool_calls:
                raise TaskBudgetExceeded("任务工具调用预算耗尽")
            self.tool_calls += 1

    def snapshot(self):
        with self.lock:
            return {"max_tokens": self.max_tokens, "max_tool_calls": self.max_tool_calls, "timeout_seconds": self.timeout_seconds, "tokens": self.tokens, "tool_calls": self.tool_calls, "started_at": self.started_at}

    def restore(self, saved):
        with self.lock:
            if any(saved.get(key) != getattr(self, key) for key in ("max_tokens", "max_tool_calls", "timeout_seconds")):
                raise TaskBudgetExceeded("恢复任务的预算配置发生变化")
            self.tokens = max(self.tokens, saved["tokens"])
            self.tool_calls = max(self.tool_calls, saved["tool_calls"])
            self.started_at = min(self.started_at, saved["started_at"])
            self.check()


ACTIVE_TASK_BUDGET: ContextVar[TaskBudget | None] = ContextVar("iatest_agent_budget", default=None)


@contextmanager
def task_budget(root, *, budget=None):
    if ACTIVE_TASK_BUDGET.get() is not None:
        yield ACTIVE_TASK_BUDGET.get()
        return
    settings = load_config(root).get("agents", {}).get("task_budget", {})
    value = budget or (TaskBudget(**{key: settings[key] for key in ("max_tokens", "max_tool_calls", "timeout_seconds") if key in settings}) if settings.get("enabled") else None)
    token = ACTIVE_TASK_BUDGET.set(value)
    try:
        yield value
    finally:
        ACTIVE_TASK_BUDGET.reset(token)


def bounded_task(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        bound = inspect.signature(function).bind(*args, **kwargs)
        with task_budget(bound.arguments["root"]):
            return function(*args, **kwargs)
    return wrapped


def route_model(config, role, current: dict, *, prompt_chars: int, endpoints: int):
    rules = config.get("agents", {}).get("model_routing", {}).get("rules", [])
    if role.id == "quality_reviewer":
        return dict(current), {"selected": role.model_role, "reason": "independent_quality_audit"}
    if not isinstance(rules, list) or len(rules) > 100:
        raise ValueError("模型路由规则必须为有界数组")
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise ValueError("模型路由规则必须是对象")
        for key in ("min_endpoints", "max_endpoints", "max_prompt_chars"):
            if key in rule and (type(rule[key]) is not int or rule[key] < 0):
                raise ValueError("模型路由阈值必须为非负整数")
        if rule.get("min_endpoints", 0) > rule.get("max_endpoints", 1000000):
            raise ValueError("模型路由阈值范围非法")
        if not isinstance(rule.get("role", "*"), str):
            raise ValueError("模型路由角色必须为字符串")
        if rule.get("role", "*") not in {"*", role.id}:
            continue
        if not rule.get("min_endpoints", 0) <= endpoints <= rule.get("max_endpoints", 1000000) or prompt_chars > rule.get("max_prompt_chars", 10000000):
            continue
        selected = rule.get("model_role")
        if selected not in {"master", "case_generator", "analyzer"}:
            raise ValueError("路由只能选择已定义的三个模型角色")
        value = config.get("llm", {}).get(selected, {})
        if not value.get("enabled") or not value.get("model"):
            raise ValueError("路由目标模型未启用或未配置")
        return dict(value), {"selected": selected, "rule_index": index, "reason": rule.get("reason", "explicit_rule"), "endpoints": endpoints, "prompt_chars": prompt_chars}
    return dict(current), {"selected": role.model_role, "reason": "configured_role"}
