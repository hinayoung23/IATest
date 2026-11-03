from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from iatest.engines.cli import workspace
from iatest.nodes.store import ControllerStore

app = typer.Typer(
    help="Paired local/LAN execution nodes and controller", no_args_is_help=True
)


@app.command("windows-bundle")
def windows_bundle(
    output: Path,
    wheel: Path,
    ca: Path,
    controller: str,
    node_id: str,
    origins: Annotated[list[str], typer.Option("--origin")],
    token_env: str = "IATEST_NODE_TOKEN",
    project: str = "default",
    kinds: str = "api,performance",
):
    from iatest.nodes.windows_bundle import build_bundle

    typer.echo(
        json.dumps(
            build_bundle(
                output,
                wheel,
                ca,
                controller=controller,
                allowed_origins=origins,
                node_id=node_id,
                token_env=token_env,
                project=project,
                allowed_kinds=kinds.split(","),
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("enroll")
def enroll(path: Path, project: str = "default"):
    root, _ = workspace(project)
    ControllerStore(root, project).enroll(json.loads(path.read_text()))
    typer.echo(
        "Node enrollment saved; token values remain in existing environment variables"
    )


@app.command("list")
def list_nodes(project: str = "default"):
    root, _ = workspace(project)
    typer.echo(
        json.dumps(ControllerStore(root, project).nodes(), ensure_ascii=False, indent=2)
    )


@app.command("controller")
def controller(
    project: str = "default",
    host: str = "127.0.0.1",
    port: int = 8768,
    cert: str = "",
    key: str = "",
):
    from iatest.nodes.protocol import make_controller

    root, _ = workspace(project)
    server = make_controller(root, project, host, port, certfile=cert, keyfile=key)
    typer.echo(
        f"Node controller: {'https' if cert else 'http'}://{host}:{server.server_port}"
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


@app.command("claims")
def claims(project: str = "default"):
    root, _ = workspace(project)
    typer.echo(
        json.dumps(
            ControllerStore(root, project).claims(), ensure_ascii=False, indent=2
        )
    )


@app.command("coordinator")
def coordinator(
    identities: Path,
    database: Path,
    host: str = "127.0.0.1",
    port: int = 8769,
    cert: str = "",
    key: str = "",
    etcd_config: Path | None = None,
):
    """Serve one shared resource authority; identity files contain env references only."""
    from iatest.nodes.coordinator import make_coordinator

    workspace("default")
    server = make_coordinator(
        database,
        json.loads(identities.read_text(encoding="utf-8")),
        host,
        port,
        cert=cert,
        key=key,
        etcd=json.loads(etcd_config.read_text()) if etcd_config else None,
    )
    typer.echo(
        f"Resource authority: {'https' if cert else 'http'}://{host}:{server.server_port}"
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


@app.command("coordinator-config")
def coordinator_config(path: Path, project: str = "default"):
    from iatest.core.config import load_config, save_config
    from iatest.nodes.coordinator import CoordinatorClient

    root, _ = workspace(project)
    spec = json.loads(path.read_text(encoding="utf-8"))
    if spec:
        CoordinatorClient(spec).close()
    config = load_config(root)
    config["resource_coordinator"] = spec
    save_config(root, config)
    typer.echo(
        "Resource authority configuration saved; existing secret values unchanged"
    )


@app.command("global-claims")
def global_claims(project: str = "default"):
    from iatest.nodes.coordinator import CoordinatorClient

    root, _ = workspace(project)
    client = CoordinatorClient.configured(root)
    if client is None:
        raise typer.BadParameter("Configure resource_coordinator first")
    try:
        typer.echo(json.dumps(client.call("list", {}), ensure_ascii=False, indent=2))
    finally:
        client.close()


@app.command("global-reconcile")
def global_reconcile(
    lease_id: str, note: str, stopped: bool = False, project: str = "default"
):
    from iatest.nodes.coordinator import CoordinatorClient

    root, _ = workspace(project)
    client = CoordinatorClient.configured(root)
    if client is None:
        raise typer.BadParameter("Configure resource_coordinator first")
    try:
        typer.echo(
            json.dumps(
                client.call(
                    "reconcile", {"id": lease_id, "stopped": stopped, "note": note}
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        client.close()


@app.command("reconcile")
def reconcile(job_id: str, note: str, stopped: bool = False, project: str = "default"):
    """Record external stop/side-effect checks before releasing uncertain resources."""
    root, _ = workspace(project)
    ControllerStore(root, project).reconcile(job_id, note, stopped)
    typer.echo("Reconciliation recorded; no test action was replayed")


@app.command("attempt")
def attempt(job_id: str, project: str = "default"):
    """Read the local worker's durable outcome journal; never executes the task."""
    _, store = workspace(project)
    typer.echo(
        json.dumps(
            store.get_artifact("node_attempt", job_id), ensure_ascii=False, indent=2
        )
    )


@app.command("worker")
def worker(
    controller: str,
    node_id: str,
    origins: Annotated[list[str], typer.Option("--origin")],
    project: str = "default",
    token_env: str = "IATEST_NODE_TOKEN",
    kinds: str = "performance",
    ca: str = "",
):
    from iatest.nodes.worker import NodeWorker

    root, _ = workspace(project)
    node = NodeWorker(
        root,
        project,
        controller,
        node_id,
        token_env,
        origins,
        allowed_kinds=kinds.split(","),
        ca_file=ca,
    )
    try:
        node.run()
    finally:
        node.close()


@app.command("run")
def run(node_id: str, plan_id: str, project: str = "default"):
    from dataclasses import asdict

    from iatest.execution.control import RunControl
    from iatest.nodes.distributed import execute_remote

    root, store = workspace(project)
    result = execute_remote(
        root, project, node_id, store.get_plan(plan_id), RunControl(timeout_seconds=600)
    )
    typer.echo(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    if result.verdict != "passed":
        raise typer.Exit(1)
