"""Versioned project knowledge, discovered by metadata and read on demand."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import yaml

from ai_apitest.core.config import load_config
from ai_apitest.core.paths import safe_project_name
from ai_apitest.core.sandbox import SandboxPolicy, policy_scope, read_file
from ai_apitest.core.redaction import redact_text


class RecipeCatalog:
    def __init__(self, root: Path, project: str, config: dict | None = None):
        self.root = root
        self.project = safe_project_name(project)
        config = config or load_config(root)
        paths = config.get("agents", {}).get("recipes", {}).get("paths", [])
        if not isinstance(paths, list) or len(paths) > 50 or any(not isinstance(path, str) for path in paths):
            raise ValueError("配方 paths 必须为有界路径数组")
        self.items = {}
        policy = SandboxPolicy.from_config(root, config, project=self.project)
        with policy_scope(policy):
            for source in paths:
                base = policy.check_path(root / source)
                files = [base] if base.is_file() else [base / "SKILL.md", *sorted(base.glob("*/SKILL.md"))[:101]]
                for path in files:
                    if not path.exists():
                        continue
                    if len(self.items) >= 100:
                        raise ValueError("配方数量超过 100")
                    raw = read_file(path, 64000).decode("utf-8")
                    parts = raw.split("---", 2)
                    if len(parts) != 3 or parts[0].strip():
                        raise ValueError("配方需要 YAML front matter")
                    header = yaml.safe_load(parts[1])
                    if not isinstance(header, dict):
                        raise ValueError("配方元数据必须为对象")
                    name, description = header.get("name"), header.get("description")
                    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name) or not isinstance(description, str) or not 1 <= len(description) <= 1024:
                        raise ValueError("配方 name/description 不合法")
                    roles = header.get("allowed_roles", ["test_planner", "case_designer", "case_maintainer"])
                    projects = header.get("projects", [])
                    if not isinstance(roles, list) or any(not isinstance(value, str) for value in roles) or not isinstance(projects, list) or any(not isinstance(value, str) for value in projects):
                        raise ValueError("配方角色和项目范围必须为字符串数组")
                    if projects and self.project not in projects:
                        continue
                    if name in self.items:
                        raise ValueError("配方名称重复")
                    self.items[name] = {"name": name, "description": redact_text(description), "version": str(header.get("version", "1")), "sha256": hashlib.sha256(raw.encode()).hexdigest(), "allowed_roles": roles, "content": redact_text(parts[2].strip())}

    def list(self, role: str | None = None):
        return [{key: value[key] for key in ("name", "description", "version", "sha256")} for _, value in sorted(self.items.items()) if role is None or role in value["allowed_roles"]]

    def read(self, name: str, role: str, sha256: str = ""):
        item = self.items.get(name)
        if not item or role not in item["allowed_roles"]:
            raise PermissionError("配方不存在或不属于当前角色")
        if sha256 and item["sha256"] != sha256:
            raise ValueError("配方版本发生变化，请重新发现")
        return {key: item[key] for key in ("name", "version", "sha256", "content")}


def register_recipe_tools(registry):
    from ai_apitest.tools.registry import ToolSpec
    roles = ("test_planner", "case_designer", "case_maintainer")
    def catalog(context):
        return RecipeCatalog(context.root, context.project)
    registry.register(ToolSpec("list_test_recipes", "发现当前角色可用的测试配方摘要；配方不授权工具或覆盖可信契约。", {"type": "object", "additionalProperties": False}, lambda args, context: {"items": catalog(context).list(context.state["_agent_role"])}, roles, output_schema={"type": "object", "required": ["items"]}))
    registry.register(ToolSpec("read_test_recipe", "按名称和可选内容哈希读取配方；不会执行配方中的脚本。", {"type": "object", "properties": {"name": {"type": "string", "maxLength": 80}, "sha256": {"type": "string", "maxLength": 64}}, "required": ["name"], "additionalProperties": False}, lambda args, context: catalog(context).read(args["name"], context.state["_agent_role"], args.get("sha256", "")), roles, output_schema={"type": "object", "required": ["name", "version", "sha256", "content"]}))
