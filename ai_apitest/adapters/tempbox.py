"""Legacy Tempbox contract support, scoped to its documented routes.

The generic contract compiler does not infer these application routes or TTLs.
"""
from __future__ import annotations
import re
import copy
from typing import Any
from ai_apitest.core.models import Endpoint

ROUTES = {("GET", "/api/generate"), ("DELETE", "/api/delete/{email}"),
          ("POST", "/api/extend/{email}"), ("GET", "/api/check/{email}"),
          ("GET", "/api/messages/{email}")}

def supports(method: str, path: str) -> bool:
    return (method.upper(), path) in ROUTES


def lifetime_assertion(text: str) -> dict:
    match = re.search(r"(?:默认有效期|默认返回|expires_in\s*[=:：]?|有效期重置(?:为)?)\s*`?(\d+)", text)
    if match:
        return {"type": "json_path", "target": "$.expires_in", "operator": "eq", "expected": int(match.group(1))}
    return {"type": "json_path", "target": "$.expires_in", "operator": "is_positive_integer", "expected": True}


def infer_documented_postcondition_chain(text: str, *, method: str = "", path: str = "") -> list[dict[str, Any]]:
    if not supports(method, path):
        return []
    if re.search(r"删除成功后.+不能再查询邮件.+续期", text):
        return [
            {
                "name": "生成真实临时邮箱",
                "method": "GET",
                "path": "/api/generate",
                "extract": {"email": "$.email"},
            },
            {
                "name": "删除刚生成的临时邮箱",
                "method": "DELETE",
                "path": "/api/delete/{email}",
                "path_params": {"email": "{{email}}"},
                "assertions": [
                    {"type": "status_code", "target": "", "operator": "eq", "expected": 200},
                    {"type": "json_path", "target": "$.status", "operator": "eq", "expected": "ok"},
                    {
                        "type": "json_path",
                        "target": "$.message",
                        "operator": "eq",
                        "expected": "Mailbox deleted",
                    },
                ],
            },
            {
                "name": "验证删除后无法再查询邮件",
                "method": "GET",
                "path": "/api/messages/{email}",
                "path_params": {"email": "{{email}}"},
                "assertions": [
                    {"type": "status_code", "target": "", "operator": "eq", "expected": 200},
                    {"type": "json_path", "target": "$", "operator": "is_array", "expected": True},
                    {"type": "json_path", "target": "$", "operator": "length_eq", "expected": 0},
                ],
            },
            {
                "name": "验证删除后无法续期",
                "method": "POST",
                "path": "/api/extend/{email}",
                "path_params": {"email": "{{email}}"},
                "assertions": [
                    {"type": "status_code", "target": "", "operator": "eq", "expected": 404},
                    {
                        "type": "json_path",
                        "target": "$.detail",
                        "operator": "eq",
                        "expected": "Mailbox not found or expired",
                    },
                ],
            },
        ]
    if re.search(r"同一个.+前缀.+已经存在.+刷新.+创建时间", text):
        prefix = "refresh-{{random}}"
        return [
            {
                "name": "先创建同前缀邮箱",
                "method": "GET",
                "path": "/api/generate",
                "query": {"prefix": prefix},
                "extract": {"email": "$.email", "first_created_at": "$.created_at"},
            },
            {
                "name": "再次生成同前缀邮箱并验证刷新",
                "method": "GET",
                "path": "/api/generate",
                "query": {"prefix": prefix},
                "assertions": [
                    {"type": "status_code", "target": "", "operator": "eq", "expected": 200},
                    {
                        "type": "json_path",
                        "target": "$.email",
                        "operator": "eq",
                        "expected": "{{email}}",
                    },
                    {
                        "type": "json_path",
                        "target": "$.created_at",
                        "operator": "gte",
                        "expected": "{{first_created_at}}",
                    },
                    lifetime_assertion(text),
                ],
            },
        ]
    if re.search(r"续期成功后.+(?:创建|刷新)时间更新为当前时间.+新的剩余有效期", text):
        return [
            {
                "name": "生成真实临时邮箱",
                "method": "GET",
                "path": "/api/generate",
                "extract": {"email": "$.email"},
            },
            {
                "name": "续期刚生成的临时邮箱",
                "method": "POST",
                "path": "/api/extend/{email}",
                "path_params": {"email": "{{email}}"},
                "assertions": [
                    {"type": "status_code", "target": "", "operator": "eq", "expected": 200},
                    {"type": "json_path", "target": "$.status", "operator": "eq", "expected": "ok"},
                    lifetime_assertion(text),
                ],
            },
            {
                "name": "验证续期后邮箱恢复有效",
                "method": "GET",
                "path": "/api/check/{email}",
                "path_params": {"email": "{{email}}"},
                "assertions": [
                    {"type": "status_code", "target": "", "operator": "eq", "expected": 200},
                    {"type": "json_path", "target": "$.valid", "operator": "eq", "expected": True},
                    {
                        "type": "json_path",
                        "target": "$.expires_in",
                        "operator": "is_positive_integer",
                        "expected": True,
                    },
                ],
            },
        ]
    return []


def deterministic_lifecycle_chain(
    endpoint: Endpoint,
    rule: dict[str, Any],
    endpoints: list[Endpoint],
) -> list[dict[str, Any]]:
    from ai_apitest.domain.contracts import business_condition_signature

    if not supports(endpoint.method, endpoint.path):
        return []

    text = " ".join(
        str(rule.get(key) or "")
        for key in ("title", "description", "source_condition")
    )
    condition = business_condition_signature(text)
    expected = rule.get("expected") if isinstance(rule.get("expected"), dict) else {}
    status = expected.get("status")
    endpoint_keys = {(item.method.upper(), item.path) for item in endpoints}
    generate_key = ("GET", "/api/generate")
    delete_key = ("DELETE", "/api/delete/{email}")

    if (
        endpoint.method.upper() == "DELETE"
        and endpoint.path == delete_key[1]
        and generate_key in endpoint_keys
        and re.search(r"删除成功后.+不能再查询邮件.+续期", text)
    ):
        steps: list[dict[str, Any]] = [
            {
                "name": "生成真实临时邮箱",
                "method": generate_key[0],
                "path": generate_key[1],
                "extract": {"email": "$.email"},
            },
            {
                "name": "删除刚生成的临时邮箱",
                "method": delete_key[0],
                "path": delete_key[1],
                "path_params": {"email": "{{email}}"},
                "assertions": [
                    {"type": "status_code", "target": "", "operator": "eq", "expected": 200},
                    {"type": "json_path", "target": "$.status", "operator": "eq", "expected": "ok"},
                ],
            },
        ]
        messages_key = ("GET", "/api/messages/{email}")
        if messages_key in endpoint_keys:
            steps.append(
                {
                    "name": "验证删除后无法再查询邮件",
                    "method": messages_key[0],
                    "path": messages_key[1],
                    "path_params": {"email": "{{email}}"},
                    "assertions": [
                        {"type": "status_code", "target": "", "operator": "eq", "expected": 200},
                        {"type": "json_path", "target": "$", "operator": "is_array", "expected": True},
                        {"type": "json_path", "target": "$", "operator": "length_eq", "expected": 0},
                    ],
                }
            )
        extend_key = ("POST", "/api/extend/{email}")
        if extend_key in endpoint_keys:
            steps.append(
                {
                    "name": "验证删除后无法续期",
                    "method": extend_key[0],
                    "path": extend_key[1],
                    "path_params": {"email": "{{email}}"},
                    "assertions": [
                        {"type": "status_code", "target": "", "operator": "eq", "expected": 404},
                    ],
                }
            )
        return steps if len(steps) >= 3 else []

    if endpoint.path == "/api/generate" and re.search(r"同一个.+前缀.+已经存在|刷新.+创建时间", text):
        prefix = "refresh-{{random}}"
        return [
            {
                "name": "先创建同前缀邮箱",
                "method": "GET",
                "path": "/api/generate",
                "query": {"prefix": prefix},
                "extract": {"email": "$.email", "first_created_at": "$.created_at"},
            },
            {
                "name": "再次生成同前缀邮箱并验证刷新",
                "method": "GET",
                "path": "/api/generate",
                "query": {"prefix": prefix},
                "assertions": [
                    {"type": "status_code", "target": "", "operator": "eq", "expected": 200},
                    {
                        "type": "json_path",
                        "target": "$.email",
                        "operator": "eq",
                        "expected": "{{email}}",
                    },
                    {
                        "type": "json_path",
                        "target": "$.created_at",
                        "operator": "gte",
                        "expected": "{{first_created_at}}",
                    },
                    lifetime_assertion(text),
                ],
            },
        ]

    has_email_path = any(
        isinstance(item, dict)
        and item.get("in") == "path"
        and item.get("name") == "email"
        for item in endpoint.parameters
    ) or "{email}" in endpoint.path
    if not has_email_path or generate_key not in endpoint_keys:
        return []
    if condition in {"missing", "cleaned"}:
        return []
    if not isinstance(status, int) or not 200 <= status < 300:
        if condition != "deleted":
            return []

    steps: list[dict[str, Any]] = [
        {
            "name": "生成真实临时邮箱",
            "method": generate_key[0],
            "path": generate_key[1],
            "extract": {"email": "$.email"},
        }
    ]
    if condition == "deleted":
        if delete_key not in endpoint_keys:
            return []
        steps.append(
            {
                "name": "删除刚生成的临时邮箱",
                "method": delete_key[0],
                "path": delete_key[1],
                "path_params": {"email": "{{email}}"},
            }
        )
    steps.append(
        {
            "name": str(rule.get("title") or endpoint.summary or "验证目标接口"),
            "method": endpoint.method.upper(),
            "path": endpoint.path,
            "path_params": {"email": "{{email}}"},
        }
    )
    return steps



def global_rules_for_endpoint(
    rules: list[dict[str, Any]],
    method: str,
    path: str,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for rule in rules:
        text = str(rule.get("source_evidence") or rule.get("description") or "")
        if path == "/api/generate" and re.search(r"默认有效期|默认返回\s*`?\d+", text):
            copied = copy.deepcopy(rule)
            copied["expected_status"] = 200
            result.append(copied)
    return result
