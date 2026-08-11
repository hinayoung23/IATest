from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from ai_apitest.ai.provider import llm_check
from ai_apitest.ai.workflows import (
    apply_ai_discovery_if_configured,
    build_ai_report_bundle,
    generate_cases_with_config,
    configured_llm,
    run_pending_context_governance,
)
from ai_apitest.core.config import current_environment, init_project, load_config, save_config
from ai_apitest.core.artifact_language import (
    configured_artifact_language,
    set_current_artifact_language,
    validate_artifact_language,
)
from ai_apitest.core.files import atomic_write_text
from ai_apitest.core.locking import project_operation_lock
from ai_apitest.core.env import load_dotenv
from ai_apitest.core.paths import (
    project_docs_file,
    project_metadata_file,
    project_root,
    project_source_snapshot_file,
)
from ai_apitest.core.serde import endpoint_from_dict, read_json, to_jsonable, write_json
from ai_apitest.context.storage import (
    context_stats,
    export_lessons,
    get_context_pack,
    import_lessons,
    list_context_packs,
    migrate_project,
    prune_context_packs,
    prune_context_history,
    rebuild_fts_index,
    rebuild_project_memory,
    trend_overview,
    validate_context,
    mark_lessons_for_endpoint_changes,
)
from ai_apitest.context.dependencies import synchronize_project_dependencies, verify_dependency_candidates
from ai_apitest.data.agent_runs import AgentRunRepository
from ai_apitest.context.extraction import retry_due_report_extractions
from ai_apitest.application.discovery import normalize_document_for_agent
from ai_apitest.discovery.document import load_source_text, parse_api_document
from ai_apitest.execution.runner import run_cases
from ai_apitest.generator.cases import load_cases_from_markdown, save_cases
from ai_apitest.mock_server import build_mock_server
from ai_apitest.orchestrator.maintainer.auto_updater import auto_update_cases_with_llm, impacted_cases
from ai_apitest.orchestrator.maintainer.coverage import coverage_summary
from ai_apitest.orchestrator.maintainer.change_detector import diff_endpoints
from ai_apitest.orchestrator.scheduler.runner import run_schedule_forever
from ai_apitest.report.markdown import write_report
from ai_apitest.web.server import serve

app = typer.Typer(help="AI 接口自动化框架", no_args_is_help=True)
context_app = typer.Typer(help="项目上下文管理", no_args_is_help=True)
app.add_typer(context_app, name="context")
console = Console()


def cwd_root() -> Path:
    root = project_root(Path.cwd())
    load_dotenv(root)
    return root


@app.command()
def init(project: str = "default") -> None:
    path = init_project(cwd_root(), project)
    console.print(f"[green]已初始化:[/green] {path}")


@app.command("language")
def artifact_language(
    language: str = typer.Argument("", help="产物语言：en、zh、ja、fr 或 ru；留空查看当前值"),
) -> None:
    """查看或切换后续用例、经验和报告使用的语言。"""
    root = cwd_root()
    config = load_config(root)
    if not language:
        console.print(configured_artifact_language(config))
        return
    normalized = validate_artifact_language(language)
    config.setdefault("artifacts", {})["language"] = normalized
    save_config(root, config)
    console.print(f"[green]Artifact language:[/green] {normalized}")


@app.command()
def discover(source: str, project: str = "default") -> None:
    root = cwd_root()
    init_project(root, project)
    with project_operation_lock(root, project, operation="cli:discover"):
        _discover(root, source, project)


def _discover(root: Path, source: str, project: str) -> None:
    set_current_artifact_language(configured_artifact_language(load_config(root)))
    text = load_source_text(source)
    endpoints, metadata = parse_api_document(text, source=source)
    endpoints, metadata = apply_ai_discovery_if_configured(root, text, endpoints, metadata, project=project)
    metadata["source"] = source
    old_endpoints = [endpoint_from_dict(item) for item in read_json(project_docs_file(root, project), [])]
    diff = diff_endpoints(old_endpoints, endpoints) if old_endpoints else {"added": [], "removed": [], "changed": []}
    if diff["removed"] or diff["changed"]:
        mark_lessons_for_endpoint_changes(root, project, [*diff["removed"], *diff["changed"]])
    write_json(project_docs_file(root, project), to_jsonable(endpoints))
    atomic_write_text(
        project_source_snapshot_file(root, project),
        normalize_document_for_agent(text),
    )
    if metadata:
        write_json(project_metadata_file(root, project), metadata)
    synchronize_project_dependencies(root, project, old_endpoints, endpoints, actor="cli:discover")
    console.print(f"[green]已导入接口 {len(endpoints)} 个:[/green] {project_docs_file(root, project)}")
    if metadata.get("base_url"):
        console.print(f"识别到 base_url: {metadata['base_url']}")


@app.command()
def generate(
    project: str = "default",
    force: bool = typer.Option(False, help="忽略生成缓存并重新调用模型"),
) -> None:
    root = cwd_root()
    init_project(root, project)
    with project_operation_lock(root, project, operation="cli:generate"):
        endpoint_data = read_json(project_docs_file(root, project), [])
        endpoints = [endpoint_from_dict(item) for item in endpoint_data]
        with console.status("正在生成用例..."):
            cases, warning = generate_cases_with_config(root, endpoints, project=project, force_refresh=force)
        if not cases:
            raise ValueError("用例生成结果为空，已保留现有用例文件")
        path = save_cases(root, cases, project=project)
        console.print(f"[green]已生成用例 {len(cases)} 个:[/green] {path}")
    if warning:
        console.print(f"[yellow]{warning}[/yellow]")


@app.command("ai-generate")
def ai_generate(
    project: str = "default",
    base_url: str = typer.Option(..., help="大模型服务 base_url，例如 https://api.example.com/v1"),
    model: str = typer.Option(..., help="模型名称"),
    api_key_env: str = typer.Option("IATEST_LLM_API_KEY", help="API Key 环境变量名"),
    provider: str = typer.Option("openai-compatible", help="openai-compatible 或 anthropic"),
    temperature: float | None = None,
) -> None:
    root = cwd_root()
    init_project(root, project)
    endpoint_data = read_json(project_docs_file(root, project), [])
    endpoints = [endpoint_from_dict(item) for item in endpoint_data]
    with console.status("正在调用大模型生成用例..."):
        cases, warning = generate_cases_with_config(
            root,
            endpoints,
            project=project,
            llm_override={
                "enabled": True,
                "base_url": base_url,
                "model": model,
                "api_key_env": api_key_env,
                "provider": provider,
            },
        )
    path = save_cases(root, cases, project=project)
    console.print(f"[green]AI 已生成并保存用例 {len(cases)} 个:[/green] {path}")
    if warning:
        console.print(f"[yellow]{warning}[/yellow]")


@app.command("llm-check")
def llm_check_command(
    base_url: str = typer.Option(..., help="大模型服务 base_url"),
    model: str = typer.Option(..., help="模型名称"),
    api_key_env: str = typer.Option("IATEST_LLM_API_KEY", help="API Key 环境变量名"),
    provider: str = typer.Option("openai-compatible", help="openai-compatible 或 anthropic"),
    temperature: float | None = None,
) -> None:
    cwd_root()
    with console.status("正在检查大模型连通性..."):
        result = llm_check(
            base_url=base_url,
            model=model,
            api_key_env=api_key_env,
            provider=provider,
            temperature=temperature,
        )
    console.print(f"[green]LLM 连通性检查成功:[/green] {result}")


@app.command()
def run(project: str = "default", cases: str = "", base_url: str = "") -> None:
    root = cwd_root()
    init_project(root, project)
    case_ids = {item.strip() for item in cases.split(",") if item.strip()} or None
    with project_operation_lock(root, project, operation="cli:run"):
        with console.status("正在执行用例..."):
            run_scope = "custom" if case_ids else "full"
            results = run_cases(root, case_ids=case_ids, base_url=base_url or None, project=project, run_scope=run_scope)
            if not results:
                console.print("[yellow]没有符合执行范围的用例，未生成空报告。[/yellow]")
                return
            ai_review, analysis = build_ai_report_bundle(root, project, results)
            report = write_report(
                root,
                results,
                project=project,
                ai_review=ai_review,
                analysis=analysis,
            )
    passed = sum(1 for item in results if item.status == "passed")
    failed = sum(1 for item in results if item.status == "failed")
    console.print(f"执行完成: total={len(results)} passed={passed} failed={failed}")
    console.print(f"[green]报告:[/green] {report}")


@app.command()
def schedule(project: str = "default", once: bool = False) -> None:
    run_schedule_forever(cwd_root(), project=project, run_once=once, console=console)


@app.command()
def analyze(project: str = "default") -> None:
    root = cwd_root()
    summary = coverage_summary(root, project)
    console.print_json(data=summary)


@app.command("agent-runs")
def agent_runs(project: str = "default", limit: int = 20) -> None:
    """查看模型角色、Token 和工具调用审计记录。"""
    root = cwd_root()
    init_project(root, project)
    console.print_json(data=AgentRunRepository(root, project).list_recent(limit=limit))


@app.command()
def maintain(
    project: str = "default",
    source: str = typer.Option("", help="新的接口文档路径或 URL"),
    ai_update: bool = typer.Option(False, help="调用分析模型更新受影响用例"),
    write: bool = typer.Option(False, help="将 AI 更新结果写回用例文件"),
) -> None:
    root = cwd_root()
    if not source:
        console.print("[yellow]请通过 --source 提供新的接口文档路径或 URL[/yellow]")
        raise typer.Exit(1)
    with project_operation_lock(root, project, operation="cli:maintain"):
        _maintain(root, project, source, ai_update=ai_update, write=write)


def _maintain(root: Path, project: str, source: str, *, ai_update: bool, write: bool) -> None:
    set_current_artifact_language(configured_artifact_language(load_config(root)))
    text = load_source_text(source)
    new_endpoints, new_metadata = parse_api_document(text, source=source)
    old_endpoints = [endpoint_from_dict(item) for item in read_json(project_docs_file(root, project), [])]
    current_metadata = read_json(project_metadata_file(root, project), {})
    if not isinstance(current_metadata, dict):
        current_metadata = {}
    maintained_metadata = {**current_metadata, **new_metadata, "source": source}
    diff = diff_endpoints(old_endpoints, new_endpoints)
    console.print_json(data=diff)
    changed_keys = [*diff["removed"], *diff["changed"]]
    if not ai_update:
        if write:
            write_json(project_docs_file(root, project), to_jsonable(new_endpoints))
            write_json(project_metadata_file(root, project), maintained_metadata)
            synchronize_project_dependencies(root, project, old_endpoints, new_endpoints, actor="cli:maintain")
            console.print(f"[green]已写回更新后的接口定义:[/green] {project_docs_file(root, project)}")
        return
    cases = load_cases_from_markdown(root, project)
    impacted = impacted_cases(cases, changed_keys)
    mode = str(load_config(root).get("agents", {}).get("maintenance_mode") or "agent").strip().lower()
    retired_ids: set[str] = set()
    if mode == "agent":
        from ai_apitest.application.case_maintenance import maintain_cases_with_agent_runtime

        llm = configured_llm(root, "case_generator")
        maintenance = maintain_cases_with_agent_runtime(
            root,
            project,
            impacted=impacted,
            endpoints=new_endpoints,
            change_context={"endpoint_diff": diff},
            llm=llm,
            allow_retire=True,
        )
        updated = maintenance.updated
        retired_ids = maintenance.retired_case_ids
        console.print(
            f"受影响用例 {len(impacted)} 条，Agent 更新 {len(updated)} 条，退役 {len(retired_ids)} 条。"
        )
    elif mode == "legacy":
        llm = configured_llm(root, "analyzer")
        updated = auto_update_cases_with_llm(impacted=impacted, endpoints=new_endpoints, diff=diff, llm=llm)
        console.print(f"受影响用例 {len(impacted)} 条，Legacy AI 更新生成 {len(updated)} 条。")
    else:
        raise ValueError(f"不支持的维护模式：{mode}；可选值为 agent 或 legacy")
    if write:
        updated_by_id = {case.id: case for case in updated}
        merged = []
        for case in cases:
            if case.id in retired_ids:
                continue
            merged.append(updated_by_id.pop(case.id, case))
        merged.extend(updated_by_id.values())
        path = save_cases(root, merged, project=project)
        write_json(project_docs_file(root, project), to_jsonable(new_endpoints))
        write_json(project_metadata_file(root, project), maintained_metadata)
        synchronize_project_dependencies(root, project, old_endpoints, new_endpoints, actor="cli:maintain")
        console.print(f"[green]已写回更新后的用例:[/green] {path}")


@app.command()
def web(project: str = "default", host: str = "127.0.0.1", port: int = 8765) -> None:
    root = cwd_root()
    init_project(root, project)
    serve(root, host=host, port=port, project=project)


@app.command("mock")
def mock_command(
    project: str = "default",
    host: str | None = typer.Option(None, help="覆盖 mock.host"),
    port: int | None = typer.Option(None, min=0, max=65535, help="覆盖 mock.port"),
) -> None:
    """启动确定性的本地接口 Mock 服务，不调用大模型。"""
    root = cwd_root()
    init_project(root, project)
    server = build_mock_server(load_config(root), host=host, port=port)
    address, bound_port = server.server_address[:2]
    console.print(
        f"[green]Mock 服务已启动:[/green] http://{address}:{bound_port} "
        f"routes={len(server.mock_state.routes)}"  # type: ignore[attr-defined]
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


@context_app.command("migrate")
def context_migrate(project: str = "default") -> None:
    root = cwd_root()
    init_project(root, project)
    db = migrate_project(root, project)
    console.print(f"[green]上下文数据库已就绪:[/green] {db}")


@context_app.command("validate")
def context_validate(project: str = "default") -> None:
    root = cwd_root()
    init_project(root, project)
    console.print_json(data=validate_context(root, project))


@context_app.command("stats")
def context_stats_command(project: str = "default") -> None:
    root = cwd_root()
    init_project(root, project)
    console.print_json(data=context_stats(root, project))


@context_app.command("rebuild-index")
def context_rebuild_index(project: str = "default") -> None:
    root = cwd_root()
    init_project(root, project)
    migrate_project(root, project)
    indexed = rebuild_fts_index(root, project)
    memory = rebuild_project_memory(root, project)
    console.print(f"[green]上下文索引/项目记忆已重建:[/green] indexed={indexed} memory={memory}")


@context_app.command("prune-packs")
def context_prune_packs(
    project: str = "default",
    dry_run: bool = typer.Option(False, help="只统计将被清理的记录和文件，不实际删除"),
) -> None:
    root = cwd_root()
    init_project(root, project)
    config = load_config(root)
    retention = config.get("context", {}).get("retention", {}).get("context_packs", {})
    summary = prune_context_packs(
        root,
        project,
        max_records=int(retention.get("max_records", 500)),
        max_age_days=int(retention.get("max_age_days", 90)),
        remove_orphan_files=bool(retention.get("remove_orphan_files", True)),
        repair_missing_files=bool(retention.get("repair_missing_files", True)),
        dry_run=dry_run,
    )
    console.print_json(data=summary)


@context_app.command("retry-extractions")
def context_retry_extractions(project: str = "default", limit: int = 10) -> None:
    root = cwd_root()
    init_project(root, project)
    console.print_json(data=retry_due_report_extractions(root, project, limit=limit))


@context_app.command("govern")
def context_govern(project: str = "default") -> None:
    """显式处理待审查的上下文冲突和经验候选。"""
    root = cwd_root()
    init_project(root, project)
    with project_operation_lock(root, project, operation="cli:context-govern"):
        run_pending_context_governance(root, project)
    console.print("[green]待处理上下文治理已执行完成。[/green]")


@context_app.command("prune-history")
def context_prune_history(
    project: str = "default",
    dry_run: bool = typer.Option(False, help="只统计将被清理的历史记录，不实际删除"),
) -> None:
    root = cwd_root()
    init_project(root, project)
    console.print_json(data=prune_context_history(root, project, dry_run=dry_run))


@context_app.command("verify-dependencies")
def context_verify_dependencies(project: str = "default") -> None:
    root = cwd_root()
    init_project(root, project)
    config = load_config(root)
    environment = current_environment(config)
    console.print_json(data=verify_dependency_candidates(root, project, environment, config))


@context_app.command("export")
def context_export(
    project: str = "default",
    output: Path | None = typer.Option(None, "--output", "-o", help="导出 JSONL 路径"),
) -> None:
    root = cwd_root()
    init_project(root, project)
    path = export_lessons(root, project, output)
    console.print(f"[green]已导出上下文 lessons:[/green] {path}")


@context_app.command("import")
def context_import(
    source: Path = typer.Argument(..., exists=True, readable=True, help="待导入 JSONL 文件"),
    project: str = "default",
) -> None:
    root = cwd_root()
    init_project(root, project)
    summary = import_lessons(root, project, source, actor="cli")
    console.print_json(data=summary)


@context_app.command("packs")
def context_packs(project: str = "default", limit: int = 20) -> None:
    root = cwd_root()
    init_project(root, project)
    console.print_json(data=list_context_packs(root, project, limit=limit))


@context_app.command("pack")
def context_pack(pack_id: str, project: str = "default") -> None:
    root = cwd_root()
    init_project(root, project)
    pack = get_context_pack(root, project, pack_id)
    if not pack:
        console.print(f"[yellow]未找到 ContextPack:[/yellow] {pack_id}")
        raise typer.Exit(1)
    console.print_json(data=pack)


@context_app.command("trend")
def context_trend(project: str = "default", limit: int = 10) -> None:
    root = cwd_root()
    init_project(root, project)
    console.print_json(data=trend_overview(root, project, limit=limit))


def main() -> None:
    app()
