from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from ai_apitest.agents.identity import RoleRegistry
from ai_apitest.agents.runtime import AgentRuntime, AgentTaskResult
from ai_apitest.ai.model_gateway import ModelGatewayProtocol, ProviderModelGateway
from ai_apitest.core.models import Endpoint, TestCase
from ai_apitest.data.agent_runs import AgentRunRepository
from ai_apitest.data.repositories import ProjectDataAccess
from ai_apitest.tools.registry import ToolExecutionContext, ToolRegistry


GatewayFactory = Callable[[dict[str, Any]], ModelGatewayProtocol]


def run_agent_role(
    *,
    root: Path,
    project: str,
    endpoints: list[Endpoint],
    cases: list[TestCase] | None = None,
    role_id: str,
    task_prompt: str,
    model_config: dict[str, Any],
    roles: RoleRegistry,
    tools: ToolRegistry,
    enabled_tools: tuple[str, ...] | None = None,
    gateway_factory: GatewayFactory = ProviderModelGateway,
    stream_callback: Callable[[str], None] | None = None,
    metadata: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
) -> AgentTaskResult:
    data = ProjectDataAccess.create(root, project, endpoints=endpoints, cases=cases)
    context = ToolExecutionContext(root=root, project=project, data=data, state=dict(state or {}))
    runtime = AgentRuntime(
        roles=roles,
        tools=tools,
        gateway=gateway_factory(model_config),
        audit=AgentRunRepository(root, project),
    )
    return runtime.run(
        role_id=role_id,
        task_prompt=task_prompt,
        tool_context=context,
        enabled_tools=enabled_tools,
        stream_callback=stream_callback,
        metadata=metadata,
    )
