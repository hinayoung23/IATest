"""Global load allocation, ready barrier, bounded raw-sample aggregation."""

from __future__ import annotations

import copy
import uuid
from contextlib import ExitStack
from dataclasses import asdict

from iatest.application.engine_analysis import digest
from iatest.engines.contracts import EngineResult, ExecutionPlan
from iatest.engines.transactions import percentile
from iatest.execution.control import RunCancelled
from iatest.nodes.store import ControllerStore


def dispatch(root, project, plans, control):
    store = ControllerStore(root, project)
    run_id = "cluster-" + uuid.uuid4().hex[:20]
    jobs = []
    plans = copy.deepcopy(plans)
    current, chain = control, []
    while current:
        chain.append(current)
        current = getattr(current, "parent", None)
    # Reserve before enqueue: concurrent remote branches cannot each spend the same quota.
    # Lock order matches PlanControl.request (leaf -> root).
    while True:
        control.check()
        with ExitStack() as locks:
            for item in chain:
                locks.enter_context(item.request_lock)
            available = min(
                (
                    max(0, c.max_requests - c.requests_started)
                    for c in chain
                    if c.max_requests is not None
                ),
                default=10000,
            )
            if available >= len(plans):
                requested = sum(
                    p.get("budget", {}).get("max_requests", 10000)
                    for p in plans.values()
                )
                if requested > available:
                    for index, plan in enumerate(plans.values()):
                        plan.setdefault("budget", {})["max_requests"] = min(
                            plan.get("budget", {}).get("max_requests", 10000),
                            available // len(plans) + (index < available % len(plans)),
                        )
                reserved = sum(
                    p.get("budget", {}).get("max_requests", 10000)
                    for p in plans.values()
                )
                for item in chain:
                    item.requests_started += reserved
                    item.requests_reserved += reserved
                break
            if not any(c.requests_reserved for c in chain):
                return (
                    run_id,
                    [],
                    "Shared request budget cannot allocate a request to every node",
                )
        control.wait(0.02)
    states, consumed = [], reserved
    try:
        states, error = _wait_jobs(store, run_id, plans, control, jobs)
        consumed = sum(
            max(
                0,
                j["result"]["metrics"].get("requests", 0)
                - j["result"]["metrics"].get("cleanup_requests", 0),
            )
            if j["result"]
            else j["plan"].get("budget", {}).get("max_requests", 10000)
            for j in states
        )
        return run_id, states, error
    finally:
        # A broken DB/transport must not leave a reservation blocking sibling plans.
        for current in chain:
            with current.request_lock:
                current.requests_started += consumed - reserved
                current.requests_reserved -= reserved


def _wait_jobs(store, run_id, plans, control, jobs):
    started, error = False, ""
    try:
        for node_id, plan in plans.items():
            control.check()
            jobs.append(store.enqueue(run_id, node_id, plan))
        while True:
            control.check()
            states = [store.job(j) for j in jobs]
            if any(j["state"] in {"cancelled", "error"} for j in states):
                raise RuntimeError(
                    "Node preparation/execution failed: "
                    + "; ".join(j["error"] for j in states if j["error"])
                )
            online = {n["id"]: n["online"] for n in store.nodes()}
            if any(not online.get(j["node_id"]) for j in states):
                raise RuntimeError("A selected load generator is offline")
            if not started and all(j["state"] == "ready" for j in states):
                store.start(jobs)
                started = True
            if all(j["state"] == "completed" for j in states):
                break
            control.wait(0.05)
    except (RunCancelled, RuntimeError, ValueError) as exc:
        error = str(exc)
        store.cancel(jobs, error)
    states = [store.job(j) for j in jobs]
    return states, error


def allocate(plan):
    nodes = plan.spec["distribution"]["nodes"]
    total_users = plan.spec.get("users", 4)
    if total_users < len(nodes):
        raise ValueError(
            "Each selected load node needs at least one allocated virtual user"
        )

    def share(number, index):
        return number // len(nodes) + (index < number % len(nodes))

    plans, offset = {}, 0
    maximum_requests = plan.budget.get("max_requests", 10000)
    if maximum_requests < len(nodes):
        raise ValueError("Global request budget is smaller than selected node count")
    for index, node_id in enumerate(nodes):
        child = asdict(plan)
        spec = child["spec"]
        spec.pop("distribution")
        spec["namespace"] = "run-" + uuid.uuid4().hex[:20] + "-" + digest(node_id)[:8]
        spec["user_offset"] = offset
        spec["arrival_phase"] = index / len(nodes)
        child["preparation"] = {}
        spec["users"] = share(total_users, index)
        if "account_pool" in spec:
            spec["account_pool"] = spec["account_pool"][offset : offset + spec["users"]]
        if spec["users"] > 128:
            raise ValueError("A node allocation exceeds 128 virtual users")
        offset += spec["users"]
        child["budget"]["max_requests"] = share(maximum_requests, index)
        if "rate" in spec:
            spec["rate"] /= len(nodes)
        for phase in spec.get("phases", []):
            for key in ("start_rate", "end_rate"):
                if key in phase:
                    phase[key] /= len(nodes)
            for key in ("start_users", "end_users"):
                if key in phase:
                    phase[key] = share(phase[key], index)
        plans[node_id] = child
    return plans


def aggregate(plan, jobs, samples, error=""):
    requests = [r for r in samples if r["kind"] == "request"]
    transactions = [
        r for r in samples if r["kind"] == "transaction" or r.get("legacy_transaction")
    ]
    results = [j["result"] for j in jobs if j["result"]]
    complete = (
        len(results) == len(plan.spec["distribution"]["nodes"])
        and all(j["state"] == "completed" for j in jobs)
        and all(r["lifecycle"] == "completed" for r in results)
    )
    complete &= all(
        r["metrics"].get("transport_sample_count")
        == sum(s["node_id"] == j["node_id"] for s in samples)
        for j in jobs
        if (r := j["result"])
    )
    complete &= all(
        r["metrics"].get("completed")
        == sum(s["node_id"] == j["node_id"] and s["kind"] == "request" for s in samples)
        for j in jobs
        if (r := j["result"])
    )
    complete &= not any(
        step.get("lifecycle_errors") for r in results for step in r.get("steps", [])
    )
    duration = max(
        [plan.spec["duration_seconds"]]
        + [r["metrics"].get("elapsed_seconds", 0) for r in results]
    )
    scheduled = sum(r["metrics"].get("scheduled", 0) for r in results)
    dropped = sum(r["metrics"].get("dropped", 0) for r in results)
    errors = sum(not r["ok"] for r in requests)
    transaction_errors = sum(
        not r["ok"] or not r.get("business_ok", True) for r in transactions
    )
    metrics = {
        "mode": plan.spec["mode"],
        "rate_unit": "transactions" if plan.spec.get("scenarios") else "requests",
        "nodes": len(jobs),
        "expected_nodes": len(plan.spec["distribution"]["nodes"]),
        "completed": len(requests),
        "samples": len(requests),
        "transactions": len(transactions),
        "errors": errors,
        "transaction_errors": transaction_errors,
        "error_ratio": errors / max(1, len(requests)),
        "transaction_error_ratio": transaction_errors / max(1, len(transactions)),
        "requests": sum(r["metrics"].get("requests", 0) for r in results),
        "cleanup_requests": sum(
            r["metrics"].get("cleanup_requests", 0) for r in results
        ),
        "scheduled": scheduled,
        "dropped": dropped,
        "dropped_ratio": dropped / max(1, scheduled),
        "rps": len(requests) / duration,
        "tps": len(transactions) / duration,
        "p50_ms": percentile([r["duration_ms"] for r in requests], 0.5),
        "p95_ms": percentile([r["duration_ms"] for r in requests], 0.95),
        "p99_ms": percentile([r["duration_ms"] for r in requests], 0.99),
        "transaction_p95_ms": percentile(
            [r["duration_ms"] for r in transactions], 0.95
        ),
        "elapsed_seconds": duration,
        "coverage_complete": complete,
        "sample_precision": "Exact nearest-rank over bounded global raw samples; node percentiles are never averaged",
        "model_calls": 0,
        "node_results": [
            {
                "node_id": j["node_id"],
                "job_id": j["id"],
                "state": j["state"],
                "error": j["error"],
                "metrics": j["result"]["metrics"] if j["result"] else {},
            }
            for j in jobs
        ],
    }
    result = EngineResult(
        plan.id,
        "performance",
        lifecycle="completed" if complete else "error",
        metrics=metrics,
        error=error,
        limitations=[
            "Nodes are trusted paired execution hosts, not remotely attested hardware",
            "Shared measurement window uses a ready barrier and controller start epoch; polling/clock offset adds start uncertainty",
            "Generator/network saturation is not a server root-cause diagnosis",
        ],
    )
    mapping = {
        "p95_ms": "p95_ms",
        "transaction_p95_ms": "transaction_p95_ms",
        "max_error_ratio": "error_ratio",
        "max_transaction_error_ratio": "transaction_error_ratio",
        "min_rps": "rps",
        "min_tps": "tps",
        "max_dropped_ratio": "dropped_ratio",
    }
    for gate, threshold in plan.spec["thresholds"].items():
        actual = metrics[mapping[gate]]
        result.steps.append(
            {
                "gate": gate,
                "actual": actual,
                "expected": threshold,
                "passed": actual is not None
                and (
                    actual >= threshold
                    if gate.startswith("min_")
                    else actual <= threshold
                ),
            }
        )
    result.verdict = (
        "inconclusive"
        if not complete
        or not requests
        or (dropped and "max_dropped_ratio" not in plan.spec["thresholds"])
        else "passed"
        if all(s["passed"] for s in result.steps)
        and metrics["transaction_error_ratio"]
        <= plan.spec["thresholds"].get("max_transaction_error_ratio", 0)
        else "failed"
    )
    return result


def execute_distributed(root, project, plan, context):
    plans = allocate(plan)
    run_id, jobs, error = dispatch(root, project, plans, context.control)
    store = ControllerStore(root, project)
    samples = []
    for job in jobs:
        samples.extend(
            {**s, "node_id": job["node_id"]} for s in store.samples(job["id"])
        )
    result = aggregate(plan, jobs, samples, error)
    result.run_id = run_id
    if context.control.reason():
        result.lifecycle = "cancelled"
        result.verdict = "inconclusive"
    return result


def execute_remote(root, project, node_id, value, control):
    from iatest.nodes.coordinator import global_scope

    plan = ExecutionPlan(
        "remote-resource-scope", "remote", {"node_id": node_id, "plan": value}
    )
    with global_scope(root, project, plan, control) as ticket:
        result = _execute_remote(root, project, node_id, value, control)
        ticket.finish(result)
        return result


def _execute_remote(root, project, node_id, value, control):
    from iatest.engines.store import EngineStore

    plan = ExecutionPlan.from_dict(value)
    if plan.kind == "remote" or (
        plan.kind == "performance" and plan.spec.get("distribution")
    ):
        raise ValueError("Remote tasks cannot delegate recursively")
    run_id, jobs, error = dispatch(root, project, {node_id: value}, control)
    if len(jobs) == 1 and jobs[0]["state"] == "completed" and jobs[0]["result"]:
        result = EngineResult(**jobs[0]["result"])
        result.metrics["execution_node"] = node_id
    else:
        result = EngineResult(
            plan.id,
            plan.kind,
            lifecycle="error",
            verdict="inconclusive",
            error=error or "Remote execution incomplete",
        )
    result.run_id = result.run_id or run_id
    EngineStore(root, project).save_result(result)
    return result


def validate_remote(plan):
    from iatest.engines.http import validate
    from iatest.engines.validation import validate_plan

    validate(
        plan.spec,
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["node_id", "plan"],
            "properties": {
                "node_id": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,80}$"},
                "plan": {"type": "object"},
            },
        },
    )
    child = ExecutionPlan.from_dict(plan.spec["plan"])
    if child.kind == "remote" or (
        child.kind == "performance" and child.spec.get("distribution")
    ):
        raise ValueError("Recursive remote delegation is not supported")
    validate_plan(child)


class RemoteEngine:
    def __init__(self, root, project):
        self.root, self.project = root, project

    def capabilities(self):
        return {
            "kind": "remote",
            "available": True,
            "protocol": 1,
            "limitations": [
                "Requires an enrolled live controller and paired node with matching engine fingerprint"
            ],
        }

    def execute(self, plan, context):
        validate_remote(plan)
        result = execute_remote(
            self.root,
            self.project,
            plan.spec["node_id"],
            plan.spec["plan"],
            context.control,
        )
        return EngineResult(
            plan.id,
            "remote",
            lifecycle=result.lifecycle,
            verdict=result.verdict,
            steps=[{"node_id": plan.spec["node_id"], "result": asdict(result)}],
            metrics={
                "target_kind": result.kind,
                "execution_node": plan.spec["node_id"],
            },
            error=result.error,
            evidence=[
                {**e, "run_id": e.get("run_id") or result.run_id}
                for e in result.evidence
            ],
            outputs=result.outputs,
        )
