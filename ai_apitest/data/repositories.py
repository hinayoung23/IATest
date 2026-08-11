from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_apitest.context.storage import list_lessons
from ai_apitest.core.config import load_config
from ai_apitest.core.models import Endpoint, TestCase
from ai_apitest.core.paths import project_docs_file, project_metadata_file, safe_project_name
from ai_apitest.core.serde import endpoint_from_dict, read_json, to_jsonable
from ai_apitest.data.agent_runs import redact_sensitive, redact_text, sensitive_key


class EndpointRepository:
    def __init__(self, root: Path, project: str, endpoints: list[Endpoint] | None = None) -> None:
        self.root = root
        self.project = safe_project_name(project)
        self._endpoints = list(endpoints) if endpoints is not None else None

    def all(self) -> list[Endpoint]:
        if self._endpoints is None:
            raw = read_json(project_docs_file(self.root, self.project), [])
            self._endpoints = [endpoint_from_dict(item) for item in raw if isinstance(item, dict)]
        return list(self._endpoints)

    def list_summaries(self, *, query: str = "", offset: int = 0, limit: int = 50) -> dict[str, Any]:
        query_lower = query.strip().lower()
        endpoints = self.all()
        if query_lower:
            endpoints = [
                endpoint
                for endpoint in endpoints
                if query_lower in f"{endpoint.method} {endpoint.path} {endpoint.summary} {' '.join(endpoint.tags)}".lower()
            ]
        safe_offset = max(0, int(offset))
        safe_limit = max(1, min(100, int(limit)))
        page = endpoints[safe_offset : safe_offset + safe_limit]
        return {
            "items": [endpoint_summary(item) for item in page],
            "offset": safe_offset,
            "limit": safe_limit,
            "total": len(endpoints),
            "has_more": safe_offset + len(page) < len(endpoints),
        }

    def get(self, method: str, path: str) -> Endpoint:
        key = (str(method).upper(), str(path))
        for endpoint in self.all():
            if (endpoint.method.upper(), endpoint.path) == key:
                return endpoint
        raise KeyError(f"接口不存在：{key[0]} {key[1]}")

    def detail(self, method: str, path: str) -> dict[str, Any]:
        return redact_tool_data(to_jsonable(self.get(method, path)))

    def dependency_graph(self) -> dict[str, Any]:
        metadata = read_json(project_metadata_file(self.root, self.project), {})
        graph = metadata.get("dependency_graph") if isinstance(metadata, dict) else None
        return graph if isinstance(graph, dict) else {"edges": []}

    def list_business_rules(
        self,
        *,
        method: str = "",
        path: str = "",
        query: str = "",
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        from ai_apitest.domain.contracts import business_rule_inventory

        items = business_rule_inventory(self.all(), testable_only=False)
        method_value = str(method or "").upper()
        path_value = str(path or "")
        query_value = str(query or "").strip().lower()
        if method_value:
            items = [item for item in items if item["endpoint_method"] == method_value]
        if path_value:
            items = [item for item in items if item["endpoint_path"] == path_value]
        if query_value:
            items = [
                item
                for item in items
                if query_value
                in " ".join(
                    str(item.get(key) or "")
                    for key in ("rule_id", "title", "description", "category", "strategy", "lifecycle_state")
                ).lower()
            ]
        safe_offset = max(0, int(offset))
        safe_limit = max(1, min(200, int(limit)))
        page = items[safe_offset : safe_offset + safe_limit]
        return {
            "items": redact_tool_data(page),
            "offset": safe_offset,
            "limit": safe_limit,
            "total": len(items),
            "has_more": safe_offset + len(page) < len(items),
        }

    def business_rule_detail(self, rule_id: str) -> dict[str, Any]:
        from ai_apitest.domain.contracts import business_rule_inventory

        requested = str(rule_id or "").strip()
        for item in business_rule_inventory(self.all(), testable_only=False):
            if str(item.get("rule_id") or "") == requested:
                return redact_tool_data(item)
        raise KeyError(f"业务规则不存在：{requested}")


class ConfigRepository:
    def __init__(self, root: Path) -> None:
        self.root = root

    def config(self) -> dict[str, Any]:
        return load_config(self.root)

    def generation_strategies(self) -> dict[str, Any]:
        generation = self.config().get("case_generation", {})
        defaults = [name for name, enabled in generation.get("default_strategies", {}).items() if enabled]
        custom = [
            {
                "name": str(item.get("name") or ""),
                "description": str(item.get("description") or ""),
            }
            for item in generation.get("custom_strategies", [])
            if item.get("enabled", True) and str(item.get("name") or "").strip()
        ]
        return {"enabled_default_strategies": defaults, "custom_strategies": custom}


class ContextRepository:
    def __init__(self, root: Path, project: str) -> None:
        self.root = root
        self.project = safe_project_name(project)

    def search_confirmed(
        self,
        *,
        query: str = "",
        method: str = "",
        path: str = "",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        size = max(1, min(50, int(limit)))
        rows: list[dict[str, Any]] = []
        for lifecycle in ("active", "reopened"):
            rows.extend(
                list_lessons(
                    self.root,
                    self.project,
                    lifecycle_status=lifecycle,
                    query_text=query,
                    limit=size * 2,
                )
            )
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in rows:
            lesson_id = str(item.get("id") or "")
            if not lesson_id or lesson_id in seen:
                continue
            if item.get("review_status") != "confirmed":
                continue
            if item.get("lesson_type") == "data_dependency" and item.get("verification_status") != "verified":
                continue
            if method and str(item.get("method") or "").upper() != method.upper():
                continue
            if path and str(item.get("path") or "") != path:
                continue
            seen.add(lesson_id)
            result.append(
                {
                    "id": lesson_id,
                    "scope": item.get("scope"),
                    "method": item.get("method"),
                    "path": item.get("path"),
                    "lesson_type": item.get("lesson_type"),
                    "title": item.get("title"),
                    "content": item.get("content"),
                    "confidence": item.get("confidence"),
                    "verification_status": item.get("verification_status"),
                    "updated_at": item.get("updated_at"),
                }
            )
            if len(result) >= size:
                break
        return redact_tool_data(result)


class CaseRepository:
    def __init__(self, root: Path, project: str, cases: list[TestCase] | None = None) -> None:
        self.root = root
        self.project = safe_project_name(project)
        self._cases = list(cases) if cases is not None else None

    def all(self) -> list[TestCase]:
        if self._cases is None:
            from ai_apitest.generator.cases import load_cases_from_markdown

            self._cases = load_cases_from_markdown(self.root, self.project)
        return list(self._cases)

    def list_summaries(self, *, query: str = "", offset: int = 0, limit: int = 50) -> dict[str, Any]:
        query_lower = query.strip().lower()
        cases = self.all()
        if query_lower:
            cases = [
                case
                for case in cases
                if query_lower
                in f"{case.id} {case.scenario_id} {case.method} {case.path} {case.title} {case.design_method} {' '.join(case.tags)}".lower()
            ]
        safe_offset = max(0, int(offset))
        safe_limit = max(1, min(100, int(limit)))
        page = cases[safe_offset : safe_offset + safe_limit]
        return {
            "items": [case_summary(item) for item in page],
            "offset": safe_offset,
            "limit": safe_limit,
            "total": len(cases),
            "has_more": safe_offset + len(page) < len(cases),
        }

    def get(self, case_id: str) -> TestCase:
        requested = str(case_id).strip()
        for case in self.all():
            if case.id == requested:
                return case
        raise KeyError(f"用例不存在：{requested}")

    def detail(self, case_id: str) -> dict[str, Any]:
        return redact_sensitive(to_jsonable(self.get(case_id)))


@dataclass(slots=True)
class ProjectDataAccess:
    root: Path
    project: str
    endpoints: EndpointRepository
    cases: CaseRepository
    config: ConfigRepository
    context: ContextRepository

    @classmethod
    def create(
        cls,
        root: Path,
        project: str,
        *,
        endpoints: list[Endpoint] | None = None,
        cases: list[TestCase] | None = None,
    ) -> ProjectDataAccess:
        safe_project = safe_project_name(project)
        return cls(
            root=root,
            project=safe_project,
            endpoints=EndpointRepository(root, safe_project, endpoints=endpoints),
            cases=CaseRepository(root, safe_project, cases=cases),
            config=ConfigRepository(root),
            context=ContextRepository(root, safe_project),
        )


def endpoint_summary(endpoint: Endpoint) -> dict[str, Any]:
    parameters = []
    for item in endpoint.parameters:
        schema = item.get("schema") if isinstance(item.get("schema"), dict) else {}
        parameters.append(
            {
                "name": str(item.get("name") or ""),
                "in": str(item.get("in") or ""),
                "required": bool(item.get("required")),
                "type": schema.get("type"),
                "format": schema.get("format"),
                "enum": schema.get("enum"),
                "minimum": schema.get("minimum"),
                "maximum": schema.get("maximum"),
                "min_length": schema.get("minLength"),
                "max_length": schema.get("maxLength"),
                "pattern": schema.get("pattern"),
                "description": str(item.get("description") or schema.get("description") or "")[:320],
                "allow_duplicate": item.get("x-iatest-allow-duplicate"),
            }
        )
    response_summaries = []
    for status, response in endpoint.responses.items():
        description = str(response.get("description") or "") if isinstance(response, dict) else ""
        response_summaries.append({"status": str(status), "description": description[:240]})
    return {
        "method": endpoint.method.upper(),
        "path": endpoint.path,
        "summary": endpoint.summary,
        "description": endpoint.description[:320],
        "tags": endpoint.tags,
        "deprecated": endpoint.deprecated,
        "parameters": parameters,
        "query_parameters": [item for item in parameters if item["in"] == "query"],
        "path_parameters": [item["name"] for item in parameters if item["in"] == "path"],
        "has_request_body": bool(endpoint.request_body),
        "request_body_required": bool((endpoint.request_body or {}).get("required")) if isinstance(endpoint.request_body, dict) else False,
        "responses": response_summaries,
        "response_statuses": [item["status"] for item in response_summaries],
        "business_rule_count": len(endpoint.business_rules),
        "business_rule_ids": [
            str(item.get("rule_id") or "")
            for item in endpoint.business_rules
            if isinstance(item, dict) and item.get("rule_id")
        ],
    }


def case_summary(case: TestCase) -> dict[str, Any]:
    return {
        "id": case.id,
        "scenario_id": case.scenario_id,
        "title": case.title,
        "method": case.method.upper(),
        "path": case.path,
        "design_method": case.design_method,
        "priority": case.priority,
        "tags": list(case.tags),
        "runnable": case.runnable,
        "risk": case.risk,
    }


def redact_tool_data(value: Any) -> Any:
    """Remove credential values while preserving Endpoint schema definitions."""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if sensitive_key(str(key)) and not schema_definition(item):
                result[str(key)] = "[REDACTED]"
            else:
                result[str(key)] = redact_tool_data(item)
        return result
    if isinstance(value, list):
        return [redact_tool_data(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def schema_definition(value: Any) -> bool:
    return isinstance(value, dict) and bool(
        {"type", "$ref", "properties", "items", "oneOf", "anyOf", "allOf"}.intersection(value)
    )
