"""Repeatable plan/index benchmarks; timings are measured, never simulated."""
from __future__ import annotations

import argparse
import json
import platform
import statistics
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from iatest.engines.contracts import ExecutionPlan
from iatest.engines.store import EngineStore
from iatest.engines.validation import validate_plan


def measure(operation, repetitions=15):
    operation()
    samples = []
    for _ in range(repetitions):
        started = time.perf_counter()
        operation()
        samples.append((time.perf_counter() - started) * 1000)
    ordered = sorted(samples)
    return {"p50_ms": statistics.median(samples), "p95_ms": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], "samples": len(samples)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--max-regression", type=float, default=2.0)
    args = parser.parse_args()
    plan = ExecutionPlan("benchmark", "data", {"records": [{"id": 1}], "checks": [{"id": "unique", "rule": "unique", "path": "$.id"}]})
    metrics = {"validate_data_plan": measure(lambda: validate_plan(plan))}
    with tempfile.TemporaryDirectory(prefix="iatest-benchmark-") as temporary:
        for size in (100, 1000):
            store = EngineStore(Path(temporary), "size-" + str(size))
            with store.connection() as conn:
                for index in range(size):
                    value = {**asdict(plan), "id": "plan-" + str(index)}
                    conn.execute("insert into plans values(?,?,?)", (value["id"], value["kind"], json.dumps(value)))
            metrics[f"read_{size}_plans"] = measure(store.plans)
    environment = {"system": platform.system(), "machine": platform.machine(), "python": platform.python_version()}
    result = {"environment": environment, "metrics": metrics}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if args.baseline:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        if baseline["environment"] != environment:
            raise SystemExit("Benchmark baseline environment differs; record a matching baseline")
        regressions = [name for name, current in metrics.items() if current["p95_ms"] > max(1, baseline["metrics"][name]["p95_ms"]) * args.max_regression]
        if regressions:
            raise SystemExit("Benchmark regression: " + ", ".join(regressions))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
