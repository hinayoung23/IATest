"""Internal finite-plan worker. No project checkout, model config or .env is loaded."""

from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import asdict
from pathlib import Path

from iatest.engines.contracts import EngineContext, EngineResult, ExecutionPlan
from iatest.execution.control import RunCancelled, RunControl


def main():
    # Read the one-time start payload only after the Windows Job has been attached.
    raw = sys.stdin.buffer.readline(67108865)
    if len(raw) > 67108864 or not raw.endswith(b"\n"):
        raise ValueError("Invalid worker start payload")
    payload = json.loads(raw)
    for key, value in payload.get("environment", {}).items():
        if (
            not isinstance(key, str)
            or not key.isidentifier()
            or not isinstance(value, str)
        ):
            raise ValueError("Invalid worker environment reference")
        os.environ[key] = value
    root = Path(payload["root"])
    root.mkdir(parents=True, exist_ok=True)
    (root / "iatest.json").write_text(json.dumps(payload["config"]), encoding="utf-8")
    from iatest.execution.worker import apply_resource_limits

    apply_resource_limits(payload["settings"])
    control = RunControl(
        timeout_seconds=payload["timeout"], max_requests=payload["max_requests"]
    )
    writer, pending, serial, condition = (
        threading.Lock(),
        {},
        threading.Lock(),
        threading.Condition(),
    )
    closed = threading.Event()
    sequence = 0

    def emit(value):
        data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode() + b"\n"
        if len(data) > payload["max_output"]:
            raise ValueError("Worker output exceeds quota")
        with writer:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    def listen():
        try:
            while line := sys.stdin.buffer.readline(4097):
                if len(line) > 4096:
                    break
                value = json.loads(line)
                if value["type"] == "cancel":
                    control.cancelled.set()
                elif value["type"] == "grant":
                    with condition:
                        if value["id"] in pending:
                            pending[value["id"]] = value["allowed"]
                        condition.notify_all()
                else:
                    break
        except (OSError, ValueError, KeyError):
            pass
        finally:
            closed.set()
            control.cancelled.set()
            with condition:
                condition.notify_all()

    threading.Thread(target=listen, daemon=True, name="iatest-native-control").start()

    def authorize(current):
        nonlocal sequence
        normal, cursor = False, current
        while cursor is not None:
            normal |= cursor is control
            cursor = getattr(cursor, "parent", None) or cursor.request_parent
        with serial:
            current.check()
            if closed.is_set():
                raise RunCancelled("Controller channel closed")
            sequence += 1
            number = sequence
            with condition:
                pending[number] = None
            try:
                emit(
                    {
                        "type": "request",
                        "id": number,
                        "phase": "normal" if normal else "cleanup",
                    }
                )
                with condition:
                    while pending[number] is None:
                        if closed.is_set():
                            raise RunCancelled("Controller channel closed")
                        current.check()
                        condition.wait(0.05)
                    if not pending[number]:
                        raise RunCancelled("Controller denied request budget")
            finally:
                with condition:
                    pending.pop(number, None)

    from iatest.execution import native_ipc

    native_ipc.authorize_request = authorize
    from iatest.engines.frozen import install
    from iatest.engines.registry import create_engine_registry
    from iatest.engines.store import EngineStore
    from iatest.nodes.worker import export_evidence

    install(root, "worker", payload.get("frozen_assets", []))

    plan = ExecutionPlan.from_dict(payload["plan"])
    samples = []
    try:
        registry = create_engine_registry(root, "worker")
        from iatest.engines.performance import PerformanceEngine

        registry._engines["performance"] = PerformanceEngine(
            root, "worker", sample_sink=samples.append
        )
        from iatest.core.sandbox import SandboxPolicy, policy_scope

        with policy_scope(
            SandboxPolicy.from_dict(payload["parent_policy"])
            if payload.get("parent_policy")
            else None
        ):
            from iatest.nodes.coordinator import DELEGATED

            delegated = DELEGATED.set(payload.get("global_leases", []))
            try:
                result = registry.execute(plan, EngineContext(control))
            finally:
                DELEGATED.reset(delegated)
    except Exception as exc:  # noqa: BLE001 -- bounded worker result, parent owns persistence
        result = EngineResult(
            plan.id,
            plan.kind,
            lifecycle="cancelled" if isinstance(exc, RunCancelled) else "error",
            verdict="inconclusive",
            error=str(exc),
        )
    files = export_evidence(root, "worker", result)
    children = EngineStore(root, "worker").runs(200)
    for item in children:
        item.pop("created_at", None)
    emit(
        {
            "type": "result",
            "result": asdict(result),
            "children": children,
            "evidence": files,
            "worker_pid": os.getpid(),
            "samples": samples,
        }
    )


if __name__ == "__main__":
    main()
