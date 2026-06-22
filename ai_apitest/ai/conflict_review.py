from __future__ import annotations

import json
from typing import Any

from ai_apitest.ai.provider import chat_completion, extract_json_array, retry_with_json_repair


VALID_DECISIONS = {"conflict", "not_conflict", "uncertain"}


def review_conflicts_with_llm(conflicts: list[dict[str, Any]], llm: dict) -> list[dict[str, Any]]:
    if not conflicts:
        return []
    payload = [conflict_payload(item) for item in conflicts]
    content = chat_completion(
        base_url=llm.get("base_url", ""),
        model=llm.get("model", ""),
        api_key_env=llm.get("api_key_env", ""),
        provider=llm.get("provider", "openai-compatible"),
        messages=[
            {
                "role": "system",
                "content": (
                    "你是接口测试上下文经验的冲突复核助手。"
                    "判断两条经验是否真的语义冲突，只输出 JSON 数组，不要输出解释性正文。"
                ),
            },
            {
                "role": "user",
                "content": build_review_prompt(payload),
            },
        ],
        timeout=None,
    )
    return parse_conflict_review_response(
        content,
        base_url=llm.get("base_url", ""),
        model=llm.get("model", ""),
        api_key_env=llm.get("api_key_env", ""),
        provider=llm.get("provider", "openai-compatible"),
    )


def conflict_payload(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "relation_id": item.get("relation_id"),
        "interface": {
            "method": item.get("method") or "",
            "path": item.get("path") or "",
            "environment": item.get("environment") or "",
        },
        "a": {
            "title": item.get("a_title") or "",
            "content": item.get("a_content") or "",
            "confidence": item.get("a_confidence") or "",
            "source": item.get("a_source") or "",
        },
        "b": {
            "title": item.get("b_title") or "",
            "content": item.get("b_content") or "",
            "confidence": item.get("b_confidence") or "",
            "source": item.get("b_source") or "",
        },
    }


def build_review_prompt(conflicts: list[dict[str, Any]]) -> str:
    return (
        "请复核下面由规则引擎发现的疑似冲突经验。\n"
        "判定标准：\n"
        "1. 如果只是表达方式不同、标题不同但业务含义一致，decision 设为 not_conflict。\n"
        "2. 如果状态码区间、布尔结论、存在性结论、断言预期互相排斥，decision 设为 conflict。\n"
        "3. 如果缺少业务上下文无法确认，decision 设为 uncertain。\n"
        "4. 只输出 JSON 数组，每项格式为："
        "[{\"relation_id\":123,\"decision\":\"conflict|not_conflict|uncertain\",\"reason\":\"简短原因\"}]\n\n"
        f"疑似冲突列表：\n{json.dumps(conflicts, ensure_ascii=False, indent=2)}"
    )


def parse_conflict_review_response(
    content: str,
    *,
    base_url: str = "",
    model: str = "",
    api_key_env: str = "",
    provider: str = "openai-compatible",
) -> list[dict[str, Any]]:
    def _parse(text: str) -> list[dict[str, Any]]:
        return extract_json_array(text)

    if base_url and model and api_key_env:
        try:
            data, _warning = retry_with_json_repair(
                content, parse_json=_parse,
                base_url=base_url, model=model, api_key_env=api_key_env, provider=provider,
            )
        except ValueError:
            data = _parse(content)  # last-resort fallback
    else:
        data = _parse(content)

    reviews: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            relation_id = int(item.get("relation_id"))
        except (TypeError, ValueError):
            continue
        decision = str(item.get("decision", "")).strip()
        if decision not in VALID_DECISIONS:
            decision = "uncertain"
        reviews.append(
            {
                "relation_id": relation_id,
                "decision": decision,
                "reason": str(item.get("reason", "")).strip(),
            }
        )
    return reviews
