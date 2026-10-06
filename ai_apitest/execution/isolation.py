"""Run-only workers: the parent owns project files, locks and result persistence."""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from ai_apitest.core.models import CaseResult
from ai_apitest.core.sandbox import SandboxPolicy, SandboxViolation, policy_scope, read_file, string_list, positive
from ai_apitest.core.files import atomic_write_text
from ai_apitest.execution.control import RunControl


def worker_environment(settings: dict, temporary: Path) -> dict[str, str]:
    # No .env reload, PYTHONPATH, proxy, cloud or LLM credentials inherited.
    names = {"PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL"} | set(string_list(settings, "env_allowlist"))
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in names):
        raise ValueError("sandbox.env_allowlist 包含非法环境变量名称")
    result = {name: os.environ[name] for name in names if name in os.environ}
    result.update({"HOME": str(temporary), "USERPROFILE": str(temporary), "TMPDIR": str(temporary), "TMP": str(temporary), "TEMP": str(temporary), "PYTHONDONTWRITEBYTECODE": "1"})
    return result


def stop_worker(process: subprocess.Popen, grace: float) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=max(1, grace))
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def prepare_container(payload: dict, cases: list, environment: dict, policy: SandboxPolicy, input_dir: Path, max_input: int) -> int:
    """Mount only required inputs, never a project directory containing secrets."""
    files = input_dir / "files"
    files.mkdir()
    total = 0
    copied: dict[tuple[str, bool], str] = {}

    def stage(value: str, context: dict, *, tls: bool = False) -> str:
        nonlocal total
        from ai_apitest.execution.runner import render_template
        value = str(render_template(value, context))
        if "{{" in value:
            raise SandboxViolation("容器文件路径需由用例 variables 提供，不能依赖运行时提取变量")
        path = Path(value).expanduser()
        path = (policy.root / path).resolve()
        key = (str(path), tls)
        if key in copied:
            return copied[key]
        with policy_scope(None if tls else policy):
            content = read_file(path, min(max_input, 20_000_000))
        total += len(content)
        if total > max_input:
            raise ValueError("容器输入文件总大小超过 sandbox.max_input_bytes 限制")
        target = files / f"{len(copied)}-{re.sub(r'[^a-zA-Z0-9_.-]', '_', path.name)}"
        target.write_bytes(content)
        target.chmod(0o600)
        copied[key] = "/input/files/" + target.name
        return copied[key]

    def rewrite(item, context):
        body = item.get("body")
        if isinstance(body, dict) and set(body) == {"$iatest_file"}:
            item["body"] = {"$iatest_file": stage(str(body["$iatest_file"]), context)}
        headers = {**environment.get("headers", {}), **item.get("headers", {})}
        content_type = next((str(value) for key, value in headers.items() if key.lower() == "content-type"), "")
        if content_type.split(";", 1)[0].strip().lower() == "multipart/form-data" and isinstance(body, dict):
            for value in body.values():
                if isinstance(value, dict) and value.get("file_path") and "content" not in value:
                    value["file_path"] = stage(str(value["file_path"]), context)

    for raw, case in zip(payload["cases"], cases):
        context = {**case.variables, "iatest_namespace": payload["config"]["execution"]["namespace"]}
        for item in [raw, *raw["steps"], *raw["setup_steps"], *raw["cleanup_steps"]]:
            rewrite(item, context)
    selected = payload["environment"]
    for steps in (selected.get("setup_steps", []), selected.get("teardown_steps", [])):
        for item in steps:
            rewrite(item, {"iatest_namespace": payload["config"]["execution"]["namespace"]})
    if isinstance(selected.get("verify_tls"), str):
        selected["verify_tls"] = stage(selected["verify_tls"], {}, tls=True)
    cert = selected.get("client_cert")
    tls_denied = []
    if isinstance(cert, str):
        selected["client_cert"] = stage(cert, {}, tls=True)
        tls_denied.append(selected["client_cert"])
    elif isinstance(cert, dict):
        for key in ("certfile", "keyfile"):
            if cert.get(key):
                cert[key] = stage(cert[key], {}, tls=True)
                tls_denied.append(cert[key])
    payload["policy"] = {**policy.to_dict(), "root": "/", "read_paths": ["/input/files"], "denied_paths": tls_denied, "project": ""}
    return total


def container_command(settings: dict, input_dir: Path, output_dir: Path, name: str) -> list[str]:
    image = settings.get("container_image", "")
    network = settings.get("container_network", "none")
    if not isinstance(image, str) or not image or image.startswith("-"):
        raise ValueError("容器模式需要 sandbox.container_image，镜像须预先安装框架及插件")
    if not isinstance(network, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]+", network) or network in {"host", "bridge", "default"}:
        raise ValueError("容器网络仅支持 none 或预先创建的 internal 测试网络")
    if any("," in str(path) or "\n" in str(path) for path in (input_dir, output_dir)):
        raise ValueError("Docker 挂载路径不能包含逗号或换行")
    memory = int(positive(settings, "memory_mb", 512))
    pids = int(positive(settings, "pids_limit", 64))
    if memory < 64 or pids < 8:
        raise ValueError("容器至少需要 64MB 内存和 8 个进程/线程配额")
    uid = os.getuid() if hasattr(os, "getuid") else 65532
    gid = os.getgid() if hasattr(os, "getgid") else 65532
    if uid == 0:
        uid = gid = 65532
        for directory in (input_dir,):
            for path in [directory, *directory.rglob("*")]:
                os.chown(path, uid, gid)
    output_size = int(positive(settings, "max_output_bytes", 64_000_000))
    return ["docker", "run", "--rm", "--pull", "never", "--name", name, "--init", "--log-driver", "none", "--read-only", "--user", f"{uid}:{gid}", "--cap-drop", "ALL", "--security-opt", "no-new-privileges=true", "--memory", f"{memory}m", "--memory-swap", f"{memory}m", "--cpus", str(positive(settings, "cpus", 1)), "--pids-limit", str(pids), "--network", network, "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", "--tmpfs", f"/output:rw,noexec,nosuid,size={output_size},uid={uid},gid={gid},mode=700", "--mount", f"type=bind,src={input_dir},dst=/input,readonly", "--workdir", "/tmp", "--env-file", str(input_dir / "worker.env"), "--entrypoint", "python", image, "-I", "-B", "-m", "ai_apitest.execution.worker", "/input/request.json", "/output"]


def collect_container_output(stream, output: Path, case_count: int, limit: int, errors: list[str]) -> None:
    """Only the controller writes host files. Bound protocol, lines and results."""
    buffered = bytearray()
    consumed = 0
    sizes = {}
    try:
        while chunk := stream.read(8192):
            consumed += len(chunk)
            buffered.extend(chunk)
            if consumed > limit * 4 + 65536 or len(buffered) > limit + 65536:
                raise ValueError("容器输出超过大小限制")
            while b"\n" in buffered:
                line, _, remaining = buffered.partition(b"\n")
                buffered = bytearray(remaining)
                if line == b"IATEST_COMPLETE":
                    (output / "complete").touch()
                elif line.startswith(b"IATEST_ERROR "):
                    raw = json.loads(line[len(b"IATEST_ERROR "):])
                    atomic_write_text(output / "error.json", json.dumps({"error": str(raw["error"])[:2000]}))
                elif line.startswith(b"IATEST_RESULT "):
                    index, _, raw = line[len(b"IATEST_RESULT "):].partition(b" ")
                    index = int(index)
                    if not 0 <= index < case_count:
                        raise ValueError("容器返回了未知用例索引")
                    sizes[index] = len(raw)
                    if sum(sizes.values()) > limit:
                        raise ValueError("容器结果超过输出大小限制")
                    json.loads(raw)
                    atomic_write_text(output / f"{index}.json", raw.decode("utf-8"))
                else:
                    raise ValueError("容器输出协议无效")
        if buffered:
            raise ValueError("容器输出协议不完整")
    except (ValueError, OSError, KeyError) as exc:
        errors.append(str(exc))


def run_isolated(cases: list, environment: dict, config: dict, control: RunControl, policy: SandboxPolicy, rate_policies: list, *, operation: str = "run", fixture_options: dict | None = None) -> tuple[list[CaseResult], dict]:
    settings = config.get("sandbox", {})
    timeout = positive(settings, "hard_timeout_seconds", 300)
    if control.timeout_seconds is not None:
        timeout = min(timeout, max(0.001, control.timeout_seconds - (time.monotonic() - control.started)))
    grace = min(positive(settings, "termination_grace_seconds", 5), 30)
    max_output = int(positive(settings, "max_output_bytes", 64_000_000))
    max_input = int(positive(settings, "max_input_bytes", 64_000_000))
    worker_config = {key: config[key] for key in ("execution", "fixtures", "plugins", "sandbox") if key in config}
    if "fixtures" in worker_config:
        from ai_apitest.execution.fixtures import fixture_provider_config
        needed = {name for case in cases for name in case.fixture_requirements}
        fixtures = worker_config["fixtures"]
        worker_config["fixtures"] = {**fixtures, "providers": {name: fixture_provider_config(config, name) or provider for name, provider in fixtures.get("providers", {}).items() if name in needed}}
    # Only fixture credentials needed for this run are resolved, never unrelated LLM settings.
    payload = {"cases": [asdict(case) for case in cases], "environment": json.loads(json.dumps(environment)), "config": worker_config, "policy": policy.to_dict(), "rate_policies": [asdict(item) for item in rate_policies], "timeout": timeout, "operation": operation, "fixture_options": fixture_options or {}}
    reason = ""
    forced = False
    complete = False
    with tempfile.TemporaryDirectory(prefix="iatest-run-") as temporary:
        work = Path(temporary)
        input_dir, output_dir = work / "input", work / "output"
        input_dir.mkdir(mode=0o700)
        output_dir.mkdir(mode=0o700)
        env = worker_environment(settings, work)
        name = "iatest-" + uuid.uuid4().hex
        if policy.mode == "container":
            if not shutil.which("docker"):
                raise ValueError("容器模式需要已安装并运行的 Docker；不会自动降低隔离级别")
            network = settings.get("container_network", "none")
            if network != "none":
                probe = subprocess.run(["docker", "network", "inspect", "--format", "{{.Internal}}", str(network)], capture_output=True, text=True, timeout=10)
                if probe.returncode or probe.stdout.strip() != "true":
                    raise ValueError("容器模式仅允许 internal 测试网络；外部访问需接入受控出口代理")
            max_input -= prepare_container(payload, cases, environment, policy, input_dir, max_input)
            container_env = {key: value for key, value in env.items() if key in string_list(settings, "env_allowlist")}
            container_env.update({"HOME": "/tmp", "TMPDIR": "/tmp", "PYTHONDONTWRITEBYTECODE": "1"})
            if any("\n" in value or "\r" in value for value in container_env.values()):
                raise ValueError("容器环境变量不能包含换行")
            (input_dir / "worker.env").write_text("".join(f"{key}={value}\n" for key, value in container_env.items()))
            (input_dir / "worker.env").chmod(0o600)
        request = input_dir / "request.json"
        # Avoid constructing an unbounded serialized packet in the controller.
        with request.open("wb") as file:
            size = 0
            for part in json.JSONEncoder(ensure_ascii=False).iterencode(payload):
                encoded = part.encode("utf-8")
                size += len(encoded)
                if size > max_input:
                    raise ValueError("隔离输入超过 sandbox.max_input_bytes 限制")
                file.write(encoded)
        request.chmod(0o600)
        if policy.mode == "container":
            command = container_command(settings, input_dir, output_dir, name)
            process_env = None  # Docker CLI needs the operator's engine/context; these are not passed to the container.
        else:
            source = str(Path(__file__).resolve().parents[2])
            bootstrap = f"import sys; sys.path.insert(0, {source!r}); from ai_apitest.execution.worker import main; main()"
            command = [sys.executable, "-I", "-B", "-c", bootstrap, str(request), str(output_dir)]
            process_env = env
        process = subprocess.Popen(command, cwd=work, env=process_env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE if policy.mode == "container" else subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=os.name == "posix", bufsize=0)
        close_job = None
        if os.name == "nt":
            from ai_apitest.execution.windows_job import attach_job
            try:
                close_job = attach_job(process, settings)
            except OSError as exc:
                stop_worker(process, 0.2)
                raise ValueError("无法创建 Windows 执行隔离 Job Object，不会降级运行") from exc
        (output_dir / "start").touch()
        protocol_errors = []
        collector = None
        if policy.mode == "container":
            collector = threading.Thread(target=collect_container_output, args=(process.stdout, output_dir, len(cases), max_output, protocol_errors), daemon=True)
            collector.start()
        started = time.monotonic()
        cancelled_at = None
        try:
            while process.poll() is None:
                reason = control.reason() or (protocol_errors[0] if protocol_errors else "") or ("隔离执行超过硬时限" if time.monotonic() - started >= timeout else "")
                if reason:
                    if cancelled_at is None:
                        (output_dir / "cancel").touch()
                        cancelled_at = time.monotonic()
                        if policy.mode == "container":
                            try:
                                subprocess.run(["docker", "exec", name, "python", "-I", "-c", "from pathlib import Path; Path('/output/cancel').touch()"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=min(grace, 0.5))
                            except (OSError, subprocess.TimeoutExpired):
                                pass
                    if time.monotonic() - cancelled_at >= grace:
                        forced = True
                        stop_worker(process, 0.2)
                        break
                time.sleep(0.05)
            if collector:
                collector.join(1)
                if protocol_errors:
                    reason = protocol_errors[0]
            complete = (output_dir / "complete").is_file() and not forced and not protocol_errors and process.returncode == 0
            if not reason and (process.returncode or not complete):
                reason = f"隔离工作进程异常退出（exit={process.returncode}）"
                error_file = output_dir / "error.json"
                if error_file.is_file() and not error_file.is_symlink():
                    with policy_scope(None):
                        try:
                            reason += ": " + str(json.loads(read_file(error_file, 16000)).get("error", ""))[:2000]
                        except (ValueError, OSError):
                            pass
            results = []
            total_bytes = 0
            for index, case in enumerate(cases):
                checkpoint = output_dir / f"{index}.json"
                result = None
                if checkpoint.is_file() and not checkpoint.is_symlink():
                    total_bytes += checkpoint.stat().st_size
                    if total_bytes > max_output:
                        raise ValueError("隔离结果超过 sandbox.max_output_bytes 限制")
                    try:
                        with policy_scope(None):
                            raw = json.loads(read_file(checkpoint, max_output))
                        result = CaseResult(**raw)
                        if result.case_id != case.id or result.status not in {"passed", "failed", "skipped", "cancelled"}:
                            raise ValueError("工作进程返回了无效的用例结果")
                    except (ValueError, TypeError, OSError):
                        reason = "隔离工作进程返回了损坏的结果"
                        result = None
                if result is None:
                    cancelled = bool(control.reason()) or "时限" in reason
                    result = CaseResult(case.id, case.title, "cancelled" if cancelled else "failed", case.method, case.path, None, 0, error=reason or "隔离工作进程未返回结果", request={"blocked": True}, cleanup={"status": "skipped", "reason": "工作进程中断，无法确认远端数据清理完成"})
                elif not complete and environment.get("teardown_steps"):
                    result.cleanup = {**result.cleanup, "status": "failed", "isolation_interrupted": True, "reason": "工作进程中断，无法确认环境清理完成"}
                    if result.status == "passed":
                        result.status = "cancelled" if control.reason() or "时限" in reason else "failed"
                    result.error = result.error or reason
                results.append(result)
            if not complete and results and all(result.status == "passed" for result in results):
                result = results[-1]
                result.status = "cancelled" if control.reason() or "时限" in reason else "failed"
                result.error = reason or "隔离工作进程未完成退出流程"
                result.cleanup = {**result.cleanup, "status": "failed", "isolation_interrupted": True}
            return results, {"forced_termination": forced, "completed": complete, "interruption": reason}
        finally:
            if close_job:
                close_job()
            stop_worker(process, 0.2)
            if policy.mode == "container":
                subprocess.run(["docker", "rm", "--force", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                if process.stdout:
                    process.stdout.close()
                if collector:
                    collector.join(1)
