from __future__ import annotations

import json
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ai_apitest.agents.identity import RoleRegistry, RoleSpec
from ai_apitest.agents.memory import (
    ContextRecoveryPolicy,
    TaskMemory,
    tool_call_signature,
)
from ai_apitest.ai.model_gateway import ModelGatewayProtocol
from ai_apitest.ai.provider import ModelResponse, TokenUsage
from ai_apitest.data.agent_runs import AgentRunRepository, redact_sensitive
from ai_apitest.core.config import load_config
from ai_apitest.tools.registry import (
    ToolExecutionContext,
    ToolExecutionResult,
    ToolInputError,
    ToolPermissionError,
    ToolRegistry,
)


class AgentRuntimeError(RuntimeError):
    pass


class AgentBudgetExceeded(AgentRuntimeError):
    pass


class AgentLoopLimitExceeded(AgentRuntimeError):
    pass


def uses_kimi_thinking_protocol(model_config: dict[str, Any]) -> bool:
    if str(model_config.get("provider") or "openai-compatible") != "openai-compatible":
        return False
    model = str(model_config.get("model") or "").strip().lower()
    base_url = str(model_config.get("base_url") or "").strip().lower()
    return model.startswith("kimi-") or "moonshot" in base_url


@dataclass(slots=True)
class AgentTaskResult:
    run_id: str
    role: RoleSpec
    artifact: Any
    text: str
    usage: TokenUsage
    turns: int
    tool_calls: int
    messages: list[dict[str, Any]] = field(default_factory=list)


class AgentRuntime:
    def __init__(
        self,
        *,
        roles: RoleRegistry,
        tools: ToolRegistry,
        gateway: ModelGatewayProtocol,
        audit: AgentRunRepository,
    ) -> None:
        self.roles = roles
        self.tools = tools
        self.gateway = gateway
        self.audit = audit

    def run(
        self,
        *,
        role_id: str,
        task_prompt: str,
        tool_context: ToolExecutionContext,
        enabled_tools: tuple[str, ...] | None = None,
        stream_callback: Callable[[str], None] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentTaskResult:
        role = self.roles.resolve(role_id)
        active_tools = tuple(enabled_tools) if enabled_tools is not None else role.allowed_tools
        if (
            enabled_tools is None
            and "read_task_evidence" in role.allowed_tools
            and "read_task_evidence" not in active_tools
        ):
            try:
                self.tools.get("read_task_evidence")
            except KeyError:
                pass
            else:
                active_tools = (*active_tools, "read_task_evidence")
        unauthorized = sorted(set(active_tools) - set(role.allowed_tools))
        if unauthorized:
            raise ToolPermissionError(
                f"任务尝试向角色 {role.id} 开放身份白名单外工具：{', '.join(unauthorized)}"
            )
        definitions = self.tools.definitions_for(role.id, active_tools)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": role.identity_text},
            {"role": "user", "content": task_prompt.strip()},
        ]
        config = self.gateway.model_config
        run_id = self.audit.start(
            role_id=role.id,
            role_version=role.version,
            model_role=role.model_role,
            provider=str(config.get("provider") or "openai-compatible"),
            model=str(config.get("model") or ""),
            prompt=role.identity_text + "\n" + task_prompt,
            metadata=metadata,
        )
        usage = TokenUsage()
        turns = 0
        tool_call_count = 0
        protocol_correction_used = False
        last_text = ""
        runtime_config = load_config(tool_context.root).get("agents", {})
        history_limit = max(10_000, int(runtime_config.get("history_max_chars", 60_000)))
        recovery_policy = ContextRecoveryPolicy.from_config(runtime_config.get("context_recovery"))
        memory = TaskMemory(
            goal=task_prompt.strip(),
            evidence_preview_chars=recovery_policy.evidence_preview_chars,
        )
        try:
            memory.goal_evidence_id = self.audit.save_evidence(
                run_id,
                0,
                call_id="task-prompt",
                tool_name="task_prompt",
                content=task_prompt.strip(),
                metadata={"role_id": role.id, "kind": "original_task"},
            )
        except Exception as exc:
            self.audit.finish(
                run_id,
                status="failed",
                usage=usage,
                turn_count=turns,
                tool_call_count=tool_call_count,
                error=f"保存任务证据失败：{exc}",
            )
            raise
        tool_context.run_id = run_id
        tool_context.evidence_reader = lambda evidence_id, offset, max_chars: self.audit.read_evidence(
            run_id,
            evidence_id,
            offset=offset,
            max_chars=max_chars,
        )
        completed_effects: dict[str, ToolExecutionResult] = {}
        terminal_tools = {name for name in active_tools if self.tools.get(name).terminal}
        original_tool_choice = config.get("tool_choice")
        original_thinking = config.get("thinking")
        try:
            for turn_index in range(1, role.max_tool_rounds + 1):
                turns = turn_index
                endpoint_checkpoint = tool_context.state.pop(
                    "endpoint_submission_checkpoint",
                    None,
                )
                if isinstance(endpoint_checkpoint, dict):
                    document_text = str(tool_context.state.get("document_text") or "")
                    accepted = endpoint_checkpoint.get("accepted") or []
                    missing = endpoint_checkpoint.get("missing") or []
                    messages = [
                        messages[0],
                        {
                            "role": "user",
                            "content": (
                                memory.goal
                                + "\n\n[接口识别分段检查点]\n"
                                + "你已经完整读取并验证过下面的接口文档。此前提交的接口已由工具持久保留，"
                                + "不要重复提交。现在只从 missing 中选择一个接口，完整提交该接口及其全部业务规则。\n"
                                + "accepted="
                                + json.dumps(accepted, ensure_ascii=False, separators=(",", ":"))
                                + "\nmissing="
                                + json.dumps(missing, ensure_ascii=False, separators=(",", ":"))
                                + "\n\n[已读取的接口文档]\n"
                                + document_text
                            ),
                        },
                    ]
                    memory.add_protocol_note(
                        "接口草稿分段提交后已重建定长上下文；已提交草稿保存在工具状态中。"
                    )
                messages, estimated_input = self._prepare_messages(
                    messages=messages,
                    definitions=definitions,
                    role=role,
                    turn_index=turn_index,
                    usage=usage,
                    memory=memory,
                    tool_context=tool_context,
                    history_limit=history_limit,
                    policy=recovery_policy,
                    run_id=run_id,
                    stream_callback=stream_callback,
                )
                remaining_budget = role.token_budget - usage.total_tokens
                max_output = min(
                    int(config.get("default_output_tokens") or 8000),
                    max(0, remaining_budget - estimated_input),
                    max(
                        0,
                        int(config.get("context_window") or 128000)
                        - estimated_input
                        - recovery_policy.safety_margin_tokens,
                    ),
                )
                if max_output < 128:
                    raise AgentBudgetExceeded(
                        f"角色 {role.id} 剩余 Token 预算不足以安全发起下一轮："
                        f"剩余 {remaining_budget}，预计输入 {estimated_input}"
                    )
                if original_tool_choice is None:
                    self.gateway.model_config.pop("tool_choice", None)
                else:
                    self.gateway.model_config["tool_choice"] = original_tool_choice
                if original_thinking is None:
                    self.gateway.model_config.pop("thinking", None)
                else:
                    self.gateway.model_config["thinking"] = original_thinking
                forced_terminal_tool = str(tool_context.state.get("forced_terminal_tool") or "")
                forced_next_tool = str(tool_context.state.pop("forced_next_tool", "") or "")
                forced_queue = tool_context.state.get("forced_tool_queue")
                queued_tool = ""
                if isinstance(forced_queue, list) and forced_queue:
                    queued_tool = str(forced_queue.pop(0) or "")
                forced_tool = (
                    forced_terminal_tool
                    if forced_terminal_tool in terminal_tools
                    else forced_next_tool or queued_tool
                )
                if turn_index == role.max_tool_rounds and len(terminal_tools) == 1:
                    forced_tool = next(iter(terminal_tools))
                    memory.add_protocol_note(
                        f"已到最后模型轮次，强制调用终止工具 {forced_tool}，避免无结果退出。"
                    )
                if forced_tool and forced_tool in active_tools:
                    self.gateway.model_config["tool_choice"] = {
                        "type": "function",
                        "function": {"name": forced_tool},
                    }
                    if forced_tool not in terminal_tools:
                        max_output = min(max_output, 1024)
                    if uses_kimi_thinking_protocol(self.gateway.model_config):
                        # Kimi does not allow explicit tool_choice with thinking.
                        # Forced read turns are deterministic orchestration and
                        # terminal turns already have all required evidence.
                        self.gateway.model_config["thinking"] = {"type": "disabled"}
                turn_definitions = definitions
                if forced_tool and forced_tool in active_tools:
                    turn_definitions = self.tools.definitions_for(role.id, (forced_tool,))
                response = self.gateway.complete(
                    messages=messages,
                    tools=turn_definitions,
                    timeout=role.timeout_seconds,
                    max_tokens=max_output,
                    stream_callback=stream_callback,
                )
                ensure_usage(response, messages, turn_definitions)
                usage.add(response.usage)
                self.audit.record_turn(run_id, turn_index, response)
                if usage.total_tokens > role.token_budget:
                    raise AgentBudgetExceeded(
                        f"角色 {role.id} 超出 Token 预算：{usage.total_tokens}/{role.token_budget}"
                    )
                last_text = response.text
                if response.tool_calls:
                    messages.append(response.assistant_message())
                    for call in response.tool_calls:
                        tool_call_count += 1
                        if tool_call_count > role.max_tool_calls:
                            raise AgentLoopLimitExceeded(
                                f"角色 {role.id} 超出工具调用上限：{role.max_tool_calls}"
                            )
                        signature = tool_call_signature(call.name, call.arguments)
                        try:
                            spec = self.tools.get(call.name)
                        except KeyError:
                            spec = None
                        deduplicated = (
                            spec is not None
                            and spec.side_effect != "read"
                            and spec.idempotent
                            and signature in completed_effects
                        )
                        started = time.monotonic()
                        result = (
                            completed_effects[signature]
                            if deduplicated
                            else self._execute_tool(role, active_tools, call, tool_context)
                        )
                        elapsed_ms = int((time.monotonic() - started) * 1000)
                        if (
                            spec is not None
                            and spec.side_effect != "read"
                            and spec.idempotent
                            and not result.is_error
                        ):
                            completed_effects[signature] = result
                        evidence_id = ""
                        if call.name == "read_task_evidence":
                            evidence_id = str(call.arguments.get("evidence_id") or "")
                        else:
                            evidence_id = self.audit.save_evidence(
                                run_id,
                                turn_index,
                                call_id=call.id,
                                tool_name=call.name,
                                content=evidence_content(result),
                                metadata={
                                    "arguments": call.arguments,
                                    "arguments_repaired": bool(call.repair_warning),
                                    "repair_warning": call.repair_warning,
                                    "is_error": result.is_error,
                                    "terminal": result.terminal,
                                    "truncated_in_prompt": result.truncated,
                                    "deduplicated": deduplicated,
                                },
                            )
                        self.audit.record_tool_call(
                            run_id,
                            turn_index,
                            call_id=call.id,
                            name=call.name,
                            arguments=call.arguments,
                            result=result.content,
                            status="failed" if result.is_error else ("deduplicated" if deduplicated else "completed"),
                            error=result.content if result.is_error else "",
                            duration_ms=elapsed_ms,
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.id,
                                "name": call.name,
                                "content": result.content,
                                "is_error": result.is_error,
                            }
                        )
                        memory.record_tool_call(
                            turn_index=turn_index,
                            call_id=call.id,
                            name=call.name,
                            arguments=redact_sensitive(call.arguments),
                            evidence_id=evidence_id,
                            result_content=result.content,
                            is_error=result.is_error,
                            terminal=result.terminal,
                            deduplicated=deduplicated,
                        )
                        if result.terminal and not result.is_error:
                            self.audit.finish(
                                run_id,
                                status="completed",
                                usage=usage,
                                turn_count=turns,
                                tool_call_count=tool_call_count,
                                artifact=result.value,
                            )
                            return AgentTaskResult(
                                run_id,
                                role,
                                result.value,
                                last_text,
                                usage,
                                turns,
                                tool_call_count,
                                messages,
                            )
                    continue

                if terminal_tools:
                    if not protocol_correction_used:
                        protocol_correction_used = True
                        names = "、".join(sorted(terminal_tools))
                        messages.append(
                            {
                                "role": "user",
                                "content": f"任务尚未完成。不要用普通文本提交结果；请调用终止工具 {names}。这是唯一一次协议纠正。",
                            }
                        )
                        memory.add_protocol_note(
                            f"第 {turn_index} 轮未调用终止工具；已要求下一轮调用 {names}。"
                        )
                        continue
                    raise AgentRuntimeError(
                        f"角色 {role.id} 未调用必须的终止工具：{', '.join(sorted(terminal_tools))}"
                    )

                self.audit.finish(
                    run_id,
                    status="completed",
                    usage=usage,
                    turn_count=turns,
                    tool_call_count=tool_call_count,
                )
                return AgentTaskResult(run_id, role, None, last_text, usage, turns, tool_call_count, messages)
            raise AgentLoopLimitExceeded(f"角色 {role.id} 超出最大模型轮次：{role.max_tool_rounds}")
        except Exception as exc:
            self.audit.finish(
                run_id,
                status="failed",
                usage=usage,
                turn_count=turns,
                tool_call_count=tool_call_count,
                error=str(exc),
            )
            raise

    def _prepare_messages(
        self,
        *,
        messages: list[dict[str, Any]],
        definitions: list[dict[str, Any]],
        role: RoleSpec,
        turn_index: int,
        usage: TokenUsage,
        memory: TaskMemory,
        tool_context: ToolExecutionContext,
        history_limit: int,
        policy: ContextRecoveryPolicy,
        run_id: str,
        stream_callback: Callable[[str], None] | None,
    ) -> tuple[list[dict[str, Any]], int]:
        estimated = estimate_request_tokens(messages, definitions)
        if len(json.dumps(messages, ensure_ascii=False, default=str)) > history_limit:
            messages = compact_agent_history(
                messages,
                history_limit,
                evidence_by_call_id=evidence_by_call_id(memory),
            )
            estimated = estimate_request_tokens(messages, definitions)
        if not policy.enabled:
            return messages, estimated

        context_window = max(1024, int(self.gateway.model_config.get("context_window") or 128000))
        output_reserve = max(128, int(self.gateway.model_config.get("default_output_tokens") or 8000))
        input_capacity = max(128, context_window - output_reserve - policy.safety_margin_tokens)
        ratio_before = estimated / input_capacity
        if ratio_before >= policy.soft_ratio:
            serialized_chars = len(json.dumps(messages, ensure_ascii=False, default=str))
            soft_limit = min(history_limit, max(10000, int(serialized_chars * 0.75)))
            messages = compact_agent_history(
                messages,
                soft_limit,
                evidence_by_call_id=evidence_by_call_id(memory),
            )
            estimated = estimate_request_tokens(messages, definitions)
            ratio_before = estimated / input_capacity
        if ratio_before < policy.rebase_ratio:
            return messages, estimated
        if memory.rebases >= policy.max_rebases:
            raise AgentBudgetExceeded(
                f"角色 {role.id} 已自动重建上下文 {memory.rebases} 次，"
                f"当前输入仍占安全预算 {ratio_before:.1%}；为避免无限续跑已停止。"
            )

        before_tokens = estimated
        memory.rebases += 1
        state_limit = policy.max_state_chars
        system_message = messages[0]
        safe_tool_state = redact_sensitive(tool_context.state)
        recovery_messages, state = memory.recovery_messages(
            system_message=system_message,
            role_id=role.id,
            turn_index=turn_index,
            tool_context_state=safe_tool_state,
            total_tokens_used=usage.total_tokens,
            max_state_chars=state_limit,
        )
        estimated_after = estimate_request_tokens(recovery_messages, definitions)
        target_tokens = max(128, int(input_capacity * policy.target_ratio))
        while estimated_after > target_tokens and state_limit > 4000:
            state_limit = max(4000, int(state_limit * 0.65))
            recovery_messages, state = memory.recovery_messages(
                system_message=system_message,
                role_id=role.id,
                turn_index=turn_index,
                tool_context_state=safe_tool_state,
                total_tokens_used=usage.total_tokens,
                max_state_chars=state_limit,
            )
            estimated_after = estimate_request_tokens(recovery_messages, definitions)
        ratio_after = estimated_after / input_capacity
        self.audit.record_checkpoint(
            run_id,
            checkpoint_index=memory.rebases,
            reason="context_window_rebase",
            input_tokens_before=before_tokens,
            input_tokens_after=estimated_after,
            ratio_before=ratio_before,
            ratio_after=ratio_after,
            state=state,
            messages=recovery_messages,
        )
        if stream_callback:
            stream_callback(
                f"上下文达到 {ratio_before:.0%}，已自动保存检查点并重建为 {ratio_after:.0%}；继续执行任务。\n"
            )
        if ratio_after >= policy.circuit_breaker_ratio:
            raise AgentBudgetExceeded(
                f"角色 {role.id} 自动重建后输入仍占安全预算 {ratio_after:.1%}，"
                "原始任务、身份或工具定义本身过大，已停止模型调用。"
            )
        return recovery_messages, estimated_after

    def _execute_tool(
        self,
        role: RoleSpec,
        active_tools: tuple[str, ...],
        call,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        if call.parse_error:
            return ToolExecutionResult(
                content=json.dumps(
                    {"error": "invalid_tool_arguments", "detail": call.parse_error},
                    ensure_ascii=False,
                ),
                is_error=True,
            )
        try:
            return self.tools.execute(
                role_id=role.id,
                allowed_tools=active_tools,
                name=call.name,
                arguments=call.arguments,
                context=context,
            )
        except ToolPermissionError:
            raise
        except (ToolInputError, KeyError, TypeError, ValueError) as exc:
            return ToolExecutionResult(
                content=json.dumps(
                    {"error": type(exc).__name__, "detail": str(exc)},
                    ensure_ascii=False,
                ),
                is_error=True,
            )


def ensure_usage(
    response: ModelResponse,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> None:
    if response.usage.total_tokens > 0:
        return
    input_text = json.dumps({"messages": messages, "tools": tools or []}, ensure_ascii=False, default=str)
    output_text = response.text + json.dumps(
        [call.to_openai_dict() for call in response.tool_calls],
        ensure_ascii=False,
    )
    input_tokens = estimate_tokens(input_text)
    output_tokens = estimate_tokens(output_text)
    response.usage = TokenUsage(input_tokens, output_tokens, input_tokens + output_tokens)


def evidence_content(result: ToolExecutionResult) -> str:
    if result.value is None:
        return str(result.content or "")
    try:
        return json.dumps(result.value, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return str(result.content or "")


def estimate_tokens(text: str) -> int:
    ascii_chars = sum(1 for char in text if ord(char) < 128)
    non_ascii_chars = len(text) - ascii_chars
    return max(1, ascii_chars // 4 + non_ascii_chars // 2)


def estimate_request_tokens(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> int:
    return estimate_tokens(json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False, default=str))


def evidence_by_call_id(memory: TaskMemory) -> dict[str, str]:
    return {
        str(item.get("call_id") or ""): str(item.get("evidence_id") or "")
        for item in memory.completed_tool_calls
        if item.get("call_id") and item.get("evidence_id")
    }


def compact_agent_history(
    messages: list[dict[str, Any]],
    max_chars: int,
    *,
    evidence_by_call_id: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    serialized = json.dumps(messages, ensure_ascii=False, default=str)
    if len(serialized) <= max_chars:
        return messages
    compacted = [dict(message) for message in messages]
    protected_from = max(2, len(compacted) - 8)
    for index in range(2, protected_from):
        message = compacted[index]
        content = str(message.get("content") or "")
        if len(content) <= 300:
            continue
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if message.get("role") == "tool":
            call_id = str(message.get("tool_call_id") or "")
            evidence_id = str((evidence_by_call_id or {}).get(call_id) or "")
            compacted_payload = {
                "compacted": True,
                "tool": message.get("name") or call_id,
                "sha256": digest,
            }
            if evidence_id:
                compacted_payload.update(
                    {
                        "evidence_id": evidence_id,
                        "next_action": (
                            "如需原始明细，调用 read_task_evidence，"
                            f"evidence_id 必须原样使用 {evidence_id}；不要把 sha256 当作 evidence_id。"
                        ),
                    }
                )
            else:
                compacted_payload["next_action"] = "如需原始明细，请重新按更小范围调用原只读工具。"
            message["content"] = json.dumps(compacted_payload, ensure_ascii=False)
        elif message.get("role") == "assistant":
            message["content"] = f"[前序模型文本已压缩 sha256:{digest}]"
        if len(json.dumps(compacted, ensure_ascii=False, default=str)) <= max_chars:
            break
    return compacted
