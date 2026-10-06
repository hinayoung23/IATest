from __future__ import annotations

import copy
import csv
import hashlib
import itertools
import io
import json
import re
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from ai_apitest.core.models import TestCase
from ai_apitest.core.sandbox import ACTIVE_POLICY, SandboxPolicy, policy_scope, read_file, positive


MAX_ROWS = 10000
MAX_DATASET_BYTES = 20_000_000


def dataset_rows(spec: Any, root: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(spec, str):
        configured = config.get("datasets", {}).get(spec)
        spec = configured if configured is not None else {"source": spec}
    if isinstance(spec, list):
        rows = spec
    elif isinstance(spec, dict):
        rows = spec.get("rows", [])
        if "source" in spec:
            path = (root / str(spec["source"])).resolve()
            with policy_scope(ACTIVE_POLICY.get() or SandboxPolicy.from_config(root, config)):
                content = read_file(path, MAX_DATASET_BYTES).decode("utf-8-sig")
            with io.StringIO(content, newline="") as file:
                if path.suffix.lower() == ".csv":
                    rows = list(itertools.islice(csv.DictReader(file), MAX_ROWS + 1))
                elif path.suffix.lower() == ".json":
                    rows = json.load(file)
                else:
                    raise ValueError("数据集仅支持 JSON 或 CSV 文件")
        if "matrix" in spec:
            if rows:
                raise ValueError("数据集 rows/source 与 matrix 不能同时使用")
            matrix = spec["matrix"]
            if not isinstance(matrix, dict) or not matrix or any(not isinstance(v, list) or not v for v in matrix.values()):
                raise ValueError("matrix 必须是非空字段到非空数组的映射")
            names = list(matrix)
            rows = [dict(zip(names, values)) for values in itertools.islice(itertools.product(*(matrix[name] for name in names)), MAX_ROWS + 1)]
    else:
        raise ValueError("dataset 必须是数组、配置对象或数据集名称")
    if not isinstance(rows, list) or not rows or len(rows) > MAX_ROWS or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"数据集必须包含 1 至 {MAX_ROWS} 个对象")
    return rows


def expand_cases(cases: list[TestCase], root: Path, config: dict[str, Any]) -> list[TestCase]:
    expanded = []
    settings = config.get("sandbox", {})
    max_cases = int(positive(settings, "max_cases", 10000))
    max_input = int(positive(settings, "max_input_bytes", 64_000_000))
    input_bytes = 0

    def append(case):
        nonlocal input_bytes
        if len(expanded) >= max_cases:
            raise ValueError("展开用例数量超过 sandbox.max_cases 限制")
        # Dataset rows have already been expanded; do not repeatedly copy the source dataset.
        serialized = asdict(replace(case, dataset=None))
        serialized.pop("dataset", None)
        input_bytes += sum(len(part.encode("utf-8")) for part in json.JSONEncoder(ensure_ascii=False).iterencode(serialized))
        if input_bytes > max_input:
            raise ValueError("展开用例总大小超过 sandbox.max_input_bytes 限制")
        expanded.append(case)
    for case in cases:
        if not isinstance(case.variables, dict) or not isinstance(case.poll, dict):
            raise ValueError(f"{case.id}: variables 和 poll 必须是对象")
        if any(type(getattr(case, name)) is not bool for name in ("parallel_safe", "retry_safe")):
            raise ValueError(f"{case.id}: parallel_safe 和 retry_safe 必须是布尔值")
        if not isinstance(case.resources, list) or any(not isinstance(name, str) or not name for name in case.resources):
            raise ValueError(f"{case.id}: resources 必须是非空资源名称数组")
        if case.dataset is None:
            append(case)
            continue
        row_ids: set[str] = set()
        for index, row in enumerate(dataset_rows(case.dataset, root, config), start=1):
            row_id = str(row.get("_id", index))
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", row_id) or row_id in row_ids:
                raise ValueError(f"数据集行标识不合法或重复：{row_id}")
            row_ids.add(row_id)
            item = copy.deepcopy(replace(case, dataset=None))
            item.origin_case_id = case.id
            item.dataset_id = row_id
            item.id = f"{case.id}[{row_id}]"
            item.title = f"{case.title} [{row_id}]"
            values = row.get("variables", {key: value for key, value in row.items() if key not in {"_id", "request_overrides"}})
            if not isinstance(values, dict):
                raise ValueError("数据集 variables 必须是对象")
            item.variables = {**case.variables, **values}
            if any(str(key).startswith("_iatest_") for key in item.variables):
                raise ValueError("数据集变量不能使用 _iatest_ 保留前缀")
            overrides = row.get("request_overrides", {})
            if not isinstance(overrides, dict):
                raise ValueError("数据集 request_overrides 必须是对象")
            for name, value in overrides.items():
                if name not in {"headers", "query", "path_params", "body"}:
                    raise ValueError(f"不支持的数据集请求覆盖字段：{name}")
                if name != "body" and not isinstance(value, dict):
                    raise ValueError(f"数据集 {name} 必须是对象")
                original = getattr(item, name)
                setattr(item, name, {**original, **value} if isinstance(original, dict) and isinstance(value, dict) else copy.deepcopy(value))
            append(item)
    if len({case.id for case in expanded}) != len(expanded):
        raise ValueError("展开后的用例 ID 重复")
    return expanded


def seeded_variables(seed: str | int, case_id: str) -> dict[str, str]:
    digest = hashlib.sha256(f"{seed}:{case_id}".encode()).hexdigest()
    return {"uuid": digest[:32], "random": digest[32:], "timestamp": str(10**18 + int(digest[:15], 16))}
