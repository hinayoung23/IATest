from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from ai_apitest.core.models import Endpoint


HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def load_openapi(source: str) -> dict[str, Any]:
    if source.startswith(("http://", "https://")):
        response = get_with_proxy_fallback(source, headers={"Accept": "application/json"})
        response.raise_for_status()
        return response.json()
    with Path(source).expanduser().open("r", encoding="utf-8") as file:
        return json.load(file)


def get_with_proxy_fallback(source: str, headers: dict[str, str]) -> httpx.Response:
    try:
        return httpx.get(source, headers=headers, timeout=20)
    except httpx.ConnectError:
        return httpx.get(source, headers=headers, timeout=20, trust_env=False)


def parse_openapi(document: dict[str, Any]) -> list[Endpoint]:
    endpoints: list[Endpoint] = []
    paths = document.get("paths", {})
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            parameters = []
            parameters.extend(path_item.get("parameters", []))
            parameters.extend(operation.get("parameters", []))
            endpoints.append(
                Endpoint(
                    method=method.upper(),
                    path=path,
                    summary=operation.get("summary", ""),
                    description=operation.get("description", ""),
                    parameters=parameters,
                    request_body=operation.get("requestBody"),
                    responses=operation.get("responses", {}),
                    tags=operation.get("tags", []),
                )
            )
    return endpoints
