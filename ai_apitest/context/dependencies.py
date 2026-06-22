from __future__ import annotations

import json
import re
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import httpx

from ai_apitest.context.storage import audit, context_connection, dumps, loads_json, now_iso, upsert_lesson
from ai_apitest.core.models import Endpoint

SCHEMA_WORDS = {
    "application",
    "content",
    "description",
    "example",
    "examples",
    "format",
    "items",
    "json",
    "nullable",
    "properties",
    "requestBody",
    "required",
    "schema",
    "type",
}


def infer_dependency_candidates(root: Path, project: str, endpoints: list[Endpoint]) -> dict[str, int]:
    producers = [(endpoint, response_fields(endpoint)) for endpoint in endpoints]
    created = 0
    merged = 0
    for consumer in endpoints:
        needs = consumer_parameter_names(consumer)
        if not needs:
            continue
        for producer, fields in producers:
            if producer is consumer:
                continue
            shared = sorted(needs.intersection(fields))
            if not shared:
                continue
            result = upsert_lesson(
                root,
                project,
                {
                    "scope": "dependency",
                    "method": consumer.method,
                    "path": consumer.path,
                    "lesson_type": "data_dependency",
                    "lesson_subtype": "inferred_by_rule",
                    "title": f"{consumer.method} {consumer.path} 可能依赖 {producer.method} {producer.path}",
                    "content": dependency_candidate_content(producer, consumer, shared),
                    "source": f"{producer.method} {producer.path} -> {consumer.method} {consumer.path}",
                    "source_type": "inferred_by_rule",
                    "confidence": "medium",
                    "tags": ["data_dependency", "candidate"],
                    "lifecycle_status": "active",
                    "review_status": "unreviewed",
                    "verification_status": "candidate",
                    "metadata": {
                        "dependency_from": {"method": producer.method, "path": producer.path, "extract": {name: f"$.{name}" for name in shared}},
                        "dependency_to": {"method": consumer.method, "path": consumer.path, "needs": shared},
                    },
                },
                actor="rule:dependency",
            )
            if result.action == "duplicate_merged":
                merged += 1
            else:
                created += 1
    return {"created": created, "merged": merged}


def dependency_candidate_content(producer: Endpoint, consumer: Endpoint, shared: list[str]) -> str:
    fields = "、".join(shared)
    usage = dependency_usage_description(consumer, shared)
    return (
        f"数据来源：{producer.method} {producer.path} 的响应字段 {fields}。"
        f"使用位置：{consumer.method} {consumer.path} 的{usage}。"
        "当前状态为候选依赖，需人工确认并执行验证通过后，才会用于默认链路用例。"
    )


def dependency_usage_description(endpoint: Endpoint, names: list[str]) -> str:
    path_params = set(re.findall(r"{([^}]+)}", endpoint.path))
    declared = {str(item.get("name", "")): str(item.get("in", "") or "参数") for item in endpoint.parameters if item.get("name")}
    parts = []
    for name in names:
        if name in path_params:
            parts.append(f"路径参数 {name}")
        elif declared.get(name):
            parts.append(f"{declared[name]} 参数 {name}")
        else:
            parts.append(f"请求参数 {name}")
    return "、".join(parts)


def response_fields(endpoint: Endpoint) -> set[str]:
    fields: set[str] = set()
    payload = json.dumps(endpoint.responses, ensure_ascii=False)
    fields.update(re.findall(r'"([a-zA-Z_][a-zA-Z0-9_]*)"\s*:', payload))
    for token in {"id", "email", "token", "access_token", "product_id", "order_id"}:
        if token in payload:
            fields.add(token)
    return normalize_names(fields)


def consumer_parameter_names(endpoint: Endpoint) -> set[str]:
    names = {str(item.get("name", "")) for item in endpoint.parameters if item.get("name")}
    names.update(re.findall(r"{([^}]+)}", endpoint.path))
    body = endpoint.request_body if isinstance(endpoint.request_body, dict) else {}
    names.update(body_fields(body))
    return normalize_names(names)


def body_fields(schema: dict[str, Any]) -> set[str]:
    payload = json.dumps(schema, ensure_ascii=False)
    return normalize_names(set(re.findall(r'"([a-zA-Z_][a-zA-Z0-9_]*)"\s*:', payload)))


def normalize_names(names: set[str]) -> set[str]:
    return {name for name in names if name and name not in SCHEMA_WORDS and not name.startswith("$")}


def verify_dependency_candidates(
    root: Path,
    project: str,
    environment: dict[str, Any],
    config: dict[str, Any],
    client: httpx.Client | None = None,
) -> dict[str, int]:
    policy = config.get("context", {}).get("dependency_verification", {})
    rows = candidate_dependency_rows(root, project)
    summary = {"verified": 0, "failed": 0, "skipped": 0, "invalidated": 0}
    owned_client = client is None
    if client is None:
        timeout = float(config.get("execution", {}).get("timeout_seconds", 20))
        client = httpx.Client(timeout=timeout)
    try:
        for lesson in rows:
            result = verify_one_dependency(root, project, lesson, environment, policy, client)
            summary[result] = summary.get(result, 0) + 1
    finally:
        if owned_client:
            client.close()
    return summary


def candidate_dependency_rows(root: Path, project: str) -> list[dict[str, Any]]:
    with context_connection(root, project) as conn:
        rows = conn.execute(
            """
            select *
            from lessons
            where project = ?
              and lesson_type = 'data_dependency'
              and verification_status = 'candidate'
              and lifecycle_status = 'active'
            order by updated_at desc
            """,
            (project,),
        ).fetchall()
        items = []
        for row in rows:
            data = dict(row)
            data["metadata"] = loads_json(data.get("metadata"), {})
            items.append(data)
        return items


def verify_one_dependency(
    root: Path,
    project: str,
    lesson: dict[str, Any],
    environment: dict[str, Any],
    policy: dict[str, Any],
    client: httpx.Client,
) -> str:
    metadata = dict(lesson.get("metadata") or {})
    producer = metadata.get("dependency_from") or {}
    consumer = metadata.get("dependency_to") or {}
    producer_method = str(producer.get("method") or "GET").upper()
    producer_path = str(producer.get("path") or "")
    consumer_method = str(consumer.get("method") or lesson.get("method") or "GET").upper()
    consumer_path = str(consumer.get("path") or lesson.get("path") or "")
    allowed, reason = dependency_policy_allows(policy, lesson, producer_method, producer_path, consumer_method, consumer_path)
    if not allowed:
        update_dependency_status(root, project, lesson["id"], "candidate", "verification_skipped_by_policy", reason)
        return "skipped"
    try:
        base_url = str(environment.get("base_url") or "").rstrip("/")
        if not base_url:
            raise ValueError("环境未配置 base_url")
        producer_response = client.request(producer_method, f"{base_url}{producer_path}", headers=environment.get("headers", {}))
        if producer_response.status_code >= 400:
            raise ValueError(f"生产者接口返回 HTTP {producer_response.status_code}")
        values = extract_dependency_values(producer_response, producer.get("extract") or {})
        if not values:
            raise ValueError("生产者响应中未提取到依赖数据")
        target_url, kwargs = build_consumer_request(base_url, consumer_method, consumer_path, values, environment)
        consumer_response = client.request(consumer_method, target_url, **kwargs)
        if consumer_response.status_code >= 400:
            update_dependency_status(root, project, lesson["id"], "invalidated", "verification_failed", f"消费者接口返回 HTTP {consumer_response.status_code}")
            return "invalidated"
    except Exception as exc:
        update_dependency_status(root, project, lesson["id"], "candidate", "verification_failed", str(exc))
        return "failed"
    update_dependency_status(root, project, lesson["id"], "verified", "verified_safe", "链路依赖验证通过")
    return "verified"


def dependency_policy_allows(
    policy: dict[str, Any],
    lesson: dict[str, Any],
    producer_method: str,
    producer_path: str,
    consumer_method: str,
    consumer_path: str,
) -> tuple[bool, str]:
    deny_methods = {str(item).upper() for item in policy.get("deny_methods", ["DELETE"])}
    allow_methods = {str(item).upper() for item in policy.get("allow_methods", ["GET", "POST"])}
    if producer_method in deny_methods or consumer_method in deny_methods:
        return False, "请求方法被 deny_methods 阻止"
    if allow_methods and (producer_method not in allow_methods or consumer_method not in allow_methods):
        return False, "请求方法不在 allow_methods 内"
    blocked = [str(item) for item in policy.get("blocked_paths", [])]
    if any(path_matches(producer_path, item) or path_matches(consumer_path, item) for item in blocked):
        return False, "路径被 blocked_paths 阻止"
    allowed_paths = [str(item) for item in policy.get("allowed_paths", [])]
    if allowed_paths and not any(path_matches(producer_path, item) for item in allowed_paths):
        return False, "生产者路径不在 allowed_paths 内"
    if allowed_paths and not any(path_matches(consumer_path, item) for item in allowed_paths):
        return False, "消费者路径不在 allowed_paths 内"
    if bool(policy.get("requires_confirmation", True)) and lesson.get("review_status") != "confirmed":
        return False, "依赖候选尚未人工确认"
    return True, ""


def path_matches(path: str, pattern: str) -> bool:
    return fnmatch(path, pattern.replace("{", "*").replace("}", "*"))


def extract_dependency_values(response: httpx.Response, extract: dict[str, str]) -> dict[str, Any]:
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return {}
    values = {}
    for name, selector in extract.items():
        value = simple_json_path(payload, str(selector))
        if value not in (None, ""):
            values[str(name)] = value
    return values


def simple_json_path(payload: Any, selector: str) -> Any:
    if not selector.startswith("$."):
        return None
    current = payload
    for part in selector[2:].split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def build_consumer_request(
    base_url: str,
    method: str,
    path: str,
    values: dict[str, Any],
    environment: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    rendered_path = path
    remaining = dict(values)
    for name, value in values.items():
        token = "{" + name + "}"
        if token in rendered_path:
            rendered_path = rendered_path.replace(token, str(value))
            remaining.pop(name, None)
    kwargs: dict[str, Any] = {"headers": environment.get("headers", {})}
    if method in {"POST", "PUT", "PATCH"} and remaining:
        kwargs["json"] = remaining
    elif remaining:
        kwargs["params"] = remaining
    return f"{base_url}{rendered_path}", kwargs


def update_dependency_status(root: Path, project: str, lesson_id: str, verification_status: str, result: str, reason: str) -> None:
    timestamp = now_iso()
    with context_connection(root, project) as conn:
        row = conn.execute("select metadata from lessons where project = ? and id = ?", (project, lesson_id)).fetchone()
        metadata = loads_json(row["metadata"], {}) if row else {}
        attempts = int(metadata.get("verification_attempts") or 0) + 1
        metadata["verification_attempts"] = attempts
        metadata["last_verification_result"] = result
        metadata["last_verification_reason"] = reason
        conn.execute(
            """
            update lessons
            set verification_status = ?,
                review_status = case when ? = 'invalidated' then 'needs_review' else review_status end,
                metadata = ?,
                revision = revision + 1,
                updated_at = ?,
                updated_by = 'system'
            where project = ? and id = ?
            """,
            (verification_status, verification_status, dumps(metadata), timestamp, project, lesson_id),
        )
        audit(conn, "dependency_" + result, "lesson", lesson_id, project, "system", {"reason": reason, "attempts": attempts})
