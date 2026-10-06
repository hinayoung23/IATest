"""One redaction policy for reports, persistence, exports and model evidence."""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SENSITIVE_KEYS = ("authorization", "token", "api_key", "apikey", "secret", "password", "cookie")
_JSON_STRING = re.compile(r'''(["']([^"']+)["']\s*:\s*)("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')''')


def sensitive_key(key: str) -> bool:
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key)).lower().replace("-", "_")
    parts = normalized.split("_")
    token_key = "token" in parts and parts[-1] not in {"count", "limit", "usage", "budget", "cost", "estimate", "length", "type"}
    return any(part in normalized for part in SENSITIVE_KEYS if part != "token") or token_key


def redact_url(value: str, replacement: str = "[REDACTED]") -> str:
    try:
        parts = urlsplit(value)
        query = [(key, replacement if sensitive_key(key) else item) for key, item in parse_qsl(parts.query, keep_blank_values=True)]
        netloc = parts.netloc
        if parts.username is not None:
            netloc = replacement + "@" + netloc.rsplit("@", 1)[-1]
        fragment = re.sub(r"(^|&)([A-Za-z][A-Za-z0-9_-]*)=([^&]*)", lambda match: match[1] + match[2] + "=" + (replacement if sensitive_key(match[2]) else match[3]), parts.fragment)
        return urlunsplit((parts.scheme, netloc, parts.path, urlencode(query), fragment))
    except ValueError:
        return replacement


def redact_text(value: str, replacement: str = "[REDACTED]") -> str:
    text = str(value or "")
    text = re.sub(r"https?://[^\s<>\"'`]+", lambda match: redact_url(match[0], replacement), text)
    text = re.sub(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", lambda match: match[1] + " " + replacement, text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", lambda _: "sk-" + replacement, text)
    text = _JSON_STRING.sub(
        lambda match: match[1] + json.dumps(replacement) if sensitive_key(match[2]) else match[0], text,
    )
    text = re.sub(r'("([^"\\]+)"\s*:\s*)(-?\d+(?:\.\d+)?|true|false|null)\b', lambda match: match[1] + json.dumps(replacement) if sensitive_key(match[2]) else match[0], text)
    # Query strings and form bodies can also appear inside exception messages.
    text = re.sub(
        r"([?#&\s]|^)([A-Za-z][A-Za-z0-9_-]*)=([^&\s\"'<>]*)",
        lambda match: match[1] + match[2] + "=" + (replacement if sensitive_key(match[2]) else match[3]),
        text,
    )
    return text


def redact(value: Any, replacement: str = "[REDACTED]") -> Any:
    secrets: set[str] = set()

    def collect(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if sensitive_key(str(key)) and isinstance(child, str) and len(child) >= 4:
                    secrets.add(child)
                    if child.lower().startswith("bearer "):
                        secrets.add(child[7:])
                if sensitive_key(str(key)) and isinstance(child, dict):
                    def literals(value):
                        if isinstance(value, str) and len(value) >= 4:
                            secrets.add(value)
                        elif isinstance(value, (list, tuple)):
                            for element in value:
                                literals(element)
                        elif isinstance(value, dict):
                            for element in value.values():
                                literals(element)
                    def schema_literals(value):
                        if isinstance(value, dict):
                            for name, element in value.items():
                                if name in {"example", "examples", "enum", "const", "default"}:
                                    literals(element)
                                else:
                                    schema_literals(element)
                        elif isinstance(value, list):
                            for element in value:
                                schema_literals(element)
                    schema_literals(child)
                collect(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                collect(child)
        elif isinstance(item, str):
            if item.lstrip().startswith(("{", "[")):
                try:
                    collect(json.loads(item))
                except (ValueError, RecursionError):
                    pass

    collect(value)

    def clean(item: Any, *, schema: bool = False, definitions: bool = False, secret_schema: bool = False) -> Any:
        if isinstance(item, dict):
            return {str(key): (
                clean(child, schema=True, secret_schema=sensitive_key(str(key))) if definitions else
                replacement if secret_schema and key in {"example", "examples", "enum", "const", "default"} else
                clean(child, definitions=True) if schema and key in {"properties", "patternProperties", "$defs", "definitions"} else
                clean(child, schema=True) if key == "schema" or (key == "expected" and item.get("type") == "json_schema") else
                replacement if sensitive_key(str(key)) else clean(child, schema=schema, secret_schema=secret_schema)
            ) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [clean(child, schema=schema, secret_schema=secret_schema) for child in item]
        if isinstance(item, str):
            item = item.encode("utf-8", errors="replace").decode("utf-8")
            text = redact_url(item, replacement) if item.startswith(("http://", "https://")) else redact_text(item, replacement)
            for secret in sorted(secrets, key=len, reverse=True):
                if secret and secret != replacement:
                    text = text.replace(secret, replacement)
            return text
        return item

    return clean(value)
