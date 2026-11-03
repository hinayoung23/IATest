"""Scoped crawler, immutable declarative rule catalogues and evidence-checked chains."""

from __future__ import annotations

import json
import posixpath
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from iatest.application.engine_analysis import digest
from iatest.engines.contracts import EngineResult
from iatest.engines.http import (
    REQUEST_SCHEMA,
    HTTPExecutor,
    NetworkScope,
    resolve_secrets,
    validate,
)
from iatest.engines.store import EngineStore
from iatest.execution.assertions import MISSING, json_path_value

MATCH = {
    "type": "object",
    "additionalProperties": False,
    "minProperties": 1,
    "properties": {
        "status": {
            "type": "array",
            "minItems": 1,
            "maxItems": 100,
            "items": {"type": "integer", "minimum": 100, "maximum": 599},
        },
        "json_path": {"type": "string", "maxLength": 500},
        "equals": {},
        "text_contains": {"type": "string", "minLength": 1, "maxLength": 1000},
    },
}
LIBRARY = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "version", "source", "rules"],
    "properties": {
        "id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,60}$"},
        "version": {"type": "string", "minLength": 1, "maxLength": 80},
        "source": {"type": "string", "minLength": 1, "maxLength": 2000},
        "rules": {
            "type": "array",
            "minItems": 1,
            "maxItems": 100,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "title", "severity", "chain"],
                "properties": {
                    "id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,60}$"},
                    "title": {"type": "string", "minLength": 1, "maxLength": 300},
                    "severity": {"enum": ["low", "medium", "high", "critical"]},
                    "references": {
                        "type": "array",
                        "maxItems": 10,
                        "items": {"type": "string", "maxLength": 2048},
                    },
                    "cve": {"type": "string", "pattern": "^CVE-[0-9]{4}-[0-9]{4,}$"},
                    "chain": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 8,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["request"],
                            "properties": {
                                "request": REQUEST_SCHEMA,
                                "control": MATCH,
                                "finding": MATCH,
                                "extract": {
                                    "type": "object",
                                    "maxProperties": 10,
                                    "additionalProperties": {
                                        "type": "string",
                                        "maxLength": 500,
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
    },
}
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["allowed_origins", "scan"],
    "properties": {
        "allowed_origins": {
            "type": "array",
            "minItems": 1,
            "maxItems": 32,
            "items": {"type": "string"},
        },
        "allow_mutation": {"type": "boolean"},
        "scan": {
            "type": "object",
            "additionalProperties": False,
            "required": ["roots", "path_prefixes", "library_id", "library_digest"],
            "properties": {
                "roots": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 10,
                    "items": {"type": "string", "maxLength": 8192},
                },
                "path_prefixes": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 20,
                    "items": {"type": "string", "pattern": "^/", "maxLength": 1000},
                },
                "library_id": {"type": "string", "maxLength": 80},
                "library_digest": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                "max_pages": {"type": "integer", "minimum": 1, "maximum": 100},
                "max_depth": {"type": "integer", "minimum": 0, "maximum": 5},
            },
        },
    },
}


def import_library(root, project, value):
    validate(value, LIBRARY)
    if len(json.dumps(value).encode()) > 1048576:
        raise ValueError("Rule catalogue exceeds 1 MiB")
    if len({r["id"] for r in value["rules"]}) != len(value["rules"]):
        raise ValueError("Duplicate security rule ID")
    for rule in value["rules"]:
        if "finding" not in rule["chain"][-1] or not any(
            "control" in s for s in rule["chain"][:-1]
        ):
            raise ValueError(
                "A verification chain needs a positive control before its final finding predicate"
            )
        for step in rule["chain"]:
            if ("control" in step) == ("finding" in step):
                raise ValueError(
                    "Each chain step must have exactly one explicit predicate"
                )
            predicate = step.get("control", step.get("finding"))
            if "finding" in step and not (
                {"json_path", "text_contains"} & predicate.keys()
            ):
                raise ValueError(
                    "A confirmed finding needs a content/resource predicate, not HTTP status alone"
                )
            if ("json_path" in predicate) != ("equals" in predicate):
                raise ValueError("JSON predicate requires path and expected value")
    frozen = {**value, "catalogue_id": value["id"], "sha256": digest(value)}
    identifier = value["id"] + "-" + digest(value)[:16]
    EngineStore(root, project).save_artifact(
        "security_library", frozen, artifact_id=identifier
    )
    return {**frozen, "artifact_id": identifier}


def validate_scan(plan):
    validate(plan.spec, SCHEMA)


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a" and len(self.links) < 200:
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)


def matches(response, predicate):
    if response["truncated"]:
        raise ValueError("Truncated response cannot prove a security predicate")
    if "status" in predicate and response["status_code"] not in predicate["status"]:
        return False
    if (
        "text_contains" in predicate
        and predicate["text_contains"] not in response["text"]
    ):
        return False
    if "json_path" in predicate:
        if not response["json_valid"]:
            return False
        if (
            json_path_value(response["json"], predicate["json_path"], missing=MISSING)
            != predicate["equals"]
        ):
            return False
    return True


class SecurityScan:
    def __init__(self, root, project):
        self.root, self.project = root, project

    def execute(self, plan, context):
        validate_scan(plan)
        spec = plan.spec
        settings = spec["scan"]
        scope = NetworkScope(self.root, self.project, spec["allowed_origins"])
        library = EngineStore(self.root, self.project).get_artifact(
            "security_library", settings["library_id"]
        )
        if library["sha256"] != settings["library_digest"]:
            raise ValueError("Security catalogue digest differs")
        document = {
            **{
                k: v
                for k, v in library.items()
                if k not in {"sha256", "catalogue_id", "id"}
            },
            "id": library["catalogue_id"],
        }
        validate(document, LIBRARY)
        if digest(document) != library["sha256"]:
            raise ValueError("Security catalogue contents changed")
        for rule in library["rules"]:
            if any(
                s["request"].get("method", "GET") not in {"GET", "HEAD", "OPTIONS"}
                for s in rule["chain"]
            ) and not spec.get("allow_mutation"):
                raise ValueError(
                    "Writing verification chains require allow_mutation=true"
                )

        def allowed(url):
            scope.check(url)
            path = posixpath.normpath(unquote(urlsplit(url).path))
            return any(
                path == p.rstrip("/") or path.startswith(p.rstrip("/") + "/")
                for p in settings["path_prefixes"]
            )

        pending = [(url, 0) for url in settings["roots"]]
        seen = set()
        urls = []
        client = HTTPExecutor(scope, context.control)
        result = EngineResult(plan.id, plan.kind)
        findings = []
        unassessed = []
        start = context.control.request_count()
        try:
            while pending and len(urls) < settings.get("max_pages", 20):
                context.control.check()
                url, depth = pending.pop(0)
                url = urlunsplit(urlsplit(url)._replace(fragment=""))
                if url in seen:
                    continue
                if not allowed(url):
                    raise ValueError(
                        "Crawler root/link exceeds the explicit path scope"
                    )
                seen.add(url)
                response = client.request({"url": url}, max_bytes=1048576)
                if response["truncated"]:
                    unassessed.append(
                        {"url_sha256": digest(url), "reason": "crawler body truncated"}
                    )
                    continue
                urls.append(url)
                if depth < settings.get("max_depth", 2) and "html" in response[
                    "headers"
                ].get("content-type", ""):
                    parser = Links()
                    parser.feed(response["text"])
                    for href in parser.links:
                        link = urljoin(url, href)
                        try:
                            inside = allowed(link)
                        except (ValueError, PermissionError):
                            inside = False
                        if inside and link not in seen and len(pending) < 200:
                            pending.append((link, depth + 1))
            if pending:
                unassessed.append({"reason": "crawl page/queue budget exhausted"})
            for url in urls:
                for rule in library["rules"]:
                    variables = {
                        "url": url,
                        "origin": urlunsplit(
                            urlsplit(url)._replace(path="", query="", fragment="")
                        ),
                    }
                    evidence = []
                    controls = True
                    for index, step in enumerate(rule["chain"]):
                        from iatest.execution.runner import render_template

                        request = render_template(
                            resolve_secrets(step["request"]), variables
                        )
                        if not allowed(request["url"]):
                            raise ValueError("Security chain exceeds path scope")
                        response = client.request(request)
                        evidence.append(
                            {
                                "index": index,
                                "status": response["status_code"],
                                "sha256": digest(response["text"]),
                            }
                        )
                        for name, path in step.get("extract", {}).items():
                            value = (
                                json_path_value(response["json"], path, missing=MISSING)
                                if response["json_valid"]
                                else MISSING
                            )
                            if value is MISSING or not isinstance(
                                value, (str, int, float, bool)
                            ):
                                raise ValueError(
                                    "Security chain extraction is absent or non-scalar"
                                )
                            variables[name] = value
                        matched = matches(
                            response, step.get("control", step.get("finding"))
                        )
                        if "finding" in step and not matched:
                            break
                        if "control" in step and not matched:
                            controls = False
                            unassessed.append(
                                {
                                    "rule": rule["id"],
                                    "reason": "trusted control not reproduced",
                                }
                            )
                            break
                        if "finding" in step and matched:
                            if index != len(rule["chain"]) - 1:
                                continue
                            findings.append(
                                {
                                    "rule": rule["id"],
                                    "title": rule["title"],
                                    "severity": rule["severity"],
                                    "cve": rule.get("cve"),
                                    "resource_sha256": digest(url),
                                    "evidence": evidence,
                                    "catalogue_sha256": library["sha256"],
                                }
                            )
                    result.steps.append(
                        {
                            "rule": rule["id"],
                            "resource_sha256": digest(url),
                            "controls_reproduced": controls,
                            "evidence": evidence,
                        }
                    )
            result.verdict = (
                "failed" if findings else "inconclusive" if unassessed else "passed"
            )
            result.metrics = {
                "pages": len(urls),
                "catalogue_sha256": library["sha256"],
                "findings": findings,
                "not_assessed": unassessed,
                "requests": context.control.request_count() - start,
            }
            result.limitations = [
                "Only explicit scoped pages and reviewed catalogue rules; a version/CVE label alone is not an exploit proof"
            ]
        finally:
            client.close()
        return result
