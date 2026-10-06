from __future__ import annotations

import httpx
import uuid
import time
from typing import Callable

from ai_apitest.core.models import TestCase
from ai_apitest.execution.fixtures import FixtureManager, fixture_provider_config, validate_fixture_providers
from ai_apitest.execution.plugins import PluginRegistry


def check_fixtures(config: dict, cases: list[TestCase], client: httpx.Client, *, probe: bool = False, exercise: bool = False, plugins: PluginRegistry | None = None, on_result: Callable[[dict], None] | None = None) -> list[dict]:
    providers = validate_fixture_providers(config.get("fixtures", {}).get("providers", {}))
    required = {requirement for case in cases for requirement in case.fixture_requirements}
    manager = FixtureManager(config, client, plugins=plugins)
    results = []
    for name in sorted(set(providers) | required):
        provider = fixture_provider_config(config, name)
        row = {"name": name, "required": name in required, "status": "configured", "error": "", "probed": False}
        if provider is None:
            row.update(status="missing" if name in required else "disabled", error="提供者未启用、缺失或环境变量未配置")
        elif provider["type"] == "plugin" and provider.get("adapter") not in manager.plugins.fixtures:
            row.update(status="failed", error="插件夹具适配器未注册")
        elif exercise:
            case = TestCase("fixture-check", name, "GET", "/", fixture_requirements=[name])
            context = {"uuid": uuid.uuid4().hex, "random": uuid.uuid4().hex, "timestamp": str(time.time_ns()), "iatest_namespace": config.get("execution", {}).get("namespace") or uuid.uuid4().hex}
            setup, session = manager.prepare(case, context)
            try:
                row.update(status=setup["status"], setup=setup)
            finally:
                cleanup = manager.cleanup(session, context)
                row["cleanup"] = cleanup
                if cleanup.get("status") == "failed":
                    row["status"] = "failed"
        elif probe and provider.get("check"):
            action = {"method": "GET", **provider["check"]}
            if action["method"].upper() not in {"GET", "HEAD"}:
                row.update(status="failed", error="只读自检仅允许 GET/HEAD；执行 prepare/cleanup 请使用 --exercise")
            else:
                result, _ = manager._request_action(name, action, {}, extract_values=False)
                row.update(status=result["status"], error=result["error"], probed=True)
        results.append(row)
        if on_result:
            on_result(row)
    return results
