import copy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys

import pytest

from ai_apitest.agents.evaluation import evaluate_agent_suite
from ai_apitest.agents.identity import RoleRegistry, RoleSpec
from ai_apitest.agents.runtime import AgentRuntime
from ai_apitest.ai.provider import ModelResponse, ModelToolCall, TokenUsage
from ai_apitest.data.agent_runs import AgentRunRepository
from ai_apitest.data.agent_sessions import SessionRecoveryError
from ai_apitest.data.repositories import ProjectDataAccess
from ai_apitest.tools.registry import ToolExecutionContext, ToolRegistry, ToolSpec


class Gateway:
    model_config = {"model": "test", "context_window": 128000}
    def __init__(self, responses):
        self.responses = responses
        self.model_config = {"model": "test", "context_window": 128000}
    def complete(self, **kwargs):
        return self.responses.pop(0)


def durable_runtime(root, writes, failing, *, idempotent=True, version="1"):
    role = RoleSpec("task", "1", "test", "test", "master", "Task", "Artifact", allowed_tools=("write", "finish"), max_tool_rounds=4, token_budget=10000)
    tools = ToolRegistry()
    def write(args, context):
        writes.append(args)
        context.state["written"] = True
        if failing == "write":
            raise RuntimeError("write interrupted")
        return {"written": True}
    def finish(args, context):
        if failing == "finish":
            raise RuntimeError("finish interrupted")
        assert context.state["written"]
        return {"ok": True}
    tools.register(ToolSpec("write", "write", {"type": "object"}, write, ("task",), side_effect="write", idempotent=idempotent, version=version))
    tools.register(ToolSpec("finish", "finish", {"type": "object"}, finish, ("task",), side_effect="transform", terminal=True))
    response = ModelResponse(tool_calls=[ModelToolCall("a", "write", {}), ModelToolCall("b", "finish", {})], usage=TokenUsage(10, 10, 20))
    gateway = Gateway([response])
    runtime = AgentRuntime(roles=RoleRegistry([role]), tools=tools, gateway=gateway, audit=AgentRunRepository(root, "demo"))
    context = ToolExecutionContext(root, "demo", ProjectDataAccess.create(root, "demo", endpoints=[]))
    return runtime, context, gateway


def test_durable_resume_reuses_completed_calls_state_and_budget(tmp_path):
    writes = []
    runtime, context, _ = durable_runtime(tmp_path, writes, "finish")
    with pytest.raises(RuntimeError, match="finish interrupted"):
        runtime.run(role_id="task", task_prompt="task", tool_context=context, session_id="job")
    runtime, context, gateway = durable_runtime(tmp_path, writes, "")
    result = runtime.run(role_id="task", task_prompt="task", tool_context=context, session_id="job", resume=True)
    assert result.artifact == {"ok": True} and len(writes) == 1
    assert result.usage.total_tokens == 20 and result.tool_calls == 2
    assert len(gateway.responses) == 1
    runtime, context, gateway = durable_runtime(tmp_path, writes, "")
    cached = runtime.run(role_id="task", task_prompt="task", tool_context=context, session_id="job", resume=True)
    assert cached.artifact == result.artifact and len(writes) == 1 and len(gateway.responses) == 1


def test_durable_resume_blocks_unknown_writes_and_version_drift(tmp_path):
    runtime, context, _ = durable_runtime(tmp_path, [], "write", idempotent=False)
    with pytest.raises(RuntimeError):
        runtime.run(role_id="task", task_prompt="task", tool_context=context, session_id="unsafe")
    runtime, context, _ = durable_runtime(tmp_path, [], "", idempotent=False)
    with pytest.raises(SessionRecoveryError, match="副作用未知"):
        runtime.run(role_id="task", task_prompt="task", tool_context=context, session_id="unsafe", resume=True)
    runtime, context, _ = durable_runtime(tmp_path, [], "finish")
    with pytest.raises(RuntimeError):
        runtime.run(role_id="task", task_prompt="task", tool_context=context, session_id="changed")
    runtime, context, _ = durable_runtime(tmp_path, [], "", version="2")
    with pytest.raises(SessionRecoveryError, match="版本"):
        runtime.run(role_id="task", task_prompt="task", tool_context=context, session_id="changed", resume=True)


def test_baseline_and_bad_reference_evaluations(tmp_path):
    from pathlib import Path
    source = Path(__file__).resolve().parents[1] / "examples/evals/agent-baseline.json"
    suite = json.loads(source.read_text(encoding="utf-8"))
    result = evaluate_agent_suite(tmp_path, suite, trials=2)
    assert result["summary"]["attempts"] == 60 and result["summary"]["passed"] == 60
    bad = copy.deepcopy(suite["tasks"][1])
    bad["expected"]["artifact_schema"]["items"]["properties"]["endpoint_path"]["const"] = "/invented"
    failed = evaluate_agent_suite(tmp_path, {"tasks": [bad]})
    assert failed["summary"]["success_rate"] == 0
    live = evaluate_agent_suite(tmp_path, {"tasks": [suite["tasks"][0]]}, live=True)
    assert not live["results"][0]["passed"] and "未启用" in live["results"][0]["errors"][0]


def test_evaluation_accepts_semantic_basis_but_checks_compiled_status(tmp_path):
    suite = json.loads((Path(__file__).resolve().parents[1] / "examples/evals/agent-baseline.json").read_text())
    task = copy.deepcopy(suite["tasks"][1])
    task["responses"][-1]["tool_calls"][0]["arguments"]["drafts"][0]["expected_basis"] = "Trusted endpoint documents HTTP 200 success."
    passed = evaluate_agent_suite(tmp_path, {"tasks": [task]})
    assert passed["summary"]["passed"] == 1
    assert passed["results"][0]["model"] == "offline-replay"
    task["expected"]["compiled_schema"]["items"]["properties"]["assertions"]["contains"]["properties"]["expected"]["const"] = 201
    assert evaluate_agent_suite(tmp_path, {"tasks": [task]})["summary"]["passed"] == 0


def test_live_evaluator_uses_configured_master_fallback(tmp_path, monkeypatch):
    from ai_apitest.core.config import save_config
    import ai_apitest.agents.evaluation as evaluation
    task = json.loads((Path(__file__).resolve().parents[1] / "examples/evals/agent-baseline.json").read_text())["tasks"][1]
    save_config(tmp_path, {"llm": {"master": {"enabled": True, "base_url": "https://model.invalid", "model": "configured-master", "api_key_env": "TEST_MODEL_KEY"}}})
    selected = []
    def gateway(config):
        selected.append(config["model"])
        return evaluation.ReplayGateway(task["responses"])
    monkeypatch.setattr(evaluation, "ProviderModelGateway", gateway)
    assert evaluation.evaluate_agent_suite(tmp_path, {"tasks": [task]}, live=True)["summary"]["passed"] == 1
    assert selected == ["configured-master"]
    save_config(tmp_path, {"llm": {"analyzer": {"enabled": True, "base_url": "https://model.invalid", "model": "configured-analyzer", "api_key_env": "TEST_MODEL_KEY"}}})
    assert evaluation.evaluate_agent_suite(tmp_path, {"tasks": [task]}, live=True, model_role="analyzer")["summary"]["passed"] == 1
    assert selected[-1] == "configured-analyzer"
    with pytest.raises(ValueError):
        evaluation.evaluate_agent_suite(tmp_path, {"tasks": [task]}, model_role="unknown")


def test_designer_schema_guides_assertion_types_and_short_audit_reads_work(tmp_path):
    from ai_apitest.tools.builtin import build_case_generation_tool_registry
    from ai_apitest.tools.registry import ToolInputError
    tools = build_case_generation_tool_registry()
    task = json.loads((Path(__file__).resolve().parents[1] / "examples/evals/agent-baseline.json").read_text())["tasks"][1]
    args = copy.deepcopy(task["responses"][-1]["tool_calls"][0]["arguments"])
    args["drafts"][0]["assertions"] = [{"type": "status_eq", "expected": 200}]
    context = ToolExecutionContext(tmp_path, "eval", None)
    with pytest.raises(ToolInputError):
        tools.execute(role_id="case_designer", allowed_tools=("submit_case_drafts",), name="submit_case_drafts", arguments=args, context=context)
    args["drafts"][0]["assertions"] = [{"type": "json_schema", "expected": "array"}]
    with pytest.raises(ToolInputError):
        tools.execute(role_id="case_designer", allowed_tools=("submit_case_drafts",), name="submit_case_drafts", arguments=args, context=context)
    context.state["audit_artifact_text"] = "short evidence"
    result = tools.execute(role_id="quality_reviewer", allowed_tools=("read_audit_artifact_chunk",), name="read_audit_artifact_chunk", arguments={"offset": 0, "max_chars": 14}, context=context)
    assert result.value["content"] == "short evidence" and result.value["has_more"] is False


def test_complete_tool_input_output_and_deadline(tmp_path):
    from ai_apitest.tools.registry import ToolInputError, ToolOutputError, ToolTimeoutError
    import time
    context = ToolExecutionContext(tmp_path, "demo", None)
    registry = ToolRegistry()
    registry.register(ToolSpec("input", "input", {"type": "object", "properties": {"id": {"type": "string", "pattern": "^[A-Z]{2}$"}}, "required": ["id"], "additionalProperties": False}, lambda args, ctx: {}, ("task",)))
    with pytest.raises(ToolInputError):
        registry.execute(role_id="task", allowed_tools=("input",), name="input", arguments={"id": "abc"}, context=context)
    registry.register(ToolSpec("output", "output", {"type": "object"}, lambda args, ctx: {"ok": False}, ("task",), output_schema={"type": "object", "properties": {"ok": {"const": True}}}))
    with pytest.raises(ToolOutputError):
        registry.execute(role_id="task", allowed_tools=("output",), name="output", arguments={}, context=context)
    registry.register(ToolSpec("slow", "slow", {"type": "object"}, lambda args, ctx: time.sleep(0.03), ("task",), timeout_seconds=0.01))
    with pytest.raises(ToolTimeoutError):
        registry.execute(role_id="task", allowed_tools=("slow",), name="slow", arguments={}, context=context)


def test_shared_task_budget_and_model_routing(tmp_path):
    from ai_apitest.agents.policy import TaskBudget, TaskBudgetExceeded, route_model, task_budget
    budget = TaskBudget(max_tokens=1000, max_tool_calls=1)
    allowed = budget.reserve(250, 400)
    assert allowed == 400
    with pytest.raises(TaskBudgetExceeded):
        budget.reserve(250, 400)
    budget.settle(650, 100)
    assert budget.tokens == 100 and budget.reserved == 0
    with task_budget(tmp_path, budget=budget):
        with task_budget(tmp_path, budget=TaskBudget()) as nested:
            assert nested is budget
            nested.tool()
        with pytest.raises(TaskBudgetExceeded):
            budget.tool()
    role = RoleSpec("case_designer", "1", "test", "test", "case_generator", "Task", "Artifact")
    config = {"agents": {"model_routing": {"rules": [{"role": "*", "max_endpoints": 3, "model_role": "master"}]}}, "llm": {"master": {"enabled": True, "model": "small"}}}
    selected, reason = route_model(config, role, {"model": "large"}, prompt_chars=100, endpoints=1)
    assert selected["model"] == "small" and reason["selected"] == "master"
    selected, _ = route_model(config, replace(role, id="quality_reviewer"), {"model": "judge"}, prompt_chars=100, endpoints=1)
    assert selected["model"] == "judge"


def test_recipe_permissions_versions_and_cache_invalidation(tmp_path):
    from ai_apitest.agents.recipes import RecipeCatalog
    from ai_apitest.core.config import load_config
    from ai_apitest.ai.workflows import case_generation_fingerprint
    from ai_apitest.core.models import Endpoint
    folder = tmp_path / "recipes"
    folder.mkdir()
    path = folder / "SKILL.md"
    path.write_text('---\nname: test\ndescription: test recipe\nversion: "1"\nallowed_roles: [case_designer]\n---\nUse documented data.\n', encoding="utf-8")
    config = load_config(tmp_path)
    config["agents"]["recipes"]["paths"] = ["recipes"]
    catalog = RecipeCatalog(tmp_path, "demo", config)
    assert catalog.list("test_planner") == []
    summary = catalog.list("case_designer")[0]
    assert "content" not in summary
    assert catalog.read("test", "case_designer", summary["sha256"])["content"] == "Use documented data."
    with pytest.raises(PermissionError):
        catalog.read("test", "api_analyst")
    endpoints = [Endpoint("GET", "/health", responses={"200": {}})]
    before = case_generation_fingerprint(tmp_path, "demo", endpoints, config, {})
    path.write_text(path.read_text(encoding="utf-8") + "Changed rule.\n", encoding="utf-8")
    assert before != case_generation_fingerprint(tmp_path, "demo", endpoints, config, {})
    with pytest.raises(ValueError, match="版本"):
        RecipeCatalog(tmp_path, "demo", config).read("test", "case_designer", summary["sha256"])


@pytest.mark.parametrize("crash_write", [False, True])
def test_durable_recovery_after_real_process_exit(tmp_path, crash_write):
    script = tmp_path / "restart.py"
    script.write_text('''import json, os, sys
from pathlib import Path
from ai_apitest.agents.identity import RoleRegistry, RoleSpec
from ai_apitest.agents.policy import TaskBudget, task_budget
from ai_apitest.agents.runtime import AgentRuntime
from ai_apitest.ai.provider import ModelResponse, ModelToolCall, TokenUsage
from ai_apitest.data.agent_runs import AgentRunRepository
from ai_apitest.data.repositories import ProjectDataAccess
from ai_apitest.tools.registry import ToolExecutionContext, ToolRegistry, ToolSpec
root = Path(sys.argv[1])
resume, unsafe = sys.argv[2] == "resume", sys.argv[3] == "unsafe"
role = RoleSpec("task", "1", "test", "test", "master", "Task", "Artifact", allowed_tools=("write", "finish"), max_tool_rounds=4, token_budget=20000)
tools = ToolRegistry()
def write(args, context):
    with (root / "writes.txt").open("a") as target:
        target.write("written\\n")
    context.state["written"] = True
    if unsafe and not resume:
        os._exit(17)
    return {"written": True}
def finish(args, context):
    if not resume:
        os._exit(17)
    assert context.state["written"]
    return {"ok": True}
tools.register(ToolSpec("write", "write", {"type": "object"}, write, ("task",), side_effect="write", idempotent=not unsafe))
tools.register(ToolSpec("finish", "finish", {"type": "object"}, finish, ("task",), terminal=True))
class Gateway:
    model_config = {"model": "test", "context_window": 128000}
    def complete(self, **kwargs):
        assert not resume, "Recovery unexpectedly called the model"
        return ModelResponse(tool_calls=[ModelToolCall("a", "write", {}), ModelToolCall("b", "finish", {})], usage=TokenUsage(10, 10, 20))
runtime = AgentRuntime(roles=RoleRegistry([role]), tools=tools, gateway=Gateway(), audit=AgentRunRepository(root, "demo"))
context = ToolExecutionContext(root, "demo", ProjectDataAccess.create(root, "demo", endpoints=[]))
budget = TaskBudget(max_tokens=20000, max_tool_calls=4)
with task_budget(root, budget=budget):
    result = runtime.run(role_id="task", task_prompt="task", tool_context=context, session_id="restart", resume=resume)
print(json.dumps({"artifact": result.artifact, "tokens": budget.tokens, "calls": budget.tool_calls}))
''', encoding="utf-8")
    args = [sys.executable, "-X", "utf8", str(script), str(tmp_path)]
    crashed = subprocess.run([*args, "crash", "unsafe" if crash_write else "safe"], capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert crashed.returncode == 17, crashed.stderr
    restored = subprocess.run([*args, "resume", "unsafe" if crash_write else "safe"], capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert (tmp_path / "writes.txt").read_text().splitlines() == ["written"]
    if crash_write:
        assert restored.returncode != 0 and "副作用未知" in restored.stderr
    else:
        assert restored.returncode == 0, restored.stderr
        result = json.loads(restored.stdout)
        assert result == {"artifact": {"ok": True}, "tokens": 20, "calls": 3}


def test_budget_restore_failure_releases_session_and_model_timeout_is_bounded(tmp_path):
    from ai_apitest.agents.policy import TaskBudget, task_budget, TaskBudgetExceeded
    runtime, context, gateway = durable_runtime(tmp_path, [], "finish")
    seen = []
    original = gateway.complete
    def complete(**kwargs):
        seen.append(kwargs["timeout"])
        return original(**kwargs)
    gateway.complete = complete
    with task_budget(tmp_path, budget=TaskBudget(max_tokens=20000, timeout_seconds=60)):
        with pytest.raises(RuntimeError):
            runtime.run(role_id="task", task_prompt="task", tool_context=context, session_id="budget")
    assert 0 < seen[0] <= 60
    runtime, context, _ = durable_runtime(tmp_path, [], "")
    with task_budget(tmp_path, budget=TaskBudget(max_tokens=30000, timeout_seconds=60)):
        with pytest.raises(TaskBudgetExceeded, match="配置"):
            runtime.run(role_id="task", task_prompt="task", tool_context=context, session_id="budget", resume=True)
    runtime, context, _ = durable_runtime(tmp_path, [], "")
    with task_budget(tmp_path, budget=TaskBudget(max_tokens=20000, timeout_seconds=60)):
        assert runtime.run(role_id="task", task_prompt="task", tool_context=context, session_id="budget", resume=True).artifact == {"ok": True}


@pytest.mark.parametrize("model_stream", [False, True])
def test_task_deadline_stops_model_stream_even_for_heartbeats(tmp_path, model_stream):
    from ai_apitest.agents.policy import TaskBudget, task_budget, TaskBudgetExceeded
    from ai_apitest.ai.provider import read_openai_model_stream, read_openai_stream
    budget = TaskBudget(timeout_seconds=60)
    def lines():
        yield 'data: {"choices":[{"delta":{"content":"ok"}}]}'
        budget.started_at -= 61
        yield ': heartbeat'
        pytest.fail("Expired model stream continued reading")
    with task_budget(tmp_path, budget=budget):
        with pytest.raises(TaskBudgetExceeded, match="时限"):
            (read_openai_model_stream if model_stream else read_openai_stream)(lines(), lambda text: None)
