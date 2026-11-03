import pytest

from iatest.engines.cpu import android_cpu, android_stat, chromium_cpu


def test_process_churn_and_counter_reset_cannot_manufacture_low_cpu():
    before = {1: ("browser", 3.0), 2: ("renderer", 1.0)}
    assert (
        chromium_cpu(
            before, {1: ("browser", 4.0), 2: ("renderer", 2.0), 3: ("utility", 0.5)}, 2
        )
        == 125
    )
    for after in ({1: ("browser", 4.0)}, {1: ("browser", 4.0), 2: ("renderer", 0.1)}):
        with pytest.raises(ValueError):
            chromium_cpu(before, after, 2)


def test_android_stat_handles_command_parentheses_and_rejects_restarted_pid():
    fields = ["S"] + ["0"] * 49
    fields[11], fields[12], fields[19] = "10", "5", "1000"
    value = android_stat("123 (app (worker)) " + " ".join(fields))
    assert value == (123, 1000, 15)
    assert android_cpu(value, (123, 1000, 35), 100, 2) == 10
    with pytest.raises(ValueError, match="restarted"):
        android_cpu(value, (123, 1001, 35), 100, 2)
