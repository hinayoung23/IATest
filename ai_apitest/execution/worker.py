"""Internal worker protocol; never loads project config or persists project state."""
from __future__ import annotations

from dataclasses import asdict, replace
from contextlib import redirect_stdout, redirect_stderr
import json
import math
import os
from pathlib import Path
import sys
import threading
import time

from ai_apitest.core.files import atomic_write_text
from ai_apitest.core.sandbox import SandboxPolicy, policy_scope
from ai_apitest.core.models import TestCase, CaseResult
from ai_apitest.core.serde import assertion_from_dict, step_from_dict
from ai_apitest.execution.control import RunControl


def apply_resource_limits(settings: dict) -> None:
    if os.name != "posix":
        return
    import resource
    from ai_apitest.execution.isolation import positive
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    output = int(positive(settings, "max_output_bytes", 64_000_000))
    resource.setrlimit(resource.RLIMIT_FSIZE, (output, output))
    cpu = math.ceil(positive(settings, "hard_timeout_seconds", 300))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
    # macOS RLIMIT_AS is not a reliable resident memory limit. Docker is the
    # portable resource-limited mode; Linux process workers also cap address space.
    if sys.platform.startswith("linux"):
        memory = int(positive(settings, "memory_mb", 512)) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))


def main() -> None:
    from ai_apitest.execution.runner import EndpointRateLimit, RequestRateLimiter, execute_runnable_cases
    request, output = Path(sys.argv[1]), Path(sys.argv[2])
    payload = json.loads(request.read_text(encoding="utf-8"))
    # The Windows parent assigns its Job Object before any extension can run.
    started = time.monotonic()
    while payload["policy"]["mode"] != "container" and not (output / "start").exists():
        if time.monotonic() - started > payload["timeout"]:
            raise TimeoutError("执行隔离启动握手超时")
        time.sleep(0.01)
    apply_resource_limits(payload["config"].get("sandbox", {}))
    # The parent has already normalized and expanded these cases. Normalizing
    # again would collapse dataset IDs and change the selected execution plan.
    cases = []
    for raw in payload["cases"]:
        case = dict(raw)
        case["assertions"] = [assertion_from_dict(item) for item in raw["assertions"]]
        for key in ("steps", "setup_steps", "cleanup_steps"):
            case[key] = [step_from_dict(item) for item in raw[key]]
        cases.append(TestCase(**case))
    policy = SandboxPolicy.from_dict(payload["policy"])
    indexes = {case.id: index for index, case in enumerate(cases)}
    lock = threading.Lock()
    sizes = {}
    max_output = int(payload["config"].get("sandbox", {}).get("max_output_bytes", 64_000_000))
    protocol = sys.stdout
    container = policy.mode == "container"

    def emit(line):
        if container:
            protocol.write(line + "\n")
            protocol.flush()

    def checkpoint(result):
        serialized = json.dumps(asdict(result), ensure_ascii=False)
        size = len(serialized.encode("utf-8"))
        with lock:
            sizes[result.case_id] = size
            if sum(sizes.values()) > max_output:
                raise ValueError("隔离结果超过输出大小限制")
            atomic_write_text(output / f"{indexes[result.case_id]}.json", serialized)
            emit(f"IATEST_RESULT {indexes[result.case_id]} {serialized}")

    control = RunControl(timeout_seconds=payload["timeout"], cancellation_check=lambda: (output / "cancel").exists(), max_requests=payload["config"].get("execution", {}).get("max_requests"))
    limiter = RequestRateLimiter([EndpointRateLimit(**item) for item in payload["rate_policies"]])
    try:
        with policy_scope(policy), open(os.devnull, "w") as logs, redirect_stdout(logs), redirect_stderr(logs):
            if payload.get("operation", "run") == "fixtures-check":
                import httpx
                from ai_apitest.execution.fixture_checks import check_fixtures
                from ai_apitest.execution.plugins import PluginRegistry
                from ai_apitest.execution.transport import client_options
                from ai_apitest.execution.control import ExecutionRuntime, activate
                plugins = PluginRegistry.from_config(payload["config"])
                runtime = ExecutionRuntime(control=control, plugins=plugins, policy=policy)
                def fixture_result(row):
                    case = next(case for case in cases if case.title == row["name"])
                    status = "failed" if row["status"] in {"failed", "missing"} else "skipped" if row["status"] == "disabled" else "passed"
                    checkpoint(CaseResult(case.id, case.title, status, "GET", "/", None, 0, error=row.get("error", ""), response={"fixture_check": row}))
                with activate(runtime), httpx.Client(**client_options(payload["environment"], 10)) as client:
                    options = payload["fixture_options"]
                    required = set(options.get("required_names", [name for case in cases for name in case.fixture_requirements]))
                    check_cases = [replace(case, fixture_requirements=[name for name in case.fixture_requirements if name in required]) for case in cases]
                    check_fixtures(payload["config"], check_cases, client, plugins=plugins, on_result=fixture_result, **{key: options[key] for key in ("probe", "exercise") if key in options})
            else:
                execute_runnable_cases(cases, payload["environment"], payload["config"], control, limiter, on_result=checkpoint)
        (output / "complete").touch()
        emit("IATEST_COMPLETE")
    except Exception as exc:
        from ai_apitest.core.redaction import redact
        error = redact({"error": str(exc), "environment": payload["environment"], "config": payload["config"]})["error"]
        atomic_write_text(output / "error.json", json.dumps({"error": error[:2000]}, ensure_ascii=False))
        emit("IATEST_ERROR " + json.dumps({"error": error[:2000]}, ensure_ascii=False))
        raise


if __name__ == "__main__":
    main()
