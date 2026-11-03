"""Environment bindings for iOS collector calibration; no device secrets retained."""

from __future__ import annotations

import hashlib
import json
import plistlib
from pathlib import Path

from iatest.mobile.devices import command
from iatest.mobile.ios_capture import app_record, device_query


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def runner_digest(source):
    source = Path(source).expanduser().resolve()
    if not source.is_file() or source.stat().st_size > 1048576:
        raise ValueError("A bounded xctestrun is required for calibration scope")
    value = plistlib.loads(source.read_bytes())
    hosts = []

    def scan(item):
        if isinstance(item, dict):
            if "TestBundlePath" in item and isinstance(item.get("TestHostPath"), str):
                hosts.append(
                    Path(
                        item["TestHostPath"].replace("__TESTROOT__", str(source.parent))
                    )
                )
            for child in item.values():
                scan(child)
        elif isinstance(item, list):
            for child in item:
                scan(child)

    scan(value)
    if not hosts:
        raise ValueError("The Runner host is missing from the xctestrun")
    fingerprints = {"xctestrun": hashlib.sha256(source.read_bytes()).hexdigest()}
    total = 0
    for host in hosts:
        if not host.is_dir() or not host.resolve().is_relative_to(source.parent):
            raise ValueError(
                "Runner products must be contained under the xctestrun directory"
            )
        for path in host.rglob("*"):
            if path.is_symlink():
                raise ValueError("Calibration Runner products cannot contain symlinks")
            if not path.is_file():
                continue
            total += path.stat().st_size
            if total > 67108864 or len(fingerprints) > 2000:
                raise ValueError("Runner fingerprint exceeds its size budget")
            fingerprints[str(path.relative_to(source.parent))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return digest(fingerprints)


def measurement_scope(profile, spec, control):
    app = app_record(profile, control)
    device = device_query(profile, control, "details")
    properties = device.get("deviceProperties", {})
    os_build = properties.get("osBuildUpdate") or properties.get("osBuildVersion")
    if not os_build or not app.get("url") or not app.get("bundleVersion"):
        raise ValueError(
            "Device/App build metadata is incomplete; calibration cannot be bound"
        )
    toolchain = command(["xcodebuild", "-version"], timeout=10, control=control).strip()
    if not toolchain or len(toolchain) > 2048:
        raise ValueError("Xcode build metadata is unavailable")
    recipe = {
        k: spec.get(k, default)
        for k, default in (
            ("target_identifier", ""),
            ("origin_identifier", ""),
            ("reset_identifier", ""),
            ("max_reset_swipes", 3),
            ("hitch_collector", "signpost"),
            ("configuration_id", ""),
        )
    }
    recipe.update(gesture="swipeUp_fast", contract_version=1, bindings=spec["bindings"])
    return {
        "app_id": profile["app_id"],
        "device_id": profile["device_id"],
        "os_build": os_build,
        "toolchain_build": toolchain,
        "app_installation_sha256": digest(
            {
                k: app.get(k)
                for k in ("bundleIdentifier", "version", "bundleVersion", "url")
            }
        ),
        "runner_sha256": runner_digest(profile.get("xctestrun", "")),
        "recipe_sha256": digest(recipe),
        "collector": spec.get("hitch_collector", "signpost"),
    }
