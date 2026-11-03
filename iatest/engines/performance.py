"""Single host load generator. AI is never part of the request loop."""

from __future__ import annotations

import math
import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from iatest.engines.contracts import EngineContext, EngineResult, ExecutionPlan
from iatest.engines.http import REQUEST_SCHEMA, HTTPExecutor, NetworkScope, validate
from iatest.engines.transactions import (
    PHASES_SCHEMA,
    SCENARIOS_SCHEMA,
    validate_scenarios,
)
from iatest.execution.control import RunCancelled

PERFORMANCE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "allowed_origins",
        "mode",
        "duration_seconds",
        "thresholds",
    ],
    "properties": {
        "allowed_origins": {
            "type": "array",
            "minItems": 1,
            "maxItems": 32,
            "items": {"type": "string"},
        },
        "mode": {"enum": ["arrival_rate", "users"]},
        "duration_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 600},
        "users": {"type": "integer", "minimum": 1, "maximum": 4096},
        "scenarios": SCENARIOS_SCHEMA,
        "phases": PHASES_SCHEMA,
        "pacing_seconds": {"type": "number", "minimum": 0, "maximum": 60},
        "rate_unit": {"enum": ["requests", "transactions"]},
        "namespace": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,80}$"},
        "user_offset": {"type": "integer", "minimum": 0, "maximum": 1000000},
        "account_pool": {
            "type": "array",
            "minItems": 1,
            "maxItems": 4096,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "input_env"],
                "properties": {
                    "id": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,80}$"},
                    "input_env": {
                        "type": "object",
                        "minProperties": 1,
                        "maxProperties": 20,
                        "propertyNames": {"pattern": "^[A-Za-z_][A-Za-z0-9_]*$"},
                        "additionalProperties": {
                            "type": "string",
                            "pattern": "^[A-Za-z_][A-Za-z0-9_]*$",
                        },
                    },
                },
            },
        },
        "arrival_phase": {"type": "number", "minimum": 0, "exclusiveMaximum": 1},
        "distribution": {
            "type": "object",
            "additionalProperties": False,
            "required": ["nodes"],
            "properties": {
                "nodes": {
                    "type": "array",
                    "uniqueItems": True,
                    "minItems": 1,
                    "maxItems": 32,
                    "items": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,80}$"},
                }
            },
        },
        "rate": {"type": "number", "exclusiveMinimum": 0, "maximum": 10000},
        "think_seconds": {"type": "number", "minimum": 0, "maximum": 60},
        "seed": {"type": "integer"},
        "request": REQUEST_SCHEMA,
        "setup": REQUEST_SCHEMA,
        "teardown": REQUEST_SCHEMA,
        "thresholds": {
            "type": "object",
            "minProperties": 1,
            "additionalProperties": False,
            "properties": {
                "p95_ms": {"type": "number", "exclusiveMinimum": 0},
                "max_error_ratio": {"type": "number", "minimum": 0, "maximum": 1},
                "min_rps": {"type": "number", "minimum": 0},
                "max_dropped_ratio": {"type": "number", "minimum": 0, "maximum": 1},
                "max_transaction_error_ratio": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
                "transaction_p95_ms": {"type": "number", "exclusiveMinimum": 0},
                "min_tps": {"type": "number", "minimum": 0},
            },
        },
    },
    "oneOf": [
        {"required": ["request"], "not": {"required": ["scenarios"]}},
        {"required": ["scenarios"], "not": {"required": ["request"]}},
    ],
}


def validate_performance(plan: ExecutionPlan):
    if plan.spec.get("transport"):
        from iatest.engines.wire import validate_wire

        return validate_wire(plan)
    validate(plan.spec, PERFORMANCE_SCHEMA)
    validate_scenarios(plan.spec)
    pool = plan.spec.get("account_pool")
    if pool:
        if len(pool) != plan.spec.get("users", 4) or len(
            {v["id"] for v in pool}
        ) != len(pool):
            raise ValueError(
                "Account pool requires one distinct entry per declared virtual user"
            )
        if any(
            set(v["input_env"]) & {"namespace", "user", "iteration", "random"}
            for v in pool
        ):
            raise ValueError(
                "Account inputs cannot replace built-in transaction variables"
            )
        if any(set(v["input_env"]) != set(pool[0]["input_env"]) for v in pool):
            raise ValueError(
                "Account pool entries must declare the same variable names"
            )
    if not plan.spec.get("distribution") and plan.spec.get("users", 4) > 128:
        raise ValueError("Local load generator supports at most 128 users")
    if (
        plan.spec["mode"] == "arrival_rate"
        and "rate" not in plan.spec
        and not plan.spec.get("phases")
    ):
        raise ValueError("arrival_rate requires rate")
    if (
        plan.spec["mode"] == "arrival_rate"
        and plan.spec["duration_seconds"] * plan.spec.get("rate", 1) > 100000
    ):
        raise ValueError("Arrival schedule exceeds 100000 requests")


def substitute(value, variables):
    if isinstance(value, str):
        for key, replacement in variables.items():
            value = value.replace("{{" + key + "}}", str(replacement))
        return value
    if isinstance(value, dict):
        return {k: substitute(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, variables) for v in value]
    return value


class PerformanceEngine:
    def __init__(self, root, project, *, sample_sink=None):
        self.root, self.project, self.sample_sink = root, project, sample_sink

    def capabilities(self):
        return {
            "available": True,
            "kind": "performance",
            "modes": ["arrival_rate", "users"],
            "transports": ["http", "websocket", "mqtt", "grpc"],
            "wire_limits": {
                "users": 32,
                "total_sessions": 10000,
                "duration_seconds": 600,
                "distributed_global_rate": True,
            },
            "limits": {
                "users_per_node": 128,
                "global_users": 4096,
                "requests": 100000,
                "duration_seconds": 600,
            },
            "features": [
                "weighted multi-step transactions",
                "per-user extraction/assertions",
                "ramp phases and pacing",
                "paired LAN workers",
                "global raw-sample percentile aggregation",
            ],
            "limitations": [
                "Distributed load requires paired compatible workers and explicit origin/type scopes",
                "HTTP lifecycle timings include response consumption; not browser rendering timings",
            ],
        }

    def execute(self, plan: ExecutionPlan, context: EngineContext) -> EngineResult:
        if plan.spec.get("transport"):
            from iatest.engines.wire import WireEngine

            return WireEngine(self.root, self.project, self.sample_sink).execute(plan, context)
        validate_performance(plan)
        if plan.spec.get("distribution"):
            from iatest.nodes.distributed import execute_distributed

            return execute_distributed(self.root, self.project, plan, context)
        if (
            plan.spec.get("scenarios")
            or plan.spec.get("account_pool")
            or plan.spec.get("phases")
            or set(plan.spec["thresholds"])
            & {"min_tps", "max_transaction_error_ratio", "transaction_p95_ms"}
        ):
            from iatest.engines.transactions import TransactionEngine

            return TransactionEngine(
                self.root, self.project, sample_sink=self.sample_sink
            ).execute(plan, context)
        spec, control = plan.spec, context.control
        scope = NetworkScope(self.root, self.project, spec["allowed_origins"])
        for key in ("request", "setup", "teardown"):
            if key in spec:
                # Substitute only data variables, never an origin.
                scope.check(
                    substitute(
                        spec[key]["url"],
                        {
                            "namespace": "preflight",
                            "user": 0,
                            "iteration": 0,
                            "random": 0,
                        },
                    )
                )
        workers = spec.get("users", 4)
        duration = float(spec["duration_seconds"])
        result = EngineResult(
            plan.id,
            "performance",
            run_id="test-" + uuid.uuid4().hex[:20],
            limitations=self.capabilities()["limitations"],
        )
        lock = threading.Lock()
        samples, lags, errors, lifecycle_errors = [], [], [], []
        cleanup_budget = [None]
        dropped, scheduled, active, peak = 0, 0, 0, 0
        stop = threading.Event()
        ready = threading.Event()
        setup_done = threading.Barrier(workers + 1)
        namespace = spec.get("namespace", uuid.uuid4().hex)
        run_start = [0.0]
        load_end = [0.0]
        cpu_start = time.process_time()
        clients = [HTTPExecutor(scope, control) for _ in range(workers)]
        rngs = [random.Random(spec.get("seed", 0) + i) for i in range(workers)]
        iterations = [0] * workers

        def request(index, lag=0):
            nonlocal active, peak
            control.check()
            iterations[index] += 1
            variables = {
                "namespace": namespace,
                "user": index + spec.get("user_offset", 0),
                "iteration": iterations[index],
                "random": rngs[index].getrandbits(64),
            }
            with lock:
                if len(samples) + active >= 100000:
                    stop.set()
                    raise RunCancelled("Sample budget exhausted")
                active += 1
                peak = max(peak, active)
            started = time.monotonic()
            ok = False
            try:
                response = clients[index].request(
                    substitute(spec["request"], variables)
                )
                ok = response["ok"]
                reason = (
                    "" if ok else f"HTTP {response['status_code']} or response limit"
                )
            except RunCancelled:
                raise
            except Exception as exc:  # noqa: BLE001 -- report execution failure while retaining cleanup
                ok, reason = False, type(exc).__name__
            finally:
                with lock:
                    active -= 1
                    load_end[0] = time.monotonic()
                    samples.append((time.monotonic() - started) * 1000)
                    lags.append(lag * 1000)
                if self.sample_sink:
                    self.sample_sink(
                        {
                            "kind": "request",
                            "duration_ms": (time.monotonic() - started) * 1000,
                            "offset_seconds": started - run_start[0],
                            "ok": ok,
                            "business_ok": True,
                            "legacy_transaction": True,
                        }
                    )
            if not ok:
                with lock:
                    errors.append(reason)

        def lifecycle(index, phase):
            if phase not in spec:
                return
            response = clients[index].request(
                substitute(
                    spec[phase],
                    {
                        "namespace": namespace,
                        "user": index + spec.get("user_offset", 0),
                        "iteration": 0,
                        "random": 0,
                    },
                )
            )
            if not response["ok"]:
                raise ValueError(f"User {index} {phase} failed")

        def user(index):
            try:
                lifecycle(index, "setup")
            except Exception as exc:  # noqa: BLE001 -- report execution failure while retaining cleanup
                with lock:
                    lifecycle_errors.append(f"setup: {type(exc).__name__}")
            finally:
                setup_done.wait()
            ready.wait()
            try:
                while not stop.is_set() and time.monotonic() - run_start[0] < duration:
                    request(index)
                    control.wait(spec.get("think_seconds", 0))
            except RunCancelled:
                stop.set()
            finally:
                cleanup(index)

        def cleanup(index):
            # A separate bounded cleanup budget still works after cancellation.
            from iatest.execution.control import RunControl

            original = clients[index].control
            with lock:
                if cleanup_budget[0] is None:
                    cleanup_budget[0] = RunControl(
                        timeout_seconds=5, max_requests=workers * 6
                    )
            clients[index].control = cleanup_budget[0]
            try:
                lifecycle(index, "teardown")
            except Exception as exc:  # noqa: BLE001 -- report execution failure while retaining cleanup
                with lock:
                    lifecycle_errors.append(f"teardown: {type(exc).__name__}")
            finally:
                clients[index].control = original

        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="iatest-load")
        futures = {}
        try:
            if spec["mode"] == "users":
                jobs = [pool.submit(user, i) for i in range(workers)]
                setup_done.wait()
                run_start[0] = time.monotonic()
                if lifecycle_errors:
                    stop.set()
                ready.set()
                while not all(f.done() for f in jobs):
                    control.wait(0.02)
                for job in jobs:
                    job.result()
                scheduled = len(samples)
            else:
                for i in range(workers):
                    lifecycle(i, "setup")
                run_start[0] = time.monotonic()
                total = math.ceil(duration * spec["rate"])
                for n in range(total):
                    control.check()
                    due = (
                        run_start[0] + (n + spec.get("arrival_phase", 0)) / spec["rate"]
                    )
                    if due >= run_start[0] + duration:
                        break
                    control.wait(max(0, due - time.monotonic()))
                    scheduled += 1
                    lag = time.monotonic() - due
                    # Never build an unbounded queue or hide missed offered load.
                    available = next(
                        (
                            i
                            for i in range(workers)
                            if i not in futures or futures[i].done()
                        ),
                        None,
                    )
                    if available is None or lag > max(0.05, 1 / spec["rate"]):
                        dropped += 1
                        continue
                    if available in futures:
                        futures[available].result()
                    futures[available] = pool.submit(request, available, lag)
                control.wait(max(0, run_start[0] + duration - time.monotonic()))
                while not all(f.done() for f in futures.values()):
                    control.wait(0.02)
                for f in futures.values():
                    f.result()
        except RunCancelled:
            result.lifecycle = (
                "timed_out" if "总时限" in control.reason() else "cancelled"
            )
            result.error = control.reason() or "Sample budget exhausted"
        except Exception as exc:  # noqa: BLE001 -- report execution failure while retaining cleanup
            result.lifecycle, result.error = "error", str(exc)
        finally:
            stop.set()
            ready.set()
            pool.shutdown(wait=True, cancel_futures=True)
            if spec["mode"] == "arrival_rate":
                for i in range(workers):
                    cleanup(i)
            for client in clients:
                client.close()
        measurement_end = (
            max(load_end[0], run_start[0] + duration)
            if result.lifecycle == "completed"
            else load_end[0]
        )
        elapsed = max(0.001, measurement_end - run_start[0])
        ordered = sorted(samples)
        percentile = lambda p: (
            ordered[max(0, math.ceil(len(ordered) * p) - 1)] if ordered else None
        )
        metrics = {
            "mode": spec["mode"],
            "requests": control.request_count()
            + (cleanup_budget[0].request_count() if cleanup_budget[0] else 0),
            "cleanup_requests": cleanup_budget[0].request_count()
            if cleanup_budget[0]
            else 0,
            "cleanup_budget_seconds": 5,
            "completed": len(samples),
            "scheduled": scheduled,
            "dropped": dropped,
            "dropped_ratio": dropped / max(1, scheduled),
            "errors": len(errors),
            "error_ratio": len(errors) / max(1, len(samples)),
            "rps": len(samples) / elapsed,
            "p50_ms": percentile(0.5),
            "p95_ms": percentile(0.95),
            "p99_ms": percentile(0.99),
            "elapsed_seconds": elapsed,
            "max_inflight": peak,
            "max_schedule_lag_ms": max(lags, default=0),
            "generator_cpu_seconds": time.process_time() - cpu_start,
            "samples": len(samples),
            "sample_precision": "Exact nearest-rank, all completed attempts, <=100000",
            "namespace": namespace,
            "seed": spec.get("seed", 0),
        }
        result.metrics = metrics
        gates = []
        for name, threshold in spec["thresholds"].items():
            metric = {
                "max_error_ratio": "error_ratio",
                "min_rps": "rps",
                "max_dropped_ratio": "dropped_ratio",
                "p95_ms": "p95_ms",
            }[name]
            actual = metrics[metric]
            passed = actual is not None and (
                actual >= threshold if name == "min_rps" else actual <= threshold
            )
            gates.append(
                {
                    "gate": name,
                    "actual": actual,
                    "expected": threshold,
                    "passed": passed,
                }
            )
        result.steps = gates + [
            {"lifecycle_errors": lifecycle_errors, "error_examples": errors[:10]}
        ]
        if result.lifecycle != "completed" or not samples or lifecycle_errors:
            result.verdict = "inconclusive"
        elif dropped and "max_dropped_ratio" not in spec["thresholds"]:
            result.verdict = "inconclusive"
            result.limitations.append(
                "Offered load missed; declare max_dropped_ratio to evaluate generator saturation explicitly"
            )
        else:
            result.verdict = "passed" if all(g["passed"] for g in gates) else "failed"
        return result
