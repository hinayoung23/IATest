"""Explicitly enabled, deterministic project extensions; never exposed as AI tools."""
from __future__ import annotations

from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any, Callable

from ai_apitest.core.sandbox import ACTIVE_POLICY, SandboxViolation


@dataclass
class PluginRegistry:
    assertions: dict[str, Callable] = field(default_factory=dict)
    fixtures: dict[str, Any] = field(default_factory=dict)
    before_request: list[Callable] = field(default_factory=list)
    after_response: list[Callable] = field(default_factory=list)

    def register_assertion(self, name: str, handler: Callable) -> None:
        if not name or name in self.assertions or not callable(handler):
            raise ValueError(f"自定义断言重复或无效：{name}")
        self.assertions[name] = handler

    def register_fixture(self, name: str, provider: Any) -> None:
        if not name or name in self.fixtures or not callable(getattr(provider, "prepare", None)) or not callable(getattr(provider, "cleanup", None)):
            raise ValueError(f"夹具适配器重复或无效：{name}")
        self.fixtures[name] = provider

    @classmethod
    def from_config(cls, config: dict) -> PluginRegistry:
        result = cls()
        configured = config.get("plugins", [])
        if not isinstance(configured, list) or any(not isinstance(name, str) for name in configured):
            raise ValueError("plugins 必须是已安装插件名称的数组")
        if configured and (policy := ACTIVE_POLICY.get()) and policy.mode != "off" and not policy.allow_plugins:
            raise SandboxViolation("沙箱默认禁用插件；可信插件需显式设置 sandbox.allow_plugins=true")
        installed = {item.name: item for item in entry_points(group="iatest.plugins")}
        for name in configured:
            if name not in installed:
                raise ValueError(f"插件未安装：{name}")
            installed[name].load()(result)
        return result
