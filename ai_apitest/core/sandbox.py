"""Application access policy. OS isolation is provided by execution/isolation.py."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import ipaddress
import math
import os
from pathlib import Path
import socket
import stat
from typing import Any

import httpx

from ai_apitest.core.paths import project_dir


class SandboxViolation(PermissionError):
    pass


ACTIVE_POLICY: ContextVar[SandboxPolicy | None] = ContextVar("iatest_sandbox", default=None)
MODES = {"off", "policy", "process", "container"}


def origin(value: str) -> str:
    try:
        url = httpx.URL(value)
        if url.scheme not in {"http", "https"} or not url.host or url.userinfo or url.fragment:
            raise ValueError
        port = url.port if url.port is not None else (443 if url.scheme == "https" else 80)
        host = f"[{url.host}]" if ":" in url.host else url.host
        return f"{url.scheme}://{host}:{port}"
    except (ValueError, httpx.InvalidURL) as exc:
        raise SandboxViolation("沙箱仅允许不含用户凭据和片段的 HTTP/HTTPS 地址") from exc


def string_list(settings: dict, key: str) -> list[str]:
    value = settings.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"sandbox.{key} 必须是非空字符串的数组")
    return value


def positive(settings: dict, key: str, default: float) -> float:
    value = settings.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"sandbox.{key} 必须是大于 0 的有限数值")
    return float(value)


@dataclass(frozen=True)
class SandboxPolicy:
    root: Path
    read_paths: tuple[Path, ...]
    denied_paths: tuple[Path, ...]
    allowed_origins: frozenset[str]
    allowed_networks: tuple[Any, ...] = ()
    mode: str = "policy"
    allow_plugins: bool = False
    project: str = ""

    @classmethod
    def from_config(cls, root: Path, config: dict, *, project: str = "", base_url: str = "", source: str = "", allow_source_file: bool = False, allow_source_url: bool = False):
        settings = config.get("sandbox", {})
        if not isinstance(settings, dict):
            raise ValueError("sandbox 必须是配置对象")
        mode = settings.get("mode", "policy")
        if not isinstance(mode, str) or mode not in MODES:
            raise ValueError("sandbox.mode 仅支持 off/policy/process/container")
        allow_plugins = settings.get("allow_plugins", False)
        if not isinstance(allow_plugins, bool):
            raise ValueError("sandbox.allow_plugins 必须是布尔值")
        root = root.resolve()
        paths = string_list(settings, "read_paths")
        reads = tuple((root / Path(item).expanduser()).resolve() for item in paths) if paths else (root,)
        if source and not source.startswith(("http://", "https://")) and allow_source_file:
            reads += ((root / Path(source).expanduser()).resolve().parent,)
        denied = tuple((root / Path(item).expanduser()).resolve() for item in string_list(settings, "denied_read_paths"))
        origins = string_list(settings, "allowed_origins")
        origins += [value for value in (base_url, source if allow_source_url and source.startswith(("http://", "https://")) else "") if value]
        networks = tuple(ipaddress.ip_network(item, strict=False) for item in string_list(settings, "allowed_networks"))
        return cls(root, reads, denied, frozenset(origin(value) for value in origins), networks, mode, allow_plugins, project)

    def check_path(self, value: str | Path) -> Path:
        path = (self.root / Path(value).expanduser()).resolve()
        if self.mode == "off":
            return path
        if not any(path == allowed or (allowed.is_dir() and path.is_relative_to(allowed)) for allowed in self.read_paths):
            raise SandboxViolation("沙箱拒绝读取允许目录之外的文件")
        sensitive = {".git", ".ssh", ".aws", ".gnupg", ".iatest", ".venv", "iatest.json"}
        if any(part in sensitive or part == ".env" or part.startswith(".env.") for part in path.parts):
            raise SandboxViolation("沙箱拒绝读取配置、凭据或版本库文件")
        if any(path == denied or path.is_relative_to(denied) for denied in self.denied_paths):
            raise SandboxViolation("沙箱拒绝读取禁止的文件")
        projects = self.root / "projects"
        if self.project and path.is_relative_to(projects) and not path.is_relative_to(project_dir(self.root, self.project)):
            raise SandboxViolation("沙箱拒绝读取其他项目的文件")
        return path

    def check_url(self, value: str) -> None:
        if self.mode == "off":
            return
        normalized = origin(value)
        if normalized not in self.allowed_origins:
            raise SandboxViolation(f"沙箱拒绝访问未授权的地址：{normalized}")
        if self.allowed_networks:
            url = httpx.URL(value)
            addresses = socket.getaddrinfo(url.host, url.port if url.port is not None else (443 if url.scheme == "https" else 80), type=socket.SOCK_STREAM)
            if not addresses or any(not any(ipaddress.ip_address(item[4][0].split("%")[0]) in network for network in self.allowed_networks) for item in addresses):
                raise SandboxViolation("沙箱拒绝访问允许网段之外的地址")

    def request_hook(self, request: httpx.Request) -> None:
        self.check_url(str(request.url))

    def to_dict(self) -> dict:
        return {"root": str(self.root), "read_paths": [str(path) for path in self.read_paths], "denied_paths": [str(path) for path in self.denied_paths], "allowed_origins": sorted(self.allowed_origins), "allowed_networks": [str(item) for item in self.allowed_networks], "mode": self.mode, "allow_plugins": self.allow_plugins, "project": self.project}

    @classmethod
    def from_dict(cls, value: dict):
        return cls(Path(value["root"]), tuple(Path(item) for item in value["read_paths"]), tuple(Path(item) for item in value["denied_paths"]), frozenset(value["allowed_origins"]), tuple(ipaddress.ip_network(item) for item in value["allowed_networks"]), value["mode"], value["allow_plugins"], value.get("project", ""))


@contextmanager
def policy_scope(policy: SandboxPolicy | None):
    token = ACTIVE_POLICY.set(policy)
    try:
        yield policy
    finally:
        ACTIVE_POLICY.reset(token)


def check_url(value: str) -> None:
    if policy := ACTIVE_POLICY.get():
        policy.check_url(value)


def read_file(value: str | Path, max_bytes: int) -> bytes:
    """Bound reads even when a file grows; avoid blocking on pipes/devices."""
    policy = ACTIVE_POLICY.get()
    path = policy.check_path(value) if policy else Path(value).expanduser().resolve()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags), "rb") as file:
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            raise SandboxViolation("沙箱仅允许读取普通文件")
        content = file.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise ValueError(f"文件超过 {max_bytes} 字节限制：{path.name}")
    return content
