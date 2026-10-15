"""Evaluate real agent/tool execution; reference answers never enter prompts."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import time

from ai_apitest.agents.identity import default_role_registry
from ai_apitest.ai.model_gateway import ProviderModelGateway
from ai_apitest.ai.provider import ModelResponse, ModelToolCall, TokenUsage
from ai_apitest.application.agent_service import run_agent_role
from ai_apitest.core.config import load_config, save_config
from ai_apitest.core.schema import schema_errors
from ai_apitest.core.serde import endpoint_from_dict
from ai_apitest.core.serde import to_jsonable
from ai_apitest.ai.llm_config import resolve_llm_role
from ai_apitest.core.redaction import redact
from ai_apitest.data.agent_runs import AgentRunRepository
from ai_apitest.tools.builtin import build_case_generation_tool_registry


class ReplayGateway:
    def __init__(self, responses: list):
        self.responses = copy.deepcopy(responses)
        self.model_config = {"model": "offline-replay", "context_window": 128000}

    def complete(self, **kwargs):
        if not self.responses:
            raise ValueError("回放响应已耗尽，Agent 未完成任务")
        row = self.responses.pop(0)
        calls = [ModelToolCall(str(call.get("id", f"call-{i}")), call["name"], call.get("arguments", {})) for i, call in enumerate(row.get("tool_calls", []))]
        return ModelResponse(text=row.get("text", ""), tool_calls=calls, usage=TokenUsage(**row.get("usage", {})))


def evaluate_agent_suite(root: Path, suite: dict, *, trials: int = 1, live: bool = False, model_role: str | None = None) -> dict:
    if model_role is not None and model_role not in {"master", "case_generator", "analyzer"}:
        raise ValueError("评测模型只支持 master、case_generator、analyzer")
    tasks = suite.get("tasks")
    if not isinstance(tasks, list) or not tasks or len(tasks) > 1000 or type(trials) is not int or not 1 <= trials <= 100:
        raise ValueError("评测需要 1–1000 条任务、1–100 次试验")
    if len(tasks) * trials > 10000:
        raise ValueError("评测试验总数超过 10000")
    identifiers = [task.get("id") for task in tasks if isinstance(task, dict)]
    if len(identifiers) != len(tasks) or any(not isinstance(key, str) or not key for key in identifiers) or len(set(identifiers)) != len(tasks):
        raise ValueError("评测任务 id 必须唯一且非空")
    config, roles, tools = load_config(root), default_role_registry(), build_case_generation_tool_registry()
    rows = []
    for task in tasks:
        role = roles.resolve(task["role"])
        expected = task.get("expected", {})
        for trial in range(trials):
            start = time.monotonic()
            row = {"task_id": task["id"], "trial": trial + 1, "role": role.id, "mode": "live" if live else "replay", "passed": False, "errors": [], "tokens": 0, "tool_calls": 0}
            # Each trial has independent SQLite/context state; source project data is never modified.
            with tempfile.TemporaryDirectory(prefix="iatest-agent-eval-") as temporary:
                work = Path(temporary)
                policy_config = copy.deepcopy(config.get("agents", {}))
                policy_config["recipes"] = {"paths": []}
                save_config(work, {"agents": policy_config, "llm": config.get("llm", {})})
                endpoints = [endpoint_from_dict(item) for item in task.get("endpoints", [])]
                context_state = copy.deepcopy(task.get("state", {}))
                try:
                    profile = model_role or role.model_role
                    model_config = resolve_llm_role(config.get("llm", {}), profile)
                    if live and not model_config.get("enabled"):
                        raise ValueError(f"真实评测模型 {profile} 未启用")
                    factory = ProviderModelGateway if live else lambda _: ReplayGateway(task.get("responses", []))
                    result = run_agent_role(root=work, project="eval", endpoints=endpoints, role_id=role.id, task_prompt=task["prompt"], model_config=model_config, roles=roles, tools=tools, enabled_tools=tuple(task.get("tools", role.allowed_tools)), gateway_factory=factory, state=context_state, metadata={"evaluation": task["id"], "trial": trial + 1})
                    row.update(tokens=result.usage.total_tokens, tool_calls=result.tool_calls, artifact=result.artifact, run_id=result.run_id)
                    row["errors"].extend(schema_errors(result.artifact, expected.get("artifact_schema", True)))
                    if expected.get("gate") == "compile_cases":
                        from ai_apitest.domain.case_drafts import compile_scenario_drafts, validate_compiled_cases
                        compiled = compile_scenario_drafts(result.artifact, endpoints)
                        issues = compiled.issues + validate_compiled_cases(compiled.cases, endpoints)
                        row["errors"].extend(str(issue) for issue in issues if getattr(issue, "severity", "error") == "error")
                        if "compiled_schema" in expected:
                            row["errors"].extend(schema_errors(to_jsonable(compiled.cases), expected["compiled_schema"]))
                    with AgentRunRepository(work, "eval").connection() as conn:
                        calls = [dict(zip(("name", "status", "error"), value)) for value in conn.execute("select name, status, error from agent_tool_calls where run_id = ? order by id", (result.run_id,))]
                    row["trajectory"] = calls
                    names = {call["name"] for call in calls}
                    for name in expected.get("required_tools", []):
                        if name not in names:
                            row["errors"].append(f"缺少工具调用：{name}")
                    for name in expected.get("forbidden_tools", []):
                        if name in names:
                            row["errors"].append(f"存在禁止工具调用：{name}")
                    if result.tool_calls > expected.get("max_tool_calls", role.max_tool_calls):
                        row["errors"].append("工具调用超出评测预算")
                    if expected.get("no_tool_errors", True) and any(call["status"] == "failed" for call in calls):
                        row["errors"].append("轨迹中存在工具失败")
                    row["passed"] = not row["errors"]
                except Exception as exc:
                    row["errors"].append(f"{type(exc).__name__}: {exc}")
                    previous = AgentRunRepository(work, "eval").list_recent(1)
                    if previous:
                        row.update(tokens=previous[0]["total_tokens"], tool_calls=previous[0]["tool_call_count"])
                previous = AgentRunRepository(work, "eval").list_recent(1)
                if previous:
                    row.update(provider=previous[0]["provider"], model=previous[0]["model"], run_id=previous[0]["id"])
                    if "trajectory" not in row:
                        with AgentRunRepository(work, "eval").connection() as conn:
                            row["trajectory"] = [dict(zip(("name", "status", "error"), value)) for value in conn.execute("select name, status, error from agent_tool_calls where run_id = ? order by id", (previous[0]["id"],))]
                row["elapsed_ms"] = round((time.monotonic() - start) * 1000)
                rows.append(row)
    grouped = [[row for row in rows if row["task_id"] == key] for key in identifiers]
    return redact({"version": 1, "mode": "live" if live else "replay", "trials": trials, "summary": {"tasks": len(tasks), "attempts": len(rows), "passed": sum(row["passed"] for row in rows), "success_rate": sum(row["passed"] for row in rows) / len(rows), "all_trials_success_rate": sum(all(row["passed"] for row in group) for group in grouped) / len(grouped), "any_trial_success_rate": sum(any(row["passed"] for row in group) for group in grouped) / len(grouped), "tokens": sum(row["tokens"] for row in rows), "tool_calls": sum(row["tool_calls"] for row in rows)}, "results": rows})
