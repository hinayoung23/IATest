from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urldefrag, urljoin, urlparse

from ai_apitest.core.http import fetch_text_limited
from ai_apitest.core.sandbox import read_file
from ai_apitest.core.models import Endpoint


HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
MAX_REFERENCE_DOCUMENTS = 50
MAX_REFERENCE_BYTES = 20_000_000


def load_openapi(source: str) -> dict[str, Any]:
    if source.startswith(("http://", "https://")):
        text = fetch_text_limited(
            source,
            headers={"Accept": "application/json,application/yaml,text/yaml,*/*"},
            max_bytes=MAX_REFERENCE_BYTES,
        )
        return parse_document_text(text, source=source)
    path = Path(source).expanduser()
    ensure_reference_size_from_path(path)
    return parse_document_text(read_file(path, MAX_REFERENCE_BYTES).decode("utf-8"), source=str(path))


def parse_document_text(text: str, *, source: str = "") -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as json_error:
        try:
            import yaml
        except ModuleNotFoundError as exc:
            raise ValueError("YAML 接口文档需要安装 PyYAML 后才能解析") from exc
        try:
            value = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            location = f"（{source}）" if source else ""
            raise ValueError(f"接口文档不是有效的 JSON 或 YAML{location}：{exc}") from json_error
    if not isinstance(value, dict):
        raise ValueError("OpenAPI 文档根节点必须是对象")
    return value


class ReferenceResolver:
    def __init__(self, document: dict[str, Any], source: str = "") -> None:
        self.root_source = normalize_source(source)
        self.documents: dict[str, dict[str, Any]] = {self.root_source: document}
        self.recursive_definitions: dict[str, tuple[Any, dict, str, tuple[str, str]]] = {}
        self.local_root = Path(self.root_source).parent if self.root_source and not self.root_source.startswith(("http://", "https://")) else None

    def resolve(
        self,
        value: Any,
        *,
        document: dict[str, Any] | None = None,
        source: str | None = None,
        stack: frozenset[tuple[str, str]] = frozenset(),
        depth: int = 0,
    ) -> Any:
        if depth > 100:
            raise ValueError("OpenAPI $ref 嵌套超过 100 层")
        current_document = self.documents[self.root_source] if document is None else document
        current_source = self.root_source if source is None else source
        if isinstance(value, list):
            return [
                self.resolve(item, document=current_document, source=current_source, stack=stack, depth=depth + 1)
                for item in value
            ]
        if not isinstance(value, dict):
            return value
        reference = value.get("$ref")
        if isinstance(reference, str) and reference:
            if reference.startswith("#/$defs/iatest_"):
                return dict(value)
            target, target_document, target_source, reference_key = self.lookup(
                reference,
                current_document,
                current_source,
            )
            if reference_key in stack:
                name = "iatest_" + hashlib.sha256(repr(reference_key).encode()).hexdigest()[:24]
                self.recursive_definitions[name] = (target, target_document, target_source, reference_key)
                return {**value, "$ref": f"#/$defs/{name}"}
            resolved = self.resolve(
                target,
                document=target_document,
                source=target_source,
                stack=stack | {reference_key},
                depth=depth + 1,
            )
            siblings = {key: item for key, item in value.items() if key != "$ref"}
            if not siblings:
                return resolved
            resolved_siblings = self.resolve(
                siblings,
                document=current_document,
                source=current_source,
                stack=stack,
                depth=depth + 1,
            )
            if isinstance(resolved, dict):
                return {**resolved, **resolved_siblings}
            return resolved_siblings
        return {
            key: self.resolve(item, document=current_document, source=current_source, stack=stack, depth=depth + 1)
            for key, item in value.items()
        }

    def bind_schemas(self, value: Any) -> Any:
        """Keep cyclic references local when extracting a schema from its document."""
        if isinstance(value, list):
            return [self.bind_schemas(item) for item in value]
        if not isinstance(value, dict):
            return value
        output = {}
        for key, item in value.items():
            output[key] = self.bundle_schema(item) if key == "schema" else self.bind_schemas(item)
        return output

    def bundle_schema(self, schema: Any) -> Any:
        if not isinstance(schema, dict):
            return schema
        definitions = dict(schema.get("$defs", {}))
        def references(value):
            if isinstance(value, dict):
                ref = value.get("$ref", "")
                if isinstance(ref, str) and ref.startswith("#/$defs/iatest_"):
                    yield ref.split("/")[-1]
                for child in value.values():
                    yield from references(child)
            elif isinstance(value, list):
                for child in value:
                    yield from references(child)
        pending = list(references(schema))
        while pending:
            name = pending.pop()
            if name in definitions:
                continue
            target, document, source, reference_key = self.recursive_definitions[name]
            definition = self.resolve(target, document=document, source=source, stack=frozenset({reference_key}))
            definitions[name] = definition
            pending.extend(references(definition))
        return {**schema, "$defs": definitions} if definitions else schema

    def lookup(
        self,
        reference: str,
        current_document: dict[str, Any],
        current_source: str,
    ) -> tuple[Any, dict[str, Any], str, tuple[str, str]]:
        document_part, fragment = urldefrag(reference)
        if document_part:
            target_source = resolve_reference_source(current_source, document_part)
            target_document = self.load_document(target_source)
        else:
            target_source = current_source
            target_document = current_document
        target = resolve_json_pointer(target_document, fragment)
        return target, target_document, target_source, (target_source, fragment)

    def load_document(self, source: str) -> dict[str, Any]:
        cached = self.documents.get(source)
        if cached is not None:
            return cached
        if len(self.documents) >= MAX_REFERENCE_DOCUMENTS:
            raise ValueError(f"OpenAPI 外部引用文档超过 {MAX_REFERENCE_DOCUMENTS} 个限制")
        if source.startswith(("http://", "https://")):
            text = fetch_text_limited(
                source,
                headers={"Accept": "application/json,application/yaml,text/yaml,*/*"},
                max_bytes=MAX_REFERENCE_BYTES,
            )
            document = parse_document_text(text, source=source)
        else:
            path = source_path(source)
            resolved = path.resolve()
            if self.local_root is not None:
                try:
                    resolved.relative_to(self.local_root.resolve())
                except ValueError as exc:
                    raise ValueError(f"OpenAPI 外部引用越过主文档目录：{resolved}") from exc
            if not path.exists():
                raise ValueError(f"OpenAPI 外部引用文件不存在：{path}")
            ensure_reference_size_from_path(path)
            document = parse_document_text(read_file(path, MAX_REFERENCE_BYTES).decode("utf-8"), source=str(path))
        self.documents[source] = document
        return document


def parse_openapi(document: dict[str, Any], *, source: str = "") -> list[Endpoint]:
    resolver = ReferenceResolver(document, source)
    endpoints: list[Endpoint] = []
    paths = document.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("OpenAPI paths 必须是对象")
    root_security = document.get("security", [])
    for path, raw_path_item in paths.items():
        path_item = resolver.resolve(raw_path_item)
        if not isinstance(path_item, dict):
            continue
        path_parameters = resolved_parameters(path_item.get("parameters", []), resolver)
        for method, raw_operation in path_item.items():
            if method.lower() not in HTTP_METHODS:
                continue
            operation = resolver.resolve(raw_operation)
            if not isinstance(operation, dict):
                continue
            operation_parameters = resolved_parameters(operation.get("parameters", []), resolver)
            parameters = merge_parameters(path_parameters, operation_parameters)
            request_body = resolver.resolve(operation.get("requestBody")) if operation.get("requestBody") else None
            if document.get("swagger"):
                parameters, request_body = normalize_swagger2_request(
                    parameters,
                    request_body,
                    consumes=operation.get("consumes") or document.get("consumes") or ["application/json"],
                )
            responses = resolver.resolve(operation.get("responses", {}))
            security = resolver.resolve(operation["security"]) if "security" in operation else resolver.resolve(root_security)
            endpoints.append(
                Endpoint(
                    method=method.upper(),
                    path=str(path),
                    summary=str(operation.get("summary") or ""),
                    description=str(operation.get("description") or ""),
                    parameters=resolver.bind_schemas(parameters),
                    request_body=resolver.bind_schemas(request_body) if isinstance(request_body, dict) else None,
                    responses=resolver.bind_schemas(responses) if isinstance(responses, dict) else {},
                    tags=[str(tag) for tag in operation.get("tags", []) if str(tag)],
                    operation_id=str(operation.get("operationId") or ""),
                    deprecated=bool(operation.get("deprecated", False)),
                    security=security if isinstance(security, list) else [],
                )
            )
    return endpoints


def resolved_parameters(value: Any, resolver: ReferenceResolver) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    parameters = []
    for raw_parameter in value:
        parameter = resolver.resolve(raw_parameter)
        if isinstance(parameter, dict):
            parameters.append(normalize_parameter(parameter))
    return parameters


def normalize_parameter(parameter: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(parameter)
    if "schema" not in normalized and normalized.get("in") not in {"body", "formData"}:
        schema_keys = {
            "type", "format", "enum", "default", "example", "minimum", "maximum",
            "minLength", "maxLength", "pattern", "items", "collectionFormat",
        }
        schema = {key: normalized[key] for key in schema_keys if key in normalized}
        if schema:
            normalized["schema"] = schema
    return normalized


def merge_parameters(path_parameters: list[dict[str, Any]], operation_parameters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for parameter in [*path_parameters, *operation_parameters]:
        key = (str(parameter.get("name") or ""), str(parameter.get("in") or ""))
        if key not in merged:
            order.append(key)
        merged[key] = parameter
    return [merged[key] for key in order]


def normalize_swagger2_request(
    parameters: list[dict[str, Any]],
    request_body: dict[str, Any] | None,
    *,
    consumes: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    body_parameters = [item for item in parameters if item.get("in") == "body"]
    form_parameters = [item for item in parameters if item.get("in") == "formData"]
    regular_parameters = [item for item in parameters if item.get("in") not in {"body", "formData"}]
    media_types = [str(item) for item in consumes] if isinstance(consumes, list) and consumes else ["application/json"]
    if body_parameters:
        body_parameter = body_parameters[-1]
        media_type = media_types[0]
        request_body = {
            "required": bool(body_parameter.get("required", False)),
            "description": body_parameter.get("description", ""),
            "content": {media_type: {"schema": body_parameter.get("schema", {})}},
        }
    elif form_parameters:
        properties: dict[str, Any] = {}
        required = []
        for parameter in form_parameters:
            name = str(parameter.get("name") or "")
            if not name:
                continue
            properties[name] = parameter.get("schema") or {
                key: parameter[key]
                for key in ("type", "format", "items", "enum", "default")
                if key in parameter
            }
            if parameter.get("required"):
                required.append(name)
        schema: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        media_type = "multipart/form-data" if "multipart/form-data" in media_types else media_types[0]
        request_body = {"required": bool(required), "content": {media_type: {"schema": schema}}}
    return regular_parameters, request_body


def normalize_source(source: str) -> str:
    source = str(source or "").strip()
    if not source:
        return ""
    if source.startswith(("http://", "https://")):
        return source
    if source.startswith("file://"):
        return str(source_path(source).resolve())
    return str(Path(source).expanduser().resolve())


def resolve_reference_source(current_source: str, reference: str) -> str:
    if reference.startswith(("http://", "https://")):
        return reference
    if current_source.startswith(("http://", "https://")):
        resolved = urljoin(current_source, reference)
        if not resolved.startswith(("http://", "https://")):
            raise ValueError(f"远程 OpenAPI 文档不能引用本地文件或非 HTTP 资源：{reference}")
        return resolved
    if reference.startswith("file://"):
        return str(source_path(reference).resolve())
    if not current_source:
        raise ValueError(f"OpenAPI 外部引用缺少主文档来源，无法解析：{reference}")
    candidate = Path(reference).expanduser()
    if candidate.is_absolute():
        return str(candidate.resolve())
    return str((Path(current_source).parent / candidate).resolve())


def source_path(source: str) -> Path:
    if source.startswith("file://"):
        parsed = urlparse(source)
        return Path(unquote(parsed.path))
    return Path(source).expanduser()


def resolve_json_pointer(document: Any, fragment: str) -> Any:
    if not fragment:
        return document
    pointer = unquote(fragment)
    if not pointer.startswith("/"):
        raise ValueError(f"OpenAPI $ref 片段不是合法 JSON Pointer：#{fragment}")
    current = document
    for raw_part in pointer[1:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise ValueError(f"OpenAPI $ref 指向不存在的位置：#{fragment}")
    return current


def ensure_reference_size(content: bytes, source: str) -> None:
    if len(content) > MAX_REFERENCE_BYTES:
        raise ValueError(f"OpenAPI 引用文档超过 {MAX_REFERENCE_BYTES} 字节限制：{source}")


def ensure_reference_size_from_path(path: Path) -> None:
    if path.stat().st_size > MAX_REFERENCE_BYTES:
        raise ValueError(f"OpenAPI 引用文档超过 {MAX_REFERENCE_BYTES} 字节限制：{path}")
