from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_apitest.context.storage import context_connection, loads_json, save_context_pack
from ai_apitest.core.models import Endpoint
from ai_apitest.core.paths import safe_project_name


@dataclass(slots=True)
class ContextBundle:
    text: str
    pack_id: str
    included_lesson_ids: list[str]
    excluded_lesson_ids: list[dict[str, str]]
    used_tokens: int
    context_budget: int


def estimate_tokens(text: str) -> int:
    ascii_chars = sum(1 for char in text if ord(char) < 128)
    non_ascii_chars = len(text) - ascii_chars
    return max(1, ascii_chars // 4 + non_ascii_chars // 2)


def calculate_injection_budget(
    model_config: dict[str, Any],
    context_config: dict[str, Any],
    task_type: str,
    system_prompt: str,
    user_prompt_base: str,
) -> int:
    default_budget = {"max_context_ratio": 0.20, "max_context_tokens": 12000, "reserve_output_tokens": 8000}
    budget_config = context_budget_config(context_config, task_type, default_budget)
    context_window = int(model_config.get("context_window") or 128000)
    reserve_output = int(budget_config.get("reserve_output_tokens") or model_config.get("default_output_tokens") or 8000)
    safety_margin = 1000
    base_tokens = estimate_tokens(system_prompt) + estimate_tokens(user_prompt_base)
    available = max(0, context_window - base_tokens - reserve_output - safety_margin)
    by_ratio = int(available * float(budget_config.get("max_context_ratio", 0.20)))
    return max(0, min(by_ratio, int(budget_config.get("max_context_tokens", 12000))))


def build_context_bundle(
    root: Path,
    project: str,
    *,
    task_type: str,
    model_config: dict[str, Any],
    context_config: dict[str, Any],
    system_prompt: str,
    user_prompt_base: str,
    endpoints: list[Endpoint] | None = None,
    environment: str = "",
) -> ContextBundle:
    budget = calculate_injection_budget(model_config, context_config, task_type, system_prompt, user_prompt_base)
    max_candidates = max(100, min(10000, int(context_config.get("injection", {}).get("max_candidates", 2000))))
    candidates = aggregate_prompt_candidates(
        retrieve_candidates(root, project, endpoints or [], environment=environment, limit=max_candidates)
    )
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    used = 0
    for lesson in candidates:
        reason = exclusion_reason(lesson)
        if reason:
            for lesson_id in lesson_ids(lesson):
                excluded.append({"id": lesson_id, "reason": reason})
            continue
        text = lesson_prompt_text(lesson, compact=False)
        tokens = estimate_tokens(text)
        if used + tokens <= budget:
            included.append({**lesson, "_prompt_text": text})
            used += tokens
            continue
        compact_text = lesson_prompt_text(lesson, compact=True)
        compact_tokens = estimate_tokens(compact_text)
        if used + compact_tokens <= budget:
            included.append({**lesson, "_prompt_text": compact_text})
            used += compact_tokens
        else:
            for lesson_id in lesson_ids(lesson):
                excluded.append({"id": lesson_id, "reason": "over_budget"})
    block = render_context_block(included)
    included_ids = flatten_lesson_ids(included)
    pack_id = save_context_pack(
        root,
        project,
        {
            "task_id": f"{task_type}-{safe_project_name(project)}",
            "task_type": task_type,
            "model": str(model_config.get("model", "")),
            "model_context_window": int(model_config.get("context_window") or 128000),
            "context_budget": budget,
            "used_tokens": used,
            "reserve_output_tokens": int(
                context_budget_config(
                    context_config,
                    task_type,
                    {"reserve_output_tokens": 8000},
                ).get("reserve_output_tokens")
                or model_config.get("default_output_tokens")
                or 8000
            ),
            "included_lesson_ids": included_ids,
            "excluded_lesson_ids": excluded,
            "final_prompt_hash": "sha256:" + hashlib.sha256(block.encode("utf-8")).hexdigest(),
        },
    )
    return ContextBundle(block, pack_id, included_ids, excluded, used, budget)


def context_budget_config(
    context_config: dict[str, Any],
    task_type: str,
    default: dict[str, Any],
) -> dict[str, Any]:
    value = (
        context_config.get("injection", {})
        .get("budgets", {})
        .get(task_type)
    )
    return value if isinstance(value, dict) else default


def aggregate_prompt_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, tuple[str, ...], str, str, str], list[dict[str, Any]]] = {}
    order: list[tuple[str, str, tuple[str, ...], str, str, str]] = []
    passthrough: list[tuple[int, dict[str, Any]]] = []
    positions: dict[tuple[str, str, tuple[str, ...], str, str, str], int] = {}
    for index, lesson in enumerate(candidates):
        key = dependency_group_key(lesson)
        if not key:
            passthrough.append((index, lesson))
            continue
        if key not in groups:
            groups[key] = []
            order.append(key)
            positions[key] = index
        groups[key].append(lesson)
    aggregated: list[tuple[int, dict[str, Any]]] = list(passthrough)
    for key in order:
        items = groups[key]
        item = aggregate_dependency_group(key, items) if len(items) > 1 else items[0]
        aggregated.append((positions[key], item))
    return [item for _, item in sorted(aggregated, key=lambda pair: pair[0])]


def dependency_group_key(lesson: dict[str, Any]) -> tuple[str, str, tuple[str, ...], str, str, str] | None:
    if lesson.get("lesson_type") != "data_dependency":
        return None
    metadata = loads_json(lesson.get("metadata"), {}) if isinstance(lesson.get("metadata"), str) else dict(lesson.get("metadata") or {})
    producer = metadata.get("dependency_from") or {}
    consumer = metadata.get("dependency_to") or {}
    producer_method = str(producer.get("method") or "")
    producer_path = str(producer.get("path") or "")
    fields = tuple(sorted(str(item) for item in (consumer.get("needs") or (producer.get("extract") or {}).keys())))
    if not producer_method or not producer_path or not fields:
        return None
    return (
        producer_method,
        producer_path,
        fields,
        str(lesson.get("verification_status") or ""),
        str(lesson.get("review_status") or ""),
        str(lesson.get("environment") or ""),
    )


def aggregate_dependency_group(key: tuple[str, str, tuple[str, ...], str, str, str], lessons: list[dict[str, Any]]) -> dict[str, Any]:
    producer_method, producer_path, fields, verification_status, review_status, environment = key
    consumers = []
    ids = []
    for lesson in lessons:
        metadata = loads_json(lesson.get("metadata"), {}) if isinstance(lesson.get("metadata"), str) else dict(lesson.get("metadata") or {})
        consumer = metadata.get("dependency_to") or {}
        method = str(consumer.get("method") or lesson.get("method") or "")
        path = str(consumer.get("path") or lesson.get("path") or "")
        needs = [str(item) for item in (consumer.get("needs") or fields)]
        consumers.append(f"{method} {path} 使用 {', '.join(needs)}")
        ids.extend(lesson_ids(lesson))
    digest = hashlib.md5("|".join(sorted(ids)).encode("utf-8")).hexdigest()[:12]
    field_text = "、".join(fields)
    status_text = dependency_status_text(verification_status)
    first = lessons[0]
    return {
        **first,
        "id": f"depgrp-{digest}",
        "_lesson_ids": ids,
        "_group_items": lessons,
        "scope": "dependency",
        "method": producer_method,
        "path": producer_path,
        "title": f"{producer_method} {producer_path} 产出的 {field_text} 被 {len(consumers)} 个接口使用",
        "content": f"数据来源：{producer_method} {producer_path} 的响应字段 {field_text}。使用接口：{'；'.join(consumers)}。当前状态：{status_text}。",
        "verification_status": verification_status,
        "review_status": review_status,
        "environment": environment,
    }


def dependency_status_text(value: str) -> str:
    return {
        "candidate": "候选依赖，需要人工确认并执行验证后才能作为默认链路依据",
        "verified": "已验证依赖，可以作为链路用例的数据传递依据",
        "invalidated": "依赖已失效，需要重新识别或复核",
    }.get(value, value or "未验证")


def lesson_ids(lesson: dict[str, Any]) -> list[str]:
    ids = lesson.get("_lesson_ids")
    if isinstance(ids, list) and ids:
        return [str(item) for item in ids]
    return [str(lesson["id"])]


def flatten_lesson_ids(lessons: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for lesson in lessons:
        for lesson_id in lesson_ids(lesson):
            if lesson_id not in seen:
                seen.add(lesson_id)
                result.append(lesson_id)
    return result


def retrieve_candidates(
    root: Path,
    project: str,
    endpoints: list[Endpoint],
    environment: str = "",
    *,
    limit: int = 2000,
) -> list[dict[str, Any]]:
    safe_project = safe_project_name(project)
    endpoint_keys = {(endpoint.method, endpoint.path) for endpoint in endpoints}
    path_prefixes = {endpoint.path.split("/", 3)[1] for endpoint in endpoints if endpoint.path.startswith("/") and len(endpoint.path.split("/")) > 1}
    with context_connection(root, safe_project) as conn:
        conn.execute("create temp table if not exists requested_endpoints(method text, path text, primary key(method, path))")
        conn.execute("delete from requested_endpoints")
        conn.executemany(
            "insert or ignore into requested_endpoints(method, path) values (?, ?)",
            sorted(endpoint_keys),
        )
        conn.execute("create temp table if not exists requested_modules(module text primary key)")
        conn.execute("delete from requested_modules")
        conn.executemany(
            "insert or ignore into requested_modules(module) values (?)",
            [(prefix,) for prefix in sorted(path_prefixes)],
        )
        relevance = ""
        if endpoints:
            relevance = """
              and (
                    l.scope in ('project', 'report')
                    or exists(
                        select 1 from requested_endpoints e
                        where e.method = l.method and e.path = l.path
                    )
                    or exists(
                        select 1 from requested_modules m
                        where l.path = '/' || m.module or l.path like '/' || m.module || '/%'
                    )
              )
            """
        rows = conn.execute(
            f"""
            select l.*,
                   exists(select 1 from lesson_relations r where r.relation_type = 'conflict' and (r.lesson_id = l.id or r.related_lesson_id = l.id)) as has_conflict
            from lessons l
            where l.project = ?
              and l.lifecycle_status in ('active', 'reopened')
              and l.review_status = 'confirmed'
              and l.confidence != 'low'
              and (l.lesson_type != 'data_dependency' or l.verification_status = 'verified')
              {relevance}
            order by
              case
                when exists(
                    select 1 from requested_endpoints e
                    where e.method = l.method and e.path = l.path
                ) then 0
                when exists(
                    select 1 from requested_modules m
                    where l.path = '/' || m.module or l.path like '/' || m.module || '/%'
                ) then 1
                when l.scope in ('project', 'report') then 2
                else 3
              end,
              l.updated_at desc
            limit ?
            """,
            (safe_project, max(1, int(limit))),
        ).fetchall()
    lessons = [row_to_candidate(row, endpoint_keys, path_prefixes, environment) for row in rows]
    return sorted(lessons, key=rank_key)


def row_to_candidate(row: Any, endpoint_keys: set[tuple[str, str]], path_prefixes: set[str], environment: str = "") -> dict[str, Any]:
    data = dict(row)
    data["tags"] = loads_json(data.get("tags"), [])
    data["_exact_endpoint"] = (data.get("method", ""), data.get("path", "")) in endpoint_keys
    prefix = str(data.get("path") or "").split("/", 3)[1] if str(data.get("path") or "").startswith("/") and len(str(data.get("path") or "").split("/")) > 1 else ""
    data["_same_module"] = bool(prefix and prefix in path_prefixes)
    lesson_env = str(data.get("environment") or "")
    if environment and lesson_env == environment:
        data["_environment_rank"] = 0
        data["_environment_strength"] = "强约束"
    elif not lesson_env:
        data["_environment_rank"] = 1
        data["_environment_strength"] = "通用约束"
    else:
        data["_environment_rank"] = 2
        data["_environment_strength"] = "跨环境参考"
    return data


def rank_key(lesson: dict[str, Any]) -> tuple[int, int, int, int, int, float, int]:
    exact = 0 if lesson.get("_exact_endpoint") else 1
    same_module = 0 if lesson.get("_same_module") else 1
    project_scope = 0 if lesson.get("scope") == "project" else 1
    environment_rank = int(lesson.get("_environment_rank", 1))
    confirmed_verified = 0 if lesson.get("review_status") == "confirmed" and lesson.get("verification_status") == "verified" else 1
    quality = -float(lesson.get("quality_score") or 0)
    occurrences = -int(lesson.get("occurrence_count") or 0)
    return (exact, same_module, environment_rank, project_scope, confirmed_verified, quality, occurrences)


def exclusion_reason(lesson: dict[str, Any]) -> str:
    if lesson.get("lifecycle_status") == "pending":
        return "lifecycle_status_pending"
    if lesson.get("confidence") == "low":
        return "confidence_low"
    if lesson.get("review_status") != "confirmed":
        return f"review_status_{lesson.get('review_status')}"
    if lesson.get("lesson_type") == "data_dependency" and lesson.get("verification_status") != "verified":
        return f"dependency_{lesson.get('verification_status') or 'unverified'}"
    if lesson.get("lifecycle_status") in {"archived", "merged", "ignored"}:
        return f"lifecycle_status_{lesson.get('lifecycle_status')}"
    if lesson.get("has_conflict"):
        return "active_conflict"
    return ""


def lesson_prompt_text(lesson: dict[str, Any], *, compact: bool) -> str:
    scope = lesson.get("scope", "")
    endpoint = f"{lesson.get('method', '')} {lesson.get('path', '')}".strip()
    title = lesson.get("title", "")
    if compact:
        return f"- [{lesson['id']}] {scope} {endpoint}: {title}".strip()
    env = lesson.get("environment") or "通用环境"
    strength = lesson.get("_environment_strength") or "通用约束"
    return f"- [{lesson['id']}] {scope} {endpoint} ({env}, {strength}, {lesson.get('confidence', 'medium')}): {title}。{lesson.get('content', '')}".strip()


def render_context_block(lessons: list[dict[str, Any]]) -> str:
    if not lessons:
        return "## 项目历史上下文\n\n无可注入的已确认上下文。"
    groups = [
        ("### 项目级规则（已确认）", lambda item: item.get("scope") == "project"),
        ("### 当前接口经验", lambda item: item.get("scope") == "endpoint"),
        ("### 已知问题（当前未解决）", lambda item: item.get("lesson_type") == "known_issue"),
        ("### 历史断言问题（避免重复错误）", lambda item: item.get("lesson_type") in {"assertion_pattern", "failure_pattern"}),
        ("### 数据依赖（已验证优先）", lambda item: item.get("scope") == "dependency"),
    ]
    used_ids: set[str] = set()
    sections = ["## 项目历史上下文"]
    for title, predicate in groups:
        items = [item for item in lessons if item["id"] not in used_ids and predicate(item)]
        if not items:
            continue
        sections.append(title)
        sections.extend(item["_prompt_text"] for item in items)
        used_ids.update(item["id"] for item in items)
    rest = [item for item in lessons if item["id"] not in used_ids]
    if rest:
        sections.append("### 其它相关经验")
        sections.extend(item["_prompt_text"] for item in rest)
    sections.append(
        """
要求：
- lifecycle_status=active 且 review_status=confirmed 的经验优先于通用策略。
- 不要重复生成历史上已确认错误的断言模式。
- 如果环境不匹配，只作为参考，不作为强约束。
""".strip()
    )
    return "\n\n".join(sections)
