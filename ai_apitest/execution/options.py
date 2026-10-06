from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Iterable

from ai_apitest.core.models import TestCase


@dataclass(slots=True)
class RunOptions:
    environment: str = ""
    tags: set[str] = field(default_factory=set)
    priorities: set[str] = field(default_factory=set)
    shard_index: int = 1
    shard_count: int = 1
    seed: str | int | None = None
    timeout_seconds: float | None = None
    rerun_failed: bool = False
    retries: int = 0
    isolation: str = ""

    def __post_init__(self) -> None:
        if self.isolation and self.isolation not in {"off", "policy", "process", "container"}:
            raise ValueError("隔离模式仅支持 off/policy/process/container")
        if self.shard_count < 1 or not 1 <= self.shard_index <= self.shard_count:
            raise ValueError("分片必须满足 1 <= index <= count")
        if self.timeout_seconds is not None and (not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0):
            raise ValueError("总超时必须大于 0")
        if not 0 <= self.retries <= 10:
            raise ValueError("失败重试次数必须为 0 至 10")
        if self.priorities - {"P0", "P1", "P2", "P3"}:
            raise ValueError("优先级仅支持 P0/P1/P2/P3")

    def select(self, cases: Iterable[TestCase], ids: set[str] | None = None) -> list[TestCase]:
        cases = list(cases)
        known = {item for case in cases for item in (case.id, case.origin_case_id) if item}
        if ids is not None and ids - known:
            raise ValueError(f"指定的用例不存在：{', '.join(sorted(ids - known))}")
        return [
            case for case in cases
            if (ids is None or case.id in ids or case.origin_case_id in ids)
            and (not self.tags or self.tags.intersection(case.tags))
            and (not self.priorities or case.priority in self.priorities)
            and int(hashlib.sha256(case.id.encode()).hexdigest(), 16) % self.shard_count == self.shard_index - 1
        ]
