from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from ai_apitest.agents.identity import RoleRegistry, default_role_registry
from ai_apitest.ai.api_discovery import _parse_dep_graph, endpoints_from_payload
from ai_apitest.ai.model_gateway import ModelGatewayProtocol, ProviderModelGateway
from ai_apitest.core.models import Endpoint
from ai_apitest.discovery.document import extract_markdown
from ai_apitest.tools.builtin import build_case_generation_tool_registry
from ai_apitest.tools.registry import ToolRegistry
from ai_apitest.application.agent_service import run_agent_role


GatewayFactory = Callable[[dict[str, Any]], ModelGatewayProtocol]


def discover_endpoints_with_agent_runtime(
    root: Path,
    project: str,
    document_text: str,
    llm: dict[str, Any],
    *,
    expected_endpoints: list[Endpoint] | None = None,
    quality_feedback: dict[str, Any] | None = None,
    quality_round: int = 1,
    stream_callback: Callable[[str], None] | None = None,
    gateway_factory: GatewayFactory = ProviderModelGateway,
    role_registry: RoleRegistry | None = None,
    tool_registry: ToolRegistry | None = None,
) -> list[Endpoint]:
    if not document_text.strip():
        return []
    normalized_document = normalize_document_for_agent(document_text)
    expected_keys = endpoint_key_payload(expected_endpoints or [])
    inventory = json.dumps(expected_keys, ensure_ascii=False, separators=(",", ":"))
    roles = role_registry or default_role_registry()
    tools = tool_registry or build_case_generation_tool_registry()
    result = run_agent_role(
        root=root,
        project=project,
        endpoints=[],
        role_id="api_analyst",
        task_prompt=(
            f"识别当前项目接口文档中的全部真实接口。规范化文档共 {len(normalized_document)} 个字符。"
            f"本地解析器已确定以下接口至少必须存在：{inventory}。该清单只约束 method/path 完整性，"
            "其他字段仍须以文档证据为准；如果文档中还有真实接口也必须一并提交。"
            "先调用 get_document_info，再从 offset=0 开始连续调用 read_document_chunk，直到 has_more=false。"
            "只根据读取到的文档证据提取 method、path、summary、description、parameters、request_body、responses、tags、operation_id、deprecated、security。"
            "每个接口还必须提交 business_rules：把文档中影响输入、响应、状态转换、生命周期、排序、限制或错误触发条件的规则逐条结构化，"
            "包含 rule_id、title、description、category、strategy、testable、preconditions、lifecycle_state、trigger、expected、"
            "runnable、risk、fixture_requirements、source_section、source_evidence。trigger 中给出文档能证明的 request_overrides、"
            "omit_fields 或 chain_steps；expected 中给出 status、basis、description 和 assertions。"
            "无法直接执行但文档明确的规则也必须保留，并声明 fixture_requirements，不能省略。"
            "不要猜测文档未声明的响应或字段。完整阅读后调用 submit_endpoint_drafts，且每次必须只提交一个接口；"
            "工具返回 incomplete_endpoint_inventory 时，从 missing 中选择下一个接口继续单独提交，"
            "直到工具确认全部接收。不要输出思考过程。"
            + (
                "\n\n上一轮质量审计未通过。必须依据文档证据修复以下问题，不得为了提高分数猜测字段："
                + json.dumps(quality_feedback, ensure_ascii=False, separators=(",", ":"), default=str)
                if quality_feedback
                else ""
            )
        ),
        model_config={
            **llm,
            "default_output_tokens": max(
                12000,
                int(llm.get("default_output_tokens") or 8000),
            ),
        },
        roles=roles,
        tools=tools,
        enabled_tools=("get_document_info", "read_document_chunk", "submit_endpoint_drafts"),
        gateway_factory=gateway_factory,
        stream_callback=stream_callback,
        metadata={
            "task": "api_discovery",
            "document_chars": len(document_text),
            "normalized_document_chars": len(normalized_document),
            "expected_endpoint_count": len(expected_keys),
            "quality_round": quality_round,
        },
        state={
            "document_text": normalized_document,
            "expected_endpoint_keys": expected_keys,
            "forced_tool_queue": ["get_document_info"],
        },
    )
    if not isinstance(result.artifact, list):
        raise ValueError("api_analyst 没有提交有效接口草稿")
    endpoints = endpoints_from_payload([item for item in result.artifact if isinstance(item, dict)])
    deduped: list[Endpoint] = []
    seen: set[tuple[str, str]] = set()
    for endpoint in endpoints:
        key = (endpoint.method.upper(), endpoint.path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(endpoint)
    missing = {
        (item["method"], item["path"])
        for item in expected_keys
    } - {(item.method.upper(), item.path) for item in deduped}
    if missing:
        detail = ", ".join(f"{method} {path}" for method, path in sorted(missing))
        raise ValueError(f"接口识别结果不完整，缺少本地文档清单中的接口：{detail}")
    return deduped


def normalize_document_for_agent(document_text: str) -> str:
    """Remove rendered HTML/CSS duplication while preserving plain text specs verbatim."""
    lowered = document_text[:2000].lower()
    if "<html" in lowered or "id=\"markdown-source\"" in document_text or "id='markdown-source'" in document_text:
        normalized = extract_markdown(document_text)
        if normalized.strip():
            return normalized
    return document_text


def endpoint_key_payload(endpoints: list[Endpoint]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for endpoint in endpoints:
        key = (endpoint.method.upper(), endpoint.path)
        if key in seen:
            continue
        seen.add(key)
        result.append({"method": key[0], "path": key[1]})
    return result


def generate_dependency_graph_with_agent_runtime(
    root: Path,
    project: str,
    endpoints: list[Endpoint],
    llm: dict[str, Any],
    *,
    existing_graph: dict[str, Any] | None = None,
    stream_callback: Callable[[str], None] | None = None,
    gateway_factory: GatewayFactory = ProviderModelGateway,
    role_registry: RoleRegistry | None = None,
    tool_registry: ToolRegistry | None = None,
) -> dict[str, Any]:
    if not endpoints:
        return {"edges": []}
    roles = role_registry or default_role_registry()
    tools = tool_registry or build_case_generation_tool_registry()
    result = run_agent_role(
        root=root,
        project=project,
        endpoints=endpoints,
        role_id="api_analyst",
        task_prompt=(
            f"分析当前项目 {len(endpoints)} 个接口之间的数据依赖。先调用 list_endpoints，再按需读取接口详情。"
            "只有生产者响应字段能够注入消费者 path/query/body 时才建立边。"
            "safe_to_prepare 表示能否通过临时数据准备并清理，不等同于消费者 HTTP 方法是否为写操作。"
            "本地解析器已给出以下确定性候选，不要重复或删除；只补充字段名无法直接匹配但文档语义明确的依赖："
            + json.dumps(existing_graph or {"edges": []}, ensure_ascii=False, separators=(",", ":"))
            + "。"
            "完成后调用 submit_dependency_graph，不要提交接口草稿。"
        ),
        model_config=llm,
        roles=roles,
        tools=tools,
        enabled_tools=("list_endpoints", "get_endpoint_detail", "submit_dependency_graph"),
        gateway_factory=gateway_factory,
        stream_callback=stream_callback,
        metadata={"task": "dependency_graph", "endpoint_count": len(endpoints)},
    )
    if not isinstance(result.artifact, dict) or not isinstance(result.artifact.get("edges"), list):
        raise ValueError("api_analyst 没有提交有效依赖图")
    return _parse_dep_graph(json.dumps(result.artifact, ensure_ascii=False), endpoints)
