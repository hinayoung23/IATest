from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

import httpx

from ai_apitest.core.models import Endpoint
from ai_apitest.discovery.openapi import parse_openapi


HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


def load_source_text(source: str) -> str:
    source = source.strip()
    if not source:
        raise ValueError("接口文档路径或 URL 不能为空")
    if source.startswith(("http://", "https://")):
        response = get_with_proxy_fallback(source, headers={"Accept": "application/json,text/html,text/markdown"})
        response.raise_for_status()
        return response.text
    path = Path(source).expanduser()
    if path.is_dir():
        raise ValueError(f"接口文档路径不能是目录：{path}")
    if not path.exists():
        raise ValueError(f"接口文档路径不存在：{path}")
    return path.read_text(encoding="utf-8")


def get_with_proxy_fallback(source: str, headers: dict[str, str]) -> httpx.Response:
    try:
        return httpx.get(source, headers=headers, timeout=20)
    except httpx.ConnectError:
        return httpx.get(source, headers=headers, timeout=20, trust_env=False)


def parse_api_document(text: str) -> tuple[list[Endpoint], dict[str, Any]]:
    stripped = text.lstrip()
    if stripped.startswith("{"):
        document = json.loads(text)
        return parse_openapi(document), {"source_type": "openapi", "base_url": detect_openapi_base_url(document)}
    markdown = extract_markdown(text)
    endpoints = parse_markdown_endpoints(markdown)
    return endpoints, {"source_type": "markdown", "base_url": detect_base_url(markdown)}


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
        endpoints.append(
            Endpoint(
                method=method,
                path=path,
                summary=current_summary or f"{method} {path}",
                description=current_summary,
                parameters=parameters,
                responses={"200": {"description": "OK"}},
                tags=[tag_from_path(path)],
            )
        )
    return dedupe_endpoints(endpoints)


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
        return "aitest"
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
            continue
        by_key[key] = endpoint
        unique.append(endpoint)
    return unique


def detect_base_url(markdown: str) -> str:
    current = re.search(r"当前 HTTP 基础地址：`([^`]+)`", markdown)
    if current:
        return current.group(1)
    preferred = re.search(r"推荐 HTTPS 基础地址：`([^`]+)`", markdown)
    if preferred:
        return preferred.group(1)
    return ""


def detect_openapi_base_url(document: dict[str, Any]) -> str:
    servers = document.get("servers") or []
    if servers and isinstance(servers[0], dict):
        return servers[0].get("url", "")
    return ""
