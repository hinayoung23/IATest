"""Provider-independent, lossless reductions of model input."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from typing import Any


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class TokenOptimization:
    enabled: bool = True
    evidence_dedup: bool = True
    prefetch: bool = True
    incremental_audit: bool = True
    adaptive_batches: bool = True
    prefetch_max_tokens: int = 18000
    batch_input_tokens: int = 18000
    audit_output_tokens: int = 6000

    @classmethod
    def from_config(cls, agents: dict) -> "TokenOptimization":
        raw = agents.get("token_optimization") or {}
        values = {}
        for name in ("enabled", "evidence_dedup", "prefetch", "incremental_audit", "adaptive_batches"):
            values[name] = bool(raw.get(name, True))
        for name, default in (("prefetch_max_tokens", 18000), ("batch_input_tokens", 18000), ("audit_output_tokens", 6000)):
            try:
                values[name] = max(1024, min(64000, int(raw.get(name, default))))
            except (TypeError, ValueError):
                values[name] = default
        return cls(**values)


FACT_FORMAT = "iatest-facts/1"
FACT_REF = "$iatest_fact"


def pack_evidence(value: Any, *, min_chars: int = 400) -> tuple[Any, dict]:
    """Replace repeated JSON subtrees with inline facts; preserve *every* value.

    Reserved-key collisions fall back to the original JSON. Facts are expanded
    before returning to prove equivalence; a larger representation is discarded.
    """
    counts: Counter[str] = Counter()
    nodes: dict[str, Any] = {}
    collision = False

    def scan(node):
        nonlocal collision
        if isinstance(node, dict):
            collision |= FACT_REF in node
            children = node.values()
        elif isinstance(node, list):
            children = node
        else:
            children = ()
        if isinstance(node, (dict, list, str)):
            encoded = canonical(node)
            if len(encoded) >= min_chars:
                digest = hashlib.sha256(encoded.encode()).hexdigest()
                counts[digest] += 1
                nodes[digest] = node
        for child in children:
            scan(child)

    original = canonical(value)
    scan(value)
    facts = {}

    def transform(node):
        if isinstance(node, (dict, list, str)):
            encoded = canonical(node)
            if len(encoded) >= min_chars:
                digest = hashlib.sha256(encoded.encode()).hexdigest()
                if counts[digest] > 1:
                    # Definitions remain literal JSON, so refs never form cycles.
                    facts[digest] = nodes[digest]
                    return {FACT_REF: digest}
        if isinstance(node, dict):
            return {key: transform(child) for key, child in node.items()}
        if isinstance(node, list):
            return [transform(child) for child in node]
        return node

    packed = {"format": FACT_FORMAT, "root": transform(value), "facts": facts} if not collision else value
    used = bool(facts) and not collision and len(canonical(packed)) < len(original)
    if used and canonical(unpack_evidence(packed)) != original:
        raise ValueError("事实字典不能无损还原证据")
    result = packed if used else value
    return result, {"format": FACT_FORMAT if used else "json", "original_chars": len(original), "wire_chars": len(canonical(result)), "fact_count": len(facts) if used else 0, "original_sha256": hashlib.sha256(original.encode()).hexdigest()}


def unpack_evidence(value: Any) -> Any:
    if not isinstance(value, dict) or value.get("format") != FACT_FORMAT:
        return value
    facts = value["facts"]

    def expand(node):
        if isinstance(node, dict):
            if set(node) == {FACT_REF}:
                return facts[node[FACT_REF]]
            return {key: expand(child) for key, child in node.items()}
        if isinstance(node, list):
            return [expand(child) for child in node]
        return node

    return expand(value["root"])


def prefetch_plan(role_id, context, active_tools, policy, available_tokens):
    """Fixed facts only: no model-selected tools and no write/execute actions."""
    from ai_apitest.agents.runtime import estimate_tokens
    limit = min(policy.prefetch_max_tokens, available_tokens)
    if role_id == "quality_reviewer":
        text = str(context.state.get("audit_artifact_text") or "")
        if not text or estimate_tokens(text) + 1024 > limit:
            return []
        plan = [("get_audit_artifact_info", {})]
        plan += [("read_audit_artifact_chunk", {"offset": offset, "max_chars": 180000}) for offset in range(0, len(text), 180000)]
    elif role_id == "case_designer":
        endpoints = context.data.endpoints.all()
        if not endpoints or len(endpoints) > 8:
            return []
        from ai_apitest.core.serde import to_jsonable
        if estimate_tokens(canonical(to_jsonable(endpoints))) + 1024 > limit:
            return []
        plan = [("get_endpoint_detail", {"method": endpoint.method.upper(), "path": endpoint.path}) for endpoint in endpoints]
    else:
        return []
    return plan if all(name in active_tools for name, _ in plan) else []
