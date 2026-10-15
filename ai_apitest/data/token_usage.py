"""Read-only usage summaries, including legacy records and unknown costs."""
from __future__ import annotations

import json
import sqlite3

from ai_apitest.ai.provider import usage_count
from ai_apitest.core.paths import project_agent_db


def json_object(value):
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def token_statistics(root, project, *, limit=500):
    if type(limit) is not int or not 1 <= limit <= 5000:
        raise ValueError("limit 必须为 1–5000")
    summary = {"runs": 0, "roles": [], "model_http_attempts": 0, "failed_model_calls": 0, "unknown_cost_calls": 0, "note": "缓存与推理是用量子集，不额外叠加 total_tokens；失败请求费用未知。输入分项为估算，不能等同供应商账单。"}
    db = project_agent_db(root, project)
    if not db.is_file():
        return summary
    with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        exists = conn.execute("select 1 from sqlite_master where name='agent_runs'").fetchone()
        if not exists:
            return summary
        rows = conn.execute("select * from agent_runs where project=? order by started_at desc, rowid desc limit ?", (project, limit)).fetchall()
        turn_columns = {value[1] for value in conn.execute("pragma table_info(agent_turns)")}
        tool_columns = {value[1] for value in conn.execute("pragma table_info(agent_tool_calls)")}
        groups = {}
        for row in rows:
            key = (row["role_id"], row["provider"], row["model"])
            group = groups.setdefault(key, {"role": key[0], "provider": key[1], "model": key[2], "runs": 0, **dict.fromkeys(("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "cache_write_tokens", "reasoning_tokens", "estimated_runs", "legacy_runs", "model_calls", "http_attempts", "http_retries", "failed_model_calls", "unknown_cost_calls", "estimated_system_tokens", "estimated_tool_schema_tokens", "estimated_input_tokens", "prefetched_tool_calls"), 0)})
            usage = json_object(row["usage_json"]) if "usage_json" in row.keys() else {}
            group["runs"] += 1
            group["estimated_runs"] += int(bool(usage.get("estimated")))
            group["legacy_runs"] += int(not usage)
            for field in ("input_tokens", "output_tokens", "total_tokens"):
                group[field] += usage_count(row[field])
            for field in ("cached_input_tokens", "cache_write_tokens", "reasoning_tokens"):
                group[field] += usage_count(usage.get(field))
            if {"request_metrics_json", "error"} <= turn_columns:
                prefetch = 0
                for turn in conn.execute("select request_metrics_json, error from agent_turns where run_id=?", (row["id"],)):
                    metrics = json_object(turn[0])
                    attempts = usage_count(metrics.get("http_attempts"))
                    group["model_calls"] += 1
                    group["http_attempts"] += attempts
                    group["http_retries"] += max(0, attempts - 1)
                    group["failed_model_calls"] += int(bool(turn[1]))
                    group["unknown_cost_calls"] += int(bool(metrics.get("cost_uncertain")))
                    for field in ("estimated_system_tokens", "estimated_tool_schema_tokens", "estimated_input_tokens"):
                        group[field] += usage_count(metrics.get(field))
                    prefetch = max(prefetch, usage_count(metrics.get("prefetch_calls")))
                if not {"run_id", "status"} <= tool_columns:
                    group["prefetched_tool_calls"] += prefetch
            else:
                group["model_calls"] += usage_count(row["turn_count"])
            if {"run_id", "status"} <= tool_columns:
                group["prefetched_tool_calls"] += conn.execute("select count(*) from agent_tool_calls where run_id=? and status='prefetched'", (row["id"],)).fetchone()[0]
        summary.update(runs=len(rows), roles=sorted(groups.values(), key=lambda item: -item["total_tokens"]))
        for target, field in (("model_http_attempts", "http_attempts"), ("failed_model_calls", "failed_model_calls"), ("unknown_cost_calls", "unknown_cost_calls")):
            summary[target] = sum(group[field] for group in groups.values())
    return summary
