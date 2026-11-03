"""Global protocol-session allocation using the existing paired-node barrier."""

from __future__ import annotations

import copy
import math
from dataclasses import asdict

from iatest.engines.contracts import EngineResult
from iatest.nodes.distributed import dispatch
from iatest.nodes.store import ControllerStore


def allocate_wire(plan):
    nodes = plan.spec["distribution"]["nodes"]
    load = plan.spec.get("load", {})
    users = load.get("users", 1)
    budget = plan.budget.get("max_requests", 10000)
    if users < len(nodes) or budget < len(nodes):
        raise ValueError("Global users/request budget cannot cover all selected nodes")
    plans = {}
    for i, node in enumerate(nodes):
        child = copy.deepcopy(asdict(plan))
        child["preparation"] = {}
        child["spec"].pop("distribution")
        portion = child["spec"].setdefault("load", {})
        portion["users"] = users // len(nodes) + (i < users % len(nodes))
        if "rate" in portion:
            portion["rate"] /= len(nodes)
            portion["arrival_phase"] = i / len(nodes)
        child["budget"]["max_requests"] = budget // len(nodes) + (
            i < budget % len(nodes)
        )
        plans[node] = child
    return plans


def execute_distributed_wire(root, project, plan, context):
    run_id, jobs, error = dispatch(root, project, allocate_wire(plan), context.control)
    store = ControllerStore(root, project)
    rows = []
    complete = len(jobs) == len(plan.spec["distribution"]["nodes"])
    metrics = []
    for job in jobs:
        data = store.samples(job["id"])
        result = job.get("result") or {}
        measured = result.get("metrics", {})
        complete &= (
            job["state"] == "completed"
            and result.get("lifecycle") == "completed"
            and measured.get("transport_sample_count") == len(data)
            and measured.get("sessions") == len(data)
        )
        rows.extend(data)
        metrics.append(measured)
    elapsed = max([m.get("elapsed_seconds", 0) for m in metrics] + [0.000001])
    latencies = sorted(r["duration_ms"] for r in rows)
    errors = sum(not r["ok"] for r in rows)
    offered = sum(m.get("scheduled", 0) for m in metrics)
    dropped = sum(m.get("dropped", 0) for m in metrics)
    result = EngineResult(plan.id, plan.kind)
    result.run_id = run_id
    result.error = error
    result.metrics = {
        "transport": plan.spec["transport"],
        "requests": len(rows),
        "sessions": len(rows),
        "scheduled": offered,
        "dropped": dropped,
        "dropped_ratio": dropped / max(1, offered),
        "errors": errors,
        "error_ratio": errors / max(1, len(rows)),
        "p95_ms": latencies[math.ceil(len(latencies) * 0.95) - 1]
        if latencies
        else None,
        "sessions_per_second": len(rows) / elapsed,
        "elapsed_seconds": elapsed,
        "complete": complete,
    }
    allowed_errors = plan.spec.get("thresholds", {}).get("max_error_ratio", 0)
    result.verdict = (
        "passed"
        if rows and result.metrics["error_ratio"] <= allowed_errors
        else "failed"
    )
    if (
        error
        or not complete
        or any((j.get("result") or {}).get("verdict") == "inconclusive" for j in jobs)
    ):
        result.verdict = "inconclusive"
    for key, bound in plan.spec.get("thresholds", {}).items():
        name = {
            "max_error_ratio": "error_ratio",
            "p95_ms": "p95_ms",
            "min_sessions_per_second": "sessions_per_second",
            "max_dropped_ratio": "dropped_ratio",
        }[key]
        value = result.metrics[name]
        if (
            value is None
            or (value < bound if key.startswith("min_") else value > bound)
        ) and result.verdict != "inconclusive":
            result.verdict = "failed"
    if dropped and "max_dropped_ratio" not in plan.spec.get("thresholds", {}):
        result.verdict = "inconclusive"
    return result
