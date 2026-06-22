from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlencode

import httpx

from ai_apitest.core.config import current_environment, load_config
from ai_apitest.core.models import Assertion, CaseResult, TestCase, TestStep
from ai_apitest.core.paths import project_metadata_file, project_root
from ai_apitest.core.serde import read_json, step_from_dict
from ai_apitest.execution.assertions import evaluate, json_path
from ai_apitest.generator.cases import load_cases_from_markdown
from ai_apitest.context.storage import mark_suspect_lessons_after_run, save_execution_trend, update_known_issue_lifecycle
from ai_apitest.report.storage import save_run


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
    config = load_config(root)
    environment = current_environment(config)
    if base_url:
        environment["base_url"] = base_url
    else:
        metadata = read_json(project_metadata_file(root, project), {})
        if metadata.get("base_url"):
            environment["base_url"] = metadata["base_url"]
    cases = load_cases(root, project)
    if case_ids:
        cases = [case for case in cases if case.id in case_ids]
    runnable_cases = [case for case in cases if case.runnable]
    skipped_by_id = {case.id: skipped_result(case) for case in cases if not case.runnable}
    executed_results: list[CaseResult] = []
    if runnable_cases:
        timeout = float(config.get("execution", {}).get("timeout_seconds", 20))
        with httpx.Client(timeout=timeout) as client:
            environment_setup = run_environment_setup(environment, config, client)
            if environment_setup.get("status") in {"failed", "skipped"}:
                executed_results = [environment_setup_failed_result(case, environment_setup) for case in runnable_cases]
            else:
                executed_results = run_ordered_cases(runnable_cases, environment, config, client, environment_setup.get("context", {}))
            environment_teardown = run_environment_teardown(environment, config, client, environment_setup.get("context", {}))
            if environment_teardown:
                for result in executed_results:
                    result.cleanup = merge_lifecycle_result(result.cleanup, "environment_teardown", environment_teardown)
    executed_iter = iter(executed_results)
    results = [next(executed_iter) if case.runnable else skipped_by_id[case.id] for case in cases]
    run_id = save_run(root, results, project=project)
    run_case_ids = [item.case_id for item in results]
    save_execution_trend(root, project, run_id, results, run_scope=run_scope, case_ids=run_case_ids)
    update_known_issue_lifecycle(root, project, results)
    mark_suspect_lessons_after_run(root, project, results)
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
        return "用例标记为不可执行：缺少可靠前置测试数据或 fixture。"
    if case.risk == "env_dependent":
        return "用例标记为不可执行：依赖环境配置或鉴权条件。"
    if case.risk == "negative_behavior":
        return "用例标记为不可执行：负向行为需确认接口文档或业务容忍度。"
    return "用例标记为不可执行，已跳过真实请求。"


def run_ordered_cases(
    cases: list[TestCase],
    environment: dict[str, Any],
    config: dict[str, Any],
    client: httpx.Client,
    base_context: dict[str, Any] | None = None,
) -> list[CaseResult]:
    parallel = max(1, int(config.get("execution", {}).get("parallel", 1)))
    indexed_results: dict[int, CaseResult] = {}
    independent = [(index, case) for index, case in enumerate(cases) if not case.steps]
    chain_cases = [(index, case) for index, case in enumerate(cases) if case.steps]
    if parallel <= 1 or len(independent) <= 1:
        for index, case in independent:
            indexed_results[index] = run_case(case, environment, config, client, base_context)
    else:
        with ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = {
                executor.submit(run_case, case, environment, config, client, base_context): index
                for index, case in independent
            }
            for future in as_completed(futures):
                indexed_results[futures[future]] = future.result()
    for index, case in chain_cases:
        indexed_results[index] = run_case(case, environment, config, client, base_context)
    return [indexed_results[index] for index in range(len(cases))]


def run_case(
    case: TestCase,
    environment: dict[str, Any],
    config: dict[str, Any],
    client: httpx.Client,
    base_context: dict[str, Any] | None = None,
) -> CaseResult:
    if case.steps:
        return run_step_case(case, environment, config, client, base_context)
    context = initial_context(base_context)
    setup = run_setup_steps(case.setup_steps, environment, config, context, client)
    if setup.get("status") in {"failed", "skipped"}:
        cleanup = run_cleanup_steps(case.cleanup_steps, environment, config, context, client)
        return setup_failed_result(case, setup, cleanup)
    step = TestStep(
        name=case.title,
        method=case.method,
        path=case.path,
        headers=case.headers,
        path_params=case.path_params,
        query=case.query,
        body=case.body,
        assertions=case.assertions,
    )
    step_result = execute_step(step, environment, config, context, client)
    populate_context_from_response(context, step_result["response"])
    cleanup = run_cleanup_steps(case.cleanup_steps, environment, config, context, client)
    return CaseResult(
        case_id=case.id,
        title=case.title,
        status=step_result["status"],
        method=case.method,
        path=case.path,
        status_code=step_result["response"].get("status_code"),
        elapsed_ms=step_result["elapsed_ms"],
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


def run_step_case(
    case: TestCase,
    environment: dict[str, Any],
    config: dict[str, Any],
    client: httpx.Client,
    base_context: dict[str, Any] | None = None,
) -> CaseResult:
    started = time.perf_counter()
    context: dict[str, Any] = initial_context(base_context)
    setup = run_setup_steps(case.setup_steps, environment, config, context, client)
    if setup.get("status") in {"failed", "skipped"}:
        cleanup = run_cleanup_steps(case.cleanup_steps, environment, config, context, client)
        return setup_failed_result(case, setup, cleanup, elapsed_ms=int((time.perf_counter() - started) * 1000))
    step_results = []
    assertion_errors: list[str] = []
    error = ""
    status_code = None
    for index, step in enumerate(case.steps, start=1):
        result = execute_step(step, environment, config, context, client)
        step_results.append(result)
        status_code = result["response"].get("status_code")
        populate_context_from_response(context, result["response"])
        for name, path in step.extract.items():
            context[name] = json_path(result["response"].get("json"), path)
        if result["error"] or result["assertion_errors"]:
            error = result["error"]
            assertion_errors.extend(f"Step {index} {step.name}: {item}" for item in result["assertion_errors"])
            break
    cleanup = run_cleanup_steps(case.cleanup_steps, environment, config, context, client)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    status = "passed" if not error and not assertion_errors else "failed"
    return CaseResult(
        case_id=case.id,
        title=case.title,
        status=status,
        method="CHAIN",
        path=" -> ".join(f"{step.method} {step.path}" for step in case.steps),
        status_code=status_code,
        elapsed_ms=elapsed_ms,
        error=error,
        assertion_errors=assertion_errors,
        request={"steps": [item["request"] for item in step_results], "context": context},
        response={"steps": [item["response"] for item in step_results]},
        preconditions=case.preconditions,
        test_steps=case.test_steps,
        expected_result=case.expected_result,
        case_risk=case.risk,
        design_method=case.design_method,
        setup=setup,
        cleanup=cleanup,
    )


def run_environment_setup(environment: dict[str, Any], config: dict[str, Any], client: httpx.Client) -> dict[str, Any]:
    context = initial_context()
    steps = environment_steps(environment, "setup_steps")
    result = run_setup_steps(steps, environment, config, context, client, lifecycle="environment_setup")
    result["context"] = context
    return result


def run_environment_teardown(
    environment: dict[str, Any],
    config: dict[str, Any],
    client: httpx.Client,
    context: dict[str, Any],
) -> dict[str, Any]:
    return run_cleanup_steps(environment_steps(environment, "teardown_steps"), environment, config, context, client, lifecycle="environment_teardown")


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
) -> dict[str, Any]:
    if not steps:
        return {}
    return run_lifecycle_steps(steps, environment, config, context, client, lifecycle=lifecycle, stop_on_failure=True)


def run_cleanup_steps(
    steps: list[TestStep],
    environment: dict[str, Any],
    config: dict[str, Any],
    context: dict[str, Any],
    client: httpx.Client,
    lifecycle: str = "cleanup",
) -> dict[str, Any]:
    if not steps:
        return {}
    return run_lifecycle_steps(steps, environment, config, context, client, lifecycle=lifecycle, stop_on_failure=False)


def run_lifecycle_steps(
    steps: list[TestStep],
    environment: dict[str, Any],
    config: dict[str, Any],
    context: dict[str, Any],
    client: httpx.Client,
    *,
    lifecycle: str,
    stop_on_failure: bool,
) -> dict[str, Any]:
    results = []
    skipped = []
    failed = []
    for index, step in enumerate(steps, start=1):
        missing = missing_cleanup_context(step, context)
        if missing:
            result = {
                "status": "skipped",
                "elapsed_ms": 0,
                "error": f"缺少变量：{', '.join(missing)}",
                "assertion_errors": [],
                "request": {"method": step.method, "path": step.path, "skipped": True},
                "response": {},
            }
            skipped.append(index)
        else:
            result = execute_step(step, environment, config, context, client)
            populate_context_from_response(context, result["response"])
            for name, path in step.extract.items():
                context[name] = json_path(result["response"].get("json"), path)
            if result["error"] or result["assertion_errors"]:
                failed.append(index)
        results.append({"name": step.name, **result})
        if stop_on_failure and (failed or skipped):
            break
    status = "failed" if failed else ("skipped" if skipped else "passed")
    return {
        "lifecycle": lifecycle,
        "status": status,
        "total": len(steps),
        "failed": len(failed),
        "skipped": len(skipped),
        "steps": results,
    }


def missing_cleanup_context(step: TestStep, context: dict[str, Any]) -> list[str]:
    missing = []
    for value in step.path_params.values():
        for name in re.findall(r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}", str(value)):
            if context.get(name) in {None, ""}:
                missing.append(name)
    return sorted(set(missing))


def populate_context_from_response(context: dict[str, Any], response: dict[str, Any]) -> None:
    body = response.get("json")
    if isinstance(body, dict):
        for key, value in body.items():
            if isinstance(key, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                context.setdefault(key, value)


def execute_step(
    step: TestStep,
    environment: dict[str, Any],
    config: dict[str, Any],
    context: dict[str, Any],
    client: httpx.Client,
) -> dict[str, Any]:
    base_url = environment["base_url"].rstrip("/")
    headers = render_template({**environment.get("headers", {}), **step.headers}, context)
    path_params = render_template(step.path_params, context)
    query = render_template(step.query, context)
    body = render_template(step.body, context)
    path = apply_path_params(step.path, path_params)
    if query:
        path = f"{path}?{urlencode(query)}"
    url = f"{base_url}{path}"
    body_bytes = None
    if body is not None:
        body_bytes = json.dumps(body).encode("utf-8")
    started = time.perf_counter()
    response_snapshot: dict[str, Any] = {}
    error = ""
    try:
        response = client.request(step.method, url, content=body_bytes, headers=headers)
        response_snapshot = parse_response(response.status_code, response.headers, response.content)
        if should_fail_http_status(response.status_code, config):
            error = f"HTTP {response.status_code} 服务端错误"
    except httpx.RequestError as exc:
        error = str(exc)
    except Exception as exc:  # pragma: no cover - defensive execution boundary
        error = str(exc)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    assertion_errors = []
    if not error:
        for assertion in step.assertions:
            failure = evaluate(assertion, response_snapshot)
            if failure:
                assertion_errors.append(failure)
    status = "passed" if not error and not assertion_errors else "failed"
    return {
        "status": status,
        "elapsed_ms": elapsed_ms,
        "error": error,
        "assertion_errors": assertion_errors,
        "request": {
            "url": url,
            "method": step.method,
            "headers": headers,
            "body": body,
        },
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


def setup_failed_result(case: TestCase, setup: dict[str, Any], cleanup: dict[str, Any], elapsed_ms: int = 0) -> CaseResult:
    error = lifecycle_error_message(setup, "前置准备失败")
    return CaseResult(
        case_id=case.id,
        title=case.title,
        status="failed",
        method=case.method,
        path=case.path,
        status_code=None,
        elapsed_ms=elapsed_ms,
        error=error,
        assertion_errors=[error],
        request={"setup_failed": True},
        response={},
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


def lifecycle_error_message(result: dict[str, Any], fallback: str) -> str:
    for step in result.get("steps", []):
        if step.get("error"):
            return f"{fallback}: {step['error']}"
        if step.get("assertion_errors"):
            return f"{fallback}: {'; '.join(step['assertion_errors'])}"
    return fallback


def merge_lifecycle_result(base: dict[str, Any], key: str, value: dict[str, Any]) -> dict[str, Any]:
    if not value:
        return base
    merged = dict(base or {})
    merged[key] = value
    return merged


def initial_context(base: dict[str, Any] | None = None) -> dict[str, Any]:
    timestamp = str(int(time.time() * 1000))
    return {**(base or {}), "timestamp": timestamp}


def apply_path_params(path: str, params: dict[str, Any]) -> str:
    rendered = path
    for key, value in params.items():
        rendered = rendered.replace("{" + key + "}", quote(unquote(str(value)), safe=""))
    return rendered


def should_fail_http_status(status_code: int, config: dict[str, Any]) -> bool:
    execution = config.get("execution", {})
    return bool(execution.get("fail_on_http_5xx", execution.get("fail_on_http_error", True))) and status_code >= 500


def parse_response(status_code: int, headers: Any, body: bytes) -> dict[str, Any]:
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
    }
