from __future__ import annotations

import json
import re
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import httpx

from ai_apitest.context.storage import audit, context_connection, dumps, loads_json, now_iso, upsert_lesson
from ai_apitest.core.artifact_language import artifact_text, current_artifact_language
from ai_apitest.core.models import Endpoint
from ai_apitest.core.paths import project_docs_file, project_metadata_file, safe_project_name
from ai_apitest.core.serde import endpoint_from_dict, read_json, write_json
from ai_apitest.execution.assertions import json_path
from ai_apitest.orchestrator.maintainer.change_detector import diff_endpoints

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


def synchronize_project_dependencies(
    root: Path,
    project: str,
    old_endpoints: list[Endpoint],
    new_endpoints: list[Endpoint],
    *,
    actor: str = "system",
) -> dict[str, int]:
    """Keep persisted dependency edges and dependency lessons aligned with endpoint edits."""
    safe_project = safe_project_name(project)
    changes = diff_endpoints(old_endpoints, new_endpoints)
    current_keys = {endpoint_key(endpoint) for endpoint in new_endpoints}
    changed_keys = dependency_changed_keys(old_endpoints, new_endpoints)

    metadata = read_json(project_metadata_file(root, safe_project), {})
    if not isinstance(metadata, dict):
        metadata = {}
    graph = metadata.get("dependency_graph")
    existing_edges = graph.get("edges") if isinstance(graph, dict) else []
    kept: list[dict[str, Any]] = []
    removed_edges = 0
    for raw_edge in existing_edges if isinstance(existing_edges, list) else []:
        edge = normalize_dependency_edge(raw_edge)
        if edge is None:
            removed_edges += 1
            continue
        producer_key = endpoint_ref_key(edge.get("from"))
        consumer_key = endpoint_ref_key(edge.get("to"))
        if producer_key not in current_keys or consumer_key not in current_keys:
            removed_edges += 1
            continue
        if producer_key in changed_keys or consumer_key in changed_keys:
            removed_edges += 1
            continue
        kept.append(edge)

    by_identity = {dependency_edge_identity(edge): edge for edge in kept}
    added_edges = 0
    for edge in infer_dependency_edges(new_endpoints):
        identity = dependency_edge_identity(edge)
        if identity in by_identity:
            continue
        by_identity[identity] = edge
        added_edges += 1

    metadata["dependency_graph"] = {
        "edges": list(by_identity.values()),
        "rule": (
            "依赖边必须引用当前接口；接口手动新增、编辑或删除后同步清理并按响应字段与请求参数重新推导。"
            "safe_to_prepare=true 表示生产者可用于准备临时测试数据。"
        ),
        "updated_at": now_iso(),
        "updated_by": actor,
    }
    metadata["dependency_graph_sync"] = {
        "added_endpoints": len(changes["added"]),
        "removed_endpoints": len(changes["removed"]),
        "changed_endpoints": len(changes["changed"]),
        "dependency_changed_endpoints": len(changed_keys),
        "removed_edges": removed_edges,
        "added_edges": added_edges,
    }
    write_json(project_metadata_file(root, safe_project), metadata)

    lesson_counts = reconcile_dependency_lessons(
        root,
        safe_project,
        new_endpoints,
        changed_keys=changed_keys,
        actor=actor,
    )
    candidates = infer_dependency_candidates(root, safe_project, new_endpoints)
    return {
        "removed_edges": removed_edges,
        "added_edges": added_edges,
        "invalidated_lessons": lesson_counts["invalidated"],
        "review_lessons": lesson_counts["needs_review"],
        "created_candidates": candidates["created"],
        "merged_candidates": candidates["merged"],
    }


def infer_dependency_edges(endpoints: list[Endpoint]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for producer, consumer, shared in dependency_pairs(endpoints):
        for field in shared:
            injection_target = dependency_injection_target(consumer, field)
            result.append(
                {
                    "from": {"method": producer.method.upper(), "path": producer.path},
                    "to": {"method": consumer.method.upper(), "path": consumer.path},
                    "type": "produces_consumes",
                    "produced_field": field,
                    "extract": {field: f"$.{field}"},
                    "inject": {injection_target: "{{" + field + "}}"},
                    "safe_to_prepare": True,
                    "confidence": "medium",
                    "reason": (
                        f"本地规则确认 {producer.method.upper()} {producer.path} 的响应字段 {field} "
                        f"可注入 {consumer.method.upper()} {consumer.path} 的 {injection_target}。"
                    ),
                    "source": "local_rule",
                }
            )
    return result


def normalize_dependency_edge(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    producer_key = endpoint_ref_key(value.get("from"))
    consumer_key = endpoint_ref_key(value.get("to"))
    if not producer_key or not consumer_key:
        return None
    field = str(value.get("produced_field") or "").strip()
    raw_extract = value.get("extract") if isinstance(value.get("extract"), dict) else {}
    if not field:
        field = str(raw_extract.get("field") or "").strip()
    if not field and len(raw_extract) == 1:
        field = str(next(iter(raw_extract))).strip()
    normalized = dict(value)
    if field:
        normalized["produced_field"] = field
        normalized["extract"] = {field: str(raw_extract.get(field) or f"$.{field}")}
        raw_inject = value.get("inject") if isinstance(value.get("inject"), dict) else {}
        dotted = next((str(key) for key in raw_inject if "." in str(key)), "")
        if not dotted:
            target = str(raw_inject.get("to") or "")
            section = {
                "path_parameter": "path_params",
                "query_parameter": "query",
                "request_body": "body",
            }.get(target, "path_params")
            dotted = f"{section}.{field}"
        normalized["inject"] = {dotted: "{{" + field + "}}"}
    normalized["from"] = split_endpoint_key(producer_key)
    normalized["to"] = split_endpoint_key(consumer_key)
    normalized["safe_to_prepare"] = bool(value.get("safe_to_prepare", True))
    confidence = str(value.get("confidence") or "medium")
    normalized["confidence"] = confidence if confidence in {"high", "medium", "low"} else "medium"
    return normalized


def reconcile_dependency_lessons(
    root: Path,
    project: str,
    endpoints: list[Endpoint],
    *,
    changed_keys: set[str],
    actor: str,
) -> dict[str, int]:
    current_keys = {endpoint_key(endpoint) for endpoint in endpoints}
    counts = {"invalidated": 0, "needs_review": 0}
    timestamp = now_iso()
    with context_connection(root, project) as conn:
        rows = conn.execute(
            """
            select id, metadata from lessons
            where project = ? and lesson_type = 'data_dependency'
              and lifecycle_status in ('active', 'pending', 'reopened')
            """,
            (project,),
        ).fetchall()
        for row in rows:
            metadata = loads_json(row["metadata"], {})
            producer_key = endpoint_ref_key(metadata.get("dependency_from"))
            consumer_key = endpoint_ref_key(metadata.get("dependency_to"))
            if producer_key not in current_keys or consumer_key not in current_keys:
                metadata["invalidated_reason"] = "依赖引用的接口已被删除或 method/path 已变更"
                conn.execute(
                    """
                    update lessons set lifecycle_status = 'ignored', review_status = 'needs_review',
                        verification_status = 'invalidated', metadata = ?, revision = revision + 1,
                        updated_at = ?, updated_by = ? where id = ?
                    """,
                    (dumps(metadata), timestamp, actor, row["id"]),
                )
                counts["invalidated"] += 1
            elif producer_key in changed_keys or consumer_key in changed_keys:
                metadata["invalidated_reason"] = "依赖关联接口定义已修改，需要重新确认"
                conn.execute(
                    """
                    update lessons set review_status = 'needs_review', verification_status = 'candidate',
                        metadata = ?, revision = revision + 1, updated_at = ?, updated_by = ? where id = ?
                    """,
                    (dumps(metadata), timestamp, actor, row["id"]),
                )
                counts["needs_review"] += 1
        if counts["invalidated"] or counts["needs_review"]:
            audit(conn, "endpoint_dependencies_reconciled", "dependency", project, project, actor, counts)
    return counts


def dependency_injection_target(endpoint: Endpoint, field: str) -> str:
    if field in set(re.findall(r"{([^}]+)}", endpoint.path)):
        return f"path_params.{field}"
    declared = {
        str(item.get("name") or ""): str(item.get("in") or "")
        for item in endpoint.parameters
    }
    location = declared.get(field)
    if location == "query":
        return f"query.{field}"
    if location == "header":
        return f"headers.{field}"
    return f"body.{field}"


def endpoint_key(endpoint: Endpoint) -> str:
    return f"{endpoint.method.upper()} {endpoint.path}"


def endpoint_ref_key(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    method = str(value.get("method") or "").strip().upper()
    path = str(value.get("path") or "").strip()
    return f"{method} {path}" if method and path else ""


def split_endpoint_key(value: str) -> dict[str, str]:
    method, path = value.split(" ", 1)
    return {"method": method, "path": path}


def dependency_edge_identity(edge: dict[str, Any]) -> tuple[str, str, str]:
    return (
        endpoint_ref_key(edge.get("from")),
        endpoint_ref_key(edge.get("to")),
        str(edge.get("produced_field") or ""),
    )


def dependency_changed_keys(old_endpoints: list[Endpoint], new_endpoints: list[Endpoint]) -> set[str]:
    old_by_key = {endpoint_key(endpoint): endpoint for endpoint in old_endpoints}
    new_by_key = {endpoint_key(endpoint): endpoint for endpoint in new_endpoints}
    changed: set[str] = set()
    for key in set(old_by_key).intersection(new_by_key):
        old = old_by_key[key]
        new = new_by_key[key]
        if (
            old.parameters != new.parameters
            or old.request_body != new.request_body
            or old.responses != new.responses
        ):
            changed.add(key)
    return changed


def infer_dependency_candidates(root: Path, project: str, endpoints: list[Endpoint]) -> dict[str, int]:
    created = 0
    merged = 0
    unchanged = 0
    with context_connection(root, project) as conn:
        existing = {
            (str(row["source"]), str(row["content"]))
            for row in conn.execute(
                """
                select source, content from lessons
                where project = ? and lesson_type = 'data_dependency'
                  and lifecycle_status in ('active', 'pending', 'reopened')
                """,
                (safe_project_name(project),),
            ).fetchall()
        }
    for producer, consumer, shared in dependency_pairs(endpoints):
        language = current_artifact_language()
        source = f"{producer.method} {producer.path} -> {consumer.method} {consumer.path}"
        content = dependency_candidate_content(producer, consumer, shared, language=language)
        if (source, content) in existing:
            unchanged += 1
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
                    "title": artifact_text(
                        "dependency_candidate_title",
                        language,
                        consumer=f"{consumer.method} {consumer.path}",
                        producer=f"{producer.method} {producer.path}",
                    ),
                    "content": content,
                    "source": source,
                    "source_type": "inferred_by_rule",
                    "confidence": "medium",
                    "tags": ["data_dependency", "candidate"],
                    "lifecycle_status": "active",
                    "review_status": "unreviewed",
                    "verification_status": "candidate",
                    "metadata": {
                        "artifact_language": language,
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
    return {"created": created, "merged": merged, "unchanged": unchanged}


def dependency_pairs(endpoints: list[Endpoint]) -> list[tuple[Endpoint, Endpoint, list[str]]]:
    """Index fields once instead of comparing every producer with every consumer."""
    producers_by_field: dict[str, list[Endpoint]] = {}
    for producer in endpoints:
        for field in response_fields(producer):
            producers_by_field.setdefault(field, []).append(producer)
    result: list[tuple[Endpoint, Endpoint, list[str]]] = []
    for consumer in endpoints:
        matches: dict[tuple[str, str], tuple[Endpoint, set[str]]] = {}
        for field in consumer_parameter_names(consumer):
            for producer in producers_by_field.get(field, []):
                if producer is consumer:
                    continue
                key = endpoint_key(producer)
                item = matches.setdefault(key, (producer, set()))
                item[1].add(field)
        for producer, shared in matches.values():
            result.append((producer, consumer, sorted(shared)))
    return result


def dependency_candidate_content(
    producer: Endpoint,
    consumer: Endpoint,
    shared: list[str],
    *,
    language: str | None = None,
) -> str:
    locale = language or current_artifact_language()
    return artifact_text(
        "dependency_candidate_content",
        locale,
        producer=f"{producer.method} {producer.path}",
        consumer=f"{consumer.method} {consumer.path}",
        fields=", ".join(shared),
        usage=dependency_usage_description(consumer, shared, language=locale),
    )


def dependency_usage_description(
    endpoint: Endpoint,
    names: list[str],
    *,
    language: str | None = None,
) -> str:
    locale = language or current_artifact_language()
    path_params = set(re.findall(r"{([^}]+)}", endpoint.path))
    declared = {str(item.get("name", "")): str(item.get("in", "") or "参数") for item in endpoint.parameters if item.get("name")}
    parts = []
    for name in names:
        if name in path_params:
            parts.append(artifact_text("dependency_path_parameter", locale, name=name))
        elif declared.get(name):
            parts.append(
                artifact_text(
                    "dependency_declared_parameter",
                    locale,
                    location=declared[name],
                    name=name,
                )
            )
        else:
            parts.append(artifact_text("dependency_request_parameter", locale, name=name))
    return ", ".join(parts)


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
    max_attempts = max(1, int(policy.get("max_verification_attempts", 3)))
    rows = candidate_dependency_rows(root, project, max_attempts=max_attempts)
    summary = {"verified": 0, "failed": 0, "skipped": 0, "invalidated": 0}
    owned_client = client is None
    if client is None:
        timeout = float(config.get("execution", {}).get("timeout_seconds", 20))
        client = httpx.Client(timeout=timeout)
    endpoints = [
        endpoint_from_dict(item)
        for item in read_json(project_docs_file(root, project), [])
        if isinstance(item, dict)
    ]
    try:
        for lesson in rows:
            result = verify_one_dependency(root, project, lesson, environment, policy, client, endpoints)
            summary[result] = summary.get(result, 0) + 1
    finally:
        if owned_client:
            client.close()
    return summary


def candidate_dependency_rows(root: Path, project: str, *, max_attempts: int = 3) -> list[dict[str, Any]]:
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
            if int(data["metadata"].get("verification_attempts") or 0) < max_attempts:
                items.append(data)
        return items


def verify_one_dependency(
    root: Path,
    project: str,
    lesson: dict[str, Any],
    environment: dict[str, Any],
    policy: dict[str, Any],
    client: httpx.Client,
    endpoints: list[Endpoint],
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
    values: dict[str, Any] = {}
    created_resource = False
    outcome = "verified"
    reason = "链路依赖验证通过"
    try:
        base_url = str(environment.get("base_url") or "").rstrip("/")
        if not base_url:
            raise ValueError("环境未配置 base_url")
        endpoint_by_key = {(item.method.upper(), item.path): item for item in endpoints}
        producer_endpoint = endpoint_by_key.get((producer_method, producer_path))
        if producer_endpoint is None:
            # Legacy candidates may predate endpoints.json persistence. The
            # candidate still contains enough information for a body-less
            # producer request; current projects always use the full schema.
            producer_endpoint = Endpoint(method=producer_method, path=producer_path)
        from ai_apitest.generator.cases import is_resource_producer

        # A GET-style generator may return an expiring value (for example a
        # temporary mailbox) without creating a deletable REST resource. Only
        # mutating producers require a matching compensating DELETE.
        created_resource = producer_method in {"POST", "PUT", "PATCH"} and is_resource_producer(producer_endpoint)
        if created_resource and not has_cleanup_endpoint(endpoints, set((producer.get("extract") or {}).keys())):
            raise ValueError("生产者会创建临时资源，但项目没有可匹配的安全清理接口")
        producer_url, producer_kwargs = build_endpoint_verification_request(base_url, producer_endpoint, environment)
        producer_response = client.request(producer_method, producer_url, **producer_kwargs)
        if producer_response.status_code >= 400:
            raise ValueError(f"生产者接口返回 HTTP {producer_response.status_code}")
        values = extract_dependency_values(producer_response, producer.get("extract") or {})
        if not values:
            raise ValueError("生产者响应中未提取到依赖数据")
        target_url, kwargs = build_consumer_request(base_url, consumer_method, consumer_path, values, environment)
        consumer_response = client.request(consumer_method, target_url, **kwargs)
        if consumer_response.status_code >= 400:
            outcome = "invalidated"
            reason = f"消费者接口返回 HTTP {consumer_response.status_code}"
    except Exception as exc:
        outcome = "failed"
        reason = str(exc)

    cleanup_error = (
        cleanup_verified_resource(client, str(environment.get("base_url") or "").rstrip("/"), endpoints, values, environment)
        if created_resource
        else ""
    )
    if cleanup_error:
        outcome = "failed"
        reason = f"依赖请求完成，但临时资源清理失败：{cleanup_error}"
    if outcome == "verified":
        update_dependency_status(root, project, lesson["id"], "verified", "verified_safe", reason)
    elif outcome == "invalidated":
        update_dependency_status(root, project, lesson["id"], "invalidated", "verification_failed", reason)
    else:
        update_dependency_status(root, project, lesson["id"], "candidate", "verification_failed", reason)
    return outcome


def build_endpoint_verification_request(
    base_url: str,
    endpoint: Endpoint,
    environment: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    from ai_apitest.execution.runner import apply_path_params, encode_query, encode_request_body
    from ai_apitest.generator.cases import default_body, default_headers, default_path_params, default_query

    headers = {**environment.get("headers", {}), **default_headers(endpoint)}
    headers = {str(key): value for key, value in headers.items() if value is not None}
    path = apply_path_params(endpoint.path, default_path_params(endpoint))
    query = default_query(endpoint, include_optional=False)
    if query:
        path += "?" + encode_query(query)
    kwargs: dict[str, Any] = {"headers": headers}
    kwargs.update(encode_request_body(headers, default_body(endpoint)))
    return base_url + path, kwargs


def cleanup_verified_resource(
    client: httpx.Client,
    base_url: str,
    endpoints: list[Endpoint],
    values: dict[str, Any],
    environment: dict[str, Any],
) -> str:
    if not values or not base_url:
        return ""
    candidates = []
    for endpoint in endpoints:
        if endpoint.method.upper() != "DELETE":
            continue
        names = set(re.findall(r"{([^{}]+)}", endpoint.path))
        if names and names.issubset(values):
            candidates.append((endpoint, names))
    if not candidates:
        return ""
    endpoint, names = candidates[0]
    path = endpoint.path
    for name in names:
        from urllib.parse import quote

        path = path.replace("{" + name + "}", quote(str(values[name]), safe=""))
    try:
        response = client.request("DELETE", base_url + path, headers=environment.get("headers", {}))
        if response.status_code not in {200, 202, 204, 404}:
            return f"HTTP {response.status_code}"
    except Exception as exc:
        return str(exc)
    return ""


def has_cleanup_endpoint(endpoints: list[Endpoint], fields: set[str]) -> bool:
    if not fields:
        return False
    return any(
        endpoint.method.upper() == "DELETE"
        and bool(set(re.findall(r"{([^{}]+)}", endpoint.path)))
        and set(re.findall(r"{([^{}]+)}", endpoint.path)).issubset(fields)
        for endpoint in endpoints
    )


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
    return json_path(payload, selector)


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
