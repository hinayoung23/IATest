"""Explicit live A/B acceptance on isolated, synthetic API contracts.

Run from the repository: .venv/bin/python scripts/validate_token_optimization.py
Uses existing credentials and all configured independent judges. Does not call
business endpoints or write to the source project's data/configuration.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import tempfile
import time

from ai_apitest.application.quality_audit import QualityAuditService
from ai_apitest.core.config import load_config, save_config
from ai_apitest.core.models import Assertion, Endpoint, TestCase
from ai_apitest.core.serde import case_from_dict, to_jsonable, write_json
from ai_apitest.data.token_usage import token_statistics
from ai_apitest.domain.case_drafts import validate_compiled_cases


def fixture():
    common = {"type": "object", "required": ["ready", "dependencies", "uptime_seconds"], "properties": {
        "ready": {"type": "boolean"}, "uptime_seconds": {"type": "integer", "minimum": 0},
        "dependencies": {"type": "array", "items": {"type": "object", "required": ["name", "state"], "properties": {"name": {"type": "string", "minLength": 1}, "state": {"type": "string", "enum": ["available", "degraded", "unavailable"]}, "latency_ms": {"type": "number", "minimum": 0}, "checked_at": {"type": "string", "format": "date-time"}}}},
        "memory": {"type": "object", "properties": {"used_bytes": {"type": "integer", "minimum": 0}, "limit_bytes": {"type": "integer", "minimum": 0}}},
        "version": {"type": "string", "minLength": 1}, "region": {"type": "string"}, "instance_count": {"type": "integer", "minimum": 0},
    }}
    domains = ["inventory", "orders", "payments", "shipping", "catalog", "users", "sessions", "search", "notifications", "billing", "returns", "pricing", "coupons", "reviews", "analytics", "exports", "imports", "webhooks", "audit", "scheduler"]
    endpoints, cases = [], []
    for domain in domains:
        schema = {"type": "object", "required": ["service", "status"], "properties": {"service": {"type": "string", "const": domain}, "status": copy.deepcopy(common)}}
        endpoint = Endpoint(method="GET", path=f"/{domain}/health", description=f"独立的 {domain} 服务只读状态接口；服务之间不能互相替代。无鉴权和请求参数，仅声明 HTTP 200，响应 service 固定为 {domain}；status.ready 可为 true 或 false，依赖列表允许为空。", responses={"200": {"description": "服务状态", "content": {"application/json": {"schema": schema}}}})
        endpoints.append(endpoint)
        cases.append(TestCase(id=f"health-{domain}", scenario_id=f"health-{domain}-success", title=f"{domain} 服务状态符合独立契约", method="GET", path=endpoint.path, assertions=[Assertion(type="status_code", expected=200), Assertion(type="json_schema", expected=copy.deepcopy(schema))], expected_result=f"HTTP 200，service 为 {domain}，完整响应满足契约类型和约束。", parallel_safe=True, retry_safe=True))
    evidence = {"endpoints": to_jsonable(endpoints), "trusted_contract": {"contract_hash": "synthetic-health-contract-v1", "contract_version": "acceptance-v1", "contract_status": "trusted"}, "generation_strategies": {"enabled_default_strategies": ["happy_path"]}, "coverage_manifest": {"endpoint_count": 20, "covered_endpoints": [{"method": endpoint.method, "path": endpoint.path, "case_ids": [cases[i].id], "documented_status_codes": [200], "covered_status_codes": [200]} for i, endpoint in enumerate(endpoints)], "missing": []}, "local_gate_issues": []}
    return endpoints, to_jsonable(cases), evidence


def main():
    parser = argparse.ArgumentParser(description="真实模型 Token 优化 A/B 验收（使用现有模型配置）")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, default=Path(".iatest/token-acceptance/live.json"))
    parser.add_argument("--trials", type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.trials <= 3:
        parser.error("trials 必须为 1–3")
    source = load_config(args.root)
    endpoints, cases, evidence = fixture()
    report = {"version": 1, "mode": "live", "fixture": "synthetic-independent-health-contracts", "all_models_required": True, "results": []}
    for trial in range(1, args.trials + 1):
        for enabled in (False, True):
            with tempfile.TemporaryDirectory(prefix="iatest-token-acceptance-") as temporary:
                work = Path(temporary)
                config = copy.deepcopy(source)
                config["agents"]["token_optimization"]["enabled"] = enabled
                config["agents"]["recipes"] = {"paths": []}
                config["agents"]["model_routing"] = {"rules": []}
                save_config(work, config)
                service = QualityAuditService(work, "acceptance", "test_cases", endpoints=endpoints)
                if not service.models:
                    raise RuntimeError("没有可用审计模型，真实验收不能通过本地门禁代替")
                variants = [("full", copy.deepcopy(cases))]
                if enabled:
                    changed = copy.deepcopy(cases)
                    changed[0]["title"] = "inventory 服务状态满足完整响应契约"
                    variants.append(("changed", changed))
                    negative = copy.deepcopy(changed)
                    negative[1]["assertions"][0]["expected"] = 500
                    variants.append(("negative_changed", negative))
                for round_index, (name, candidate) in enumerate(variants, 1):
                    start = time.monotonic()
                    from ai_apitest.data.agent_runs import AgentRunRepository
                    repository = AgentRunRepository(work, "acceptance")
                    with repository.connection() as conn:
                        prior_ids = {row[0] for row in conn.execute("select id from agent_runs")}
                    # Deliberately bypass the caller's deterministic status gate
                    # only for the synthetic negative to exercise model rejection.
                    gates = [] if name == "negative_changed" else [{"severity": "high", "reason": issue.message, "location": issue.field} for issue in validate_compiled_cases([case_from_dict(case) for case in candidate], endpoints) if issue.severity == "error"]
                    result = service.evaluate(candidate, evidence=evidence, round_index=round_index, hard_gate_issues=gates)
                    with repository.connection() as conn:
                        runs = [dict(row) for row in conn.execute("select id, provider, model, input_tokens, output_tokens, total_tokens, turn_count, tool_call_count, status from agent_runs") if row["id"] not in prior_ids]
                    row = {"trial": trial, "optimization": enabled, "variant": name, "expected_pass": name != "negative_changed", "passed": result.passed, "audit_complete": result.audit_complete, "average_score": result.average_score, "scores": [{"model": score.model, "status": score.status, "dimensions": score.dimensions, "issues": score.issues, "scope": score.audit_scope, "error": score.error} for score in result.scores], "runs": runs, "tokens": sum(run["total_tokens"] for run in runs), "input_tokens": sum(run["input_tokens"] for run in runs), "output_tokens": sum(run["output_tokens"] for run in runs), "model_calls": sum(run["turn_count"] for run in runs), "elapsed_ms": round((time.monotonic() - start) * 1000)}
                    row["incremental_models"] = sum(score.audit_scope.get("mode") == "incremental" for score in result.scores)
                    row["acceptance_passed"] = row["passed"] == row["expected_pass"] and row["audit_complete"]
                    report["results"].append(row)
                    report["success"] = all(item["acceptance_passed"] for item in report["results"])
                    write_json(args.output, report)
                    print(json.dumps({key: row[key] for key in ("trial", "optimization", "variant", "passed", "audit_complete", "tokens", "model_calls", "elapsed_ms")}, ensure_ascii=False), flush=True)
                service.finalize(result, decision="acceptance", reason="合成样本真实模型验收")
                # Aggregate ledgers contain no credentials or source project data.
                report.setdefault("ledgers", []).append({"trial": trial, "optimization": enabled, **token_statistics(work, "acceptance")})
                write_json(args.output, report)
    comparisons = []
    for trial in range(1, args.trials + 1):
        before, after = [next(row for row in report["results"] if row["trial"] == trial and row["optimization"] == enabled and row["variant"] == "full") for enabled in (False, True)]
        comparisons.append({"trial": trial, "baseline_tokens": before["tokens"], "optimized_tokens": after["tokens"], "token_reduction_percent": round((1 - after["tokens"] / before["tokens"]) * 100, 2) if before["tokens"] else None, "baseline_model_calls": before["model_calls"], "optimized_model_calls": after["model_calls"], "baseline_elapsed_ms": before["elapsed_ms"], "optimized_elapsed_ms": after["elapsed_ms"]})
    report["full_candidate_comparisons"] = comparisons
    write_json(args.output, report)
    if not report["success"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
