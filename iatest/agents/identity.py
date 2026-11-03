from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


IDENTITY_DIR = Path(__file__).resolve().parent / "identities"


class UnknownRoleError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RoleSpec:
    id: str
    version: str
    mission: str
    system_prompt: str
    model_role: str
    input_contract: str
    output_contract: str
    allowed_tools: tuple[str, ...] = ()
    forbidden_actions: tuple[str, ...] = ()
    max_tool_rounds: int = 6
    max_tool_calls: int = 20
    token_budget: int = 24000
    timeout_seconds: int = 180
    quality_gates: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, source: str = "") -> RoleSpec:
        required = ["id", "version", "mission", "system_prompt", "model_role", "input_contract", "output_contract"]
        missing = [key for key in required if not str(data.get(key) or "").strip()]
        if missing:
            raise ValueError(f"角色定义缺少字段 {', '.join(missing)}{f'（{source}）' if source else ''}")
        return cls(
            id=str(data["id"]).strip(),
            version=str(data["version"]).strip(),
            mission=str(data["mission"]).strip(),
            system_prompt=str(data["system_prompt"]).strip(),
            model_role=str(data["model_role"]).strip(),
            input_contract=str(data["input_contract"]).strip(),
            output_contract=str(data["output_contract"]).strip(),
            allowed_tools=tuple(unique_strings(data.get("allowed_tools"))),
            forbidden_actions=tuple(unique_strings(data.get("forbidden_actions"))),
            max_tool_rounds=bounded_int(data.get("max_tool_rounds"), 6, minimum=1, maximum=20),
            max_tool_calls=bounded_int(data.get("max_tool_calls"), 20, minimum=1, maximum=100),
            token_budget=bounded_int(data.get("token_budget"), 24000, minimum=1000, maximum=1_000_000),
            timeout_seconds=bounded_int(data.get("timeout_seconds"), 180, minimum=10, maximum=3600),
            quality_gates=tuple(unique_strings(data.get("quality_gates"))),
            metadata=dict(data.get("metadata") or {}),
        )

    def allows_tool(self, tool_name: str) -> bool:
        return tool_name in self.allowed_tools

    @property
    def identity_text(self) -> str:
        forbidden = "\n".join(f"- {item}" for item in self.forbidden_actions) or "- 不得执行角色职责之外的任务"
        return (
            f"{self.system_prompt}\n\n"
            f"## 身份契约\n"
            f"- 角色：{self.id}\n"
            f"- 版本：{self.version}\n"
            f"- 职责：{self.mission}\n"
            f"- 输入契约：{self.input_contract}\n"
            f"- 输出契约：{self.output_contract}\n\n"
            f"## 禁止事项\n{forbidden}\n\n"
            "只能使用会话中提供的工具；不得假设存在未提供的工具、数据或权限。"
        )


class RoleRegistry:
    def __init__(self, roles: list[RoleSpec] | None = None) -> None:
        self._roles: dict[str, RoleSpec] = {}
        for role in roles or []:
            self.register(role)

    def register(self, role: RoleSpec) -> None:
        existing = self._roles.get(role.id)
        if existing and existing.version != role.version:
            raise ValueError(f"角色 {role.id} 已注册版本 {existing.version}，不能覆盖为 {role.version}")
        self._roles[role.id] = role

    def resolve(self, role_id: str) -> RoleSpec:
        role = self._roles.get(str(role_id).strip())
        if role is None:
            raise UnknownRoleError(f"未知模型角色：{role_id}")
        return role

    def all(self) -> list[RoleSpec]:
        return sorted(self._roles.values(), key=lambda item: item.id)


def load_role_specs(directory: Path = IDENTITY_DIR) -> list[RoleSpec]:
    roles: list[RoleSpec] = []
    if not directory.exists():
        raise FileNotFoundError(f"身份定义目录不存在：{directory}")
    for path in sorted(directory.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"角色定义必须是对象：{path}")
        roles.append(RoleSpec.from_dict(data, source=str(path)))
    if not roles:
        raise ValueError(f"身份定义目录中没有 YAML 文件：{directory}")
    return roles


def default_role_registry() -> RoleRegistry:
    return RoleRegistry(load_role_specs())


def unique_strings(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def bounded_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))
