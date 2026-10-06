from __future__ import annotations

import json
from typing import Any, Callable

from ai_apitest.ai.prompting import render_prompt
from ai_apitest.ai.provider import DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS, chat_completion, extract_json_array, retry_with_json_repair
from ai_apitest.core.models import Endpoint
from ai_apitest.core.serde import endpoint_from_dict


def discover_endpoints_with_llm(
    text: str,
    llm: dict,
    stream_callback: Callable[[str], None] | None = None,
    *,
    quality_feedback: dict[str, Any] | None = None,
) -> list[Endpoint]:
    messages = [
        {
            "role": "user",
            "content": render_prompt("api_discovery.md", {"DOCUMENT_TEXT": text}),
        }
    ]
    if quality_feedback:
        messages.append(
            {
                "role": "user",
                "content": (
                    "上一轮质量审计未通过。只根据上方原始文档修复以下问题；"
                    "不得为了提高分数补充文档没有声明的字段：\n"
                    + json.dumps(quality_feedback, ensure_ascii=False, separators=(",", ":"), default=str)
                ),
            }
        )
    content = chat_completion(
        base_url=llm.get("base_url", ""),
        model=llm.get("model", ""),
        api_key_env=llm.get("api_key_env", ""),
        provider=llm.get("provider", "openai-compatible"),
        messages=messages,
        timeout=DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS,
        stream_callback=stream_callback,
    )
    payload, _warning = parse_endpoint_payload(
        content,
        base_url=llm.get("base_url", ""),
        model=llm.get("model", ""),
        api_key_env=llm.get("api_key_env", ""),
        provider=llm.get("provider", "openai-compatible"),
    )
    return endpoints_from_payload(payload)


def parse_endpoint_payload(
    content: str,
    *,
    base_url: str = "",
    model: str = "",
    api_key_env: str = "",
    provider: str = "openai-compatible",
) -> tuple[list[dict[str, Any]], str]:
    """Parse LLM endpoint output with self-repair on JSON failure."""
    def _parse(text: str) -> list[dict[str, Any]]:
        try:
            items = extract_json_array(text)
            if not isinstance(items, list):
                raise ValueError("不是 JSON 数组")
            return items
        except Exception:
            stripped = strip_code_fence(text)
            data = json.loads(stripped)
            if isinstance(data, dict) and isinstance(data.get("endpoints"), list):
                return data["endpoints"]
            if isinstance(data, list):
                return data
            raise

    if base_url and model and api_key_env:
        try:
            return retry_with_json_repair(
                content, parse_json=_parse,
                base_url=base_url, model=model, api_key_env=api_key_env, provider=provider,
            )
        except ValueError:
            # Repair exhausted — try bare fallback one last time
            pass
    # Fallback: try parsing without repair (legacy behavior)
    return _parse(content), ""


def strip_code_fence(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = stripped.removeprefix("```json").removeprefix("```").strip()
        if stripped.endswith("```"):
            stripped = stripped[:-3].strip()
    return stripped


def endpoints_from_payload(items: list[dict[str, Any]]) -> list[Endpoint]:
    endpoints: list[Endpoint] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        method = str(item.get("method", "")).upper()
        path = str(item.get("path", ""))
        if not method or not path:
            continue
        payload = {
            "method": method,
            "path": path,
            "summary": item.get("summary") or item.get("name") or f"{method} {path}",
            "description": item.get("description") or "",
            "parameters": item.get("parameters") if isinstance(item.get("parameters"), list) else [],
            "request_body": item.get("request_body") or item.get("body"),
            # Missing response documentation stays unknown; inventing a 200 here
            # later turns into an unsupported assertion in generated cases.
            "responses": item.get("responses") if isinstance(item.get("responses"), dict) else {},
            "tags": item.get("tags") if isinstance(item.get("tags"), list) else [],
            "operation_id": item.get("operation_id") or item.get("operationId", ""),
            "deprecated": bool(item.get("deprecated", False)),
            "security": item.get("security") if isinstance(item.get("security"), list) else [],
            "business_rules": item.get("business_rules") if isinstance(item.get("business_rules"), list) else [],
        }
        endpoints.append(endpoint_from_dict(payload))
    return endpoints


def generate_dependency_graph(
    endpoints: list[Endpoint],
    llm: dict,
) -> dict[str, Any] | None:
    """Master generates a structured data dependency graph from discovered endpoints."""
    from ai_apitest.ai.provider import chat_completion, retry_with_json_repair
    from ai_apitest.core.serde import to_jsonable

    if not llm.get("base_url") or not llm.get("model") or not llm.get("api_key_env"):
        return None

    ep_data = []
    for ep in endpoints:
        ep_data.append({
            "method": ep.method, "path": ep.path,
            "summary": ep.summary or "", "description": (ep.description or "")[:300],
            "parameters": [{"name": p.get("name",""), "in": p.get("in",""), "required": p.get("required",False),
                            "schema": p.get("schema",{})} for p in (ep.parameters or [])],
            "responses": {str(k): str(v)[:200] for k, v in (ep.responses or {}).items()},
        })

    prompt = f"""你是 API 接口关系分析专家。分析以下接口，找出数据依赖关系：如果接口 A 的响应包含某字段，接口 B 的路径参数/查询参数/请求体需要同一字段，则 A→B 存在依赖边。

对每条边标注：from/to、type（produce_to_path_param/query/body）、produced_field、extract（如 $.email）、inject（如 path_params.email）、safe_to_prepare、confidence（high/medium/low）、reason。

**重要**：safe_to_prepare 判断标准不是操作本身是否"破坏性"（如 DELETE），而是"能否通过 setup_steps 安全准备测试数据"。DELETE/POST/PUT 的正向用例通过 setup_steps 调用生产者创建临时数据，操作完成后通过 cleanup_steps 清理，因此 safe_to_prepare=true。只有无法准备数据或无法清理的操作才标记 false。

只输出 JSON：
```json
{{"edges": [{{"from": {{"method":"POST","path":"/users"}}, "to": {{"method":"DELETE","path":"/users/{{id}}"}}, "type": "produce_to_path_param", "produced_field": "id", "extract": {{"id": "$.id"}}, "inject": {{"path_params.id": "{{{{id}}}}"}}, "safe_to_prepare": true, "confidence": "high", "reason": "..."}}]}}
```

接口列表：
{json.dumps(ep_data, ensure_ascii=False, indent=2)}"""

    content = chat_completion(
        base_url=llm.get("base_url", ""), model=llm.get("model", ""),
        api_key_env=llm.get("api_key_env", ""), provider=llm.get("provider", "openai-compatible"),
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        result, _warning = retry_with_json_repair(
            content, parse_json=lambda t: _parse_dep_graph(t, endpoints),
            base_url=llm.get("base_url",""), model=llm.get("model",""),
            api_key_env=llm.get("api_key_env",""), provider=llm.get("provider","openai-compatible"),
            description="（接口依赖图谱）",
        )
        return result
    except ValueError:
        return None


def _parse_dep_graph(content: str, endpoints: list[Endpoint]) -> dict[str, Any]:
    import json as _json
    endpoint_keys = {(ep.method, ep.path) for ep in endpoints}
    # Try direct JSON parse first, then array extraction
    stripped = content.strip()
    if stripped.startswith("{"):
        data = _json.loads(stripped)
    else:
        from ai_apitest.ai.provider import extract_json_array
        arr = extract_json_array(stripped)
        data = {"edges": arr} if isinstance(arr, list) else arr[0] if arr else {}
    edges = []
    for e in data.get("edges", []) if isinstance(data, dict) else (data if isinstance(data, list) else []):
        if not isinstance(e, dict):
            continue
        frm = e.get("from", {})
        to = e.get("to", {})
        f_key = (str(frm.get("method","")).upper(), str(frm.get("path","")))
        t_key = (str(to.get("method","")).upper(), str(to.get("path","")))
        if f_key not in endpoint_keys or t_key not in endpoint_keys:
            continue
        confidence = str(e.get("confidence", "medium"))
        if confidence not in ("high", "medium", "low"):
            confidence = "medium"
        edges.append({
            "from": {"method": f_key[0], "path": f_key[1]},
            "to": {"method": t_key[0], "path": t_key[1]},
            "type": str(e.get("type", "produce_to_path_param")),
            "produced_field": str(e.get("produced_field", "")),
            "extract": e.get("extract", {}),
            "inject": e.get("inject", {}),
            "safe_to_prepare": bool(e.get("safe_to_prepare", True)),
            "confidence": confidence,
            "reason": str(e.get("reason", "")),
        })
    return {
        "edges": edges,
        "rule": "safe_to_prepare=true 表示可通过生产者准备测试数据。DELETE/POST/PUT/PATCH 正向用例不应标记 destructive/runnable=false，应通过 setup_steps 调用生产者。路径参数用 {{variable}} 模板引用 extract 变量，不得硬编码。"
    }
