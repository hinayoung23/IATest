from __future__ import annotations

import json
from typing import Any, Callable

from ai_apitest.ai.provider import chat_completion, extract_json_array, repair_json_array
from ai_apitest.ai.prompting import render_prompt
from ai_apitest.core.artifact_language import output_language_instruction
from ai_apitest.core.case_schema import normalize_design_method, normalize_priority, normalize_test_case
from ai_apitest.core.models import Assertion, Endpoint, TestCase, TestStep
from ai_apitest.core.serde import normalize_assertion_operator, step_from_dict, string_list, to_jsonable
from ai_apitest.generator.cases import module_name, parameter_serialization, success_assertions

UNCERTAIN_RESULT_MARKERS = (
    "需要结合业务规则确认",
    "可能返回400",
    "可能返回 400",
    "可能返回404",
    "可能返回 404",
    "可能返回422",
    "可能返回 422",
    "可能返回500",
    "可能返回 500",
)

POSITIVE_DESIGN_METHODS = {"happy_path", "optional_field", "equivalence_class"}
NEGATIVE_DESIGN_METHODS = {"type_error", "boundary_value", "path_parameter_error", "required_field", "auth_error"}


def generate_cases_with_llm(
    *,
    endpoints: list[Endpoint],
    base_url: str,
    model: str,
    api_key_env: str,
    strategy_context: dict | None = None,
    provider: str = "openai-compatible",
    temperature: float | None = None,
    timeout: int | None = None,
    stream_callback: Callable[[str], None] | None = None,
    context_block: str = "",
) -> tuple[list[TestCase], str]:
    """Generate cases via LLM. Returns (cases, warning). Warning is empty on success."""
    content = chat_completion(
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
        provider=provider,
        messages=[
            {"role": "system", "content": system_prompt()},
            {"role": "user", "content": user_prompt(endpoints, strategy_context or default_strategy_context(), context_block=context_block)},
        ],
        temperature=temperature,
        timeout=timeout,
        stream_callback=stream_callback,
    )
    try:
        raw_cases = extract_json_array(content)
        return validate_cases(raw_cases, endpoints), ""
    except (json.JSONDecodeError, ValueError) as exc:
        return _repair_and_parse(content, str(exc), endpoints, base_url, model, api_key_env, provider)


MAX_REPAIR_ATTEMPTS = 2


def _repair_and_parse(
    content: str,
    error: str,
    endpoints: list[Endpoint],
    base_url: str,
    model: str,
    api_key_env: str,
    provider: str,
) -> tuple[list[TestCase], str]:
    """Attempt to repair malformed JSON output from LLM. Returns (cases, warning)."""
    for attempt in range(1, MAX_REPAIR_ATTEMPTS + 1):
        try:
            repaired = repair_json_array(
                original=content,
                parse_error=error,
                base_url=base_url,
                model=model,
                api_key_env=api_key_env,
                provider=provider,
            )
            raw_cases = extract_json_array(repaired)
            cases = validate_cases(raw_cases, endpoints)
            msg = "已生成用例" if attempt == 1 else f"自动修复 {attempt} 次后已生成用例"
            return cases, f"大模型输出格式异常，{msg} {len(cases)} 个。"
        except (json.JSONDecodeError, ValueError) as exc:
            error = str(exc)
            content = content  # keep original for next repair reference
        except Exception:
            break
    raise ValueError(
        f"大模型输出格式异常，自动修复 {MAX_REPAIR_ATTEMPTS} 次仍未成功，已使用本地规则生成用例。"
    )


def generate_supplemental_cases_with_llm(
    *,
    endpoints: list[Endpoint],
    existing_cases: list[TestCase],
    scenario_text: str,
    max_cases: int,
    base_url: str,
    model: str,
    api_key_env: str,
    strategy_context: dict | None = None,
    provider: str = "openai-compatible",
    temperature: float | None = None,
    timeout: int | None = None,
    stream_callback: Callable[[str], None] | None = None,
    context_block: str = "",
) -> list[TestCase]:
    content = chat_completion(
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
        provider=provider,
        messages=[
            {"role": "system", "content": system_prompt()},
            {
                "role": "user",
                "content": supplement_user_prompt(
                    endpoints=endpoints,
                    existing_cases=existing_cases,
                    scenario_text=scenario_text,
                    max_cases=max_cases,
                    strategy_context=strategy_context or default_strategy_context(),
                    context_block=context_block,
                ),
            },
        ],
        temperature=temperature,
        timeout=timeout,
        stream_callback=stream_callback,
    )
    try:
        raw_cases = extract_json_array(content)
        cases = validate_cases(raw_cases, endpoints)
        return cases[:max_cases]
    except (json.JSONDecodeError, ValueError) as exc:
        repaired_cases, _warning = _repair_and_parse(content, str(exc), endpoints, base_url, model, api_key_env, provider)
        return repaired_cases[:max_cases]


def system_prompt() -> str:
    return render_prompt(
        "case_generation_system.md",
        {"OUTPUT_LANGUAGE_INSTRUCTION": output_language_instruction()},
    )


def user_prompt(endpoints: list[Endpoint], strategy_context: dict | None = None, context_block: str = "") -> str:
    payload = [to_jsonable(endpoint) for endpoint in endpoints]
    return render_prompt(
        "case_generation_user.md",
        {
            "CONTEXT_BLOCK": context_block.strip() or "## 项目历史上下文\n\n无可注入的已确认上下文。",
            "ENDPOINTS_JSON": json.dumps(payload, ensure_ascii=False, indent=2),
            "STRATEGIES_JSON": json.dumps(strategy_context or default_strategy_context(), ensure_ascii=False, indent=2),
        },
    )


def supplement_user_prompt(
    *,
    endpoints: list[Endpoint],
    existing_cases: list[TestCase],
    scenario_text: str,
    max_cases: int,
    strategy_context: dict | None = None,
    context_block: str = "",
) -> str:
    return render_prompt(
        "case_supplement_user.md",
        {
            "CONTEXT_BLOCK": context_block.strip() or "## 项目历史上下文\n\n无可注入的已确认上下文。",
            "SCENARIO_TEXT": scenario_text.strip(),
            "STRATEGIES_JSON": json.dumps(strategy_context or default_strategy_context(), ensure_ascii=False, indent=2),
            "ENDPOINTS_JSON": json.dumps([to_jsonable(endpoint) for endpoint in endpoints], ensure_ascii=False, indent=2),
            "EXISTING_CASES_JSON": json.dumps([case_summary_for_prompt(case) for case in existing_cases], ensure_ascii=False, indent=2),
            "MAX_CASES": str(max(1, max_cases)),
        },
    )


def case_summary_for_prompt(case: TestCase) -> dict[str, Any]:
    return {
        "id": case.id,
        "title": case.title,
        "method": case.method,
        "path": case.path,
        "design_method": case.design_method,
        "tags": case.tags,
        "path_params": case.path_params,
        "query": case.query,
        "body": case.body,
        "expected_result": case.expected_result,
        "assertions": to_jsonable(case.assertions),
    }


def default_strategy_context() -> dict:
    return {
        "enabled_default_strategies": [
            {"name": "happy_path", "description": "合法参数、正常返回的主流程用例"},
            {"name": "optional_field", "description": "覆盖可选参数缺省或不同组合"},
            {"name": "type_error", "description": "参数或请求体字段类型错误"},
            {"name": "boundary_value", "description": "字符串长度、数值范围、数组长度等边界值"},
            {"name": "path_parameter_error", "description": "路径参数为空、非法格式或不存在资源"},
            {"name": "dependency_chain", "description": "接口之间存在创建、查询、删除等依赖时生成链路用例"},
        ],
        "custom_strategies": [],
    }


def validate_cases(raw_cases: list[object], endpoints: list[Endpoint]) -> list[TestCase]:
    endpoint_keys = {(endpoint.method.upper(), endpoint.path) for endpoint in endpoints}
    endpoint_by_key = {key: endpoint for endpoint in endpoints for key in [(endpoint.method.upper(), endpoint.path)]}
    counters: dict[str, int] = {}
    cases: list[TestCase] = []
    for raw in raw_cases:
        if not isinstance(raw, dict):
            continue
        method = str(raw.get("method", "")).upper()
        path = str(raw.get("path", ""))
        if method == "CHAIN":
            raw_steps = raw.get("steps")
            if not isinstance(raw_steps, list) or len(raw_steps) < 2:
                continue
            chain_steps = []
            all_valid = True
            for step in raw_steps:
                if not isinstance(step, dict):
                    all_valid = False
                    break
                s_method = str(step.get("method", "")).upper()
                s_path = str(step.get("path", ""))
                if not s_method or not s_path:
                    all_valid = False
                    break
                if (s_method, s_path) not in endpoint_keys:
                    all_valid = False
                    break  # hallucinated step — discard entire chain
                chain_steps.append(step)
            if not all_valid or not chain_steps:
                continue
            raw["steps"] = chain_steps
            module = "CHAIN"
            endpoint = Endpoint(method="CHAIN", path="", summary="链路用例")
        elif (method, path) not in endpoint_keys:
            continue
        else:
            endpoint = endpoint_by_key[(method, path)]
            module = module_name(endpoint)
        counters[module] = counters.get(module, 0) + 1
        design_method = normalize_design_method(raw.get("design_method") or "happy_path")
        negative = bool_value(raw.get("negative", False))
        case_id = str(raw.get("id") or f"TC-{module.upper()}-{counters[module]:03d}")
        assertions = []
        for item in raw.get("assertions", []):
            if not isinstance(item, dict) or not item.get("type"):
                continue
            assertions.append(
                Assertion(
                    type=str(item["type"]),
                    target=str(item.get("target", "")),
                    operator=normalize_assertion_operator(item.get("operator", "eq")),
                    expected=item.get("expected"),
                )
            )
        if not assertions:
            assertions = success_assertions(endpoint)
        case = TestCase(
            id=case_id,
            title=str(raw.get("title") or endpoint.summary or f"{method} {path}"),
            method=method,
            path=path,
            priority=normalize_priority(raw.get("priority"), negative=negative, design_method=design_method),
            tags=string_list(raw.get("tags")) or [*endpoint.tags, "ai_generated"],
            design_method=design_method,
            headers=string_dict(raw.get("headers")),
            path_params=raw.get("path_params") if isinstance(raw.get("path_params"), dict) else {},
            query=raw.get("query") if isinstance(raw.get("query"), dict) else {},
            body=raw.get("body"),
            assertions=assertions,
            source=str(raw.get("source") or f"{method} {path}"),
            runnable=bool_value(raw.get("runnable", True)),
            risk=str(raw.get("risk") or "none"),
            negative=negative,
            preconditions=string_list(raw.get("preconditions")) or [
                "测试环境和接口基础地址已正确配置。",
                "用例中的测试数据符合接口文档和业务规则。",
            ],
            test_steps=string_list(raw.get("test_steps")) or [
                f"发送 {method} {path} 请求。",
                "检查实际响应是否满足断言列表。",
            ],
            expected_result=str(raw.get("expected_result") or expected_result_from_assertions(assertions)),
            setup_steps=valid_steps(raw.get("setup_steps")),
            steps=valid_steps(raw.get("steps")),
            cleanup_steps=valid_steps(raw.get("cleanup_steps")),
            scenario_id=str(raw.get("scenario_id") or ""),
        )
        refined = refine_case_with_endpoint(case, endpoint)
        if method != "CHAIN":
            refined.parameter_serialization = parameter_serialization(endpoint)
        cases.append(normalize_test_case(refined, module=module, index=counters[module], source_tag="ai_generated"))
    return cases


def string_dict(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def valid_steps(value: object) -> list[TestStep]:
    if not isinstance(value, list):
        return []
    steps = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if not item.get("method") or not item.get("path"):
            continue
        try:
            steps.append(step_from_dict(item))
        except Exception:
            continue
    return steps


def bool_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"false", "0", "no", "否"}
    return bool(value)


def expected_result_from_assertions(assertions: list[Assertion]) -> str:
    if not assertions:
        return "以接口文档和业务规则为准。"
    return "实际响应应满足断言列表；如断言与业务规则不一致，应优先修正用例预期。"


def refine_case_with_endpoint(case: TestCase, endpoint: Endpoint) -> TestCase:
    method = case.design_method.strip()
    if method == "status_code_coverage":
        align_status_code_coverage(case, endpoint)
        return case

    if method in POSITIVE_DESIGN_METHODS and should_replace_expected_result(case.expected_result):
        success_text = documented_success_expectation(endpoint)
        if success_text:
            case.expected_result = success_text
            align_status_assertions(case.assertions, documented_success_status_codes(endpoint))
        return case

    if method in NEGATIVE_DESIGN_METHODS or case.negative:
        error_text = documented_error_expectation(endpoint)
        if error_text and should_replace_expected_result(case.expected_result):
            case.expected_result = error_text
            align_negative_assertions(case, endpoint)
        elif should_replace_expected_result(case.expected_result):
            case.expected_result = "接口文档未明确说明该异常场景响应；本用例用于探索接口对异常输入的实际处理，断言不应包含未声明的固定错误状态码。"
            remove_guessed_error_status_assertions(case)
    return case


def align_status_code_coverage(case: TestCase, endpoint: Endpoint) -> None:
    codes = documented_numeric_status_codes(endpoint)
    if not codes:
        remove_guessed_error_status_assertions(case)
        case.expected_result = documented_success_expectation(endpoint) or "接口文档未明确声明数字状态码，状态码覆盖用例不应猜测 4xx/5xx。"
        return
    align_status_assertions(case.assertions, codes)
    details = documented_response_expectation(endpoint, include_errors=True)
    case.expected_result = f"响应状态码应落在接口文档明确声明的状态码集合中：{codes}。{details}".strip()


def align_status_assertions(assertions: list[Assertion], codes: list[int]) -> None:
    if not codes:
        return
    for assertion in assertions:
        if assertion.type != "status_code":
            continue
        if len(codes) == 1:
            assertion.operator = "eq"
            assertion.expected = codes[0]
        else:
            assertion.operator = "in"
            assertion.expected = codes


def align_negative_assertions(case: TestCase, endpoint: Endpoint) -> None:
    error_codes = documented_error_status_codes(endpoint)
    if error_codes:
        align_status_assertions(case.assertions, error_codes)
        return
    remove_guessed_error_status_assertions(case)
    if not any(assertion.type != "status_code" for assertion in case.assertions):
        field = documented_error_field(endpoint)
        if field:
            case.assertions.append(Assertion(type="json_path", target=f"$.{field}", operator="exists"))


def remove_guessed_error_status_assertions(case: TestCase) -> None:
    kept = []
    for assertion in case.assertions:
        if assertion.type == "status_code" and only_guessed_error_codes(assertion.expected):
            continue
        kept.append(assertion)
    case.assertions = kept


def only_guessed_error_codes(value: Any) -> bool:
    values = value if isinstance(value, list) else [value]
    codes = []
    for item in values:
        try:
            codes.append(int(item))
        except (TypeError, ValueError):
            return False
    return bool(codes) and all(code >= 400 for code in codes)


def should_replace_expected_result(text: str) -> bool:
    normalized = text.replace(" ", "")
    return not text.strip() or any(marker in text or marker.replace(" ", "") in normalized for marker in UNCERTAIN_RESULT_MARKERS)


def documented_numeric_status_codes(endpoint: Endpoint) -> list[int]:
    codes = []
    for key in endpoint.responses:
        try:
            codes.append(int(str(key)))
        except ValueError:
            continue
    return sorted(set(codes))


def documented_success_status_codes(endpoint: Endpoint) -> list[int]:
    return [code for code in documented_numeric_status_codes(endpoint) if 200 <= code < 300]


def documented_error_status_codes(endpoint: Endpoint) -> list[int]:
    return [code for code in documented_numeric_status_codes(endpoint) if code >= 400]


def documented_success_expectation(endpoint: Endpoint) -> str:
    return documented_response_expectation(endpoint, include_errors=False)


def documented_error_expectation(endpoint: Endpoint) -> str:
    items = []
    for key, response in endpoint.responses.items():
        if is_error_response_key(key):
            items.append(response_summary(str(key), response))
    if not items:
        return ""
    return "接口文档说明该异常场景应返回错误响应：" + "；".join(items) + "。"


def documented_response_expectation(endpoint: Endpoint, *, include_errors: bool) -> str:
    items = []
    for key, response in endpoint.responses.items():
        if not include_errors and is_error_response_key(key):
            continue
        items.append(response_summary(str(key), response))
    if not items:
        return ""
    return "接口文档说明：" + "；".join(items) + "。"


def response_summary(key: str, response: Any) -> str:
    if not isinstance(response, dict):
        return f"{key} 响应 {response}"
    description = str(response.get("description") or key).strip()
    fields = response_fields(response)
    examples = response_examples(response)
    parts = [f"{key} {description}"]
    if fields:
        parts.append("响应体包含 " + "、".join(fields[:8]))
    if examples:
        parts.append("示例 " + "；".join(examples[:2]))
    return "，".join(parts)


def response_fields(response: dict[str, Any]) -> list[str]:
    schema = response.get("schema")
    if not schema and isinstance(response.get("content"), dict):
        media = response["content"].get("application/json") or next(iter(response["content"].values()), {})
        if isinstance(media, dict):
            schema = media.get("schema")
    return schema_fields(schema if isinstance(schema, dict) else {})


def schema_fields(schema: dict[str, Any]) -> list[str]:
    if schema.get("type") == "array" and isinstance(schema.get("items"), dict):
        return schema_fields(schema["items"])
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return []
    fields = []
    for name, prop in properties.items():
        description = ""
        if isinstance(prop, dict) and prop.get("description"):
            description = f"（{prop['description']}）"
        fields.append(f"{name}{description}")
    return fields


def response_examples(response: dict[str, Any]) -> list[str]:
    examples = []
    for key in ("example", "examples", "variants"):
        value = response.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            examples.extend(compact_example(item) for item in value[:3])
        else:
            examples.append(compact_example(value))
    return [item for item in examples if item]


def compact_example(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except TypeError:
        return str(value)


def documented_error_field(endpoint: Endpoint) -> str:
    for key, response in endpoint.responses.items():
        if not is_error_response_key(key) or not isinstance(response, dict):
            continue
        fields = response_fields(response)
        if fields:
            return fields[0].split("（", 1)[0]
        for example in response_examples(response):
            for candidate in ("detail", "message", "error"):
                if f'"{candidate}"' in example or f"'{candidate}'" in example:
                    return candidate
    return ""


def is_error_response_key(key: object) -> bool:
    text = str(key).lower()
    try:
        return int(text) >= 400
    except ValueError:
        return any(token in text for token in ("error", "default", "fail", "错误", "异常"))


# ═══════════════════════════════════════════════════════════════
# Two-phase generation: plan → batch execution
# ═══════════════════════════════════════════════════════════════

def generate_coverage_plan(
    *,
    endpoints: list[Endpoint],
    base_url: str,
    model: str,
    api_key_env: str,
    strategy_context: dict | None = None,
    provider: str = "openai-compatible",
    timeout: int | None = None,
    context_block: str = "",
    max_tokens: int | None = None,
    dep_graph: dict[str, Any] | None = None,
    stream_callback: Callable[[str], None] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Phase 1: LLM generates a lightweight coverage plan.

    Adaptive: single-shot for ≤15 endpoints; batched for larger projects.
    Single-shot failure automatically falls back to batched generation.
    """
    from ai_apitest.ai.prompting import render_prompt
    from ai_apitest.ai.provider import chat_completion, extract_json_array, retry_with_json_repair
    from ai_apitest.core.serde import to_jsonable

    PLAN_BATCH_SIZE = 4
    PROMPT_TOO_LARGE_CHARS = 12000

    # Estimate prompt size: endpoints JSON is the dominant factor
    prompt_estimate = len(json.dumps([to_jsonable(ep) for ep in endpoints], ensure_ascii=False))
    prompt_too_large = prompt_estimate > PROMPT_TOO_LARGE_CHARS
    many_endpoints = len(endpoints) > 15

    # Small project with manageable prompt: try single-shot first
    if not many_endpoints and not prompt_too_large:
        try:
            plan, warning = _generate_plan_single(
                endpoints=endpoints, all_endpoints=endpoints,
                base_url=base_url, model=model, api_key_env=api_key_env,
                strategy_context=strategy_context, provider=provider, timeout=timeout,
                context_block=context_block, max_tokens=max_tokens,
                dep_graph=dep_graph, stream_callback=stream_callback,
            )
            # Chain plan for single-shot path too
            if dep_graph and dep_graph.get("edges"):
                try:
                    chain_plan, chain_w = _generate_chain_plan(
                        endpoints=endpoints, existing_plan=plan,
                        base_url=base_url, model=model, api_key_env=api_key_env,
                        provider=provider, timeout=timeout, context_block=context_block,
                        max_tokens=max_tokens, dep_graph=dep_graph,
                    )
                    if chain_plan:
                        plan.extend(chain_plan)
                    if chain_w:
                        warning = (warning + "；" + chain_w) if warning else chain_w
                except Exception as exc:
                    warning = (warning + "；" if warning else "") + f"链路覆盖计划生成失败: {exc}"
            plan = _normalize_plan_ids(plan, endpoints)  # dedupe after chain merge
            return plan, warning
        except ValueError:
            # Single-shot failed → fallback to batched
            pass

    # Batched generation (large/complex project or single-shot fallback)
    all_plan: list[dict[str, Any]] = []
    warnings: list[str] = []
    if many_endpoints:
        warnings.append(f"接口较多（{len(endpoints)}个），已自动分批生成覆盖计划")
    elif prompt_too_large:
        warnings.append("接口定义较复杂，已自动分批生成覆盖计划")
    else:
        warnings.append("单次覆盖计划生成失败，已自动切换为分批生成")
    batches = [endpoints[i:i + PLAN_BATCH_SIZE] for i in range(0, len(endpoints), PLAN_BATCH_SIZE)]

    for bi, batch in enumerate(batches):
        if stream_callback:
            stream_callback(f"\n生成覆盖计划第 {bi+1}/{len(batches)} 批...\n")
        try:
            batch_plan, bw = _generate_plan_single(
                endpoints=batch, all_endpoints=endpoints,
                base_url=base_url, model=model, api_key_env=api_key_env,
                strategy_context=strategy_context, provider=provider, timeout=timeout,
                context_block=context_block, max_tokens=max_tokens,
                dep_graph=dep_graph, stream_callback=stream_callback,
                batch_context=f"第 {bi+1}/{len(batches)} 批。只为当前批接口生成场景，不要为批外接口生成，不生成 CHAIN。",
            )
            all_plan.extend(batch_plan)
            if bw:
                warnings.append(f"第{bi+1}批: {bw}")
        except Exception as exc:
            warnings.append(f"第{bi+1}批覆盖计划生成失败: {exc}")

    if not all_plan:
        raise ValueError("覆盖计划生成失败：所有批次均未产出有效场景")

    # Merge: normalize scenario_ids, dedupe
    all_plan = _normalize_plan_ids(all_plan, endpoints)

    # ── Independent CHAIN plan generation ──
    if dep_graph and dep_graph.get("edges"):
        try:
            chain_plan, chain_warning = _generate_chain_plan(
                endpoints=endpoints, existing_plan=all_plan,
                base_url=base_url, model=model, api_key_env=api_key_env,
                provider=provider, timeout=timeout, context_block=context_block,
                max_tokens=max_tokens, dep_graph=dep_graph,
            )
            if chain_plan:
                all_plan.extend(chain_plan)
            if chain_warning:
                warnings.append(f"链路计划: {chain_warning}")
        except Exception as exc:
            warnings.append(f"链路覆盖计划生成失败: {exc}")

    return all_plan, "；".join(warnings)


def _generate_plan_single(
    *,
    endpoints: list[Endpoint],
    all_endpoints: list[Endpoint],
    base_url: str,
    model: str,
    api_key_env: str,
    strategy_context: dict | None = None,
    provider: str = "openai-compatible",
    timeout: int | None = None,
    context_block: str = "",
    max_tokens: int | None = None,
    dep_graph: dict[str, Any] | None = None,
    stream_callback: Callable[[str], None] | None = None,
    batch_context: str = "",
) -> tuple[list[dict[str, Any]], str]:
    """Single LLM call to generate plan items for the given endpoints."""
    from ai_apitest.ai.prompting import render_prompt
    from ai_apitest.ai.provider import chat_completion, retry_with_json_repair
    from ai_apitest.core.serde import to_jsonable

    dep_info = _read_dep_graph(all_endpoints if batch_context else endpoints, dep_graph)
    chain_rule = "不要生成 CHAIN 条目，链路计划由独立阶段生成。" if batch_context else "不要生成 CHAIN 条目，链路计划由独立阶段生成。"
    content = chat_completion(
        base_url=base_url, model=model, api_key_env=api_key_env, provider=provider,
        messages=[{
            "role": "user",
            "content": output_language_instruction() + "\n\n" + render_prompt("coverage_plan.md", {
                "ENDPOINTS_JSON": json.dumps([to_jsonable(ep) for ep in endpoints], ensure_ascii=False, indent=2),
                "ALL_ENDPOINTS_JSON": json.dumps([to_jsonable(ep) for ep in all_endpoints], ensure_ascii=False, indent=2) if batch_context else "",
                "STRATEGIES_JSON": json.dumps(strategy_context or {}, ensure_ascii=False, indent=2),
                "CONTEXT_BLOCK": context_block.strip() or "(无)",
                "DEPENDENCY_GRAPH_JSON": json.dumps(dep_info, ensure_ascii=False, indent=2),
                "BATCH_CONTEXT": batch_context,
                "CHAIN_RULE": chain_rule,
            }),
        }],
        timeout=timeout, max_tokens=max_tokens, stream_callback=stream_callback,
    )
    try:
        plan, warning = retry_with_json_repair(
            content, parse_json=lambda t: _parse_plan(t, all_endpoints, reject_chain=True),
            base_url=base_url, model=model, api_key_env=api_key_env, provider=provider,
            max_tokens=max_tokens,
            description="（覆盖计划）",
        )
        return plan, warning
    except ValueError:
        raise ValueError("覆盖计划 JSON 格式异常，已尝试自动修复但仍无法解析")


def _normalize_plan_ids(plan: list[dict[str, Any]], endpoints: list[Endpoint]) -> list[dict[str, Any]]:
    """Dedupe and renumber scenario_ids in merged plan."""
    seen = set()
    result: list[dict[str, Any]] = []
    chain_idx = 1
    for p in plan:
        method = p.get("endpoint_method", "")
        path = p.get("endpoint_path", "")
        dm = p.get("design_method", "")
        if method == "CHAIN":
            chain_steps_key = tuple((s.get("method",""), s.get("path","")) for s in (p.get("chain_steps") or []))
            key = ("CHAIN", "", dm, chain_steps_key)
        else:
            key = (method, path, dm, p.get("title_hint", ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(p)
    # Renumber scenario_ids consistently
    for i, p in enumerate(result):
        if p.get("endpoint_method") == "CHAIN":
            p["scenario_id"] = f"SCN-CHAIN-{chain_idx:03d}"
            chain_idx += 1
        else:
            p["scenario_id"] = f"SCN-{i+1:03d}"
    return result


def _generate_chain_plan(
    *,
    endpoints: list[Endpoint],
    existing_plan: list[dict[str, Any]],
    base_url: str,
    model: str,
    api_key_env: str,
    provider: str = "openai-compatible",
    timeout: int | None = None,
    context_block: str = "",
    max_tokens: int | None = None,
    dep_graph: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Generate CHAIN-only plan items from the dependency graph."""
    if not dep_graph or not dep_graph.get("edges"):
        return [], ""

    from ai_apitest.ai.prompting import render_prompt
    from ai_apitest.ai.provider import chat_completion, retry_with_json_repair
    from ai_apitest.core.serde import to_jsonable

    # Summarize existing plan to avoid duplication
    existing_summary = [{"scenario_id": p.get("scenario_id"), "title_hint": p.get("title_hint"),
                         "endpoint_method": p.get("endpoint_method"), "endpoint_path": p.get("endpoint_path")}
                        for p in existing_plan[:20]]

    content = chat_completion(
        base_url=base_url, model=model, api_key_env=api_key_env, provider=provider,
        messages=[{
            "role": "user",
            "content": output_language_instruction() + "\n\n" + render_prompt("coverage_plan_chain.md", {
                "ENDPOINTS_JSON": json.dumps([to_jsonable(ep) for ep in endpoints], ensure_ascii=False, indent=2),
                "DEPENDENCY_GRAPH_JSON": json.dumps(dep_graph, ensure_ascii=False, indent=2),
                "EXISTING_PLAN_JSON": json.dumps(existing_summary, ensure_ascii=False, indent=2),
                "CONTEXT_BLOCK": context_block.strip() or "(无)",
            }),
        }],
        timeout=timeout, max_tokens=max_tokens,
    )
    try:
        raw, warning = retry_with_json_repair(
            content, parse_json=lambda t: _parse_chain_plan(t, endpoints, dep_graph),
            base_url=base_url, model=model, api_key_env=api_key_env, provider=provider,
            max_tokens=max_tokens, description="（链路覆盖计划）",
        )
        return raw, warning
    except ValueError:
        return [], "链路覆盖计划 JSON 格式异常，已尝试自动修复但仍无法解析"


def _parse_chain_plan(content: str, endpoints: list[Endpoint], dep_graph: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse and validate CHAIN plan items."""
    from ai_apitest.ai.provider import extract_json_array
    raw = extract_json_array(content)
    endpoint_keys = {(ep.method.upper(), ep.path) for ep in endpoints}
    edge_ids = {e.get("id", "") for e in dep_graph.get("edges", [])}
    result: list[dict[str, Any]] = []
    seen_steps_sigs = set()

    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        if str(item.get("endpoint_method", "")).upper() != "CHAIN":
            continue
        chain_steps = item.get("chain_steps")
        if not isinstance(chain_steps, list) or len(chain_steps) < 2:
            continue
        # Validate each step references real endpoints
        valid = True
        for s in chain_steps:
            if not isinstance(s, dict):
                valid = False
                break
            key = (str(s.get("method", "")).upper(), str(s.get("path", "")))
            if key not in endpoint_keys:
                valid = False
                break
        if not valid:
            continue
        # Dedupe by steps signature
        sig = tuple((str(s.get("method","")).upper(), str(s.get("path",""))) for s in chain_steps)
        if sig in seen_steps_sigs:
            continue
        seen_steps_sigs.add(sig)

        edge_refs = item.get("dependency_edge_ids") if isinstance(item.get("dependency_edge_ids"), list) else []
        result.append({
            "scenario_id": item.get("scenario_id") or f"SCN-CHAIN-{len(result)+1:03d}",
            "endpoint_method": "CHAIN",
            "endpoint_path": "",
            "design_method": "dependency_chain",
            "priority": str(item.get("priority") or "P0"),
            "title_hint": str(item.get("title_hint") or "链路用例"),
            "coverage_tags": ["dependency_chain"],
            "expected_basis": None,
            "data_dependency": None,
            "chain_steps": chain_steps,
            "setup_required": False,
            "dependency_edge_ids": [e for e in edge_refs if e in edge_ids],
            "expected_setup": [],
        })
    return result


def _parse_plan(content: str, endpoints: list[Endpoint], *, reject_chain: bool = False) -> list[dict[str, Any]]:
    from ai_apitest.ai.provider import extract_json_array
    raw = extract_json_array(content)
    endpoint_keys = {(ep.method.upper(), ep.path) for ep in endpoints}
    plan: list[dict[str, Any]] = []
    seen = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        method = str(item.get("endpoint_method", "")).upper()
        path = str(item.get("endpoint_path", ""))
        if method == "CHAIN":
            if reject_chain:
                continue  # batch mode — CHAIN handled by separate generation
            raw_steps = item.get("chain_steps")
            if not isinstance(raw_steps, list) or len(raw_steps) < 2:
                continue
            valid_steps = []
            for step in raw_steps:
                if not isinstance(step, dict):
                    continue
                step_method = str(step.get("method", "")).upper()
                step_path = str(step.get("path", ""))
                if (step_method, step_path) in endpoint_keys:
                    valid_steps.append({**step, "method": step_method, "path": step_path})
            if len(valid_steps) < 2:
                continue
        elif (method, path) not in endpoint_keys:
            continue
        dm = normalize_design_method(item.get("design_method") or "happy_path")
        scenario_id = str(item.get("scenario_id") or f"SCN-{i:04d}")
        semantic_hint = str(item.get("title_hint") or "").strip()
        key = (
            method,
            path,
            dm,
            tuple((step["method"], step["path"]) for step in valid_steps) if method == "CHAIN" else semantic_hint,
        )
        if key in seen:
            continue
        seen.add(key)
        plan.append({
            "scenario_id": scenario_id,
            "endpoint_method": method,
            "endpoint_path": path,
            "design_method": dm,
            "priority": str(item.get("priority") or "P1"),
            "title_hint": str(item.get("title_hint") or f"{method} {path} {dm}"),
            "coverage_tags": item.get("coverage_tags") if isinstance(item.get("coverage_tags"), list) else [dm],
            "expected_basis": item.get("expected_basis"),
            "data_dependency": item.get("data_dependency"),
            "chain_steps": valid_steps if method == "CHAIN" else None,
            "setup_required": bool(item.get("setup_required", False)),
            "dependency_edge_ids": item.get("dependency_edge_ids") if isinstance(item.get("dependency_edge_ids"), list) else [],
            "expected_setup": item.get("expected_setup") if isinstance(item.get("expected_setup"), list) else [],
        })
    return plan


def generate_cases_from_plan_batch(
    *,
    plan_items: list[dict[str, Any]],
    endpoints: list[Endpoint],
    base_url: str,
    model: str,
    api_key_env: str,
    provider: str = "openai-compatible",
    timeout: int | None = None,
    context_block: str = "",
    existing_cases: list[TestCase] | None = None,
    max_tokens: int | None = None,
    dep_graph: dict[str, Any] | None = None,
    stream_callback: Callable[[str], None] | None = None,
) -> tuple[list[TestCase], str]:
    """Phase 2: Generate full TestCase JSON for a batch of plan items.

    Returns (cases, warning). Each case has scenario_id matching a plan item.
    """
    from ai_apitest.ai.prompting import render_prompt
    from ai_apitest.ai.provider import chat_completion, extract_json_array, retry_with_json_repair
    from ai_apitest.core.serde import to_jsonable

    scenario_ids = {item["scenario_id"] for item in plan_items}
    existing_brief = []
    if existing_cases:
        existing_brief = [{"id": c.id, "scenario_id": getattr(c, "scenario_id", ""),
                           "title": c.title, "method": c.method, "path": c.path}
                          for c in existing_cases[-20:]]

    content = chat_completion(
        base_url=base_url, model=model, api_key_env=api_key_env, provider=provider,
        messages=[{
            "role": "system",
            "content": system_prompt(),
        }, {
            "role": "user",
            "content": output_language_instruction() + "\n\n" + render_prompt("case_generation_from_plan.md", {
                "PLAN_ITEMS_JSON": json.dumps(plan_items, ensure_ascii=False, indent=2),
                "ENDPOINTS_JSON": json.dumps([to_jsonable(ep) for ep in endpoints], ensure_ascii=False, indent=2),
                "EXISTING_CASES_JSON": json.dumps(existing_brief, ensure_ascii=False, indent=2),
                "CONTEXT_BLOCK": context_block.strip() or "(无)",
                "DEPENDENCY_GRAPH_JSON": json.dumps(_read_dep_graph(endpoints, dep_graph), ensure_ascii=False, indent=2),
            }),
        }],
        timeout=timeout, max_tokens=max_tokens, stream_callback=stream_callback,
    )
    try:
        raw, warning = retry_with_json_repair(
            content, parse_json=extract_json_array,
            base_url=base_url, model=model, api_key_env=api_key_env, provider=provider,
            max_tokens=max_tokens,
            description="（用例批次）",
        )
    except ValueError:
        return [], "JSON 解析失败"

    cases = validate_cases(raw, endpoints)

    # ── Pre-remap diagnostics: count raw scenario_id issues ──
    raw_sids = [getattr(c, "scenario_id", "") for c in cases]
    raw_empty = sum(1 for s in raw_sids if not s)
    raw_extra = set(s for s in raw_sids if s and s not in scenario_ids)
    raw_dupes = {s for s in raw_sids if s and raw_sids.count(s) > 1}
    parts = []
    if raw_empty:
        parts.append(f"空值 {raw_empty}")
    if raw_extra:
        parts.append(f"计划外 {len(raw_extra)}")
    if raw_dupes:
        parts.append(f"重复 {len(raw_dupes)}")

    # ── Remap: fill missing scenario_ids where unambiguous ──
    plan_by_triplet: dict[tuple[str, str, str], list[str]] = {}
    chain_sids = []
    for item in plan_items:
        if item["endpoint_method"] == "CHAIN":
            chain_sids.append(item["scenario_id"])
            continue
        key = (item["endpoint_method"], item["endpoint_path"], item["design_method"])
        plan_by_triplet.setdefault(key, []).append(item["scenario_id"])
    remapped = 0
    result: list[TestCase] = []
    for c in cases:
        sid = getattr(c, "scenario_id", None)
        if sid not in scenario_ids:
            if c.method == "CHAIN" and len(chain_sids) == 1:
                c.scenario_id = chain_sids[0]
                remapped += 1
            else:
                matches = plan_by_triplet.get((c.method, c.path, c.design_method), [])
                if len(matches) == 1:
                    c.scenario_id = matches[0]
                    remapped += 1
        if getattr(c, "scenario_id", "") in scenario_ids:
            result.append(c)

    # ── Post-remap diagnostics: what's still missing ──
    result_sids = [getattr(c, "scenario_id", "") for c in result if getattr(c, "scenario_id", "")]
    missing = scenario_ids - set(result_sids)
    if remapped:
        parts.append(f"已映射 {remapped}")
    if missing:
        parts.append(f"缺失 {len(missing)}")
    if parts:
        diag = "scenario_id：" + "，".join(parts)
        warning = (warning + "；" + diag) if warning else diag

    # Force-sync plan-level fields from plan items by scenario_id
    plan_by_sid = {p["scenario_id"]: p for p in plan_items}
    for c in result:
        sid = getattr(c, "scenario_id", "")
        plan_item = plan_by_sid.get(sid)
        if plan_item:
            if c.design_method != plan_item.get("design_method", c.design_method):
                c.design_method = plan_item["design_method"]
            if c.priority != plan_item.get("priority", c.priority):
                c.priority = plan_item["priority"]
            if getattr(c, "runnable", True) is False and plan_item.get("setup_required"):
                c.runnable = True
                c.risk = "data_dependent"

    return result, warning


def _rule_fill_scenario(item: dict[str, Any], endpoints: list[Endpoint], counters: dict[str, int]) -> list[TestCase]:
    """Local rule fallback for a single plan scenario."""
    from ai_apitest.generator.cases import (
        generate_dependency_chain_cases,
        generate_endpoint_cases,
    )
    dm = normalize_design_method(item.get("design_method", "happy_path"))
    method = item.get("endpoint_method", "GET")
    path = item.get("endpoint_path", "")
    if method == "CHAIN" or dm == "dependency_chain":
        result = generate_dependency_chain_cases(endpoints, counters)
        sid = item.get("scenario_id", "")
        for case in result:
            case.scenario_id = sid
        return result[:1]
    ep = next((e for e in endpoints if e.method == method and e.path == path), None)
    if not ep:
        return []
    generated = generate_endpoint_cases(ep, counters)
    result = [case for case in generated if case.design_method == dm]
    if not result and dm == "happy_path":
        result = [case for case in generated if case.method == method and case.path == path][:1]
    # Tag with scenario_id
    sid = item.get("scenario_id", "")
    for c in result:
        c.scenario_id = sid
    return result[:2] if dm == "path_parameter_error" else result[:1]


def _read_dep_graph(endpoints: list[Endpoint], dep_graph: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return validated dependency graph, filtered to current endpoints."""
    if not dep_graph or not isinstance(dep_graph.get("edges"), list):
        return {"edges": [], "rule": ""}
    ep_keys = {(ep.method, ep.path) for ep in endpoints}
    valid_edges = [e for e in dep_graph["edges"]
                   if (e.get("from",{}).get("method",""), e.get("from",{}).get("path","")) in ep_keys
                   and (e.get("to",{}).get("method",""), e.get("to",{}).get("path","")) in ep_keys]
    return {"edges": valid_edges, "rule": dep_graph.get("rule", "")}
