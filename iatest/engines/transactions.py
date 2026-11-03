"""Deterministic multi-request business transactions and phase scheduling."""

from __future__ import annotations

import math
import os
import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from iatest.engines.contracts import EngineResult
from iatest.engines.http import REQUEST_SCHEMA, HTTPExecutor, NetworkScope
from iatest.execution.assertions import MISSING, compare, json_path_value
from iatest.execution.control import RunCancelled, RunControl

STEP_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "request"],
    "properties": {
        "id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,60}$"},
        "request": REQUEST_SCHEMA,
        "extract": {
            "type": "object",
            "maxProperties": 20,
            "additionalProperties": {
                "type": "string",
                "minLength": 1,
                "maxLength": 300,
            },
        },
        "assertions": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "operator"],
                "properties": {
                    "path": {"type": "string", "maxLength": 300},
                    "operator": {
                        "enum": [
                            "eq",
                            "ne",
                            "contains",
                            "exists",
                            "not_exists",
                            "gt",
                            "gte",
                            "lt",
                            "lte",
                        ]
                    },
                    "expected": {},
                },
            },
        },
        "think_seconds": {"type": "number", "minimum": 0, "maximum": 60},
    },
}
SCENARIOS_SCHEMA = {
    "type": "array",
    "minItems": 1,
    "maxItems": 16,
    "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "steps"],
        "properties": {
            "id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,60}$"},
            "weight": {"type": "number", "exclusiveMinimum": 0, "maximum": 10000},
            **{
                key: {
                    "type": "array",
                    "maxItems": 50,
                    "minItems": 1 if key == "steps" else 0,
                    "items": STEP_SCHEMA,
                }
                for key in ("setup", "steps", "teardown")
            },
        },
    },
}
PHASES_SCHEMA = {
    "type": "array",
    "minItems": 1,
    "maxItems": 30,
    "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["duration_seconds"],
        "properties": {
            "duration_seconds": {
                "type": "number",
                "exclusiveMinimum": 0,
                "maximum": 600,
            },
            **{
                key: {"type": "number", "minimum": 0, "maximum": 10000}
                for key in ("start_rate", "end_rate")
            },
            **{
                key: {"type": "integer", "minimum": 0, "maximum": 4096}
                for key in ("start_users", "end_users")
            },
        },
    },
}


def substitute(value, variables):
    if isinstance(value, str):
        if value.startswith("{{") and value.endswith("}}") and value[2:-2] in variables:
            return variables[value[2:-2]]
        for key, item in variables.items():
            value = value.replace("{{" + key + "}}", str(item))
        if "{{" in value:
            raise ValueError("Unresolved transaction variable")
        return value
    if isinstance(value, dict):
        return {k: substitute(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, variables) for v in value]
    return value


def validate_scenarios(spec):
    scenarios = spec.get("scenarios", [])
    if scenarios and (spec.get("setup") or spec.get("teardown")):
        raise ValueError("Place setup/teardown inside each scenario")
    ids = [s["id"] for s in scenarios]
    if len(set(ids)) != len(ids):
        raise ValueError("Scenario IDs must be unique")
    for scenario in scenarios:
        for phase in ("setup", "steps", "teardown"):
            names = [s["id"] for s in scenario.get(phase, [])]
            if len(names) != len(set(names)):
                raise ValueError("Step IDs must be unique within their phase")
    if scenarios and spec.get("rate_unit", "transactions") != "transactions":
        raise ValueError(
            "Multi-request scenarios require transaction arrival rates; request RPS is measured separately"
        )
    if spec.get("phases"):
        if not math.isclose(
            sum(p["duration_seconds"] for p in spec["phases"]),
            spec["duration_seconds"],
            abs_tol=0.001,
        ):
            raise ValueError("Phase durations must sum to duration_seconds")
        mode = "rate" if spec["mode"] == "arrival_rate" else "users"
        for phase in spec["phases"]:
            if any(
                key.endswith("rate" if mode == "users" else "users") for key in phase
            ):
                raise ValueError("Phase fields must match the load mode")
            if mode == "users" and max(
                phase.get("start_users", spec.get("users", 4)),
                phase.get("end_users", spec.get("users", 4)),
            ) > spec.get("users", 4):
                raise ValueError(
                    "Phase user count exceeds the declared worker capacity"
                )
    estimate = sum(
        p["duration_seconds"]
        * max(
            p.get("start_rate", spec.get("rate", 1)),
            p.get("end_rate", spec.get("rate", 1)),
        )
        for p in spec.get("phases", [])
    ) or spec["duration_seconds"] * spec.get("rate", 1)
    if spec["mode"] == "arrival_rate" and estimate > 100000:
        raise ValueError("Arrival schedule exceeds 100000 transactions")


def phase_target(spec, elapsed):
    mode = "rate" if spec["mode"] == "arrival_rate" else "users"
    default = spec.get("rate", 1) if mode == "rate" else spec.get("users", 4)
    offset = 0
    for index, phase in enumerate(
        spec.get("phases", [{"duration_seconds": spec["duration_seconds"]}])
    ):
        if elapsed < offset + phase["duration_seconds"]:
            start = phase.get("start_" + mode, default)
            end = phase.get("end_" + mode, start)
            return index, start + (end - start) * max(0, elapsed - offset) / phase[
                "duration_seconds"
            ]
        offset += phase["duration_seconds"]
    return len(spec.get("phases", [None])) - 1, 0


def percentile(values, fraction):
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


class TransactionEngine:
    def __init__(self, root, project, *, sample_sink=None):
        self.root, self.project, self.sample_sink = root, project, sample_sink

    def execute(self, plan, context):
        spec, control = plan.spec, context.control
        accounts = []
        for entry in spec.get("account_pool", []):
            try:
                accounts.append(
                    {k: os.environ[v] for k, v in entry["input_env"].items()}
                )
            except KeyError:
                raise ValueError(
                    "Account pool environment reference is missing"
                ) from None
        if len({tuple(sorted(v.items())) for v in accounts}) != len(accounts):
            raise ValueError("Account pool contains equivalent credential inputs")
        scope = NetworkScope(self.root, self.project, spec["allowed_origins"])
        scenarios = spec.get("scenarios") or [
            {
                "id": "request",
                "steps": [{"id": "request", "request": spec["request"]}],
                "setup": [{"id": "setup", "request": spec["setup"]}]
                if spec.get("setup")
                else [],
                "teardown": [{"id": "teardown", "request": spec["teardown"]}]
                if spec.get("teardown")
                else [],
            }
        ]
        result = EngineResult(
            plan.id, "performance", run_id="test-" + uuid.uuid4().hex[:20]
        )
        namespace = spec.get("namespace", uuid.uuid4().hex)
        workers = spec.get("users", 4)
        lock, stop = threading.Lock(), threading.Event()
        samples, transaction_samples, faults, rows, lifecycle_errors = (
            [],
            [],
            [],
            [],
            [],
        )
        scheduled, dropped = 0, 0
        start = 0.0
        load_end = [0.0]
        state = {}
        counts = {"errors": 0, "business_errors": 0, "transaction_errors": 0}
        cpu_start = time.process_time()

        def emit(row):
            with lock:
                if len(rows) >= 200000:
                    raise RunCancelled("Performance sample budget exhausted")
                rows.append(row)
            if self.sample_sink:
                self.sample_sink(row)

        def steps(client, definitions, variables, scenario_id, phase, *, measured):
            for step in definitions:
                control.check() if measured else client.control.check()
                began = time.monotonic()
                http_ok, business_ok, reason = False, True, ""
                try:
                    response = client.request(substitute(step["request"], variables))
                    http_ok = response["ok"]
                    if http_ok:
                        for assertion in step.get("assertions", []):
                            if not compare(
                                json_path_value(
                                    response["json"], assertion["path"], missing=MISSING
                                ),
                                assertion["operator"],
                                substitute(assertion.get("expected"), variables),
                            ):
                                business_ok = False
                                reason = "business_assertion:" + step["id"]
                                break
                        if business_ok:
                            for key, path in step.get("extract", {}).items():
                                value = json_path_value(
                                    response["json"], path, missing=MISSING
                                )
                                if value is MISSING:
                                    business_ok, reason = (
                                        False,
                                        "missing_extraction:" + step["id"],
                                    )
                                    break
                                variables[key] = value
                except RunCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001 -- errors count; requests are never retried
                    reason = type(exc).__name__
                if measured:
                    elapsed_ms = (time.monotonic() - began) * 1000
                    with lock:
                        samples.append(elapsed_ms)
                        counts["errors"] += not http_ok
                        counts["business_errors"] += not business_ok
                        load_end[0] = time.monotonic()
                    emit(
                        {
                            "kind": "request",
                            "scenario": scenario_id,
                            "step": step["id"],
                            "phase": phase,
                            "duration_ms": elapsed_ms,
                            "offset_seconds": began - start,
                            "ok": http_ok,
                            "business_ok": business_ok,
                        }
                    )
                if not http_ok or not business_ok:
                    raise AssertionError(reason or "HTTP request failed")
                if measured and step.get("think_seconds"):
                    control.wait(step["think_seconds"])

        def initialize(index):
            rng = random.Random(
                spec.get("seed", 0) + index + spec.get("user_offset", 0)
            )
            for scenario in scenarios:
                variables = {
                    "namespace": namespace,
                    "user": index + spec.get("user_offset", 0),
                    "iteration": 0,
                    "random": rng.getrandbits(64),
                }
                if accounts:
                    variables.update(accounts[index])
                client = HTTPExecutor(scope, control)
                state[index, scenario["id"]] = client, variables, rng
                try:
                    steps(
                        client,
                        scenario.get("setup", []),
                        variables,
                        scenario["id"],
                        -1,
                        measured=False,
                    )
                except Exception as exc:  # noqa: BLE001 -- every preparation is part of the lifecycle verdict
                    with lock:
                        lifecycle_errors.append(
                            "setup:" + scenario["id"] + ":" + type(exc).__name__
                        )

        def transaction(index):
            control.check()
            rng = state[index, scenarios[0]["id"]][2]
            scenario = rng.choices(
                scenarios, weights=[s.get("weight", 1) for s in scenarios]
            )[0]
            client, persistent, _ = state[index, scenario["id"]]
            persistent["iteration"] += 1
            variables = {**persistent, "random": rng.getrandbits(64)}
            began, passed = time.monotonic(), False
            phase, _ = phase_target(spec, began - start)
            try:
                steps(
                    client,
                    scenario["steps"],
                    variables,
                    scenario["id"],
                    phase,
                    measured=True,
                )
                passed = True
            except AssertionError as exc:
                with lock:
                    if len(faults) < 10:
                        faults.append(str(exc))
            finally:
                elapsed_ms = (time.monotonic() - began) * 1000
                with lock:
                    transaction_samples.append(elapsed_ms)
                    counts["transaction_errors"] += not passed
                    load_end[0] = time.monotonic()
                emit(
                    {
                        "kind": "transaction",
                        "scenario": scenario["id"],
                        "phase": phase,
                        "duration_ms": elapsed_ms,
                        "offset_seconds": began - start,
                        "ok": passed,
                    }
                )
            wait = max(
                spec.get("think_seconds", 0),
                spec.get("pacing_seconds", 0) - (time.monotonic() - began),
            )
            if wait:
                control.wait(wait)

        def user(index):
            while (
                not stop.is_set()
                and time.monotonic() - start < spec["duration_seconds"]
            ):
                _, target = phase_target(spec, time.monotonic() - start)
                if index < math.ceil(target):
                    transaction(index)
                else:
                    control.wait(0.02)

        pool = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="iatest-transaction"
        )
        futures = {}
        try:
            for future in [pool.submit(initialize, i) for i in range(workers)]:
                future.result()
            if lifecycle_errors:
                raise RuntimeError("Scenario preparation failed")
            start = time.monotonic()
            if spec["mode"] == "users":
                futures = {i: pool.submit(user, i) for i in range(workers)}
            else:
                initial_rate = phase_target(spec, 0)[1]
                due_offset = (
                    spec.get("arrival_phase", 0) / initial_rate
                    if initial_rate > 0
                    else 0
                )
                while time.monotonic() - start < spec["duration_seconds"]:
                    control.check()
                    _phase, rate = phase_target(spec, time.monotonic() - start)
                    if rate <= 0:
                        control.wait(0.02)
                        due_offset = time.monotonic() - start
                        continue
                    if due_offset >= spec["duration_seconds"] - 1e-9:
                        break
                    due = start + due_offset
                    control.wait(max(0, due - time.monotonic()))
                    scheduled += 1
                    index = next(
                        (
                            i
                            for i in range(workers)
                            if i not in futures or futures[i].done()
                        ),
                        None,
                    )
                    if index is None or time.monotonic() - due > max(0.05, 1 / rate):
                        dropped += 1
                    else:
                        if index in futures:
                            futures[index].result()
                        futures[index] = pool.submit(transaction, index)
                    due_offset += 1 / rate
            while not all(f.done() for f in futures.values()):
                control.wait(0.02)
            for future in futures.values():
                future.result()
        except RunCancelled as exc:
            result.lifecycle, result.error = (
                ("timed_out" if "总时限" in control.reason() else "cancelled"),
                str(exc),
            )
        except Exception as exc:  # noqa: BLE001 -- retain partial measurements and cleanup
            result.lifecycle, result.error = "error", str(exc)
        finally:
            stop.set()
            pool.shutdown(wait=True, cancel_futures=True)
            cleanup = RunControl(
                timeout_seconds=5, max_requests=max(1, workers * len(scenarios) * 50)
            )
            for (index, scenario_id), (client, variables, _) in state.items():
                client.control = cleanup
                scenario = next(s for s in scenarios if s["id"] == scenario_id)
                try:
                    steps(
                        client,
                        scenario.get("teardown", []),
                        variables,
                        scenario_id,
                        -1,
                        measured=False,
                    )
                except Exception as exc:  # noqa: BLE001
                    lifecycle_errors.append(
                        "teardown:" + scenario_id + ":" + type(exc).__name__
                    )
                finally:
                    client.close()
        transactions, requests = len(transaction_samples), len(samples)
        elapsed = (
            max(0.001, max(load_end[0], start + spec["duration_seconds"]) - start)
            if start
            else 0.001
        )
        if spec["mode"] == "users":
            scheduled = transactions
        metrics = {
            "mode": spec["mode"],
            "rate_unit": "transactions" if spec.get("scenarios") else "requests",
            "namespace": namespace,
            "seed": spec.get("seed", 0),
            "requests": control.request_count() + cleanup.request_count(),
            "cleanup_requests": cleanup.request_count(),
            "completed": requests,
            "samples": requests,
            "transactions": transactions,
            "scheduled": scheduled,
            "dropped": dropped,
            "dropped_ratio": dropped / max(1, scheduled),
            **counts,
            "error_ratio": counts["errors"] / max(1, requests),
            "transaction_error_ratio": counts["transaction_errors"]
            / max(1, transactions),
            "rps": requests / elapsed,
            "tps": transactions / elapsed,
            "p50_ms": percentile(samples, 0.5),
            "p95_ms": percentile(samples, 0.95),
            "p99_ms": percentile(samples, 0.99),
            "transaction_p95_ms": percentile(transaction_samples, 0.95),
            "elapsed_seconds": elapsed,
            "generator_cpu_seconds": time.process_time() - cpu_start,
            "sample_precision": "Exact nearest-rank; bounded raw request and transaction samples",
            "scenario_counts": {
                s["id"]: sum(
                    r["kind"] == "transaction" and r["scenario"] == s["id"]
                    for r in rows
                )
                for s in scenarios
            },
            "phase_counts": {
                str(i): sum(
                    r["kind"] == "transaction" and r["phase"] == i for r in rows
                )
                for i, _ in enumerate(spec.get("phases", [None]))
            },
        }
        result.metrics = metrics
        mapping = {
            "p95_ms": "p95_ms",
            "transaction_p95_ms": "transaction_p95_ms",
            "max_error_ratio": "error_ratio",
            "max_transaction_error_ratio": "transaction_error_ratio",
            "min_rps": "rps",
            "min_tps": "tps",
            "max_dropped_ratio": "dropped_ratio",
        }
        result.steps = [
            {
                "gate": k,
                "actual": metrics[mapping[k]],
                "expected": v,
                "passed": metrics[mapping[k]] is not None
                and (
                    metrics[mapping[k]] >= v
                    if k.startswith("min_")
                    else metrics[mapping[k]] <= v
                ),
            }
            for k, v in spec["thresholds"].items()
        ]
        result.steps.append(
            {"lifecycle_errors": lifecycle_errors, "error_examples": faults}
        )
        acceptable_errors = spec["thresholds"].get("max_transaction_error_ratio", 0)
        result.verdict = (
            "inconclusive"
            if result.lifecycle != "completed"
            or not transactions
            or lifecycle_errors
            or (dropped and "max_dropped_ratio" not in spec["thresholds"])
            else "passed"
            if all(r["passed"] for r in result.steps[:-1])
            and metrics["transaction_error_ratio"] <= acceptable_errors
            else "failed"
        )
        return result
