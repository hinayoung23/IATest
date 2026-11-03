from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ContextRecoveryPolicy:
    enabled: bool = True
    soft_ratio: float = 0.55
    rebase_ratio: float = 0.70
    circuit_breaker_ratio: float = 0.80
    target_ratio: float = 0.35
    max_rebases: int = 3
    safety_margin_tokens: int = 2000
    evidence_preview_chars: int = 500
    max_state_chars: int = 16000

    @classmethod
    def from_config(cls, value: dict[str, Any] | None) -> "ContextRecoveryPolicy":
        raw = value or {}
        soft = bounded_float(raw.get("soft_ratio"), 0.55, 0.20, 0.75)
        rebase = bounded_float(raw.get("rebase_ratio"), 0.70, soft + 0.05, 0.85)
        circuit = bounded_float(raw.get("circuit_breaker_ratio"), 0.80, rebase + 0.05, 0.95)
        target = bounded_float(raw.get("target_ratio"), 0.35, 0.10, soft)
        return cls(
            enabled=bool(raw.get("enabled", True)),
            soft_ratio=soft,
            rebase_ratio=rebase,
            circuit_breaker_ratio=circuit,
            target_ratio=target,
            max_rebases=bounded_int(raw.get("max_rebases"), 3, 1, 10),
            safety_margin_tokens=bounded_int(raw.get("safety_margin_tokens"), 2000, 500, 16000),
            evidence_preview_chars=bounded_int(raw.get("evidence_preview_chars"), 500, 100, 2000),
            max_state_chars=bounded_int(raw.get("max_state_chars"), 16000, 4000, 50000),
        )


@dataclass(slots=True)
class TaskMemory:
    goal: str
    goal_evidence_id: str = ""
    evidence_preview_chars: int = 500
    rebases: int = 0
    completed_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    evidence_index: list[dict[str, Any]] = field(default_factory=list)
    protocol_notes: list[str] = field(default_factory=list)

    def record_tool_call(
        self,
        *,
        turn_index: int,
        call_id: str,
        name: str,
        arguments: dict[str, Any],
        evidence_id: str,
        result_content: str,
        is_error: bool,
        terminal: bool,
        deduplicated: bool = False,
    ) -> None:
        preview = compact_text(result_content, self.evidence_preview_chars)
        signature = tool_call_signature(name, arguments)
        self.completed_tool_calls.append(
            {
                "turn": turn_index,
                "call_id": call_id,
                "tool": name,
                "arguments": compact_value(arguments),
                "signature": signature,
                "status": "failed" if is_error else "completed",
                "terminal": bool(terminal),
                "deduplicated": bool(deduplicated),
                "evidence_id": evidence_id,
                "result_preview": preview,
            }
        )
        if evidence_id:
            self.evidence_index.append(
                {
                    "evidence_id": evidence_id,
                    "tool": name,
                    "turn": turn_index,
                    "status": "failed" if is_error else "completed",
                    "preview": preview,
                }
            )

    def add_protocol_note(self, value: str) -> None:
        text = str(value or "").strip()
        if text and text not in self.protocol_notes:
            self.protocol_notes.append(text)

    def checkpoint_state(
        self,
        *,
        role_id: str,
        turn_index: int,
        tool_context_state: dict[str, Any],
        total_tokens_used: int,
    ) -> dict[str, Any]:
        completed_items = collect_completed_items(tool_context_state, self.completed_tool_calls)
        last_call = self.completed_tool_calls[-1] if self.completed_tool_calls else {}
        return {
            "goal": compact_goal(self.goal, self.goal_evidence_id),
            "role_id": role_id,
            "turn_index": turn_index,
            "rebase_count": self.rebases,
            "total_tokens_used": total_tokens_used,
            "completed_items": completed_items,
            "completed_tool_calls": self.completed_tool_calls[-40:],
            "evidence_index": self.evidence_index[-40:],
            "runtime_state": compact_runtime_state(tool_context_state),
            "protocol_notes": self.protocol_notes[-10:],
            "next_action": next_action(last_call),
        }

    def recovery_messages(
        self,
        *,
        system_message: dict[str, Any],
        role_id: str,
        turn_index: int,
        tool_context_state: dict[str, Any],
        total_tokens_used: int,
        max_state_chars: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        state = self.checkpoint_state(
            role_id=role_id,
            turn_index=turn_index,
            tool_context_state=tool_context_state,
            total_tokens_used=total_tokens_used,
        )
        state_json = bounded_json(state, max_state_chars)
        prompt = (
            "## 自动上下文恢复检查点\n\n"
            "这是同一任务的自动续跑，不是新任务。身份、职责、权限和总 Token 预算保持不变。\n"
            "严格依据下面的结构化状态继续执行；不要重复 completed_tool_calls 或 completed_items 中已经完成的工作。\n"
            "如需已移出上下文的原始工具结果，调用 read_task_evidence 并传入 evidence_id。\n"
            "不得把摘要中的 preview 当成完整证据；涉及精确字段时必须读取证据或重新调用原只读工具。\n\n"
            f"```json\n{state_json}\n```"
        )
        return [dict(system_message), {"role": "user", "content": prompt}], state


def tool_call_signature(name: str, arguments: dict[str, Any]) -> str:
    payload = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(f"{name}\n{payload}".encode("utf-8")).hexdigest()


def compact_goal(value: str, evidence_id: str) -> dict[str, Any]:
    text = str(value or "")
    if len(text) <= 12000:
        return {"text": text, "evidence_id": evidence_id}
    return {
        "text": text[:8000] + "\n...[原始任务中段已移入证据库]...\n" + text[-2000:],
        "truncated": True,
        "sha256": "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "evidence_id": evidence_id,
    }


def compact_runtime_state(value: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in list(value.items())[:60]:
        if str(key).startswith("_"):
            continue
        if key == "document_text":
            text = str(item or "")
            result[key] = {
                "stored_outside_prompt": True,
                "length_chars": len(text),
                "sha256": "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
            continue
        result[str(key)] = compact_value(item)
    return result


def compact_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 4:
        return compact_text(str(value), 500)
    if isinstance(value, dict):
        return {
            str(key): compact_value(item, depth=depth + 1)
            for key, item in list(value.items())[:50]
            if not str(key).startswith("_")
        }
    if isinstance(value, (list, tuple)):
        return [compact_value(item, depth=depth + 1) for item in list(value)[:50]]
    if isinstance(value, str):
        return compact_text(value, 2000)
    return value


def collect_completed_items(
    state: dict[str, Any],
    calls: list[dict[str, Any]],
) -> list[str]:
    values: list[str] = []
    for key in ("coverage_plan", "case_drafts"):
        items = state.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    add_unique(values, str(item.get("scenario_id") or ""))
    endpoint_map = state.get("endpoint_drafts_by_key")
    if isinstance(endpoint_map, dict):
        for key in endpoint_map:
            add_unique(values, str(key))
    for call in calls:
        arguments = call.get("arguments")
        if not isinstance(arguments, dict):
            continue
        for key in ("items", "drafts", "decisions", "endpoints", "edges", "reviews"):
            items = arguments.get(key)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                identifier = (
                    item.get("scenario_id")
                    or item.get("target_case_id")
                    or item.get("candidate_id")
                    or item.get("relation_id")
                )
                if not identifier and item.get("method") and item.get("path"):
                    identifier = f"{item['method']} {item['path']}"
                add_unique(values, str(identifier or ""))
    return values[-200:]


def next_action(last_call: dict[str, Any]) -> str:
    if not last_call:
        return "从原始任务开始，按身份契约调用必要工具。"
    if last_call.get("status") == "failed":
        return f"处理工具 {last_call.get('tool')} 的失败结果，修正参数或补齐缺失信息后继续。"
    return "基于已完成工具调用和证据继续剩余任务，不重复已经完成的工作。"


def bounded_json(value: Any, limit: int) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(text) <= limit:
        return text
    reduced = dict(value) if isinstance(value, dict) else {"state": compact_value(value)}
    if isinstance(reduced.get("completed_tool_calls"), list):
        reduced["completed_tool_calls"] = reduced["completed_tool_calls"][-12:]
    if isinstance(reduced.get("evidence_index"), list):
        reduced["evidence_index"] = reduced["evidence_index"][-12:]
    if isinstance(reduced.get("completed_items"), list):
        reduced["completed_items"] = reduced["completed_items"][-100:]
    text = json.dumps(reduced, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(text) <= limit:
        return text
    goal = reduced.get("goal") if isinstance(reduced.get("goal"), dict) else {}
    minimal = {
        "goal": {
            "text": compact_text(str(goal.get("text") or ""), min(1000, max(200, limit // 4))),
            "evidence_id": goal.get("evidence_id"),
            "truncated": True,
        },
        "role_id": reduced.get("role_id"),
        "turn_index": reduced.get("turn_index"),
        "rebase_count": reduced.get("rebase_count"),
        "total_tokens_used": reduced.get("total_tokens_used"),
        "completed_items": reduced.get("completed_items", [])[-20:],
        "evidence_index": [
            {
                "evidence_id": item.get("evidence_id"),
                "tool": item.get("tool"),
                "status": item.get("status"),
            }
            for item in reduced.get("evidence_index", [])[-6:]
            if isinstance(item, dict)
        ],
        "next_action": compact_text(str(reduced.get("next_action") or ""), 500),
        "state_truncated": True,
    }
    text = json.dumps(
        minimal,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    if len(text) <= limit:
        return text
    minimal["completed_items"] = []
    minimal["evidence_index"] = minimal["evidence_index"][-2:]
    minimal["goal"]["text"] = compact_text(str(minimal["goal"]["text"]), 300)
    return json.dumps(minimal, ensure_ascii=False, separators=(",", ":"), default=str)


def compact_text(value: str, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    head = max(1, int(limit * 0.75))
    tail = max(1, limit - head - 20)
    return text[:head] + "...[已压缩]..." + text[-tail:]


def add_unique(values: list[str], value: str) -> None:
    text = str(value or "").strip()
    if text and text not in values:
        values.append(text)


def bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))
