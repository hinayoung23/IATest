from __future__ import annotations

import copy
import html
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from ai_apitest.core.http import fetch_text_limited
from ai_apitest.core.models import Endpoint
from ai_apitest.discovery.openapi import parse_document_text, parse_openapi


HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
MAX_DOCUMENT_BYTES = 20_000_000


def load_source_text(source: str) -> str:
    source = source.strip()
    if not source:
        raise ValueError("接口文档路径或 URL 不能为空")
    if source.startswith(("http://", "https://")):
        return fetch_text_limited(
            source,
            headers={"Accept": "application/json,application/yaml,text/yaml,text/html,text/markdown"},
            max_bytes=MAX_DOCUMENT_BYTES,
        )
    path = Path(source).expanduser()
    if path.is_dir():
        raise ValueError(f"接口文档路径不能是目录：{path}")
    if not path.exists():
        raise ValueError(f"接口文档路径不存在：{path}")
    if path.stat().st_size > MAX_DOCUMENT_BYTES:
        raise ValueError(f"接口文档超过 {MAX_DOCUMENT_BYTES} 字节限制：{path}")
    return path.read_text(encoding="utf-8")


def ensure_document_size(content: bytes, source: str) -> None:
    if len(content) > MAX_DOCUMENT_BYTES:
        raise ValueError(f"接口文档超过 {MAX_DOCUMENT_BYTES} 字节限制：{source}")


def parse_api_document(text: str, *, source: str = "") -> tuple[list[Endpoint], dict[str, Any]]:
    stripped = text.lstrip()
    if stripped.startswith("{") or looks_like_openapi_yaml(text):
        document = parse_document_text(text, source=source)
        version = str(document.get("openapi") or document.get("swagger") or "")
        source_format = "json" if stripped.startswith("{") else "yaml"
        return parse_openapi(document, source=source), {
            "source_type": "openapi",
            "source_format": source_format,
            "spec_version": version,
            "base_url": detect_openapi_base_url(document, source=source),
        }
    markdown = extract_markdown(text)
    endpoints = parse_markdown_endpoints(markdown)
    return endpoints, {"source_type": "markdown", "base_url": detect_base_url(markdown)}


def looks_like_openapi_yaml(text: str) -> bool:
    return bool(re.search(r"^\s*(?:openapi|swagger)\s*:\s*[^\n]+", text, flags=re.M))


def extract_markdown(text: str) -> str:
    match = re.search(
        r'<script[^>]+id=["\']markdown-source["\'][^>]*>(.*?)</script>',
        text,
        flags=re.S | re.I,
    )
    if match:
        return html.unescape(match.group(1)).strip()
    if "<html" in text.lower():
        return html_to_markdownish(text)
    return html.unescape(text)


def html_to_markdownish(text: str) -> str:
    converted = re.sub(r"<h([1-6])[^>]*>(.*?)</h\1>", lambda m: "#" * int(m.group(1)) + " " + strip_tags(m.group(2)), text, flags=re.S | re.I)
    converted = re.sub(r"<code>(.*?)</code>", lambda m: "`" + strip_tags(m.group(1)) + "`", converted, flags=re.S | re.I)
    converted = re.sub(r"<br\s*/?>", "\n", converted, flags=re.I)
    converted = strip_tags(converted)
    return html.unescape(converted)


def strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def parse_markdown_endpoints(markdown: str) -> list[Endpoint]:
    endpoints: list[Endpoint] = []
    current_summary = ""
    current_rules: list[dict[str, Any]] = []
    current_parameter_descriptions: dict[str, str] = {}
    current_responses: dict[str, Any] = {}
    section_facts = markdown_section_facts(markdown)
    global_rules = list((section_facts.get("概览") or ([], {}, {}))[0])
    in_code = False
    code_lang = ""
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if line.startswith("```"):
            if in_code:
                in_code = False
                code_lang = ""
            else:
                in_code = True
                code_lang = line.removeprefix("```").strip().lower()
            continue
        heading = re.match(r"^##\s+(.+)$", line)
        if heading:
            current_summary = heading.group(1).strip()
            current_rules, current_parameter_descriptions, current_responses = section_facts.get(
                current_summary,
                ([], {}, {}),
            )
            continue
        if in_code and code_lang not in {"http", ""}:
            continue
        match = re.match(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(\S+)", line, flags=re.I)
        if not match:
            continue
        method = match.group(1).upper()
        raw_path = match.group(2)
        path, query_params = split_path_query(raw_path)
        parameters = path_parameters(path) + query_parameters(query_params)
        for parameter in parameters:
            description = current_parameter_descriptions.get(str(parameter.get("name") or ""))
            if description:
                parameter["description"] = description
        endpoints.append(
            Endpoint(
                method=method,
                path=path,
                summary=current_summary or f"{method} {path}",
                description=current_summary,
                parameters=parameters,
                responses=copy.deepcopy(current_responses),
                tags=[tag_from_path(path)],
                business_rules=[
                    {
                        **rule,
                        "endpoint_method": method,
                        "endpoint_path": path,
                    }
                    for rule in [
                        *current_rules,
                        *global_rules_for_endpoint(global_rules, method, path),
                    ]
                ],
            )
        )
    return dedupe_endpoints(endpoints)


def markdown_section_facts(
    markdown: str,
) -> dict[str, tuple[list[dict[str, Any]], dict[str, str], dict[str, Any]]]:
    """Extract source-backed rule candidates without interpreting downstream tests.

    This is intentionally conservative: it preserves business bullets and
    parameter-table descriptions as interface-recognition evidence. Semantic
    normalization remains part of the trusted contract build.
    """
    matches = list(re.finditer(r"^##\s+(.+?)\s*$", markdown, flags=re.M))
    result: dict[str, tuple[list[dict[str, Any]], dict[str, str], dict[str, Any]]] = {}
    for index, match in enumerate(matches):
        heading = match.group(1).strip()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        section = markdown[match.end() : end]
        rules: list[dict[str, Any]] = []
        parameters: dict[str, str] = {}
        responses: dict[str, Any] = {}
        subsection = ""
        expected_status: int | None = None
        last_response_status: int | None = None
        in_code = False
        code_lang = ""
        code_lines: list[str] = []
        for raw_line in section.splitlines():
            line = raw_line.strip()
            if line.startswith("```"):
                if in_code:
                    if code_lang == "json" and code_lines and isinstance(expected_status, int):
                        try:
                            example = json.loads("\n".join(code_lines))
                        except json.JSONDecodeError:
                            pass
                        else:
                            add_markdown_response_example(
                                responses,
                                expected_status,
                                subsection,
                                example,
                            )
                            last_response_status = expected_status
                    in_code = False
                    code_lang = ""
                    code_lines = []
                else:
                    in_code = True
                    code_lang = line.removeprefix("```").strip().lower()
                continue
            if in_code:
                code_lines.append(raw_line)
                continue
            if not line:
                continue
            subheading = re.match(r"^###\s+(.+)$", line)
            if subheading:
                subsection = subheading.group(1).strip()
                status_match = re.search(r"\b([1-5]\d\d)\b", subsection)
                if status_match:
                    expected_status = int(status_match.group(1))
                elif "成功响应" in subsection:
                    expected_status = 200
                else:
                    expected_status = None
                continue
            status_match = re.search(r"(?:返回|响应(?:状态码)?(?:为|是)?)\s*`?([1-5]\d\d)`?", line)
            if status_match and re.search(
                r"(?:不|不会|不使用|不是)\s*$",
                line[max(0, status_match.start() - 8) : status_match.start()],
            ):
                status_match = None
            if status_match:
                expected_status = int(status_match.group(1))
                tail = re.split(r"[：:]", line, maxsplit=1)
                condition = tail[1].strip() if len(tail) == 2 else ""
                if expected_status >= 400 and condition and source_line_is_business_fact(condition):
                    evidence = f"{condition}，返回 {expected_status}"
                    rules.append(
                        {
                            "title": evidence,
                            "description": evidence,
                            "source_section": f"{heading} > {subsection}" if subsection else heading,
                            "source_evidence": line,
                            "expected_status": expected_status,
                        }
                    )
            bullet = re.match(r"^[-*]\s+(.+)$", line)
            if bullet:
                evidence = bullet.group(1).strip()
                if source_line_is_business_fact(evidence):
                    rules.append(
                        {
                            "title": evidence,
                            "description": evidence,
                            "source_section": f"{heading} > {subsection}" if subsection else heading,
                            "source_evidence": evidence,
                            "expected_status": expected_status,
                        }
                    )
                continue
            if line.startswith("|") and line.endswith("|"):
                cells = [cell.strip().strip("`") for cell in line.strip("|").split("|")]
                if len(cells) >= 3 and cells[0] not in {"参数", "字段", "---"}:
                    name = cells[0]
                    field_type = cells[1] if len(cells) >= 2 else ""
                    description = cells[-1]
                    if name and description and not set(name) <= {"-"} and not set(description) <= {"-"}:
                        if len(cells) >= 4:
                            parameters[name] = description
                        elif subsection == "字段说明" and isinstance(last_response_status, int):
                            annotate_markdown_response_field(
                                responses,
                                last_response_status,
                                name,
                                field_type,
                                description,
                            )
        if "200" in responses:
            for rule in rules:
                semantic_text = " ".join(
                    str(rule.get(key) or "")
                    for key in ("title", "description", "source_condition")
                )
                if (
                    rule.get("expected_status") is None
                    and "错误响应" not in str(rule.get("source_section") or "")
                    and (
                        not re.search(
                            r"不能|不存在|从未创建|已被(?:用户)?删除|已删除|超过.+保留窗口.+清理",
                            semantic_text,
                        )
                        or re.search(
                            r"返回.+(?:空数组|valid\s*:|expires_in)",
                            semantic_text,
                            flags=re.I,
                        )
                    )
                ):
                    rule["expected_status"] = 200
        result[heading] = (rules, parameters, responses)
    return result


def global_rules_for_endpoint(
    rules: list[dict[str, Any]],
    method: str,
    path: str,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for rule in rules:
        text = str(rule.get("source_evidence") or rule.get("description") or "")
        if path == "/api/generate" and re.search(r"默认有效期|默认返回\s*`?600", text):
            copied = copy.deepcopy(rule)
            copied["expected_status"] = 200
            result.append(copied)
    return result


def add_markdown_response_example(
    responses: dict[str, Any],
    status: int,
    subsection: str,
    example: Any,
) -> None:
    schema = schema_from_example(example)
    key = str(status)
    existing = responses.get(key)
    if not isinstance(existing, dict):
        existing = {
            "description": subsection or f"HTTP {status}",
            "content": {
                "application/json": {
                    "schema": schema,
                    "example": example,
                }
            },
        }
        responses[key] = existing
        return
    media = existing.setdefault("content", {}).setdefault("application/json", {})
    current_schema = media.get("schema") if isinstance(media.get("schema"), dict) else {}
    media["schema"] = merge_example_schemas(current_schema, schema)
    if example not in (None, [], {}):
        media["example"] = example


def annotate_markdown_response_field(
    responses: dict[str, Any],
    status: int,
    name: str,
    field_type: str,
    description: str,
) -> None:
    response = responses.get(str(status))
    if not isinstance(response, dict):
        return
    media = response.get("content")
    json_media = media.get("application/json") if isinstance(media, dict) else None
    schema = json_media.get("schema") if isinstance(json_media, dict) else None
    if not isinstance(schema, dict):
        return
    target_schema = schema.get("items") if schema.get("type") == "array" else schema
    properties = target_schema.get("properties") if isinstance(target_schema, dict) else None
    if not isinstance(properties, dict):
        return
    field_schema = properties.setdefault(name, {})
    if not isinstance(field_schema, dict):
        return
    types = [item.strip() for item in field_type.lower().split("/") if item.strip()]
    non_null = next((item for item in types if item != "null"), "")
    if non_null:
        field_schema["type"] = non_null
    if "null" in types:
        field_schema["nullable"] = True
    field_schema["description"] = description
    default_match = re.search(r"默认(?:返回|为)\s*`?(-?\d+)", description)
    if default_match:
        field_schema["default"] = int(default_match.group(1))
    domain_match = re.search(r"邮箱域名固定为\s*`?([A-Za-z0-9.-]+)", description)
    if domain_match:
        field_schema["pattern"] = rf"^[^@]+@{re.escape(domain_match.group(1))}$"


def schema_from_example(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if value is None:
        return {"nullable": True}
    if isinstance(value, list):
        item_schema = schema_from_example(value[0]) if value else {}
        return {"type": "array", "items": item_schema}
    if isinstance(value, dict):
        return {
            "type": "object",
            "properties": {
                str(name): schema_from_example(item)
                for name, item in value.items()
            },
            "required": [str(name) for name in value],
        }
    return {}


def merge_example_schemas(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    if not first:
        return copy.deepcopy(second)
    if not second:
        return copy.deepcopy(first)
    if first.get("type") != second.get("type"):
        return copy.deepcopy(first)
    result = copy.deepcopy(first)
    if first.get("type") == "object":
        properties = result.setdefault("properties", {})
        for name, schema in (second.get("properties") or {}).items():
            properties[name] = merge_example_schemas(
                properties.get(name, {}) if isinstance(properties.get(name), dict) else {},
                schema if isinstance(schema, dict) else {},
            )
        result["required"] = sorted(
            set(first.get("required") or []) & set(second.get("required") or [])
        )
    elif first.get("type") == "array":
        result["items"] = merge_example_schemas(
            first.get("items") if isinstance(first.get("items"), dict) else {},
            second.get("items") if isinstance(second.get("items"), dict) else {},
        )
    if first.get("nullable") or second.get("nullable"):
        result["nullable"] = True
    return result


def source_line_is_business_fact(value: str) -> bool:
    return bool(
        re.search(
            r"返回|生成|创建|固定|刷新|只能|必须|最长|最短|最大|最小|"
            r"过期|到期|续期|删除|清理|倒序|升序|有效|不存在|空数组|保留|接收|超过|限流|"
            r"非法|重复|受限|空格|中文|URL\s*encode|URL\s*编码",
            str(value or ""),
            flags=re.I,
        )
    )


def split_path_query(value: str) -> tuple[str, list[str]]:
    if "?" not in value:
        return value, []
    path, query = value.split("?", 1)
    names = []
    for part in query.split("&"):
        if "=" in part:
            names.append(part.split("=", 1)[0])
    return path, names


def path_parameters(path: str) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "in": "path",
            "required": True,
            "schema": {"type": "string", "example": sample_param_value(name)},
        }
        for name in re.findall(r"{([^}]+)}", path)
    ]


def query_parameters(names: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "in": "query",
            "required": False,
            "schema": {"type": "string", "example": sample_param_value(name)},
        }
        for name in names
    ]


def sample_param_value(name: str) -> str:
    if name.lower() == "email":
        return "test123@mail.tempbox.cn"
    if name.lower() == "prefix":
        return "iatest"
    return "sample"


def tag_from_path(path: str) -> str:
    parts = [part for part in path.split("/") if part and not part.startswith("{")]
    return parts[1] if parts and parts[0] == "api" and len(parts) > 1 else (parts[0] if parts else "api")


def dedupe_endpoints(endpoints: list[Endpoint]) -> list[Endpoint]:
    by_key: dict[tuple[str, str], Endpoint] = {}
    unique = []
    for endpoint in endpoints:
        key = (endpoint.method, endpoint.path)
        if key in by_key:
            existing = by_key[key]
            existing_names = {(item.get("in"), item.get("name")) for item in existing.parameters}
            for parameter in endpoint.parameters:
                param_key = (parameter.get("in"), parameter.get("name"))
                if param_key not in existing_names:
                    existing.parameters.append(parameter)
                    existing_names.add(param_key)
            existing_rule_keys = {
                markdown_rule_identity(item)
                for item in existing.business_rules
                if isinstance(item, dict)
            }
            for rule in endpoint.business_rules:
                if not isinstance(rule, dict):
                    continue
                rule_key = markdown_rule_identity(rule)
                if rule_key not in existing_rule_keys:
                    existing.business_rules.append(rule)
                    existing_rule_keys.add(rule_key)
            continue
        by_key[key] = endpoint
        unique.append(endpoint)
    return unique


def markdown_rule_identity(rule: dict[str, Any]) -> tuple[str, str, str]:
    return (
        re.sub(r"\s+", " ", str(rule.get("source_evidence") or "")).strip(),
        str(rule.get("expected_status") or ""),
        str(rule.get("source_section") or ""),
    )


def detect_base_url(markdown: str) -> str:
    current = re.search(r"当前 HTTP 基础地址：`([^`]+)`", markdown)
    if current:
        return current.group(1)
    preferred = re.search(r"推荐 HTTPS 基础地址：`([^`]+)`", markdown)
    if preferred:
        return preferred.group(1)
    return ""


def detect_openapi_base_url(document: dict[str, Any], *, source: str = "") -> str:
    servers = document.get("servers") or []
    if servers and isinstance(servers[0], dict):
        value = str(servers[0].get("url", ""))
        variables = servers[0].get("variables", {})
        if isinstance(variables, dict):
            for name, config in variables.items():
                if isinstance(config, dict) and config.get("default") is not None:
                    value = value.replace("{" + str(name) + "}", str(config["default"]))
        if source.startswith(("http://", "https://")):
            return urljoin(source, value)
        return value
    if document.get("swagger"):
        host = str(document.get("host") or "")
        base_path = "/" + str(document.get("basePath") or "").lstrip("/")
        base_path = base_path.rstrip("/")
        schemes = document.get("schemes") or []
        scheme = str(schemes[0]) if schemes else "https"
        if host:
            return f"{scheme}://{host}{base_path}"
    return ""
