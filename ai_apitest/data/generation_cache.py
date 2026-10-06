"""Reuse independent endpoint groups, including their dependency contracts."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from ai_apitest.core.models import Endpoint, TestCase
from ai_apitest.core.paths import project_runtime_dir, project_metadata_file
from ai_apitest.core.serde import read_json, write_json, case_from_dict
from ai_apitest.execution.provenance import fingerprint


def endpoint_key(endpoint: Endpoint) -> tuple[str, str]:
    return endpoint.method.upper(), endpoint.path


def case_keys(case: TestCase) -> set[tuple[str, str]]:
    keys = {(step.method.upper(), step.path) for step in [*case.setup_steps, *case.steps, *case.cleanup_steps]}
    if case.method.upper() != "CHAIN":
        keys.add((case.method.upper(), case.path))
    return keys


def dependency_groups(endpoints: list[Endpoint], graph: dict, cases: list[TestCase] | None = None) -> list[list[Endpoint]]:
    from ai_apitest.generator.cases import dependency_producers, likely_produced_fields
    keys = {endpoint_key(endpoint) for endpoint in endpoints}
    neighbors = {key: set() for key in keys}
    def connect(members):
        members = set(members) & keys
        for key in members:
            neighbors[key].update(members - {key})
    for edge in graph.get("edges", []):
        if isinstance(edge, dict):
            connect((tuple(str(edge.get(name, {}).get(field, "")).upper() if field == "method" else str(edge.get(name, {}).get(field, "")) for field in ("method", "path")) for name in ("from", "to")))
    for endpoint in endpoints:
        for rule in endpoint.business_rules:
            steps = (rule.get("trigger") or {}).get("chain_steps", [])
            connect([endpoint_key(endpoint), *((str(step.get("method", "")).upper(), str(step.get("path", ""))) for step in steps)])
    for producer in dependency_producers(endpoints):
        fields = likely_produced_fields(producer)
        for consumer in endpoints:
            if fields.intersection(str(param.get("name", "")) for param in consumer.parameters):
                connect([endpoint_key(producer), endpoint_key(consumer)])
    for case in cases or []:
        connect(case_keys(case))
    groups, seen = [], set()
    for key in sorted(keys):
        if key in seen:
            continue
        pending, component = [key], set()
        while pending:
            member = pending.pop()
            if member in component:
                continue
            component.add(member)
            pending.extend(neighbors[member] - component)
        seen.update(component)
        groups.append([endpoint for endpoint in endpoints if endpoint_key(endpoint) in component])
    return groups


class IncrementalGenerationCache:
    def __init__(self, root: Path, project: str, endpoints: list[Endpoint], config: dict, llm: dict) -> None:
        from ai_apitest.ai.workflows import case_generation_fingerprint
        self.path = project_runtime_dir(root, project) / "generation-scopes.json"
        try:
            self.payload = read_json(self.path, {})
        except (ValueError, OSError):
            self.payload = {}
        if not isinstance(self.payload, dict) or not isinstance(self.payload.get("entries", {}), dict):
            self.payload = {}
        graph = read_json(project_metadata_file(root, project), {}).get("dependency_graph", {})
        previous_cases = []
        for entry in self.payload.get("entries", {}).values():
            if not isinstance(entry, dict):
                continue
            try:
                previous_cases.extend(case_from_dict(item) for item in entry.get("cases", []))
            except (KeyError, TypeError, ValueError):
                continue
        self.groups = dependency_groups(endpoints, graph, previous_cases)
        self.signatures = {fingerprint(sorted(endpoint_key(endpoint) for endpoint in group)): case_generation_fingerprint(root, project, group, config, llm, scoped=True) for group in self.groups}

    def load(self) -> tuple[list[TestCase], list[Endpoint]]:
        from ai_apitest.ai.workflows import case_quality_gate_issues
        reused, missing = [], []
        for group in self.groups:
            identifier = fingerprint(sorted(endpoint_key(endpoint) for endpoint in group))
            entry = self.payload.get("entries", {}).get(identifier, {})
            if not isinstance(entry, dict):
                missing.extend(group)
                continue
            raw_cases = entry.get("cases", [])
            if entry.get("fingerprint") != self.signatures[identifier] or entry.get("content_hash") != fingerprint(raw_cases):
                missing.extend(group)
                continue
            try:
                cases = [case_from_dict(item) for item in raw_cases]
                if not cases or case_quality_gate_issues(cases, group):
                    raise ValueError("缓存未通过本地门禁")
                reused.extend(cases)
            except (KeyError, TypeError, ValueError):
                missing.extend(group)
        return reused, missing

    def save(self, cases: list[TestCase]) -> None:
        entries = {}
        edges = [{"from": {"method": group[0].method, "path": group[0].path}, "to": {"method": endpoint.method, "path": endpoint.path}} for group in self.groups for endpoint in group[1:]]
        for group in dependency_groups([endpoint for group in self.groups for endpoint in group], {"edges": edges}, cases):
            keys = {endpoint_key(endpoint) for endpoint in group}
            identifier = fingerprint(sorted(keys))
            # Newly observed cross-endpoint cases are conservatively regenerated next time.
            signature = self.signatures.get(identifier)
            if signature is None:
                continue
            members = [asdict(case) for case in cases if case_keys(case) and case_keys(case).issubset(keys)]
            if members:
                entries[identifier] = {"fingerprint": signature, "content_hash": fingerprint(members), "cases": members}
        write_json(self.path, {"version": 1, "entries": entries})
