from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from iatest.core.env import load_dotenv
from iatest.core.paths import project_root
from iatest.engines.registry import create_engine_registry
from iatest.engines.store import EngineStore

app = typer.Typer(
    help="Native multi-type test plans, runs and reports", no_args_is_help=True
)


@app.command("proto-compile")
def proto_compile(directory: Path, files: list[str], output: Annotated[Path, typer.Option("--output")]):
    """Compile explicit .proto files/imports to a frozen descriptor JSON."""
    from iatest.engines.protobuf import compile_proto

    output.write_text(json.dumps(compile_proto(directory, files), indent=2), encoding="utf-8")
    typer.echo(str(output))


@app.command("desktop-build")
def desktop_build(output: Path):
    """Compile the macOS AX driver using the installed SDK; never opens an App."""
    import subprocess
    import sys

    if sys.platform != "darwin":
        raise typer.BadParameter(
            "Build this driver on macOS; Windows uses the bundled UIAutomation script"
        )
    source = Path(__file__).with_name("desktop_native") / "DesktopDriver.swift"
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "swiftc",
            str(source),
            "-o",
            str(output),
            "-framework",
            "AppKit",
            "-framework",
            "ApplicationServices",
            "-Xlinker",
            "-weak_framework",
            "-Xlinker",
            "ScreenCaptureKit",
        ],
        check=True,
    )
    typer.echo(f"Built {output}; configure IATEST_DESKTOP_MACOS_DRIVER with this path")


@app.command("wayland-setup")
def wayland_setup(
    output: Annotated[Path | None, typer.Option("--output")] = None,
    install: Annotated[bool, typer.Option("--install")] = False,
    enable: Annotated[bool, typer.Option("--enable")] = False,
    overwrite: Annotated[bool, typer.Option("--overwrite")] = False,
):
    """Prepare the scoped GNOME adapter; install/enable only with explicit flags."""
    import os
    import subprocess
    import sys

    from iatest.engines.desktop_setup import prepare_wayland_helper

    if enable and not install:
        raise typer.BadParameter("--enable requires --install")
    if install:
        if sys.platform != "linux" or output is not None:
            raise typer.BadParameter("Use --install on Linux, or --output for a portable copy")
        output = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "gnome-shell/extensions"
    if output is None:
        raise typer.BadParameter("Choose --output DIRECTORY or explicit --install")
    try:
        result = prepare_wayland_helper(output, overwrite=overwrite)
        if enable:
            completed = subprocess.run(result["enable_command"], check=False, capture_output=True, text=True, timeout=10)
            result["enabled"] = completed.returncode == 0
            if not result["enabled"]:
                result["next_step"] = "Log out and back in so GNOME discovers the new adapter, then run the enable command"
        else:
            result["next_step"] = "Explicitly enable the adapter in GNOME; a new installation may require logging out and back in"
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("ios-hid-build")
def ios_hid_build(output: Path):
    """Build the optional macOS Simulator DTUHID helper without opening a device."""
    import subprocess
    import sys

    if sys.platform != "darwin":
        raise typer.BadParameter("Build the Simulator driver on macOS")
    source = Path(__file__).resolve().parents[1] / "mobile/native/ios/SimulatorHID.m"
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "xcrun",
            "clang",
            "-fobjc-arc",
            "-fblocks",
            str(source),
            "-framework",
            "Foundation",
            "-o",
            str(output),
        ],
        check=True,
    )
    typer.echo(str(output))


@app.command("campaign-create")
def campaign_create(path: Path, project: str = "default"):
    from iatest.application.campaigns import create_campaign

    root, _ = workspace(project)
    typer.echo(
        json.dumps(
            create_campaign(
                root, project, json.loads(path.read_text(encoding="utf-8"))
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("campaign-run")
def campaign_run(campaign_id: str, project: str = "default", analyze: bool = False):
    from iatest.application.campaigns import run_campaign

    root, _ = workspace(project)
    result = run_campaign(root, project, campaign_id, analyze=analyze)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if result["verdict"] != "passed":
        raise typer.Exit(1)


@app.command("campaigns")
def campaigns(project: str = "default"):
    _, store = workspace(project)
    typer.echo(
        json.dumps(
            {
                "manifests": store.artifacts("campaign"),
                "runs": store.artifacts("campaign_run"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def workspace(project):
    root = project_root(Path.cwd())
    load_dotenv(root)
    return root, EngineStore(root, project)


@app.command("security-library-import")
def security_library_import(path: Path, project: str = "default"):
    from iatest.engines.security_scan import import_library

    root, _ = workspace(project)
    typer.echo(
        json.dumps(
            import_library(root, project, json.loads(path.read_text())),
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("accessibility-review-import")
def accessibility_review_import(path: Path, project: str = "default"):
    from iatest.engines.accessibility import import_review

    root, _ = workspace(project)
    typer.echo(
        json.dumps(
            import_review(root, project, json.loads(path.read_text())),
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("capabilities")
def capabilities(project: str = "default"):
    root, _ = workspace(project)
    typer.echo(
        json.dumps(
            create_engine_registry(root, project).capabilities(),
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("doctor")
def runtime_doctor(browser: str = "chromium", network_policy: str = "strict"):
    """Check a browser on an owned local fixture; no project or model needed."""
    from iatest.engines.readiness import check_browser

    result = check_browser(browser, network_policy=network_policy)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["runtime_verified"]:
        raise typer.Exit(1)


@app.command("import-plan")
def import_plan(path: Path, project: str = "default"):
    _, store = workspace(project)
    store.save_plan(json.loads(path.read_text(encoding="utf-8")))
    typer.echo("Typed plan validated and saved")


@app.command("list")
def list_plans(project: str = "default"):
    _, store = workspace(project)
    typer.echo(json.dumps(store.plans(), ensure_ascii=False, indent=2))


@app.command("run")
def run(plan_id: str, project: str = "default"):
    from iatest.application.testing_service import execute_test_plan

    root, _ = workspace(project)
    result = execute_test_plan(root, project, plan_id)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if result["verdict"] != "passed":
        raise typer.Exit(1)


@app.command("reports")
def reports(project: str = "default"):
    _, store = workspace(project)
    typer.echo(json.dumps(store.runs(), ensure_ascii=False, indent=2))


@app.command("visual-find")
def visual_find(path: Path, project: str = "default"):
    """Locate inside an explicit region, without executing any click."""
    from iatest.application.ui_visual import locate_region
    from iatest.engines.web import BrowserSession
    from iatest.execution.control import RunControl

    root, store = workspace(project)
    spec = json.loads(path.read_text(encoding="utf-8"))
    browser = BrowserSession(
        root, project, spec, RunControl(timeout_seconds=90), max_queries=10
    )
    try:
        value = locate_region(root, project, browser, spec["target"], spec["goal"])
        value["id"] = store.save_artifact("visual_location", value)
        typer.echo(json.dumps(value, ensure_ascii=False, indent=2))
    finally:
        browser.close()


@app.command("draft")
def draft(kind: str, goal: str, context: Path, project: str = "default"):
    from iatest.application.testing_service import draft_test_plan

    root, _ = workspace(project)
    result = draft_test_plan(
        root, project, kind, goal, json.loads(context.read_text(encoding="utf-8"))
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("analyze")
def analyze(
    run_id: str,
    project: str = "default",
    baseline: str = "",
    observations: Path | None = None,
):
    """Analyze performance/security evidence without changing the original verdict."""
    from iatest.application.engine_analysis import analyze_engine_run

    root, _ = workspace(project)
    result = analyze_engine_run(
        root,
        project,
        run_id,
        baseline_id=baseline,
        telemetry=json.loads(observations.read_text(encoding="utf-8"))
        if observations
        else None,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("explore")
def explore(
    kind: str, goal: str, context: Path, project: str = "default", force: bool = False
):
    """Discover missing UI steps, compile an observed trace and replay twice."""
    from iatest.application.ui_exploration import explore_ui_goal

    root, _ = workspace(project)
    result = explore_ui_goal(
        root,
        project,
        kind,
        goal,
        json.loads(context.read_text(encoding="utf-8")),
        force=force,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["status"].startswith("reproducible_"):
        raise typer.Exit(1)


@app.command("artifacts")
def artifacts(kind: str = "ui_case", project: str = "default"):
    """List saved ui_case or analysis artifacts, including traces and advice."""
    if kind not in {"ui_case", "analysis"}:
        raise typer.BadParameter("Choose ui_case or analysis")
    _, store = workspace(project)
    typer.echo(json.dumps(store.artifacts(kind), ensure_ascii=False, indent=2))


@app.command("template")
def template(output: Path, mode: str = "goals"):
    from iatest.application.test_assets import template_bytes

    output.write_bytes(template_bytes(output.suffix.lstrip("."), mode))
    typer.echo(str(output))


@app.command("preview-import")
def preview(
    path: Path,
    project: str = "default",
    policy: str = "error",
    defaults: Path | None = None,
):
    from iatest.application.test_assets import preview_import

    root, _ = workspace(project)
    result = preview_import(
        root,
        project,
        path.read_bytes(),
        path.name,
        policy=policy,
        defaults=json.loads(defaults.read_text()) if defaults else {},
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("commit-import")
def commit(batch_id: str, project: str = "default", valid_only: bool = False):
    from iatest.application.test_assets import commit_import

    root, _ = workspace(project)
    typer.echo(
        json.dumps(
            commit_import(root, project, batch_id, valid_only=valid_only),
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("assets")
def assets(project: str = "default"):
    from iatest.application.test_assets import AssetRepository

    root, _ = workspace(project)
    typer.echo(
        json.dumps(AssetRepository(root, project).list(), ensure_ascii=False, indent=2)
    )


@app.command("process-import")
def process_batch(batch_id: str, project: str = "default"):
    from iatest.application.test_assets import process_import

    root, _ = workspace(project)
    typer.echo(
        json.dumps(
            process_import(root, project, batch_id), ensure_ascii=False, indent=2
        )
    )


@app.command("process-asset")
def process_case(
    case_id: str,
    project: str = "default",
    version: int | None = None,
    force: bool = False,
):
    from iatest.application.test_assets import process_asset

    root, _ = workspace(project)
    typer.echo(
        json.dumps(
            process_asset(root, project, case_id, version=version, force=force),
            ensure_ascii=False,
            indent=2,
        )
    )
