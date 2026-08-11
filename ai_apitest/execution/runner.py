from __future__ import annotations

import copy
import json
import re
import threading
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote

import httpx

from ai_apitest.core.config import current_environment, load_config
from ai_apitest.core.locking import project_operation_lock
from ai_apitest.core.models import Assertion, CaseResult, Endpoint, TestCase, TestStep
from ai_apitest.core.paths import project_docs_file, project_metadata_file, project_root
from ai_apitest.core.serde import endpoint_from_dict, read_json, step_from_dict
from ai_apitest.execution.assertions import evaluate, json_path
from ai_apitest.execution.fixtures import FixtureManager, case_is_executable
from ai_apitest.generator.cases import load_cases_from_markdown
from ai_apitest.context.storage import mark_suspect_lessons_after_run, prune_context_history, save_execution_trend, update_known_issue_lifecycle
from ai_apitest.report.storage import save_run


_context_nonce_lock = threading.Lock()
_last_context_timestamp = 0


@dataclass(frozen=True, slots=True)
class EndpointRateLimit:
    method: str
    path: str
    max_requests: int
    window_seconds: float


class RequestRateLimiter:
    """Thread-safe sliding-window limiter shared by every case in one run."""

    def __init__(
        self,
        policies: list[EndpointRateLimit] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._policies = {
            (item.method.upper(), item.path): item
            for item in (policies or [])
            if item.max_requests > 0 and item.window_seconds > 0
        }
        self._clock = clock
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._requests: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._blocked_until: dict[tuple[str, str], float] = {}

    def acquire(self, method: str, path: str) -> float:
        key = (method.upper(), path)
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                policy = self._policies.get(key)
                delay = max(0.0, self._blocked_until.get(key, 0.0) - now)
                if policy is not None:
                    requests = self._requests[key]
                    threshold = now - policy.window_seconds
                    while requests and requests[0] <= threshold:
                        requests.popleft()
                    if len(requests) >= policy.max_requests:
                        delay = max(delay, requests[0] + policy.window_seconds - now)
                if delay <= 0:
                    if policy is not None:
                        self._requests[key].append(now)
                    return waited
            self._sleeper(delay)
            waited += delay

    def penalize(
        self,
        method: str,
        path: str,
        headers: dict[str, Any],
        *,
        fallback_seconds: float,
        max_wait_seconds: float,
    ) -> float:
        key = (method.upper(), path)
        delay = retry_after_seconds(headers)
        policy = self._policies.get(key)
        if delay is None and policy is not None:
            delay = policy.window_seconds
        if delay is None:
            delay = fallback_seconds
        delay = max(0.0, min(float(delay), max_wait_seconds))
        with self._lock:
            self._blocked_until[key] = max(self._blocked_until.get(key, 0.0), self._clock() + delay)
        return delay


def retry_after_seconds(headers: dict[str, Any]) -> float | None:
    value = next((str(item) for key, item in headers.items() if str(key).lower() == "retry-after"), "").strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            return max(0.0, parsed.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return None


def parse_documented_rate_limit(endpoint: Endpoint) -> EndpointRateLimit | None:
    response = endpoint.responses.get("429") or endpoint.responses.get(429)
    if not isinstance(response, dict):
        return None
    description = str(response.get("description") or "")
    duration = re.search(r"(\d+(?:\.\d+)?)\s*(秒|分钟|小时|seconds?|minutes?|hours?)", description, re.I)
    if not duration:
        return None
    unit = duration.group(2).lower()
    multiplier = 1 if unit in {"秒", "second", "seconds"} else (60 if unit in {"分钟", "minute", "minutes"} else 3600)
    window_seconds = float(duration.group(1)) * multiplier
    count = None
    for pattern in (
        r"(?:超过|最多|不超过|至多|上限(?:为)?|限制(?:为)?)\s*(\d+)\s*次",
        r"(?:more\s+than|max(?:imum)?|limit(?:ed)?\s+to)\s*(\d+)\s*(?:requests?|times?)",
        r"(\d+)\s*(?:requests?|times?)\s*(?:per|within)",
    ):
        match = re.search(pattern, description, re.I)
        if match:
            count = int(match.group(1))
            break
    if not count:
        return None
    return EndpointRateLimit(endpoint.method.upper(), endpoint.path, count, window_seconds)


def build_request_rate_limiter(root: Path, project: str, config: dict[str, Any]) -> RequestRateLimiter:
    policies: dict[tuple[str, str], EndpointRateLimit] = {}
    for raw in read_json(project_docs_file(root, project), []):
        if not isinstance(raw, dict):
            continue
        try:
            policy = parse_documented_rate_limit(endpoint_from_dict(raw))
        except (KeyError, TypeError, ValueError):
            continue
        if policy is not None:
            policies[(policy.method, policy.path)] = policy
    for raw in config.get("execution", {}).get("endpoint_rate_limits", []):
        if not isinstance(raw, dict):
            continue
        try:
            policy = EndpointRateLimit(
                method=str(raw["method"]).upper(),
                path=str(raw["path"]),
                max_requests=int(raw["max_requests"]),
                window_seconds=float(raw["window_seconds"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if policy.max_requests > 0 and policy.window_seconds > 0:
            policies[(policy.method, policy.path)] = policy
    return RequestRateLimiter(list(policies.values()))


def load_cases(root: Path, project: str = "default") -> list[TestCase]:
    return load_cases_from_markdown(root, project)


def run_cases(
    root: Path | None = None,
    case_ids: set[str] | None = None,
    base_url: str | None = None,
    project: str = "default",
    *,
    run_scope: str = "",
) -> list[CaseResult]:
    root = root or project_root()
    with project_operation_lock(root, project, operation=f"run:{run_scope or 'full'}"):
        return _run_cases_unlocked(
            root,
            case_ids=case_ids,
            base_url=base_url,
            project=project,
            run_scope=run_scope,
        )


def _run_cases_unlocked(
    root: Path,
    case_ids: set[str] | None = None,
    base_url: str | None = None,
    project: str = "default",
    *,
    run_scope: str = "",
) -> list[CaseResult]:
    config = load_config(root)
    environment = current_environment(config)
    if base_url:
        environment["base_url"] = base_url
    else:
        metadata = read_json(project_metadata_file(root, project), {})
        if metadata.get("base_url"):
            environment["base_url"] = metadata["base_url"]
    cases = load_cases(root, project)
    if case_ids is not None:
        requested_ids = {str(item) for item in case_ids}
        known_ids = {case.id for case in cases}
        unknown_ids = sorted(requested_ids - known_ids)
        if unknown_ids:
            raise ValueError(f"指定的用例不存在：{', '.join(unknown_ids)}")
        cases = [case for case in cases if case.id in requested_ids]
    if not cases:
        return []
    executable_by_id = {case.id: case_is_executable(case, config) for case in cases}
    runnable_cases = [case for case in cases if executable_by_id[case.id]]
    skipped_by_id = {
        case.id: skipped_result(case)
        for case in cases
        if not executable_by_id[case.id]
    }
    executed_results: list[CaseResult] = []
    if runnable_cases:
        timeout = float(config.get("execution", {}).get("timeout_seconds", 20))
        rate_limiter = build_request_rate_limiter(root, project, config)
        with httpx.Client(timeout=timeout) as client:
            fixture_manager = FixtureManager(config, client)
            environment_setup = run_environment_setup(environment, config, client, rate_limiter=rate_limiter)
            if environment_setup.get("status") in {"failed", "skipped"}:
                executed_results = [environment_setup_failed_result(case, environment_setup) for case in runnable_cases]
            else:
                executed_results = run_ordered_cases(
                    runnable_cases,
                    environment,
                    config,
                    client,
                    environment_setup.get("context", {}),
                    rate_limiter=rate_limiter,
                    fixture_manager=fixture_manager,
                )
            environment_teardown = run_environment_teardown(
                environment,
                config,
                client,
                environment_setup.get("context", {}),
                rate_limiter=rate_limiter,
            )
            if environment_teardown:
                for result in executed_results:
                    result.cleanup = merge_lifecycle_result(result.cleanup, "environment_teardown", environment_teardown)
                    apply_lifecycle_failure(result, environment_teardown, config, "环境清理失败")
    executed_iter = iter(executed_results)
    results = [
        next(executed_iter) if executable_by_id[case.id] else skipped_by_id[case.id]
        for case in cases
    ]
    run_id = save_run(root, results, project=project)
    run_case_ids = [item.case_id for item in results]
    save_execution_trend(root, project, run_id, results, run_scope=run_scope, case_ids=run_case_ids)
    update_known_issue_lifecycle(root, project, results)
    mark_suspect_lessons_after_run(root, project, results)
    prune_context_history(root, project)
    return results


def skipped_result(case: TestCase) -> CaseResult:
    reason = skipped_reason(case)
    return CaseResult(
        case_id=case.id,
        title=case.title,
        status="skipped",
        method=case.method,
        path=case.path,
        status_code=None,
        elapsed_ms=0,
        error=reason,
        assertion_errors=[],
        request={"skipped": True, "reason": reason},
        response={},
        preconditions=case.preconditions,
        test_steps=case.test_steps,
        expected_result=case.expected_result,
        case_risk=case.risk,
        design_method=case.design_method,
        setup={},
        cleanup={},
    )


def skipped_reason(case: TestCase) -> str:
    if case.risk == "destructive":
        return "用例标记为不可执行：破坏性操作需要人工确认后执行。"
    if case.risk == "data_dependent_without_fixture":
        return "用例标记为不可执行：缺少可靠前置测试数据或项目方场景准备能力。"
    if case.risk == "env_dependent":
        return "用例标记为不可执行：依赖环境配置或鉴权条件。"
    if case.risk == "negative_behavior":
        return "用例标记为不可执行：负向行为需确认接口文档或业务容忍度。"
    if case.risk == "documented_response_without_fixture":
        return "用例标记为不可执行：文档声明了该响应，但缺少可证明且安全的触发数据。"
    if case.risk == "shared_rate_limit":
        return "用例标记为不可执行：限流场景依赖共享流量状态，默认计划不会污染测试环境。"
    if case.risk == "fixture_required" or case.fixture_requirements:
        return "用例标记为不可执行：依赖项目方提供的外部测试能力，当前项目未接入，已跳过真实请求。"
    return "用例标记为不可执行，已跳过真实请求。"


def run_ordered_cases(
    cases: list[TestCase],
    environment: dict[str, Any],
    config: dict[str, Any],
    client: httpx.Client,
    base_context: dict[str, Any] | None = None,
    *,
    rate_limiter: RequestRateLimiter | None = None,
    fixture_manager: FixtureManager | None = None,
) -> list[CaseResult]:
    parallel = max(1, int(config.get("execution", {}).get("parallel", 1)))
    indexed_results: dict[int, CaseResult] = {}
    independent = [(index, case) for index, case in enumerate(cases) if not case.steps]
    chain_cases = [(index, case) for index, case in enumerate(cases) if case.steps]
    if parallel <= 1 or len(independent) <= 1:
        for index, case in independent:
            indexed_results[index] = run_case(
                case,
                environment,
                config,
                client,
                base_context,
                rate_limiter=rate_limiter,
                fixture_manager=fixture_manager,
            )
    else:
        with ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = {
                executor.submit(
                    run_case,
                    case,
                    environment,
                    config,
                    client,
                    base_context,
                    rate_limiter=rate_limiter,
                    fixture_manager=fixture_manager,
                ): index
                for index, case in independent
            }
            for future in as_completed(futures):
                indexed_results[futures[future]] = future.result()
    for index, case in chain_cases:
        indexed_results[index] = run_case(
            case,
            environment,
            config,
            client,
            base_context,
            rate_limiter=rate_limiter,
            fixture_manager=fixture_manager,
        )
    return [indexed_results[index] for index in range(len(cases))]


def run_case(
    case: TestCase,
    environment: dict[str, Any],
    config: dict[str, Any],
    client: httpx.Client,
    base_context: dict[str, Any] | None = None,
    *,
    rate_limiter: RequestRateLimiter | None = None,
    fixture_manager: FixtureManager | None = None,
) -> CaseResult:
    if not case.fixture_requirements:
        return run_case_without_fixture(
            case,
            environment,
            config,
            client,
            base_context,
            rate_limiter=rate_limiter,
        )
    if fixture_manager is None:
        return skipped_result(case)

    fixture_context = fixture_case_context(case, base_context)
    fixture_setup, fixture_session = fixture_manager.prepare(case, fixture_context)
    if fixture_setup.get("status") != "passed":
        fixture_cleanup = fixture_manager.cleanup(fixture_session, fixture_context)
        result = fixture_setup_failed_result(case, fixture_setup)
        result.cleanup = merge_lifecycle_result(
            result.cleanup,
            "external_capability_cleanup",
            fixture_cleanup,
        )
        apply_lifecycle_failure(result, fixture_cleanup, config, "外部测试能力清理失败")
        return result

    result = run_case_without_fixture(
        case,
        environment,
        config,
        client,
        {**(base_context or {}), **fixture_session.injected_values},
        rate_limiter=rate_limiter,
    )
    result.setup = merge_lifecycle_result(
        result.setup,
        "external_capability_setup",
        fixture_setup,
    )
    fixture_cleanup = fixture_manager.cleanup(fixture_session, fixture_context)
    result.cleanup = merge_lifecycle_result(
        result.cleanup,
        "external_capability_cleanup",
        fixture_cleanup,
    )
    apply_lifecycle_failure(result, fixture_cleanup, config, "外部测试能力清理失败")
    return result


def run_case_without_fixture(
    case: TestCase,
    environment: dict[str, Any],
    config: dict[str, Any],
    client: httpx.Client,
    base_context: dict[str, Any] | None = None,
    *,
    rate_limiter: RequestRateLimiter | None = None,
) -> CaseResult:
    if case.steps:
        return run_step_case(case, environment, config, client, base_context, rate_limiter=rate_limiter)
    started = time.perf_counter()
    context = initial_context(base_context)
    setup = run_setup_steps(case.setup_steps, environment, config, context, client, rate_limiter=rate_limiter)
    if setup.get("status") in {"failed", "skipped"}:
        cleanup = run_cleanup_steps(
            case.cleanup_steps,
            environment,
            config,
            context,
            client,
            rate_limiter=rate_limiter,
            missing_context_is_not_needed=True,
        )
        return setup_failed_result(case, setup, cleanup, elapsed_ms=int((time.perf_counter() - started) * 1000))
    step = TestStep(
        name=case.title,
        method=case.method,
        path=case.path,
        headers=case.headers,
        path_params=case.path_params,
        query=case.query,
        parameter_serialization=case.parameter_serialization,
        body=case.body,
        assertions=case.assertions,
    )
    step_result = execute_step(step, environment, config, context, client, rate_limiter=rate_limiter)
    populate_context_from_response(context, step_result["response"])
    cleanup = run_cleanup_steps(
        case.cleanup_steps,
        environment,
        config,
        context,
        client,
        rate_limiter=rate_limiter,
        missing_context_is_not_needed=bool(step_result["error"] or step_result["assertion_errors"]),
    )
    result = CaseResult(
        case_id=case.id,
        title=case.title,
        status=step_result["status"],
        method=case.method,
        path=case.path,
        status_code=step_result["response"].get("status_code"),
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        error=step_result["error"],
        assertion_errors=step_result["assertion_errors"],
        request=step_result["request"],
        response=step_result["response"],
        preconditions=case.preconditions,
        test_steps=case.test_steps,
        expected_result=case.expected_result,
        case_risk=case.risk,
        design_method=case.design_method,
        setup=setup,
        cleanup=cleanup,
    )
    apply_lifecycle_failure(result, cleanup, config, "测试数据清理失败")
    return result


def run_step_case(
    case: TestCase,
    environment: dict[str, Any],
    config: dict[str, Any],
    client: httpx.Client,
    base_context: dict[str, Any] | None = None,
    *,
    rate_limiter: RequestRateLimiter | None = None,
) -> CaseResult:
    started = time.perf_counter()
    context: dict[str, Any] = initial_context(base_context)
    setup = run_setup_steps(case.setup_steps, environment, config, context, client, rate_limiter=rate_limiter)
    if setup.get("status") in {"failed", "skipped"}:
        cleanup = run_cleanup_steps(
            case.cleanup_steps,
            environment,
            config,
            context,
            client,
            rate_limiter=rate_limiter,
            missing_context_is_not_needed=True,
        )
        return setup_failed_result(case, setup, cleanup, elapsed_ms=int((time.perf_counter() - started) * 1000))
    step_results = []
    assertion_errors: list[str] = []
    error = ""
    status_code = None
    for index, step in enumerate(case.steps, start=1):
        result = execute_step(step, environment, config, context, client, rate_limiter=rate_limiter)
        step_results.append(result)
        status_code = result["response"].get("status_code")
        populate_context_from_response(context, result["response"])
        for name, path in step.extract.items():
            context[name] = json_path(result["response"].get("json"), path)
        if result["error"] or result["assertion_errors"]:
            error = result["error"]
            assertion_errors.extend(f"Step {index} {step.name}: {item}" for item in result["assertion_errors"])
            break
    execution_context = copy.deepcopy(context)
    cleanup = run_cleanup_steps(
        case.cleanup_steps,
        environment,
        config,
        context,
        client,
        rate_limiter=rate_limiter,
        missing_context_is_not_needed=bool(error or assertion_errors),
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    status = "passed" if not error and not assertion_errors else "failed"
    result = CaseResult(
        case_id=case.id,
        title=case.title,
        status=status,
        method="CHAIN",
        path=" -> ".join(f"{step.method} {step.path}" for step in case.steps),
        status_code=status_code,
        elapsed_ms=elapsed_ms,
        error=error,
        assertion_errors=assertion_errors,
        request={"steps": [item["request"] for item in step_results], "context": execution_context},
        response={"steps": [item["response"] for item in step_results]},
        preconditions=case.preconditions,
        test_steps=case.test_steps,
        expected_result=case.expected_result,
        case_risk=case.risk,
        design_method=case.design_method,
        setup=setup,
        cleanup=cleanup,
    )
    apply_lifecycle_failure(result, cleanup, config, "测试数据清理失败")
    return result


def run_environment_setup(
    environment: dict[str, Any],
    config: dict[str, Any],
    client: httpx.Client,
    *,
    rate_limiter: RequestRateLimiter | None = None,
) -> dict[str, Any]:
    context = initial_context()
    steps = environment_steps(environment, "setup_steps")
    result = run_setup_steps(
        steps,
        environment,
        config,
        context,
        client,
        lifecycle="environment_setup",
        rate_limiter=rate_limiter,
    )
    result["context"] = context
    return result


def run_environment_teardown(
    environment: dict[str, Any],
    config: dict[str, Any],
    client: httpx.Client,
    context: dict[str, Any],
    *,
    rate_limiter: RequestRateLimiter | None = None,
) -> dict[str, Any]:
    return run_cleanup_steps(
        environment_steps(environment, "teardown_steps"),
        environment,
        config,
        context,
        client,
        lifecycle="environment_teardown",
        rate_limiter=rate_limiter,
    )


def environment_steps(environment: dict[str, Any], key: str) -> list[TestStep]:
    raw_steps = environment.get(key, [])
    if not isinstance(raw_steps, list):
        return []
    steps = []
    for item in raw_steps:
        if not isinstance(item, dict):
            continue
        try:
            steps.append(step_from_dict(item))
        except Exception:
            continue
    return steps


def run_setup_steps(
    steps: list[TestStep],
    environment: dict[str, Any],
    config: dict[str, Any],
    context: dict[str, Any],
    client: httpx.Client,
    lifecycle: str = "setup",
    *,
    rate_limiter: RequestRateLimiter | None = None,
) -> dict[str, Any]:
    if not steps:
        return {}
    return run_lifecycle_steps(
        steps,
        environment,
        config,
        context,
        client,
        lifecycle=lifecycle,
        stop_on_failure=True,
        rate_limiter=rate_limiter,
    )


def run_cleanup_steps(
    steps: list[TestStep],
    environment: dict[str, Any],
    config: dict[str, Any],
    context: dict[str, Any],
    client: httpx.Client,
    lifecycle: str = "cleanup",
    *,
    rate_limiter: RequestRateLimiter | None = None,
    missing_context_is_not_needed: bool = False,
) -> dict[str, Any]:
    if not steps:
        return {}
    return run_lifecycle_steps(
        steps,
        environment,
        config,
        context,
        client,
        lifecycle=lifecycle,
        stop_on_failure=False,
        rate_limiter=rate_limiter,
        missing_context_is_not_needed=missing_context_is_not_needed,
    )


def run_lifecycle_steps(
    steps: list[TestStep],
    environment: dict[str, Any],
    config: dict[str, Any],
    context: dict[str, Any],
    client: httpx.Client,
    *,
    lifecycle: str,
    stop_on_failure: bool,
    rate_limiter: RequestRateLimiter | None = None,
    missing_context_is_not_needed: bool = False,
) -> dict[str, Any]:
    results = []
    skipped = []
    failed = []
    not_needed = []
    for index, step in enumerate(steps, start=1):
        missing = missing_template_context(step, context)
        if missing:
            is_not_needed = lifecycle in {"cleanup", "environment_teardown"} and missing_context_is_not_needed
            result = {
                "status": "not_needed" if is_not_needed else "skipped",
                "elapsed_ms": 0,
                "error": (
                    f"上游未创建待清理资源，无需执行；缺少变量：{', '.join(missing)}"
                    if is_not_needed
                    else f"缺少变量：{', '.join(missing)}"
                ),
                "assertion_errors": [],
                "request": {"method": step.method, "path": step.path, "skipped": True},
                "response": {},
            }
            (not_needed if is_not_needed else skipped).append(index)
        else:
            result = execute_step(step, environment, config, context, client, rate_limiter=rate_limiter)
            populate_context_from_response(context, result["response"])
            for name, path in step.extract.items():
                context[name] = json_path(result["response"].get("json"), path)
            if result["error"] or result["assertion_errors"]:
                failed.append(index)
        results.append({"name": step.name, **result})
        if stop_on_failure and (failed or skipped):
            break
    status = "failed" if failed else ("skipped" if skipped else ("not_needed" if not_needed else "passed"))
    return {
        "lifecycle": lifecycle,
        "status": status,
        "total": len(steps),
        "failed": len(failed),
        "skipped": len(skipped),
        "not_needed": len(not_needed),
        "steps": results,
    }


def missing_cleanup_context(step: TestStep, context: dict[str, Any]) -> list[str]:
    """Backward-compatible alias for the complete request template preflight."""
    return missing_template_context(step, context)


def missing_template_context(step: TestStep, context: dict[str, Any]) -> list[str]:
    missing: set[str] = set()

    def inspect(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                inspect(item)
            return
        if isinstance(value, list):
            for item in value:
                inspect(item)
            return
        if not isinstance(value, str):
            return
        for name in re.findall(r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}", value):
            current = context.get(name)
            if current is None or current == "":
                missing.add(name)

    inspect(step.headers)
    inspect(step.path_params)
    inspect(step.query)
    inspect(step.body)
    for assertion in step.assertions:
        inspect(assertion.expected)
    for name in re.findall(r"{([a-zA-Z_][a-zA-Z0-9_]*)}", step.path):
        value = step.path_params.get(name)
        if value is None or value == "":
            missing.add(name)
    return sorted(missing)


def populate_context_from_response(context: dict[str, Any], response: dict[str, Any]) -> None:
    body = response.get("json")
    if isinstance(body, dict):
        for key, value in body.items():
            if isinstance(key, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                # The context represents the latest completed step. Keeping a stale
                # value here can make a later dependent request use an earlier
                # resource even though the immediately preceding response changed it.
                context[key] = value


def execute_step(
    step: TestStep,
    environment: dict[str, Any],
    config: dict[str, Any],
    context: dict[str, Any],
    client: httpx.Client,
    *,
    rate_limiter: RequestRateLimiter | None = None,
) -> dict[str, Any]:
    missing = missing_template_context(step, context)
    if missing:
        error = f"缺少变量：{', '.join(missing)}"
        return {
            "status": "failed",
            "elapsed_ms": 0,
            "error": error,
            "assertion_errors": [],
            "request": {"method": step.method, "path": step.path, "blocked": True},
            "response": {},
        }
    started = time.perf_counter()
    response_snapshot: dict[str, Any] = {}
    request_snapshot: dict[str, Any] = {"method": step.method, "path": step.path}
    error = ""
    attempts = 0
    rate_limit_wait_seconds = 0.0
    rate_limit_events: list[dict[str, Any]] = []
    try:
        base_url = str(environment["base_url"]).rstrip("/")
        headers = render_template({**environment.get("headers", {}), **step.headers}, context)
        headers = {str(key): value for key, value in headers.items() if value is not None}
        path_params = render_template(step.path_params, context)
        query = render_template(step.query, context)
        body = render_template(step.body, context)
        path = apply_path_params(step.path, path_params, step.parameter_serialization)
        if query:
            path = f"{path}?{encode_query(query, step.parameter_serialization)}"
        url = f"{base_url}{path}"
        request_kwargs = encode_request_body(
            headers,
            body,
            max_bytes=max(1, int(config.get("execution", {}).get("max_request_bytes", 20_000_000))),
        )
        request_snapshot = {
            "url": url,
            "method": step.method,
            "headers": headers,
            "body": request_body_snapshot(body, request_kwargs),
        }
        max_response_bytes = max(0, int(config.get("execution", {}).get("max_response_bytes", 5_000_000)))
        execution = config.get("execution", {})
        max_rate_limit_retries = max(0, int(execution.get("rate_limit_max_retries", 1)))
        fallback_wait = max(0.0, float(execution.get("rate_limit_fallback_wait_seconds", 1)))
        max_rate_limit_wait = max(0.0, float(execution.get("max_rate_limit_wait_seconds", 120)))
        while True:
            if rate_limiter is not None:
                rate_limit_wait_seconds += rate_limiter.acquire(step.method, step.path)
            attempts += 1
            with client.stream(step.method, url, headers=headers, **request_kwargs) as response:
                content, truncated = read_response_content(response, max_response_bytes)
                response_snapshot = parse_response(
                    response.status_code,
                    response.headers,
                    content,
                    truncated=truncated,
                    max_response_bytes=max_response_bytes,
                )
            expected_status = expected_http_status(response_snapshot, step.assertions)
            if (
                response_snapshot.get("status_code") == 429
                and not expected_status
                and rate_limiter is not None
                and attempts <= max_rate_limit_retries
            ):
                delay = rate_limiter.penalize(
                    step.method,
                    step.path,
                    response_snapshot.get("headers", {}),
                    fallback_seconds=fallback_wait,
                    max_wait_seconds=max_rate_limit_wait,
                )
                rate_limit_events.append({"attempt": attempts, "status_code": 429, "wait_seconds": delay})
                continue
            if truncated:
                error = f"响应体超过执行上限 {max_response_bytes} 字节"
            elif response_snapshot.get("status_code") == 429 and not expected_status:
                retries = max(0, attempts - 1)
                error = f"HTTP 429 接口限流，等待并重试 {retries} 次后仍未恢复"
            elif should_fail_http_status(int(response_snapshot.get("status_code") or 0), config) and not expected_status:
                error = f"HTTP {response_snapshot.get('status_code')} 服务端错误"
            break
    except httpx.RequestError as exc:
        error = str(exc)
    except Exception as exc:  # pragma: no cover - defensive execution boundary
        error = str(exc)
    request_snapshot["attempts"] = attempts
    if rate_limit_wait_seconds:
        request_snapshot["rate_limit_wait_ms"] = int(rate_limit_wait_seconds * 1000)
    if rate_limit_events:
        request_snapshot["rate_limit_events"] = rate_limit_events
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    assertion_errors = []
    if not error:
        for assertion in step.assertions:
            rendered_assertion = Assertion(
                type=assertion.type,
                target=assertion.target,
                operator=assertion.operator,
                expected=render_template(assertion.expected, context),
            )
            failure = evaluate(rendered_assertion, response_snapshot)
            if failure:
                assertion_errors.append(failure)
    status = "passed" if not error and not assertion_errors else "failed"
    return {
        "status": status,
        "elapsed_ms": elapsed_ms,
        "error": error,
        "assertion_errors": assertion_errors,
        "request": request_snapshot,
        "response": response_snapshot,
    }


def render_template(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, str):
        match = re.fullmatch(r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}", value)
        if match:
            return context.get(match.group(1), "")
        return re.sub(
            r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}",
            lambda item: str(context.get(item.group(1), "")),
            value,
        )
    if isinstance(value, list):
        return [render_template(item, context) for item in value]
    if isinstance(value, dict):
        return {key: render_template(item, context) for key, item in value.items()}
    return value


def encode_query(query: dict[str, Any], serialization: dict[str, dict[str, Any]] | None = None) -> str:
    encoded: list[str] = []
    for key, value in query.items():
        name = str(key)
        rule = (serialization or {}).get(f"query.{name}", {})
        style = str(rule.get("style") or "form")
        explode = bool(rule.get("explode", style == "form"))
        value_safe = ":/?#[]@!$&'()*+,;=" if rule.get("allow_reserved") else ""
        for pair_name, pair_value in query_parameter_pairs(name, value, style=style, explode=explode):
            encoded.append(f"{quote(str(pair_name), safe='')}={quote(wire_scalar(pair_value), safe=value_safe)}")
    return "&".join(encoded)


def query_parameter_pairs(name: str, value: Any, *, style: str, explode: bool) -> list[tuple[str, Any]]:
    if value is None:
        return [(name, "")]
    if isinstance(value, dict):
        items = [(str(key), item) for key, item in value.items()]
        if style == "deepObject":
            return [(f"{name}[{key}]", item) for key, item in items]
        if style == "form" and explode:
            return items
        flattened = [part for item in items for part in item]
        return [(name, ",".join(wire_scalar(part) for part in flattened))]
    if isinstance(value, (list, tuple)):
        if style == "spaceDelimited":
            return [(name, " ".join(wire_scalar(item) for item in value))]
        if style == "pipeDelimited":
            return [(name, "|".join(wire_scalar(item) for item in value))]
        if style == "form" and explode:
            return [(name, item) for item in value]
        return [(name, ",".join(wire_scalar(item) for item in value))]
    return [(name, value)]


def wire_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def encode_request_body(headers: dict[str, Any], body: Any, *, max_bytes: int = 20_000_000) -> dict[str, Any]:
    if body is None:
        return {}
    if isinstance(body, dict) and set(body) == {"$iatest_file"}:
        path = Path(str(body["$iatest_file"])).expanduser()
        if not path.is_file():
            raise ValueError(f"请求体文件不存在或不是文件：{path}")
        if path.stat().st_size > max_bytes:
            raise ValueError(f"请求体文件超过 {max_bytes} 字节限制：{path.name}")
        return {"content": path.read_bytes()}
    content_type = next(
        (str(value) for key, value in headers.items() if key.lower() == "content-type"),
        "application/json",
    ).split(";", 1)[0].strip().lower()
    if content_type == "application/json" or content_type.endswith("+json"):
        return {"json": body}
    if content_type == "application/x-www-form-urlencoded":
        return {"data": body if isinstance(body, dict) else {"value": body}}
    if content_type == "multipart/form-data":
        # httpx must generate the multipart boundary itself.
        for key in [name for name in headers if name.lower() == "content-type"]:
            headers.pop(key, None)
        return {"files": multipart_fields(body, max_bytes=max_bytes)}
    if isinstance(body, bytes):
        return {"content": body}
    if isinstance(body, str):
        return {"content": body.encode("utf-8")}
    return {"content": json.dumps(body, ensure_ascii=False).encode("utf-8")}


def multipart_fields(body: Any, *, max_bytes: int = 20_000_000) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {"value": (None, str(body))}
    result: dict[str, Any] = {}
    for key, value in body.items():
        if isinstance(value, dict) and "content" in value:
            content = value.get("content")
            if isinstance(content, str):
                content = content.encode("utf-8")
            result[str(key)] = (
                str(value.get("filename") or key),
                content if isinstance(content, bytes) else bytes(content or b""),
                str(value.get("content_type") or "application/octet-stream"),
            )
        elif isinstance(value, dict) and value.get("file_path"):
            path = Path(str(value["file_path"])).expanduser()
            if not path.is_file():
                raise ValueError(f"Multipart 文件不存在或不是文件：{path}")
            if path.stat().st_size > max_bytes:
                raise ValueError(f"Multipart 文件超过 {max_bytes} 字节限制：{path.name}")
            result[str(key)] = (
                str(value.get("filename") or path.name),
                path.read_bytes(),
                str(value.get("content_type") or "application/octet-stream"),
            )
        else:
            result[str(key)] = (None, str(value))
    return result


def request_body_snapshot(body: Any, request_kwargs: dict[str, Any]) -> Any:
    if isinstance(body, dict) and set(body) == {"$iatest_file"}:
        content = request_kwargs.get("content", b"")
        return {
            "file": Path(str(body["$iatest_file"])).name,
            "bytes": len(content) if isinstance(content, bytes) else 0,
        }
    return body


def read_response_content(response: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
    if max_bytes <= 0:
        return response.read(), False
    content = bytearray()
    truncated = False
    for chunk in response.iter_bytes():
        remaining = max_bytes - len(content)
        if len(chunk) > remaining:
            content.extend(chunk[:remaining])
            truncated = True
            break
        content.extend(chunk)
    return bytes(content), truncated


def setup_failed_result(case: TestCase, setup: dict[str, Any], cleanup: dict[str, Any], elapsed_ms: int = 0) -> CaseResult:
    error = lifecycle_error_message(setup, "前置准备失败")
    failed_step = next(
        (
            step
            for step in setup.get("steps", [])
            if step.get("status") in {"failed", "skipped"}
        ),
        {},
    )
    failed_response = failed_step.get("response") if isinstance(failed_step.get("response"), dict) else {}
    return CaseResult(
        case_id=case.id,
        title=case.title,
        status="failed",
        method=case.method,
        path=case.path,
        status_code=failed_response.get("status_code"),
        elapsed_ms=elapsed_ms,
        error=error,
        assertion_errors=[error],
        request={"setup_failed": True, "failed_step": failed_step.get("name", "")},
        response=failed_response,
        preconditions=case.preconditions,
        test_steps=case.test_steps,
        expected_result=case.expected_result,
        case_risk=case.risk,
        design_method=case.design_method,
        setup=setup,
        cleanup=cleanup,
    )


def environment_setup_failed_result(case: TestCase, setup: dict[str, Any]) -> CaseResult:
    error = lifecycle_error_message(setup, "环境准备失败")
    return CaseResult(
        case_id=case.id,
        title=case.title,
        status="failed",
        method=case.method,
        path=case.path,
        status_code=None,
        elapsed_ms=0,
        error=error,
        assertion_errors=[error],
        request={"environment_setup_failed": True},
        response={},
        preconditions=case.preconditions,
        test_steps=case.test_steps,
        expected_result=case.expected_result,
        case_risk=case.risk,
        design_method=case.design_method,
        setup={"environment_setup": setup},
        cleanup={},
    )


def fixture_setup_failed_result(case: TestCase, setup: dict[str, Any]) -> CaseResult:
    error = lifecycle_error_message(setup, "外部测试能力准备失败")
    return CaseResult(
        case_id=case.id,
        title=case.title,
        status="failed",
        method=case.method,
        path=case.path,
        status_code=None,
        elapsed_ms=sum(int(item.get("elapsed_ms") or 0) for item in setup.get("steps", [])),
        error=error,
        assertion_errors=[error],
        request={"external_capability_setup_failed": True},
        response={},
        preconditions=case.preconditions,
        test_steps=case.test_steps,
        expected_result=case.expected_result,
        case_risk=case.risk,
        design_method=case.design_method,
        setup={"external_capability_setup": setup},
        cleanup={},
    )


def lifecycle_error_message(result: dict[str, Any], fallback: str) -> str:
    for step in result.get("steps", []):
        if step.get("error"):
            return f"{fallback}: {step['error']}"
        if step.get("assertion_errors"):
            return f"{fallback}: {'; '.join(step['assertion_errors'])}"
    return fallback


def apply_lifecycle_failure(
    result: CaseResult,
    lifecycle: dict[str, Any],
    config: dict[str, Any],
    fallback: str,
) -> None:
    if not lifecycle or lifecycle.get("status") not in {"failed", "skipped"}:
        return
    if not bool(config.get("execution", {}).get("fail_on_cleanup_error", True)):
        return
    message = lifecycle_error_message(lifecycle, fallback)
    result.status = "failed"
    result.error = f"{result.error}; {message}".strip("; ")
    if message not in result.assertion_errors:
        result.assertion_errors.append(message)


def merge_lifecycle_result(base: dict[str, Any], key: str, value: dict[str, Any]) -> dict[str, Any]:
    if not value:
        return base
    merged = dict(base or {})
    merged[key] = value
    return merged


def initial_context(base: dict[str, Any] | None = None) -> dict[str, Any]:
    # Millisecond timestamps collide when parallel cases start in the same
    # executor tick, causing different setup steps to create and clean the same
    # resource. Nanoseconds keep one stable value within a case while isolating
    # concurrent cases without another shared lock or random dependency.
    global _last_context_timestamp
    with _context_nonce_lock:
        _last_context_timestamp = max(time.time_ns(), _last_context_timestamp + 1)
        timestamp = str(_last_context_timestamp)
    return {
        **(base or {}),
        "timestamp": timestamp,
        "uuid": uuid.uuid4().hex,
        "random": uuid.uuid4().hex,
    }


def fixture_case_context(case: TestCase, base: dict[str, Any] | None = None) -> dict[str, Any]:
    context = initial_context(base)
    context.update({"case_id": case.id, "case_title": case.title})
    for values in (case.path_params, case.query, case.body):
        if not isinstance(values, dict):
            continue
        for name, value in values.items():
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(name)):
                context.setdefault(str(name), value)
    return context


def apply_path_params(
    path: str,
    params: dict[str, Any],
    serialization: dict[str, dict[str, Any]] | None = None,
) -> str:
    rendered = path
    for key, value in params.items():
        rule = (serialization or {}).get(f"path.{key}", {})
        style = str(rule.get("style") or "simple")
        explode = bool(rule.get("explode", False))
        rendered = rendered.replace("{" + key + "}", serialize_path_parameter(str(key), value, style, explode))
    return rendered


def serialize_path_parameter(name: str, value: Any, style: str, explode: bool) -> str:
    encode = lambda item: quote(unquote(wire_scalar(item)), safe="")
    if isinstance(value, dict):
        items = [(encode(key), encode(item)) for key, item in value.items()]
        if style == "matrix":
            if explode:
                return "".join(f";{key}={item}" for key, item in items)
            return f";{encode(name)}=" + ",".join(part for item in items for part in item)
        if style == "label":
            separator = "." if explode else ","
            content = (
                separator.join(f"{key}={item}" for key, item in items)
                if explode
                else separator.join(part for item in items for part in item)
            )
            return "." + content
        if explode:
            return ",".join(f"{key}={item}" for key, item in items)
        return ",".join(part for item in items for part in item)
    values = list(value) if isinstance(value, (list, tuple)) else [value]
    encoded = [encode(item) for item in values]
    if style == "matrix":
        if explode and len(encoded) > 1:
            return "".join(f";{encode(name)}={item}" for item in encoded)
        return f";{encode(name)}=" + ",".join(encoded)
    if style == "label":
        return "." + (("." if explode else ",").join(encoded))
    return ",".join(encoded)


def should_fail_http_status(status_code: int, config: dict[str, Any]) -> bool:
    execution = config.get("execution", {})
    return bool(execution.get("fail_on_http_5xx", execution.get("fail_on_http_error", True))) and status_code >= 500


def expected_http_status(response: dict[str, Any], assertions: list[Assertion]) -> bool:
    status_assertions = [item for item in assertions if item.type == "status_code"]
    return bool(status_assertions) and any(evaluate(item, response) is None for item in status_assertions)


def parse_response(
    status_code: int,
    headers: Any,
    body: bytes,
    *,
    truncated: bool = False,
    max_response_bytes: int = 0,
) -> dict[str, Any]:
    text = body.decode("utf-8", errors="replace")
    parsed_json = None
    try:
        parsed_json = json.loads(text) if text else None
    except json.JSONDecodeError:
        parsed_json = None
    return {
        "status_code": status_code,
        "headers": {key.lower(): value for key, value in dict(headers).items()},
        "text": text,
        "json": parsed_json,
        "truncated": truncated,
        "captured_bytes": len(body),
        "max_response_bytes": max_response_bytes,
    }
