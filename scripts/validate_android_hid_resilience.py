"""Owned iPhone Lab HID delivery and Android lifecycle recovery checks."""

from __future__ import annotations

import argparse
import base64
import json
import time
import uuid
from pathlib import Path

from iatest.execution.control import RunControl
from iatest.mobile.bluetooth_hid import AndroidBluetoothHIDSession, PACKAGE
from iatest.mobile.protocol import CommandRejected, DeliveryUnknown, ProtocolError
from iatest.mobile.runtime import MobileSession
from iatest.mobile.usb_hid_input import observed_input


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection", type=Path, required=True)
    parser.add_argument("--ios-profile", type=Path, required=True)
    parser.add_argument("--idle-seconds", type=int, default=120)
    parser.add_argument("--force-restart", action="store_true")
    parser.add_argument("--restart-rounds", type=int, default=1)
    parser.add_argument("--restart-gap-seconds", type=int, default=0)
    parser.add_argument("--background-seconds", type=int, default=0)
    parser.add_argument("--mode-switch-rounds", type=int, default=0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 0 <= args.idle_seconds <= 600 or args.output.exists():
        parser.error("Use a new output and an idle window of 0–600 seconds")
    if not (
        1 <= args.restart_rounds <= 10
        and 0 <= args.restart_gap_seconds <= 15
        and 0 <= args.background_seconds <= 3600
        and 0 <= args.mode_switch_rounds <= 10
    ):
        parser.error("Lifecycle windows or round counts exceed their bounds")
    if not args.force_restart and (args.restart_rounds != 1 or args.restart_gap_seconds):
        parser.error("Restart options require --force-restart")
    settings = json.loads(args.connection.read_text())
    profile = json.loads(args.ios_profile.read_text())
    if (
        profile.get("app_id") != "dev.iatest.mobile.lab"
        or profile.get("platform") != "ios"
        or profile.get("simulator")
    ):
        parser.error("Only the owned physical iPhone Lab is accepted")
    if (
        args.force_restart or args.background_seconds or args.mode_switch_rounds
    ) and settings.get("transport") != "ble":
        parser.error("Lifecycle recovery is a BLE-only acceptance case")
    report = {
        "passed": False,
        "xctest_input_used": False,
        "transport": settings.get("transport", "classic"),
        "lifecycle": settings.get("lifecycle", "app"),
        "cases": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    phone = sender = probe = None

    def save():
        args.output.write_text(json.dumps(report, indent=2))

    def checkpoint(case, **details):
        report["active_case"] = {"case": case, **details}
        save()

    def marker():
        return str(uuid.uuid4().int % 10**12).zfill(12)

    def click(identifier):
        observed_input(
            phone,
            sender,
            {
                "target": {"identifier": identifier},
                "hid_command": {"op": "target_click"},
            },
            10,
            1,
        )

    def expect(identifier, wanted):
        deadline = time.monotonic() + 8
        while True:
            node = phone.unique(
                phone.query({"identifier": identifier}, cache=False, timeout=8)
            )
            attribute = "value" if identifier == "nickname_field" else "text"
            actual = node.get(attribute, node.get("label", ""))
            if (
                actual == wanted
                and attribute not in node.get("ambiguous_attributes", [])
                and attribute not in node.get("truncated_attributes", [])
            ):
                return
            if time.monotonic() >= deadline:
                if attribute in node.get("ambiguous_attributes", []):
                    detail = "ambiguous native value (possibly placeholder text)"
                elif attribute in node.get("truncated_attributes", []):
                    detail = "truncated native value"
                else:
                    detail = "observed " + (str(len(actual)) + " characters" if isinstance(actual, str) else type(actual).__name__)
                raise AssertionError(
                    "Complete business readback differs for " + identifier
                    + ": expected " + str(len(wanted)) + " characters, " + detail
                )
            time.sleep(0.1)

    def round_trip():
        click("reset_button")
        expect("saved_status", "Not saved")
        click("nickname_field")
        value = marker()
        sender.execute({"op": "text", "text": value})
        expect("nickname_field", value)
        click("save_button")
        expect("saved_status", "Saved: " + value)
        return value

    def native():
        state = json.loads(
            probe.adb("exec-out", "run-as", PACKAGE, "cat", "files/status.json")
        )
        if probe.adb("shell", "pidof", PACKAGE).split() != [str(state.get("pid"))]:
            raise AssertionError("Bridge status does not belong to the running process")
        if settings.get("lifecycle") == "adb_managed" and not state.get("ble_diagnostics"):
            from iatest.mobile.managed_ble import ManagedBLEOwner

            owner_state = ManagedBLEOwner(settings).status()
            if owner_state["owner"]["epoch"] != state.get("ble_owner_epoch"):
                raise AssertionError("Managed diagnostic owner differs from the bridge")
            state["ble_diagnostics"] = owner_state["ble_diagnostics"]
        return {
            key: state.get(key)
            for key in (
                "epoch", "pid", "foreground", "transport", "registered", "connected",
                "input_ready", "ble_recovery", "ble_diagnostics",
                "ble_owner", "ble_owner_epoch", "ble_lifecycle",
            )
        }

    def open_sender():
        nonlocal probe
        probe = AndroidBluetoothHIDSession(
            settings,
            RunControl(timeout_seconds=args.background_seconds + args.idle_seconds + 300),
        )
        return probe

    def same_database(before, after):
        if before["pid"] != after["pid"]:
            raise AssertionError("Bridge process changed during the lifecycle case")
        first, last = before.get("ble_diagnostics"), after.get("ble_diagnostics")
        if first and last and any(
            first.get(key) != last.get(key) for key in ("services", "report_handles")
        ):
            raise AssertionError("GATT database changed during the lifecycle case")
        if (
            first and last
            and first.get("counts", {}).get("service_added")
            != last.get("counts", {}).get("service_added")
        ):
            raise AssertionError("GATT services were added during the lifecycle case")

    def same_managed_owner(before, after):
        first, last = before.get("ble_owner", {}), after.get("ble_owner", {})
        if not first.get("running") or not last.get("running") or any(
            first.get(key) != last.get(key) for key in ("pid", "epoch")
        ):
            raise AssertionError("Managed GATT owner changed across UI restart")
        # The UI must change, while the independently owned database stays intact.
        if before["pid"] == after["pid"] or before["epoch"] == after["epoch"]:
            raise AssertionError("Bridge UI did not start a new generation")
        same_database({**before, "pid": after["pid"]}, after)

    save()
    try:
        phone = MobileSession(
            profile,
            control=RunControl(
                timeout_seconds=args.idle_seconds + args.background_seconds
                + 300 * (args.restart_rounds + args.mode_switch_rounds + 1)
            ),
        )
        phone.device.semantic.request(
            "activate_running", {}, operation_id=uuid.uuid4().hex, timeout=20
        )
        observed = phone.query(
            {"identifier": "nickname_field"}, cache=False, timeout=10
        )
        node = phone.unique(observed)
        geometry = phone.device.semantic.request(
            "hid_validate",
            {"ref": node["ref"], "state_version": observed["state_version"]},
            timeout=5,
        )
        calibration = {
            "width": geometry["width"],
            "height": geometry["height"],
            "rotation": geometry["orientation"],
        }
        if (
            settings.get("pointer_calibration")
            and settings["pointer_calibration"] != calibration
        ):
            raise ValueError(
                "Configured pointer calibration differs from the current phone"
            )
        settings["pointer_calibration"] = calibration
        sender = open_sender()
        checkpoint("baseline_save")
        round_trip()
        report["cases"].append({"case": "baseline_save", "passed": True})
        save()
        checkpoint("idle_save", seconds=args.idle_seconds)
        sender.control.wait(args.idle_seconds)
        round_trip()
        report["cases"].append(
            {"case": "idle_save", "seconds": args.idle_seconds, "passed": True}
        )
        save()
        checkpoint("real_input_lost_receipt")
        click("nickname_field")
        # Establish an exact known prefix before injecting the receipt fault.
        click("reset_button")
        click("nickname_field")
        prefix = marker()
        sender.execute({"op": "text", "text": prefix})
        expect("nickname_field", prefix)
        suffix = marker()
        original = sender.adb
        injected = False

        def lose_receipt(*arguments, **kwargs):
            nonlocal injected
            output = original(*arguments, **kwargs)
            if arguments[:3] == ("shell", "am", "broadcast") and not injected:
                envelope = json.loads(base64.b64decode(arguments[-1]))
                if envelope["op"] == "execute":
                    injected = True
                    raise TimeoutError(
                        "Synthetic lost host receipt after real input dispatch"
                    )
            return output

        sender.adb = lose_receipt
        try:
            try:
                sender.execute({"op": "text", "text": suffix})
            except DeliveryUnknown:
                pass
            else:
                raise AssertionError("Lost receipt was not marked unknown")
        finally:
            sender.adb = original
        if not injected:
            raise AssertionError("Receipt fault was not exercised")
        try:
            sender.execute({"op": "text", "text": suffix})
        except ProtocolError:
            pass
        else:
            raise AssertionError("Unknown input was replayed")
        expect("nickname_field", prefix + suffix)
        sender.close()
        sender = None
        report["cases"].append(
            {
                "case": "real_input_lost_receipt",
                "fault_injected": True,
                "exactly_once_readback": True,
                "replay_rejected": True,
                "passed": True,
            }
        )
        save()
        if args.background_seconds:
            sender = open_sender()
            before = native()
            samples = []
            checkpoint("background_return_save", seconds=args.background_seconds, before=before, samples=samples)
            sender.adb("shell", "input", "keyevent", "KEYCODE_HOME")
            try:
                until = time.monotonic() + 5
                while native()["foreground"] and time.monotonic() < until:
                    time.sleep(.1)
                if native()["foreground"]:
                    raise AssertionError("Bridge did not enter the background")
                try:
                    sender.execute({"op": "text", "text": marker()})
                except CommandRejected:
                    pass
                else:
                    raise AssertionError("Background input was accepted")
                began = time.monotonic()
                while time.monotonic() - began < args.background_seconds:
                    state = native()
                    same_database(before, state)
                    if state["foreground"] or state["input_ready"]:
                        raise AssertionError("Background bridge became input ready")
                    samples.append({"elapsed_seconds": round(time.monotonic() - began, 2), **state})
                    save()
                    sender.control.wait(
                        min(15, max(.01, args.background_seconds - (time.monotonic() - began)))
                    )
            finally:
                sender.adb("shell", "am", "start", "-W", "-n", PACKAGE + "/.MainActivity", "--es", "transport", "ble", cleanup=True)
            until = time.monotonic() + 5
            while not native()["foreground"]:
                if time.monotonic() >= until:
                    raise RuntimeError(
                        "Unlock Android and return the bridge to the foreground; "
                        "post-background input has not been dispatched"
                    )
                time.sleep(.1)
            expect("nickname_field", prefix + suffix)
            same_database(before, native())
            round_trip()
            sender.close()
            sender = None
            report["cases"].append({"case": "background_return_save", "seconds": args.background_seconds,
                                    "background_input_rejected": True, "samples": samples, "passed": True})
            save()
        for index in range(args.mode_switch_rounds):
            sender = open_sender()
            before = native()
            checkpoint("ble_return_after_classic_registration", round=index + 1, before=before)
            sender.close()
            sender.adb("shell", "am", "start", "-W", "-n", PACKAGE + "/.MainActivity", "--es", "transport", "classic")
            until = time.monotonic() + 5
            while True:
                intermediate = native()
                if intermediate["transport"] == "classic" and intermediate["registered"]:
                    break
                if time.monotonic() >= until:
                    raise AssertionError("Classic registration did not complete")
                time.sleep(.1)
            sender = None
            sender = open_sender()
            same_database(before, native())
            round_trip()
            sender.close()
            sender = None
            report["cases"].append({"case": "ble_return_after_classic_registration", "round": index + 1,
                                    "classic_state": intermediate, "ble_state": native(),
                                    "classic_business_input_tested": False, "passed": True})
            save()
        for index in range(args.restart_rounds if args.force_restart else 0):
            sender = open_sender()
            before_restart = native()
            checkpoint("stale_generation_and_new_session", round=index + 1,
                       restart_gap_seconds=args.restart_gap_seconds, before=before_restart)
            unchanged = phone.unique(phone.query({"identifier": "nickname_field"}, cache=False, timeout=8)).get("value")
            sender.adb("shell", "am", "force-stop", PACKAGE)
            time.sleep(args.restart_gap_seconds)
            absent_state = None
            if settings.get("lifecycle") == "adb_managed":
                from iatest.mobile.managed_ble import ManagedBLEOwner

                absent_state = ManagedBLEOwner(settings).status()
                owner = absent_state["owner"]
                if (owner["ui_pid"] or owner["ui_lease_ready"] or absent_state["input_ready"]
                        or owner["pid"] != before_restart["ble_owner"]["pid"]):
                    raise AssertionError("Stopped UI retained input authority or lost its GATT owner")
            began = time.monotonic()
            sender.adb(
                "shell",
                "am",
                "start",
                "-W",
                "-n",
                PACKAGE + "/.MainActivity",
                "--es",
                "transport",
                "ble",
            )
            try:
                sender.execute({"op": "text", "text": marker()})
            except CommandRejected:
                pass
            else:
                raise AssertionError("Stale bridge generation was accepted")
            try:
                sender.close()
            except CommandRejected:
                pass
            sender = None
            expect("nickname_field", unchanged)
            sender = open_sender()
            recovered_seconds = round(time.monotonic() - began, 3)
            after_restart = native()
            if settings.get("lifecycle") == "adb_managed":
                same_managed_owner(before_restart, after_restart)
            round_trip()
            sender.close()
            sender = None
            report["cases"].append(
                {"case": "stale_generation_and_new_session", "round": index + 1,
                 "restart_gap_seconds": args.restart_gap_seconds, "recovery_seconds": recovered_seconds,
                 "absent_ui_state": absent_state, "after": after_restart, "passed": True}
            )
            save()
        wrong = {**settings, "host_address": "02:00:00:00:00:01"}
        checkpoint("wrong_host_before_input")
        if wrong["host_address"] == settings["host_address"]:
            raise AssertionError("Fault host equals actual host")
        before_wrong = phone.unique(
            phone.query({"identifier": "nickname_field"}, cache=False, timeout=8)
        ).get("value")
        try:
            unexpected = AndroidBluetoothHIDSession(wrong)
        except CommandRejected:
            pass
        else:
            unexpected.close()
            raise AssertionError("Wrong host binding was accepted")
        expect("nickname_field", before_wrong)
        report["cases"].append({"case": "wrong_host_before_input", "passed": True})
        args.output.with_suffix(".png").write_bytes(phone.preview()["binary"])
        report["passed"] = True
        report.pop("active_case", None)
    except BaseException as error:
        report["error"] = type(error).__name__ + ": " + str(error)
        if probe:
            try:
                report["failed_native"] = native()
            except Exception as diagnostic_error:
                report["diagnostic_error"] = type(diagnostic_error).__name__
        raise
    finally:
        cleanup_errors = []
        try:
            if sender:
                try:
                    sender.close()
                except BaseException as error:
                    cleanup_errors.append(type(error).__name__ + ": " + str(error))
        finally:
            try:
                if phone:
                    try:
                        phone.close()
                    except BaseException as error:
                        cleanup_errors.append(type(error).__name__ + ": " + str(error))
            finally:
                if cleanup_errors:
                    report["passed"] = False
                    report["cleanup_errors"] = cleanup_errors
                save()
        if cleanup_errors and "error" not in report:
            raise RuntimeError("Acceptance cleanup failed: " + "; ".join(cleanup_errors))
    print(
        json.dumps({"passed": report["passed"], "cases": len(report["cases"])}),
        flush=True,
    )


if __name__ == "__main__":
    main()
