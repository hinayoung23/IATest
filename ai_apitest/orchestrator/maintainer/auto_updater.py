from __future__ import annotations

import json

from ai_apitest.ai.case_generation import validate_cases
from ai_apitest.ai.prompting import render_prompt
from ai_apitest.ai.provider import chat_completion, extract_json_array, retry_with_json_repair
from ai_apitest.core.models import Endpoint, TestCase
from ai_apitest.core.serde import to_jsonable
from ai_apitest.orchestrator.maintainer.change_detector import endpoint_key


def impacted_cases(cases: list[TestCase], changed_endpoint_keys: list[str]) -> list[TestCase]:
    changed = set(changed_endpoint_keys)
    return [case for case in cases if f"{case.method.upper()} {case.path}" in changed or any(f"{step.method.upper()} {step.path}" in changed for step in case.steps)]


def impacted_endpoint_keys(old: list[Endpoint], new: list[Endpoint]) -> list[str]:
    old_keys = {endpoint_key(item) for item in old}
    new_keys = {endpoint_key(item) for item in new}
    return sorted(old_keys.symmetric_difference(new_keys))


def auto_update_cases_with_llm(
    *,
    impacted: list[TestCase],
    endpoints: list[Endpoint],
    diff: dict,
    llm: dict,
) -> list[TestCase]:
    if not impacted:
        return []
    content = chat_completion(
        base_url=llm.get("base_url", ""),
        model=llm.get("model", ""),
        api_key_env=llm.get("api_key_env", ""),
        provider=llm.get("provider", "openai-compatible"),
        messages=[
            {
                "role": "user",
                "content": render_prompt(
                    "case_maintenance.md",
                    {
                        "DIFF_JSON": json.dumps(diff, ensure_ascii=False, indent=2),
                        "CASES_JSON": json.dumps(to_jsonable(impacted), ensure_ascii=False, indent=2),
                    },
                ),
            }
        ],
    )
    try:
        cases, _warning = retry_with_json_repair(
            content, parse_json=extract_json_array,
            base_url=llm.get("base_url", ""), model=llm.get("model", ""),
            api_key_env=llm.get("api_key_env", ""), provider=llm.get("provider", "openai-compatible"),
            description="（用例维护）",
        )
        return validate_cases(cases, endpoints)
    except ValueError:
        # Repair exhausted — let caller decide fallback; don't re-parse broken content
        raise ValueError("大模型输出格式异常，用例维护自动修复失败。") from None
