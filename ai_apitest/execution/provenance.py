from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
from importlib.metadata import version, PackageNotFoundError
from typing import Any

from ai_apitest.core.models import TestCase
from ai_apitest.core.paths import project_docs_file
from ai_apitest.core.serde import read_json
from ai_apitest.execution.options import RunOptions


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def run_metadata(root: Path, project: str, environment: dict, cases: list[TestCase], options: RunOptions, seed: str | int, config: dict | None = None) -> dict:
    try:
        framework_version = version("iatest-framework")
    except PackageNotFoundError:
        framework_version = "source"
    source_root = Path(__file__).resolve().parents[1]
    source_hash = fingerprint({str(path.relative_to(source_root)): hashlib.sha256(path.read_bytes()).hexdigest() for folder in ("execution", "core") for path in sorted((source_root / folder).glob("*.py"))})
    revision = os.getenv("CI_COMMIT_SHA") or os.getenv("GITHUB_SHA") or ""
    if not revision and (root / ".git").exists():
        try:
            revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, stderr=subprocess.DEVNULL, text=True, timeout=3).strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return {
        "version": 1, "project": project, "environment": environment.get("name", ""),
        "framework_version": framework_version,
        "framework_source_hash": source_hash,
        "environment_hash": fingerprint(environment),
        "base_url": environment.get("base_url", ""), "git_revision": revision,
        "contract_hash": fingerprint(read_json(project_docs_file(root, project), [])),
        "cases_hash": fingerprint([asdict(case) for case in cases]),
        "case_hashes": [{"case_id": case.id, "origin_case_id": case.origin_case_id or case.id, "hash": fingerprint(asdict(case))} for case in cases],
        "dataset_hash": fingerprint([{ "id": case.id, "variables": case.variables} for case in cases]),
        "fixture_hash": fingerprint((config or {}).get("fixtures", {})),
        "seed": seed, "options": {key: sorted(value) if isinstance(value, set) else value for key, value in asdict(options).items()}, "case_ids": [case.id for case in cases],
    }
