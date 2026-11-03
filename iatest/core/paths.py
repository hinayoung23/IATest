from __future__ import annotations

from pathlib import Path
import re


PROJECT_CONFIG = "iatest.json"


def project_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for path in [current, *current.parents]:
        if (path / PROJECT_CONFIG).exists():
            return path
    return current


def ensure_project_dirs(root: Path, project: str = "default") -> None:
    for path in [
        root / ".iatest",
        project_docs_dir(root, project),
        project_cases_dir(root, project),
        project_reports_dir(root, project),
    ]:
        path.mkdir(parents=True, exist_ok=True)


def safe_project_name(project: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_.-]+", "-", project.strip()).strip("-")
    return normalized or "default"


def projects_dir(root: Path) -> Path:
    return root / "projects"


def project_dir(root: Path, project: str = "default") -> Path:
    return projects_dir(root) / safe_project_name(project)


def list_projects(root: Path) -> list[str]:
    directory = projects_dir(root)
    if not directory.exists():
        return ["default"]
    names = sorted(path.name for path in directory.iterdir() if path.is_dir())
    return names or ["default"]


def project_docs_dir(root: Path, project: str = "default") -> Path:
    return project_dir(root, project) / "api_docs"


def project_cases_dir(root: Path, project: str = "default") -> Path:
    return project_dir(root, project) / "test_cases"


def project_reports_dir(root: Path, project: str = "default") -> Path:
    return project_dir(root, project) / "reports"


def project_runtime_dir(root: Path, project: str = "default") -> Path:
    return project_dir(root, project) / ".iatest"


def project_context_dir(root: Path, project: str = "default") -> Path:
    return project_dir(root, project) / "context"


def project_context_exports_dir(root: Path, project: str = "default") -> Path:
    return project_context_dir(root, project) / "exports"


def project_context_packs_dir(root: Path, project: str = "default") -> Path:
    return project_context_dir(root, project) / "packs"


def project_docs_file(root: Path, project: str = "default") -> Path:
    return project_docs_dir(root, project) / "endpoints.json"


def project_metadata_file(root: Path, project: str = "default") -> Path:
    return project_docs_dir(root, project) / "metadata.json"


def project_source_snapshot_file(root: Path, project: str = "default") -> Path:
    return project_docs_dir(root, project) / "source-snapshot.md"


def project_coverage_manifest_file(root: Path, project: str = "default") -> Path:
    return project_cases_dir(root, project) / "coverage-manifest.json"


def project_cases_file(root: Path, project: str = "default") -> Path:
    return project_cases_dir(root, project) / f"{safe_project_name(project)}.md"


def docs_file(root: Path) -> Path:
    return root / "api_docs" / "endpoints.json"


def cases_dir(root: Path) -> Path:
    return root / "test_cases"


def reports_dir(root: Path) -> Path:
    return root / "reports"


def result_db(root: Path) -> Path:
    return root / ".iatest" / "results.sqlite3"


def project_result_db(root: Path, project: str = "default") -> Path:
    return project_runtime_dir(root, project) / "results.sqlite3"


def project_context_db(root: Path, project: str = "default") -> Path:
    return project_runtime_dir(root, project) / "context.db"


def project_task_db(root: Path, project: str = "default") -> Path:
    return project_runtime_dir(root, project) / "tasks.sqlite3"


def project_agent_db(root: Path, project: str = "default") -> Path:
    return project_runtime_dir(root, project) / "agent_runs.sqlite3"


def project_generation_cache(root: Path, project: str = "default") -> Path:
    return project_runtime_dir(root, project) / "generation-cache.json"
