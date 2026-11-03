import json
import shutil

import pytest

from iatest.engines.protobuf import Messages, compile_sources
from iatest.execution.control import RunControl
from iatest.mobile.bundles import extract_set, inspect_set
from iatest.mobile.isolation import DeviceNetworkIsolation
from iatest.mobile.store import validate_profile
from iatest.mobile.telemetry import measurements
from iatest.mobile.webkit import Inspector, local_url


def test_console_profile_preserves_explicit_network_isolation_and_rejects_false_os_guarantee(
    tmp_path,
):
    import json

    from iatest.core.config import init_project
    from iatest.mobile.store import MobileStore
    from iatest.web.mobile import handle_mobile_action

    init_project(tmp_path, "suite")
    handler = type("Handler", (), {"redirect": lambda self, location: None})()
    isolation = {
        "backend": "android_root_firewall",
        "allowed_endpoints": [{"ip": "127.0.0.1", "port": 8080}],
    }
    data = {
        key: [value]
        for key, value in {
            "id": "boundary-only",
            "platform": "android",
            "device_id": "no-device-actions",
            "app_id": "dev.iatest.lab",
            "advanced_profile": json.dumps(
                {"isolation": isolation, "require_os_sandbox": False}
            ),
        }.items()
    }
    handle_mobile_action(tmp_path, "suite", "/actions/mobile-profile", data, handler)
    assert (
        MobileStore(tmp_path, "suite").get("profile", "boundary-only")["isolation"]
        == isolation
    )
    data["advanced_profile"] = [json.dumps({"require_os_sandbox": True})]
    with pytest.raises(ValueError, match="OS isolation"):
        handle_mobile_action(
            tmp_path, "suite", "/actions/mobile-profile", data, handler
        )


def test_real_proto_compiler_preserves_streaming_method_contract():
    if not shutil.which("protoc"):
        pytest.skip("Actual .proto compilation requires protoc")
    result = compile_sources(
        {
            "test.proto": 'syntax="proto3"; package test; message Value { string text=1; } service Echo { rpc Call(stream Value) returns(stream Value); }'
        }
    )
    codec = Messages(result, "/test.Echo/Call")
    assert codec.method.client_streaming and codec.method.server_streaming
    assert codec.decode(codec.encode({"text": "hello"})) == {"text": "hello"}
    with pytest.raises(ValueError):
        compile_sources({"../escaped.proto": 'syntax="proto3";'})


@pytest.mark.parametrize(
    "name", ["../escape.apk", "/absolute.apk", "nested\\escape.apk", "C:/outside.apk", "C:outside.apk", "base.apk:stream"]
)
def test_split_archive_cannot_escape_or_overwrite_files(tmp_path, name):
    import zipfile

    archive = tmp_path / "bad.apks"
    with zipfile.ZipFile(archive, "w") as target:
        member = zipfile.ZipInfo("placeholder.apk")
        member.filename = name  # Preserve raw ZIP syntax on Windows too.
        target.writestr(member, b"fixture")
    with pytest.raises(ValueError, match="unsafe"):
        extract_set(archive, tmp_path / "output")


def test_split_signer_or_version_mismatch_is_rejected_before_install(tmp_path):
    import zipfile

    archive = tmp_path / "mixed.apks"
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr("base.apk", b"base")
        target.writestr("config.apk", b"split")

    def inspect(path, platform):
        return {
            "app_id": "dev.example.app",
            "version_code": "1",
            "version": "1",
            "certificate_sha256": "a" if path.name == "base.apk" else "b",
        }

    with pytest.raises(ValueError, match="signers"):
        inspect_set(archive, tmp_path / "output", inspect)


def test_config_split_can_omit_display_version_and_base_metadata_is_retained(tmp_path):
    import zipfile

    archive = tmp_path / "real-shape.apks"
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr("config.apk", b"config")
        target.writestr("base.apk", b"base")
        target.writestr("base-sdk34.apk", b"base variant")

    def inspect(path, platform):
        base = path.name.startswith("base")
        return {"app_id": "dev.example.app", "version_code": "7", "version": "7.0" if base else "", "split_name": "" if base else "config.zh", "certificate_sha256": "a", "minimum_os": "26" if base else "21", "debuggable": base}

    result = inspect_set(archive, tmp_path / "output", inspect)
    assert result["version"] == "7.0" and result["minimum_os"] == "26"
    assert result["debuggable"] is True and len(result["apk_entries"]) == 3


def test_sdk_interval_requires_same_process_and_monotonic_evidence():
    before = {
        "process_id": 1,
        "sequence": 1,
        "timestamp_ms": 1000,
        "performance": {"app_network_rx_bytes": 100, "device_thermal_status": 1},
    }
    after = {
        "process_id": 1,
        "sequence": 2,
        "timestamp_ms": 2000,
        "performance": {
            "app_network_rx_bytes": 130,
            "device_thermal_status": 0,
            "device_battery_temperature_celsius": -5,
        },
    }
    assert measurements(before, after) == {
        "app_network_rx_bytes": 30,
        "device_thermal_status": 1,
        "device_battery_temperature_celsius": -5,
    }
    for bad in (
        {**after, "process_id": 2},
        {**after, "sequence": 1},
        {**after, "performance": {"app_network_rx_bytes": 90}},
    ):
        with pytest.raises(ValueError):
            measurements(before, bad)


class FirewallFixture:
    """Command boundary only; never evidence of Android kernel acceptance."""

    def __init__(self, fail_ipv6=False):
        self.profile = {
            "platform": "android",
            "app_id": "dev.iatest.test",
            "isolation": {
                "backend": "android_root_firewall",
                "allowed_endpoints": [{"ip": "127.0.0.1", "port": 8000}],
            },
        }
        self.control = RunControl(timeout_seconds=10)
        self.commands = []
        self.fail_ipv6 = fail_ipv6

    def adb(self, *args, **kwargs):
        self.commands.append(args)
        if args[:3] == ("shell", "cmd", "package"):
            return "package:dev.iatest.test uid:10123"
        value = args[-1]
        if value == "id -u":
            return "0"
        if self.fail_ipv6 and "ip6tables -w 2 -N" in value:
            raise RuntimeError("IPv6 unavailable")
        if " -S " in value:
            return "owned-rules"
        return ""


def test_target_network_isolation_checks_both_ip_families_and_owned_cleanup():
    device = FirewallFixture()
    isolation = DeviceNetworkIsolation(device)
    isolation.open()
    isolation.close()
    commands = "\n".join(str(c) for c in device.commands)
    assert "ip6tables -w 2" in commands and "--uid-owner 10123" in commands
    assert "-j REJECT" in commands and "--dport 8000" in commands
    assert "-F OUTPUT" not in commands
    assert f"-X {isolation.chain}" in commands


def test_ipv6_failure_rolls_back_owned_ipv4_jump_without_silent_fallback():
    device = FirewallFixture(fail_ipv6=True)
    isolation = DeviceNetworkIsolation(device)
    with pytest.raises(RuntimeError, match="IPv6"):
        isolation.open()
    assert any("iptables -w 2 -D OUTPUT" in args[-1] for args in device.commands)
    assert not isolation.created


def test_adb_root_firewall_cannot_use_a_physical_or_unowned_device(monkeypatch):
    device = FirewallFixture()
    device.profile["isolation"]["root_backend"] = "adb_root"
    isolation = DeviceNetworkIsolation(device)
    with pytest.raises(ValueError, match="owned Android VM"):
        isolation.open()
    assert not device.commands
    device.profile.update(device_id="emulator-5678", managed_emulator={"sdk_root": "/sdk"})
    with pytest.raises(ValueError, match="live VM"):
        isolation.open()
    assert not device.commands


def test_full_phone_os_sandbox_requirement_cannot_pass_as_host_isolation():
    with pytest.raises(ValueError, match="Full physical-device"):
        validate_profile(
            {
                "id": "phone",
                "platform": "ios",
                "device_id": "phone",
                "app_id": "dev.iatest.test",
                "require_os_sandbox": True,
            }
        )


def test_webkit_transport_never_accepts_another_target_or_remote_endpoint():
    for endpoint in (
        "http://192.168.0.100:9221",
        "http://user:secret@localhost:9221",
        "https://localhost:9221",
    ):
        with pytest.raises(ValueError):
            local_url(endpoint)
    inspector = Inspector.__new__(Inspector)
    inspector.target, inspector.routed = "one", True
    with pytest.raises(ValueError, match="another target"):
        inspector.receive(
            {
                "method": "Target.dispatchMessageFromTarget",
                "params": {
                    "targetId": "two",
                    "message": json.dumps({"id": 1000001, "result": {}}),
                },
            }
        )
    assert (
        inspector.receive(
            {
                "method": "Target.dispatchMessageFromTarget",
                "params": {
                    "targetId": "one",
                    "message": json.dumps({"id": 1000001, "result": {"value": 1}}),
                },
            }
        )["id"]
        == 1
    )
