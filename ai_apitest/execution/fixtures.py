from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from ai_apitest.core.case_schema import EXTERNAL_TEST_CAPABILITY_LABELS
from ai_apitest.core.config import resolve_env_vars
from ai_apitest.core.models import TestCase
from ai_apitest.execution.assertions import json_path
from ai_apitest.execution.plugins import PluginRegistry
from ai_apitest.execution.control import ACTIVE_RUNTIME, cleanup_control, check_control


SUPPORTED_PROVIDER_TYPES = {"static", "http", "plugin"}


@dataclass(slots=True)
class FixtureSession:
    cleanup_actions: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    injected_values: dict[str, Any] = field(default_factory=dict)
    plugin_actions: list[tuple[str, Any, Any]] = field(default_factory=list)


def validate_fixture_providers(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise ValueError("fixtures.providers 必须是对象")
    providers: dict[str, dict[str, Any]] = {}
    for raw_name, raw_provider in value.items():
        name = str(raw_name).strip()
        if not name or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"外部能力标识不合法：{raw_name}")
        if not isinstance(raw_provider, dict):
            raise ValueError(f"外部能力 {name} 的配置必须是对象")
        provider = dict(raw_provider)
        if "enabled" in provider and not isinstance(provider["enabled"], bool):
            raise ValueError(f"外部能力 {name} 的 enabled 必须是布尔值")
        provider_type = str(provider.get("type") or "").strip().lower()
        if provider_type not in SUPPORTED_PROVIDER_TYPES:
            raise ValueError(f"外部能力 {name} 仅支持 static、http 或 plugin 提供者")
        provider["type"] = provider_type
        provider["enabled"] = bool(provider.get("enabled", False))
        if provider_type == "static" and not isinstance(provider.get("values"), dict):
            raise ValueError(f"静态外部能力 {name} 必须配置 values 对象")
        if provider_type == "http":
            prepare = provider.get("prepare")
            if not isinstance(prepare, dict) or not str(prepare.get("url") or "").strip():
                raise ValueError(f"HTTP 外部能力 {name} 必须配置 prepare.url")
            cleanup = provider.get("cleanup")
            if cleanup is not None and (
                not isinstance(cleanup, dict) or not str(cleanup.get("url") or "").strip()
            ):
                raise ValueError(f"HTTP 外部能力 {name} 的 cleanup 必须包含 url")
        if provider_type == "plugin" and not str(provider.get("adapter") or "").strip():
            raise ValueError(f"插件外部能力 {name} 必须配置 adapter")
        providers[name] = provider
    return providers


def fixture_provider_config(config: dict[str, Any], requirement: str) -> dict[str, Any] | None:
    fixtures = config.get("fixtures", {})
    providers = fixtures.get("providers", {}) if isinstance(fixtures, dict) else {}
    provider = providers.get(requirement) if isinstance(providers, dict) else None
    if not isinstance(provider, dict) or not provider.get("enabled", False):
        return None
    provider_type = str(provider.get("type") or "").strip().lower()
    if provider_type not in SUPPORTED_PROVIDER_TYPES:
        return None
    if provider_type == "static" and not isinstance(provider.get("values"), dict):
        return None
    if provider_type == "http":
        prepare = provider.get("prepare")
        if not isinstance(prepare, dict) or not str(prepare.get("url") or "").strip():
            return None
    try:
        return resolve_env_vars(provider)
    except ValueError:
        return None


def configured_fixture_requirements(config: dict[str, Any]) -> set[str]:
    fixtures = config.get("fixtures", {})
    providers = fixtures.get("providers", {}) if isinstance(fixtures, dict) else {}
    if not isinstance(providers, dict):
        return set()
    return {
        str(requirement)
        for requirement in providers
        if fixture_provider_config(config, str(requirement)) is not None
    }


def case_has_configured_fixtures(case: TestCase, config: dict[str, Any]) -> bool:
    requirements = {str(item) for item in case.fixture_requirements if str(item)}
    if not requirements or case.risk == "destructive":
        return False
    return requirements.issubset(configured_fixture_requirements(config))


def case_is_executable(case: TestCase, config: dict[str, Any]) -> bool:
    if case.fixture_requirements:
        return case_has_configured_fixtures(case, config)
    return bool(case.runnable)


class FixtureManager:
    def __init__(self, config: dict[str, Any], client: httpx.Client, *, plugins: PluginRegistry | None = None) -> None:
        self.config = config
        self.client = client
        self.plugins = plugins or PluginRegistry.from_config(config)
        fixture_config = config.get("fixtures", {})
        self.timeout_seconds = max(
            0.1,
            float(fixture_config.get("timeout_seconds", 10))
            if isinstance(fixture_config, dict)
            else 10.0,
        )

    def prepare(
        self,
        case: TestCase,
        context: dict[str, Any],
    ) -> tuple[dict[str, Any], FixtureSession]:
        session = FixtureSession()
        steps = []
        for requirement in case.fixture_requirements:
            label = capability_label(requirement)
            provider = fixture_provider_config(self.config, requirement)
            if provider is None:
                steps.append(failed_fixture_step(label, "项目未接入该外部测试能力"))
                break
            if provider.get("type") == "plugin":
                adapter = self.plugins.fixtures.get(provider.get("adapter"))
                if adapter is None:
                    steps.append(failed_fixture_step(label, "外部能力适配器未注册"))
                    break
                try:
                    check_control()
                    session.plugin_actions.append((label, adapter, None))
                    prepared = adapter.prepare(provider, context, self.client)
                    session.plugin_actions[-1] = (label, adapter, prepared.get("cleanup_state"))
                    values = prepared.get("values", {})
                    if not isinstance(values, dict):
                        raise ValueError("插件夹具 values 必须是对象")
                    context.update(values)
                    session.injected_values.update(values)
                    steps.append({"name": label, "provider": "plugin", "status": "passed", "injected_variables": sorted(values), "elapsed_ms": 0, "error": ""})
                except Exception as exc:
                    steps.append(failed_fixture_step(label, str(exc)))
                    break
                continue
            cleanup = provider.get("cleanup")
            if isinstance(cleanup, dict) and cleanup.get("url"):
                session.cleanup_actions.append((label, cleanup))
            result = self._prepare_provider(label, provider, context)
            steps.append(result)
            if result["status"] != "passed":
                break
            for name in result.get("injected_variables", []):
                session.injected_values[str(name)] = context.get(str(name))
        status = "passed" if len(steps) == len(case.fixture_requirements) and all(
            item["status"] == "passed" for item in steps
        ) else "failed"
        return lifecycle_result("external_capability_setup", status, steps), session

    def cleanup(
        self,
        session: FixtureSession,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        if not session.cleanup_actions and not session.plugin_actions:
            return {}
        steps = []
        for label, action in reversed(session.cleanup_actions):
            with cleanup_control(self.timeout_seconds):
                result, _ = self._request_action(label, action, context, extract_values=False)
            steps.append(result)
        for label, adapter, state in reversed(session.plugin_actions):
            try:
                with cleanup_control(self.timeout_seconds):
                    adapter.cleanup(state, context, self.client)
                steps.append({"name": label, "provider": "plugin", "status": "passed", "elapsed_ms": 0, "error": ""})
            except Exception as exc:
                steps.append(failed_fixture_step(label, str(exc)))
        status = "passed" if all(item["status"] in {"passed", "not_needed"} for item in steps) else "failed"
        return lifecycle_result("external_capability_cleanup", status, steps)

    def _prepare_provider(
        self,
        label: str,
        provider: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        provider_type = str(provider.get("type") or "").lower()
        if provider_type == "static":
            values = render_fixture_template(provider.get("values", {}), context)
            if not isinstance(values, dict):
                return failed_fixture_step(label, "静态能力配置的 values 必须是对象")
            context.update(values)
            return {
                "name": label,
                "provider": "static",
                "status": "passed",
                "elapsed_ms": 0,
                "error": "",
                "injected_variables": sorted(str(key) for key in values),
            }
        result, values = self._request_action(
            label,
            provider["prepare"],
            context,
            extract_values=True,
        )
        if result["status"] == "passed":
            static_values = render_fixture_template(provider.get("values", {}), context)
            if isinstance(static_values, dict):
                values = {**static_values, **values}
            context.update(values)
            result["injected_variables"] = sorted(str(key) for key in values)
        return result

    def _request_action(
        self,
        label: str,
        action: dict[str, Any],
        context: dict[str, Any],
        *,
        extract_values: bool,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        import time

        started = time.perf_counter()
        method = str(action.get("method") or "POST").upper()
        url = str(render_fixture_template(action.get("url", ""), context)).strip()
        if not url.startswith(("http://", "https://")):
            return failed_fixture_step(label, "HTTP 能力地址必须是完整的 http/https URL"), {}
        headers = render_fixture_template(action.get("headers", {}), context)
        query = render_fixture_template(action.get("query", {}), context)
        body = render_fixture_template(action.get("json", action.get("body")), context)
        expected = action.get("expected_status", [200, 201, 202, 204])
        try:
            check_control()
            missing = {name for name in re.findall(r"{{\s*(\w+)\s*}}", str(action)) if context.get(name) in (None, "")}
            if missing:
                return {**failed_fixture_step(label, f"缺少变量：{', '.join(sorted(missing))}"), "status": "not_needed" if not extract_values else "failed"}, {}
            expected_statuses = {
                int(item)
                for item in (expected if isinstance(expected, list) else [expected])
            }
            request_kwargs: dict[str, Any] = {
                "headers": headers if isinstance(headers, dict) else {},
                "params": query if isinstance(query, dict) else {},
                "timeout": self.timeout_seconds,
            }
            if runtime := ACTIVE_RUNTIME.get():
                if (remaining := runtime.control.remaining()) is not None:
                    request_kwargs["timeout"] = min(self.timeout_seconds, remaining)
            if "json" in action or "body" in action:
                request_kwargs["json"] = body
            from ai_apitest.execution.runner import read_response_content
            from ai_apitest.core.sandbox import check_url
            check_url(url)
            with self.client.stream(method, url, **request_kwargs) as response:
                content, truncated = read_response_content(response, int(self.config.get("execution", {}).get("max_response_bytes", 5000000)))
            if truncated:
                raise ValueError("夹具响应超过大小限制")
            if response.status_code not in expected_statuses:
                return failed_fixture_step(
                    label,
                    f"项目方能力调用返回 HTTP {response.status_code}，期望 {sorted(expected_statuses)}",
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                ), {}
            values: dict[str, Any] = {}
            if extract_values:
                extract = action.get("extract", {})
                if not isinstance(extract, dict):
                    return failed_fixture_step(label, "HTTP 能力的 extract 必须是对象"), {}
                import json
                response_json = json.loads(content) if extract and content else {}
                for name, path in extract.items():
                    value = json_path(response_json, str(path))
                    if value is None:
                        return failed_fixture_step(label, f"能力响应缺少待注入变量：{name}"), {}
                    values[str(name)] = value
                    context[str(name)] = value
            return {
                "name": label,
                "provider": "http",
                "status": "passed",
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
                "error": "",
                "status_code": response.status_code,
            }, values
        except Exception as exc:
            return failed_fixture_step(
                label,
                f"项目方能力调用失败：{exc}",
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            ), {}


def capability_label(requirement: str) -> str:
    return EXTERNAL_TEST_CAPABILITY_LABELS.get(
        str(requirement),
        "额外的受控场景准备能力",
    )


def failed_fixture_step(label: str, error: str, *, elapsed_ms: int = 0) -> dict[str, Any]:
    return {
        "name": label,
        "status": "failed",
        "elapsed_ms": elapsed_ms,
        "error": error,
        "injected_variables": [],
    }


def lifecycle_result(lifecycle: str, status: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "lifecycle": lifecycle,
        "status": status,
        "total": len(steps),
        "failed": sum(item.get("status") == "failed" for item in steps),
        "skipped": 0,
        "not_needed": 0,
        "steps": steps,
    }


def render_fixture_template(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, str):
        match = re.fullmatch(r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}", value)
        if match:
            return context.get(match.group(1), "")
        return re.sub(
            r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}",
            lambda item: str(context.get(item.group(1), "")),
            value,
        )
    if isinstance(value, list):
        return [render_fixture_template(item, context) for item in value]
    if isinstance(value, dict):
        return {key: render_fixture_template(item, context) for key, item in value.items()}
    return value
