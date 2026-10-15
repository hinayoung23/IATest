from __future__ import annotations

import html
import json
import base64
import hmac
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from ai_apitest.ai.llm_config import normalize_llm_base_url, normalize_llm_config, normalize_llm_provider, normalize_llm_role, resolve_llm_role
from ai_apitest.ai.provider import llm_check, looks_like_api_key
from ai_apitest.ai.workflows import apply_ai_discovery_if_configured, build_ai_report_bundle, case_quality_gate_issues, generate_cases_with_config, supplement_cases_with_config
from ai_apitest.context.dependencies import synchronize_project_dependencies
from ai_apitest.context.extraction import retry_due_report_extractions
from ai_apitest.context.injection import aggregate_prompt_candidates
from ai_apitest.data.agent_runs import AgentRunRepository
from ai_apitest.data.quality_audits import QualityAuditRepository
from ai_apitest.context.storage import (
    count_lessons,
    conflict_pairs,
    context_stats as load_context_stats,
    edit_lesson,
    latest_context_pack,
    lessons_by_ids,
    list_lessons,
    mark_lessons_for_endpoint_changes,
    prune_context_history,
    recent_audit_logs,
    resolve_conflict,
    resolve_known_issue,
    resolve_similar,
    similar_pairs,
    trend_overview,
    update_lesson_review,
)
from ai_apitest.core.config import init_project, load_config, save_config
from ai_apitest.core.case_schema import EXTERNAL_TEST_CAPABILITY_LABELS, external_test_capability_summary
from ai_apitest.core.artifact_language import set_current_artifact_language
from ai_apitest.core.files import atomic_write_bytes, atomic_write_text
from ai_apitest.core.locking import project_operation_lock
from ai_apitest.core.models import Endpoint
from ai_apitest.core.observability import record_operational_error
from ai_apitest.core.paths import (
    PROJECT_CONFIG,
    list_projects,
    project_cases_file,
    project_docs_dir,
    project_docs_file,
    project_metadata_file,
    project_reports_dir,
    project_source_snapshot_file,
    safe_project_name,
)
from ai_apitest.core.serde import case_from_dict, endpoint_from_dict, read_json, to_jsonable, write_json
from ai_apitest.domain.case_drafts import validate_compiled_cases
from ai_apitest.discovery.document import load_source_text, parse_api_document, load_and_parse_source
from ai_apitest.execution.fixtures import (
    case_has_configured_fixtures,
    case_is_executable,
    configured_fixture_requirements,
    validate_fixture_providers,
)
from ai_apitest.execution.runner import run_cases
from ai_apitest.generator.cases import load_cases_from_markdown, save_cases
from ai_apitest.mock_server import ReusableThreadingHTTPServer, build_mock_server, validate_mock_routes
from ai_apitest.orchestrator.maintainer.change_detector import diff_endpoints
from ai_apitest.report.markdown import is_report_filename, write_report
from ai_apitest.report.storage import latest_case_results, latest_results
from ai_apitest.web.i18n import (
    current_html_lang,
    current_locale,
    language_options,
    locale_from_request,
    localize_html,
    localize_task_payload,
    set_current_locale,
    translate_format,
    translate_text,
)
from ai_apitest.web.task_store import (
    active_task as load_active_task,
    claim_task,
    create_task,
    heartbeat_task,
    request_cancel,
    cancellation_requested,
    list_tasks,
    queued_tasks,
    recover_stale_tasks,
    update_task as persist_task_update,
)

WEB_DIR = Path(__file__).parent
TEMPLATE_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"
_TASK_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="iatest-web-task")
_SCHEDULED_TASKS: set[str] = set()
_SCHEDULED_TASKS_LOCK = threading.Lock()
_STARTED_WORKERS: set[tuple[str, str]] = set()
_STARTED_WORKERS_LOCK = threading.Lock()
_WEB_MOCK_SERVERS: dict[str, tuple[ReusableThreadingHTTPServer, threading.Thread]] = {}
_WEB_MOCK_SERVERS_LOCK = threading.Lock()
_CSRF_TOKEN = secrets.token_urlsafe(32)


def origin_matches_host(origin: str, host: str) -> bool:
    """Accept the exact origin plus equivalent loopback aliases."""
    try:
        parsed_origin = urlparse(origin)
        parsed_host = urlparse("//" + host)
        if parsed_origin.scheme not in {"http", "https"} or not parsed_origin.hostname or not parsed_host.hostname:
            return False
        if parsed_origin.netloc.casefold() == host.casefold():
            return True
        loopbacks = {"localhost", "127.0.0.1", "::1"}
        origin_port = parsed_origin.port or (443 if parsed_origin.scheme == "https" else 80)
        host_port = parsed_host.port or origin_port
        return (
            parsed_origin.hostname.casefold() in loopbacks
            and parsed_host.hostname.casefold() in loopbacks
            and origin_port == host_port
        )
    except ValueError:
        return False


def protect_post_forms(content: str) -> str:
    hidden = f'<input type="hidden" name="_csrf" value="{html.escape(_CSRF_TOKEN, quote=True)}">'
    pattern = re.compile(r"(<form\b[^>]*\bmethod\s*=\s*(['\"])post\2[^>]*>)", re.IGNORECASE)
    return pattern.sub(lambda match: match.group(1) + hidden, content)


def serve(root: Path, host: str = "127.0.0.1", port: int = 8765, project: str = "default") -> None:
    if host.strip().lower() not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Web 控制台包含项目写操作，只允许绑定本机回环地址；远程访问请使用带认证的反向代理")
    project = safe_project_name(project)
    for known_project in sorted(set(list_projects(root)) | {project}):
        recover_stale_tasks(root, known_project)
    start_context_retry_worker(root, project)
    start_web_task_worker(root, project)
    handler = make_handler(root, project)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"本地 Web 控制台已启动: http://{host}:{port}")
    try:
        server.serve_forever()
    finally:
        stop_all_web_mock_services()
        server.server_close()


def _web_mock_key(root: Path) -> str:
    return str(root.resolve())


def web_mock_status(root: Path) -> dict[str, Any]:
    key = _web_mock_key(root)
    with _WEB_MOCK_SERVERS_LOCK:
        entry = _WEB_MOCK_SERVERS.get(key)
        if entry is None:
            return {"running": False}
        server, thread = entry
        if not thread.is_alive():
            _WEB_MOCK_SERVERS.pop(key, None)
            server.server_close()
            return {"running": False}
        address, port = server.server_address[:2]
        return {
            "running": True,
            "host": str(address),
            "port": int(port),
            "route_count": len(server.mock_state.routes),  # type: ignore[attr-defined]
        }


def start_web_mock_service(root: Path) -> dict[str, Any]:
    key = _web_mock_key(root)
    with _WEB_MOCK_SERVERS_LOCK:
        existing = _WEB_MOCK_SERVERS.get(key)
        if existing is not None and existing[1].is_alive():
            server = existing[0]
        else:
            if existing is not None:
                existing[0].server_close()
            server = build_mock_server(load_config(root))
            ready = threading.Event()

            def serve_mock() -> None:
                ready.set()
                server.serve_forever(poll_interval=0.1)

            thread = threading.Thread(
                target=serve_mock,
                name="iatest-web-mock",
                daemon=True,
            )
            _WEB_MOCK_SERVERS[key] = (server, thread)
            try:
                thread.start()
                if not ready.wait(timeout=2) or not thread.is_alive():
                    raise RuntimeError("接口 Mock 监听线程未能进入运行状态")
            except Exception:
                _WEB_MOCK_SERVERS.pop(key, None)
                server.server_close()
                raise
        address, port = server.server_address[:2]
        return {
            "running": True,
            "host": str(address),
            "port": int(port),
            "route_count": len(server.mock_state.routes),  # type: ignore[attr-defined]
        }


def stop_web_mock_service(root: Path) -> bool:
    key = _web_mock_key(root)
    with _WEB_MOCK_SERVERS_LOCK:
        entry = _WEB_MOCK_SERVERS.pop(key, None)
    if entry is None:
        return False
    server, thread = entry
    if thread.is_alive():
        server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    return True


def stop_all_web_mock_services() -> None:
    with _WEB_MOCK_SERVERS_LOCK:
        roots = list(_WEB_MOCK_SERVERS)
    for root_key in roots:
        stop_web_mock_service(Path(root_key))


def start_context_retry_worker(root: Path, project: str) -> None:
    worker_key = (str(root.resolve()), "context-retry")
    with _STARTED_WORKERS_LOCK:
        if worker_key in _STARTED_WORKERS:
            return
        _STARTED_WORKERS.add(worker_key)

    def worker() -> None:
        while True:
            for known_project in sorted(set(list_projects(root)) | {project}):
                try:
                    retry_due_report_extractions(root, known_project, limit=5)
                    prune_context_history(root, known_project)
                except Exception as exc:
                    record_operational_error(
                        root,
                        known_project,
                        component="context_extraction",
                        operation="background_retry_worker",
                        error=exc,
                    )
            time.sleep(60)

    thread = threading.Thread(target=worker, name=f"iatest-{project}-context-retry", daemon=True)
    thread.start()


def start_web_task_worker(root: Path, project: str) -> None:
    worker_key = (str(root.resolve()), "web-task")
    with _STARTED_WORKERS_LOCK:
        if worker_key in _STARTED_WORKERS:
            return
        _STARTED_WORKERS.add(worker_key)

    def worker() -> None:
        while True:
            for known_project in sorted(set(list_projects(root)) | {project}):
                try:
                    tasks = queued_tasks(root, known_project, limit=20)
                except Exception as exc:
                    record_operational_error(
                        root,
                        known_project,
                        component="web_task",
                        operation="poll_queue",
                        error=exc,
                    )
                    continue
                for task in tasks:
                    submit_web_task(root, known_project, str(task["id"]))
            time.sleep(1)

    threading.Thread(target=worker, name="iatest-web-task-worker", daemon=True).start()


def make_handler(root: Path, default_project: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def prepare_locale(self, parsed) -> None:
            self.web_locale = set_current_locale(
                locale_from_request(parsed.query, self.headers.get("Cookie", ""))
            )
            set_current_artifact_language(self.web_locale)
            self.persist_locale = bool((parse_qs(parsed.query).get("lang") or [""])[0])

        def send_locale_cookie(self) -> None:
            if getattr(self, "persist_locale", False):
                self.send_header(
                    "Set-Cookie",
                    f"iatest_lang={self.web_locale}; Path=/; Max-Age=31536000; SameSite=Lax",
                )

        def end_headers(self) -> None:
            buffered = b"".join(getattr(self, "_headers_buffer", [])).lower()
            if b"x-content-type-options:" not in buffered:
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:")
            super().end_headers()

        def do_HEAD(self) -> None:
            parsed = urlparse(self.path)
            self.prepare_locale(parsed)
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
                self.send_locale_cookie()
                self.end_headers()
                return
            self.send_error(404)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            self.prepare_locale(parsed)
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
                self.respond_json({"tasks": localize_task_payload(project_tasks(root, project))})
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
            if parsed.path.startswith("/quality-audit/"):
                session_id = parsed.path.rsplit("/", 1)[-1]
                if re.fullmatch(r"quality-[a-f0-9]{20}", session_id):
                    content = render_quality_audit_detail(root, project, session_id)
                    if content is not None:
                        self.respond_html(content)
                        return
                self.send_error(404)
                return
            try:
                self.respond_html(route_get(root, project, parsed))
            except KeyError:
                self.send_error(404)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            self.prepare_locale(parsed)
            origin = self.headers.get("Origin", "")
            host = self.headers.get("Host", "")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.respond_error(400, "Content-Length 无效")
                return
            max_request_bytes = int(load_config(root).get("web", {}).get("max_request_bytes", 20_000_000))
            if length < 0 or length > max_request_bytes:
                self.respond_error(413, "请求体超过 Web 控制台限制")
                return
            try:
                payload = self.rfile.read(length).decode("utf-8")
            except UnicodeDecodeError:
                self.respond_error(400, "请求体必须使用 UTF-8 编码")
                return
            form = parse_qs(payload)
            csrf_token = field(form, "_csrf")
            if origin and not origin_matches_host(origin, host) and not hmac.compare_digest(csrf_token, _CSRF_TOKEN):
                self.respond_error(403, "跨站请求已拒绝，请刷新控制台页面后重试")
                return
            try:
                project = safe_project_name(field(form, "project") or default_project)
            except ValueError as exc:
                self.respond_error(400, str(exc))
                return
            try:
                if parsed.path == "/actions/cancel-task":
                    handle_action(root, project, parsed.path, form, self)
                    return
                with project_operation_lock(root, project, operation=f"web:{parsed.path}"):
                    handle_action(root, project, parsed.path, form, self)
            except Exception as exc:
                record_operational_error(
                    root,
                    project,
                    component="web",
                    operation=parsed.path,
                    error=exc,
                )
                self.respond_html(page(root, project, "操作失败", f"<section><h2>操作失败</h2><pre>{esc(exc)}</pre></section>"), status=500)

        def redirect(self, location: str) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.send_locale_cookie()
            self.send_security_headers()
            self.end_headers()

        def respond_html(self, content: str, status: int = 200) -> None:
            data = content.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_locale_cookie()
            self.send_security_headers()
            self.end_headers()
            self.wfile.write(data)

        def respond_error(self, status: int, message: str) -> None:
            body = (
                f"<!doctype html><html lang='{current_html_lang()}'><head><meta charset='utf-8'>"
                f"<title>请求失败</title></head><body><h1>请求失败（{int(status)}）</h1>"
                f"<p>{html.escape(str(message))}</p></body></html>"
            )
            self.respond_html(localize_html(body), status=status)

        def respond_json(self, payload: dict, status: int = 200) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_locale_cookie()
            self.send_security_headers()
            self.end_headers()
            self.wfile.write(data)

        def send_security_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")

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


def project_tasks(root: Path, project: str) -> list[dict]:
    return list_tasks(root, project)


def active_task(root: Path, project: str, action: str) -> dict | None:
    return load_active_task(root, project, action)


def start_task(
    root: Path,
    project: str,
    action: str,
    label: str,
    target=None,
    *,
    kind: str = "",
    payload: dict | None = None,
    resumable: bool = True,
) -> bool:
    task_payload = dict(payload or {})
    task_payload.setdefault("artifact_language", current_locale())
    task = create_task(
        root,
        project,
        action,
        label,
        kind=kind or action,
        payload=task_payload,
        resumable=resumable,
    )
    if not task:
        return False
    task_id = str(task["id"])
    submit_web_task(root, project, task_id, target)
    return True


def submit_web_task(root: Path, project: str, task_id: str, target=None) -> bool:
    key = f"{root.resolve()}:{project}:{task_id}"
    with _SCHEDULED_TASKS_LOCK:
        if key in _SCHEDULED_TASKS:
            return False
        _SCHEDULED_TASKS.add(key)

    def complete(_future) -> None:
        with _SCHEDULED_TASKS_LOCK:
            _SCHEDULED_TASKS.discard(key)

    future = _TASK_EXECUTOR.submit(run_task_by_id, root, project, task_id, target)
    future.add_done_callback(complete)
    return True


def run_task_by_id(root: Path, project: str, task_id: str, target=None) -> None:
    from ai_apitest.execution.control import RunControl, RunCancelled, ExecutionRuntime, activate
    task = claim_task(root, project, task_id)
    if not task:
        return
    action = str(task["action"])
    stop_heartbeat = threading.Event()
    control = RunControl(cancellation_check=lambda: cancellation_requested(root, project, task_id))

    def heartbeat() -> None:
        while not stop_heartbeat.wait(5):
            heartbeat_task(root, project, task_id)

    def progress(detail: str | None = None, **updates) -> None:
        control.check()
        if detail is not None:
            updates["detail"] = detail
        persist_task_update(root, project, task_id, **updates)

    threading.Thread(target=heartbeat, name=f"iatest-{project}-{action}-heartbeat", daemon=True).start()
    try:
        with activate(ExecutionRuntime(control=control)), project_operation_lock(root, project, operation=f"web-task:{action}"):
            control.check()
            message = target(progress) if target else execute_web_task(root, project, str(task["kind"]), task.get("payload") or {}, progress)
            control.check()
        persist_task_update(root, project, task_id, status="completed", stage="complete", detail="任务已完成", message=message or "")
    except RunCancelled:
        persist_task_update(root, project, task_id, status="cancelled", stage="cancelled", detail="任务已取消", message="已停止执行并完成资源清理")
    except Exception as exc:
        record_operational_error(
            root,
            project,
            component="web_task",
            operation=action,
            error=exc,
            detail={"task_id": task_id, "kind": task.get("kind", "")},
        )
        # Preserve the last real stage (read/prepare/llm/save/execute/report) so the
        # progress UI marks the operation that failed instead of falling back to queued.
        persist_task_update(root, project, task_id, status="failed", detail="任务执行失败", message=str(exc))
    finally:
        stop_heartbeat.set()


def execute_web_task(root: Path, project: str, kind: str, payload: dict, progress) -> str:
    artifact_language = set_current_artifact_language(
        payload.get("artifact_language") or "en"
    )

    def stream_progress(text: str) -> None:
        updates: dict[str, object] = {"stream_delta": text}
        if "质量审计" in str(text):
            updates.update(stage="audit", detail="正在执行多模型交付物质量审计")
        progress(**updates)

    if kind == "discover":
        source = str(payload.get("source") or "").strip()
        source_name = str(payload.get("source_name") or "").strip()
        old_endpoints = load_endpoints(root, project)
        endpoints, metadata = discover_source(
            root,
            project,
            source,
            source_name=source_name,
            progress=progress,
            stream_callback=stream_progress,
        )
        progress("保存接口识别结果", stage="save")
        write_json(project_docs_file(root, project), to_jsonable(endpoints))
        write_json(project_metadata_file(root, project), metadata)
        handle_endpoint_context_changes(root, project, old_endpoints, endpoints, actor="web:discover")
        synchronize_project_dependencies(root, project, old_endpoints, endpoints, actor="web:discover")
        return f"已识别接口 {len(endpoints)} 个"

    if kind == "generate":
        progress("读取接口定义", stage="read")
        endpoints = load_endpoints(root, project)
        progress("准备用例生成策略和大模型 Prompt", stage="prepare")
        progress("正在调用大模型生成用例；如果模型响应较慢，可能需要等待较久", stage="llm")
        cases, warning = generate_cases_with_config(
            root,
            endpoints,
            project=project,
            stream_callback=stream_progress,
            force_refresh=bool(payload.get("force", True)),
            artifact_language=artifact_language,
        )
        progress("解析并保存生成的测试用例", stage="save")
        if not cases:
            raise ValueError("用例生成结果为空，已保留现有用例文件")
        save_cases(root, cases, project=project)
        suffix = f"；{warning}" if warning else ""
        return f"已生成用例 {len(cases)} 个{suffix}"

    if kind == "supplement":
        endpoint_key = str(payload.get("endpoint_key") or "")
        scenario_text = str(payload.get("scenario_text") or "")
        max_cases = max(1, min(20, int(payload.get("max_cases") or 5)))
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
            stream_callback=stream_progress,
            artifact_language=artifact_language,
        )
        progress("追加保存补充用例", stage="save")
        if generated:
            save_cases(root, [*existing, *generated], project=project)
        suffix = f"；{warning}" if warning else ""
        return f"已补充用例 {len(generated)} 个{suffix}"

    if kind == "run":
        mode = str(payload.get("mode") or "smoke")
        parallel = max(0, int(payload.get("parallel") or 0))
        if parallel:
            config = load_config(root)
            config.setdefault("execution", {})["parallel"] = parallel
            save_config(root, config)
        progress("读取测试用例", stage="read")
        cases = load_cases_from_markdown(root, project)
        execution_config = load_config(root)
        progress("筛选执行范围", stage="filter")
        case_ids = None
        if mode == "smoke":
            case_ids = {
                case.id
                for case in cases
                if case_is_executable(case, execution_config)
                and ("happy_path" in case.tags or "dependency_chain" in case.tags)
            }
        selected_cases = [case for case in cases if case_ids is None or case.id in case_ids]
        runnable_count = sum(1 for case in selected_cases if case_is_executable(case, execution_config))
        skipped_count = len(selected_cases) - runnable_count
        progress(f"开始执行 {runnable_count} 条可执行用例；{skipped_count} 条不可执行用例会记录为 skipped", stage="execute")
        from ai_apitest.execution.options import RunOptions
        isolation = str(payload.get("isolation") or "")
        ranks = {"off": 0, "policy": 1, "process": 2, "container": 3}
        configured_isolation = execution_config.get("sandbox", {}).get("mode", "policy")
        if isolation and (isolation not in ranks or ranks[isolation] < ranks.get(configured_isolation, 1)):
            raise ValueError("Web 执行不能降低配置要求的隔离级别")
        results = run_cases(root, project=project, case_ids=case_ids, run_scope=mode, options=RunOptions(isolation=isolation))
        if not results:
            return "没有符合当前执行范围的用例，未发送请求且未生成空报告"
        passed = sum(1 for item in results if item.status == "passed")
        failed = sum(1 for item in results if item.status == "failed")
        skipped = sum(1 for item in results if item.status == "skipped")
        from ai_apitest.execution.control import ACTIVE_RUNTIME, RunCancelled
        if any(item.status == "cancelled" for item in results) or (ACTIVE_RUNTIME.get() and ACTIVE_RUNTIME.get().control.reason()):
            write_report(root, results, project=project, artifact_language=artifact_language)
            raise RunCancelled("用例执行已取消")
        progress(
            f"生成并审计执行报告：通过 {passed}，失败 {failed}，跳过 {skipped}",
            stage="audit",
        )
        ai_review, analysis = build_ai_report_bundle(
            root,
            project,
            results,
            artifact_language=artifact_language,
        )
        progress("保存通过质量门禁的执行报告", stage="report")
        write_report(
            root,
            results,
            project=project,
            ai_review=ai_review,
            analysis=analysis,
            artifact_language=artifact_language,
        )
        rate = f"{(passed / (passed + failed) * 100):.1f}%" if passed + failed else "-"
        return f"用例执行完成：共 {len(results)} 条，通过 {passed}，失败 {failed}，跳过 {skipped}，通过率 {rate}"

    if kind == "schedule":
        from ai_apitest.orchestrator.scheduler.runner import execute_scheduled_run
        progress("开始执行定时任务", stage="execute")
        execute_scheduled_run(root, project, payload["schedule"], artifact_language=artifact_language)
        return "定时任务执行完成"

    raise ValueError(f"不支持的持久化 Web 任务类型：{kind}")


def handle_action(root: Path, project: str, path: str, form: dict, handler) -> None:
    if path == "/actions/cancel-task":
        request_cancel(root, project, field(form, "task_id"))
        handler.redirect(url_for("/cases", project, {"message": "已请求取消任务"}))
        return
    if path == "/actions/discover":
        if active_task(root, project, "discover"):
            handler.redirect(url_for("/interfaces", project, {"message": "接口识别任务还在进行中，请稍后查看结果"}))
            return
        metadata = read_json(project_metadata_file(root, project), {})
        source = field(form, "source").strip() or str(metadata.get("source", "")).strip()
        start_task(root, project, "discover", "接口识别", kind="discover", payload={"source": source})
        handler.redirect(url_for("/interfaces", project, {"message": "接口识别任务已开始，可关闭提示并继续使用其它功能"}))
        return
    if path == "/actions/update-source":
        if active_task(root, project, "discover"):
            handler.redirect(url_for("/interfaces", project, {"message": "接口识别任务还在进行中，请稍后再修改来源"}))
            return
        try:
            source, _text, source_name = source_from_form(root, project, form)
        except Exception as exc:
            handler.redirect(url_for("/interfaces", project, {"message": str(exc)}))
            return
        start_task(
            root,
            project,
            "discover",
            "接口识别",
            kind="discover",
            payload={"source": source, "source_name": source_name},
        )
        handler.redirect(url_for("/interfaces", project, {"message": "接口来源已提交，识别任务正在后台进行"}))
        return
    if path == "/actions/add-endpoint":
        old_endpoints = load_endpoints(root, project)
        endpoints = list(old_endpoints)
        try:
            endpoints.append(endpoint_from_form(root, project, form))
        except Exception as exc:
            handler.redirect(url_for("/interfaces", project, {"message": str(exc)}))
            return
        save_endpoints(root, project, old_endpoints, endpoints, actor="user:web:add-endpoint")
        handler.redirect(url_for("/interfaces", project))
        return
    if path == "/actions/save-endpoint":
        index = int(field(form, "index"))
        old_endpoints = load_endpoints(root, project)
        endpoints = list(old_endpoints)
        if 0 <= index < len(endpoints):
            try:
                endpoints[index] = endpoint_from_form(root, project, form, existing=endpoints[index])
            except Exception as exc:
                handler.redirect(url_for("/interfaces", project, {"message": str(exc)}))
                return
            save_endpoints(root, project, old_endpoints, endpoints, actor="user:web:edit-endpoint")
        handler.redirect(url_for("/interfaces", project))
        return
    if path == "/actions/delete-endpoint":
        index = int(field(form, "index"))
        old_endpoints = load_endpoints(root, project)
        endpoints = list(old_endpoints)
        if 0 <= index < len(endpoints):
            endpoints.pop(index)
            save_endpoints(root, project, old_endpoints, endpoints, actor="user:web:delete-endpoint")
        handler.redirect(url_for("/interfaces", project))
        return
    if path == "/actions/save-case":
        case_id = field(form, "case_id").strip()
        cases = load_cases_from_markdown(root, project)
        case_index = next((index for index, case in enumerate(cases) if case.id == case_id), -1)
        if case_index < 0:
            handler.redirect(url_for("/cases", project, {"message": f"未找到用例：{case_id}"}))
            return
        try:
            updated = case_from_form(cases[case_index], form)
            issues = validate_compiled_cases([updated], load_endpoints(root, project))
            if issues:
                messages = "；".join(issue.message for issue in issues[:8])
                raise ValueError(f"用例未通过交付门禁：{messages}")
            candidate_cases = list(cases)
            candidate_cases[case_index] = updated
            endpoints = load_endpoints(root, project)
            baseline_gate_keys = {
                json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
                for item in case_quality_gate_issues(cases, endpoints)
                if str(item.get("severity") or "") in {"critical", "high"}
            }
            gate_issues = case_quality_gate_issues(
                candidate_cases,
                endpoints,
            )
            blocking = [
                item
                for item in gate_issues
                if str(item.get("severity") or "") in {"critical", "high"}
                and json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
                not in baseline_gate_keys
            ]
            if blocking:
                messages = "；".join(
                    str(item.get("reason") or item.get("evidence") or "")
                    for item in blocking[:8]
                )
                raise ValueError(f"用例未通过可信契约门禁：{messages}")
        except Exception as exc:
            handler.redirect(url_for("/cases", project, {"message": f"保存失败：{exc}"}))
            return
        cases[case_index] = updated
        save_cases(root, cases, project=project)
        handler.redirect(url_for("/cases", project, {"message": f"用例 {case_id} 已保存"}))
        return
    if path == "/actions/delete-case":
        case_id = field(form, "case_id").strip()
        cases = load_cases_from_markdown(root, project)
        retained = [case for case in cases if case.id != case_id]
        if len(retained) == len(cases):
            handler.redirect(url_for("/cases", project, {"message": f"未找到用例：{case_id}"}))
            return
        save_cases(root, retained, project=project)
        handler.redirect(url_for("/cases", project, {"message": f"用例 {case_id} 已删除"}))
        return
    if path == "/actions/delete-cases":
        raw_case_ids = form.get("case_ids") or []
        if isinstance(raw_case_ids, str):
            raw_case_ids = [raw_case_ids]
        requested_ids = {str(case_id).strip() for case_id in raw_case_ids if str(case_id).strip()}
        if not requested_ids:
            handler.redirect(url_for("/cases", project, {"message": "请先选择要删除的用例"}))
            return
        cases = load_cases_from_markdown(root, project)
        existing_ids = {case.id for case in cases}
        deleted_ids = requested_ids & existing_ids
        if not deleted_ids:
            handler.redirect(url_for("/cases", project, {"message": "所选用例已不存在，未删除任何内容"}))
            return
        save_cases(
            root,
            [case for case in cases if case.id not in deleted_ids],
            project=project,
        )
        handler.redirect(
            url_for("/cases", project, {"message": f"已批量删除 {len(deleted_ids)} 条用例"})
        )
        return
    if path == "/actions/generate":
        if active_task(root, project, "generate"):
            handler.redirect(url_for("/cases", project, {"message": "用例生成任务还在进行中，请稍后查看结果"}))
            return
        start_task(root, project, "generate", "用例生成", kind="generate", payload={"force": True})
        handler.redirect(url_for("/cases", project, {"message": "用例生成任务已开始，可关闭提示并继续使用其它功能"}))
        return
    if path == "/actions/supplement-cases":
        if active_task(root, project, "generate"):
            handler.redirect(url_for("/cases", project, {"message": "用例生成任务还在进行中，请稍后查看结果"}))
            return
        scenario_text = field(form, "scenario_text").strip()
        if not scenario_text:
            handler.redirect(url_for("/cases", project, {"message": "补充场景说明不能为空"}))
            return
        endpoint_key = field(form, "endpoint_key")
        max_cases = bounded_int(field(form, "max_cases"), default=5, minimum=1, maximum=20)

        start_task(
            root,
            project,
            "generate",
            "补充用例",
            kind="supplement",
            payload={"endpoint_key": endpoint_key, "scenario_text": scenario_text, "max_cases": max_cases},
        )
        handler.redirect(url_for("/cases", project, {"message": "补充用例任务已开始，可关闭提示并继续使用其它功能"}))
        return
    if path == "/actions/run-cases":
        if active_task(root, project, "run"):
            handler.redirect(url_for("/cases", project, {"message": "用例执行任务还在进行中，请稍后查看结果"}))
            return
        mode = field(form, "mode") or "smoke"
        parallel = int(field(form, "parallel") or "0")
        start_task(
            root,
            project,
            "run",
            "用例执行",
            kind="run",
            payload={"mode": mode, "parallel": parallel, "isolation": field(form, "isolation")},
            resumable=False,
        )
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
                start_task(root, project, "run", "定时任务执行", kind="schedule", payload={"schedule": item}, resumable=False)
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
    if path == "/actions/delete-reports":
        raw_names = form.get("report_names") or []
        if isinstance(raw_names, str):
            raw_names = [raw_names]
        requested_names = {str(name).strip() for name in raw_names if str(name).strip()}
        if not requested_names:
            handler.redirect(url_for("/reports", project, {"message": "请先选择要删除的报告"}))
            return
        reports_dir = project_reports_dir(root, project)
        deleted_count = 0
        for name in requested_names:
            report = reports_dir / name
            if (
                is_safe_report_name(name)
                and report.exists()
                and report.is_file()
                and report.parent == reports_dir
            ):
                report.unlink()
                deleted_count += 1
        message = (
            f"已批量删除 {deleted_count} 份报告"
            if deleted_count
            else "所选报告已不存在或名称无效，未删除任何内容"
        )
        handler.redirect(url_for("/reports", project, {"message": message}))
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
    if path == "/actions/save-quality-audit":
        config = load_config(root)
        quality = config.setdefault("quality_audit", {})
        quality["enabled"] = field(form, "enabled") == "on"
        quality["threshold"] = bounded_form_float(field(form, "threshold"), 85.0, 0.0, 99.99)
        quality["reliability_min"] = bounded_form_float(
            field(form, "reliability_min"), 90.0, 0.0, 100.0
        )
        quality["completeness_min"] = bounded_form_float(
            field(form, "completeness_min"), 85.0, 0.0, 100.0
        )
        quality["max_score_spread"] = bounded_form_float(
            field(form, "max_score_spread"), 15.0, 0.0, 100.0
        )
        quality["max_rounds"] = bounded_form_int(field(form, "max_rounds"), 3, 1, 3)
        quality["judge_max_attempts"] = bounded_form_int(
            field(form, "judge_max_attempts"), 2, 1, 3
        )
        quality["parallel_judges"] = field(form, "parallel_judges") == "on"
        # 两项是产品准入约束，保留在配置文件中便于解释，但不允许从控制台关闭。
        quality["require_all_models"] = True
        quality["critical_veto"] = True
        artifacts = quality.setdefault("artifacts", {})
        for artifact_type in ("endpoint_catalog", "test_cases", "test_report", "experiences"):
            artifacts[artifact_type] = field(form, f"artifact_{artifact_type}") == "on"
        save_config(root, config)
        handler.redirect(url_for("/config", project, {"message": "交付物质量审计配置已保存"}))
        return
    if path == "/actions/save-fixtures":
        try:
            providers = validate_fixture_providers(
                parse_json_field(field(form, "providers"), {})
            )
            config = load_config(root)
            fixtures = config.setdefault("fixtures", {})
            fixtures["timeout_seconds"] = bounded_form_float(
                field(form, "timeout_seconds"), 10.0, 0.1, 300.0
            )
            fixtures["providers"] = providers
            save_config(root, config)
            message = "外部测试能力配置已保存"
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            message = f"外部测试能力配置无效：{exc}"
        handler.redirect(url_for("/config", project, {"message": message}))
        return
    if path == "/actions/save-mock":
        try:
            routes = parse_json_field(field(form, "routes"), [])
            validate_mock_routes(routes)
            if not isinstance(routes, list):
                raise ValueError("routes 必须是数组")
            config = load_config(root)
            mock = config.setdefault("mock", {})
            mock["host"] = field(form, "host").strip() or "127.0.0.1"
            mock["port"] = bounded_form_int(field(form, "port"), 8780, 0, 65535)
            mock["max_request_bytes"] = bounded_form_int(
                field(form, "max_request_bytes"), 1_000_000, 1, 20_000_000
            )
            mock["max_history"] = bounded_form_int(
                field(form, "max_history"), 200, 1, 5000
            )
            mock["routes"] = routes
            save_config(root, config)
            message = translate_text("接口 Mock 配置已保存")
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            message = translate_format("接口 Mock 配置无效：{error}", error=exc)
        handler.redirect(url_for("/config", project, {"message": message}))
        return
    if path == "/actions/toggle-mock":
        enabled = field(form, "enabled") == "on"
        if enabled:
            try:
                status = start_web_mock_service(root)
                message = translate_format(
                    "接口 Mock 已开启：http://{host}:{port}，已加载 {count} 条路由",
                    host=status["host"],
                    port=status["port"],
                    count=status["route_count"],
                )
            except (OSError, ValueError, TypeError) as exc:
                message = translate_format("接口 Mock 启动失败：{error}", error=exc)
        else:
            stopped = stop_web_mock_service(root)
            message = translate_text("接口 Mock 已关闭" if stopped else "接口 Mock 当前未开启")
        handler.redirect(url_for("/config", project, {"message": message}))
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
    if path == "/actions/governance-save-number":
        key = field(form, "key")
        if key != "max_candidates_per_batch":
            handler.redirect(url_for("/config", project, {"message": f"不支持的配置项：{key}"}))
            return
        try:
            value = int(field(form, "value"))
        except ValueError:
            handler.redirect(url_for("/config", project, {"message": "候选治理批次必须是整数"}))
            return
        value = max(1, min(100, value))
        config = load_config(root)
        config.setdefault("governance", {})[key] = value
        save_config(root, config)
        handler.redirect(url_for("/config", project, {"message": f"候选治理批次已更新为 {value}"}))
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
    if progress:
        progress("解析接口文档", stage="parse")
    text, endpoints, metadata = load_and_parse_source(root, project, source, text=text)
    if progress:
        progress("正在调用大模型识别接口；如果模型响应较慢，可能需要等待较久", stage="llm")
    endpoints, metadata = apply_ai_discovery_if_configured(
        root,
        text,
        endpoints,
        metadata,
        stream_callback=stream_callback,
        project=project,
    )
    metadata["source"] = source
    if source_name:
        metadata["source_name"] = source_name
        metadata["source_kind"] = "file"
    from ai_apitest.application.discovery import normalize_document_for_agent

    atomic_write_text(
        project_source_snapshot_file(root, project),
        normalize_document_for_agent(text),
    )
    return endpoints, metadata


def handle_endpoint_context_changes(root: Path, project: str, old_endpoints: list[Endpoint], new_endpoints: list[Endpoint], *, actor: str) -> None:
    if old_endpoints:
        diff = diff_endpoints(old_endpoints, new_endpoints)
        changed = [*diff.get("removed", []), *diff.get("changed", [])]
        if changed:
            mark_lessons_for_endpoint_changes(root, project, changed, actor=actor)


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
    atomic_write_text(path, content)
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
    reports = sorted(
        (path for path in project_reports_dir(root, project).glob("*.md") if is_safe_report_name(path.name)),
        reverse=True,
    )
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
    message = display_message_for_tasks(project, raw_message, ["discover"], root=root)
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
    security = json.dumps(endpoint.security, ensure_ascii=False, indent=2) if endpoint.security else "无"
    tags = ", ".join(endpoint.tags) if endpoint.tags else "无"
    return f"""
    <dl class="endpoint-detail-list">
      <div><dt>请求方法</dt><dd><span class="method-pill">{esc(endpoint.method)}</span></dd></div>
      <div><dt>接口路径</dt><dd><code>{esc(endpoint.path)}</code></dd></div>
      <div><dt>operationId</dt><dd><code>{esc(endpoint.operation_id or '-')}</code></dd></div>
      <div><dt>弃用状态</dt><dd>{'已弃用' if endpoint.deprecated else '正常'}</dd></div>
      <div><dt>接口描述</dt><dd>{esc(endpoint.description or "-")}</dd></div>
      <div><dt>标签</dt><dd>{esc(tags)}</dd></div>
      <div><dt>参数</dt><dd><pre class="code-block">{esc(parameters)}</pre></dd></div>
      <div><dt>请求体</dt><dd><pre class="code-block">{esc(request_body)}</pre></dd></div>
      <div><dt>鉴权要求</dt><dd><pre class="code-block">{esc(security)}</pre></dd></div>
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

def render_case_coverage_summary(
    endpoints: list[Endpoint],
    cases: list,
    config: dict[str, Any] | None = None,
) -> str:
    from ai_apitest.domain.contracts import build_coverage_manifest

    manifest = build_coverage_manifest(endpoints, cases, config=config)
    stats = manifest["stats"]
    rows = manifest["rules"]
    if not rows:
        return (
            '<div class="notice">当前接口识别产物尚未形成可测试业务规则目录；'
            "重新识别接口后，框架会在这里展示规则到用例的完整覆盖血缘。</div>"
        )
    disposition_labels = {
        "executable": "可执行",
        "fixture_required": "依赖项目方能力",
        "fixture_configured": "外部能力已接入",
        "missing": "缺失",
    }
    detail_rows = []
    for row in rows:
        disposition = str(row.get("disposition") or "")
        requirements = set(row.get("fixture_requirements") or [])
        if (
            disposition == "fixture_required"
            and requirements
            and requirements.issubset(configured_fixture_requirements(config or {}))
        ):
            disposition = "fixture_configured"
        detail_rows.append(
            "<tr>"
            f"<td>{esc(row.get('rule_id', ''))}</td>"
            f"<td>{esc(row.get('endpoint_method', ''))} {esc(row.get('endpoint_path', ''))}</td>"
            f"<td>{esc(row.get('title', ''))}</td>"
            f"<td>{esc(disposition_labels.get(disposition, disposition))}</td>"
            f"<td>{esc(', '.join(row.get('case_ids') or []))}</td>"
            f"<td>{esc(external_test_capability_summary(row.get('fixture_requirements') or [], current_locale()) or '-')}</td>"
            "</tr>"
        )
    return f"""
    <section class="coverage-summary">
      <div class="section-head">
        <h3>可信契约覆盖</h3>
        <span class="meta-badge {'meta-ok' if not stats['missing_rule_count'] else 'meta-warn'}">
          {esc(stats['coverage_percent'])}%
        </span>
      </div>
      <div class="context-health">
        <div class="health-card"><span>用例总数</span><strong>{stats['case_count']}</strong><small>当前用例文件</small></div>
        <div class="health-card"><span>业务规则</span><strong>{stats['rule_count']}</strong><small>契约规则，不是用例数</small></div>
        <div class="health-card"><span>已覆盖</span><strong>{stats['covered_rule_count']}</strong><small>含外部能力依赖项</small></div>
        <div class="health-card"><span>可执行</span><strong>{stats['executable_rule_count']}</strong><small>当前可直接运行</small></div>
        <div class="health-card"><span>外部依赖</span><strong>{stats['fixture_required_rule_count']}</strong><small>需项目方提供，不伪造为失败</small></div>
        <div class="health-card"><span>缺失</span><strong>{stats['missing_rule_count']}</strong><small>生成门禁会阻止交付</small></div>
      </div>
      <details class="schedule-detail">
        <summary>查看规则—用例覆盖明细</summary>
        <div class="table-wrap">
          <table>
            <thead><tr><th>规则 ID</th><th>接口</th><th>业务场景</th><th>处置</th><th>用例</th><th>项目方需提供</th></tr></thead>
            <tbody>{''.join(detail_rows)}</tbody>
          </table>
        </div>
      </details>
    </section>
    """


def render_cases(root: Path, project: str, parsed) -> str:
    query = parse_qs(parsed.query)
    config = load_config(root)
    cases = load_cases_from_markdown(root, project)
    latest = latest_case_results(root, project)
    execution = config.get("execution_strategies", {})
    schedules = config.get("schedules", [])
    parallel = str(config.get("execution", {}).get("parallel", 4))
    raw_message = (query.get("message") or [""])[0]
    message = display_message_for_tasks(project, raw_message, ["generate", "run"], root=root)

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
    coverage_summary = render_case_coverage_summary(endpoints, cases, config)
    generation_pack = latest_context_pack(root, project, "case_generation")
    for index, case in enumerate(page_cases):
        last = latest.get(case.id, {})
        detail_id = f"case-detail-{start + index}"
        edit_id = f"case-edit-{start + index}"
        delete_id = f"case-delete-{start + index}"
        rows.append(
            "<tr>"
            f"<td class='select-cell'><input type='checkbox' name='case_ids' value='{esc(case.id)}' "
            f"form='case-bulk-delete-form' data-bulk-select='cases' aria-label='选择用例 {esc(case.id)}'></td>"
            f"<td>{esc(case.id)}</td><td>{esc(case.title)}</td><td>{esc(case.priority)}</td>"
            f"<td>{esc(last.get('created_at', ''))}</td><td class='{esc(last.get('status', ''))}'>{esc(last.get('status', ''))}</td>"
            f"<td class='actions-cell'>"
            f"<button type='button' onclick=\"document.getElementById('{detail_id}').showModal()\">详情</button>"
            f"<button type='button' onclick=\"document.getElementById('{edit_id}').showModal()\">编辑</button>"
            f"<button type='button' class='danger' onclick=\"document.getElementById('{delete_id}').showModal()\">删除</button>"
            f"</td>"
            "</tr>"
        )
        generation_context_html = render_context_pack_panel(root, project, generation_pack, case=case)
        modals.append(
            render_case_detail_modal(
                case,
                detail_id,
                generation_context_html,
                project=project,
                config=config,
            )
        )

    sort_headers = _render_sort_headers(project, sort_field, sort_order)
    pager_html = _render_case_pager(project, cur_page, total_pages, total, page_size, sort_field, sort_order)

    body = render_template(
        "cases.html",
        {
            "DASHBOARD_URL": url_for("/", project),
            "HIDDEN_PROJECT": hidden_project(project),
            "MESSAGE": f'<div class="notice">{esc(message)}</div>' if message else "",
            "COVERAGE_SUMMARY": coverage_summary,
            "SUPPLEMENT_FORM": render_supplement_form(project, endpoints),
            "PARALLEL": parallel,
            "SCHEDULE_ROWS": render_schedule_rows(project, schedules),
            "EXECUTION_MODE_OPTIONS": execution_mode_options(execution.get("custom_strategies", [])),
            "SORT_HEADERS": sort_headers,
            "PAGER": pager_html,
            "PAGE_INFO": f"共 {total} 条，第 {cur_page}/{total_pages} 页" if total else "",
            "ROWS": "".join(rows) or '<tr><td colspan="7">暂无用例</td></tr>',
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


def render_case_detail_modal(
    case,
    dialog_id: str,
    context_pack_html: str = "",
    *,
    project: str = "",
    config: dict[str, Any] | None = None,
) -> str:
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

    fixture_configured = case_has_configured_fixtures(case, config or {})
    effective_runnable = case_is_executable(case, config or {})
    if fixture_configured:
        runnable_badge = '<span class="meta-badge meta-ok">外部能力已接入，可执行</span>'
    elif effective_runnable:
        runnable_badge = '<span class="meta-badge meta-ok">可执行</span>'
    else:
        runnable_badge = '<span class="meta-badge meta-warn">不可执行</span>'
    risk_label = {
        "none": "无特殊风险",
        "negative_behavior": "负向行为",
        "destructive": "破坏性操作",
        "data_dependent": "依赖测试数据",
        "data_dependent_without_fixture": "缺少数据准备",
        "behavior_dependent": "依赖环境行为",
        "env_dependent": "依赖环境",
        "documented_response_without_fixture": "文档契约（缺少安全触发数据）",
        "shared_rate_limit": "共享环境限流",
        "fixture_required": "依赖项目方外部测试能力",
    }.get(str(case.risk).lower(), str(case.risk) or "-")
    edit_id = dialog_id.replace("case-detail-", "case-edit-", 1)
    delete_id = dialog_id.replace("case-detail-", "case-delete-", 1)
    actions = ""
    extra_dialogs = ""
    if project:
        actions = f"""
        <div class="modal-actions">
          <button type="button" onclick="document.getElementById('{dialog_id}').close(); document.getElementById('{edit_id}').showModal()">编辑用例</button>
          <button type="button" class="danger" onclick="document.getElementById('{dialog_id}').close(); document.getElementById('{delete_id}').showModal()">删除用例</button>
        </div>
        """
        extra_dialogs = render_case_edit_dialog(case, edit_id, delete_id, project)

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
          <h4>可信契约覆盖</h4>
          <p class="case-text">业务规则：{esc(", ".join(case.rule_ids) or "未关联结构化业务规则")}</p>
          <p class="case-text">覆盖分区：{esc(", ".join(case.coverage_partition_ids) or "-")}</p>
          <p class="case-text">项目方需提供的外部测试能力：{esc(external_test_capability_summary(case.fixture_requirements, current_locale()) or "无")}</p>
          <p class="case-text">接入状态：{esc("已接入，执行时自动准备并清理" if fixture_configured else ("未接入" if case.fixture_requirements else "不需要"))}</p>
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
          <summary>AI 生成依据与经验</summary>
          <div style="padding:12px">{context_pack_html}</div>
        </details>
        {actions}
      </div>
    </dialog>
    {extra_dialogs}
    """


def render_case_edit_dialog(case, edit_id: str, delete_id: str, project: str) -> str:
    from ai_apitest.core.models import execution_case_options
    request_data = json.dumps(
        {
            **{name: value for name, value in execution_case_options(case).items() if value},
            "method": case.method,
            "path": case.path,
            "headers": case.headers,
            "path_params": case.path_params,
            "query": case.query,
            "parameter_serialization": case.parameter_serialization,
            "body": case.body,
        },
        ensure_ascii=False,
        indent=2,
    )
    priority_options = "".join(
        f"<option value='{value}' {'selected' if case.priority == value else ''}>{value}</option>"
        for value in ("P0", "P1", "P2", "P3")
    )
    method_labels = {
        "happy_path": "主流程",
        "optional_field": "可选字段",
        "required_field": "必填字段",
        "type_error": "类型错误",
        "boundary_value": "边界值",
        "path_parameter_error": "路径参数异常",
        "dependency_chain": "依赖链",
        "auth_error": "鉴权异常",
        "equivalence_class": "等价类",
        "status_code_coverage": "状态码覆盖",
    }
    design_options = "".join(
        f"<option value='{value}' {'selected' if case.design_method == value else ''}>{esc(label)}</option>"
        for value, label in method_labels.items()
    )
    risk_labels = {
        "none": "无特殊风险",
        "negative_behavior": "负向行为",
        "destructive": "破坏性操作",
        "data_dependent": "依赖测试数据",
        "data_dependent_without_fixture": "缺少数据准备",
        "behavior_dependent": "依赖环境行为",
        "env_dependent": "依赖环境",
        "documented_response_without_fixture": "文档契约（缺少安全触发数据）",
        "shared_rate_limit": "共享环境限流",
        "fixture_required": "依赖项目方外部测试能力",
    }
    risk_options = "".join(
        f"<option value='{value}' {'selected' if case.risk == value else ''}>{esc(label)}</option>"
        for value, label in risk_labels.items()
    )
    assertions = json.dumps(to_jsonable(case.assertions), ensure_ascii=False, indent=2)
    setup_steps = json.dumps(to_jsonable(case.setup_steps), ensure_ascii=False, indent=2)
    chain_steps = json.dumps(to_jsonable(case.steps), ensure_ascii=False, indent=2)
    cleanup_steps = json.dumps(to_jsonable(case.cleanup_steps), ensure_ascii=False, indent=2)
    return f"""
    <dialog id="{edit_id}" class="modal case-detail-modal" data-reset-on-close>
      <form method="post" action="/actions/save-case" class="modal-form">
        <div class="modal-head">
          <h3>编辑用例 <span class="case-id-chip">{esc(case.id)}</span></h3>
          <button type="button" class="ghost" onclick="cancelDialog('{edit_id}')">关闭</button>
        </div>
        {hidden_project(project)}
        <input type="hidden" name="case_id" value="{esc(case.id)}">
        <label>用例名称</label>
        <input name="title" value="{esc(case.title)}" required>
        <div class="form-grid">
          <div><label>优先级</label><select name="priority">{priority_options}</select></div>
          <div><label>设计方法</label><select name="design_method">{design_options}</select></div>
          <div><label>风险</label><select name="risk">{risk_options}</select></div>
        </div>
        <div class="inline-options">
          <label><input type="checkbox" name="runnable" {'checked' if case.runnable else ''}> 可执行</label>
          <label><input type="checkbox" name="negative" {'checked' if case.negative else ''}> 负向用例</label>
        </div>
        <label>标签（逗号或换行分隔）</label>
        <input name="tags" value="{esc(', '.join(case.tags))}">
        <label>前置条件（每行一条）</label>
        <textarea name="preconditions" class="small-textarea">{esc(chr(10).join(case.preconditions))}</textarea>
        <label>用例步骤（每行一条）</label>
        <textarea name="test_steps" class="small-textarea">{esc(chr(10).join(case.test_steps))}</textarea>
        <label>预期结果</label>
        <textarea name="expected_result" class="small-textarea" required>{esc(case.expected_result)}</textarea>
        <details class="case-data" open>
          <summary>请求与断言</summary>
          <label>请求数据 JSON</label>
          <textarea name="request_data" class="body-textarea code-input" required>{esc(request_data)}</textarea>
          <label>断言列表 JSON</label>
          <textarea name="assertions" class="body-textarea code-input" required>{esc(assertions)}</textarea>
        </details>
        <details class="case-data">
          <summary>准备、链路与清理步骤</summary>
          <label>前置准备步骤 JSON</label>
          <textarea name="setup_steps" class="body-textarea code-input">{esc(setup_steps)}</textarea>
          <label>链路步骤 JSON</label>
          <textarea name="chain_steps" class="body-textarea code-input">{esc(chain_steps)}</textarea>
          <label>清理步骤 JSON</label>
          <textarea name="cleanup_steps" class="body-textarea code-input">{esc(cleanup_steps)}</textarea>
        </details>
        <small>保存时会重新执行接口引用、请求契约、断言语义和链路变量门禁；不符合交付标准的修改不会写入文件。</small>
        <div class="modal-actions">
          <button type="button" class="ghost" onclick="cancelDialog('{edit_id}')">取消</button>
          <button>保存修改</button>
        </div>
      </form>
    </dialog>
    <dialog id="{delete_id}" class="modal">
      <form method="post" action="/actions/delete-case" class="modal-form">
        <div class="modal-head">
          <h3>确认删除用例</h3>
          <button type="button" class="ghost" onclick="document.getElementById('{delete_id}').close()">关闭</button>
        </div>
        {hidden_project(project)}
        <input type="hidden" name="case_id" value="{esc(case.id)}">
        <p class="danger-text">确认删除 {esc(case.id)} · {esc(case.title)}？执行报告中的历史结果不会被删除。</p>
        <div class="modal-actions">
          <button type="button" class="ghost" onclick="document.getElementById('{delete_id}').close()">取消</button>
          <button class="danger">确认删除</button>
        </div>
      </form>
    </dialog>
    """


def html_list(items: list[str]) -> str:
    return "<ul class='compact-list'>" + "".join(f"<li>{esc(item)}</li>" for item in items) + "</ul>"


def html_ordered_list(items: list[str]) -> str:
    return "<ol class='compact-list'>" + "".join(f"<li>{esc(item)}</li>" for item in items) + "</ol>"


def render_reports(root: Path, project: str, parsed) -> str:
    query = parse_qs(parsed.query)
    current_page = max(1, int((query.get("page") or ["1"])[0]))
    message = (query.get("message") or [""])[0]
    reports = sorted(
        (path for path in project_reports_dir(root, project).glob("*.md") if is_safe_report_name(path.name)),
        reverse=True,
    )
    page_size = 10
    page_reports = reports[(current_page - 1) * page_size : current_page * page_size]
    rows = []
    for path in page_reports:
        summary = parse_report_summary(path)
        rows.append(
            "<tr>"
            f"<td class='select-cell'><input type='checkbox' name='report_names' value='{esc(path.name)}' "
            f"form='report-bulk-delete-form' data-bulk-select='reports' aria-label='选择报告 {esc(path.name)}'></td>"
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
            "HIDDEN_PROJECT": hidden_project(project),
            "MESSAGE": f'<div class="notice">{esc(message)}</div>' if message else "",
            "ROWS": "".join(rows) or '<tr><td colspan="7">暂无报告</td></tr>',
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

    l_total = count_lessons(root, project, lifecycle_status=lifecycle, lesson_type=lesson_type, scope=scope, query_text=q)
    l_total_pages = max(1, (l_total + l_size - 1) // l_size)
    l_page = min(l_page, l_total_pages)
    l_start = (l_page - 1) * l_size
    page_lessons = list_lessons(
        root,
        project,
        lifecycle_status=lifecycle,
        lesson_type=lesson_type,
        scope=scope,
        query_text=q,
        limit=l_size,
        offset=l_start,
    )

    conflicts = conflict_pairs(root, project, limit=20)
    similars = similar_pairs(root, project, limit=20)
    audits = recent_audit_logs(root, project, limit=100)

    # Anchor nav + section badges
    anchor_nav = _render_context_anchor(
        project, stats, lesson_count=l_total,
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
        },
    )
    return page(root, project, "项目上下文", body)


def _render_context_anchor(project: str, stats: dict, lesson_count: int, conflict_count: int, similar_count: int, audit_count: int, lifecycle: str, lesson_type: str, scope: str, q: str) -> str:
    base = {"status": lifecycle, "type": lesson_type, "scope": scope, "q": q}
    items = [
        ("lessons", "经验库", lesson_count),
        ("conflicts", "冲突裁决", conflict_count),
        ("similars", "相似合并", similar_count),
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
            actions.append(f"<form method='post' action='/actions/context-confirm'>{hidden_project(project)}<input type='hidden' name='lesson_id' value='{esc(lesson['id'])}'><button>{translate_text('确认')}</button></form>")
        if lesson.get("lesson_type") == "known_issue" and lesson.get("lifecycle_status") == "resolved_candidate":
            actions.append(f"<form method='post' action='/actions/context-resolve-known-issue'>{hidden_project(project)}<input type='hidden' name='lesson_id' value='{esc(lesson['id'])}'><button>{translate_text('确认已解决')}</button></form>")
        if lesson.get("lifecycle_status") not in {"ignored", "archived", "merged"}:
            ignore_label = translate_text("忽略" if lesson.get("lifecycle_status") == "pending" else "停用")
            actions.append(f"<form method='post' action='/actions/context-ignore'>{hidden_project(project)}<input type='hidden' name='lesson_id' value='{esc(lesson['id'])}'><button class='danger'>{ignore_label}</button></form>")
        actions.append(f"<button type='button' onclick=\"document.getElementById('{edit_id}').showModal()\">{translate_text('编辑')}</button>")
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
                <small>{esc(translate_format('{endpoint} · 出现 {count} 次 · 标签：{tags}', endpoint=endpoint, count=lesson.get('occurrence_count', 0), tags=tags))}</small>
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
                <button type="button" onclick="document.getElementById('{edit_id}').showModal()">{translate_text('编辑')}</button>
                <form method="post" action="/actions/context-ignore">
                  {hidden_project(project)}
                  <input type="hidden" name="lesson_id" value="{esc(item['id'])}">
                  <button class="danger">{translate_text('停用')}</button>
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
              <button class="danger">{translate_text('停用全部')}</button>
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
          <summary>{esc(translate_format('查看 {count} 条具体依赖', count=len(items)))}</summary>
          {detail_html}
        </details>
        <small>{esc(translate_format('聚合展示 · 底层保留 {count} 条可独立验证的依赖记录 · 标签：{tags}', count=len(items), tags=tags))}</small>
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
        return f"{translate_text(prefix)} · {label_for('review_status', lesson.get('review_status', '-'))}"
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
        + f"<button{button_class}>{esc(translate_text(label))}</button></form>"
    )


def similar_form(project: str, relation_id: str, resolution: str, label: str) -> str:
    return (
        "<form method='post' action='/actions/context-resolve-similar'>"
        + hidden_project(project)
        + f"<input type='hidden' name='relation_id' value='{esc(relation_id)}'>"
        + f"<input type='hidden' name='resolution' value='{esc(resolution)}'>"
        + f"<button>{esc(translate_text(label))}</button></form>"
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


def render_context_pack_panel(root: Path, project: str, pack: dict | None, *, case=None) -> str:
    if not pack:
        return "<div class='empty-state'>暂无生成依据审计记录。本用例仍可根据接口文档、请求数据和断言内容进行检查。</div>"
    included_ids = [str(item) for item in pack.get("included_lesson_ids", [])]
    excluded = [item for item in pack.get("excluded_lesson_ids", []) if isinstance(item, dict)]
    excluded_ids = [str(item.get("id") or "") for item in excluded if str(item.get("id") or "")]
    all_lessons = lessons_by_ids(root, project, list(dict.fromkeys([*included_ids, *excluded_ids])))
    relevant_included_ids = [
        lesson_id
        for lesson_id in included_ids
        if lesson_id in all_lessons and context_lesson_matches_case(all_lessons[lesson_id], case)
    ]
    included_rows = []
    for lesson_id in relevant_included_ids:
        lesson = all_lessons[lesson_id]
        title = lesson.get("title") or "未命名经验"
        endpoint = f"{lesson.get('method', '')} {lesson.get('path', '')}".strip()
        content = str(lesson.get("content") or "").strip()
        meta = " · ".join(
            item
            for item in (
                endpoint,
                label_for("lesson_type", lesson.get("lesson_type", "")),
                lesson_state_label(lesson),
            )
            if item
        )
        included_rows.append(
            f"<li><strong>{esc(title)}</strong><small>{esc(meta)}</small>"
            f"{f'<p>{esc(content)}</p>' if content else ''}</li>"
        )
    excluded_rows = []
    for item in excluded:
        lesson = all_lessons.get(str(item.get("id") or ""))
        if not lesson or not context_lesson_matches_case(lesson, case):
            continue
        title = str(lesson.get("title") or "未命名经验")
        endpoint = f"{lesson.get('method', '')} {lesson.get('path', '')}".strip()
        reason = context_exclusion_reason_label(str(item.get("reason") or ""))
        excluded_rows.append(
            f"<li><strong>{esc(title)}</strong><small>{esc(endpoint)}</small><p>未采用原因：{esc(reason)}</p></li>"
        )
    included_html = (
        "<ul class='compact-list context-evidence-list'>" + "".join(included_rows) + "</ul>"
        if included_rows
        else "<div class='empty-state'>本用例没有引用已确认经验，主要依据接口文档和启用的生成策略生成。</div>"
    )
    excluded_html = (
        "<ul class='compact-list context-evidence-list'>" + "".join(excluded_rows) + "</ul>"
        if excluded_rows
        else "<div class='empty-state'>没有与本用例相关但未采用的经验。</div>"
    )
    task_label = {
        "case_generation": "用例生成",
        "report_analysis": "报告分析",
    }.get(str(pack.get("task_type") or ""), str(pack.get("task_type") or "-"))
    return f"""
    <div class="context-pack">
      <p class="case-text">展示本批生成时实际提供给模型、且与当前用例接口相关的已确认经验。接口文档、生成策略和本地质量门禁不计入经验条数。</p>
      <div class="context-pack-summary">
        <div><span>参考经验</span><strong>{len(included_rows)} 条</strong></div>
        <div><span>未采用经验</span><strong>{len(excluded_rows)} 条</strong></div>
        <div><span>生成时间</span><strong>{esc(pack.get('created_at', '-') or '-')}</strong></div>
      </div>
      <details class="case-data">
        <summary>查看生成时参考的经验（{len(included_rows)}）</summary>
        {included_html}
      </details>
      <details class="case-data">
        <summary>查看相关但未采用的经验（{len(excluded_rows)}）</summary>
        {excluded_html}
      </details>
      <details class="case-data context-technical-audit">
        <summary>技术审计信息（仅用于排查生成问题）</summary>
        <dl class="endpoint-detail-list">
          <div><dt>任务类型</dt><dd>{esc(task_label)}</dd></div>
          <div><dt>生成模型</dt><dd>{esc(pack.get('model', '-') or '-')}</dd></div>
          <div><dt>经验上下文上限</dt><dd>{esc(pack.get('context_budget', 0))} 估算 Token</dd></div>
          <div><dt>本次经验占用</dt><dd>{esc(pack.get('used_tokens', 0))} 估算 Token</dd></div>
          <div><dt>审计快照编号</dt><dd>{esc(pack.get('id', ''))}</dd></div>
        </dl>
        <small>预算用于限制经验文本占用模型上下文，避免挤占接口事实和模型输出空间；它不是用例执行预算，也不会影响接口请求。</small>
      </details>
    </div>
    """


def context_lesson_matches_case(lesson: dict, case) -> bool:
    if case is None:
        return True
    method = str(lesson.get("method") or "").upper()
    path = str(lesson.get("path") or "")
    if not method and not path:
        return True
    case_keys = set()
    if str(case.method).upper() != "CHAIN":
        case_keys.add((str(case.method).upper(), str(case.path)))
    for step in [*case.setup_steps, *case.steps, *case.cleanup_steps]:
        case_keys.add((str(step.method).upper(), str(step.path)))
    return (method, path) in case_keys


def context_exclusion_reason_label(reason: str) -> str:
    return {
        "over_budget": "相关性排序后未进入本次经验上下文",
        "lifecycle_status_pending": "尚未完成人工确认",
        "confidence_low": "置信度不足",
    }.get(reason, "未满足本次注入条件")


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


def localized_trend_comparability_note(latest: dict, previous: dict, comp: dict) -> str:
    latest_total = int(latest.get("total_cases", 0))
    previous_total = int(previous.get("total_cases", 0))
    if comp.get("scope_mismatch"):
        latest_scope = translate_text(
            {"smoke": "冒烟", "full": "全量"}.get(
                str(latest.get("run_scope") or ""), str(latest.get("run_scope") or "")
            )
        )
        previous_scope = translate_text(
            {"smoke": "冒烟", "full": "全量"}.get(
                str(previous.get("run_scope") or ""), str(previous.get("run_scope") or "")
            )
        )
        return translate_format(
            "本次 {latest_scope} vs 上次 {previous_scope}，执行范围不同",
            latest_scope=latest_scope,
            previous_scope=previous_scope,
        )
    jaccard = comp.get("jaccard")
    if jaccard is not None:
        overlap = f"{float(jaccard):.0%}"
        if float(jaccard) >= 0.7:
            template = "用例重合度 {overlap}，可以比较"
        elif float(jaccard) >= 0.4:
            template = "用例重合度 {overlap}，仅做参考"
        else:
            template = "用例重合度仅 {overlap}，不可直接比较"
        return translate_format(template, overlap=overlap)
    if not comp.get("comparable"):
        template = "本次 {latest_total} 条 vs 上次 {previous_total} 条，样本量差异过大"
    elif comp.get("confidence") == "medium":
        template = "本次 {latest_total} 条 vs 上次 {previous_total} 条，样本量有变化，通过率仅做参考"
    else:
        template = "两次执行规模接近（{latest_total} vs {previous_total}），可以比较"
    return translate_format(
        template,
        latest_total=latest_total,
        previous_total=previous_total,
    )


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
    scope_label = translate_text(
        {"smoke": "冒烟", "full": "全量"}.get(
            latest.get("run_scope", ""), latest.get("run_scope", "") or ""
        )
    )
    scope_note = f" · {translate_text('范围')}: {scope_label}" if scope_label else ""

    if previous:
        comp = assess_trend_comparability(latest, previous)
        comparable = comp["comparable"]
        confidence = comp["confidence"]
        comparability_note = localized_trend_comparability_note(latest, previous, comp)
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
        trend_label = translate_text("不可直接比较")
        summary = translate_format(
            "{note}，通过率变化可能主要来自样本差异而非质量变化。",
            note=comparability_note,
        )
    elif total_delta > 0.01:
        trend_class = "trend-up"
        trend_label = translate_text("通过率上升")
        summary = translate_text("最近通过率在上升，说明接口稳定性、测试数据或用例预期正在改善。")
    elif total_delta < -0.01:
        trend_class = "trend-down"
        trend_label = translate_text("通过率下降")
        summary = translate_text("最近通过率下降，需要优先判断是接口真实回归、测试数据失效，还是用例预期不准确。")
    else:
        trend_class = "trend-stable"
        trend_label = translate_text("通过率稳定")
        summary = translate_text("最近通过率变化不大，重点应放在长期重复失败和待复核经验上。")

    # ── Insight cards ──
    confidence_label = translate_text({"high": "高", "medium": "中", "low": "低"}.get(confidence, confidence))
    confidence_badge = f"<span class='trend-confidence trend-conf-{confidence}'>{translate_text({'high': '高置信', 'medium': '中置信', 'low': '低置信'}.get(confidence, confidence))}</span>"
    cards = [
        (translate_text("最新通过率"), f"{latest_rate:.1%}", esc(translate_format("{passed}/{total} 通过，{failed} 失败", passed=latest_passed, total=latest_total, failed=latest_failed))),
        (translate_text("样本可比性"), confidence_label, esc(comparability_note) if comparability_note else translate_text("两次执行规模接近")),
        (translate_text("接口覆盖"), str(len(latest_eps)), esc(translate_format("新增失败 {new} · 恢复 {recovered} · 持续失败 {persistent}", new=len(new_fail_eps), recovered=len(recovered_eps), persistent=len(persistent_fail_eps)))),
    ]
    if previous:
        prev_rate = float(previous.get("pass_rate", 0))
        cards.insert(1, (translate_text("与上次比较"), signed_percent(latest_rate - prev_rate), esc(translate_format("上次通过率 {rate}", rate=f"{prev_rate:.1%}"))))
    insight_cards = "".join(
        f"<div><span>{esc(label)}</span><strong>{esc(value)}</strong><small>{desc}</small></div>"
        for label, value, desc in cards
    )

    # ── Risk & Suggestions (data-driven) ──
    risk_items = []
    suggestions = []

    if not comparable:
        risk_items.append(translate_format("本次 {total} 条用例与上次样本差异过大，不建议直接比较通过率。", total=latest_total))
        suggestions.append(translate_text("使用相同的执行范围（全量/冒烟）再执行一次，以便准确判断趋势。"))

    if new_fail_eps:
        names = ", ".join(f"<code>{esc(ep)}</code>" for ep in new_fail_eps[:5])
        suffix = translate_format("等 {count} 个", count=len(new_fail_eps)) if len(new_fail_eps) > 5 else ""
        risk_items.append(translate_format("新增失败接口：{names}{suffix}，优先确认是否真实回归。", names=names, suffix=suffix))
        suggestions.append(translate_format("检查 {names}{suffix} 的接口文档是否变更，或环境数据是否异常。", names=names, suffix=suffix))

    if persistent_fail_eps:
        names = ", ".join(f"<code>{esc(ep)}</code>" for ep in persistent_fail_eps[:3])
        suffix = translate_format("等 {count} 个", count=len(persistent_fail_eps)) if len(persistent_fail_eps) > 3 else ""
        risk_items.append(translate_format("持续失败接口：{names}{suffix}，建议标记为已知问题或修正前置数据。", names=names, suffix=suffix))
        suggestions.append(translate_format("将 {names}{suffix} 的失败原因沉淀为项目上下文，避免 AI 反复生成不合预期的断言。", names=names, suffix=suffix))

    if recovered_eps:
        names = ", ".join(f"<code>{esc(ep)}</code>" for ep in recovered_eps[:3])
        suffix = translate_format("等 {count} 个", count=len(recovered_eps)) if len(recovered_eps) > 3 else ""
        risk_items.append(translate_format("恢复通过接口：{names}{suffix}，可回顾之前失败原因是否已消除。", names=names, suffix=suffix))
        suggestions.append(translate_format("如果 {names}{suffix} 之前是已知问题，建议执行一次验证确认解决。", names=names, suffix=suffix))

    if fail_concentration:
        for key, failed, total_ep in fail_concentration[:3]:
            name = f"<code>{esc(key)}</code>"
            risk_items.append(translate_format("失败集中：{name} 失败 {failed}/{total}，集中度 {rate}。", name=name, failed=failed, total=total_ep, rate=f"{failed/max(total_ep,1):.0%}"))
            suggestions.append(translate_format("优先排查 {name} 的断言预期，可能是用例生成时对返回格式理解有误。", name=name))

    if latest_failed == 0 and latest_total > 0:
        risk_items.append(translate_text("本次全部通过，当前无可关注风险。"))
        suggestions.append(translate_text("保持当前执行策略，可关注覆盖率是否有提升空间。"))

    # Fallback when no endpoint_metrics (e.g. historical data)
    if not latest_eps and latest_total > 0:
        if not risk_items:
            risk_items.append(translate_format("本次执行 {total} 条用例，通过 {passed} 条，失败 {failed} 条。", total=latest_total, passed=latest_passed, failed=latest_failed))
        if not suggestions:
            if latest_failed > 0:
                suggestions.append(translate_format("查看最新报告中的 {failed} 条失败用例，逐一确认失败原因。", failed=latest_failed))
                suggestions.append(translate_text("把确认后的业务规则沉淀到项目上下文，避免下次生成相同错误预期。"))
            else:
                suggestions.append(translate_text("保持当前执行策略，后续关注新增失败类别。"))

    # ── Latency insight ──
    latency_note = ""
    pcts = latest.get("latency_percentiles", {})
    if pcts and pcts.get("p95"):
        p50 = pcts.get("p50", 0)
        p95 = pcts.get("p95", 0)
        latency_note = f"<small>{esc(translate_format('响应延迟 P50={p50}ms P95={p95}ms', p50=p50, p95=p95))}</small>"

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
        <span>{esc(translate_format('最近 {count} 次执行', count=len(runs)))}</span>
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
          <div><span>{translate_text('具体发现')}</span><ul>{risk_html}</ul></div>
          <div><span>{translate_text('建议动作')}</span><ul>{step_html}</ul></div>
        </div>
      </div>
    </div>
    <div class='table-wrap'><table><thead><tr><th>Run</th><th>{translate_text('时间')}</th><th>{translate_text('总数')}</th><th>{translate_text('通过')}</th><th>{translate_text('失败')}</th><th>{translate_text('通过率')}</th></tr></thead><tbody>{rows}</tbody></table></div>
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
    return translate_text(labels.get(kind, {}).get(text, text or "-"))


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
    label = {
        "conflict": "确认存在冲突",
        "not_conflict": "不是冲突，已移出裁决列表",
        "uncertain": "无法确定，保留人工裁决",
    }.get(str(value or ""), str(value or "未知"))
    return translate_text(label)


def resolution_label(value: object) -> str:
    label = {
        "keep_a": "保留 A，忽略 B",
        "keep_b": "保留 B，忽略 A",
        "keep_both": "两条都保留，但标记为待复核",
        "ignore_both": "两条都忽略",
        "merge_a": "合并到 A",
        "merge_b": "合并到 B",
    }.get(str(value or ""), str(value or "未说明"))
    return translate_text(label)


def lifecycle_status_label(value: object) -> str:
    label = {
        "active": "已启用",
        "resolved": "已解决",
        "resolved_candidate": "候选已解决",
        "reopened": "重新出现",
        "ignored": "已忽略",
        "archived": "已归档",
    }.get(str(value or ""), str(value or "未说明"))
    return translate_text(label)


def task_type_label(value: object) -> str:
    label = {
        "api_discovery": "接口识别",
        "case_generation": "用例生成",
        "report_analysis": "报告分析",
        "case_maintenance": "用例维护",
    }.get(str(value or ""), str(value or "AI 任务"))
    return translate_text(label)


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
    fixtures = config.get("fixtures", {})
    mock = config.get("mock", {})
    governance = config.get("governance", {})
    agents = config.get("agents", {})
    quality_audit = config.get("quality_audit", {})
    message = (query.get("message") or [""])[0]
    governance_toggles = [
        ("enabled", "治理总开关", "关闭后暂停全部主控治理流程。"),
        ("inline_after_report", "报告后同步治理", "开启后报告生成会继续串行执行冲突、报告和经验治理；默认关闭以避免多次模型调用阻塞报告。"),
        ("review_reports", "报告质量审查", "核对报告结论、归因和建议是否与执行结果一致。"),
        ("review_cases", "用例质量审查", "审查覆盖计划及最终用例，并在需要时定向修订。"),
        ("review_experience_candidates", "经验候选治理", "裁决待确认经验的创建、合并、拒绝或冲突状态。"),
        ("low_confidence_requires_manual", "低置信度人工确认", "低置信度裁决不自动生效，转入人工确认。"),
    ]
    gov_rows = "".join(
        f"<tr><td>{esc(label)}<br><small>{esc(key)}</small></td>"
        f"<td>{'启用' if governance.get(key, True) else '停用'}</td>"
        f"<td>{esc(description)}</td>"
        f"<td>{_governance_toggle_form(project, key, bool(governance.get(key, True)))}</td></tr>"
        for key, label, description in governance_toggles
    )
    max_candidates = max(1, min(100, int(governance.get("max_candidates_per_batch", 10))))
    gov_rows += (
        "<tr><td>单批经验候选数<br><small>max_candidates_per_batch</small></td>"
        f"<td>{max_candidates}</td><td>每轮主控治理处理的候选上限，范围 1–100。</td>"
        f"<td>{_governance_number_form(project, 'max_candidates_per_batch', max_candidates)}</td></tr>"
    )
    body = render_template(
        "config.html",
        {
            "DASHBOARD_URL": url_for("/", project),
            "MESSAGE": f'<div class="notice">{esc(message)}</div>' if message else "",
            "LLM_MASTER": render_llm_role_form(project, "master", llm["master"]),
            "LLM_CASE_GENERATOR": render_llm_role_form(project, "case_generator", llm["case_generator"]),
            "LLM_ANALYZER": render_llm_role_form(project, "analyzer", llm["analyzer"]),
            "AGENT_RUNTIME_CONFIG": render_agent_runtime_config(
                agents,
                AgentRunRepository(root, project).list_recent(limit=10),
            ),
            "QUALITY_AUDIT_CONFIG": render_quality_audit_config(
                project,
                quality_audit,
                QualityAuditRepository(root, project).list_recent(limit=20),
            ),
            "GENERATION_STRATEGIES": render_default_generation_strategies(project, generation.get("default_strategies", {})),
            "CUSTOM_GENERATION_STRATEGIES": render_custom_strategies(project, "case_generation", generation.get("custom_strategies", []), "/actions/add-custom-generation-strategy", "/actions/delete-custom-generation-strategy"),
            "CUSTOM_EXECUTION_STRATEGIES": render_custom_execution_strategies(project, execution.get("custom_strategies", [])),
            "FIXTURE_CONFIG": render_fixture_config(project, fixtures),
            "MOCK_CONFIG": render_mock_config(root, project, mock),
            "GOVERNANCE_ROWS": gov_rows,
        },
    )
    return page(root, project, "配置中心", body)


def render_fixture_config(project: str, config: dict[str, Any]) -> str:
    config = config if isinstance(config, dict) else {}
    providers = config.get("providers", {})
    providers = providers if isinstance(providers, dict) else {}
    configured = configured_fixture_requirements({"fixtures": {"providers": providers}})
    rows = []
    for requirement, provider in providers.items():
        provider_type = str(provider.get("type") or "-") if isinstance(provider, dict) else "-"
        label = EXTERNAL_TEST_CAPABILITY_LABELS.get(
            str(requirement),
            "额外的受控场景准备能力",
        )
        rows.append(
            "<tr>"
            f"<td>{esc(label)}</td><td>{esc(provider_type)}</td>"
            f"<td>{'已接入' if str(requirement) in configured else '未启用或配置无效'}</td>"
            "</tr>"
        )
    rows_html = "".join(rows) or "<tr><td colspan='3'>尚未配置项目方外部测试能力</td></tr>"
    providers_json = json.dumps(providers, ensure_ascii=False, indent=2)
    return f"""
    <p>框架只负责调用项目方提供的能力。支持 <code>static</code> 固定测试数据和
    <code>http</code> 准备/清理接口；执行过程不调用大模型。</p>
    <div class="table-wrap"><table>
      <thead><tr><th>能力</th><th>提供者</th><th>状态</th></tr></thead>
      <tbody>{rows_html}</tbody>
    </table></div>
    <details class="schedule-detail">
      <summary>编辑外部测试能力配置</summary>
      <form method="post" action="/actions/save-fixtures" class="stack-form">
        {hidden_project(project)}
        <label>调用超时（秒）<input type="number" name="timeout_seconds" min="0.1" max="300" step="0.1" value="{esc(config.get('timeout_seconds', 10))}"></label>
        <label>提供者 JSON<textarea name="providers" rows="18" spellcheck="false">{esc(providers_json)}</textarea></label>
        <small>HTTP 提供者可使用环境变量，例如 <code>${{TEST_SUPPORT_TOKEN}}</code>；密钥不要直接写入配置。</small>
        <button type="submit">保存外部测试能力</button>
      </form>
    </details>
    """


def render_mock_config(root: Path, project: str, config: dict[str, Any]) -> str:
    config = config if isinstance(config, dict) else {}
    routes = config.get("routes", [])
    routes = routes if isinstance(routes, list) else []
    enabled_count = sum(
        isinstance(route, dict) and route.get("enabled", True)
        for route in routes
    )
    routes_json = json.dumps(routes, ensure_ascii=False, indent=2)
    route_example = json.dumps(
        [
            {
                "name": translate_text("模拟上游用户服务异常"),
                "enabled": True,
                "method": "GET",
                "path": "/upstream/users/{user_id}",
                "match": {"query": {"mode": "failure"}},
                "response": {
                    "status": 503,
                    "headers": {"Content-Type": "application/json"},
                    "body": {
                        "user_id": "{{user_id}}",
                        "error": "upstream unavailable",
                    },
                },
            }
        ],
        ensure_ascii=False,
        indent=2,
    )
    command = f"iatest mock --project {project}"
    status = web_mock_status(root)
    running = bool(status.get("running"))
    status_label = translate_text("监听中" if running else "已关闭")
    actual_address = (
        f"{status.get('host')}:{status.get('port')}"
        if running
        else f"{config.get('host', '127.0.0.1')}:{config.get('port', 8780)}"
    )
    running_routes = int(status.get("route_count", 0)) if running else enabled_count
    return f"""
    <p>Mock 服务用于替代被测系统依赖的上游接口、构造错误响应或验证重试逻辑；
    不会替换被测接口本身，也不参与断言判定。</p>
    <div class="context-health">
      <div class="health-card"><span>服务状态</span><strong>{status_label}</strong><small>由当前 Web 进程管理</small></div>
      <div class="health-card"><span>{translate_text('运行路由' if running else '已配置路由')}</span><strong>{running_routes}</strong><small>按配置顺序匹配</small></div>
      <div class="health-card"><span>{translate_text('当前监听' if running else '配置地址')}</span><strong>{esc(actual_address)}</strong><small>默认仅本机访问</small></div>
    </div>
    <form method="post" action="/actions/toggle-mock" style="margin:16px 0">
      {hidden_project(project)}
      <label><input type="checkbox" name="enabled" value="on" {'checked' if running else ''} onchange="this.form.submit()"> 开启 Mock 端口监听</label>
    </form>
    <p>健康检查：<code>/__iatest_mock__/health</code>，请求记录：<code>/__iatest_mock__/requests</code>。
    运行中保存的新配置将在关闭并重新开启后生效；也可以单独运行 <code>{esc(command)}</code>。</p>
    <details class="schedule-detail">
      <summary>编辑接口 Mock 配置</summary>
      <form method="post" action="/actions/save-mock" class="stack-form">
        {hidden_project(project)}
        <label>
          <span>监听地址</span>
          <input name="host" value="{esc(config.get('host', '127.0.0.1'))}" placeholder="127.0.0.1" required>
          <small class="field-help">Mock 服务绑定的网络地址。一般保持 <code>127.0.0.1</code>，表示仅本机可以访问。</small>
        </label>
        <label>
          <span>监听端口</span>
          <input name="port" inputmode="numeric" pattern="[0-9]+" value="{esc(config.get('port', 8780))}" placeholder="8780" required>
          <small class="field-help">可填写 1–65535；填写 <code>0</code> 时由系统临时分配一个空闲端口。</small>
        </label>
        <label>
          <span>单次请求体上限（字节）</span>
          <input name="max_request_bytes" inputmode="numeric" pattern="[0-9]+" value="{esc(config.get('max_request_bytes', 1000000))}" placeholder="1000000" required>
          <small class="field-help">限制 Mock 接收的单个请求体大小，超出后返回 413。默认 1000000 字节，约为 1 MB。</small>
        </label>
        <label>
          <span>请求记录保留条数</span>
          <input name="max_history" inputmode="numeric" pattern="[0-9]+" value="{esc(config.get('max_history', 200))}" placeholder="200" required>
          <small class="field-help">在内存中保留最近多少条 Mock 请求，范围 1–5000；超过后自动移除最早的记录。</small>
        </label>
        <label>
          <span>Mock 路由 JSON</span>
          <textarea name="routes" rows="20" spellcheck="false" placeholder="[]">{esc(routes_json)}</textarea>
          <small class="field-help">必须填写 JSON 数组。数组中的每个对象代表一条 Mock 路由，按照从上到下的顺序匹配请求。</small>
        </label>
        <div class="table-wrap"><table>
          <thead><tr><th>路由字段</th><th>作用</th><th>示例</th></tr></thead>
          <tbody>
            <tr><td><code>name</code></td><td>便于识别的路由名称</td><td>模拟上游服务异常</td></tr>
            <tr><td><code>enabled</code></td><td>是否启用该路由，可省略</td><td><code>true</code></td></tr>
            <tr><td><code>method</code></td><td>需要匹配的 HTTP 方法</td><td><code>GET</code></td></tr>
            <tr><td><code>path</code></td><td>请求路径；支持 <code>{{parameter}}</code></td><td><code>/upstream/users/{{user_id}}</code></td></tr>
            <tr><td><code>match</code></td><td>可选的 Query、Header 或 JSON 请求条件</td><td><code>{{"query": {{"mode": "failure"}}}}</code></td></tr>
            <tr><td><code>response</code></td><td>返回的状态码、响应头、响应体和延迟</td><td><code>{{"status": 503, "body": {{...}}}}</code></td></tr>
          </tbody>
        </table></div>
        <details class="schedule-detail">
          <summary>查看可直接修改的路由 JSON 示例</summary>
          <pre>{esc(route_example)}</pre>
        </details>
        <button type="submit">保存 Mock 配置</button>
      </form>
    </details>
    """


def render_quality_audit_config(project: str, config: dict, sessions: list[dict]) -> str:
    artifacts = config.get("artifacts", {})
    artifact_fields = "".join(
        f"<label><input type='checkbox' name='artifact_{key}' "
        f"{'checked' if artifacts.get(key, True) else ''}> {esc(label)}</label>"
        for key, label in (
            ("endpoint_catalog", "接口信息"),
            ("test_cases", "测试用例"),
            ("test_report", "测试报告"),
            ("experiences", "经验候选"),
        )
    )
    rows = "".join(
        "<tr>"
        f"<td>{esc(item.get('started_at', ''))}</td>"
        f"<td>{esc(quality_artifact_label(item.get('artifact_type', '')))}</td>"
        f"<td>{esc(item.get('candidate_count', 0))}/{esc(item.get('max_rounds', 3))}</td>"
        f"<td>{esc(item.get('unique_model_count', 0))}</td>"
        f"<td>{quality_score_text(item.get('final_score'))}</td>"
        f"<td>{esc(quality_decision_label(item.get('decision', '') or item.get('status', '')))}</td>"
        f"<td><a href='{esc(url_for('/quality-audit/' + str(item.get('id', '')), project))}'>{translate_text('查看详情')}</a></td>"
        "</tr>"
        for item in sessions
    ) or "<tr><td colspan='7'>暂无质量审计记录</td></tr>"
    return f"""
    <p>每个产物由配置中的唯一模型分别审计。唯一模型按 provider、规范化 base_url 和 model 去重；
    每个模型票权相同，使用独立的 quality_reviewer 身份和全新上下文。</p>
    <form method="post" action="/actions/save-quality-audit" class="inline-form">
      {hidden_project(project)}
      <label>总开关<input type="checkbox" name="enabled" {'checked' if config.get('enabled', True) else ''}></label>
      <label>平均分门槛（严格高于）<input type="number" name="threshold" min="0" max="99.99" step="0.01" value="{esc(config.get('threshold', 85))}"></label>
      <label>可靠性最低分<input type="number" name="reliability_min" min="0" max="100" step="0.01" value="{esc(config.get('reliability_min', 90))}"></label>
      <label>完整性最低分<input type="number" name="completeness_min" min="0" max="100" step="0.01" value="{esc(config.get('completeness_min', 85))}"></label>
      <label>模型最大分差<input type="number" name="max_score_spread" min="0" max="100" step="0.01" value="{esc(config.get('max_score_spread', 15))}"></label>
      <label>产出/审计轮数<input type="number" name="max_rounds" min="1" max="3" value="{esc(config.get('max_rounds', 3))}"></label>
      <label>单模型审计最多尝试<input type="number" name="judge_max_attempts" min="1" max="3" value="{esc(config.get('judge_max_attempts', 2))}"></label>
      <label>并行审计<input type="checkbox" name="parallel_judges" {'checked' if config.get('parallel_judges', True) else ''}></label>
      {artifact_fields}
      <button type="submit">保存质量审计配置</button>
    </form>
    <p><small>固定准入规则：全部唯一模型必须完成；存在有证据的 critical 问题直接否决；
    四维权重为可靠性 40%、完整性 30%、合理性 20%、非冗余性 10%。</small></p>
    <h3>最近质量审计</h3>
    <div class="table-wrap"><table><thead><tr><th>时间</th><th>产物</th><th>候选轮数</th>
    <th>唯一模型</th><th>最终分</th><th>决策</th><th>操作</th></tr></thead>
    <tbody>{rows}</tbody></table></div>
    """


def render_quality_audit_detail(
    root: Path,
    project: str,
    session_id: str,
) -> str | None:
    detail = QualityAuditRepository(root, project).session_detail(session_id)
    if detail is None:
        return None
    scores_by_candidate: dict[str, list[dict]] = {}
    for score in detail.get("scores", []):
        scores_by_candidate.setdefault(str(score.get("candidate_id") or ""), []).append(score)
    candidate_blocks: list[str] = []
    selected_id = str(detail.get("selected_candidate_id") or "")
    for candidate in detail.get("candidates", []):
        candidate_id = str(candidate.get("id") or "")
        gate_issues = render_quality_issues(candidate.get("hard_gate_issues") or [])
        score_rows: list[str] = []
        issue_blocks: list[str] = []
        for score in scores_by_candidate.get(candidate_id, []):
            dimensions = [
                quality_score_text(score.get("reliability")),
                quality_score_text(score.get("completeness")),
                quality_score_text(score.get("reasonableness")),
                quality_score_text(score.get("non_redundancy")),
            ]
            score_rows.append(
                "<tr>"
                f"<td>{esc(score.get('provider', ''))} / {esc(score.get('model', ''))}</td>"
                f"<td>{esc(score.get('status', ''))}</td>"
                + "".join(f"<td>{value}</td>" for value in dimensions)
                + f"<td>{quality_score_text(score.get('total_score'))}</td>"
                f"<td>{esc(score.get('confidence', '') or '-')}</td>"
                "</tr>"
            )
            issue_blocks.append(
                f"<details><summary>{esc(score.get('model', ''))}："
                f"{esc(score.get('summary', '') or score.get('error', '') or '无扣分说明')}</summary>"
                f"{render_quality_issues(score.get('issues_json') or [])}"
                f"{'<pre>' + esc(score.get('error', '')) + '</pre>' if score.get('error') else ''}"
                "</details>"
            )
        selected_badge = " · 已选交付产物" if candidate_id == selected_id else ""
        dimension_avg = candidate.get("dimension_averages") or {}
        score_rows_html = "".join(score_rows) or '<tr><td colspan="8">本轮未调用模型审计</td></tr>'
        candidate_blocks.append(
            "<section>"
            f"<h2>第 {esc(candidate.get('round_index', ''))} 轮{esc(selected_badge)}</h2>"
            f"<p>候选：{esc(candidate_id)}；硬门禁：{'通过' if candidate.get('hard_gate_pass') else '未通过'}；"
            f"审计完整：{'是' if candidate.get('audit_complete') else '否'}；"
            f"准入：{'通过' if candidate.get('passed') else '未通过'}；"
            f"平均分：{quality_score_text(candidate.get('average_score'))}；"
            f"分差：{quality_score_text(candidate.get('score_spread'))}</p>"
            f"<p><small>四维均分：可靠性 {quality_score_text(dimension_avg.get('reliability'))}，"
            f"完整性 {quality_score_text(dimension_avg.get('completeness'))}，"
            f"合理性 {quality_score_text(dimension_avg.get('reasonableness'))}，"
            f"非冗余性 {quality_score_text(dimension_avg.get('non_redundancy'))}。"
            f"产物摘要 {esc(candidate.get('artifact_hash', ''))}，证据摘要 {esc(candidate.get('evidence_hash', ''))}</small></p>"
            f"{'<h3>本地硬门禁问题</h3>' + gate_issues if candidate.get('hard_gate_issues') else ''}"
            "<div class='table-wrap'><table><thead><tr><th>审计模型</th><th>状态</th>"
            "<th>可靠性</th><th>完整性</th><th>合理性</th><th>非冗余</th><th>总分</th><th>置信度</th>"
            f"</tr></thead><tbody>{score_rows_html}</tbody></table></div>"
            f"<h3>各模型评分原因</h3>{''.join(issue_blocks) or '<p>无模型评分记录。</p>'}"
            "</section>"
        )
    body = (
        "<section><div class='section-head'><h2>质量审计详情</h2>"
        f"<a class='button-link' href='{esc(url_for('/config', project))}'>返回配置中心</a></div>"
        f"<p>会话：{esc(session_id)}；产物：{esc(quality_artifact_label(detail.get('artifact_type', '')))}；"
        f"状态：{esc(detail.get('status', ''))}；唯一模型：{esc(detail.get('unique_model_count', 0))}；"
        f"最终决策：{esc(quality_decision_label(detail.get('decision', '') or detail.get('status', '')))}；"
        f"最终分：{quality_score_text(detail.get('final_score'))}</p>"
        f"<p>{esc(detail.get('reason', '') or '暂无最终说明')}</p></section>"
        + "".join(candidate_blocks)
    )
    return page(root, project, "质量审计详情", body)


def render_quality_issues(issues: list[dict]) -> str:
    if not issues:
        return "<p>未发现问题。</p>"
    rows = "".join(
        "<tr>"
        f"<td>{esc(item.get('severity', ''))}</td>"
        f"<td>{esc(item.get('dimension', ''))}</td>"
        f"<td>{esc(item.get('location', ''))}</td>"
        f"<td>{esc(item.get('reason', ''))}<br><small>证据：{esc(item.get('evidence', '') or '-')}</small></td>"
        f"<td>{esc(item.get('suggested_fix', '') or '-')}</td>"
        "</tr>"
        for item in issues
        if isinstance(item, dict)
    )
    return (
        "<div class='table-wrap'><table><thead><tr><th>级别</th><th>维度</th><th>位置</th>"
        f"<th>原因与证据</th><th>修复建议</th></tr></thead><tbody>{rows}</tbody></table></div>"
    )


def quality_score_text(value: object) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "-"


def quality_artifact_label(value: object) -> str:
    label = {
        "endpoint_catalog": "接口信息",
        "test_cases": "测试用例",
        "test_report": "测试报告",
        "experiences": "经验候选",
    }.get(str(value), str(value) or "-")
    return translate_text(label)


def quality_decision_label(value: object) -> str:
    label = {
        "accepted": "审计通过",
        "highest_safe_candidate": "最高分安全候选",
        "pending_highest_safe": "最高分安全经验待确认",
        "deterministic_fallback": "确定性产物兜底",
        "factual_report_fallback": "事实报告兜底",
        "no_experience_persisted": "未写入经验",
        "no_safe_artifact": "无安全产物",
        "failed": "失败",
        "running": "进行中",
        "completed": "已完成",
    }.get(str(value), str(value) or "-")
    return translate_text(label)


def display_message_for_tasks(project: str, message: str, actions: list[str], *, root: Path | None = None) -> str:
    if not message:
        return ""
    has_active = bool(root) and any(active_task(root, project, action) for action in actions)
    if is_transient_task_message(message) and not has_active:
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
            "TOOLS_STATUS": "原生工具协议" if config.get("supports_tools", True) else "兼容信封",
            "DIALOG_ID": dialog_id,
            "HIDDEN_PROJECT": hidden_project(project),
            "ROLE": role,
            "PROVIDER_OPTIONS": provider_options,
            "BASE_URL_VALUE": esc(config.get("base_url", "")),
            "MODEL_VALUE": esc(config.get("model", "")),
            "API_KEY_PLACEHOLDER": esc(api_key_placeholder(config.get("api_key_env") or "")),
            "SUPPORTS_TOOLS_CHECKED": "checked" if config.get("supports_tools", True) else "",
        },
    )


def llm_role_from_form(root: Path, role: str, form: dict, existing: dict | None = None) -> dict:
    existing = existing or {}
    base_url = field(form, "base_url")
    model = field(form, "model")
    api_key_env = normalize_api_key_ref(root, role, field(form, "api_key_env") or existing.get("api_key_env", ""))
    provider = normalize_llm_provider(field(form, "provider") or "openai-compatible")
    return {
        **existing,
        "enabled": bool(base_url and model and api_key_env),
        "provider": provider,
        "base_url": normalize_llm_base_url(provider, base_url),
        "model": model,
        "api_key_env": api_key_env,
        "supports_tools": field(form, "supports_tools") == "on",
    }


def render_agent_runtime_config(
    config: dict,
    runs: list[dict],
) -> str:
    role_labels = {
        "api_analyst": "接口分析",
        "test_planner": "覆盖规划",
        "case_designer": "场景设计",
        "case_maintainer": "用例维护",
        "result_analyst": "报告分析",
        "quality_reviewer": "质量审计",
        "context_curator": "经验治理",
    }
    status_labels = {"running": "运行中", "completed": "已完成", "failed": "失败"}
    run_rows = "".join(
        "<tr>"
        f"<td>{esc(item.get('started_at', ''))}</td>"
        f"<td><strong class='runtime-role-name'>{esc(translate_text(role_labels.get(str(item.get('role_id', '')), str(item.get('role_id', '')))))}</strong>"
        f"<small>{esc(item.get('role_id', ''))}@{esc(item.get('role_version', ''))}</small></td>"
        f"<td>{esc(item.get('model', '') or '-')}</td>"
        f"<td>{esc(translate_text(status_labels.get(str(item.get('status', '')), str(item.get('status', '')))))}</td>"
        f"<td>{esc(item.get('total_tokens', 0))}</td>"
        f"<td>{esc(item.get('tool_call_count', 0))}</td>"
        f"<td>{esc(item.get('context_rebase_count', 0))}</td>"
        f"<td>{esc(item.get('evidence_count', 0))}</td>"
        "</tr>"
        for item in runs
    ) or "<tr><td colspan='8'>暂无 Agent 运行记录</td></tr>"
    runtime_config_path = translate_text(f"项目根目录/{PROJECT_CONFIG}")
    mode_keys = [
        ("case_generation_mode", "用例生成"),
        ("api_discovery_mode", "接口识别"),
        ("report_analysis_mode", "报告分析"),
        ("governance_mode", "治理执行器"),
        ("maintenance_mode", "用例维护"),
    ]
    rollback_modes = [
        label
        for key, label in mode_keys
        if str(config.get(key) or "agent").strip().lower() == "legacy"
    ]
    invalid_modes = [
        f"{label}={str(config.get(key) or '')}"
        for key, label in mode_keys
        if str(config.get(key) or "agent").strip().lower() not in {"agent", "legacy"}
    ]
    if invalid_modes:
        current_mode_class = "warning"
        current_mode_title = "检测到无效运行模式"
        current_mode_detail = "、".join(invalid_modes) + "。请在 iatest.json 中改为 agent 或 legacy。"
    elif rollback_modes:
        current_mode_class = "warning"
        current_mode_title = "当前处于部分 Legacy 回滚"
        current_mode_detail = "以下流程正在使用旧链路：" + "、".join(rollback_modes) + "。故障解除后建议在 iatest.json 中恢复为 agent。"
    else:
        current_mode_class = ""
        current_mode_title = "当前全部使用 Agent Runtime"
        current_mode_detail = "接口识别、用例生成、报告分析、治理和用例维护均使用推荐的受限角色与工具链。"
    return f"""
    <div class="runtime-explainer">
      <div><strong>身份与工具隔离</strong><span>不同任务只开放对应角色允许的数据读取和提交工具。</span></div>
      <div><strong>本地编译与门禁</strong><span>模型提交语义结果，框架负责格式编译、校验和质量拦截。</span></div>
      <div><strong>运行审计</strong><span>记录角色、模型、Token、工具调用、上下文恢复和证据数量。</span></div>
    </div>
    <div class="runtime-mode-comparison">
      <div class="recommended">
        <span class="runtime-mode-badge">推荐</span>
        <strong>Agent 模式</strong>
        <p>按任务分配受限角色，通过多轮工具读取证据并提交结构化结果，再由本地编译器和质量门禁校验；运行过程可审计、可恢复。</p>
      </div>
      <div>
        <span class="runtime-mode-badge">兼容回滚</span>
        <strong>Legacy 模式</strong>
        <p>绕过 Agent Runtime，使用旧版直接提示词和结果解析链路；仅在模型不兼容工具调用或新版链路异常时临时使用。</p>
      </div>
    </div>
    <div class="runtime-block">
      <div class="runtime-block-head">
        <div><h3>当前运行状态</h3><p>Web 页面只读展示运行架构；模式切换仅允许通过项目 JSON 完成。</p></div>
      </div>
      <div class="runtime-current-mode {current_mode_class}">
        <strong>{esc(current_mode_title)}</strong>
        <p>{esc(current_mode_detail)}</p>
      </div>
    </div>
    <details class="runtime-advanced-note">
      <summary>高级参数改用 JSON 配置</summary>
      <div>
        <p>批处理数量、语义修订次数和上下文恢复水位不再通过 Web 修改，避免误调导致调用次数、成本或稳定性异常。</p>
        <p><strong>配置文件：</strong><code>{esc(runtime_config_path)}</code></p>
        <dl class="runtime-json-groups">
          <div><dt>运行链路回滚</dt><dd><code>agents.case_generation_mode</code>、<code>agents.api_discovery_mode</code>、<code>agents.report_analysis_mode</code>、<code>agents.governance_mode</code>、<code>agents.maintenance_mode</code>；正常值为 <code>agent</code>，故障回滚时临时改为 <code>legacy</code>。</dd></div>
          <div><dt>批次与修订</dt><dd><code>agents.planner_batch_endpoints</code>、<code>agents.designer_batch_endpoints</code>、<code>agents.designer_batch_scenarios</code>、<code>agents.semantic_repair_attempts</code></dd></div>
          <div><dt>上下文保护</dt><dd><code>agents.history_max_chars</code> 及 <code>agents.context_recovery</code> 下的恢复水位、目标水位、熔断和安全余量。</dd></div>
        </dl>
      </div>
    </details>
    <div class="runtime-run-panel">
      <div class="runtime-block-head">
        <div><h3>最近 Agent 运行审计</h3><p>这里只展示运行结果，不属于配置项。</p></div>
      </div>
      <div class="table-wrap"><table><thead><tr><th>时间</th><th>角色</th><th>模型</th><th>状态</th><th>Token</th><th>工具调用</th><th>上下文恢复</th><th>保存证据</th></tr></thead><tbody>{run_rows}</tbody></table></div>
    </div>
    """


def bounded_form_int(value: str, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def bounded_form_float(value: str, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def normalize_api_key_ref(root: Path, role: str, value: str) -> str:
    stripped = value.strip()
    if not stripped:
        return ""
    if looks_like_api_key(stripped):
        env_name = {
            "master": "IATEST_MASTER_KEY",
            "case_generator": "IATEST_CASE_GENERATOR_KEY",
            "analyzer": "IATEST_ANALYZER_KEY",
        }.get(role, "IATEST_LLM_API_KEY")
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
    atomic_write_text(path, "\n".join(lines) + "\n")


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


_GOVERNANCE_TOGGLE_KEYS = {
    "enabled",
    "review_reports",
    "review_cases",
    "review_experience_candidates",
    "low_confidence_requires_manual",
}


def _governance_toggle_form(project: str, key: str, current: bool) -> str:
    label = "停用" if current else "启用"
    value = "off" if current else "on"
    return (
        f"<form method='post' action='/actions/governance-toggle' style='display:inline;margin:0'>"
        f"{hidden_project(project)}<input type='hidden' name='key' value='{esc(key)}'>"
        f"<input type='hidden' name='value' value='{esc(value)}'>"
        f"<button class='ghost' style='min-height:28px;font-size:12px'>{esc(label)}</button></form>"
    )


def _governance_number_form(project: str, key: str, current: int) -> str:
    return (
        "<form method='post' action='/actions/governance-save-number' class='inline-form' style='margin:0'>"
        f"{hidden_project(project)}<input type='hidden' name='key' value='{esc(key)}'>"
        f"<input type='number' name='value' value='{current}' min='1' max='100' style='width:80px'>"
        "<button class='ghost' style='min-height:28px;font-size:12px'>保存</button></form>"
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


def save_endpoints(
    root: Path,
    project: str,
    old_endpoints: list[Endpoint],
    endpoints: list[Endpoint],
    *,
    actor: str,
) -> None:
    validate_endpoint_collection(endpoints)
    write_json(project_docs_file(root, project), to_jsonable(endpoints))
    from ai_apitest.domain.contracts import TRUSTED_CONTRACT_VERSION, contract_hash

    metadata = read_json(project_metadata_file(root, project), {})
    metadata.update(
        {
            "contract_version": TRUSTED_CONTRACT_VERSION,
            "contract_hash": contract_hash(endpoints),
            "contract_status": "trusted_manual",
            "business_rule_count": sum(len(item.business_rules) for item in endpoints),
        }
    )
    write_json(project_metadata_file(root, project), metadata)
    changed = diff_endpoints(old_endpoints, endpoints)
    affected = [*changed.get("removed", []), *changed.get("changed", [])]
    if affected:
        mark_lessons_for_endpoint_changes(root, project, affected, actor=actor)
    synchronize_project_dependencies(root, project, old_endpoints, endpoints, actor=actor)


def case_from_form(existing, form: dict):
    title = field(form, "title").strip()
    expected_result = field(form, "expected_result").strip()
    if not title:
        raise ValueError("用例名称不能为空")
    if not expected_result:
        raise ValueError("预期结果不能为空")

    request_data = parse_json_field(field(form, "request_data"), {})
    assertions = parse_json_field(field(form, "assertions"), [])
    setup_steps = parse_json_field(field(form, "setup_steps"), [])
    chain_steps = parse_json_field(field(form, "chain_steps"), [])
    cleanup_steps = parse_json_field(field(form, "cleanup_steps"), [])
    if not isinstance(request_data, dict):
        raise ValueError("请求数据必须是 JSON 对象")
    for label, value in (
        ("断言列表", assertions),
        ("前置准备步骤", setup_steps),
        ("链路步骤", chain_steps),
        ("清理步骤", cleanup_steps),
    ):
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise ValueError(f"{label}必须是 JSON 对象数组")

    method = str(request_data.get("method") or existing.method).upper()
    path = str(request_data.get("path") if "path" in request_data else existing.path)
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "CHAIN"}:
        raise ValueError(f"不支持的用例方法：{method}")
    if method != "CHAIN" and (not path.startswith("/") or re.search(r"\s", path)):
        raise ValueError("用例路径必须以 / 开头且不能包含空白字符")

    payload = to_jsonable(existing)
    payload.update(
        {
            "id": existing.id,
            "title": title,
            "method": method,
            "path": path,
            "priority": field(form, "priority") or existing.priority,
            "design_method": field(form, "design_method") or existing.design_method,
            "risk": field(form, "risk") or existing.risk,
            "runnable": field(form, "runnable") == "on",
            "negative": field(form, "negative") == "on",
            "tags": split_form_lines(field(form, "tags"), allow_commas=True),
            "preconditions": split_form_lines(field(form, "preconditions")),
            "test_steps": split_form_lines(field(form, "test_steps")),
            "expected_result": expected_result,
            "headers": request_data.get("headers", {}),
            "path_params": request_data.get("path_params", {}),
            "query": request_data.get("query", {}),
            "parameter_serialization": request_data.get("parameter_serialization", {}),
            "body": request_data.get("body"),
            "assertions": assertions,
            "setup_steps": setup_steps,
            "steps": chain_steps,
            "cleanup_steps": cleanup_steps,
        }
    )
    from ai_apitest.core.models import execution_case_options
    for name in execution_case_options(existing):
        if name in request_data:
            payload[name] = request_data[name]
    if any(type(payload[name]) is not bool for name in ("parallel_safe", "retry_safe")):
        raise ValueError("parallel_safe 和 retry_safe 必须是布尔值")
    if any(not isinstance(payload[name], dict) for name in ("variables", "poll", "data_generation")):
        raise ValueError("variables、poll 和 data_generation 必须是 JSON 对象")
    if type(payload["samples"]) is not int or not 1 <= payload["samples"] <= 10000:
        raise ValueError("samples 必须为 1 至 10000")
    if not isinstance(payload["resources"], list) or any(not isinstance(name, str) or not name for name in payload["resources"]):
        raise ValueError("resources 必须是非空资源名称数组")
    for name in ("headers", "path_params", "query", "parameter_serialization"):
        if not isinstance(payload[name], dict):
            raise ValueError(f"请求数据中的 {name} 必须是 JSON 对象")
    return case_from_dict(payload)


def split_form_lines(value: str, *, allow_commas: bool = False) -> list[str]:
    text = value.replace(",", "\n") if allow_commas else value
    return [item.strip() for item in text.splitlines() if item.strip()]


def endpoint_from_form(root: Path, project: str, form: dict, *, existing: Endpoint | None = None) -> Endpoint:
    parameters = parse_json_field(field(form, "parameters"), [])
    if not isinstance(parameters, list) or any(not isinstance(item, dict) for item in parameters):
        raise ValueError("接口参数必须是 JSON 对象数组")
    request_body = request_body_from_form(root, project, form)
    method = field(form, "method").upper() or "GET"
    path = field(form, "path") or "/"
    allowed_methods = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
    if method not in allowed_methods:
        raise ValueError(f"不支持的 HTTP 方法：{method}")
    if not path.startswith("/") or re.search(r"\s", path):
        raise ValueError("接口路径必须以 / 开头且不能包含空白字符")
    if path.count("{") != path.count("}") or any(
        not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
        for name in re.findall(r"{([^{}]*)}", path)
    ):
        raise ValueError("接口路径参数必须使用合法的 {name} 格式")
    return Endpoint(
        method=method,
        path=path,
        summary=field(form, "summary"),
        description=existing.description if existing else "",
        parameters=parameters,
        request_body=request_body,
        responses=existing.responses if existing else {},
        tags=existing.tags if existing else [safe_project_name(field(form, "summary") or "api")],
        operation_id=existing.operation_id if existing else "",
        deprecated=existing.deprecated if existing else False,
        security=existing.security if existing else [],
        business_rules=existing.business_rules if existing else [],
    )


def validate_endpoint_collection(endpoints: list[Endpoint]) -> None:
    seen: set[tuple[str, str]] = set()
    duplicates: list[str] = []
    for endpoint in endpoints:
        key = (endpoint.method.upper(), endpoint.path)
        if key in seen:
            duplicates.append(f"{key[0]} {key[1]}")
        seen.add(key)
    if duplicates:
        raise ValueError(f"接口 method/path 重复：{', '.join(sorted(set(duplicates)))}")


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
    if mode in {"json", "form", "multipart"}:
        media_type = {
            "json": "application/json",
            "form": "application/x-www-form-urlencoded",
            "multipart": "multipart/form-data",
        }[mode]
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
        try:
            content = base64.b64decode(payload, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise ValueError("请求体文件不是有效的 Base64 数据") from exc
        atomic_write_bytes(path, content)
    else:
        atomic_write_text(path, data_url)
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
    options = "".join(
        f"<option value='{method}' {'selected' if method == current else ''}>{method}</option>"
        for method in ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    )
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
        <option value="multipart" {'selected' if mode == 'multipart' else ''}>Multipart 表单</option>
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
    if mode in {"json", "form", "multipart", "file"}:
        return mode
    content = data.get("content") if isinstance(data, dict) else {}
    if not isinstance(content, dict) or not content:
        return ""
    if "application/json" in content:
        return "json"
    if "multipart/form-data" in content:
        return "multipart"
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
        <small>v1.0.3 · 项目：{esc(project)}</small>
      </div>
    </aside>
    """
    content = render_template(
        "base.html",
        {
            "TITLE": esc(title),
            "PROJECT": esc(project),
            "HTML_LANG": current_html_lang(),
            "LOCALE": current_locale(),
            "SHELL_CLASS": shell_class,
            "SIDEBAR": sidebar,
            "PROJECT_OPTIONS": project_options(root, project),
            "LANGUAGE_OPTIONS": language_options(),
            "PROJECT_DIALOG": render_project_dialog(project),
            "BODY": body,
        },
    )
    return protect_post_forms(localize_html(content))


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
    return is_report_filename(name)


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
