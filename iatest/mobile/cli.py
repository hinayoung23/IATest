from __future__ import annotations

import json
from pathlib import Path

import typer

from iatest.core.env import load_dotenv
from iatest.core.paths import project_root
from iatest.mobile.devices import doctor
from iatest.mobile.store import MobileStore

app = typer.Typer(help="Native Android/iOS testing", no_args_is_help=True)


@app.command("bluetooth-hid-status")
def bluetooth_hid_status(android_device_id: str, list_hosts: bool = False):
    """Read the owned sender's registration state; optionally list paired hosts."""
    from iatest.mobile.bluetooth_hid import status

    typer.echo(json.dumps(status(android_device_id, list_hosts=list_hosts), ensure_ascii=False, indent=2))


@app.command("bluetooth-hid-run")
def bluetooth_hid_run(path: Path):
    """Send paired Android Bluetooth HID input; independently verify business results."""
    from iatest.engines.http import validate
    from iatest.mobile.bluetooth_hid import (
        CONFIG_SCHEMA,
        AndroidBluetoothHIDSession,
    )
    from iatest.mobile.usb_hid import COMMAND_SCHEMA

    value = json.loads(path.read_text(encoding="utf-8"))
    validate(value, {"type": "object", "additionalProperties": False,
                     "required": ["connection", "commands"], "properties": {
                         "connection": CONFIG_SCHEMA, "commands": {"type": "array", "minItems": 1,
                             "maxItems": 100, "items": COMMAND_SCHEMA}}})
    if any(item["op"] == "touch" for item in value["commands"]):
        raise ValueError("Bluetooth HID does not advertise a touch digitizer")
    results = []
    channel = AndroidBluetoothHIDSession(value["connection"])
    try:
        for item in value["commands"]:
            results.append(channel.execute(item))
    finally:
        channel.close()
    typer.echo(json.dumps({"status": "delivered_unverified", "business_postcondition_verified": False,
                           "results": results}, ensure_ascii=False, indent=2))


@app.command("bluetooth-hid-reconnect")
def bluetooth_hid_reconnect(path: Path):
    """Release a confirmed BLE session and reconnect only its explicitly paired host."""
    from iatest.mobile.bluetooth_hid import AndroidBluetoothHIDSession

    settings = json.loads(path.read_text(encoding="utf-8"))
    channel = AndroidBluetoothHIDSession(settings)
    try:
        typer.echo(json.dumps(channel.reconnect(), ensure_ascii=False, indent=2))
    finally:
        channel.close()


@app.command("bluetooth-hid-owner-start")
def bluetooth_hid_owner_start(
    path: Path,
    artifact_dir: Path = Path("native/mobile/build/android-hid"),
    lifetime_seconds: int = 3600,
    pairing_window_seconds: int = typer.Option(
        0, help="Prepare services before pairing for 5–300 seconds; requires Android 16+ shell identity access. 0 requires an existing pair."
    ),
):
    """Explicitly own a finite BLE GATT lifetime independently of the bridge UI."""
    from iatest.mobile.managed_ble import ManagedBLEOwner

    owner = ManagedBLEOwner(json.loads(path.read_text(encoding="utf-8")), artifact_dir)
    typer.echo(json.dumps(owner.start(lifetime_seconds, pairing_window_seconds=pairing_window_seconds), ensure_ascii=False, indent=2))


@app.command("bluetooth-hid-owner-status")
def bluetooth_hid_owner_status(path: Path):
    """Read the explicit GATT owner without opening the Android bridge UI."""
    from iatest.mobile.managed_ble import ManagedBLEOwner

    typer.echo(json.dumps(ManagedBLEOwner(json.loads(path.read_text(encoding="utf-8"))).status(), ensure_ascii=False, indent=2))


@app.command("bluetooth-hid-owner-stop")
def bluetooth_hid_owner_stop(path: Path):
    """Release keys and remove only the explicitly bound managed GATT owner."""
    from iatest.mobile.managed_ble import ManagedBLEOwner

    typer.echo(json.dumps(ManagedBLEOwner(json.loads(path.read_text(encoding="utf-8"))).stop(), ensure_ascii=False, indent=2))


@app.command("bluetooth-hid-recover")
def bluetooth_hid_recover(path: Path, artifact_dir: Path = Path("native/mobile/build/android-hid"),
                          lifetime_seconds: int = 3600, restart_bluetooth: bool = False):
    """Explicit radio reset and service rebuild; interrupts this Android's Bluetooth links."""
    from iatest.mobile.bluetooth_recovery import BluetoothHIDRecovery

    recovery = BluetoothHIDRecovery(json.loads(path.read_text()), artifact_dir)
    typer.echo(json.dumps(recovery.recover(lifetime_seconds, restart_bluetooth=restart_bluetooth), indent=2))


@app.command("bluetooth-hid-watch")
def bluetooth_hid_watch(path: Path, artifact_dir: Path = Path("native/mobile/build/android-hid"),
                        lifetime_seconds: int = 3600, restart_bluetooth: bool = False):
    """Finite recovery across process/radio/phone restart; never replays HID input."""
    from iatest.execution.control import RunControl
    from iatest.mobile.bluetooth_recovery import BluetoothHIDRecovery

    recovery = BluetoothHIDRecovery(json.loads(path.read_text()), artifact_dir,
                                    RunControl(timeout_seconds=lifetime_seconds + 30))
    typer.echo(json.dumps(recovery.watch(lifetime_seconds, restart_bluetooth=restart_bluetooth,
                                        emit=lambda row: typer.echo(json.dumps(row))), indent=2))


@app.command("bluetooth-hid-watch-stop")
def bluetooth_hid_watch_stop(path: Path):
    """Cancel a recovery watch locally, even when the Android device is offline."""
    from iatest.mobile.bluetooth_recovery import cancel_watch

    typer.echo(json.dumps({"cancelled": cancel_watch(json.loads(path.read_text()))}))


@app.command("hid-run")
def hid_run(path: Path):
    """Send explicit external USB HID keyboard/mouse commands without XCTest."""
    from iatest.engines.http import validate
    from iatest.mobile.usb_hid import COMMAND_SCHEMA, CONFIG_SCHEMA, USBHIDSession

    value = json.loads(path.read_text(encoding="utf-8"))
    validate(value, {"type": "object", "additionalProperties": False,
                     "required": ["connection", "commands"], "properties": {
                         "connection": CONFIG_SCHEMA, "commands": {"type": "array", "minItems": 1,
                             "maxItems": 100, "items": COMMAND_SCHEMA}}})
    channel = USBHIDSession(value["connection"])
    results = []
    try:
        for command in value["commands"]:
            results.append(channel.execute(command))
    finally:
        channel.close()
    typer.echo(json.dumps({"status": "delivered_unverified", "business_postcondition_verified": False,
                           "results": results}, ensure_ascii=False, indent=2))


@app.command("perf-doctor")
def performance_doctor():
    """Inspect the host Instruments toolchain without enumerating/operating phones."""
    from iatest.mobile.ios_capture import capabilities

    typer.echo(json.dumps(capabilities(), ensure_ascii=False, indent=2))


@app.command("import-metrics")
def import_metrics(path: Path, project: str = "default"):
    """Import existing trace/XML/measurement JSON; no fresh UI execution."""
    from iatest.application.ios_measurements import import_measurement

    root, _ = store(project)
    typer.echo(
        json.dumps(
            import_measurement(
                root, project, json.loads(path.read_text(encoding="utf-8"))
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("measurements")
def measurements(project: str = "default"):
    from iatest.engines.store import EngineStore

    root, _ = store(project)
    typer.echo(
        json.dumps(
            EngineStore(root, project).artifacts("ios_measurement"),
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("ios-measure")
def ios_measure(path: Path, project: str = "default"):
    """Run finite XCTest launch/scroll measurement on an explicitly selected profile."""
    from iatest.mobile.ios_xctest import run_measurement

    root, _ = store(project)
    typer.echo(
        json.dumps(
            run_measurement(
                root, project, json.loads(path.read_text(encoding="utf-8"))
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("ios-calibrate")
def ios_calibrate(path: Path, project: str = "default"):
    """Verify a scroll collector using the owned Lab's quiet/injected controls."""
    from iatest.mobile.ios_calibration import run_calibration

    root, _ = store(project)
    result = run_calibration(
        root, project, json.loads(path.read_text(encoding="utf-8"))
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "usable":
        raise typer.Exit(1)


@app.command("calibrations")
def calibrations(project: str = "default"):
    from iatest.engines.store import EngineStore

    root, _ = store(project)
    typer.echo(
        json.dumps(
            EngineStore(root, project).artifacts("ios_calibration"),
            ensure_ascii=False,
            indent=2,
        )
    )


def store(project):
    root = project_root(Path.cwd())
    load_dotenv(root)
    return root, MobileStore(root, project)


@app.command("doctor")
def check_devices():
    typer.echo(json.dumps(doctor(), ensure_ascii=False, indent=2))


@app.command("outline")
def outline(profile: str, project: str = "default"):
    """Discover bounded actionable controls, including Flutter/custom widgets."""
    from iatest.execution.control import RunControl
    from iatest.mobile.runtime import MobileSession

    root, repository = store(project)
    session = MobileSession(
        repository.get("profile", profile),
        control=RunControl(timeout_seconds=90),
        root=root,
        project=project,
    )
    try:
        typer.echo(json.dumps(session.outline(), ensure_ascii=False, indent=2))
    finally:
        session.close()


@app.command("preview")
def preview(profile: str, output: Path, codec: str = "h264", project: str = "default"):
    """Open bounded native media and save one freshly decoded PNG, then close it."""
    from iatest.core.files import atomic_write_bytes
    from iatest.mobile.runtime import MobileSession

    if codec not in {"h264", "png"}:
        raise typer.BadParameter("Choose h264 or png")
    root, repository = store(project)
    session = MobileSession(
        repository.get("profile", profile), root=root, project=project
    )
    try:
        value = session.preview(separate=True, codec=codec)
        atomic_write_bytes(output, value.pop("binary"))
        typer.echo(json.dumps(value, ensure_ascii=False, indent=2))
    finally:
        session.close()


@app.command("bridge-read")
def bridge_read(profile: str, artifact: str, project: str = "default"):
    """Read development-build hints; does not execute business actions."""
    from iatest.mobile.bridge import read_bridge

    root, _ = store(project)
    typer.echo(
        json.dumps(
            read_bridge(root, project, profile, artifact), ensure_ascii=False, indent=2
        )
    )


@app.command("visual-find")
def visual_find(
    profile: str,
    target: Path,
    goal: str,
    project: str = "default",
    visual_crop: str = "",
):
    """Use one bounded native image for suggestions; never performs input."""
    from iatest.application.ui_visual import locate_region
    from iatest.mobile.runtime import MobileSession

    root, repository = store(project)
    session = MobileSession(
        repository.get("profile", profile), root=root, project=project
    )
    try:
        value = locate_region(
            root,
            project,
            session,
            json.loads(target.read_text(encoding="utf-8")),
            goal,
            visual_crop=json.loads(visual_crop) if visual_crop else None,
        )
        typer.echo(json.dumps(value, ensure_ascii=False, indent=2))
    finally:
        session.close()


@app.command("query")
def query(
    profile: str,
    target: Path,
    repeat: int = 1,
    fresh: bool = False,
    project: str = "default",
):
    """Inspect bounded native targets and cache metrics without input actions."""
    from iatest.mobile.runtime import MobileSession

    if not 1 <= repeat <= 20:
        raise typer.BadParameter("repeat must be 1–20")
    root, repository = store(project)
    session = MobileSession(
        repository.get("profile", profile), root=root, project=project
    )
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
        for _ in range(repeat):
            typer.echo(
                json.dumps(session.query(value, cache=not fresh), ensure_ascii=False)
            )
    finally:
        session.close()


@app.command("gesture-preview")
def gesture_preview(path: Path):
    """Review contact scheduling locally; never opens a device session."""
    from iatest.mobile.gestures import preview_contacts

    value = json.loads(path.read_text(encoding="utf-8"))
    typer.echo(
        json.dumps(preview_contacts(value["contacts"]), ensure_ascii=False, indent=2)
    )


@app.command("build")
def build(
    platform: str,
    java_home: str = "",
    sdk: str = "",
    team: str = "",
    device: bool = False,
    performance: bool = False,
    hid: bool = False,
    keystore: str = "",
):
    """Build native components with installed SDKs; no toolchains are downloaded."""
    import subprocess
    import sys

    if platform not in {"android", "ios"}:
        raise typer.BadParameter("Choose android or ios")
    if performance and platform != "ios":
        raise typer.BadParameter("Performance Runner configuration applies to iOS")
    if (hid or keystore) and platform != "android":
        raise typer.BadParameter("Bluetooth HID and development keystore options apply to Android")
    if hid and not keystore:
        raise typer.BadParameter("Bluetooth HID requires an existing development keystore")
    root, _ = store("default")
    args = [
        sys.executable,
        "-m",
        "iatest.mobile.build",
        platform,
        "--output",
        str(root / "native/mobile/build" / ("android-hid" if hid else platform)),
    ]
    for key, value in (("--java-home", java_home), ("--sdk", sdk), ("--team", team)):
        if value:
            args.extend([key, value])
    if device:
        args.append("--device")
    if performance:
        args.append("--performance")
    if hid:
        args.append("--hid")
    if keystore:
        args.extend(["--keystore", keystore])
    try:
        subprocess.run(args, cwd=root, check=True)
    except subprocess.CalledProcessError as exc:
        raise typer.Exit(exc.returncode)


@app.command("import-profile")
def import_profile(path: Path, project: str = "default"):
    _, repository = store(project)
    repository.save_profile(json.loads(path.read_text()))
    typer.echo("Device profile saved")


@app.command("import-plan")
def import_plan(path: Path, project: str = "default"):
    _, repository = store(project)
    repository.save_plan(json.loads(path.read_text()))
    typer.echo("Mobile plan validated and saved")


@app.command("list")
def list_assets(project: str = "default"):
    _, repository = store(project)
    typer.echo(
        json.dumps(
            {"profiles": repository.list("profile"), "plans": repository.list("plan")},
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("run")
def run(plan_id: str, project: str = "default"):
    from iatest.application.mobile_service import execute_mobile_plan

    root, _ = store(project)
    result = execute_mobile_plan(root, project, plan_id)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if result["verdict"] != "passed":
        raise typer.Exit(1)


@app.command("reports")
def reports(project: str = "default"):
    _, repository = store(project)
    typer.echo(json.dumps(repository.runs(), ensure_ascii=False, indent=2))


@app.command("fetch-app")
def fetch_app(path: Path, project: str = "default"):
    """Fetch and inspect an APK/IPA without installing it on a phone."""
    from iatest.mobile.delivery import fetch_app as fetch

    root, _ = store(project)
    typer.echo(
        json.dumps(
            fetch(root, project, json.loads(path.read_text())),
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("apps")
def apps(project: str = "default"):
    from iatest.engines.store import EngineStore

    root, _ = store(project)
    typer.echo(
        json.dumps(
            EngineStore(root, project).artifacts("app"), ensure_ascii=False, indent=2
        )
    )


@app.command("prepare-app")
def prepare(path: Path, project: str = "default"):
    """Execute explicitly selected fresh/upgrade deployment and verify the installed version."""
    from iatest.execution.control import RunControl
    from iatest.mobile.delivery import prepare_app

    root, _ = store(project)
    result = prepare_app(
        root, project, json.loads(path.read_text()), RunControl(timeout_seconds=300)
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if result["state"] != "prepared":
        raise typer.Exit(1)
