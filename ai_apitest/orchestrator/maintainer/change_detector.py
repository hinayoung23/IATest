from __future__ import annotations

from ai_apitest.core.models import Endpoint


def diff_endpoints(old: list[Endpoint], new: list[Endpoint]) -> dict:
    old_map = {endpoint_key(item): item for item in old}
    new_map = {endpoint_key(item): item for item in new}
    added = sorted(set(new_map) - set(old_map))
    removed = sorted(set(old_map) - set(new_map))
    changed = []
    for key in sorted(set(old_map).intersection(new_map)):
        if endpoint_signature(old_map[key]) != endpoint_signature(new_map[key]):
            changed.append(key)
    return {"added": added, "removed": removed, "changed": changed}


def endpoint_key(endpoint: Endpoint) -> str:
    return f"{endpoint.method.upper()} {endpoint.path}"


def endpoint_signature(endpoint: Endpoint) -> dict:
    return {
        "summary": endpoint.summary,
        "description": endpoint.description,
        "parameters": endpoint.parameters,
        "request_body": endpoint.request_body,
        "responses": endpoint.responses,
        "tags": endpoint.tags,
        "operation_id": endpoint.operation_id,
        "deprecated": endpoint.deprecated,
        "security": endpoint.security,
        "business_rules": endpoint.business_rules,
    }
