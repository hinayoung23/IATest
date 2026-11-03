import json

import pytest

from iatest.mobile.bridge import parse_bridge


def observation():
    return {
        "contract_version": 1,
        "test_build": True,
        "app_id": "com.example.app",
        "build": "42",
        "process_id": 321,
        "sequence": 1,
        "timestamp_ms": 100000,
        "ready": {"cartLoaded": True},
        "identifiers": {"save": "com.example.app:id/save"},
    }


def test_bridge_is_bounded_and_bound_to_app_build_age_without_becoming_an_oracle():
    value = observation()
    result = parse_bridge(json.dumps(value).encode(), "com.example.app", "42", now=101)
    assert result["ready"]["cartLoaded"]
    assert "native query" in result["trust"]
    for app, build, now in [
        ("com.other.app", "42", 101),
        ("com.example.app", "43", 101),
        ("com.example.app", "42", 200),
    ]:
        with pytest.raises(ValueError):
            parse_bridge(json.dumps(value).encode(), app, build, now=now)
    value["test_build"] = False
    with pytest.raises(ValueError):
        parse_bridge(json.dumps(value).encode(), "com.example.app", "42", now=101)


def test_bridge_rejects_secrets_and_action_channels():
    value = observation()
    value["identifiers"]["password"] = "credential"
    with pytest.raises(ValueError, match="secret"):
        parse_bridge(json.dumps(value).encode(), "com.example.app", "42", now=101)
    value = observation()
    value["actions"] = {"pay": "internalPay"}
    with pytest.raises(ValueError):
        parse_bridge(json.dumps(value).encode(), "com.example.app", "42", now=101)
