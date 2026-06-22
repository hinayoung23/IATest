from __future__ import annotations

import html
import json
import base64
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from ai_apitest.ai.llm_config import normalize_llm_base_url, normalize_llm_config, normalize_llm_provider, normalize_llm_role, resolve_llm_role
from ai_apitest.ai.provider import llm_check, looks_like_api_key
from ai_apitest.ai.workflows import apply_ai_discovery_if_configured, build_ai_failure_review, build_ai_report_analysis, generate_cases_with_config, supplement_cases_with_config
from ai_apitest.context.dependencies import infer_dependency_candidates
from ai_apitest.context.extraction import retry_due_report_extractions
from ai_apitest.context.injection import aggregate_prompt_candidates
from ai_apitest.context.storage import (
    conflict_pairs,
    context_stats as load_context_stats,
    edit_lesson,
    latest_context_pack,
    lessons_by_ids,
    list_lessons,
    mark_lessons_for_endpoint_changes,
    recent_audit_logs,
    resolve_conflict,
    resolve_known_issue,
    resolve_similar,
    similar_pairs,
    trend_overview,
    update_lesson_review,
)
from ai_apitest.core.config import init_project, load_config, save_config
from ai_apitest.core.models import Endpoint
from ai_apitest.core.paths import (
    list_projects,
    project_cases_file,
    project_docs_dir,
    project_docs_file,
    project_metadata_file,
    project_reports_dir,
    safe_project_name,
)
from ai_apitest.core.serde import endpoint_from_dict, read_json, to_jsonable, write_json
from ai_apitest.discovery.document import load_source_text, parse_api_document
from ai_apitest.execution.runner import run_cases
from ai_apitest.generator.cases import load_cases_from_markdown, save_cases
from ai_apitest.orchestrator.maintainer.change_detector import diff_endpoints
from ai_apitest.report.markdown import write_report
from ai_apitest.report.storage import latest_case_results, latest_results

WEB_DIR = Path(__file__).parent
TEMPLATE_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"
TASKS: dict[str, dict] = {}
TASK_LOCK = threading.Lock()



def serve(root: Path, host: str = "127.0.0.1", port: int = 8765, project: str = "default") -> None:
    project = safe_project_name(project)
    start_context_retry_worker(root, project)
    handler = make_handler(root, project)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"本地 Web 控制台已启动: http://{host}:{port}")
    server.serve_forever()


def start_context_retry_worker(root: Path, project: str) -> None:
    def worker() -> None:
        while True:
            try:
                retry_due_report_extractions(root, project, limit=5)
            except Exception:
                pass
            time.sleep(60)

    thread = threading.Thread(target=worker, name=f"aitest-{project}-context-retry", daemon=True)
    thread.start()


def make_handler(root: Path, default_project: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self) -> None:
            parsed = urlparse(self.path)
            project = active_project(parsed, default_project)
            if parsed.path.startswith("/static/"):
                asset = static_asset(parsed.path)
                if asset:
                    data, content_type = asset
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    return
            if parsed.path in {"/", "/interfaces", "/cases", "/reports", "/config", "/context"}:
                content = route_get(root, project, parsed).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                return
            self.send_error(404)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            project = active_project(parsed, default_project)
            if parsed.path.startswith("/static/"):
                asset = static_asset(parsed.path)
                if asset:
                    data, content_type = asset
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_error(404)
                return
            if parsed.path == "/task-status":
                self.respond_json({"tasks": project_tasks(project)})
                return
            if parsed.path.startswith("/report/"):
                name = parsed.path.rsplit("/", 1)[-1]
                if is_safe_report_name(name):
                    report = project_reports_dir(root, project) / name
                    if report.exists() and report.parent == project_reports_dir(root, project):
                        self.respond_html(render_report_detail(root, project, report))
                        return
                self.send_error(404)
                return
            try:
                self.respond_html(route_get(root, project, parsed))
            except KeyError:
                self.send_error(404)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            payload = self.rfile.read(length).decode("utf-8")
            form = parse_qs(payload)
            project = safe_project_name(field(form, "project") or default_project)
            try:
                handle_action(root, project, parsed.path, form, self)
            except Exception as exc:
                self.respond_html(page(root, project, "操作失败", f"<section><h2>操作失败</h2><pre>{esc(exc)}</pre></section>"), status=500)

        def redirect(self, location: str) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.end_headers()

        def respond_html(self, content: str, status: int = 200) -> None:
            data = content.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def respond_json(self, payload: dict, status: int = 200) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def static_asset(path: str) -> tuple[bytes, str] | None:
    name = path.rsplit("/", 1)[-1]
    allowed = {"app.css": "text/css; charset=utf-8", "app.js": "application/javascript; charset=utf-8"}
    content_type = allowed.get(name)
    if not content_type:
        return None
    file_path = STATIC_DIR / name
    if not file_path.exists():
        return None
    return file_path.read_bytes(), content_type


def render_template(name: str, context: dict[str, str]) -> str:
    content = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
    for key, value in context.items():
        content = content.replace("{{" + key + "}}", value)
    return content


def route_get(root: Path, project: str, parsed) -> str:
    routes = {
        "/": render_dashboard,
        "/interfaces": render_interfaces,
        "/cases": render_cases,
        "/reports": render_reports,
        "/config": render_config,
        "/context": render_context,
    }
    return routes[parsed.path](root, project, parsed)


def task_key(project: str, action: str) -> str:
    return f"{project}:{action}"


def project_tasks(project: str) -> list[dict]:
    now = time.time()
    with TASK_LOCK:
        return [
            {key: value for key, value in task.items() if key != "project"}
            for task in TASKS.values()
            if task.get("project") == project
            and (
                task.get("status") == "running"
                or now - float(task.get("updated_at", now)) < 120
            )
        ]


def active_task(project: str, action: str) -> dict | None:
    with TASK_LOCK:
        task = TASKS.get(task_key(project, action))
        if task and task.get("status") == "running":
            return dict(task)
    return None


def update_task(project: str, action: str, **updates) -> None:
    with TASK_LOCK:
        task = TASKS.get(task_key(project, action))
        if not task:
            return
        stream_delta = updates.pop("stream_delta", "")
        if stream_delta:
            task["stream_excerpt"] = (str(task.get("stream_excerpt", "")) + stream_delta)[-1200:]
        task.update(updates)
        task["updated_at"] = time.time()


def start_task(project: str, action: str, label: str, target) -> bool:
    key = task_key(project, action)
    now = time.time()
    with TASK_LOCK:
        task = TASKS.get(key)
        if task and task.get("status") == "running":
            return False
        TASKS[key] = {
            "id": uuid.uuid4().hex,
            "project": project,
            "action": action,
            "label": label,
            "status": "running",
            "stage": "queued",
            "detail": "任务已启动",
            "message": "",
            "stream_excerpt": "",
            "started_at": now,
            "heartbeat_at": now,
            "updated_at": now,
        }

    def heartbeat() -> None:
        while True:
            time.sleep(5)
            with TASK_LOCK:
                task = TASKS.get(key)
                if not task or task.get("status") != "running":
                    return
                task["heartbeat_at"] = time.time()
                task["updated_at"] = task["heartbeat_at"]

    def runner() -> None:
        def progress(detail: str | None = None, **updates) -> None:
            if detail is not None:
                updates["detail"] = detail
            update_task(project, action, **updates)

        try:
            message = target(progress)
            update_task(project, action, status="completed", stage="complete", detail="任务已完成", message=message or "")
        except Exception as exc:
            update_task(project, action, status="failed", detail="任务执行失败", message=str(exc))

    threading.Thread(target=heartbeat, name=f"aitest-{project}-{action}-heartbeat", daemon=True).start()
    thread = threading.Thread(target=runner, name=f"aitest-{project}-{action}", daemon=True)
    thread.start()
    return True


def handle_action(root: Path, project: str, path: str, form: dict, handler) -> None:
    if path == "/actions/discover":
        if active_task(project, "discover"):
            handler.redirect(url_for("/interfaces", project, {"message": "接口识别任务还在进行中，请稍后查看结果"}))
            return
        metadata = read_json(project_metadata_file(root, project), {})
        source = field(form, "source").strip() or str(metadata.get("source", "")).strip()
        def run_discover(progress):
            old_endpoints = load_endpoints(root, project)
            endpoints, metadata = discover_source(
                root,
                project,
                source,
                progress=progress,
                stream_callback=lambda text: progress(stream_delta=text),
            )
            progress("保存接口识别结果", stage="save")
            handle_endpoint_context_changes(root, project, old_endpoints, endpoints, actor="web:discover")
            write_json(project_docs_file(root, project), to_jsonable(endpoints))
            write_json(project_metadata_file(root, project), metadata)
            return f"已识别接口 {len(endpoints)} 个"

        start_task(project, "discover", "接口识别", run_discover)
        handler.redirect(url_for("/interfaces", project, {"message": "接口识别任务已开始，可关闭提示并继续使用其它功能"}))
        return
    if path == "/actions/update-source":
        if active_task(project, "discover"):
            handler.redirect(url_for("/interfaces", project, {"message": "接口识别任务还在进行中，请稍后再修改来源"}))
            return
        try:
            source, text, source_name = source_from_form(root, project, form)
        except Exception as exc:
            handler.redirect(url_for("/interfaces", project, {"message": str(exc)}))
            return
        def run_update_source(progress):
            old_endpoints = load_endpoints(root, project)
            endpoints, metadata = discover_source(
                root,
                project,
                source,
                text=text,
                source_name=source_name,
                progress=progress,
                stream_callback=lambda chunk: progress(stream_delta=chunk),
            )
            progress("保存接口识别结果", stage="save")
            handle_endpoint_context_changes(root, project, old_endpoints, endpoints, actor="web:update_source")
            write_json(project_docs_file(root, project), to_jsonable(endpoints))
            write_json(project_metadata_file(root, project), metadata)
            return f"已更新来源并识别接口 {len(endpoints)} 个"

        start_task(project, "discover", "接口识别", run_update_source)
        handler.redirect(url_for("/interfaces", project, {"message": "接口来源已提交，识别任务正在后台进行"}))
        return
    if path == "/actions/add-endpoint":
        endpoints = load_endpoints(root, project)
        try:
            endpoints.append(endpoint_from_form(root, project, form))
        except Exception as exc:
            handler.redirect(url_for("/interfaces", project, {"message": str(exc)}))
            return
        save_endpoints(root, project, endpoints)
        handler.redirect(url_for("/interfaces", project))
        return
    if path == "/actions/save-endpoint":
        index = int(field(form, "index"))
        endpoints = load_endpoints(root, project)
        if 0 <= index < len(endpoints):
            try:
                endpoints[index] = endpoint_from_form(root, project, form)
            except Exception as exc:
                handler.redirect(url_for("/interfaces", project, {"message": str(exc)}))
                return
            save_endpoints(root, project, endpoints)
        handler.redirect(url_for("/interfaces", project))
        return
    if path == "/actions/delete-endpoint":
        index = int(field(form, "index"))
        endpoints = load_endpoints(root, project)
        if 0 <= index < len(endpoints):
            endpoints.pop(index)
            save_endpoints(root, project, endpoints)
        handler.redirect(url_for("/interfaces", project))
        return
    if path == "/actions/generate":
        if active_task(project, "generate"):
            handler.redirect(url_for("/cases", project, {"message": "用例生成任务还在进行中，请稍后查看结果"}))
            return
        def run_generate(progress):
            progress("读取接口定义", stage="read")
            endpoints = load_endpoints(root, project)
            progress("准备用例生成策略和大模型 Prompt", stage="prepare")
            progress("正在调用大模型生成用例；如果模型响应较慢，可能需要等待较久", stage="llm")
            cases, warning = generate_cases_with_config(root, endpoints, project=project, stream_callback=lambda chunk: progress(stream_delta=chunk))
            progress("解析并保存生成的测试用例", stage="save")
            save_cases(root, cases, project=project)
            suffix = f"；{warning}" if warning else ""
            return f"已生成用例 {len(cases)} 个{suffix}"

        start_task(project, "generate", "用例生成", run_generate)
        handler.redirect(url_for("/cases", project, {"message": "用例生成任务已开始，可关闭提示并继续使用其它功能"}))
        return
    if path == "/actions/supplement-cases":
        if active_task(project, "generate"):
            handler.redirect(url_for("/cases", project, {"message": "用例生成任务还在进行中，请稍后查看结果"}))
            return
        scenario_text = field(form, "scenario_text").strip()
        if not scenario_text:
            handler.redirect(url_for("/cases", project, {"message": "补充场景说明不能为空"}))
            return
        endpoint_key = field(form, "endpoint_key")
        max_cases = bounded_int(field(form, "max_cases"), default=5, minimum=1, maximum=20)

        def run_supplement(progress):
            progress("读取接口定义和现有用例", stage="read")
            all_endpoints = load_endpoints(root, project)
            existing = load_cases_from_markdown(root, project)
            endpoints = selected_supplement_endpoints(all_endpoints, endpoint_key)
            related_cases = cases_for_endpoints(existing, endpoints)
            progress("准备用例补充策略和大模型 Prompt", stage="prepare")
            progress("正在调用大模型补充用例；如果模型响应较慢，可能需要等待较久", stage="llm")
            generated, warning = supplement_cases_with_config(
                root,
                endpoints,
                related_cases,
                project=project,
                scenario_text=scenario_text,
                max_cases=max_cases,
                stream_callback=lambda chunk: progress(stream_delta=chunk),
            )
            progress("追加保存补充用例", stage="save")
            if generated:
                save_cases(root, [*existing, *generated], project=project)
            suffix = f"；{warning}" if warning else ""
            return f"已补充用例 {len(generated)} 个{suffix}"

        start_task(project, "generate", "补充用例", run_supplement)
        handler.redirect(url_for("/cases", project, {"message": "补充用例任务已开始，可关闭提示并继续使用其它功能"}))
        return
    if path == "/actions/run-cases":
        if active_task(project, "run"):
            handler.redirect(url_for("/cases", project, {"message": "用例执行任务还在进行中，请稍后查看结果"}))
            return
        mode = field(form, "mode") or "smoke"
        parallel = int(field(form, "parallel") or "0")
        if parallel > 0:
            config = load_config(root)
            config.setdefault("execution", {})["parallel"] = parallel
            save_config(root, config)

        def run_case_task(progress):
            progress("读取测试用例", stage="read")
            cases = load_cases_from_markdown(root, project)
            progress("筛选执行范围", stage="filter")
            case_ids = None
            if mode == "smoke":
                case_ids = {case.id for case in cases if case.runnable and ("happy_path" in case.tags or "dependency_chain" in case.tags)}
            selected_cases = [case for case in cases if case_ids is None or case.id in case_ids]
            runnable_count = sum(1 for case in selected_cases if case.runnable)
            skipped_count = len(selected_cases) - runnable_count
            progress(f"开始执行 {runnable_count} 条可执行用例；{skipped_count} 条不可执行用例会记录为 skipped", stage="execute")
            results = run_cases(root, project=project, case_ids=case_ids, run_scope=mode)
            passed = sum(1 for item in results if item.status == "passed")
            failed = sum(1 for item in results if item.status == "failed")
            skipped = sum(1 for item in results if item.status == "skipped")
            progress(f"生成执行报告：通过 {passed}，失败 {failed}，跳过 {skipped}", stage="report")
            write_report(
                root,
                results,
                project=project,
                ai_review=build_ai_failure_review(root, project, results),
                analysis=build_ai_report_analysis(root, project, results),
            )
            rate = f"{(passed / len(results) * 100):.1f}%" if results else "-"
            return f"用例执行完成：共 {len(results)} 条，通过 {passed}，失败 {failed}，跳过 {skipped}，通过率 {rate}"

        start_task(project, "run", "用例执行", run_case_task)
        handler.redirect(url_for("/cases", project, {"message": "用例执行任务已开始，可关闭提示并继续使用其它功能"}))
        return
    if path == "/actions/add-schedule":
        config = load_config(root)
        config.setdefault("schedules", []).append(schedule_from_form(form))
        save_config(root, config)
        handler.redirect(url_for("/cases", project, {"message": "定时任务已添加"}))
        return
    if path == "/actions/delete-schedule":
        config = load_config(root)
        name = field(form, "name")
        config["schedules"] = [item for item in config.get("schedules", []) if str(item.get("name", "")) != name]
        save_config(root, config)
        handler.redirect(url_for("/cases", project, {"message": "定时任务已删除"}))
        return
    if path == "/actions/run-schedule-once":
        name = field(form, "name")
        config = load_config(root)
        for item in config.get("schedules", []):
            if str(item.get("name", "")) == name:
                from ai_apitest.orchestrator.scheduler.runner import execute_scheduled_run
                execute_scheduled_run(root, project, item)
                break
        handler.redirect(url_for("/reports", project))
        return
    if path == "/actions/delete-report":
        name = field(form, "name")
        report = project_reports_dir(root, project) / name
        if is_safe_report_name(name) and report.exists() and report.parent == project_reports_dir(root, project):
            report.unlink()
        handler.redirect(url_for("/reports", project))
        return
    if path == "/actions/context-confirm":
        update_lesson_review(root, project, field(form, "lesson_id"), lifecycle_status="active", review_status="confirmed", actor="user:web")
        handler.redirect(url_for("/context", project))
        return
    if path == "/actions/context-ignore":
        update_lesson_review(root, project, field(form, "lesson_id"), lifecycle_status="ignored", review_status="rejected", actor="user:web")
        handler.redirect(url_for("/context", project))
        return
    if path == "/actions/context-ignore-group":
        for lesson_id in form.get("lesson_id", []):
            update_lesson_review(root, project, str(lesson_id), lifecycle_status="ignored", review_status="rejected", actor="user:web")
        handler.redirect(url_for("/context", project))
        return
    if path == "/actions/context-resolve-known-issue":
        resolve_known_issue(root, project, field(form, "lesson_id"), actor="user:web")
        handler.redirect(url_for("/context", project))
        return
    if path == "/actions/context-edit":
        tags = [item.strip() for item in field(form, "tags").split(",") if item.strip()]
        edit_lesson(
            root,
            project,
            field(form, "lesson_id"),
            title=field(form, "title"),
            content=field(form, "content"),
            confidence=field(form, "confidence"),
            tags=tags,
            actor="user:web",
        )
        handler.redirect(url_for("/context", project))
        return
    if path == "/actions/context-resolve-conflict":
        resolve_conflict(
            root,
            project,
            relation_id=int(field(form, "relation_id")),
            resolution=field(form, "resolution"),
            actor="user:web",
        )
        handler.redirect(url_for("/context", project))
        return
    if path == "/actions/context-resolve-similar":
        resolve_similar(
            root,
            project,
            relation_id=int(field(form, "relation_id")),
            resolution=field(form, "resolution"),
            actor="user:web",
        )
        handler.redirect(url_for("/context", project))
        return
    if path == "/actions/save-llm":
        config = load_config(root)
        role = normalize_llm_role(field(form, "role"))
        config["llm"] = normalize_llm_config(config.get("llm", {}))
        config["llm"][role] = llm_role_from_form(root, role, form, config["llm"][role])
        save_config(root, config)
        handler.redirect(url_for("/config", project, {"saved": "llm"}))
        return
    if path == "/actions/test-llm":
        config = load_config(root)
        role = normalize_llm_role(field(form, "role"))
        llm_config = normalize_llm_config(config.get("llm", {}))
        llm = resolve_llm_role(llm_config, role)
        message = "ok"
        try:
            result = llm_check(
                base_url=llm.get("base_url", ""),
                model=llm.get("model", ""),
                api_key_env=llm.get("api_key_env", ""),
                provider=llm.get("provider", "openai-compatible"),
            )
            message = f"{llm_role_label(role)}连通性正常：{result}"
        except Exception as exc:
            message = f"{llm_role_label(role)}连通性失败：{exc}"
        handler.redirect(url_for("/config", project, {"message": message}))
        return
    if path == "/actions/toggle-generation-strategy":
        config = load_config(root)
        config.setdefault("case_generation", {}).setdefault("default_strategies", {})[field(form, "name")] = field(form, "enabled") == "on"
        save_config(root, config)
        handler.redirect(url_for("/config", project))
        return
    if path == "/actions/add-custom-generation-strategy":
        add_custom_strategy(root, "case_generation", form)
        handler.redirect(url_for("/config", project))
        return
    if path == "/actions/delete-custom-generation-strategy":
        delete_custom_strategy(root, "case_generation", field(form, "name"))
        handler.redirect(url_for("/config", project))
        return
    if path == "/actions/add-execution-strategy":
        add_custom_execution_strategy(root, form)
        handler.redirect(url_for("/config", project))
        return
    if path == "/actions/delete-execution-strategy":
        delete_custom_strategy(root, "execution_strategies", field(form, "name"))
        handler.redirect(url_for("/config", project))
        return
    if path == "/actions/governance-toggle":
        key = field(form, "key")
        if key not in _GOVERNANCE_TOGGLE_KEYS:
            handler.redirect(url_for("/config", project, {"message": f"不支持的配置项：{key}"}))
            return
        value = field(form, "value") == "on"
        config = load_config(root)
        config.setdefault("governance", {})[key] = value
        save_config(root, config)
        handler.redirect(url_for("/config", project))
        return
    if path == "/actions/create-project":
        raw_name = field(form, "name")
        new_project = safe_project_name(raw_name)
        if not raw_name.strip():
            handler.redirect(url_for("/", project, {"message": "项目名称不能为空"}))
            return
        init_project(root, new_project)
        handler.redirect(url_for("/", new_project, {"message": f"项目 {new_project} 已创建"}))
        return
    handler.send_error(404)


def discover_source(
    root: Path,
    project: str,
    source: str,
    *,
    text: str | None = None,
    source_name: str = "",
    progress=None,
    stream_callback=None,
) -> tuple[list[Endpoint], dict]:
    source = source.strip()
    if text is None:
        if progress:
            progress("读取接口文档", stage="read")
        text = load_source_text(source)
    if progress:
        progress("解析接口文档", stage="parse")
    endpoints, metadata = parse_api_document(text)
    if progress:
        progress("正在调用大模型识别接口；如果模型响应较慢，可能需要等待较久", stage="llm")
    endpoints, metadata = apply_ai_discovery_if_configured(root, text, endpoints, metadata, stream_callback=stream_callback)
    metadata["source"] = source
    if source_name:
        metadata["source_name"] = source_name
        metadata["source_kind"] = "file"
    return endpoints, metadata


def handle_endpoint_context_changes(root: Path, project: str, old_endpoints: list[Endpoint], new_endpoints: list[Endpoint], *, actor: str) -> None:
    if old_endpoints:
        diff = diff_endpoints(old_endpoints, new_endpoints)
        changed = [*diff.get("removed", []), *diff.get("changed", [])]
        if changed:
            mark_lessons_for_endpoint_changes(root, project, changed, actor=actor)
    infer_dependency_candidates(root, project, new_endpoints)


def source_from_form(root: Path, project: str, form: dict) -> tuple[str, str | None, str]:
    source_mode = field(form, "source_mode") or "file"
    if source_mode == "url":
        source = field(form, "source").strip()
        if not source:
            raise ValueError("接口文档 URL 不能为空")
        return source, None, ""
    uploaded_text = field(form, "source_text")
    uploaded_name = field(form, "source_filename").strip()
    if not uploaded_text or not uploaded_name:
        raise ValueError("请选择本地接口文档文件")
    source_path = save_uploaded_source(root, project, uploaded_name, uploaded_text)
    return str(source_path), uploaded_text, uploaded_name


def save_uploaded_source(root: Path, project: str, filename: str, content: str) -> Path:
    safe_name = safe_project_name(Path(filename).name) or "api-doc"
    source_dir = project_docs_dir(root, project) / "sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    path = source_dir / safe_name
    path.write_text(content, encoding="utf-8")
    return path


def source_display(metadata: dict) -> str:
    source = str(metadata.get("source", "")).strip()
    if not source:
        return "未设置"
    if str(metadata.get("source_kind", "")) == "file" and metadata.get("source_name"):
        return str(metadata["source_name"])
    if source.startswith(("http://", "https://")):
        return source
    return Path(source).name or source


def source_full(metadata: dict) -> str:
    return str(metadata.get("source", "")).strip()


def render_dashboard(root: Path, project: str, parsed) -> str:
    endpoints = load_endpoints(root, project)
    cases = load_cases_from_markdown(root, project)
    reports = sorted(project_reports_dir(root, project).glob("*.md"), reverse=True)
    latest_report = parse_report_summary(reports[0]) if reports else None
    context = load_context_stats(root, project)
    pending_context = context.get("by_lifecycle", {}).get("pending", 0)
    master_llm = normalize_llm_config(load_config(root).get("llm", {}))["master"]
    llm_configured = bool(master_llm.get("base_url") and master_llm.get("model"))
    llm_title = master_llm.get("model") if llm_configured else "未配置"
    llm_hint = "当前主控模型" if llm_configured else "未配置大模型服务"
    body = render_template(
        "dashboard.html",
        {
            "PROJECT": esc(project),
            "INTERFACES_URL": url_for("/interfaces", project),
            "CASES_URL": url_for("/cases", project),
            "REPORTS_URL": url_for("/reports", project),
            "CONTEXT_URL": url_for("/context", project),
            "CONFIG_URL": url_for("/config", project),
            "ENDPOINT_COUNT": str(len(endpoints)),
            "CASE_COUNT": str(len(cases)),
            "SUCCESS_RATE": esc(latest_report["success_rate"] if latest_report else "-"),
            "LLM_TITLE": esc(llm_title),
            "LLM_HINT": esc(llm_hint),
            "CONTEXT_COUNT": str(context.get("total_lessons", 0)),
            "CONTEXT_HINT": f"待确认 {pending_context} 条",
        },
    )
    return page(root, project, "仪表盘", body)


def render_interfaces(root: Path, project: str, parsed) -> str:
    query = parse_qs(parsed.query)
    metadata = read_json(project_metadata_file(root, project), {})
    raw_message = (query.get("message") or [metadata.get("ai_discovery_error", "")])[0]
    message = display_message_for_tasks(project, raw_message, ["discover"])
    rows = []
    modals = []
    endpoints = load_endpoints(root, project)
    for index, endpoint in enumerate(endpoints):
        rows.append(render_endpoint_row(endpoint, index, project))
        modals.append(render_endpoint_modals(endpoint, index, project))
    body = render_template(
        "interfaces.html",
        {
            "DASHBOARD_URL": url_for("/", project),
            "HIDDEN_PROJECT": hidden_project(project),
            "MESSAGE": f'<div class="notice">{esc(message)}</div>' if message else "",
            "SOURCE": esc(source_full(metadata)),
            "SOURCE_DISPLAY": esc(source_display(metadata)),
            "SOURCE_FULL": esc(source_full(metadata)),
            "SOURCE_DETAIL_DISABLED": "disabled" if not source_full(metadata) else "",
            "ROWS": "".join(rows) or '<tr><td colspan="4">暂无接口</td></tr>',
            "ENDPOINT_MODALS": "".join(modals),
            "ENDPOINT_FORM": render_endpoint_form(project),
        },
    )
    return page(root, project, "接口信息", body)


def render_endpoint_row(endpoint: Endpoint, index: int, project: str) -> str:
    detail_id = f"endpoint-detail-{index}"
    delete_id = f"endpoint-delete-{index}"
    return f"""
    <tr>
      <td>{esc(endpoint.summary or f"{endpoint.method} {endpoint.path}")}</td>
      <td><span class="method-pill">{esc(endpoint.method)}</span></td>
      <td><code>{esc(endpoint.path)}</code></td>
      <td class="actions-cell">
        <button type="button" onclick="document.getElementById('{detail_id}').showModal()">详情</button>
        <button type="button" class="danger" onclick="document.getElementById('{delete_id}').showModal()">删除</button>
      </td>
    </tr>
    """


def render_endpoint_modals(endpoint: Endpoint, index: int, project: str) -> str:
    detail_id = f"endpoint-detail-{index}"
    edit_id = f"endpoint-edit-{index}"
    delete_id = f"endpoint-delete-{index}"
    return f"""
    <dialog id="{detail_id}" class="modal endpoint-modal">
      <div class="modal-form">
        <div class="modal-head">
          <h3>{esc(endpoint.summary or f"{endpoint.method} {endpoint.path}")}</h3>
          <button type="button" class="ghost" onclick="document.getElementById('{detail_id}').close()">关闭</button>
        </div>
        {endpoint_detail(endpoint)}
        <div class="modal-actions">
          <button type="button" onclick="document.getElementById('{detail_id}').close(); document.getElementById('{edit_id}').showModal()">编辑</button>
        </div>
      </div>
    </dialog>
    <dialog id="{edit_id}" class="modal endpoint-modal" data-reset-on-close>
      <form method="post" action="/actions/save-endpoint" class="modal-form">
        <div class="modal-head">
          <h3>编辑接口</h3>
          <button type="button" class="ghost" onclick="cancelDialog('{edit_id}')">关闭</button>
        </div>
        {hidden_project(project)}
        <input type="hidden" name="index" value="{index}">
        {endpoint_edit_fields(endpoint)}
        <div class="modal-actions">
          <button type="button" class="ghost" onclick="cancelDialog('{edit_id}')">取消</button>
          <button>保存</button>
        </div>
      </form>
    </dialog>
    <dialog id="{delete_id}" class="modal">
      <form method="post" action="/actions/delete-endpoint" class="modal-form">
        <div class="modal-head">
          <h3>确认删除接口</h3>
          <button type="button" class="ghost" onclick="document.getElementById('{delete_id}').close()">关闭</button>
        </div>
        {hidden_project(project)}
        <input type="hidden" name="index" value="{index}">
        <p class="danger-text">删除后该接口不会参与后续用例生成。确认删除 {esc(endpoint.method)} {esc(endpoint.path)}？</p>
        <div class="modal-actions">
          <button type="button" class="ghost" onclick="document.getElementById('{delete_id}').close()">取消</button>
          <button class="danger">确认删除</button>
        </div>
      </form>
    </dialog>
    """


def render_endpoint_form(project: str) -> str:
    dialog_id = "endpoint-add-dialog"
    return f"""
    <button type="button" onclick="document.getElementById('{dialog_id}').showModal()">新增接口</button>
    <dialog id="{dialog_id}" class="modal endpoint-modal" data-reset-on-close>
      <form method="post" action="/actions/add-endpoint" class="modal-form">
        <div class="modal-head">
          <h3>新增接口</h3>
          <button type="button" class="ghost" onclick="cancelDialog('{dialog_id}')">关闭</button>
        </div>
        {hidden_project(project)}
        {endpoint_edit_fields(Endpoint(method="GET", path="", summary=""))}
        <div class="modal-actions">
          <button type="button" class="ghost" onclick="cancelDialog('{dialog_id}')">取消</button>
          <button>保存</button>
        </div>
      </form>
    </dialog>
    """


def endpoint_detail(endpoint: Endpoint) -> str:
    parameters = json.dumps(endpoint.parameters, ensure_ascii=False, indent=2) if endpoint.parameters else "无"
    request_body = json.dumps(endpoint.request_body, ensure_ascii=False, indent=2) if endpoint.request_body else "无"
    responses = json.dumps(endpoint.responses, ensure_ascii=False, indent=2) if endpoint.responses else "无"
    tags = ", ".join(endpoint.tags) if endpoint.tags else "无"
    return f"""
    <dl class="endpoint-detail-list">
      <div><dt>请求方法</dt><dd><span class="method-pill">{esc(endpoint.method)}</span></dd></div>
      <div><dt>接口路径</dt><dd><code>{esc(endpoint.path)}</code></dd></div>
      <div><dt>接口描述</dt><dd>{esc(endpoint.description or "-")}</dd></div>
      <div><dt>标签</dt><dd>{esc(tags)}</dd></div>
      <div><dt>参数</dt><dd><pre class="code-block">{esc(parameters)}</pre></dd></div>
      <div><dt>请求体</dt><dd><pre class="code-block">{esc(request_body)}</pre></dd></div>
      <div><dt>响应</dt><dd><pre class="code-block">{esc(responses)}</pre></dd></div>
    </dl>
    """


def endpoint_edit_fields(endpoint: Endpoint) -> str:
    parameters = json.dumps(endpoint.parameters, ensure_ascii=False, indent=2) if endpoint.parameters else ""
    return f"""
      <label>接口名称</label>
      <input name="summary" value="{esc(endpoint.summary)}" placeholder="接口名称">
      <label>请求方法</label>
      {method_select(endpoint.method or "GET")}
      <label>接口路径</label>
      <input name="path" value="{esc(endpoint.path)}" placeholder="/api/example">
      <label>参数 JSON</label>
      <textarea class="small-textarea" name="parameters" placeholder='[{{"name":"id","in":"query","required":false,"schema":{{"type":"string"}}}}]'>{esc(parameters)}</textarea>
      <label>请求体</label>
      {request_body_fields(endpoint.request_body)}
    """


def schedule_from_form(form: dict) -> dict:
    run_time = field(form, "run_time") or "09:00"
    frequency = field(form, "frequency") or "daily"
    parts = run_time.strip().split(":")
    hour = parts[0] if len(parts) >= 1 else "9"
    minute = parts[1] if len(parts) >= 2 else "0"
    if frequency == "once":
        cron = f"{minute} {hour} * * *"
    else:
        cron = f"{minute} {hour} * * *"
    return {
        "name": field(form, "name"),
        "description": field(form, "description"),
        "scope": field(form, "scope") or "full",
        "cron": cron,
        "frequency": frequency,
        "run_time": run_time,
        "tags": [t.strip() for t in field(form, "tags").split(",") if t.strip()],
        "priority": [p.strip() for p in field(form, "priority").split(",") if p.strip()],
        "enabled": True,
    }

def render_schedule_rows(project: str, schedules: list) -> str:
    if not schedules:
        return "<tr><td colspan='5'>暂无定时任务</td></tr>"
    scope_labels = {"full": "全量用例", "smoke": "冒烟用例"}
    rows = []
    for s in schedules:
        name = esc(str(s.get("name", "")))
        scope = scope_labels.get(str(s.get("scope", "full")), str(s.get("scope", "full")))
        freq = str(s.get("frequency", "daily"))
        run_time = str(s.get("run_time", "-"))
        freq_label = "每天 " + run_time if freq == "daily" else "单次 " + run_time
        tags = esc(", ".join(s.get("tags", [])) or "-")
        onclick = esc("return confirm('确认删除定时任务？')")
        row = (
            "<tr><td>" + name + "</td>"
            "<td>" + scope + "</td>"
            "<td>" + freq_label + "</td>"
            "<td>" + tags + "</td>"
            "<td class='actions-cell'>"
            "<form method='post' action='/actions/run-schedule-once' style='display:inline'>" + hidden_project(project) + "<input type='hidden' name='name' value='" + esc(name) + "'><button>&#9654; 立即执行</button></form> "
            "<form method='post' action='/actions/delete-schedule' style='display:inline' onsubmit='" + onclick + "'>" + hidden_project(project) + "<input type='hidden' name='name' value='" + esc(name) + "'><button class='danger'>删除</button></form>"
            "</td></tr>"
        )
        rows.append(row)
    return "".join(rows)

def render_cases(root: Path, project: str, parsed) -> str:
    query = parse_qs(parsed.query)
    config = load_config(root)
    cases = load_cases_from_markdown(root, project)
    latest = latest_case_results(root, project)
    execution = config.get("execution_strategies", {})
    schedules = config.get("schedules", [])
    parallel = str(config.get("execution", {}).get("parallel", 4))
    raw_message = (query.get("message") or [""])[0]
    message = display_message_for_tasks(project, raw_message, ["generate", "run"])

    # Sort
    sort_field = (query.get("sort") or ["id"])[0]
    sort_order = (query.get("order") or ["asc"])[0]
    if sort_field not in ("id", "priority", "last_time", "last_status"):
        sort_field = "id"
    if sort_order not in ("asc", "desc"):
        sort_order = "asc"

    def _case_sort_key(case):
        last = latest.get(case.id, {})
        if sort_field == "priority":
            p = str(case.priority).upper()
            if p.startswith("P") and len(p) > 1 and p[1:].isdigit():
                return int(p[1:])
            return 999
        if sort_field == "last_time":
            return last.get("created_at", "")
        if sort_field == "last_status":
            return last.get("status", "")
        return case.id

    reverse = sort_order == "desc"
    sorted_cases = sorted(cases, key=_case_sort_key, reverse=reverse)

    # Pagination
    cur_page = bounded_int((query.get("page") or ["1"])[0], default=1, minimum=1, maximum=10000)
    page_size_raw = bounded_int((query.get("size") or ["10"])[0], default=10, minimum=1, maximum=100)
    page_size = page_size_raw if page_size_raw in (5, 10, 20, 50, 100) else 10
    total = len(sorted_cases)
    total_pages = max(1, (total + page_size - 1) // page_size)
    cur_page = min(cur_page, total_pages)
    start = (cur_page - 1) * page_size
    page_cases = sorted_cases[start:start + page_size]

    rows = []
    modals = []
    endpoints = load_endpoints(root, project)
    generation_pack = latest_context_pack(root, project, "case_generation")
    generation_pack_html = render_context_pack_panel(root, project, generation_pack)
    for index, case in enumerate(page_cases):
        last = latest.get(case.id, {})
        detail_id = f"case-detail-{start + index}"
        rows.append(
            "<tr>"
            f"<td>{esc(case.id)}</td><td>{esc(case.title)}</td><td>{esc(case.priority)}</td>"
            f"<td>{esc(last.get('created_at', ''))}</td><td class='{esc(last.get('status', ''))}'>{esc(last.get('status', ''))}</td>"
            f"<td><button type='button' onclick=\"document.getElementById('{detail_id}').showModal()\">详情</button></td>"
            "</tr>"
        )
        modals.append(render_case_detail_modal(case, detail_id, generation_pack_html))

    sort_headers = _render_sort_headers(project, sort_field, sort_order)
    pager_html = _render_case_pager(project, cur_page, total_pages, total, page_size, sort_field, sort_order)

    body = render_template(
        "cases.html",
        {
            "DASHBOARD_URL": url_for("/", project),
            "HIDDEN_PROJECT": hidden_project(project),
            "MESSAGE": f'<div class="notice">{esc(message)}</div>' if message else "",
            "SUPPLEMENT_FORM": render_supplement_form(project, endpoints),
            "PARALLEL": parallel,
            "SCHEDULE_ROWS": render_schedule_rows(project, schedules),
            "EXECUTION_MODE_OPTIONS": execution_mode_options(execution.get("custom_strategies", [])),
            "SORT_HEADERS": sort_headers,
            "PAGER": pager_html,
            "PAGE_INFO": f"共 {total} 条，第 {cur_page}/{total_pages} 页" if total else "",
            "ROWS": "".join(rows) or '<tr><td colspan="6">暂无用例</td></tr>',
            "CASE_MODALS": "".join(modals),
        },
    )
    return page(root, project, "用例信息", body)


def _render_sort_headers(project: str, field: str, order: str) -> str:
    columns = [
        ("id", "用例编号"),
        ("", "用例名称"),
        ("priority", "用例等级"),
        ("last_time", "上次执行时间"),
        ("last_status", "上次执行结果"),
        ("", "操作"),
    ]
    cells = []
    for key, label in columns:
        if not key:
            cells.append(f"<th>{label}</th>")
            continue
        next_order = "asc" if key != field or order == "desc" else "desc"
        arrow = ""
        if key == field:
            arrow = " ▾" if order == "desc" else " ▴"
        sort_url = url_for("/cases", project, {"sort": key, "order": next_order})
        cls = "sortable sorted" if key == field else "sortable"
        cells.append(f'<th class="{cls}"><a href="{sort_url}">{label}{arrow}</a></th>')
    return "".join(cells)


def _render_case_pager(project: str, page: int, total_pages: int, total: int, size: int, sort_field: str, sort_order: str) -> str:
    if total == 0:
        return ""
    base = {"sort": sort_field, "order": sort_order}
    size_opts = "".join(
        f"<option value='{s}' {'selected' if s == size else ''}>{s} 条/页</option>"
        for s in (5, 10, 20, 50, 100)
    )
    prev_url = url_for("/cases", project, {**base, "page": max(1, page - 1), "size": size}) if page > 1 else ""
    next_url = url_for("/cases", project, {**base, "page": min(total_pages, page + 1), "size": size}) if page < total_pages else ""
    prev_link = f'<a href="{prev_url}" class="pager-btn">上一页</a>' if prev_url else '<span class="pager-btn disabled">上一页</span>'
    next_link = f'<a href="{next_url}" class="pager-btn">下一页</a>' if next_url else '<span class="pager-btn disabled">下一页</span>'
    page_input = (
        f"<form method='get' action='/cases' class='pager-jump'>"
        f"<input type='hidden' name='project' value='{esc(project)}'>"
        f"<input type='hidden' name='sort' value='{esc(sort_field)}'>"
        f"<input type='hidden' name='order' value='{esc(sort_order)}'>"
        f"<input type='hidden' name='size' value='{size}'>"
        f"<input type='number' name='page' value='{page}' min='1' max='{total_pages}'>"
        f"<span>/ {total_pages}</span>"
        f"</form>"
    )
    size_selector = (
        f"<form method='get' action='/cases' class='pager-size'>"
        f"<input type='hidden' name='project' value='{esc(project)}'>"
        f"<input type='hidden' name='sort' value='{esc(sort_field)}'>"
        f"<input type='hidden' name='order' value='{esc(sort_order)}'>"
        f"<select name='size' onchange='this.form.submit()'>{size_opts}</select>"
        f"</form>"
    )
    return (
        f"<div class='table-pager'>"
        f"<div class='pager-info'>共 {total} 条</div>"
        f"{size_selector}"
        f"<div class='pager-nav'>{prev_link}{page_input}{next_link}</div>"
        f"</div>"
    )


def render_supplement_form(project: str, endpoints: list[Endpoint]) -> str:
    dialog_id = "case-supplement-dialog"
    options = ["<option value=''>自动匹配 / 全部接口</option>"]
    for endpoint in endpoints:
        label = endpoint.summary or f"{endpoint.method} {endpoint.path}"
        value = endpoint_key_value(endpoint)
        options.append(f"<option value='{esc(value)}'>{esc(endpoint.method)} {esc(endpoint.path)} · {esc(label)}</option>")
    return f"""
    <button type="button" onclick="document.getElementById('{dialog_id}').showModal()">补充用例</button>
    <dialog id="{dialog_id}" class="modal endpoint-modal" data-reset-on-close>
      <form method="post" action="/actions/supplement-cases" class="modal-form" data-background-task data-task-action="generate">
        <div class="modal-head">
          <h3>补充用例</h3>
          <button type="button" class="ghost" onclick="cancelDialog('{dialog_id}')">关闭</button>
        </div>
        {hidden_project(project)}
        <label>目标接口</label>
        <select name="endpoint_key">{''.join(options)}</select>
        <label>补充场景说明</label>
        <textarea name="scenario_text" class="body-textarea" required placeholder="例如：补充 prefix 包含中文、特殊字符、重复前缀时的异常用例"></textarea>
        <label>最多生成条数</label>
        <input type="number" name="max_cases" value="5" min="1" max="20">
        <div class="modal-actions">
          <button type="button" class="ghost" onclick="cancelDialog('{dialog_id}')">取消</button>
          <button>开始补充</button>
        </div>
      </form>
    </dialog>
    """


def render_case_detail_modal(case, dialog_id: str, context_pack_html: str = "") -> str:
    assertions = json.dumps(to_jsonable(case.assertions), ensure_ascii=False, indent=2)
    request_data = json.dumps(
        {
            "method": case.method,
            "path": case.path,
            "headers": case.headers,
            "path_params": case.path_params,
            "query": case.query,
            "body": case.body,
        },
        ensure_ascii=False,
        indent=2,
    )
    setup_steps = json.dumps(to_jsonable(case.setup_steps), ensure_ascii=False, indent=2) if case.setup_steps else "无"
    chain_steps = json.dumps(to_jsonable(case.steps), ensure_ascii=False, indent=2) if case.steps else "无"
    cleanup_steps = json.dumps(to_jsonable(case.cleanup_steps), ensure_ascii=False, indent=2) if case.cleanup_steps else "无"

    runnable_badge = '<span class="meta-badge meta-ok">可执行</span>' if case.runnable else '<span class="meta-badge meta-warn">不可执行</span>'
    risk_label = {"low": "低", "medium": "中", "high": "高"}.get(str(case.risk).lower(), str(case.risk) or "-")

    return f"""
    <dialog id="{dialog_id}" class="modal case-detail-modal">
      <div class="modal-form">
        <div class="modal-head">
          <h3><span class="case-id-chip">{esc(case.id)}</span> {esc(case.title)}</h3>
          <button type="button" class="ghost" onclick="document.getElementById('{dialog_id}').close()">关闭</button>
        </div>

        <div class="case-meta-row">
          <div class="meta-item"><span class="meta-label">等级</span><span class="meta-value">{esc(case.priority)}</span></div>
          <div class="meta-item"><span class="meta-label">设计方法</span><span class="meta-value">{esc(case.design_method)}</span></div>
          <div class="meta-item"><span class="meta-label">风险</span><span class="meta-value">{esc(risk_label)}</span></div>
          <div class="meta-item">{runnable_badge}</div>
        </div>

        <div class="case-section">
          <h4>前置条件</h4>
          {html_list(case.preconditions or ["无特殊前置条件。"])}
        </div>

        <div class="case-section">
          <h4>用例步骤</h4>
          {html_ordered_list(case.test_steps or ["按请求数据发送接口请求。", "检查实际响应是否满足断言。"])}
        </div>

        <div class="case-section">
          <h4>预期结果</h4>
          <p class="case-text">{esc(case.expected_result or "以断言列表为准。")}</p>
        </div>

        <div class="case-collapsibles">
          <details class="case-data">
            <summary>请求数据</summary>
            <pre class="code-block report-code">{esc(request_data)}</pre>
          </details>
          <details class="case-data">
            <summary>断言列表</summary>
            <pre class="code-block report-code">{esc(assertions)}</pre>
          </details>
          <details class="case-data">
            <summary>前置准备步骤</summary>
            <pre class="code-block report-code">{esc(setup_steps)}</pre>
          </details>
          <details class="case-data">
            <summary>链路步骤</summary>
            <pre class="code-block report-code">{esc(chain_steps)}</pre>
          </details>
          <details class="case-data">
            <summary>清理步骤</summary>
            <pre class="code-block report-code">{esc(cleanup_steps)}</pre>
          </details>
        </div>

        <details class="case-data">
          <summary>AI 生成上下文</summary>
          <div style="padding:12px">{context_pack_html}</div>
        </details>
      </div>
    </dialog>
    """


def html_list(items: list[str]) -> str:
    return "<ul class='compact-list'>" + "".join(f"<li>{esc(item)}</li>" for item in items) + "</ul>"


def html_ordered_list(items: list[str]) -> str:
    return "<ol class='compact-list'>" + "".join(f"<li>{esc(item)}</li>" for item in items) + "</ol>"


def render_reports(root: Path, project: str, parsed) -> str:
    query = parse_qs(parsed.query)
    current_page = max(1, int((query.get("page") or ["1"])[0]))
    reports = sorted(project_reports_dir(root, project).glob("*.md"), reverse=True)
    page_size = 10
    page_reports = reports[(current_page - 1) * page_size : current_page * page_size]
    rows = []
    for path in page_reports:
        summary = parse_report_summary(path)
        rows.append(
            "<tr>"
            f"<td>{esc(summary['title'])}</td><td>{esc(summary['time'])}</td><td>{summary['total']}</td><td>{summary['success_rate']}</td>"
            f"<td><a class='button-link' href='/report/{esc(path.name)}?project={esc(project)}'>详情</a></td>"
            f"<td><form method='post' action='/actions/delete-report' onsubmit=\"return confirm('确认删除该报告？')\">{hidden_project(project)}<input type='hidden' name='name' value='{esc(path.name)}'><button class='danger'>删除</button></form></td>"
            "</tr>"
        )
    total_pages = max(1, (len(reports) + page_size - 1) // page_size)
    pager = f"<div class='pager'><a href='{url_for('/reports', project, {'page': max(1, current_page - 1)})}'>上一页</a><span>{current_page} / {total_pages}</span><a href='{url_for('/reports', project, {'page': min(total_pages, current_page + 1)})}'>下一页</a></div>"
    trends = trend_overview(root, project, limit=10)
    trend_section = ""
    if trends.get("runs"):
        trend_section = f"""
        <section class="panel">
          <div class="section-head">
            <div><h2>趋势概览</h2><p>最近执行趋势概览，数据同步用于 AI 报告分析上下文。</p></div>
          </div>
          {render_trend_overview(trends)}
        </section>
        """
    body = render_template(
        "reports.html",
        {
            "DASHBOARD_URL": url_for("/", project),
            "ROWS": "".join(rows) or '<tr><td colspan="6">暂无报告</td></tr>',
            "PAGER": pager,
            "TREND_SECTION": trend_section,
        },
    )
    return page(root, project, "测试报告", body)


def render_report_detail(root: Path, project: str, path: Path) -> str:
    pack = latest_context_pack(root, project, "report_analysis")
    body = render_template(
        "report_detail.html",
        {
            "REPORTS_URL": url_for("/reports", project),
            "REPORT_HTML": markdown_to_html(path.read_text(encoding="utf-8")),
            "CONTEXT_PACK_HTML": render_context_pack_panel(root, project, pack),
        },
    )
    return page(root, project, "报告详情", body)


def render_context(root: Path, project: str, parsed) -> str:
    query = parse_qs(parsed.query)
    lifecycle = (query.get("status") or [""])[0]
    lesson_type = (query.get("type") or [""])[0]
    scope = (query.get("scope") or [""])[0]
    q = (query.get("q") or [""])[0]
    stats = load_context_stats(root, project)

    # Lesson pagination
    l_page = bounded_int((query.get("l_page") or ["1"])[0], default=1, minimum=1, maximum=10000)
    l_size = bounded_int((query.get("l_size") or ["10"])[0], default=10, minimum=1, maximum=100)
    l_size = l_size if l_size in (5, 10, 20, 50) else 10

    all_lessons = list_lessons(root, project, lifecycle_status=lifecycle, lesson_type=lesson_type, scope=scope, query_text=q, limit=1000)
    l_total = len(all_lessons)
    l_total_pages = max(1, (l_total + l_size - 1) // l_size)
    l_page = min(l_page, l_total_pages)
    l_start = (l_page - 1) * l_size
    page_lessons = all_lessons[l_start:l_start + l_size]

    conflicts = conflict_pairs(root, project, limit=20)
    similars = similar_pairs(root, project, limit=20)
    audits = recent_audit_logs(root, project, limit=100)
    trends = trend_overview(root, project, limit=5)

    # Anchor nav + section badges
    anchor_nav = _render_context_anchor(
        project, stats, lesson_count=len(all_lessons),
        conflict_count=len(conflicts), similar_count=len(similars), audit_count=len(audits),
        lifecycle=lifecycle, lesson_type=lesson_type, scope=scope, q=q,
    )

    # Lesson pager
    lesson_pager = _render_lesson_pager(project, l_page, l_total_pages, l_total, l_size, lifecycle, lesson_type, scope, q)

    # Fold conflicts and similars
    conflict_section = _render_foldable_section(
        "conflict", "疑似冲突裁决",
        "规则引擎先发现疑似冲突；配置分析模型后会由 AI 复核语义，最终仍由人工裁决。",
        len(conflicts),
        render_conflict_groups(project, conflicts) if conflicts else "<div class='empty-state'>暂无待裁决疑似冲突</div>"
    )
    similar_section = _render_foldable_section(
        "similar", "相似经验合并",
        "语义相近但未完全重复的经验需要人工确认是否合并。",
        len(similars),
        render_similar_cards(project, similars) if similars else "<div class='empty-state'>暂无待合并相似经验</div>"
    )

    body = render_template(
        "context.html",
        {
            "DASHBOARD_URL": url_for("/", project),
            "PROJECT": esc(project),
            "ANCHOR_NAV": anchor_nav,
            "HEALTH_CARDS": render_context_health(stats),
            "STATUS_SELECT": select_html("status", lifecycle, [("", "全部"), ("pending", "待确认"), ("active", "已启用"), ("ignored", "已忽略"), ("resolved", "已解决"), ("archived", "已归档")]),
            "TYPE_SELECT": select_html("type", lesson_type, [("", "全部"), ("business_rule", "业务规则"), ("assertion_pattern", "断言模式"), ("failure_pattern", "失败模式"), ("known_issue", "已知问题"), ("data_dependency", "数据依赖"), ("trend_insight", "趋势发现")]),
            "SCOPE_SELECT": select_html("scope", scope, [("", "全部"), ("project", "项目级"), ("endpoint", "接口级"), ("case", "用例级"), ("report", "报告级"), ("dependency", "依赖")]),
            "QUERY": esc(q),
            "LESSON_CARDS": render_lesson_cards(project, page_lessons),
            "LESSON_PAGER": lesson_pager,
            "LESSON_TOTAL": str(l_total),
            "CONFLICT_SECTION": conflict_section,
            "SIMILAR_SECTION": similar_section,
            "AUDIT_ROWS": render_audit_rows(audits),
            "TREND_HTML": render_trend_overview(trends),
        },
    )
    return page(root, project, "项目上下文", body)


def _render_context_anchor(project: str, stats: dict, lesson_count: int, conflict_count: int, similar_count: int, audit_count: int, lifecycle: str, lesson_type: str, scope: str, q: str) -> str:
    base = {"status": lifecycle, "type": lesson_type, "scope": scope, "q": q}
    items = [
        ("lessons", "经验库", lesson_count),
        ("conflicts", "冲突裁决", conflict_count),
        ("similars", "相似合并", similar_count),
        ("trend", "趋势概览", 0),
        ("audit", "审计日志", audit_count),
    ]
    links = []
    for anchor_id, label, count in items:
        badge = f'<span class="anchor-badge">{count}</span>' if count else ""
        links.append(f'<a href="#section-{anchor_id}" class="anchor-link">{label}{badge}</a>')
    return f'<nav class="context-anchor-nav">{"".join(links)}</nav>'


def _render_lesson_pager(project: str, page: int, total_pages: int, total: int, size: int, lifecycle: str, lesson_type: str, scope: str, q: str) -> str:
    if total == 0:
        return ""
    base = {"status": lifecycle, "type": lesson_type, "scope": scope, "q": q}
    prev_url = url_for("/context", project, {**base, "l_page": max(1, page - 1), "l_size": size}) if page > 1 else ""
    next_url = url_for("/context", project, {**base, "l_page": min(total_pages, page + 1), "l_size": size}) if page < total_pages else ""
    prev_link = f'<a href="{prev_url}" class="pager-btn">上一页</a>' if prev_url else '<span class="pager-btn disabled">上一页</span>'
    next_link = f'<a href="{next_url}" class="pager-btn">下一页</a>' if next_url else '<span class="pager-btn disabled">下一页</span>'
    size_opts = "".join(f"<option value='{s}' {'selected' if s == size else ''}>{s} 条/页</option>" for s in (5, 10, 20, 50))
    return (
        f"<div class='table-pager'>"
        f"<div class='pager-info'>共 {total} 条</div>"
        f"<form method='get' action='/context' class='pager-size'>"
        f"<input type='hidden' name='project' value='{esc(project)}'>"
        f"<input type='hidden' name='status' value='{esc(lifecycle)}'>"
        f"<input type='hidden' name='type' value='{esc(lesson_type)}'>"
        f"<input type='hidden' name='scope' value='{esc(scope)}'>"
        f"<input type='hidden' name='q' value='{esc(q)}'>"
        f"<select name='l_size' onchange='this.form.submit()'>{size_opts}</select>"
        f"</form>"
        f"<div class='pager-nav'>{prev_link}<span class='pager-info'>{page} / {total_pages}</span>{next_link}</div>"
        f"</div>"
    )


def _render_foldable_section(cls: str, title: str, desc: str, count: int, body_html: str) -> str:
    open_attr = " open" if count > 0 else ""
    return f"""
    <details class="panel context-fold{"" if count > 0 else " context-fold-empty"}" id="section-{cls}s" {open_attr}>
      <summary class="section-head">
        <div>
          <h2>{title} <span class="section-badge">{count}</span></h2>
          <p>{desc}</p>
        </div>
      </summary>
      <div class="context-list">{body_html}</div>
    </details>
    """


def render_context_health(stats: dict) -> str:
    lifecycle = stats.get("by_lifecycle", {})
    review = stats.get("by_review", {})
    verification = stats.get("by_verification", {})
    cards = [
        ("全部经验", stats.get("total_lessons", 0), "当前项目累计保存的经验总数"),
        ("待确认", lifecycle.get("pending", 0), "AI 提取后等待人工确认"),
        ("待裁决冲突", stats.get("conflicts", 0), "冲突关系数量，不是经验数量"),
        ("待复核", review.get("needs_review", 0), "接口变更后需要处理"),
        ("已验证", verification.get("verified", 0), "执行验证通过的事实"),
    ]
    return "".join(
        f"<div class='health-card'><span>{esc(label)}</span><strong>{value}</strong><small>{esc(hint)}</small></div>"
        for label, value, hint in cards
    )


def render_lesson_cards(project: str, lessons: list[dict]) -> str:
    if not lessons:
        return "<div class='empty-state'>暂无上下文经验</div>"
    cards = []
    for lesson in aggregate_prompt_candidates(lessons):
        if lesson.get("_group_items"):
            cards.append(render_dependency_group_lesson_card(project, lesson))
            continue
        tags = "、".join(label_for("tag", item) for item in lesson.get("tags", [])) or "-"
        endpoint = f"{lesson.get('method', '')} {lesson.get('path', '')}".strip() or lesson.get("scope", "-")
        edit_id = "lesson-edit-" + re.sub(r"[^a-zA-Z0-9_-]+", "-", lesson["id"])
        state_label = lesson_state_label(lesson)
        actions = []
        if lesson.get("lifecycle_status") == "pending":
            actions.append(f"<form method='post' action='/actions/context-confirm'>{hidden_project(project)}<input type='hidden' name='lesson_id' value='{esc(lesson['id'])}'><button>确认</button></form>")
        if lesson.get("lesson_type") == "known_issue" and lesson.get("lifecycle_status") == "resolved_candidate":
            actions.append(f"<form method='post' action='/actions/context-resolve-known-issue'>{hidden_project(project)}<input type='hidden' name='lesson_id' value='{esc(lesson['id'])}'><button>确认已解决</button></form>")
        if lesson.get("lifecycle_status") not in {"ignored", "archived", "merged"}:
            ignore_label = "忽略" if lesson.get("lifecycle_status") == "pending" else "停用"
            actions.append(f"<form method='post' action='/actions/context-ignore'>{hidden_project(project)}<input type='hidden' name='lesson_id' value='{esc(lesson['id'])}'><button class='danger'>{ignore_label}</button></form>")
        actions.append(f"<button type='button' onclick=\"document.getElementById('{edit_id}').showModal()\">编辑</button>")
        cards.append(
            f"""
            <article class="lesson-card">
              <div class="lesson-main">
                <div class="lesson-meta">
                  <span>{esc(label_for('scope', lesson.get('scope', '-')))}</span>
                  <span>{esc(label_for('lesson_type', lesson.get('lesson_type', '-')))}</span>
                  <span>{esc(state_label)}</span>
                  <span>{esc(label_for('confidence', lesson.get('confidence', '-')))}</span>
                </div>
                <h3>{esc(lesson.get('title', ''))}</h3>
                <p>{esc(lesson.get('content', ''))}</p>
                <small>{esc(endpoint)} · 出现 {lesson.get('occurrence_count', 0)} 次 · 标签：{esc(tags)}</small>
              </div>
              <div class="lesson-actions">{"".join(actions)}</div>
            </article>
            {render_lesson_edit_dialog(project, lesson, edit_id)}
            """
        )
    return "".join(cards)


def render_dependency_group_lesson_card(project: str, group: dict) -> str:
    items = [item for item in group.get("_group_items", []) if isinstance(item, dict)]
    tags = "、".join(label_for("tag", item) for item in group.get("tags", [])) or "-"
    rows = []
    dialogs = []
    for item in items:
        endpoint = f"{item.get('method', '')} {item.get('path', '')}".strip() or "-"
        edit_id = "lesson-edit-" + re.sub(r"[^a-zA-Z0-9_-]+", "-", item["id"])
        rows.append(
            f"""
            <li>
              <strong>{esc(endpoint)}</strong>
              <span>{esc(lesson_state_label(item))}</span>
              <span class="inline-actions">
                <button type="button" onclick="document.getElementById('{edit_id}').showModal()">编辑</button>
                <form method="post" action="/actions/context-ignore">
                  {hidden_project(project)}
                  <input type="hidden" name="lesson_id" value="{esc(item['id'])}">
                  <button class="danger">停用</button>
                </form>
              </span>
            </li>
            """
        )
        dialogs.append(render_lesson_edit_dialog(project, item, edit_id))
    detail_html = "<ul class='compact-list dependency-group-list'>" + "".join(rows) + "</ul>"
    group_actions = []
    if any(item.get("lifecycle_status") not in {"ignored", "archived", "merged"} for item in items):
        hidden_ids = "".join(f"<input type='hidden' name='lesson_id' value='{esc(item['id'])}'>" for item in items)
        group_actions.append(
            f"""
            <form method="post" action="/actions/context-ignore-group" onsubmit="return confirm('确认停用该依赖组下的全部具体依赖吗？')">
              {hidden_project(project)}
              {hidden_ids}
              <button class="danger">停用全部</button>
            </form>
            """
        )
    return f"""
    <article class="lesson-card lesson-card-group">
      <div class="lesson-main">
        <div class="lesson-meta">
          <span>依赖组</span>
          <span>{esc(label_for('lesson_type', group.get('lesson_type', '-')))}</span>
          <span>{esc(lesson_state_label(group))}</span>
          <span>{esc(label_for('confidence', group.get('confidence', '-')))}</span>
        </div>
        <h3>{esc(group.get('title', ''))}</h3>
        <p>{esc(group.get('content', ''))}</p>
        <details class="case-data">
          <summary>查看 {len(items)} 条具体依赖</summary>
          {detail_html}
        </details>
        <small>聚合展示 · 底层保留 {len(items)} 条可独立验证的依赖记录 · 标签：{esc(tags)}</small>
      </div>
      <div class="lesson-actions">{"".join(group_actions)}</div>
    </article>
    {"".join(dialogs)}
    """


def lesson_state_label(lesson: dict) -> str:
    if lesson.get("lesson_type") == "data_dependency":
        verification = str(lesson.get("verification_status") or "")
        if verification == "verified":
            prefix = "已验证依赖"
        elif verification == "invalidated":
            prefix = "依赖已失效"
        elif verification == "candidate":
            prefix = "候选依赖"
        else:
            prefix = label_for("lifecycle_status", lesson.get("lifecycle_status", "-"))
        return f"{prefix} · {label_for('review_status', lesson.get('review_status', '-'))}"
    return f"{label_for('lifecycle_status', lesson.get('lifecycle_status', '-'))} · {label_for('review_status', lesson.get('review_status', '-'))}"


def render_conflict_groups(project: str, conflicts: list[dict]) -> str:
    if not conflicts:
        return "<div class='empty-state'>暂无待裁决疑似冲突</div>"
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for item in conflicts:
        key = (
            str(item.get("method") or ""),
            str(item.get("path") or ""),
            str(item.get("environment") or ""),
        )
        groups.setdefault(key, []).append(item)
    blocks = []
    for (method, path, environment), items in groups.items():
        endpoint = f"{method} {path}".strip() or "-"
        pairs = "".join(render_conflict_pair(project, item) for item in items)
        blocks.append(
            f"""
            <details class="conflict-group" open>
              <summary>
                <strong>{esc(endpoint)}</strong>
                <span>{esc(environment or '通用环境')}</span>
                <span>共 {len(items)} 个疑似冲突关系</span>
              </summary>
              <div class="conflict-pairs">{pairs}</div>
            </details>
            """
        )
    return "".join(blocks)


def render_conflict_pair(project: str, item: dict) -> str:
    relation_id = str(item.get("relation_id", ""))
    note = str(item.get("notes") or "").strip()
    note_html = f"<p class='conflict-note'>{esc(note)}</p>" if note else ""
    return f"""
    <article class="conflict-card">
      <div class="lesson-meta">
        <span>疑似冲突</span>
        <span>关系 #{esc(relation_id)}</span>
      </div>
      {note_html}
      <div class="conflict-columns">
        <div>
          <h3>A：{esc(item.get('a_title', ''))}</h3>
          <p>{esc(item.get('a_content', ''))}</p>
          <small>{esc(item.get('a_confidence', '-'))} · {esc(item.get('a_source', '-'))}</small>
        </div>
        <div>
          <h3>B：{esc(item.get('b_title', ''))}</h3>
          <p>{esc(item.get('b_content', ''))}</p>
          <small>{esc(item.get('b_confidence', '-'))} · {esc(item.get('b_source', '-'))}</small>
        </div>
      </div>
      <div class="lesson-actions conflict-actions">
        {conflict_form(project, relation_id, 'keep_a', '保留 A')}
        {conflict_form(project, relation_id, 'keep_b', '保留 B')}
        {conflict_form(project, relation_id, 'keep_both', '都保留')}
        {conflict_form(project, relation_id, 'ignore_both', '都忽略', danger=True)}
      </div>
    </article>
    """


def render_conflict_cards(project: str, conflicts: list[dict]) -> str:
    return render_conflict_groups(project, conflicts)


def render_legacy_conflict_cards(project: str, conflicts: list[dict]) -> str:
    if not conflicts:
        return "<div class='empty-state'>暂无待裁决疑似冲突</div>"
    cards = []
    for item in conflicts:
        relation_id = str(item.get("relation_id", ""))
        endpoint = f"{item.get('method') or ''} {item.get('path') or ''}".strip()
        cards.append(
            f"""
            <article class="conflict-card">
              <div class="lesson-meta">
                <span>疑似冲突</span>
                <span>{esc(endpoint or '-')}</span>
                <span>{esc(item.get('environment') or '通用环境')}</span>
              </div>
              <div class="conflict-columns">
                <div>
                  <h3>A：{esc(item.get('a_title', ''))}</h3>
                  <p>{esc(item.get('a_content', ''))}</p>
                  <small>{esc(item.get('a_confidence', '-'))} · {esc(item.get('a_source', '-'))}</small>
                </div>
                <div>
                  <h3>B：{esc(item.get('b_title', ''))}</h3>
                  <p>{esc(item.get('b_content', ''))}</p>
                  <small>{esc(item.get('b_confidence', '-'))} · {esc(item.get('b_source', '-'))}</small>
                </div>
              </div>
              <div class="lesson-actions conflict-actions">
                {conflict_form(project, relation_id, 'keep_a', '保留 A')}
                {conflict_form(project, relation_id, 'keep_b', '保留 B')}
                {conflict_form(project, relation_id, 'keep_both', '都保留')}
                {conflict_form(project, relation_id, 'ignore_both', '都忽略', danger=True)}
              </div>
            </article>
            """
        )
    return "".join(cards)


def render_similar_cards(project: str, similars: list[dict]) -> str:
    if not similars:
        return "<div class='empty-state'>暂无待合并相似经验</div>"
    cards = []
    for item in similars:
        relation_id = str(item.get("relation_id", ""))
        endpoint = f"{item.get('method') or ''} {item.get('path') or ''}".strip()
        cards.append(
            f"""
            <article class="similar-card">
              <div class="lesson-meta">
                <span>similar</span>
                <span>{esc(endpoint or '-')}</span>
                <span>{esc(item.get('environment') or '通用环境')}</span>
              </div>
              <div class="conflict-columns">
                <div>
                  <h3>A：{esc(item.get('a_title', ''))}</h3>
                  <p>{esc(item.get('a_content', ''))}</p>
                  <small>{esc(item.get('a_confidence', '-'))} · {esc(item.get('a_source', '-'))}</small>
                </div>
                <div>
                  <h3>B：{esc(item.get('b_title', ''))}</h3>
                  <p>{esc(item.get('b_content', ''))}</p>
                  <small>{esc(item.get('b_confidence', '-'))} · {esc(item.get('b_source', '-'))}</small>
                </div>
              </div>
              <div class="lesson-actions conflict-actions">
                {similar_form(project, relation_id, 'merge_a', '合并到 A')}
                {similar_form(project, relation_id, 'merge_b', '合并到 B')}
                {similar_form(project, relation_id, 'keep_both', '保留两条')}
              </div>
            </article>
            """
        )
    return "".join(cards)


def conflict_form(project: str, relation_id: str, resolution: str, label: str, danger: bool = False) -> str:
    button_class = " class='danger'" if danger else ""
    return (
        "<form method='post' action='/actions/context-resolve-conflict'>"
        + hidden_project(project)
        + f"<input type='hidden' name='relation_id' value='{esc(relation_id)}'>"
        + f"<input type='hidden' name='resolution' value='{esc(resolution)}'>"
        + f"<button{button_class}>{esc(label)}</button></form>"
    )


def similar_form(project: str, relation_id: str, resolution: str, label: str) -> str:
    return (
        "<form method='post' action='/actions/context-resolve-similar'>"
        + hidden_project(project)
        + f"<input type='hidden' name='relation_id' value='{esc(relation_id)}'>"
        + f"<input type='hidden' name='resolution' value='{esc(resolution)}'>"
        + f"<button>{esc(label)}</button></form>"
    )


def render_lesson_edit_dialog(project: str, lesson: dict, dialog_id: str) -> str:
    tags = ", ".join(lesson.get("tags", []))
    confidence = str(lesson.get("confidence", "medium"))
    options = "".join(
        f"<option value='{value}' {'selected' if confidence == value else ''}>{label}</option>"
        for value, label in [("high", "高"), ("medium", "中"), ("low", "低")]
    )
    return f"""
    <dialog id="{dialog_id}" class="modal endpoint-modal" data-reset-on-close>
      <form method="post" action="/actions/context-edit" class="modal-form">
        <div class="modal-head">
          <h3>编辑上下文经验</h3>
          <button type="button" class="ghost" onclick="cancelDialog('{dialog_id}')">关闭</button>
        </div>
        {hidden_project(project)}
        <input type="hidden" name="lesson_id" value="{esc(lesson['id'])}">
        <label>标题</label>
        <input name="title" value="{esc(lesson.get('title', ''))}">
        <label>内容</label>
        <textarea name="content">{esc(lesson.get('content', ''))}</textarea>
        <label>置信度</label>
        <select name="confidence">{options}</select>
        <label>标签</label>
        <input name="tags" value="{esc(tags)}" placeholder="逗号分隔">
        <div class="modal-actions">
          <button type="button" class="ghost" onclick="cancelDialog('{dialog_id}')">取消</button>
          <button>保存</button>
        </div>
      </form>
    </dialog>
    """


def render_context_pack_panel(root: Path, project: str, pack: dict | None) -> str:
    if not pack:
        return "<div class='empty-state'>暂无 ContextPack 记录。生成用例或分析报告后会自动记录本次 AI 调用使用的上下文。</div>"
    included_ids = [str(item) for item in pack.get("included_lesson_ids", [])]
    excluded = pack.get("excluded_lesson_ids", [])
    included_lessons = lessons_by_ids(root, project, included_ids)
    included_rows = []
    for lesson_id in included_ids:
        lesson = included_lessons.get(lesson_id, {})
        title = lesson.get("title") or lesson_id
        endpoint = f"{lesson.get('method', '')} {lesson.get('path', '')}".strip()
        included_rows.append(
            f"<li><strong>{esc(lesson_id)}</strong> {esc(title)} <small>{esc(endpoint)}</small></li>"
        )
    excluded_rows = []
    for item in excluded:
        if isinstance(item, dict):
            excluded_rows.append(f"<li><strong>{esc(str(item.get('id', '')))}</strong> {esc(str(item.get('reason', '')))}</li>")
        else:
            excluded_rows.append(f"<li>{esc(str(item))}</li>")
    included_html = "<ul class='compact-list'>" + "".join(included_rows) + "</ul>" if included_rows else "<div class='empty-state'>本次没有注入 lesson。</div>"
    excluded_html = "<ul class='compact-list'>" + "".join(excluded_rows) + "</ul>" if excluded_rows else "<div class='empty-state'>没有被排除的 lesson。</div>"
    return f"""
    <div class="context-pack">
      <div class="context-pack-summary">
        <div><span>任务类型</span><strong>{esc(pack.get('task_type', '-'))}</strong></div>
        <div><span>模型</span><strong>{esc(pack.get('model', '-') or '-')}</strong></div>
        <div><span>上下文预算</span><strong>{esc(pack.get('context_budget', 0))}</strong></div>
        <div><span>已使用</span><strong>{esc(pack.get('used_tokens', 0))}</strong></div>
      </div>
      <details class="case-data">
        <summary>查看已注入经验</summary>
        {included_html}
      </details>
      <details class="case-data">
        <summary>查看未注入经验</summary>
        {excluded_html}
      </details>
      <small>Pack：{esc(pack.get('id', ''))} · {esc(pack.get('created_at', ''))}</small>
    </div>
    """


def render_audit_rows(rows: list[dict]) -> str:
    if not rows:
        return "<tr><td colspan='3'>暂无审计日志</td></tr>"
    return "".join(
        f"<tr><td>{esc(row.get('created_at', ''))}</td><td>{esc(actor_label(row.get('actor', '')))}</td><td>{esc(audit_display_label(row))}</td></tr>"
        for row in collapse_audit_rows(rows, limit=20)
    )


def collapse_audit_rows(rows: list[dict], limit: int = 20) -> list[dict]:
    collapsed: list[dict] = []
    grouped: dict[tuple, dict] = {}
    for row in rows:
        key = audit_group_key(row)
        if key and key in grouped:
            grouped[key]["_repeat_count"] = int(grouped[key].get("_repeat_count", 1)) + 1
            continue
        item = dict(row)
        item["_repeat_count"] = 1
        collapsed.append(item)
        if key:
            grouped[key] = item
        if len(collapsed) >= limit:
            break
    return collapsed


def audit_group_key(row: dict) -> tuple | None:
    action = str(row.get("action") or "")
    if not action.startswith("extraction_"):
        return None
    detail = row.get("detail") if isinstance(row.get("detail"), dict) else {}
    return (
        action,
        str(detail.get("error") or ""),
        str(detail.get("retry_count") if detail.get("retry_count") is not None else ""),
    )


def audit_display_label(row: dict) -> str:
    label = action_label(row)
    repeat_count = int(row.get("_repeat_count", 1) or 1)
    if repeat_count > 1:
        label += f"（同类记录 {repeat_count} 条，已折叠）"
    return label


def render_trend_overview(data: dict) -> str:
    runs = data.get("runs", [])
    if not runs:
        return "<div class='empty-state'>暂无趋势数据，执行用例并生成报告后会自动积累。</div>"

    # Latest first order for analysis
    ordered = list(runs)  # already desc by id
    latest = ordered[0] if ordered else {}
    previous = ordered[1] if len(ordered) > 1 else None

    latest_total = int(latest.get("total_cases", 0))
    latest_passed = int(latest.get("passed_cases", 0))
    latest_failed = int(latest.get("failed_cases", 0))
    latest_rate = float(latest.get("pass_rate", 0))

    # ── Comparability (shared logic with AI context builder) ──
    from ai_apitest.context.storage import assess_trend_comparability
    scope_label = {"smoke": "冒烟", "full": "全量"}.get(latest.get("run_scope", ""), latest.get("run_scope", "") or "")
    scope_note = f" · 范围：{scope_label}" if scope_label else ""

    if previous:
        comp = assess_trend_comparability(latest, previous)
        comparable = comp["comparable"]
        confidence = comp["confidence"]
        comparability_note = comp["note"]
    else:
        comparable = True
        confidence = "high"
        comparability_note = ""

    # ── Endpoint diffs ──
    latest_eps = {f"{e.get('method','')} {e.get('path','')}": e for e in latest.get("endpoint_metrics", [])}
    prev_eps = {} if not previous else {f"{e.get('method','')} {e.get('path','')}": e for e in previous.get("endpoint_metrics", [])}

    new_fail_eps = []      # 本次新失败的接口
    persistent_fail_eps = []  # 连续失败的接口
    recovered_eps = []     # 上次失败本次通过的接口
    fail_concentration = []  # 失败集中的接口

    for key, ep in latest_eps.items():
        failed = int(ep.get("failed", 0))
        total_ep = int(ep.get("passed", 0)) + failed
        if total_ep == 0:
            continue
        prev_ep = prev_eps.get(key)
        if failed > 0:
            fail_rate = failed / total_ep
            if fail_rate >= 0.5:
                fail_concentration.append((key, failed, total_ep))
            if prev_ep is None:
                new_fail_eps.append(key)
            elif int(prev_ep.get("failed", 0)) > 0:
                persistent_fail_eps.append(key)
        if prev_ep and int(prev_ep.get("failed", 0)) > 0 and failed == 0:
            recovered_eps.append(key)

    # ── Trend class ──
    first_rate = float(ordered[-1].get("pass_rate", latest_rate))
    total_delta = latest_rate - first_rate
    if not comparable:
        trend_class = "trend-uncertain"
        trend_label = "不可直接比较"
        summary = comparability_note + "，通过率变化可能主要来自样本差异而非质量变化。"
    elif total_delta > 0.01:
        trend_class = "trend-up"
        trend_label = "通过率上升"
        summary = "最近通过率在上升，说明接口稳定性、测试数据或用例预期正在改善。"
    elif total_delta < -0.01:
        trend_class = "trend-down"
        trend_label = "通过率下降"
        summary = "最近通过率下降，需要优先判断是接口真实回归、测试数据失效，还是用例预期不准确。"
    else:
        trend_class = "trend-stable"
        trend_label = "通过率稳定"
        summary = "最近通过率变化不大，重点应放在长期重复失败和待复核经验上。"

    # ── Insight cards ──
    confidence_label = {"high": "高", "medium": "中", "low": "低"}.get(confidence, confidence)
    confidence_badge = f"<span class='trend-confidence trend-conf-{confidence}'>{confidence_label}置信</span>"
    cards = [
        ("最新通过率", f"{latest_rate:.1%}", f"{latest_passed}/{latest_total} 通过，{latest_failed} 失败"),
        ("样本可比性", confidence_label, esc(comparability_note) if comparability_note else "两次执行规模接近"),
        ("接口覆盖", f"{len(latest_eps)} 个", f"新增失败 {len(new_fail_eps)} · 恢复 {len(recovered_eps)} · 持续失败 {len(persistent_fail_eps)}"),
    ]
    if previous:
        prev_rate = float(previous.get("pass_rate", 0))
        cards.insert(1, ("与上次比较", signed_percent(latest_rate - prev_rate), f"上次通过率 {prev_rate:.1%}"))
    insight_cards = "".join(
        f"<div><span>{esc(label)}</span><strong>{esc(value)}</strong><small>{desc}</small></div>"
        for label, value, desc in cards
    )

    # ── Risk & Suggestions (data-driven) ──
    risk_items = []
    suggestions = []

    if not comparable:
        risk_items.append(f"本次 {latest_total} 条用例与上次样本差异过大，不建议直接比较通过率。")
        suggestions.append(f"使用相同的执行范围（全量/冒烟）再执行一次，以便准确判断趋势。")

    if new_fail_eps:
        names = "、".join(f"<code>{esc(ep)}</code>" for ep in new_fail_eps[:5])
        suffix = f"等 {len(new_fail_eps)} 个" if len(new_fail_eps) > 5 else ""
        risk_items.append(f"新增失败接口：{names}{suffix}，优先确认是否真实回归。")
        suggestions.append(f"检查 {names}{suffix} 的接口文档是否变更，或环境数据是否异常。")

    if persistent_fail_eps:
        names = "、".join(f"<code>{esc(ep)}</code>" for ep in persistent_fail_eps[:3])
        suffix = f"等 {len(persistent_fail_eps)} 个" if len(persistent_fail_eps) > 3 else ""
        risk_items.append(f"持续失败接口：{names}{suffix}，建议标记为已知问题或修正前置数据。")
        suggestions.append(f"将 {names}{suffix} 的失败原因沉淀为项目上下文，避免 AI 反复生成不合预期的断言。")

    if recovered_eps:
        names = "、".join(f"<code>{esc(ep)}</code>" for ep in recovered_eps[:3])
        suffix = f"等 {len(recovered_eps)} 个" if len(recovered_eps) > 3 else ""
        risk_items.append(f"恢复通过接口：{names}{suffix}，可回顾之前失败原因是否已消除。")
        suggestions.append(f"如果 {names}{suffix} 之前是已知问题，建议执行一次验证确认解决。")

    if fail_concentration:
        for key, failed, total_ep in fail_concentration[:3]:
            risk_items.append(f"失败集中：<code>{esc(key)}</code> 失败 {failed}/{total_ep}，集中度 {failed/max(total_ep,1):.0%}。")
            suggestions.append(f"优先排查 <code>{esc(key)}</code> 的断言预期，可能是用例生成时对返回格式理解有误。")

    if latest_failed == 0 and latest_total > 0:
        risk_items.append("本次全部通过，当前无可关注风险。")
        suggestions.append("保持当前执行策略，可关注覆盖率是否有提升空间。")

    # Fallback when no endpoint_metrics (e.g. historical data)
    if not latest_eps and latest_total > 0:
        if not risk_items:
            risk_items.append(f"本次执行 {latest_total} 条用例，通过 {latest_passed} 条，失败 {latest_failed} 条。")
        if not suggestions:
            if latest_failed > 0:
                suggestions.append(f"查看最新报告中的 {latest_failed} 条失败用例，逐一确认失败原因。")
                suggestions.append("把确认后的业务规则沉淀到项目上下文，避免下次生成相同错误预期。")
            else:
                suggestions.append("保持当前执行策略，后续关注新增失败类别。")

    # ── Latency insight ──
    latency_note = ""
    pcts = latest.get("latency_percentiles", {})
    if pcts and pcts.get("p95"):
        p50 = pcts.get("p50", 0)
        p95 = pcts.get("p95", 0)
        latency_note = f"<small>响应延迟 P50={p50}ms P95={p95}ms</small>"

    risk_html = "".join(f"<li>{item}</li>" for item in risk_items[:6])
    step_html = "".join(f"<li>{item}</li>" for item in suggestions[:6])

    # ── History table ──
    rows = "".join(
        f"<tr><td>{row.get('run_id')}</td><td>{esc(row.get('created_at', ''))}</td><td>{row.get('total_cases')}</td><td>{row.get('passed_cases')}</td><td>{row.get('failed_cases')}</td><td><div class='rate-cell'><span>{float(row.get('pass_rate', 0)):.1%}</span><i style='--rate:{max(0, min(100, int(float(row.get('pass_rate', 0)) * 100)))}%'></i></div></td></tr>"
        for row in runs
    )

    return f"""
    <div class="trend-summary">
      <div>
        <span>最近 {len(runs)} 次执行</span>
        <strong>{latest_rate:.1%} {confidence_badge}</strong>
        <small>{esc(trend_label)}{esc(scope_note)} · {esc(comparability_note) if comparability_note else ''}</small>
        {latency_note}
      </div>
      <div class="trend-indicator {trend_class}" aria-label="{esc(trend_label)}"></div>
    </div>
    <div class="trend-analysis">
      <div class="trend-insight-cards">{insight_cards}</div>
      <div class="trend-advice">
        <strong>{esc(summary)}</strong>
        <div class="trend-advice-grid">
          <div><span>具体发现</span><ul>{risk_html}</ul></div>
          <div><span>建议动作</span><ul>{step_html}</ul></div>
        </div>
      </div>
    </div>
    <div class='table-wrap'><table><thead><tr><th>Run</th><th>时间</th><th>总数</th><th>通过</th><th>失败</th><th>通过率</th></tr></thead><tbody>{rows}</tbody></table></div>
    """


def signed_percent(value: float) -> str:
    return f"{value:+.1%}"


def signed_number(value: int) -> str:
    return f"{value:+d}"


def label_for(kind: str, value: object) -> str:
    text = str(value or "")
    labels = {
        "scope": {
            "project": "项目级",
            "endpoint": "接口级",
            "case": "用例级",
            "report": "报告级",
            "dependency": "依赖",
        },
        "lesson_type": {
            "business_rule": "业务规则",
            "assertion_pattern": "断言经验",
            "failure_pattern": "失败模式",
            "known_issue": "已知问题",
            "data_dependency": "数据依赖",
            "trend_insight": "趋势发现",
        },
        "lifecycle_status": {
            "pending": "待确认",
            "active": "已启用",
            "ignored": "已忽略",
            "resolved": "已解决",
            "resolved_candidate": "候选已解决",
            "reopened": "重新出现",
            "archived": "已归档",
            "merged": "已合并",
        },
        "review_status": {
            "unreviewed": "未审核",
            "confirmed": "已确认",
            "rejected": "已拒绝",
            "needs_review": "待复核",
        },
        "confidence": {
            "high": "高置信",
            "medium": "中置信",
            "low": "低置信",
        },
        "tag": {
            "case_generation": "用例生成",
            "happy_path": "正常场景",
            "optional_field": "可选参数",
            "required_field": "必填缺失",
            "type_error": "类型错误",
            "boundary_value": "边界值",
            "path_parameter_error": "路径参数异常",
            "dependency_chain": "依赖链路",
            "auth_error": "鉴权异常",
            "equivalence_class": "等价类",
            "status_code_coverage": "状态码覆盖",
            "data_dependency": "数据依赖",
            "candidate": "候选",
            "status_code": "状态码",
        },
    }
    return labels.get(kind, {}).get(text, text or "-")


def actor_label(actor: object) -> str:
    text = str(actor or "")
    if text.startswith("user"):
        return "用户操作"
    if text.startswith("ai:analyzer"):
        return "AI 报告分析"
    if text.startswith("ai:case_generator"):
        return "AI 用例生成"
    if text.startswith("rule:"):
        return "规则引擎"
    if text.startswith("web:"):
        return "Web 控制台"
    if text == "system":
        return "系统自动处理"
    return text or "-"


def action_label(row: dict) -> str:
    action = str(row.get("action") or "")
    detail = row.get("detail") if isinstance(row.get("detail"), dict) else {}
    if action == "lesson_created":
        similar = detail_count(detail, "similar")
        return f"保存新的上下文经验{f'，并发现 {similar} 条相似经验待合并' if similar else '，等待确认后可用于 AI 上下文'}"
    if action == "lesson_duplicate_merged":
        return "发现重复经验，已合并到已有经验记录"
    if action == "lesson_confirmed":
        return "确认经验，后续会参与用例生成和报告分析的 AI 上下文"
    if action == "lesson_rejected":
        return "停用经验，后续不会参与 AI 上下文"
    if action == "lesson_edited":
        return "保存经验内容修改"
    if action == "lesson_conflict_created":
        return f"发现疑似冲突经验，已加入冲突裁决列表{count_suffix(detail, 'conflicts')}"
    if action == "conflict_ai_reviewed":
        return f"AI 复核疑似冲突，结论：{decision_label(detail.get('decision'))}{reason_suffix(detail)}"
    if action == "conflict_resolved":
        return f"完成冲突裁决，处理方式：{resolution_label(detail.get('resolution'))}"
    if action == "similar_merged":
        return f"合并相似经验，处理方式：{resolution_label(detail.get('resolution'))}"
    if action == "similar_kept_separate":
        return "确认相似经验不合并，保留为独立经验"
    if action == "known_issue_resolved":
        return "确认已知问题已解决"
    if action == "known_issue_recurred":
        return f"已知问题再次出现{endpoint_suffix(detail)}，需要重新关注"
    if action == "known_issue_resolved_candidate":
        quiet_runs = detail.get("quiet_runs")
        return f"系统建议将已知问题标记为候选已解决{f'，已连续 {quiet_runs} 次未复现' if quiet_runs else ''}"
    if action == "known_issue_status_updated":
        return f"更新已知问题状态为：{lifecycle_status_label(detail.get('status'))}{reason_suffix(detail)}"
    if action == "endpoint_lessons_need_review":
        return f"接口定义发生变化，已将相关经验标记为待复核{count_suffix(detail, 'count')}"
    if action == "context_injected":
        return f"执行 {task_type_label(detail.get('task_type'))} 时注入了已确认的项目上下文"
    if action == "trend_snapshot_created":
        total = detail.get("total")
        passed = detail.get("passed")
        failed = detail.get("failed")
        return f"保存执行趋势快照{f'：共 {total} 条，通过 {passed} 条，失败 {failed} 条' if total is not None else ''}"
    if action == "failure_patterns_updated":
        return f"更新失败模式统计{count_suffix(detail, 'count')}"
    if action == "context_suspect_marked":
        return f"根据失败结果标记可疑上下文经验{count_suffix(detail, 'needs_review', '待复核')}"
    if action.startswith("extraction_"):
        return extraction_action_label(action, detail)
    if action.startswith("dependency_"):
        return dependency_action_label(action, detail)
    return "记录了一次系统操作，当前版本尚未提供更详细说明"


def detail_count(detail: dict, key: str) -> int:
    value = detail.get(key)
    if isinstance(value, list):
        return len(value)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def count_suffix(detail: dict, key: str, label: str = "数量") -> str:
    count = detail_count(detail, key)
    return f"（{label}：{count} 条）" if count else ""


def reason_suffix(detail: dict) -> str:
    reason = str(detail.get("reason") or "").strip()
    return f"；原因：{reason}" if reason else ""


def endpoint_suffix(detail: dict) -> str:
    endpoint = str(detail.get("endpoint") or "").strip()
    return f"：{endpoint}" if endpoint else ""


def decision_label(value: object) -> str:
    return {
        "conflict": "确认存在冲突",
        "not_conflict": "不是冲突，已移出裁决列表",
        "uncertain": "无法确定，保留人工裁决",
    }.get(str(value or ""), str(value or "未知"))


def resolution_label(value: object) -> str:
    return {
        "keep_a": "保留 A，忽略 B",
        "keep_b": "保留 B，忽略 A",
        "keep_both": "两条都保留，但标记为待复核",
        "ignore_both": "两条都忽略",
        "merge_a": "合并到 A",
        "merge_b": "合并到 B",
    }.get(str(value or ""), str(value or "未说明"))


def lifecycle_status_label(value: object) -> str:
    return {
        "active": "已启用",
        "resolved": "已解决",
        "resolved_candidate": "候选已解决",
        "reopened": "重新出现",
        "ignored": "已忽略",
        "archived": "已归档",
    }.get(str(value or ""), str(value or "未说明"))


def task_type_label(value: object) -> str:
    return {
        "api_discovery": "接口识别",
        "case_generation": "用例生成",
        "report_analysis": "报告分析",
        "case_maintenance": "用例维护",
    }.get(str(value or ""), str(value or "AI 任务"))


def extraction_action_label(action: str, detail: dict) -> str:
    status = action.removeprefix("extraction_")
    labels = {
        "completed": "上下文经验提取完成",
        "failed": "上下文经验提取失败",
        "permanent_failed": "上下文经验提取最终失败，已停止重试",
        "running": "上下文经验提取开始执行",
        "pending": "上下文经验提取等待重试",
    }
    text = labels.get(status, "更新上下文经验提取任务状态")
    error = str(detail.get("error") or "").strip()
    retry_count = detail.get("retry_count")
    if error:
        text += f"；错误：{error}"
    if retry_count is not None:
        text += f"；已重试 {retry_count} 次"
    return text


def dependency_action_label(action: str, detail: dict) -> str:
    result = action.removeprefix("dependency_")
    labels = {
        "verified": "验证数据依赖通过，后续可用于链路用例",
        "candidate": "记录候选数据依赖，等待验证",
        "invalidated": "数据依赖验证失败，已标记为失效",
        "failed": "数据依赖验证失败",
    }
    attempts = detail.get("attempts")
    text = labels.get(result, "更新数据依赖验证结果")
    if attempts is not None:
        text += f"；验证次数：{attempts}"
    return text + reason_suffix(detail)


def select_html(name: str, selected: str, options: list[tuple[str, str]]) -> str:
    return "<select name='" + esc(name) + "'>" + "".join(
        f"<option value='{esc(value)}' {'selected' if selected == value else ''}>{esc(label)}</option>"
        for value, label in options
    ) + "</select>"


def render_config(root: Path, project: str, parsed) -> str:
    query = parse_qs(parsed.query)
    config = load_config(root)
    llm = normalize_llm_config(config.get("llm", {}))
    generation = config.get("case_generation", {})
    execution = config.get("execution_strategies", {})
    governance = config.get("governance", {})
    message = (query.get("message") or [""])[0]
    gov_rows = (
        f"<tr><td>enabled</td>"
        f"<td>{'启用' if governance.get('enabled', True) else '停用'}</td>"
        f"<td>关闭后所有治理审查暂停（报告审查、用例审查、经验裁决、冲突复核），行为回退到原始版本</td>"
        f"<td>{_governance_toggle_form(project, 'enabled', governance.get('enabled', True))}</td></tr>"
    )
    body = render_template(
        "config.html",
        {
            "DASHBOARD_URL": url_for("/", project),
            "MESSAGE": f'<div class="notice">{esc(message)}</div>' if message else "",
            "LLM_MASTER": render_llm_role_form(project, "master", llm["master"]),
            "LLM_CASE_GENERATOR": render_llm_role_form(project, "case_generator", llm["case_generator"]),
            "LLM_ANALYZER": render_llm_role_form(project, "analyzer", llm["analyzer"]),
            "GENERATION_STRATEGIES": render_default_generation_strategies(project, generation.get("default_strategies", {})),
            "CUSTOM_GENERATION_STRATEGIES": render_custom_strategies(project, "case_generation", generation.get("custom_strategies", []), "/actions/add-custom-generation-strategy", "/actions/delete-custom-generation-strategy"),
            "CUSTOM_EXECUTION_STRATEGIES": render_custom_execution_strategies(project, execution.get("custom_strategies", [])),
            "GOVERNANCE_ROWS": gov_rows,
        },
    )
    return page(root, project, "配置中心", body)


def display_message_for_tasks(project: str, message: str, actions: list[str]) -> str:
    if not message:
        return ""
    if is_transient_task_message(message) and not any(active_task(project, action) for action in actions):
        return ""
    return message


def is_transient_task_message(message: str) -> bool:
    markers = [
        "任务已开始",
        "任务还在进行中",
        "正在后台进行",
        "接口来源已提交",
        "可关闭提示",
    ]
    return any(marker in message for marker in markers)


def render_default_generation_strategies(project: str, strategies: dict) -> str:
    names = {
        "happy_path": "正常场景",
        "optional_field": "可选参数",
        "type_error": "类型错误",
        "boundary_value": "边界值",
        "path_parameter_error": "路径参数异常",
        "dependency_chain": "依赖链路",
    }
    rows = []
    for key, label in names.items():
        enabled = strategies.get(key, True)
        rows.append(
            f"<tr><td>{label}</td><td>默认策略</td><td><form method='post' action='/actions/toggle-generation-strategy'>{hidden_project(project)}<input type='hidden' name='name' value='{key}'><label><input type='checkbox' name='enabled' {'checked' if enabled else ''} onchange='this.form.submit()'> 启用</label></form></td></tr>"
        )
    return f"<div class='table-wrap'><table><thead><tr><th>策略</th><th>类型</th><th>启用状态</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>"


def render_llm_role_form(project: str, role: str, config: dict) -> str:
    fallback = "<small>为空时复用主控模型</small>" if role != "master" else "<small>必填，作为默认降级模型</small>"
    dialog_id = f"llm-dialog-{role}"
    provider = normalize_llm_provider(config.get("provider", "openai-compatible"))
    provider_options = "".join(
        f"<option value='{value}' {'selected' if provider == value else ''}>{label}</option>"
        for value, label in [
            ("openai-compatible", "OpenAI Compatible"),
            ("anthropic", "Anthropic"),
        ]
    )
    return render_template(
        "llm_role.html",
        {
            "ROLE_LABEL": llm_role_label(role),
            "FALLBACK": fallback,
            "PROVIDER_LABEL": esc(llm_provider_label(provider)),
            "BASE_URL": esc(config.get("base_url") or "-"),
            "MODEL": esc(config.get("model") or "-"),
            "API_KEY_DISPLAY": esc(display_api_key_ref(config.get("api_key_env") or "")),
            "DIALOG_ID": dialog_id,
            "HIDDEN_PROJECT": hidden_project(project),
            "ROLE": role,
            "PROVIDER_OPTIONS": provider_options,
            "BASE_URL_VALUE": esc(config.get("base_url", "")),
            "MODEL_VALUE": esc(config.get("model", "")),
            "API_KEY_PLACEHOLDER": esc(api_key_placeholder(config.get("api_key_env") or "")),
        },
    )


def llm_role_from_form(root: Path, role: str, form: dict, existing: dict | None = None) -> dict:
    existing = existing or {}
    base_url = field(form, "base_url")
    model = field(form, "model")
    api_key_env = normalize_api_key_ref(root, role, field(form, "api_key_env") or existing.get("api_key_env", ""))
    provider = normalize_llm_provider(field(form, "provider") or "openai-compatible")
    return {
        "enabled": bool(base_url and model and api_key_env),
        "provider": provider,
        "base_url": normalize_llm_base_url(provider, base_url),
        "model": model,
        "api_key_env": api_key_env,
    }


def normalize_api_key_ref(root: Path, role: str, value: str) -> str:
    stripped = value.strip()
    if not stripped:
        return ""
    if looks_like_api_key(stripped):
        env_name = {
            "master": "AITEST_MASTER_KEY",
            "case_generator": "AITEST_CASE_GENERATOR_KEY",
            "analyzer": "AITEST_ANALYZER_KEY",
        }.get(role, "AITEST_LLM_API_KEY")
        write_env_value(root / ".env", env_name, stripped)
        return env_name
    return stripped


def write_env_value(path: Path, key: str, value: str) -> None:
    lines = []
    found = False
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[index] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def display_api_key_ref(value: str) -> str:
    value = value.strip()
    if not value:
        return "-"
    if value.startswith(("sk-", "sk_", "ak-", "ak_")) or "." in value or "-" in value or len(value) >= 32:
        return mask_secret(value)
    return value


def mask_secret(value: str) -> str:
    if len(value) <= 10:
        return "******"
    return f"{value[:4]}{'*' * max(len(value) - 8, 6)}{value[-4:]}"


def api_key_placeholder(value: str) -> str:
    value = value.strip()
    if not value:
        return "未配置 API Key"
    return f"当前：{display_api_key_ref(value)}"


def llm_provider_label(provider: str) -> str:
    return {
        "openai-compatible": "OpenAI Compatible",
        "anthropic": "Anthropic",
    }.get(provider, "OpenAI Compatible")


def llm_role_label(role: str) -> str:
    return {
        "master": "主控模型",
        "case_generator": "用例生成模型",
        "analyzer": "结果分析模型",
    }.get(role, "主控模型")


def render_custom_strategies(project: str, section: str, strategies: list, add_action: str, delete_action: str) -> str:
    rows = []
    for strategy in strategies:
        rows.append(
            f"<tr><td>{esc(strategy.get('name', ''))}</td><td>{esc(strategy.get('description', ''))}</td><td>{'启用' if strategy.get('enabled', True) else '停用'}</td><td><form method='post' action='{delete_action}' onsubmit=\"return confirm('确认删除该策略？')\">{hidden_project(project)}<input type='hidden' name='name' value='{esc(strategy.get('name', ''))}'><button class='danger'>删除</button></form></td></tr>"
        )
    return f"""
    <h3>自定义策略</h3>
    <div class='table-wrap'><table><thead><tr><th>策略名称</th><th>描述</th><th>状态</th><th>操作</th></tr></thead><tbody>{''.join(rows) or '<tr><td colspan="4">暂无自定义策略</td></tr>'}</tbody></table></div>
    <form method="post" action="{add_action}" class="inline-form">
      {hidden_project(project)}
      <input name="name" placeholder="策略名称">
      <input name="description" placeholder="策略描述">
      <label><input type="checkbox" name="enabled" checked> 启用</label>
      <button>新增自定义策略</button>
    </form>
    """


def render_custom_execution_strategies(project: str, strategies: list) -> str:
    rows = []
    for strategy in strategies:
        rows.append(
            f"<tr><td>{esc(strategy.get('name', ''))}</td><td>{esc(strategy.get('description', ''))}</td><td><form method='post' action='/actions/delete-execution-strategy' onsubmit=\"return confirm('确认删除该策略？')\">{hidden_project(project)}<input type='hidden' name='name' value='{esc(strategy.get('name', ''))}'><button class='danger'>删除</button></form></td></tr>"
        )
    return f"""
    <h3>自定义策略</h3>
    <div class='table-wrap'><table><thead><tr><th>策略名称</th><th>描述</th><th>操作</th></tr></thead><tbody>{''.join(rows) or '<tr><td colspan="3">暂无自定义策略</td></tr>'}</tbody></table></div>
    <form method="post" action="/actions/add-execution-strategy" class="inline-form">
      {hidden_project(project)}
      <input name="name" placeholder="策略名称">
      <input name="description" placeholder="策略描述">
      <button>新增自定义策略</button>
    </form>
    """


_GOVERNANCE_TOGGLE_KEYS = {"enabled"}


def _governance_toggle_form(project: str, key: str, current: bool) -> str:
    label = "停用" if current else "启用"
    value = "off" if current else "on"
    return (
        f"<form method='post' action='/actions/governance-toggle' style='display:inline;margin:0'>"
        f"{hidden_project(project)}<input type='hidden' name='key' value='{esc(key)}'>"
        f"<input type='hidden' name='value' value='{esc(value)}'>"
        f"<button class='ghost' style='min-height:28px;font-size:12px'>{esc(label)}</button></form>"
    )


def execution_mode_options(custom_strategies: list) -> str:
    options = [
        '<option value="smoke">执行冒烟用例</option>',
        '<option value="full">执行全量用例</option>',
    ]
    for strategy in custom_strategies:
        name = str(strategy.get("name", "")).strip()
        if name:
            options.append(f'<option value="custom:{esc(name)}">{esc(name)}</option>')
    return "".join(options)


def load_endpoints(root: Path, project: str) -> list[Endpoint]:
    return [endpoint_from_dict(item) for item in read_json(project_docs_file(root, project), [])]


def endpoint_key_value(endpoint: Endpoint) -> str:
    return f"{endpoint.method.upper()} {endpoint.path}"


def selected_supplement_endpoints(endpoints: list[Endpoint], key: str) -> list[Endpoint]:
    key = key.strip()
    if not key:
        return endpoints
    selected = [endpoint for endpoint in endpoints if endpoint_key_value(endpoint) == key]
    return selected or endpoints


def cases_for_endpoints(cases: list, endpoints: list[Endpoint]) -> list:
    if not endpoints:
        return cases
    keys = {(endpoint.method.upper(), endpoint.path) for endpoint in endpoints}
    key_texts = {f"{method} {path}" for method, path in keys}
    return [
        case
        for case in cases
        if (case.method.upper(), case.path) in keys
        or any(key in str(case.source) for key in key_texts)
        or any((step.method.upper(), step.path) in keys for step in case.steps)
    ]


def save_endpoints(root: Path, project: str, endpoints: list[Endpoint]) -> None:
    write_json(project_docs_file(root, project), to_jsonable(endpoints))


def endpoint_from_form(root: Path, project: str, form: dict) -> Endpoint:
    parameters = parse_json_field(field(form, "parameters"), [])
    request_body = request_body_from_form(root, project, form)
    return Endpoint(
        method=field(form, "method").upper() or "GET",
        path=field(form, "path") or "/",
        summary=field(form, "summary"),
        parameters=parameters,
        request_body=request_body,
        responses={"200": {"description": "OK"}},
        tags=[safe_project_name(field(form, "summary") or "api")],
    )


def request_body_from_form(root: Path, project: str, form: dict) -> dict | None:
    mode = field(form, "body_mode")
    schema = parse_json_field(field(form, "request_body"), None)
    file_path = field(form, "body_file")
    if not mode:
        return None
    if mode == "file":
        uploaded_name = field(form, "body_file_name").strip()
        uploaded_data = field(form, "body_file_data")
        if uploaded_name and uploaded_data:
            file_path = str(save_request_body_file(root, project, uploaded_name, uploaded_data))
        return {"mode": "file", "file_path": file_path} if file_path else {"mode": "file"}
    if mode in {"json", "form"}:
        media_type = "application/json" if mode == "json" else "application/x-www-form-urlencoded"
        return {
            "mode": mode,
            "content": {
                media_type: {
                    "schema": schema or {"type": "object", "properties": {}},
                }
            },
        }
    return None


def save_request_body_file(root: Path, project: str, filename: str, data_url: str) -> Path:
    safe_name = safe_project_name(Path(filename).name) or "request-body-file"
    directory = project_docs_dir(root, project) / "body_files"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / safe_name
    if "," in data_url and data_url.startswith("data:"):
        payload = data_url.split(",", 1)[1]
        path.write_bytes(base64.b64decode(payload))
    else:
        path.write_text(data_url, encoding="utf-8")
    return path


def parse_json_field(value: str, default):
    if not value.strip():
        return default
    return json.loads(value)


def add_custom_strategy(root: Path, section: str, form: dict) -> None:
    config = load_config(root)
    bucket = config.setdefault(section, {}).setdefault("custom_strategies", [])
    name = field(form, "name").strip()
    if name:
        bucket.append({"name": name, "description": field(form, "description"), "enabled": field(form, "enabled") == "on"})
    save_config(root, config)


def add_custom_execution_strategy(root: Path, form: dict) -> None:
    config = load_config(root)
    bucket = config.setdefault("execution_strategies", {}).setdefault("custom_strategies", [])
    name = field(form, "name").strip()
    if name:
        bucket.append({"name": name, "description": field(form, "description"), "enabled": True})
    save_config(root, config)


def delete_custom_strategy(root: Path, section: str, name: str) -> None:
    config = load_config(root)
    bucket = config.setdefault(section, {}).setdefault("custom_strategies", [])
    config[section]["custom_strategies"] = [item for item in bucket if item.get("name") != name]
    save_config(root, config)


def parse_report_summary(path: Path) -> dict[str, str | int]:
    content = path.read_text(encoding="utf-8")
    title = path.stem
    first_heading = re.search(r"^#\s+(.+)$", content, flags=re.M)
    if first_heading:
        title = first_heading.group(1).strip()
    time_match = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", title)
    report_time = time_match.group(1) if time_match else "-"
    total = extract_metric(content, "执行用例数")
    passed = extract_metric(content, "通过")
    success_rate = f"{(passed / total * 100):.1f}%" if total else "-"
    return {"title": title, "time": report_time, "total": total, "success_rate": success_rate}


def extract_metric(content: str, name: str) -> int:
    match = re.search(rf"\|\s*{re.escape(name)}\s*\|\s*(\d+)", content)
    return int(match.group(1)) if match else 0


def markdown_to_html(content: str) -> str:
    lines = content.splitlines()
    output: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if line.startswith("# "):
            output.append(f"<h1>{inline_md(line[2:].strip())}</h1>")
        elif line.startswith("## "):
            title = line[3:].strip()
            css_class = " class='ai-analysis-title'" if title.startswith(("AI ", "AI测试", "AI 测试", "大模型")) else ""
            output.append(f"<h2{css_class}>{inline_md(title)}</h2>")
        elif line.startswith("### "):
            output.append(f"<h3>{inline_md(line[4:].strip())}</h3>")
        elif line.startswith("#### "):
            output.append(f"<h4>{inline_md(line[5:].strip())}</h4>")
        elif line.startswith("```"):
            language = line[3:].strip()
            code_lines = []
            index += 1
            while index < len(lines) and not lines[index].startswith("```"):
                code_lines.append(lines[index])
                index += 1
            output.append(f"<pre class='code-block report-code {esc(language)}'><code>{esc(chr(10).join(code_lines))}</code></pre>")
        elif line.strip() == "<details>":
            output.append("<details class='case-data'>")
        elif line.strip() == "</details>":
            output.append("</details>")
        elif line.startswith("<summary>") and line.endswith("</summary>"):
            summary = line.removeprefix("<summary>").removesuffix("</summary>")
            output.append(f"<summary>{inline_md(summary)}</summary>")
        elif line.startswith("|") and index + 1 < len(lines) and set(lines[index + 1].replace("|", "").strip()) <= {"-", " "}:
            table_lines = [line]
            index += 2
            while index < len(lines) and lines[index].startswith("|"):
                table_lines.append(lines[index])
                index += 1
            output.append(render_markdown_table(table_lines))
            continue
        elif line.startswith("- "):
            items = []
            while index < len(lines) and lines[index].startswith("- "):
                items.append(f"<li>{inline_md(lines[index][2:].strip())}</li>")
                index += 1
            output.append(f"<ul>{''.join(items)}</ul>")
            continue
        elif line.startswith("> "):
            quote_lines = []
            while index < len(lines) and lines[index].startswith("> "):
                quote_lines.append(inline_md(lines[index][2:].strip()))
                index += 1
            output.append(f"<blockquote>{'<br>'.join(quote_lines)}</blockquote>")
            continue
        else:
            output.append(f"<p>{inline_md(line.strip())}</p>")
        index += 1
    return "\n".join(output)


def render_markdown_table(lines: list[str]) -> str:
    rows = [split_table_row(line) for line in lines]
    if not rows:
        return ""
    header = "".join(f"<th>{inline_md(cell)}</th>" for cell in rows[0])
    body_rows = []
    for row in rows[1:]:
        body_rows.append("<tr>" + "".join(f"<td>{inline_md(cell)}</td>" for cell in row) + "</tr>")
    return f"<div class='table-wrap'><table><thead><tr>{header}</tr></thead><tbody>{''.join(body_rows)}</tbody></table></div>"


def split_table_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def method_select(current: str) -> str:
    options = "".join(f"<option value='{method}' {'selected' if method == current else ''}>{method}</option>" for method in ["GET", "POST", "PUT", "DELETE"])
    return f"<select name='method'>{options}</select>"


def request_body_fields(request_body) -> str:
    mode = request_body_mode(request_body)
    file_path = request_body_file_path(request_body)
    schema = request_body_schema(request_body)
    schema_text = json.dumps(schema, ensure_ascii=False, indent=2) if schema else ""
    return f"""
    <div class="body-editor" data-body-editor>
      <label>请求体类型</label>
      <select name="body_mode" data-body-mode>
        <option value="" {'selected' if mode == '' else ''}>无请求体</option>
        <option value="json" {'selected' if mode == 'json' else ''}>JSON</option>
        <option value="form" {'selected' if mode == 'form' else ''}>表单</option>
        <option value="file" {'selected' if mode == 'file' else ''}>文件路径</option>
      </select>
      <div data-body-section="schema">
        <label>字段结构 / 示例</label>
        <textarea class="body-textarea" name="request_body" placeholder='{{"type":"object","properties":{{"name":{{"type":"string","example":"demo"}}}}}}'>{esc(schema_text)}</textarea>
      </div>
      <div data-body-section="file">
        <label>文件路径</label>
        <input name="body_file" placeholder="/path/to/file" value="{esc(file_path)}">
        <label>选择本地文件</label>
        <input type="file" data-body-file>
        <input type="hidden" name="body_file_name">
        <input type="hidden" name="body_file_data">
      </div>
      <small class="body-help">用于生成请求 Body、负向用例和执行时的默认请求内容</small>
    </div>
    """


def request_body_mode(request_body) -> str:
    data = request_body if isinstance(request_body, dict) else {}
    mode = str(data.get("mode", ""))
    if mode in {"json", "form", "file"}:
        return mode
    content = data.get("content") if isinstance(data, dict) else {}
    if not isinstance(content, dict) or not content:
        return ""
    if "application/json" in content:
        return "json"
    if any("form" in str(media_type) for media_type in content):
        return "form"
    return "json"


def request_body_schema(request_body):
    data = request_body if isinstance(request_body, dict) else {}
    if not isinstance(data, dict):
        return None
    if data.get("schema") is not None:
        return data.get("schema")
    content = data.get("content")
    if isinstance(content, dict) and content:
        media = content.get("application/json") or content.get("application/x-www-form-urlencoded") or next(iter(content.values()), {})
        if isinstance(media, dict):
            return media.get("schema")
    return None


def request_body_file_path(request_body) -> str:
    data = request_body if isinstance(request_body, dict) else {}
    return str(data.get("file_path", "")) if isinstance(data, dict) else ""


def page(root: Path, project: str, title: str, body: str) -> str:
    is_dashboard = title == "仪表盘"
    shell_class = "app-shell dashboard-shell" if is_dashboard else "app-shell"
    sidebar = "" if is_dashboard else f"""
    <aside class="sidebar">
      <div class="brand">
        <div class="brand-mark">AT</div>
        <div>
          <strong>AI 接口自动化</strong>
          <span>本地测试工作台</span>
        </div>
      </div>
      <nav class="nav">{nav_links(project, title)}</nav>
      <div class="sidebar-footer">
        <small>v0.1.0 · 项目：{esc(project)}</small>
      </div>
    </aside>
    """
    return render_template(
        "base.html",
        {
            "TITLE": esc(title),
            "PROJECT": esc(project),
            "SHELL_CLASS": shell_class,
            "SIDEBAR": sidebar,
            "PROJECT_OPTIONS": project_options(root, project),
            "PROJECT_DIALOG": render_project_dialog(project),
            "BODY": body,
        },
    )

def nav_links(project: str, title: str) -> str:
    items = [("/", "仪表盘"), ("/interfaces", "接口信息"), ("/cases", "用例信息"), ("/reports", "测试报告"), ("/context", "项目上下文"), ("/config", "配置中心")]
    aliases = {"接口信息": "接口信息", "用例信息": "用例信息", "测试报告": "测试报告", "项目上下文": "项目上下文", "配置中心": "配置中心", "仪表盘": "仪表盘"}
    return "".join(
        f'<a class="{"active" if aliases.get(label, label) == title else ""}" href="{url_for(path, project)}">{label}</a>'
        for path, label in items
    )


def project_options(root: Path, current: str) -> str:
    names = list_projects(root)
    if current not in names:
        names = [current, *names]
    return "".join(f"<option value='{esc(name)}' {'selected' if name == current else ''}>{esc(name)}</option>" for name in names)


def render_project_dialog(project: str) -> str:
    return f"""
    <dialog id="project-create-dialog" class="modal project-modal" data-reset-on-close>
      <form method="post" action="/actions/create-project" class="modal-form">
        <div class="modal-head">
          <h3>新建项目</h3>
          <button type="button" class="ghost" onclick="cancelDialog('project-create-dialog')">关闭</button>
        </div>
        {hidden_project(project)}
        <label>项目名称</label>
        <input name="name" placeholder="例如 tempbox、order-service、crm-api" required>
        <small>创建后会在 projects 目录下生成独立的接口、用例、报告和上下文数据。</small>
        <div class="modal-actions">
          <button type="button" class="ghost" onclick="cancelDialog('project-create-dialog')">取消</button>
          <button>创建并切换</button>
        </div>
      </form>
    </dialog>
    """


def active_project(parsed, default: str) -> str:
    query = parse_qs(parsed.query)
    return safe_project_name((query.get("project") or [default])[0])


def url_for(path: str, project: str, extra: dict | None = None) -> str:
    params = {"project": project}
    if extra:
        params.update({key: str(value) for key, value in extra.items()})
    return f"{path}?{urlencode(params)}"


def hidden_project(project: str) -> str:
    return f'<input type="hidden" name="project" value="{esc(project)}">'


def is_safe_report_name(name: str) -> bool:
    return bool(re.fullmatch(r"report-\d{8}-\d{6}\.md", name))


def field(form: dict, name: str) -> str:
    return (form.get(name) or [""])[0]


def bounded_int(value: str, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def inline_md(text: str) -> str:
    escaped = esc(text)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", escaped)
    return escaped


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)
