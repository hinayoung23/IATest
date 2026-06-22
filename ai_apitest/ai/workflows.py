from __future__ import annotations

from pathlib import Path
from typing import Callable

from ai_apitest.ai.api_discovery import discover_endpoints_with_llm
from ai_apitest.ai.case_generation import generate_cases_with_llm, generate_supplemental_cases_with_llm, generate_coverage_plan, generate_cases_from_plan_batch
from ai_apitest.ai.conflict_review import review_conflicts_with_llm
from ai_apitest.ai.llm_config import is_llm_configured, normalize_llm_config, resolve_llm_role
from ai_apitest.ai.provider import DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS
from ai_apitest.ai.result_analysis import analyze_results_with_llm, review_failed_results_with_llm
from ai_apitest.context.extraction import process_report_analysis
from ai_apitest.context.injection import build_context_bundle
from ai_apitest.context.storage import apply_conflict_review, audit, conflict_pairs, context_connection, merge_lesson_into, now_iso, record_case_generation_lessons
from ai_apitest.core.paths import project_docs_dir, safe_project_name
from ai_apitest.core.config import load_config
from ai_apitest.core.models import CaseResult, Endpoint, TestCase
from ai_apitest.generator.cases import attach_cleanup_steps, attach_setup_steps, generate_cases


DEFAULT_STRATEGY_DESCRIPTIONS = {
    "happy_path": "合法参数、正常返回的主流程用例；所有接口至少生成 1 条。",
    "optional_field": "覆盖可选参数缺省、只传必填参数、可选参数不同组合。",
    "type_error": "覆盖 query/path/body 字段类型错误、格式错误。",
    "boundary_value": "覆盖字符串长度、数值范围、数组长度、空字符串等边界值。",
    "path_parameter_error": "覆盖路径参数为空、非法格式、不存在资源等异常。",
    "dependency_chain": "识别接口依赖关系，生成先创建/生成数据再查询、更新、删除的链路用例。",
    "auth_error": "覆盖缺少鉴权、错误 Token、过期 Token 等鉴权异常。",
    "equivalence_class": "按有效/无效等价类选择代表数据，减少重复但覆盖关键输入类别。",
    "status_code_coverage": "只覆盖接口文档明确声明的响应状态码；没有声明的 4xx/5xx 不要猜测补充。",
}

def configured_llm(root: Path, role: str) -> dict:
    return resolve_llm_role(normalize_llm_config(load_config(root).get("llm", {})), role)


def case_generation_strategy_context(root: Path) -> dict:
    generation = load_config(root).get("case_generation", {})
    default_flags = generation.get("default_strategies", {})
    enabled_defaults = []
    for name, description in DEFAULT_STRATEGY_DESCRIPTIONS.items():
        if default_flags.get(name, True):
            enabled_defaults.append({"name": name, "description": description})
    custom = []
    for strategy in generation.get("custom_strategies", []):
        if not strategy.get("enabled", True):
            continue
        name = str(strategy.get("name", "")).strip()
        if name:
            custom.append({"name": name, "description": str(strategy.get("description", "")).strip()})
    return {
        "enabled_default_strategies": enabled_defaults,
        "custom_strategies": custom,
        "generation_requirements": [
            "按启用策略尽可能全面生成用例，不要只生成 happy_path。",
            "无法安全执行的破坏性、依赖外部状态或明显缺少前置数据的负向用例，runnable 应设为 false，并说明 risk。",
            "存在接口依赖时优先使用 steps 生成链路用例，例如先生成邮箱再查询、续期或删除。",
        ],
    }


def apply_ai_discovery_if_configured(
    root: Path,
    text: str,
    endpoints: list[Endpoint],
    metadata: dict,
    stream_callback: Callable[[str], None] | None = None,
) -> tuple[list[Endpoint], dict]:
    llm = configured_llm(root, "master")
    if not is_llm_configured(llm):
        return endpoints, metadata
    try:
        ai_endpoints = discover_endpoints_with_llm(text, llm, stream_callback=stream_callback)
    except Exception as exc:
        metadata["ai_discovery_error"] = str(exc)
        return endpoints, metadata
    if not ai_endpoints:
        metadata["ai_discovery_error"] = "大模型未识别到接口"
        return endpoints, metadata
    metadata["source_type"] = f"{metadata.get('source_type', 'document')}+ai"
    metadata["ai_discovery"] = True
    # Generate dependency graph via master
    try:
        from ai_apitest.ai.api_discovery import generate_dependency_graph
        dep_graph = generate_dependency_graph(ai_endpoints, llm)
        if dep_graph and dep_graph.get("edges"):
            metadata["dependency_graph"] = dep_graph
    except Exception:
        pass
    return ai_endpoints, metadata


def generate_cases_with_config(
    root: Path,
    endpoints: list[Endpoint],
    project: str = "default",
    stream_callback: Callable[[str], None] | None = None,
    llm_override: dict | None = None,
) -> tuple[list[TestCase], str]:
    llm = llm_override or configured_llm(root, "case_generator")
    if not is_llm_configured(llm):
        return generate_cases(endpoints), ""
    strategy_context = case_generation_strategy_context(root)
    config = load_config(root)
    context_block = ""
    try:
        context_block = build_context_bundle(
            root, project, task_type="case_generation", model_config=llm,
            context_config=config.get("context", {}),
            system_prompt="", user_prompt_base="",
            endpoints=endpoints, environment=str(config.get("default_environment", "")),
        ).text
    except Exception:
        context_block = ""
    base_kwargs = dict(base_url=llm.get("base_url",""), model=llm.get("model",""),
                       api_key_env=llm.get("api_key_env",""), provider=llm.get("provider","openai-compatible"),
                       timeout=DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS,
                       max_tokens=int(llm.get("default_output_tokens", 8000)))

    # Read dependency graph from project metadata (master-generated during API discovery)
    from ai_apitest.core.serde import read_json as _read_json
    dep_graph = _read_json(project_docs_dir(root, project) / "metadata.json", {}).get("dependency_graph")
    if dep_graph:
        dep_issues = validate_dependency_graph(dep_graph, endpoints)
        if dep_issues:
            dep_graph = None  # invalidate if structural issues found

    # ══ Phase 1: Coverage plan ══
    try:
        if stream_callback:
            stream_callback("开始生成覆盖计划...\n")
        plan, plan_warning = generate_coverage_plan(
            endpoints=endpoints, strategy_context=strategy_context,
            context_block=context_block, dep_graph=dep_graph,
            stream_callback=stream_callback, **base_kwargs,
        )
    except Exception as exc:
        return [], f"覆盖计划生成失败（模型 {llm.get('model','?')}）：{exc}。可检查 API 连通性或稍后重试。"
    if not plan:
        return [], "大模型未生成覆盖计划。请检查接口定义和生成策略配置。"
    if stream_callback:
        stream_callback(f"\n覆盖计划生成完成：{len(plan)} 个场景。\n")

    # ── System hard gap fill ──
    plan = _fill_hard_gaps(plan, endpoints, dep_graph=dep_graph)
    plan = _fill_doc_based_gaps(plan, endpoints)

    # ── Plan design_method consistency check ──
    plan_dm_issues = _detect_plan_design_method_mismatch(plan)
    diag_parts: list[str] = []
    if plan_dm_issues:
        sample = ", ".join(i.split(":")[0] for i in plan_dm_issues[:3])
        diag_parts.append(f"覆盖计划 {len(plan_dm_issues)} 个场景设计方法与标题语义不一致：{sample}")

    # ── Master plan review (lightweight, high-ROI) ──
    _review_coverage_plan(root, project, plan, endpoints)

    # ══ Phase 2: Batch generation from plan ══
    from collections import defaultdict
    batches: list[list[dict[str, Any]]] = []
    chain_items = [p for p in plan if p["endpoint_method"] == "CHAIN"]
    ep_items = [p for p in plan if p["endpoint_method"] != "CHAIN"]
    by_endpoint: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for p in ep_items:
        by_endpoint[(p["endpoint_method"], p["endpoint_path"])].append(p)
    ep_keys = list(by_endpoint.keys())
    for i in range(0, len(ep_keys), 4):
        batch = []
        for key in ep_keys[i:i + 4]:
            batch.extend(by_endpoint[key])
        batches.append(batch)
    if chain_items:
        batches.append(chain_items)

    all_cases: list[TestCase] = []
    diag_parts.append(f"plan: {len(plan)} 场景, {len(batches)} 批次")
    unresolved_scenario_ids: set[str] = set()
    counters: dict[str, int] = {}

    for bi, batch in enumerate(batches):
        batch_sids = {p["scenario_id"] for p in batch}
        try:
            if stream_callback:
                stream_callback(f"\n开始生成第 {bi + 1}/{len(batches)} 批用例，覆盖 {len(batch)} 个场景...\n")
            batch_cases, bw = generate_cases_from_plan_batch(
                plan_items=batch, endpoints=endpoints,
                context_block=context_block, existing_cases=all_cases,
                dep_graph=dep_graph, stream_callback=stream_callback, **base_kwargs,
            )
        except Exception:
            batch_cases, bw = [], ""
        # Only report scenario_id issues if they persist through retries
        if bw and "缺失" in bw:
            pass  # track in unresolved, report at end
        elif bw:
            diag_parts.append(bw)

        if batch_cases:
            batch_cases = attach_cleanup_steps(attach_setup_steps(batch_cases, endpoints), endpoints)
            accepted = dedupe_supplemental_cases(all_cases, batch_cases)
            all_cases = all_cases + accepted
            # Coverage based on actually-accepted cases (post-dedupe)
            accepted_sids = {getattr(c, "scenario_id", "") for c in accepted}
            missing_items = [p for p in batch if p["scenario_id"] not in accepted_sids]
        else:
            missing_items = list(batch)

        # Retry missing scenarios
        retry_count = 0
        while missing_items and retry_count < 2:
            retry_count += 1
            try:
                if stream_callback:
                    stream_callback(f"\n第 {bi + 1} 批仍缺 {len(missing_items)} 个场景，正在第 {retry_count} 次补生成...\n")
                retry_cases, rw = generate_cases_from_plan_batch(
                    plan_items=missing_items, endpoints=endpoints,
                    context_block=context_block, existing_cases=all_cases,
                    dep_graph=dep_graph, stream_callback=stream_callback, **base_kwargs,
                )
                if rw and "缺失" not in rw:
                    diag_parts.append(f"补生成: {rw}")
            except Exception:
                retry_cases = []
            if retry_cases:
                retry_cases = attach_cleanup_steps(attach_setup_steps(retry_cases, endpoints), endpoints)
                accepted_retry = dedupe_supplemental_cases(all_cases, retry_cases)
                all_cases = all_cases + accepted_retry
                retry_sids = {getattr(c, "scenario_id", "") for c in accepted_retry}
                missing_items = [p for p in missing_items if p["scenario_id"] not in retry_sids]
        unresolved_scenario_ids.update(p["scenario_id"] for p in missing_items)

    # Pre-governance coverage check
    missing_pre = _verify_plan_scenario_coverage(plan, all_cases)
    if missing_pre:
        unresolved_scenario_ids.update(missing_pre)

    if not all_cases:
        return [], "两阶段生成未产出有效用例。" + "; ".join(diag_parts)

    # Record lessons
    try:
        record_case_generation_lessons(root, project, all_cases, actor="ai:case_generator")
    except Exception:
        pass
    # Governance
    if _governance_enabled(root):
        revised = _govern_cases(root, project, all_cases, endpoints)
        if revised:
            all_cases = revised
            # Re-check after governance
            missing_post = _verify_plan_scenario_coverage(plan, all_cases)
            if missing_post:
                unresolved_scenario_ids.update(missing_post)
                diag_parts.append(f"治理后发现 {len(missing_post)} 个计划场景无对应用例")

    # ══ Final unified status (after governance) ══
    # Pre-check silently, then repair, then report final status
    final_gaps = _detect_coverage_gaps(all_cases, endpoints)
    final_missing = _verify_plan_scenario_coverage(plan, all_cases)
    dm_mismatches = _detect_design_method_mismatch(plan, all_cases)
    if final_missing:
        unresolved_scenario_ids.update(final_missing)

    # ── One-shot targeted fix (uses case_generator, not governance-gated) ──
    fix_attempted = False
    case_gen = configured_llm(root, "case_generator")
    if (dm_mismatches or final_gaps) and is_llm_configured(case_gen):
        affected_sids: set[str] = set()
        for m in dm_mismatches:
            sid = m.split("(")[1].split(")")[0] if "(" in m else ""
            if sid:
                affected_sids.add(sid)
        for gap in final_gaps:
            gap_method = gap.get("method", "")
            gap_path = gap.get("path", "")
            gap_strategy = gap.get("missing_strategy", "")
            for p in plan:
                if p["endpoint_method"] == gap_method and p["endpoint_path"] == gap_path:
                    if gap_strategy == "dependency_chain" or p["design_method"] == gap_strategy or gap_strategy == "happy_path":
                        affected_sids.add(p["scenario_id"])
        affected_items = [p for p in plan if p["scenario_id"] in affected_sids]
        if affected_items:
            try:
                fix_cases, fw = generate_cases_from_plan_batch(
                    plan_items=affected_items, endpoints=endpoints,
                    context_block=context_block, existing_cases=all_cases,
                    dep_graph=dep_graph, **base_kwargs,
                )
                if fix_cases:
                    fix_cases = attach_cleanup_steps(attach_setup_steps(fix_cases, endpoints), endpoints)
                    fix_sids = {getattr(c, "scenario_id", "") for c in fix_cases if getattr(c, "scenario_id", "")}
                    all_cases = [c for c in all_cases if getattr(c, "scenario_id", "") not in fix_sids]
                    all_cases = all_cases + fix_cases
                    fix_attempted = True
                    diag_parts.append(f"验收未通过后定向修复 {len(fix_cases)} 条")
                    # Re-evaluate after successful fix
                    final_gaps = _detect_coverage_gaps(all_cases, endpoints)
                    final_missing = _verify_plan_scenario_coverage(plan, all_cases)
                    dm_mismatches = _detect_design_method_mismatch(plan, all_cases)
            except Exception:
                pass

    # Report diagnostics AFTER repair attempt (no stale warnings)
    if dm_mismatches:
        diag_parts.append(f"{len(dm_mismatches)} 条用例 design_method 与计划不一致")
    if final_gaps:
        gap_details = ", ".join(f"{g['method']} {g['path']}/{g['missing_strategy']}" for g in final_gaps[:5])
        diag_parts.append(f"仍有 {len(final_gaps)} 项缺口：{gap_details}")

    if final_gaps or final_missing or dm_mismatches or plan_dm_issues:
        diag_parts.append("覆盖验收未通过")
    else:
        diag_parts.append("覆盖验收通过")

    # Clean up: remove scenario_ids that are now covered (e.g. by final repair)
    final_sids = {getattr(c, "scenario_id", "") for c in all_cases if getattr(c, "scenario_id", "")}
    unresolved_scenario_ids = {sid for sid in unresolved_scenario_ids if sid not in final_sids}

    warning = (plan_warning + "；" if plan_warning else "") + "; ".join(diag_parts)
    if unresolved_scenario_ids:
        warning += f"。{len(unresolved_scenario_ids)} 个场景未产出用例，可尝试重新生成或调整策略配置后重试"

    # ── Local validation: runnable misclassification + variable flow ──
    misclassifications = detect_runnable_misclassification(all_cases, dep_graph)
    if misclassifications:
        warning += f"。发现 {len(misclassifications)} 条用例可执行性误判"
    setup_issues = validate_setup_variable_flow(all_cases)
    if setup_issues:
        warning += f"。{len(setup_issues)} 条用例 setup_steps 变量未被引用"

    return all_cases, warning


def detect_runnable_misclassification(
    cases: list[TestCase],
    dep_graph: dict[str, Any] | None,
) -> list[dict[str, str]]:
    """Detect cases marked runnable=false that could execute via setup_steps."""
    issues: list[dict[str, str]] = []
    if not dep_graph or not dep_graph.get("edges"):
        return issues
    safe_edges = [e for e in dep_graph["edges"] if e.get("safe_to_prepare")]
    if not safe_edges:
        return issues
    for case in cases:
        if case.runnable:
            continue
        if case.design_method not in ("happy_path", "status_code_coverage", "equivalence_class"):
            continue
        for edge in safe_edges:
            to = edge.get("to", {})
            if to.get("method") == case.method and to.get("path") == case.path:
                producer = edge.get("from", {})
                issues.append({
                    "case_id": case.id, "scenario_id": getattr(case, "scenario_id", ""),
                    "severity": "error", "type": "runnable_misclassified",
                    "detail": f"runnable=false 但 {producer.get('method')} {producer.get('path')} 可准备数据",
                    "producer": f"{producer.get('method')} {producer.get('path')}",
                })
                break
    return issues


def validate_dependency_graph(dep_graph: dict[str, Any] | None, endpoints: list[Endpoint]) -> list[dict[str, str]]:
    """Validate dependency graph edges reference real endpoints."""
    issues: list[dict[str, str]] = []
    if not dep_graph or not dep_graph.get("edges"):
        return issues
    ep_keys = {(ep.method, ep.path) for ep in endpoints}
    for i, edge in enumerate(dep_graph["edges"]):
        frm = edge.get("from", {})
        to = edge.get("to", {})
        f_key = (str(frm.get("method","")).upper(), str(frm.get("path","")))
        t_key = (str(to.get("method","")).upper(), str(to.get("path","")))
        if f_key not in ep_keys:
            issues.append({"severity": "error", "type": "invalid_from",
                           "detail": f"edge[{i}] from {f_key} not in endpoints"})
        if t_key not in ep_keys:
            issues.append({"severity": "error", "type": "invalid_to",
                           "detail": f"edge[{i}] to {t_key} not in endpoints"})
        if edge.get("confidence") not in ("high", "medium", "low"):
            issues.append({"severity": "warn", "type": "invalid_confidence",
                           "detail": f"edge[{i}] confidence={edge.get('confidence')}"})
    return issues


def validate_setup_variable_flow(cases: list[TestCase]) -> list[dict[str, str]]:
    """Detect cases with setup_steps where extract variables aren't referenced in path_params/query/body."""
    issues: list[dict[str, str]] = []
    for case in cases:
        if not case.setup_steps or not case.runnable:
            continue
        for step in case.setup_steps:
            if not step.extract:
                continue
            for var_name in step.extract:
                ref = "{{" + var_name + "}}"
                params_str = str(case.path_params) + str(case.query) + str(case.body or "")
                if ref not in params_str:
                    issues.append({
                        "case_id": case.id, "scenario_id": getattr(case, "scenario_id", ""),
                        "severity": "warn", "type": "extract_not_referenced",
                        "detail": f"setup_steps 提取了 {var_name} 但主请求未引用 {ref}",
                    })
    return issues


def _detect_plan_design_method_mismatch(plan: list[dict[str, Any]]) -> list[str]:
    """Detect plan items whose title_hint suggests a different design_method.

    Uses lightweight keyword rules on title_hint (structured, short field).
    """
    RULES = [
        (["路径参数", "非法格式", "不存在资源", "空值路径"], "path_parameter_error"),
        (["鉴权", "认证", "未授权", "Token"], "auth_error"),
        (["边界", "超长", "最小值", "最大值", "最小长度", "最大长度", "空字符串"], "boundary_value"),
        (["类型错误", "格式非法", "非法类型", "JSON非法"], "type_error"),
    ]
    issues = []
    for item in plan:
        hint = str(item.get("title_hint", "")).lower()
        dm = str(item.get("design_method", ""))
        for keywords, expected_dm in RULES:
            if any(kw.lower() in hint for kw in keywords) and dm != expected_dm and dm == "happy_path":
                issues.append(f"{item.get('scenario_id','')}: title_hint='{item.get('title_hint','')}' 应为 {expected_dm} 非 {dm}")
                break
    return issues


def _verify_plan_scenario_coverage(plan: list[dict[str, Any]], cases: list[TestCase]) -> set[str]:
    """Return scenario_ids from plan that have no matching case."""
    planned_sids = {p["scenario_id"] for p in plan}
    final_sids = {getattr(c, "scenario_id", "") for c in cases if getattr(c, "scenario_id", "")}
    return planned_sids - final_sids


def _detect_design_method_mismatch(plan: list[dict[str, Any]], cases: list[TestCase]) -> list[str]:
    """Detect cases whose design_method doesn't match the plan or its own tags.

    Two checks:
    A. case.design_method vs plan[scenario_id].design_method (syntactic)
    B. case.design_method vs case.tags — if tags contain a known design_method
       that differs from case.design_method, flag it (semantic via structured data).
    """
    plan_by_sid = {p["scenario_id"]: p.get("design_method", "") for p in plan}
    known_methods = {"happy_path", "path_parameter_error", "boundary_value", "type_error",
                     "auth_error", "optional_field", "equivalence_class", "status_code_coverage",
                     "dependency_chain", "required_field"}
    issues = []
    for c in cases:
        sid = getattr(c, "scenario_id", "")
        expected = plan_by_sid.get(sid)
        # A: plan mismatch
        if expected and c.design_method != expected:
            issues.append(f"{c.id} ({sid}): design_method={c.design_method} 计划为 {expected}")
        # B: tags mismatch
        tag_methods = {t for t in (c.tags or []) if t in known_methods}
        if tag_methods and c.design_method not in tag_methods:
            issues.append(f"{c.id} ({sid}): design_method={c.design_method} 但 tags 含 {tag_methods}")
    return issues


def _fill_doc_based_gaps(plan: list[dict[str, Any]], endpoints: list[Endpoint]) -> list[dict[str, Any]]:
    """Add plan items for document-declared constraints not yet covered.

    Unlike a mechanical strategy×endpoint matrix, this only triggers when the API
    doc explicitly declares: error response codes, parameter constraints, or auth
    requirements. LLM-merged coverage (via coverage_tags) is respected.
    """
    result = list(plan)
    covered_methods: dict[tuple[str, str], set[str]] = {}
    for p in result:
        key = (p["endpoint_method"], p["endpoint_path"])
        covered_methods.setdefault(key, set()).update(p.get("coverage_tags", []))

    for ep in endpoints:
        key = (ep.method, ep.path)
        covered = covered_methods.get(key, set())

        # Documented error responses → need negative coverage
        error_codes = [k for k in (ep.responses or {}) if isinstance(k, str) and k.isdigit() and int(k) >= 400]
        if error_codes and not (covered & {"status_code_coverage", "type_error", "path_parameter_error", "auth_error", "boundary_value"}):
            result.append(_make_plan_item(ep, "status_code_coverage", "P1", f"文档声明错误响应 {error_codes[0]}", ["status_code_coverage"]))

        # Parameter constraints → need boundary or equivalence
        for param in ep.parameters or []:
            schema = param.get("schema", {}) if isinstance(param.get("schema"), dict) else {}
            desc = str(param.get("description", "")) + " " + str(schema.get("description", ""))
            has_constraint = (
                any(k in schema for k in ("maxLength", "minLength", "maximum", "minimum", "enum"))
                or any(kw in desc for kw in ("最长", "最短", "最大", "最小", "不超过", "至少", "字符", "长度"))
            )
            if has_constraint and not (covered & {"boundary_value", "equivalence_class", "type_error"}):
                result.append(_make_plan_item(ep, "boundary_value", "P1", f"参数 {param.get('name','')} 有文档约束", ["boundary_value"]))
                break

        # Auth declared → need auth_error (only if doc/params mention it)
        auth_hints = ("auth", "token", "api_key", "apikey", "bearer", "鉴权", "认证")
        ep_text = f"{ep.summary or ''} {ep.description or ''} {' '.join(p.get('name','')+p.get('description','') for p in (ep.parameters or []))}".lower()
        if any(h in ep_text for h in auth_hints) and "auth_error" not in covered:
            result.append(_make_plan_item(ep, "auth_error", "P1", "文档声明鉴权要求", ["auth_error"]))

    return result


def _make_plan_item(ep: Endpoint, dm: str, priority: str, title_hint: str, tags: list[str]) -> dict[str, Any]:
    sid_base = f"{ep.method}-{ep.path}".replace("/", "-").replace("{", "").replace("}", "").strip("-")[:50]
    return {
        "scenario_id": f"SCN-DOC-{dm.upper()}-{sid_base}",
        "endpoint_method": ep.method, "endpoint_path": ep.path,
        "design_method": dm, "priority": priority, "title_hint": title_hint,
        "coverage_tags": tags, "expected_basis": None, "data_dependency": None,
        "chain_steps": None, "setup_required": False, "dependency_edge_ids": [],
        "expected_setup": [],
    }


def _fill_hard_gaps(plan: list[dict[str, Any]], endpoints: list[Endpoint], *, dep_graph: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """System hard gap fill: append missing critical scenarios without calling LLM.

    Prefers master-generated dep_graph when available (from API discovery metadata),
    falls back to local _build_dependency_graph heuristic.
    """
    result = list(plan)
    covered = {(p["endpoint_method"], p["endpoint_path"], p["design_method"]) for p in result}
    has_chain = any(p["endpoint_method"] == "CHAIN" and p.get("chain_steps") for p in result)

    # Per-endpoint happy_path
    for ep in endpoints:
        if (ep.method, ep.path, "happy_path") not in covered:
            result.append({"scenario_id": f"SCN-HARD-HP-{ep.method}-{ep.path.replace('/','-').strip('-')}",
                           "endpoint_method": ep.method, "endpoint_path": ep.path,
                           "design_method": "happy_path", "priority": "P0",
                           "title_hint": f"{ep.summary or ep.path} 主流程",
                           "coverage_tags": ["happy_path"], "expected_basis": None, "data_dependency": None, "chain_steps": None})

    # CHAIN gap — prefer master dep_graph, fallback to local heuristic
    if not has_chain:
        if not (dep_graph and dep_graph.get("edges")):
            local = _build_dependency_graph(endpoints)
            if local:
                # Normalize local format to edges format
                edges = []
                for prod_key, consumer_keys in local.items():
                    pm, pp = prod_key.split(" ", 1)
                    for ck in consumer_keys:
                        cm, cp = ck.split(" ", 1)
                        edges.append({"from": {"method": pm, "path": pp}, "to": {"method": cm, "path": cp}})
                dep_graph = {"edges": edges}
        if dep_graph and dep_graph.get("edges"):
            chain_steps = []
            edges = dep_graph["edges"]
            prod = edges[0]["from"]
            chain_steps.append({"method": prod.get("method","GET"), "path": prod.get("path","/")})
            # Sort consumer steps: GET/HEAD first, PUT/PATCH middle, DELETE last
            consumers = [e.get("to", {}) for e in edges[:5]]
            METHOD_ORDER = {"GET": 0, "HEAD": 0, "POST": 1, "PUT": 2, "PATCH": 2, "DELETE": 3}
            consumers.sort(key=lambda x: METHOD_ORDER.get(str(x.get("method","GET")).upper(), 1))
            for to in consumers[:3]:
                chain_steps.append({"method": to.get("method","GET"), "path": to.get("path","/")})
            if chain_steps:
                result.append({"scenario_id": "SCN-HARD-CHAIN-001", "endpoint_method": "CHAIN", "endpoint_path": "",
                               "design_method": "dependency_chain", "priority": "P0",
                               "title_hint": "链路用例", "coverage_tags": ["dependency_chain"],
                               "expected_basis": None, "data_dependency": None, "chain_steps": chain_steps})

    # Path parameter error
    for ep in endpoints:
        if "{" in ep.path and "}" in ep.path:
            if (ep.method, ep.path, "path_parameter_error") not in covered:
                result.append({"scenario_id": f"SCN-HARD-PPE-{ep.method}-{ep.path.replace('/','-').strip('-')}",
                               "endpoint_method": ep.method, "endpoint_path": ep.path,
                               "design_method": "path_parameter_error", "priority": "P1",
                               "title_hint": f"{ep.path} 路径参数异常", "coverage_tags": ["path_parameter_error"],
                               "expected_basis": None, "data_dependency": None, "chain_steps": None})

    # Error response coverage
    for ep in endpoints:
        error_codes = [k for k in (ep.responses or {}) if isinstance(k, str) and k.isdigit() and int(k) >= 400]
        if error_codes and (ep.method, ep.path, "type_error") not in covered:
            result.append({"scenario_id": f"SCN-HARD-ERR-{ep.method}-{ep.path.replace('/','-').strip('-')}",
                           "endpoint_method": ep.method, "endpoint_path": ep.path,
                           "design_method": "type_error", "priority": "P1",
                           "title_hint": f"{ep.path} 错误响应", "coverage_tags": ["type_error"],
                           "expected_basis": None, "data_dependency": None, "chain_steps": None})
    return result


def supplement_cases_with_config(
    root: Path,
    endpoints: list[Endpoint],
    existing_cases: list[TestCase],
    *,
    project: str = "default",
    scenario_text: str,
    max_cases: int = 5,
    stream_callback: Callable[[str], None] | None = None,
) -> tuple[list[TestCase], str]:
    scenario_text = scenario_text.strip()
    if not scenario_text:
        raise ValueError("补充场景说明不能为空")
    llm = configured_llm(root, "case_generator")
    if not is_llm_configured(llm):
        return [], "用例生成模型未配置，无法补充生成用例"
    strategy_context = case_generation_strategy_context(root)
    config = load_config(root)
    context_block = ""
    try:
        context_block = build_context_bundle(
            root,
            project,
            task_type="case_generation",
            model_config=llm,
            context_config=config.get("context", {}),
            system_prompt=case_generation_system_prompt(),
            user_prompt_base=case_supplement_user_prompt_base(endpoints, existing_cases, strategy_context, scenario_text, max_cases),
            endpoints=endpoints,
            environment=str(config.get("default_environment", "")),
        ).text
    except Exception:
        context_block = ""
    try:
        generated = generate_supplemental_cases_with_llm(  # supplemental raises on failure, no repair needed here
            endpoints=endpoints,
            existing_cases=existing_cases,
            scenario_text=scenario_text,
            max_cases=max_cases,
            base_url=llm.get("base_url", ""),
            model=llm.get("model", ""),
            api_key_env=llm.get("api_key_env", ""),
            strategy_context=strategy_context,
            provider=llm.get("provider", "openai-compatible"),
            timeout=DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS,
            stream_callback=stream_callback,
            context_block=context_block,
        )
    except Exception as exc:
        return [], f"大模型补充用例未完成：{exc}"
    if not generated:
        return [], "大模型未生成有效补充用例"
    generated = dedupe_supplemental_cases(existing_cases, generated)
    generated = attach_cleanup_steps(attach_setup_steps(generated, endpoints), endpoints)
    if not generated:
        return [], "大模型生成的补充用例与现有用例重复，未追加"
    try:
        record_case_generation_lessons(root, project, generated, actor="ai:case_generator")
    except Exception:
        pass
    # ── Master governance: review supplemental cases ──
    if _governance_enabled(root):
        _govern_cases(root, project, generated, endpoints)
    return generated[:max_cases], ""


def _detect_coverage_gaps(cases: list[TestCase], endpoints: list[Endpoint]) -> list[dict[str, str]]:
    """Detect critical coverage gaps. Returns structured gap dicts with method/path/missing_strategy."""
    gaps: list[dict[str, str]] = []

    # Per-endpoint happy_path check
    covered_endpoints = {(c.method, c.path) for c in cases if c.method != "CHAIN"}
    for ep in endpoints:
        if (ep.method, ep.path) not in covered_endpoints:
            gaps.append({"method": ep.method, "path": ep.path, "missing_strategy": "happy_path"})

    # Dependency chain check
    all_endpoint_keys = {(ep.method, ep.path) for ep in endpoints}

    def _step_key(s) -> tuple[str, str] | None:
        if isinstance(s, dict):
            m, p = str(s.get("method", "")).upper(), str(s.get("path", ""))
        else:
            m, p = str(getattr(s, "method", "")).upper(), str(getattr(s, "path", ""))
        return (m, p) if m and p else None

    def _is_valid_chain(case) -> bool:
        if case.method != "CHAIN":
            return False
        steps = case.steps or []
        if len(steps) < 2:
            return False
        return all(
            (key := _step_key(s)) is not None and key in all_endpoint_keys
            for s in steps
        )

    has_chain = any(_is_valid_chain(c) for c in cases)
    if not has_chain:
        dep_graph = _build_dependency_graph(endpoints)
        if dep_graph:
            gaps.append({"method": "CHAIN", "path": "", "missing_strategy": "dependency_chain"})

    # Path parameter coverage check
    for ep in endpoints:
        if "{" in ep.path and "}" in ep.path:
            path_cases = [c for c in cases if c.method == ep.method and c.path == ep.path
                          and c.design_method in ("path_parameter_error",)]
            if not path_cases:
                gaps.append({"method": ep.method, "path": ep.path, "missing_strategy": "path_parameter_error"})

    # Error response coverage check
    for ep in endpoints:
        responses = ep.responses or {}
        error_codes = [int(k) for k in responses if isinstance(k, str) and k.isdigit() and int(k) >= 400]
        if error_codes:
            error_cases = [c for c in cases if c.method == ep.method and c.path == ep.path
                           and c.design_method in ("type_error", "path_parameter_error", "auth_error", "status_code_coverage")]
            if not error_cases:
                gaps.append({"method": ep.method, "path": ep.path, "missing_strategy": "type_error"})

    return gaps


def _valid_chain_case(case: TestCase, endpoints: list[Endpoint]) -> bool:
    if case.method != "CHAIN":
        return False
    endpoint_keys = {(ep.method, ep.path) for ep in endpoints}
    steps = case.steps or []
    if len(steps) < 2:
        return False
    return all((step.method.upper(), step.path) in endpoint_keys for step in steps)


def _rule_fill_coverage_gaps(cases: list[TestCase], endpoints: list[Endpoint]) -> list[TestCase]:
    """Use local rules only for critical gaps that AI failed to fill."""
    rule_cases = generate_cases(endpoints)
    selected: list[TestCase] = []

    covered_endpoints = {(case.method, case.path) for case in cases if case.method != "CHAIN"}
    for endpoint in endpoints:
        if (endpoint.method, endpoint.path) in covered_endpoints:
            continue
        match = next(
            (
                case
                for case in rule_cases
                if case.method == endpoint.method
                and case.path == endpoint.path
                and case.design_method == "happy_path"
            ),
            None,
        )
        if match:
            selected.append(match)

    if not any(_valid_chain_case(case, endpoints) for case in cases):
        match = next((case for case in rule_cases if _valid_chain_case(case, endpoints)), None)
        if match:
            selected.append(match)

    for endpoint in endpoints:
        if "{" not in endpoint.path or "}" not in endpoint.path:
            continue
        has_path_error = any(
            case.method == endpoint.method
            and case.path == endpoint.path
            and case.design_method == "path_parameter_error"
            for case in cases
        )
        if has_path_error:
            continue
        selected.extend(
            case
            for case in rule_cases
            if case.method == endpoint.method
            and case.path == endpoint.path
            and case.design_method == "path_parameter_error"
        )

    for endpoint in endpoints:
        error_codes = [
            int(key)
            for key in endpoint.responses or {}
            if isinstance(key, str) and key.isdigit() and int(key) >= 400
        ]
        if not error_codes:
            continue
        has_error_case = any(
            case.method == endpoint.method
            and case.path == endpoint.path
            and case.design_method in {"type_error", "path_parameter_error", "auth_error", "status_code_coverage"}
            for case in cases
        )
        if has_error_case:
            continue
        match = next(
            (
                case
                for case in rule_cases
                if case.method == endpoint.method
                and case.path == endpoint.path
                and case.design_method in {"type_error", "path_parameter_error", "auth_error", "status_code_coverage"}
            ),
            None,
        )
        if match:
            selected.append(match)

    return dedupe_supplemental_cases(cases, selected)


def _build_dependency_graph(endpoints: list[Endpoint]) -> dict[str, set[str]]:
    """Detect data dependency chains by finding shared path parameters.

    When multiple endpoints share the same path param (e.g. '{email}' appears in
    /api/check/{email}, /api/messages/{email}, /api/delete/{email}), there is
    likely a data dependency chain.
    """
    import re as _re

    # Pass 1: collect param groups and potential producers
    param_groups: dict[str, list[str]] = {}
    paramless: list[tuple[str, str]] = []  # [(key, hint), ...]

    for ep in endpoints:
        key = f"{ep.method} {ep.path}"
        path_params = _re.findall(r'\{(\w+)\}', ep.path)
        if not path_params and ep.parameters:
            path_params = [p.get("name", "") for p in ep.parameters if p.get("in") == "path"]

        if path_params:
            for pn in path_params:
                param_groups.setdefault(pn.lower(), []).append(key)
        else:
            hint = ((ep.summary or "") + " " + ep.path.rsplit("/", 1)[-1]).lower()
            paramless.append((key, hint))

    # Pass 2: match producers to param groups
    graph: dict[str, set[str]] = {}
    for pn_lower, consumer_keys in param_groups.items():
        if len(consumer_keys) < 2:
            continue
        # Find best producer: paramless endpoint whose hint contains the param name
        prod_key = ""
        for key, hint in paramless:
            if pn_lower in hint:
                prod_key = key
                break
        if not prod_key and paramless:
            prod_key = paramless[0][0]  # fallback: first paramless endpoint
        if prod_key:
            graph.setdefault(prod_key, set()).update(consumer_keys)

    return graph


def _find_dependency_chains(graph: dict[str, set[str]]) -> list[list[str]]:
    """Find linear chains from producers to terminal consumers."""
    chains = []
    visited = set()

    def walk(node, path):
        if node in path:
            chains.append(path + [node])
            return
        new_path = path + [node]
        if node not in graph or not graph[node]:
            if len(new_path) >= 2:
                chains.append(new_path)
            return
        for next_node in graph[node]:
            walk(next_node, new_path)

    for start in graph:
        if start not in visited:
            walk(start, [])
            visited.add(start)
    return sorted(chains, key=len, reverse=True)


def _fill_coverage_gaps(
    root: Path, project: str,
    endpoints: list[Endpoint],
    existing_cases: list[TestCase],
    gaps: list[str],
    llm: dict,
    strategy_context: dict,
) -> tuple[list[TestCase], str]:
    """Targeted supplementary generation. Returns (cases, diagnostics)."""
    import json
    from ai_apitest.ai.case_generation import generate_supplemental_cases_with_llm

    endpoint_keys = {(ep.method, ep.path) for ep in endpoints}
    gap_text = "\n".join(f"- {g}" for g in gaps)
    existing_summary = [
        {"id": c.id, "title": c.title, "method": c.method, "path": c.path,
         "design_method": c.design_method, "tags": c.tags}
        for c in existing_cases
    ]

    scenario = f"""系统检测到以下覆盖缺口，请只补充缺口对应的用例，不要重新生成已有覆盖的用例。

## 检测到的缺口
{gap_text}

## 已生成用例摘要（共 {len(existing_cases)} 条）
{json.dumps(existing_summary, ensure_ascii=False, indent=2)[:3000]}

## 补充要求
- 只补充缺口列表中列出的缺失场景。
- 不要重复生成已覆盖的接口和策略。
- 特别是：如果缺口提到缺少链路用例，必须生成 method=CHAIN 的用例，steps 中每个 step 的 (method, path) 必须来自当前接口列表中的真实接口。
- 如果缺口提到缺少 path_parameter_error，必须为对应接口生成路径参数异常用例。
- 每条补充用例的 tags 中只包含实际覆盖的策略名。"""

    try:
        raw_generated = generate_supplemental_cases_with_llm(
            endpoints=endpoints,
            existing_cases=existing_cases,
            scenario_text=scenario,
            max_cases=len(gaps) * 3,
            base_url=llm.get("base_url", ""),
            model=llm.get("model", ""),
            api_key_env=llm.get("api_key_env", ""),
            strategy_context=strategy_context,
            provider=llm.get("provider", "openai-compatible"),
            timeout=DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        raise ValueError(f"补生成模型调用失败：{exc}") from exc

    # ── Structured diagnostics ──
    chain_candidates = [c for c in raw_generated if c.method == "CHAIN"]
    diag_parts = [f"补生成校验后保留 {len(raw_generated)} 条"]
    if chain_candidates:
        chain_diag = []
        for c in chain_candidates:
            steps = c.steps or []
            if not steps:
                chain_diag.append(f"{c.id} 无 steps")
            elif len(steps) < 2:
                chain_diag.append(f"{c.id} 仅 {len(steps)} 个 step（需 ≥2）")
            else:
                invalid = 0
                for s in steps:
                    if isinstance(s, dict):
                        m, p = str(s.get("method", "")).upper(), str(s.get("path", ""))
                    else:
                        m, p = str(getattr(s, "method", "")).upper(), str(getattr(s, "path", ""))
                    if not m or not p or (m, p) not in endpoint_keys:
                        invalid += 1
                if invalid:
                    chain_diag.append(f"{c.id} 有 {invalid}/{len(steps)} 个 step 不在接口列表")
                else:
                    chain_diag.append(f"{c.id} {len(steps)} steps 全部有效")
        diag_parts.append(f"其中 CHAIN {len(chain_candidates)} 条：{'；'.join(chain_diag)}")
    diag_parts.append(f"validate_cases 后保留 {len(raw_generated)} 条待去重")

    return raw_generated, "；".join(diag_parts)


def case_generation_system_prompt() -> str:
    from ai_apitest.ai.case_generation import system_prompt

    return system_prompt()


def case_generation_user_prompt_base(endpoints: list[Endpoint], strategy_context: dict) -> str:
    from ai_apitest.ai.case_generation import user_prompt

    return user_prompt(endpoints, strategy_context, context_block="")


def case_supplement_user_prompt_base(
    endpoints: list[Endpoint],
    existing_cases: list[TestCase],
    strategy_context: dict,
    scenario_text: str,
    max_cases: int,
) -> str:
    from ai_apitest.ai.case_generation import supplement_user_prompt

    return supplement_user_prompt(
        endpoints=endpoints,
        existing_cases=existing_cases,
        scenario_text=scenario_text,
        max_cases=max_cases,
        strategy_context=strategy_context,
        context_block="",
    )


def dedupe_supplemental_cases(existing_cases: list[TestCase], generated_cases: list[TestCase]) -> list[TestCase]:
    seen = {case_similarity_key(case) for case in existing_cases}
    result: list[TestCase] = []
    for case in generated_cases:
        key = case_similarity_key(case)
        if key in seen:
            continue
        seen.add(key)
        result.append(case)
    return result


def case_similarity_key(case: TestCase) -> tuple:
    import json
    from ai_apitest.core.serde import to_jsonable

    return (
        case.method.upper(),
        case.path,
        case.design_method,
        normalize_text(case.title),
        json.dumps(
            {
                "path_params": case.path_params,
                "query": case.query,
                "body": case.body,
                "assertions": to_jsonable(case.assertions),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ),
    )


def normalize_text(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def build_ai_report_analysis(root: Path, project: str, results: list[CaseResult]) -> str:
    llm = configured_llm(root, "analyzer")
    if not is_llm_configured(llm):
        return ""
    context_block = ""
    config = load_config(root)
    try:
        context_block = build_context_bundle(
            root,
            project,
            task_type="report_analysis",
            model_config=llm,
            context_config=config.get("context", {}),
            system_prompt="",
            user_prompt_base=report_analysis_user_prompt_base(root, project, results),
            endpoints=endpoints_from_results(results),
            environment=str(config.get("default_environment", "")),
        ).text
    except Exception:
        context_block = ""

    # Extract trend context for AI analysis (same data powers the web trend view)
    trend_ctx = _build_trend_context(root, project)
    try:
        analysis = analyze_results_with_llm(results, project, llm, context_block=context_block, trend_context=trend_ctx)
        visible, _summary = process_report_analysis(
            root,
            project,
            analysis,
            source=f"report_analysis:{project}",
            environment=str(config.get("default_environment", "")),
            llm=llm,
        )
        # ── Master governance: review report analysis ──
        review_context_conflicts(root, project)
        if _governance_enabled(root):
            _govern_report(root, project, results, analysis, visible)
        if _governance_enabled(root):
            _govern_experience_candidates(root, project)
        return visible
    except Exception as exc:
        return f"大模型分析失败：{exc}"


def review_context_conflicts(root: Path, project: str) -> None:
    """Master model reviews and resolves context conflicts (governance: context_governance)."""
    if not _governance_enabled(root):
        return
    master = configured_llm(root, "master")
    if not is_llm_configured(master):
        return
    try:
        conflicts = conflict_pairs(root, project, limit=20)
        if not conflicts:
            return
        reviews = review_conflicts_with_llm(conflicts, master)
        apply_conflict_review(root, project, reviews, actor="ai:master")
        _audit_governance(root, project, "conflict_reviewed", {
            "conflict_count": len(conflicts), "decisions": [r.get("decision", "") for r in reviews],
        }, task="context_governance")
    except Exception:
        return


def endpoints_from_results(results: list[CaseResult]) -> list[Endpoint]:
    seen: set[tuple[str, str]] = set()
    endpoints: list[Endpoint] = []
    for result in results:
        key = (result.method, result.path)
        if key in seen or result.method == "CHAIN":
            continue
        seen.add(key)
        endpoints.append(Endpoint(method=result.method, path=result.path))
    return endpoints


def _build_trend_context(root, project: str) -> dict:
    """Build structured trend context for AI analysis. Shared by web rendering and LLM prompts."""
    try:
        from ai_apitest.context.storage import trend_overview
        trend_data = trend_overview(root, project, limit=3)
        runs = trend_data.get("runs", [])
        if len(runs) < 2:
            return {}
        latest = runs[0]
        previous = runs[1]
        latest_eps = {f"{e.get('method','')} {e.get('path','')}": e for e in latest.get("endpoint_metrics", [])}
        prev_eps = {f"{e.get('method','')} {e.get('path','')}": e for e in previous.get("endpoint_metrics", [])}
        endpoint_deltas = []
        for key, ep in latest_eps.items():
            prev_ep = prev_eps.get(key, {})
            ep_failed = int(ep.get("failed", 0))
            prev_failed = int(prev_ep.get("failed", 0)) if prev_ep else 0
            if ep_failed != prev_failed:
                change = "new_failure" if prev_failed == 0 and ep_failed > 0 else ("recovered" if prev_failed > 0 and ep_failed == 0 else "persistent")
                endpoint_deltas.append({"endpoint": key, "current_failed": ep_failed, "previous_failed": prev_failed, "change": change})
        from ai_apitest.context.storage import assess_trend_comparability
        comp = assess_trend_comparability(latest, previous)
        return {
            "previous_run": {"total_cases": previous.get("total_cases"), "passed_cases": previous.get("passed_cases"), "failed_cases": previous.get("failed_cases"), "pass_rate": previous.get("pass_rate")},
            "run_scope": latest.get("run_scope", "") or previous.get("run_scope", ""),
            "case_overlap": comp.get("jaccard"),
            "total_cases_latest": latest.get("total_cases"),
            "total_cases_previous": previous.get("total_cases"),
            "comparable": comp["comparable"],
            "confidence": comp["confidence"],
            "endpoint_deltas": endpoint_deltas[:10],
            "note": comp["note"],
        }
    except Exception:
        return {}


def report_analysis_user_prompt_base(root, project: str, results: list[CaseResult]) -> str:
    import json
    from ai_apitest.ai.prompting import render_prompt
    from ai_apitest.ai.result_analysis import case_summary
    from ai_apitest.core.serde import to_jsonable

    total = len(results)
    passed = sum(1 for item in results if item.status == "passed")
    failed = sum(1 for item in results if item.status == "failed")

    # Build trend context for richer AI analysis
    trend_context = _build_trend_context(root, project)

    payload = {
        "project": project,
        "summary": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "success_rate": f"{(passed / total * 100):.1f}%" if total else "-",
        },
        "failed_cases": [case_summary(item) for item in results if item.status == "failed"],
        "slowest_cases": [case_summary(item) for item in sorted(results, key=lambda item: item.elapsed_ms, reverse=True)[:5]],
        "results": [to_jsonable(item) for item in results],
        "trend_context": trend_context,
    }
    return render_prompt(
        "result_analysis.md",
        {
            "CONTEXT_BLOCK": "",
            "RESULTS_JSON": json.dumps(payload, ensure_ascii=False, indent=2),
        },
    )


def build_ai_failure_review(root: Path, project: str, results: list[CaseResult]) -> str:
    if not any(item.status == "failed" for item in results):
        return ""
    llm = configured_llm(root, "analyzer")
    if not is_llm_configured(llm):
        return ""
    try:
        return review_failed_results_with_llm(results, project, llm)
    except Exception as exc:
        return f"AI 复核失败：{exc}"


# ═══════════════════════════════════════════════════════════════
# Master governance layer
# ═══════════════════════════════════════════════════════════════

def _governance_enabled(root: Path) -> bool:
    config = load_config(root)
    return bool(config.get("governance", {}).get("enabled", True))


def _govern_report(root: Path, project: str, results: list[CaseResult], analysis_text: str, visible_report: str) -> None:
    """Master reviews analyzer's report for consistency, specificity, and misattribution (governance: report_review)."""
    master = configured_llm(root, "master")
    if not is_llm_configured(master):
        return
    import json as _json
    from ai_apitest.ai.provider import chat_completion

    total = len(results)
    passed = sum(1 for r in results if r.status == "passed")
    failed = sum(1 for r in results if r.status == "failed")
    rate_text = f"{(passed/total*100):.1f}%" if total else "N/A"
    failed_brief = [{"case_id": r.case_id, "title": r.title, "error": (r.error or "")[:200]} for r in results if r.status == "failed"][:10]

    prompt = f"""你是测试策略审查者，不是用例生成者。请审查下面这份由 analyzer 模型生成的测试报告，输出严格的 JSON。

## 审查标准
1. 报告结论是否和实际执行结果一致
2. 建议是否具体、可操作
3. 是否误把断言问题说成接口缺陷
4. 是否结合了趋势上下文
5. 提取的经验候选是否可信

## 执行摘要
- 总计 {total} 条，通过 {passed}，失败 {failed}
- 通过率 {rate_text}

## 失败用例（最多10条）
{_json.dumps(failed_brief, ensure_ascii=False, indent=2)}

## 报告正文（analyzer 输出）
{analysis_text[:3000]}

## 严格 JSON 输出格式
```json
{{
  "verdict": "approved|needs_revision|rejected",
  "confidence": "high|medium|low",
  "issues": [
    {{"severity": "error|warn", "location": "summary|suggestion|attribution", "detail": "..."}}
  ],
  "suggestions": ["具体改进建议..."]
}}
```

只输出 JSON，不要其他文字。"""
    try:
        response = chat_completion(
            base_url=master.get("base_url", ""), model=master.get("model", ""),
            api_key_env=master.get("api_key_env", ""), provider=master.get("provider", "openai-compatible"),
            messages=[{"role": "user", "content": prompt}],
        ).strip()
        parsed = _governance_parse_json(response, response_type="report_review", base_url=master.get("base_url",""), model=master.get("model",""), api_key_env=master.get("api_key_env",""), provider=master.get("provider","openai-compatible"))
        if parsed:
            _audit_governance(root, project, "report_reviewed", {
                "verdict": parsed.get("verdict"), "confidence": parsed.get("confidence"),
                "issue_count": len(parsed.get("issues", [])),
            }, task="report_review")
    except Exception:
        pass


def _govern_experience_candidates(root: Path, project: str) -> None:
    """Master reviews pending experience candidates for semantic quality (governance: context_governance)."""
    master = configured_llm(root, "master")
    if not is_llm_configured(master):
        return
    import json as _json
    from ai_apitest.ai.provider import chat_completion
    from ai_apitest.context.storage import list_lessons

    # Query recent pending candidates
    candidates_raw = list_lessons(root, project, lifecycle_status="pending", limit=10)
    if not candidates_raw:
        return
    candidates = [{k: v for k, v in c.items() if k in ("id", "title", "content", "lesson_type", "confidence", "scope")}
                  for c in candidates_raw]

    # Get potentially similar existing lessons (semantic_key match)
    config = load_config(root)
    existing = list_lessons(root, project, limit=10, lesson_type="", scope="")
    existing_brief = [{"id": e.get("id"), "title": e.get("title"), "content": (e.get("content") or "")[:200],
                       "lesson_type": e.get("lesson_type"), "lifecycle": e.get("lifecycle_status")}
                      for e in existing if str(e.get("id")) not in {c.get("id") for c in candidates}][:10]

    prompt = f"""你是上下文治理审查者。请审查以下经验候选，判断每条候选是否应当入库。

## 审查标准
1. 是否和已有经验重复（表达不同但含义相同 → merge）
2. 是否和已有经验冲突
3. 内容是否足够具体、可复用
4. 置信度评估

## 候选经验
{_json.dumps(candidates, ensure_ascii=False, indent=2)}

## 已有相关经验（语义相近）
{_json.dumps(existing_brief, ensure_ascii=False, indent=2) if existing_brief else "无"}

## 严格 JSON 输出格式
```json
{{
  "decisions": [
    {{
      "candidate_id": "les-xxx",
      "decision": "create|merge|reject|needs_review|conflict",
      "target_lesson_id": null,
      "confidence": "high|medium|low",
      "reason": "..."
    }}
  ]
}}
```

只输出 JSON，不要其他文字。"""
    try:
        response = chat_completion(
            base_url=master.get("base_url", ""), model=master.get("model", ""),
            api_key_env=master.get("api_key_env", ""), provider=master.get("provider", "openai-compatible"),
            messages=[{"role": "user", "content": prompt}],
        ).strip()
        parsed = _governance_parse_json(response, response_type="experience_governance", base_url=master.get("base_url",""), model=master.get("model",""), api_key_env=master.get("api_key_env",""), provider=master.get("provider","openai-compatible"))
        if parsed:
            for decision in parsed.get("decisions", []):
                d_confidence = str(decision.get("confidence", "low"))
                if d_confidence == "low":
                    decision["decision"] = "needs_review"
                d_type = decision.get("decision")
                cid = decision.get("candidate_id", "")
                tid = decision.get("target_lesson_id", "")
                if d_type == "merge" and tid:
                    _apply_merge(root, project, cid, tid)
                elif d_type == "reject":
                    _apply_reject(root, project, cid)
                elif d_type == "create":
                    _apply_confirm(root, project, cid)
                elif d_type == "needs_review":
                    _apply_needs_review(root, project, cid)
                elif d_type == "conflict" and tid:
                    _apply_conflict(root, project, cid, tid)
            _audit_governance(root, project, "experience_governed", {
                "candidates_in": len(candidates), "decisions": parsed.get("decisions", []),
            }, task="context_governance")
    except Exception:
        pass


def _review_coverage_plan(root: Path, project: str, plan: list[dict[str, Any]], endpoints: list[Endpoint]) -> None:
    """Master reviews the coverage plan (lightweight, high-ROI)."""
    if not _governance_enabled(root):
        return
    master = configured_llm(root, "master")
    if not is_llm_configured(master):
        return
    import json as _json
    from ai_apitest.ai.provider import chat_completion

    endpoint_briefs = [{"method": e.method, "path": e.path, "summary": e.summary or "",
                         "responses": {str(k): str(v) for k, v in (e.responses or {}).items()},
                         "has_request_body": bool(e.request_body)}
                        for e in endpoints]
    prompt = f"""你是测试策略审查者。请审查以下覆盖计划，只输出结构化 JSON，不要解释。

## 审查标准
1. 是否漏掉核心业务场景（如 clear 数据依赖链未生成 CHAIN）
2. 是否有明显重复场景
3. status_code_coverage 是否可和 happy_path 合并
4. path_parameter_error 是否机械生成（空路径有路由行为时不生成空值）
5. CHAIN chain_steps 是否合理
6. 是否有不符合接口文档的预期

## 接口定义
{_json.dumps(endpoint_briefs, ensure_ascii=False, indent=2)}

## 覆盖计划（{len(plan)} 条）
{_json.dumps(plan[:30], ensure_ascii=False, indent=2)}

## 严格 JSON 输出格式
```json
{{
  "verdict": "approved|needs_revision",
  "issues": [{{"scenario_id": "...", "severity": "error|warn", "detail": "..."}}],
  "suggestions": ["..."]
}}
```
只输出 JSON。"""
    try:
        response = chat_completion(
            base_url=master.get("base_url",""), model=master.get("model",""),
            api_key_env=master.get("api_key_env",""), provider=master.get("provider","openai-compatible"),
            messages=[{"role": "user", "content": prompt}],
        ).strip()
        parsed = _governance_parse_json(response, response_type="case_review",
                                         base_url=master.get("base_url",""), model=master.get("model",""),
                                         api_key_env=master.get("api_key_env",""), provider=master.get("provider","openai-compatible"))
        if parsed:
            _audit_governance(root, project, "plan_reviewed", {"verdict": parsed.get("verdict"),
                "issue_count": len(parsed.get("issues", [])), "plan_size": len(plan)}, task="plan_review")
    except Exception:
        pass


def _govern_cases(root: Path, project: str, cases: list[TestCase], endpoints: list[Endpoint]) -> list[TestCase] | None:
    """Master tiered review of final TestCases. Returns revised cases if changes made, else None."""
    if not _governance_enabled(root):
        return None
    master = configured_llm(root, "master")
    if not is_llm_configured(master):
        return None
    import json as _json
    from ai_apitest.ai.provider import chat_completion

    # Build lookup dict for case replacement; process reviews on mutable dict
    # Use scenario_id as primary key to avoid cross-batch ID collisions
    case_by_key: dict[str, TestCase] = {}
    for c in cases:
        sid = getattr(c, "scenario_id", "")
        key = sid if sid else c.id
        case_by_key[key] = c
    from ai_apitest.core.serde import read_json as _rj
    dep_graph = _rj(project_docs_dir(root, project) / "metadata.json", {}).get("dependency_graph")
    changed = False

    # Tiered selection: mandatory all-in, P2/P3 sampled
    mandatory = [c for c in cases if c.priority in ("P0", "P1") or c.method == "CHAIN"
                 or c.risk in ("high", "destructive") or getattr(c, "scenario_id", "").startswith("SCN-HARD-")]
    sampled = [c for c in cases if c not in mandatory and c.priority in ("P2", "P3")]
    import random as _random
    sampled = _random.sample(sampled, min(max(5, len(mandatory) // 4), len(sampled))) if len(sampled) > 0 else []

    if not mandatory and not sampled:
        return

    endpoint_briefs = [{"method": e.method, "path": e.path, "summary": e.summary or ""} for e in endpoints]

    def _brief(c):
        return {"id": c.id, "scenario_id": getattr(c, "scenario_id", ""), "title": c.title,
                "method": c.method, "path": c.path, "priority": c.priority,
                "design_method": c.design_method, "runnable": c.runnable, "risk": c.risk,
                "assertions": [{"type": a.type, "target": a.target, "operator": a.operator} for a in c.assertions],
                "source": "system_gap_filled" if getattr(c, "scenario_id", "").startswith("SCN-HARD-") else "llm"}

    def _review_batch(batch_cases, label):
        if not batch_cases:
            return
        briefs = [_brief(c) for c in batch_cases]
        # Pre-review: run local validation to flag issues for master attention
        local_issues = detect_runnable_misclassification(batch_cases, dep_graph) + validate_setup_variable_flow(batch_cases)

        prompt = f"""你是测试策略审查者。请审查以下测试用例，输出结构化 JSON。

## 审查标准
1. 断言是否合理（不过度猜测，不遗漏关键字段）
2. CHAIN steps 是否完整、变量传递正确
3. 是否存在明显重复
4. 依赖链路是否合理
5. system_gap_filled 用例（系统硬性补漏追加）质量是否可接受

## 本地校验发现的问题（供参考）
{_json.dumps(local_issues[:10], ensure_ascii=False, indent=2) if local_issues else "无"}

## 接口列表
{_json.dumps(endpoint_briefs, ensure_ascii=False, indent=2)}

## 待审查用例（{label}，{len(briefs)} 条）
{_json.dumps(briefs, ensure_ascii=False, indent=2)}

## 严格 JSON 输出格式
```json
{{
  "verdict": "approved|needs_revision|rejected",
  "confidence": "high|medium|low",
  "issues": [{{"scenario_id": "...", "severity": "error|warn", "detail": "..."}}],
  "coverage_gaps": [{{"endpoint": "POST /x", "missing_strategy": "path_parameter_error"}}],
  "suggestions": ["..."]
}}
```
只输出 JSON。"""
        try:
            response = chat_completion(
                base_url=master.get("base_url", ""), model=master.get("model", ""),
                api_key_env=master.get("api_key_env", ""), provider=master.get("provider", "openai-compatible"),
                messages=[{"role": "user", "content": prompt}],
            ).strip()
            parsed = _governance_parse_json(response, response_type="case_review",
                                             base_url=master.get("base_url",""), model=master.get("model",""),
                                             api_key_env=master.get("api_key_env",""), provider=master.get("provider","openai-compatible"))
            if parsed:
                _audit_governance(root, project, "cases_reviewed", {
                    "verdict": parsed.get("verdict"), "confidence": parsed.get("confidence"),
                    "issue_count": len(parsed.get("issues", [])), "batch": label,
                }, task="case_review")
                if parsed.get("verdict") == "needs_revision" and parsed.get("issues"):
                    case_gen = configured_llm(root, "case_generator")
                    if is_llm_configured(case_gen):
                        revised = _revise_cases(root, project, batch_cases, parsed.get("issues", []), endpoints, case_gen)
                        if revised:
                            nonlocal changed
                            changed = True
                            for r in revised:
                                # Match by scenario_id first (stable across revision),
                                # fall back to id if scenario_id missing
                                sid = getattr(r, "scenario_id", "")
                                if sid:
                                    existing = next((c for c in case_by_key.values()
                                                     if getattr(c, "scenario_id", "") == sid), None)
                                    if existing:
                                        case_by_key[sid] = r
                                        continue
                                r_key = getattr(r, "scenario_id", "") or r.id
                                case_by_key[r_key] = r
        except Exception:
            pass

    # Batch mandatory cases (10 per batch) — all reviewed, no cap
    BATCH_SIZE = 10
    for i in range(0, len(mandatory), BATCH_SIZE):
        _review_batch(mandatory[i:i + BATCH_SIZE], f"必审-第{i//BATCH_SIZE + 1}批")
    # One batch for sampled P2/P3
    _review_batch(sampled, "P2/P3抽样")
    return list(case_by_key.values()) if changed else None


def _apply_merge(root: Path, project: str, candidate_id: str, target_id: str) -> None:
    """Merge candidate into target: validate, merge occurrence/content, write relations, archive candidate."""
    try:
        from ai_apitest.context.storage import lessons_by_ids, merge_lesson_into, now_iso
        safe = safe_project_name(project)
        both = lessons_by_ids(root, safe, [candidate_id, target_id])
        if candidate_id not in both or target_id not in both:
            return
        target = both[target_id]
        if target.get("lifecycle_status") not in ("active", "pending"):
            return
        with context_connection(root, safe) as conn:
            merge_lesson_into(conn, source_id=candidate_id, target_id=target_id, timestamp=now_iso(), actor="ai:master")
            audit(conn, "governance_merged", "lesson", candidate_id, safe, "ai:master", {"merged_into": target_id})
    except Exception:
        pass


def _apply_reject(root: Path, project: str, candidate_id: str) -> None:
    """Reject candidate: validate existence, mark as ignored with audit trail."""
    try:
        from ai_apitest.context.storage import lessons_by_ids
        safe = safe_project_name(project)
        candidates = lessons_by_ids(root, safe, [candidate_id])
        if candidate_id not in (candidates or {}):
            return
        with context_connection(root, safe) as conn:
            conn.execute(
                "update lessons set lifecycle_status='ignored', review_status='rejected' where id=? and project=?",
                (candidate_id, safe),
            )
            audit(conn, "governance_rejected", "lesson", candidate_id, safe, "ai:master", {"reason": "governance review"})
    except Exception:
        pass


def _apply_confirm(root: Path, project: str, candidate_id: str) -> None:
    """Confirm candidate: activate pending lesson (governance approved)."""
    try:
        from ai_apitest.context.storage import lessons_by_ids
        safe = safe_project_name(project)
        candidates = lessons_by_ids(root, safe, [candidate_id])
        if candidate_id not in (candidates or {}):
            return
        with context_connection(root, safe) as conn:
            conn.execute(
                "update lessons set lifecycle_status='active', review_status='confirmed' where id=? and project=? and lifecycle_status='pending'",
                (candidate_id, safe),
            )
            audit(conn, "governance_confirmed", "lesson", candidate_id, safe, "ai:master", {"reason": "governance approved"})
    except Exception:
        pass


def _apply_needs_review(root: Path, project: str, candidate_id: str) -> None:
    """Mark candidate for manual review (low confidence or flagged)."""
    try:
        from ai_apitest.context.storage import lessons_by_ids
        safe = safe_project_name(project)
        candidates = lessons_by_ids(root, safe, [candidate_id])
        if candidate_id not in (candidates or {}):
            return
        with context_connection(root, safe) as conn:
            conn.execute(
                "update lessons set review_status='needs_review' where id=? and project=?",
                (candidate_id, safe),
            )
            audit(conn, "governance_needs_review", "lesson", candidate_id, safe, "ai:master", {"reason": "low confidence or flagged by governance"})
    except Exception:
        pass


def _apply_conflict(root: Path, project: str, candidate_id: str, target_id: str) -> None:
    """Mark candidate as conflicting with target — both kept, manual resolution needed."""
    try:
        from ai_apitest.context.storage import lessons_by_ids, now_iso
        safe = safe_project_name(project)
        both = lessons_by_ids(root, safe, [candidate_id, target_id])
        if candidate_id not in both or target_id not in both:
            return
        with context_connection(root, safe) as conn:
            conn.execute(
                "update lessons set review_status='needs_review' where id in (?, ?) and project=?",
                (candidate_id, target_id, safe),
            )
            # Insert a conflict relation (matches actual schema: id, lesson_id, related_lesson_id, relation_type, notes, created_at)
            conn.execute(
                """insert into lesson_relations(lesson_id, related_lesson_id, relation_type, notes, created_at)
                   values (?, ?, 'conflict', ?, ?)""",
                (candidate_id, target_id, "governance detected conflict", now_iso()),
            )
            audit(conn, "governance_conflict_flagged", "lesson_relation", candidate_id, safe, "ai:master", {"conflict_with": target_id})
    except Exception:
        pass


def _revise_cases(root: Path, project: str, cases: list[TestCase], issues: list[dict[str, Any]], endpoints: list[Endpoint], llm: dict) -> list[TestCase]:
    """Ask case_generator to revise specific problematic cases. Returns revised cases."""
    import json as _json
    from ai_apitest.ai.provider import chat_completion
    from ai_apitest.ai.case_generation import validate_cases
    from ai_apitest.ai.provider import extract_json_array

    case_ids = list({i.get("case_id", i.get("scenario_id", "")) for i in issues if i.get("case_id") or i.get("scenario_id")})
    if not case_ids:
        return []
    target_cases = [c for c in cases if c.id in case_ids or getattr(c, "scenario_id", "") in case_ids][:5]
    if not target_cases:
        return []

    briefs = [{"id": c.id, "scenario_id": getattr(c, "scenario_id", ""), "title": c.title,
               "method": c.method, "path": c.path, "runnable": c.runnable, "risk": c.risk,
               "design_method": c.design_method, "tags": c.tags, "priority": c.priority,
               "assertions": [{"type": a.type, "target": a.target, "operator": a.operator, "expected": a.expected} for a in c.assertions],
               "query": c.query, "body": c.body, "expected_result": c.expected_result,
               "setup_steps": [{"method": s.method, "path": s.path, "extract": s.extract} for s in (c.setup_steps or [])],
               "path_params": c.path_params}
              for c in target_cases]
    issues_brief = [{"case_id": i.get("case_id"), "scenario_id": i.get("scenario_id"),
                     "severity": i.get("severity"), "detail": i.get("detail")}
                    for i in issues[:5]]

    prompt = f"""以下测试用例经 master 审查发现问题，请根据 issue 修复这些问题并重新输出完整 JSON 数组。

## 问题列表
{_json.dumps(issues_brief, ensure_ascii=False, indent=2)}

## 需修复的用例
{_json.dumps(briefs, ensure_ascii=False, indent=2)}

## 修复要求
- 只修复 issue 中列出的问题，不要改动其他用例内容。
- id、scenario_id、design_method、method、path 必须和原始用例完全一致，不可修改。
- 只输出修复后的 JSON 数组，不要解释。"""

    try:
        response = chat_completion(
            base_url=llm.get("base_url", ""), model=llm.get("model", ""),
            api_key_env=llm.get("api_key_env", ""), provider=llm.get("provider", "openai-compatible"),
            messages=[{"role": "user", "content": prompt}],
        ).strip()
        revised_raw = extract_json_array(response)
        revised = validate_cases(revised_raw, endpoints)
        # Force-overwrite immutable fields from originals (unless issue targets them)
        IMMUTABLE = ["id", "scenario_id", "design_method", "method", "path"]
        issue_targets = {i.get("type", "") for i in issues}
        orig_by_sid = {getattr(c, "scenario_id", ""): c for c in target_cases if getattr(c, "scenario_id", "")}
        valid_revised = []
        for r in revised:
            sid = getattr(r, "scenario_id", "")
            orig = orig_by_sid.get(sid)
            if not orig:
                continue
            for field in IMMUTABLE:
                if field not in issue_targets:
                    setattr(r, field, getattr(orig, field))
            valid_revised.append(r)
        revised = valid_revised
        # Stable identity: re-map revised ids back to originals
        orig_sid_to_id = {sid: c.id for sid, c in orig_by_sid.items()}
        for r in revised:
            sid = getattr(r, "scenario_id", "")
            if sid in orig_sid_to_id:
                r.id = orig_sid_to_id[sid]
        if revised:
            _audit_governance(root, project, "cases_revised", {
                "revised_count": len(revised), "issue_count": len(issues_brief),
            }, task="case_review")
        return revised
    except Exception:
        return []


def _audit_governance(root: Path, project: str, action: str, detail: dict, *, task: str = "") -> None:
    try:
        safe = safe_project_name(project)
        master = configured_llm(root, "master")
        detail["model"] = master.get("model", "unknown") if master else "unknown"
        detail["task"] = task
        detail["timestamp"] = now_iso()
        with context_connection(root, safe) as conn:
            audit(conn, f"governance_{action}", "governance", "", safe, "ai:master", detail)
    except Exception:
        pass


def _governance_parse_json(
    content: str,
    *,
    response_type: str,
    base_url: str = "",
    model: str = "",
    api_key_env: str = "",
    provider: str = "openai-compatible",
) -> dict | None:
    """Parse governance JSON with self-repair. Returns validated dict or None."""
    from ai_apitest.ai.provider import retry_with_json_repair

    def _parse(text: str) -> dict:
        parsed = _safe_json(text)
        if parsed is None:
            raise ValueError("JSON 对象解析失败")
        validated = _validate_governance_response(parsed, response_type)
        if validated is None:
            raise ValueError("治理响应 schema 校验失败")
        return validated

    if base_url and model and api_key_env:
        try:
            result, _warning = retry_with_json_repair(
                content, parse_json=_parse,
                base_url=base_url, model=model, api_key_env=api_key_env, provider=provider,
            )
            return result
        except ValueError:
            return None
    # No model configured — bare parse without repair
    try:
        return _parse(content)
    except ValueError:
        return None


def _safe_json(text: str) -> dict | None:
    import json as _json, re
    text = text.strip()
    m = re.search(r'\{[\s\S]*\}', text)
    if not m:
        return None
    try:
        return _json.loads(m.group())
    except _json.JSONDecodeError:
        return None


def _validate_governance_response(parsed: dict, response_type: str) -> dict | None:
    """Validate master governance JSON output against expected schema."""
    VALID_CONFIDENCE = {"high", "medium", "low"}
    if response_type == "report_review":
        if parsed.get("verdict") not in ("approved", "needs_revision", "rejected"):
            return None
        if parsed.get("confidence") not in VALID_CONFIDENCE:
            return None
        for issue in parsed.get("issues", []):
            if not isinstance(issue, dict) or "severity" not in issue or "detail" not in issue:
                return None
    elif response_type == "case_review":
        if parsed.get("verdict") not in ("approved", "needs_revision", "rejected"):
            return None
        if parsed.get("confidence") not in VALID_CONFIDENCE:
            return None
    elif response_type == "experience_governance":
        VALID_DECISIONS = {"create", "merge", "reject", "needs_review", "conflict"}
        for d in parsed.get("decisions", []):
            if not isinstance(d, dict):
                return None
            if d.get("decision") not in VALID_DECISIONS:
                return None
            if d.get("confidence") not in VALID_CONFIDENCE:
                return None
            if d.get("decision") in ("merge", "conflict") and not d.get("target_lesson_id"):
                return None
    return parsed
