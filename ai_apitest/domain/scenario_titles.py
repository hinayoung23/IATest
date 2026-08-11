from __future__ import annotations

import re
from typing import Any, Iterable

from ai_apitest.core.models import Endpoint


GENERIC_CHAIN_TITLES = {
    "接口依赖主链路",
    "接口数据准备与消费主链路",
    "接口主链路",
    "依赖链路",
    "链路主流程",
    "主链路",
}


def is_generic_chain_title(value: str) -> bool:
    normalized = "".join(str(value or "").split()).strip("-_—:：")
    return not normalized or normalized in GENERIC_CHAIN_TITLES


def dependency_chain_title(steps: Iterable[dict[str, Any]], endpoints: Iterable[Endpoint]) -> str:
    endpoint_by_key = {(endpoint.method.upper(), endpoint.path): endpoint for endpoint in endpoints}
    labels: list[str] = []
    action_counts: dict[str, int] = {}
    for raw in steps:
        method = str(raw.get("method") or "").upper()
        path = str(raw.get("path") or "")
        endpoint = endpoint_by_key.get((method, path))
        action = endpoint_action(endpoint, method, path)
        action_counts[action] = action_counts.get(action, 0) + 1
        label = observable_step_label(
            raw,
            action,
            repeated=action_counts[action] > 1,
        )
        if label and (not labels or labels[-1] != label):
            labels.append(label)
    if not labels:
        return "依赖链路"
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]}后{labels[1]}"
    if labels[0] == "生成邮箱" and labels[1] in {"删除邮箱", "续期邮箱"}:
        prefix = f"生成并{labels[1]}"
        suffix = "且".join(labels[2:])
        return f"{prefix}后{suffix}"
    return f"{'、'.join(labels[:-1])}后{labels[-1]}"


def ensure_dependency_chain_title(
    title: str,
    steps: Iterable[dict[str, Any]],
    endpoints: Iterable[Endpoint],
) -> str:
    step_items = [dict(item) for item in steps]
    endpoint_items = list(endpoints)
    current = str(title or "").strip()
    generated = dependency_chain_title(step_items, endpoint_items)
    if is_generic_chain_title(current) or chain_title_needs_rebuild(
        current,
        step_items,
        endpoint_items,
    ):
        return generated
    return current


def concise_case_intent(value: str, *, max_chars: int = 60) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= max_chars:
        return text
    first_clause = re.split(r"[；。]\s*", text, maxsplit=1)[0].strip("；。 ")
    if first_clause and len(first_clause) <= max_chars:
        return first_clause
    return text[: max_chars - 1].rstrip("，、；：: ") + "…"


def chain_title_needs_rebuild(
    title: str,
    steps: list[dict[str, Any]],
    endpoints: list[Endpoint],
) -> bool:
    if len(title) > 72 or not steps:
        return True
    endpoint_by_key = {(item.method.upper(), item.path): item for item in endpoints}
    last = steps[-1]
    method = str(last.get("method") or "").upper()
    path = str(last.get("path") or "")
    action = endpoint_action(endpoint_by_key.get((method, path)), method, path)
    keyword = action.replace("邮箱", "")
    if keyword and keyword not in title:
        return True
    final_status = assertion_status(last)
    if isinstance(final_status, int) and final_status >= 400 and str(final_status) not in title:
        return True
    same_action_count = sum(
        endpoint_action(
            endpoint_by_key.get(
                (str(item.get("method") or "").upper(), str(item.get("path") or ""))
            ),
            str(item.get("method") or "").upper(),
            str(item.get("path") or ""),
        )
        == action
        for item in steps
    )
    if (
        action == "生成邮箱"
        and same_action_count > 1
        and any(
            isinstance(item, dict)
            and item.get("target") in {"$.created_at", "$.expires_in", "$.email"}
            and item.get("operator") in {"eq", "gte"}
            for item in (
                last.get("assertions")
                if isinstance(last.get("assertions"), list)
                else []
            )
        )
        and "刷新" not in title
    ):
        return True
    return same_action_count > 1 and not re.search(r"再次|重复|重新", title)


def endpoint_action(endpoint: Endpoint | None, method: str, path: str) -> str:
    text = f"{endpoint.summary if endpoint else ''} {path}".lower()
    if "generate" in text or "生成" in text or "创建" in text:
        return "生成邮箱"
    if "delete" in text or "删除" in text:
        return "删除邮箱"
    if "extend" in text or "续期" in text:
        return "续期邮箱"
    if "message" in text or "邮件" in text:
        return "查询邮件"
    if "check" in text or "状态" in text:
        return "检查邮箱状态"
    return str(endpoint.summary if endpoint else f"{method} {path}").strip()


def observable_step_label(raw: dict[str, Any], action: str, *, repeated: bool) -> str:
    label = f"再次{action}" if repeated else action
    assertions = raw.get("assertions") if isinstance(raw.get("assertions"), list) else []
    status = assertion_status(raw)
    if repeated and action == "生成邮箱" and any(
        isinstance(item, dict)
        and item.get("target") in {"$.created_at", "$.expires_in", "$.email"}
        and item.get("operator") in {"eq", "gte"}
        for item in assertions
    ):
        return "再次生成邮箱并刷新有效期"
    if action == "查询邮件" and any(
        isinstance(item, dict)
        and item.get("target") == "$"
        and item.get("operator") == "length_eq"
        and item.get("expected") == 0
        for item in assertions
    ):
        return "查询邮件为空"
    if action == "检查邮箱状态":
        valid = next(
            (
                item.get("expected")
                for item in assertions
                if isinstance(item, dict)
                and item.get("target") == "$.valid"
                and item.get("operator") == "eq"
                and isinstance(item.get("expected"), bool)
            ),
            None,
        )
        if isinstance(valid, bool):
            return f"检查邮箱状态{'有效' if valid else '无效'}"
    if isinstance(status, int) and status >= 400:
        return f"{label}返回{status}"
    return label


def assertion_status(raw: dict[str, Any]) -> int | None:
    assertions = raw.get("assertions") if isinstance(raw.get("assertions"), list) else []
    return next(
        (
            item.get("expected")
            for item in assertions
            if isinstance(item, dict)
            and item.get("type") == "status_code"
            and item.get("operator", "eq") == "eq"
            and isinstance(item.get("expected"), int)
        ),
        None,
    )
