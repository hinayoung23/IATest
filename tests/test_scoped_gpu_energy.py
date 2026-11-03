import pytest

from iatest.mobile.android_energy import measurements as energy_delta
from iatest.mobile.android_energy import parse_checkin
from iatest.mobile.emulator import require_owned
from iatest.mobile.store import validate_profile
from iatest.mobile.telemetry import measurements, metal_measurements


def metal(sample, intervals, completed):
    return {
        "sample_uptime_ms": sample,
        "retained_since_uptime_ms": 1,
        "completed": completed,
        "invalid": 0,
        "pending": 0,
        "intervals_ms": intervals,
    }


def test_metal_interval_union_is_clipped_to_the_proven_window():
    first = metal(100, [[90, 95]], 1)
    last = metal(200, [[90, 120], [140, 160], [190, 195]], 5)
    assert metal_measurements(first, last) == {"app_metal_gpu_busy_ratio": 0.45}
    # Duplicate concurrent buffer durations must already be merged by the SDK.
    last["intervals_ms"] = [[90, 150], [140, 160]]
    with pytest.raises(ValueError, match="overlapping"):
        metal_measurements(first, last)


@pytest.mark.parametrize(
    "field,value",
    [
        ("pending", 1),
        ("invalid", 1),
        ("retained_since_uptime_ms", 101),
        ("completed", 1),
    ],
)
def test_pending_failed_dropped_and_absent_buffers_cannot_prove_gpu_ratio(field, value):
    first, last = metal(100, [], 1), metal(200, [[110, 140]], 2)
    last[field] = value
    with pytest.raises(ValueError):
        metal_measurements(first, last)


def test_sdk_without_registered_buffers_preserves_other_telemetry():
    first = {
        "process_id": 7,
        "sequence": 1,
        "timestamp_ms": 100,
        "metal_gpu": metal(100, [], 0),
    }
    last = {
        **first,
        "sequence": 2,
        "timestamp_ms": 200,
        "metal_gpu": metal(200, [], 0),
        "performance": {"gpu_allocated_memory_kb": 32},
    }
    assert measurements(first, last) == {"gpu_allocated_memory_kb": 32}
    last["performance"]["app_metal_gpu_busy_ratio"] = 0.5
    with pytest.raises(ValueError, match="native interval"):
        measurements(first, last)


def checkin(amount="1.25", *, epoch=1700000000000):
    return f"9,0,l,bt,4,10,10,10,10,{epoch},1,1\n9,10123,l,pwi,uid,{amount},0,0\n9,10124,l,pwi,uid,99\n"


def test_uid_estimate_uses_exact_uid_since_charged_and_reset_identity():
    before = {**parse_checkin(checkin(), 10123), "uid": 10123, "boot_id": "boot"}
    after = {**parse_checkin(checkin("1.5"), 10123), "uid": 10123, "boot_id": "boot"}
    assert energy_delta(before, after) == {"app_estimated_battery_mah": 0.25}
    for field, value in (
        ("uid", 10124),
        ("epoch", (5, 1700000000001)),
        ("boot_id", "new"),
        ("estimated_mah", 0),
    ):
        with pytest.raises(ValueError):
            energy_delta(before, {**after, field: value})


def test_decimal_battery_delta_does_not_fail_an_equal_decimal_threshold():
    identity = {"uid": 10123, "boot_id": "boot"}
    before = {**parse_checkin(checkin("4.81"), 10123), **identity}
    after = {**parse_checkin(checkin("5.11"), 10123), **identity}
    result = energy_delta(before, after)
    assert result["app_estimated_battery_mah"] == 0.30
    assert result["app_estimated_battery_mah"] <= 0.30


@pytest.mark.parametrize(
    "content",
    [
        checkin("nan"),
        checkin("-1"),
        checkin() + "9,10123,l,pwi,uid,0\n",
        "9,10123,l,pwi,uid,0\n",
    ],
)
def test_missing_duplicate_nonfinite_or_negative_battery_data_is_not_zero(content):
    with pytest.raises(ValueError):
        parse_checkin(content, 10123)


def test_physical_or_unowned_device_cannot_claim_os_vm_isolation():
    profile = {
        "id": "test",
        "platform": "android",
        "device_id": "physical-phone",
        "app_id": "dev.iatest.lab",
        "require_os_sandbox": True,
    }
    with pytest.raises(ValueError, match="physical-device"):
        validate_profile(profile)
    profile["managed_emulator"] = {
        "sdk_root": "/sdk",
        "system_image": "android-36;google_apis;arm64-v8a",
        "artifact_id": "lab",
    }
    validate_profile(profile)
    with pytest.raises(ValueError, match="live VM"):
        require_owned(profile)
    profile["require_os_sandbox"] = False
    with pytest.raises(ValueError, match="live VM"):
        require_owned(profile)
