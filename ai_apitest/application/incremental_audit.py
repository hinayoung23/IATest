"""Conservative change closure for previously approved case collections."""
from __future__ import annotations

from dataclasses import fields

from ai_apitest.agents.token_optimization import canonical, content_hash
from ai_apitest.core.models import TestCase
from ai_apitest.core.serde import case_from_dict
from ai_apitest.domain.case_drafts import case_execution_signature


def semantic_evidence(evidence):
    if not isinstance(evidence, dict):
        return evidence
    # These still appear in the current audit. Coverage is recomputed globally;
    # generation diagnostics describe the producer, not immutable contracts.
    return {key: value for key, value in evidence.items() if key not in {"generation_diagnostics", "coverage_manifest"}}


def case_links(case):
    if set(case) - {field.name for field in fields(TestCase)}:
        raise ValueError("未知用例字段可能包含关联依赖")
    links = set()
    requests = [case, *case.get("steps", []), *case.get("setup_steps", []), *case.get("cleanup_steps", [])]
    for request in requests:
        if not isinstance(request, dict):
            raise ValueError("未知步骤结构")
        method, path = str(request.get("method", "")).upper(), request.get("path", "")
        if method == "CHAIN":
            continue
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"} or not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("未知接口结构")
        links.add(("endpoint", method, path))
    for field in ("rule_ids", "fixture_requirements", "resources", "coverage_partition_ids"):
        if not isinstance(case.get(field, []), list):
            raise ValueError("未知依赖结构")
        links.update((field, str(value)) for value in case.get(field, []) if value)
    if case.get("dataset_id"):
        links.add(("dataset", str(case["dataset_id"])))
    if case.get("identity"):
        links.add(("identity", str(case["identity"])))
    return links


def incremental_bundle(bundle: dict, baseline: dict) -> tuple[dict, dict] | None:
    """Reuse a same-judge baseline only if we can prove an independent closure.

    Global evidence remains complete. Every unchanged case has a content and
    execution hash; all cases sharing any affected endpoint/fixture/rule are
    re-reviewed. Unsupported, broad or evidence-changing edits use a full audit.
    """
    if bundle.get("artifact_type") != "test_cases" or baseline.get("depth", 0) >= 3:
        return None
    contract = (bundle.get("evidence") or {}).get("trusted_contract", {}) if isinstance(bundle.get("evidence"), dict) else {}
    if not contract.get("contract_hash") or contract.get("contract_status") not in {"trusted", "trusted_manual", "trusted_legacy"}:
        return None
    previous = baseline.get("bundle", {})
    if previous.get("rubric_version") != bundle.get("rubric_version"):
        return None
    if canonical(semantic_evidence(previous.get("evidence"))) != canonical(semantic_evidence(bundle.get("evidence"))):
        return None
    old, new = previous.get("artifact"), bundle.get("artifact")
    if not isinstance(old, list) or not isinstance(new, list) or min(len(old), len(new)) < 10:
        return None
    try:
        def index(items):
            result = {item["id"]: item for item in items}
            if len(result) != len(items) or any(not isinstance(key, str) or not key for key in result):
                raise ValueError("用例 ID 不唯一")
            return result
        old_by_id, new_by_id = index(old), index(new)
        changed = {key for key in old_by_id.keys() | new_by_id.keys() if canonical(old_by_id.get(key)) != canonical(new_by_id.get(key))}
        if not changed or len(changed) > max(1, len(new) // 4):
            return None
        # Include old and new links to handle moves and deletions safely.
        links = {key: case_links(old_by_id[key]) if key in old_by_id else set() for key in old_by_id.keys() | new_by_id.keys()}
        for key, case in new_by_id.items():
            links[key].update(case_links(case))
        affected = set(changed)
        while True:
            closure = set().union(*(links[key] for key in affected))
            expanded = affected | {key for key, value in links.items() if value & closure}
            if expanded == affected:
                break
            affected = expanded
        if len(affected) > len(new) * 0.4:
            return None
        global_index = []
        signatures = set()
        for key, case in new_by_id.items():
            signature = content_hash(case_execution_signature(case_from_dict(case)))
            if signature in signatures:
                return None
            signatures.add(signature)
            global_index.append({"id": key, "title": case.get("title", ""), "method": case.get("method"), "path": case.get("path"), "rule_ids": case.get("rule_ids", []), "fixture_requirements": case.get("fixture_requirements", []), "runnable": case.get("runnable", True), "content_sha256": content_hash(case), "execution_sha256": signature, "unchanged_approved": key not in affected})
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    scope = {"mode": "incremental", "baseline_candidate_id": baseline["candidate_id"], "baseline_judge_key": baseline["judge_key"], "baseline_dimensions": baseline["dimensions"], "baseline_depth": baseline.get("depth", 0), "changed_ids": sorted(changed), "affected_ids": sorted(affected), "deleted_ids": sorted(old_by_id.keys() - new_by_id.keys()), "reused_cases": len(new_by_id.keys() - affected), "total_cases": len(new)}
    result = {**bundle, "artifact": [case for key, case in new_by_id.items() if key in affected], "audit_scope": scope, "global_case_index": global_index, "instruction": "这是同一审计模型已批准基线的增量复审。证据契约未变，未受影响的用例内容和执行哈希均未变。检查全部变动及关联用例、删除影响、当前完整证据、全局覆盖和重复索引；给出整份候选的评分。不得将未列出的用例视为缺失；发现无法依据基线和当前事实确认全局质量时明确提出问题。"}
    # A small candidate can be cheaper to send in full.
    return (result, scope) if len(canonical(result)) < len(canonical(bundle)) * 0.85 else None
