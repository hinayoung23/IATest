from __future__ import annotations

import json
import socket
import struct
import threading
import time
from dataclasses import asdict
from typing import ClassVar
from urllib.parse import urlparse

import pytest

from iatest.core.config import init_project
from iatest.engines.contracts import EngineContext, ExecutionPlan
from iatest.execution.control import RunControl
from iatest.mobile.contracts import validate_mobile_plan
from iatest.mobile.lease import DeviceLease
from iatest.mobile.protocol import (
    MAX_FRAME,
    CommandRejected,
    DeliveryUnknown,
    DeviceChannel,
    ObservationRejected,
    ProtocolError,
    read_frame,
    write_frame,
)
from iatest.mobile.runtime import SESSIONS, MobileEngine, MobileSession
from iatest.mobile.store import MobileStore, validate_profile
from iatest.web.i18n import set_current_locale
from iatest.web.server import route_get


def profile():
    return {
        "id": "phone",
        "platform": "android",
        "device_id": "test-mobile-device",
        "app_id": "com.example.shop",
    }


def plan(steps=None, **budget):
    return ExecutionPlan.from_dict(
        {
            "id": "login",
            "kind": "android",
            "budget": budget,
            "spec": {
                "profile": "phone",
                "steps": steps
                or [
                    {"op": "find", "target": {"identifier": "login"}, "bind": "login"},
                    {"op": "tap", "ref": "login"},
                    {
                        "op": "expect",
                        "target": {"identifier": "status"},
                        "attribute": "text",
                        "expected": "success",
                    },
                ],
            },
        }
    )


class FakeChannel:
    """Controlled platform boundary. It does not claim to simulate Android input."""

    def __init__(self, owner):
        self.owner = owner
        self.capabilities = {
            "actions": ["query", "tap", "text", "swipe", "key", "state", "screenshot"],
            "input_modes": ["semantic", "key_events"],
        }

    def request(self, op, args, **kwargs):
        self.owner.calls.append((op, args, kwargs))
        if op == "query":
            text = "success" if self.owner.tapped else "before"
            return {
                "complete": self.owner.complete,
                "match_count": self.owner.matches,
                "state_version": 1,
                "display": {"id": 0, "width": 400, "height": 800, "rotation": 0},
                "visited_nodes": 5,
                "candidates": [
                    {
                        "ref": "fresh",
                        "text": text,
                        "value": text,
                        "enabled": True,
                        "bounds": [10, 10, 100, 80],
                    }
                ],
            }
        if op == "tap":
            self.owner.tapped = True
            if self.owner.unknown:
                raise DeliveryUnknown("Connection lost after injection")
            return {"status": "injection_call_returned", "accepted": True}
        return {"status": "received"}


class FakeDevice:
    instances: ClassVar[list] = []
    complete = True
    matches = 1
    unknown = False

    def __init__(self, profile, session_id):
        self.calls = []
        self.tapped = False
        self.closed = False
        self.semantic = self.input = FakeChannel(self)
        self.instances.append(self)

    def open(self):
        return self

    def close(self):
        self.closed = True


@pytest.fixture
def repository(tmp_path):
    store = MobileStore(tmp_path, "demo")
    store.save_profile(profile())
    FakeDevice.instances = []
    return store


def test_failed_expectation_retains_last_native_observation(tmp_path, repository):
    result = MobileEngine(
        tmp_path, "demo", "android", device_factory=FakeDevice
    ).execute(
        plan(
            [
                {
                    "op": "expect",
                    "target": {"identifier": "status"},
                    "attribute": "text",
                    "expected": "saved",
                    "timeout_ms": 1,
                }
            ]
        ),
        EngineContext(RunControl(timeout_seconds=5)),
    )
    assert result.verdict == "failed"
    assert result.steps[-1]["observation"]["candidates"][0]["text"] == "before"
    assert result.steps[-1]["observation"]["complete"] is True
    assert FakeDevice.instances[-1].closed


@pytest.mark.parametrize("platform,timeout", [("android", 0.5), ("ios", 5)])
def test_platform_query_wait_is_bounded_without_changing_assertions(
    tmp_path, repository, platform, timeout
):
    repository.save_profile({**profile(), "platform": platform})
    value = asdict(
        plan(
            [
                {
                    "op": "expect",
                    "target": {"identifier": "status"},
                    "attribute": "text",
                    "expected": "before",
                }
            ],
            timeout_seconds=10,
        )
    )
    value["kind"] = platform
    result = MobileEngine(
        tmp_path, "demo", platform, device_factory=FakeDevice
    ).execute(
        ExecutionPlan.from_dict(value), EngineContext(RunControl(timeout_seconds=10))
    )
    assert result.verdict == "passed"
    queries = [
        kwargs for op, _, kwargs in FakeDevice.instances[-1].calls if op == "query"
    ]
    assert queries and all(0 < kwargs["timeout"] <= timeout for kwargs in queries)
    if platform == "ios":
        assert queries[0]["timeout"] > 4
    assert result.steps[-1]["observation"]["complete"] is True


def test_late_ios_observation_cannot_pass_an_expired_step_wait(tmp_path, repository):
    class SlowChannel(FakeChannel):
        def request(self, op, args, **kwargs):
            if op == "query":
                assert 0 < kwargs["timeout"] <= 0.02
                time.sleep(0.03)  # Opaque platform boundary ignores its deadline.
            return super().request(op, args, **kwargs)

    class SlowDevice(FakeDevice):
        def __init__(self, profile, session_id):
            super().__init__(profile, session_id)
            self.input = self.semantic = SlowChannel(self)

    repository.save_profile({**profile(), "platform": "ios"})
    value = asdict(
        plan(
            [
                {
                    "op": "expect",
                    "target": {"identifier": "status"},
                    "attribute": "text",
                    "expected": "before",
                    "timeout_ms": 20,
                }
            ]
        )
    )
    value["kind"] = "ios"
    result = MobileEngine(tmp_path, "demo", "ios", device_factory=SlowDevice).execute(
        ExecutionPlan.from_dict(value), EngineContext(RunControl(timeout_seconds=5))
    )
    assert (
        result.verdict == "inconclusive" and "after its declared wait" in result.error
    )
    assert result.steps[-1]["observation"]["complete"] is True


@pytest.mark.parametrize(
    "platform,mode,timeout", [("android", "semantic", 5), ("ios", "keyboard", 20)]
)
def test_saved_mobile_template_materializes_new_data_for_each_registry_run(
    tmp_path, repository, platform, mode, timeout
):
    from iatest.engines.contracts import EngineRegistry

    class InputChannel(FakeChannel):
        def request(self, op, args, **kwargs):
            if op == "text":
                self.owner.calls.append((op, args, kwargs))
                self.owner.value = args["value"]
                return {"status": "injection_call_returned", "accepted": True}
            response = super().request(op, args, **kwargs)
            if op == "query":
                for node in response["candidates"]:
                    node["text"] = getattr(self.owner, "value", "")
            return response

    class InputDevice(FakeDevice):
        def __init__(self, profile, session_id):
            super().__init__(profile, session_id)
            self.input = self.semantic = InputChannel(self)
            self.input.capabilities["input_modes"].append("keyboard")

    repository.save_profile({**profile(), "platform": platform})
    template = asdict(
        plan(
            [
                {
                    "op": "text",
                    "target": {"identifier": "field"},
                    "mode": mode,
                    "value": {"$value": "keyword"},
                },
                {
                    "op": "expect",
                    "target": {"identifier": "field"},
                    "attribute": "text",
                    "expected": {"$value": "keyword"},
                },
            ]
        )
    )
    template["kind"] = platform
    template["budget"]["timeout_seconds"] = 30
    template["data_generation"] = {
        "keyword": {
            "kind": "string",
            "prefix": "iatest-",
            "minLength": 20,
            "maxLength": 24,
            "unique": True,
        },
    }
    repository.save_plan(template)
    saved = ExecutionPlan.from_dict(repository.get("plan", template["id"]))
    registry = EngineRegistry()
    registry.register(
        platform, MobileEngine(tmp_path, "demo", platform, device_factory=InputDevice)
    )
    values = []
    for _ in range(2):
        result = registry.execute(saved, EngineContext(RunControl(timeout_seconds=30)))
        assert result.verdict == "passed", result
        values.append(InputDevice.instances[-1].value)
        text_calls = [
            kwargs for op, _, kwargs in InputDevice.instances[-1].calls if op == "text"
        ]
        assert text_calls and all(timeout - 1 < kwargs["timeout"] <= timeout for kwargs in text_calls)
    assert values[0] != values[1] and all(v.startswith("iatest-") for v in values)
    assert saved.spec["steps"][0]["value"] == {"$value": "keyword"}


def test_mobile_plan_rejects_missing_or_unbound_targets_and_missing_assertions():
    with pytest.raises(ValueError, match="expectation"):
        validate_mobile_plan(plan([{"op": "tap", "target": {"text": "Login"}}]))
    with pytest.raises(ValueError, match="previously bound"):
        validate_mobile_plan(
            plan(
                [
                    {"op": "tap", "ref": "unknown"},
                    {"op": "expect", "target": {"text": "ok"}, "expected": True},
                ]
            )
        )
    with pytest.raises(ValueError):
        ExecutionPlan.from_dict(
            {"id": "a", "kind": "android", "spec": {}, "arbitrary_shell": "whoami"}
        )


def test_fragment_has_finite_fresh_targets_and_platform_input_mode(repository):
    steps = [
        {
            "op": "text",
            "target": {"identifier": "name"},
            "mode": "semantic",
            "value": "fixture",
        },
        {"op": "tap", "target": {"identifier": "save"}},
        {
            "op": "expect",
            "target": {"identifier": "status"},
            "attribute": "text",
            "expected": "saved",
        },
    ]
    value = plan([{"op": "fragment", "steps": steps}])
    validate_mobile_plan(value)
    with pytest.raises(ValueError, match="16 KiB"):
        validate_mobile_plan(
            plan(
                [
                    {
                        "op": "fragment",
                        "steps": [dict(steps[0], value="界" * 6000), steps[-1]],
                    }
                ]
            )
        )
    huge = dict(steps[0], value="界" * 5000)
    with pytest.raises(ValueError, match="wire budget"):
        validate_mobile_plan(
            plan([{"op": "fragment", "steps": [huge] * 4 + [steps[-1]]}])
        )
    steps[0]["mode"] = "keyboard"
    with pytest.raises(ValueError, match="mode"):
        validate_mobile_plan(value)
    steps[0]["mode"] = "semantic"
    steps.pop()
    with pytest.raises(ValueError, match="expectation"):
        validate_mobile_plan(value)


def test_fragment_is_one_native_dispatch_and_prechecks_whole_budget(repository):
    class FragmentChannel(FakeChannel):
        def __init__(self, owner):
            super().__init__(owner)
            self.capabilities["actions"].append("fragment")

        def request(self, op, args, **kwargs):
            if op == "fragment":
                self.owner.calls.append((op, args, kwargs))
                return {
                    "status": "injection_call_returned",
                    "accepted": True,
                    "postcondition_verified": True,
                    "dispatched": 2,
                }
            return super().request(op, args, **kwargs)

    class FragmentDevice(FakeDevice):
        def __init__(self, profile, session_id):
            super().__init__(profile, session_id)
            self.semantic = self.input = FragmentChannel(self)

    steps = [
        {"op": "tap", "target": {"identifier": "first"}},
        {"op": "tap", "target": {"identifier": "second"}},
        {"op": "expect", "target": {"identifier": "status"}, "expected": True},
    ]
    result = MobileEngine(
        repository.directory.parents[3],
        "demo",
        "android",
        device_factory=FragmentDevice,
    )
    # Use the same root as the repository fixture rather than operating any platform.
    result.store = repository
    control = RunControl(timeout_seconds=10)
    passed = result.execute(
        plan([{"op": "fragment", "steps": steps}]), EngineContext(control)
    )
    assert passed.verdict == "passed", passed
    assert [op for op, _, _ in FragmentDevice.instances[-1].calls] == ["fragment"]
    blocked = result.execute(
        plan([{"op": "fragment", "steps": steps}], max_actions=1),
        EngineContext(RunControl(timeout_seconds=10)),
    )
    assert blocked.verdict == "inconclusive"
    assert not FragmentDevice.instances[-1].calls


def test_mobile_plan_requires_a_postcondition_after_the_last_mutation():
    with pytest.raises(ValueError, match="after its last action"):
        validate_mobile_plan(
            plan(
                [
                    {"op": "expect", "target": {"text": "Login"}, "expected": True},
                    {"op": "tap", "target": {"text": "Login"}},
                ]
            )
        )


def test_mobile_text_limit_counts_utf8_bytes_before_execution():
    with pytest.raises(ValueError, match="16 KiB"):
        validate_mobile_plan(
            plan(
                [
                    {
                        "op": "text",
                        "target": {"identifier": "field"},
                        "mode": "semantic",
                        "value": "中" * 6000,
                    },
                    {"op": "expect", "target": {"text": "ok"}, "expected": True},
                ]
            )
        )


def test_nonfinite_plan_budget_is_rejected_before_it_can_disable_deadlines():
    with pytest.raises(ValueError, match="finite JSON"):
        plan(timeout_seconds=float("nan"))


def test_key_events_cannot_type_into_an_unfocused_target(tmp_path, repository):
    steps = [
        {
            "op": "text",
            "target": {"identifier": "field"},
            "mode": "key_events",
            "value": "abc",
        },
        {
            "op": "expect",
            "target": {"identifier": "field"},
            "attribute": "text",
            "expected": "abc",
        },
    ]
    result = MobileEngine(
        tmp_path, "demo", "android", device_factory=FakeDevice
    ).execute(plan(steps), EngineContext(RunControl()))
    assert result.verdict == "inconclusive"
    assert not any(c[0] == "text" for c in FakeDevice.instances[-1].calls)


@pytest.mark.parametrize("ambiguous", [False, True])
def test_masked_or_ambiguous_text_cannot_prove_a_raw_value_assertion(
    tmp_path, repository, ambiguous
):
    class SecureChannel(FakeChannel):
        def request(self, op, args, **kwargs):
            value = super().request(op, args, **kwargs)
            if op == "query":
                value["candidates"][0].update(
                    {"ambiguous_attributes": ["text", "value"], "value": "Nickname"}
                    if ambiguous
                    else {"secure": True, "text": "[REDACTED]", "value": "[REDACTED]"}
                )
            return value

    class SecureDevice(FakeDevice):
        def open(self):
            self.input = self.semantic = SecureChannel(self)
            return self

    steps = [
        {
            "op": "expect",
            "target": {"identifier": "password"},
            "attribute": "value",
            "expected": "Nickname" if ambiguous else "[REDACTED]",
        }
    ]
    result = MobileEngine(
        tmp_path, "demo", "android", device_factory=SecureDevice
    ).execute(plan(steps), EngineContext(RunControl()))
    assert result.verdict == "inconclusive"


def test_driver_diagnostics_are_bounded_and_do_not_expose_session_credentials():
    import sys

    from iatest.mobile.devices import NativeDevice

    device = NativeDevice(profile(), "diagnostic-session")
    try:
        device._start(
            [
                sys.executable,
                "-c",
                "import sys;print('x'*100000);print(sys.argv[1])",
                device.token,
            ]
        )
        device.processes[0].wait(timeout=5)
        device.log_threads[0].join(timeout=2)
        assert len(device.logs[0]) <= 65536
        assert device.token not in device.diagnostics()
        assert "[REDACTED]" in device.diagnostics()
    finally:
        device.close()


@pytest.mark.parametrize(
    "update",
    [
        {"simulator": "false"},
        {"runner_port": True},
        {"app_id": "x; shell"},
        {"device_id": "$(whoami)"},
    ],
)
def test_profile_rejects_ambiguous_flags_and_shell_characters(update):
    with pytest.raises(ValueError):
        validate_profile({**profile(), **update})


def test_mobile_run_requires_postconditions_and_revalidates_bound_target(
    tmp_path, repository
):
    result = MobileEngine(
        tmp_path, "demo", "android", device_factory=FakeDevice
    ).execute(plan(), EngineContext(RunControl()))
    assert result.verdict == "passed"
    device = FakeDevice.instances[-1]
    assert [c[0] for c in device.calls] == ["query", "query", "tap", "query"]
    assert device.closed
    assert result.steps[1]["status"] == "injection_call_returned"
    assert result.steps[-1]["status"] == "postcondition_verified"
    assert repository.runs()[0]["run_id"] == result.run_id


@pytest.mark.parametrize("complete,matches", [(False, 1), (True, 2), (True, 0)])
def test_incomplete_or_ambiguous_observation_never_dispatches(
    tmp_path, repository, complete, matches
):
    class Uncertain(FakeDevice):
        pass

    Uncertain.complete = complete
    Uncertain.matches = matches
    result = MobileEngine(
        tmp_path, "demo", "android", device_factory=Uncertain
    ).execute(plan(), EngineContext(RunControl()))
    assert result.verdict == "inconclusive"
    assert not any(c[0] == "tap" for c in Uncertain.instances[-1].calls)
    assert Uncertain.instances[-1].closed


def test_unknown_side_effect_is_persisted_without_retry(tmp_path, repository):
    class Lost(FakeDevice):
        unknown = True

    result = MobileEngine(tmp_path, "demo", "android", device_factory=Lost).execute(
        plan(), EngineContext(RunControl())
    )
    assert result.verdict == "inconclusive" and result.steps[-1]["status"] == "unknown"
    assert [c[0] for c in Lost.instances[-1].calls].count("tap") == 1
    assert repository.runs()[0]["steps"][-1]["status"] == "unknown"


def test_action_budget_stops_before_next_side_effect(tmp_path, repository):
    steps = [
        {"op": "tap", "target": {"text": "one"}},
        {"op": "tap", "target": {"text": "two"}},
        {"op": "expect", "target": {"text": "ok"}, "expected": True},
    ]
    result = MobileEngine(
        tmp_path, "demo", "android", device_factory=FakeDevice
    ).execute(plan(steps, max_actions=1), EngineContext(RunControl()))
    assert result.verdict == "inconclusive"
    assert [c[0] for c in FakeDevice.instances[-1].calls].count("tap") == 1


def test_capability_preflight_rejects_unsupported_text_before_any_tap(
    tmp_path, repository
):
    steps = [
        {"op": "tap", "target": {"text": "one"}},
        {
            "op": "text",
            "target": {"text": "field"},
            "mode": "keyboard",
            "value": "中文",
        },
        {"op": "expect", "target": {"text": "ok"}, "expected": True},
    ]
    result = MobileEngine(
        tmp_path, "demo", "android", device_factory=FakeDevice
    ).execute(plan(steps), EngineContext(RunControl()))
    assert result.verdict == "inconclusive"
    assert FakeDevice.instances[-1].calls == []


def test_cancelled_run_releases_native_device_and_lease(tmp_path, repository):
    control = RunControl()

    class Cancelling(FakeDevice):
        def open(self):
            control.cancelled.set()
            return self

    result = MobileEngine(
        tmp_path, "demo", "android", device_factory=Cancelling
    ).execute(plan(), EngineContext(control))
    assert result.lifecycle == "cancelled" and Cancelling.instances[-1].closed
    lease = DeviceLease("android", profile()["device_id"])
    lease.acquire()
    lease.close()


def test_plan_deadline_also_stops_side_effects_during_driver_startup(
    tmp_path, repository
):
    class SlowStartup(FakeDevice):
        def open(self):
            time.sleep(0.04)
            self.control.check()
            self.calls.append(("late-startup-side-effect", {}, {}))
            return self

    result = MobileEngine(
        tmp_path, "demo", "android", device_factory=SlowStartup
    ).execute(plan(timeout_seconds=0.02), EngineContext(RunControl()))
    assert result.lifecycle == "timed_out"
    assert not SlowStartup.instances[-1].calls
    assert SlowStartup.instances[-1].closed


def test_device_lease_excludes_a_second_local_controller():
    first = DeviceLease("android", "exclusive-test")
    second = DeviceLease("android", "exclusive-test")
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="already leased"):
            second.acquire()
    finally:
        first.close()
    second.acquire()
    second.close()


@pytest.mark.parametrize(
    "payload", [b'{"x":1,"x":2}', b'{"x":NaN}', b"[]", b'"not an object"']
)
def test_control_protocol_rejects_invalid_objects(payload):
    client, server = socket.socketpair()
    try:
        server.sendall(struct.pack(">I", len(payload)) + payload)
        with pytest.raises(ProtocolError):
            read_frame(client)
    finally:
        client.close()
        server.close()


@pytest.mark.parametrize("size", [0, MAX_FRAME + 1, 0xFFFFFFFF])
def test_control_protocol_rejects_length_before_reading_body(size):
    client, server = socket.socketpair()
    try:
        server.sendall(struct.pack(">I", size))
        with pytest.raises(ProtocolError):
            read_frame(client)
    finally:
        client.close()
        server.close()


def test_control_transport_handles_fragmentation_and_concatenation():
    client, server = socket.socketpair()
    values = [{"text": "中文😀"}, {"status": "received"}]

    def send():
        for value in values:
            payload = json.dumps(value, ensure_ascii=False).encode()
            for byte in struct.pack(">I", len(payload)) + payload:
                server.sendall(bytes([byte]))

    thread = threading.Thread(target=send)
    thread.start()
    try:
        assert [read_frame(client), read_frame(client)] == values
    finally:
        thread.join(timeout=2)
        client.close()
        server.close()


@pytest.mark.parametrize("before_dispatch", [False, True])
@pytest.mark.parametrize("op", ["tap", "web_tap", "web_type", "clipboard_set", "clipboard_restore"])
def test_correlated_rejection_is_not_unknown_but_lost_ack_is(before_dispatch, op):
    client, server = socket.socketpair()

    def backend():
        hello = read_frame(server)
        write_frame(
            server,
            {
                "protocol": "iatest-mobile/1",
                "session_id": "s",
                "driver_epoch": "e",
                "sequence": hello["sequence"],
                "status": "received",
            },
        )
        tap = read_frame(server)
        write_frame(
            server,
            {
                "protocol": "iatest-mobile/1",
                "session_id": "s",
                "driver_epoch": "e",
                "sequence": tap["sequence"],
                "status": "rejected",
                "error": "stale",
                **({"dispatch_started": False, "error_code": "stale_observation"} if before_dispatch else {}),
            },
        )
        read_frame(server)
        server.close()

    thread = threading.Thread(target=backend)
    thread.start()
    channel = DeviceChannel(
        client, session_id="s", token="test", app_id="com.example.app"
    )
    unknown = []
    channel.on_unknown = lambda: unknown.append(True)
    try:
        with pytest.raises(ObservationRejected if before_dispatch else CommandRejected, match="stale"):
            channel.request(op, {}, operation_id="one")
        assert not channel.closed
        assert not unknown
        with pytest.raises(DeliveryUnknown):
            channel.request(op, {}, operation_id="two")
        assert channel.closed
        assert unknown == [True]
    finally:
        thread.join(timeout=2)
        channel.close()


@pytest.mark.parametrize("rejection", [ObservationRejected, CommandRejected, DeliveryUnknown])
def test_only_proven_pre_dispatch_rejection_can_refresh_before_input(tmp_path, repository, rejection):
    class RejectOnce(FakeChannel):
        def request(self, op, args, **kwargs):
            if op == "tap":
                self.owner.ids.append(kwargs["operation_id"])
                if len(self.owner.ids) == 1:
                    raise rejection("stale or unconfirmed")
            return super().request(op, args, **kwargs)

    class Device(FakeDevice):
        def __init__(self, profile, session_id):
            super().__init__(profile, session_id)
            self.ids = []
            self.semantic = self.input = RejectOnce(self)

    result = MobileEngine(tmp_path, "demo", "android", device_factory=Device).execute(
        plan(), EngineContext(RunControl(timeout_seconds=5))
    )
    device = FakeDevice.instances[-1]
    if rejection is ObservationRejected:
        assert result.verdict == "passed"
        assert len(device.ids) == len(set(device.ids)) == 2
        assert result.metrics["actions"] == 2
        assert result.steps[1]["pre_dispatch_rejections"] == 1
    else:
        assert result.verdict == "inconclusive"
        assert len(device.ids) == 1
        assert not device.tapped


@pytest.mark.parametrize("locale", ["zh", "en", "ja", "fr", "ru"])
def test_all_mobile_console_tabs_render_with_project_navigation(tmp_path, locale):
    init_project(tmp_path, "demo")
    store = MobileStore(tmp_path, "demo")
    store.save_profile(profile())
    template = asdict(
        plan(
            [
                {
                    "op": "text",
                    "target": {"identifier": "field"},
                    "mode": "semantic",
                    "value": {"$value": "keyword"},
                },
                {
                    "op": "expect",
                    "target": {"text": {"$value": "keyword"}},
                    "attribute": "text",
                    "expected": {"$value": "keyword"},
                },
            ]
        )
    )
    template["data_generation"] = {"keyword": {"kind": "string", "unique": True}}
    store.save_plan(template)
    try:
        set_current_locale(locale)
        for tab in ["devices", "plans", "inspect", "runs"]:
            content = route_get(
                tmp_path, "demo", urlparse(f"/mobile?project=demo&tab={tab}" + ("&method=manual" if tab == "plans" else ""))
            )
            assert "workspace-local-tabs" in content and 'name="_csrf"' in content
            assert "/assets?project=demo" in content
            assert "{{" not in content
            if tab == "plans":
                assert "$value" in content and "keyword" in content
    finally:
        set_current_locale("zh")


def test_mobile_plan_generation_uses_audited_role_and_does_not_dispatch(
    tmp_path, repository, monkeypatch
):
    from iatest.ai.provider import ModelResponse, ModelToolCall, TokenUsage
    from iatest.application.mobile_service import draft_mobile_plan

    session = MobileSession(profile(), device_factory=FakeDevice)
    monkeypatch.setattr(SESSIONS, "get", lambda root, project: session)

    class Gateway:
        def __init__(self, config):
            self.model_config = config

        def complete(self, **kwargs):
            return ModelResponse(
                tool_calls=[
                    ModelToolCall(
                        "submit", "submit_mobile_plan", {"plan": asdict(plan())}
                    )
                ],
                usage=TokenUsage(100, 30, 130),
            )

    try:
        result = draft_mobile_plan(
            tmp_path, "demo", "phone", "登录后应该显示 success", gateway_factory=Gateway
        )
        assert result["plan"]["id"] == "login"
        assert not session.device.calls
        assert result["agent_run_id"]
        assert repository.get("plan", "login")["kind"] == "android"
    finally:
        session.close()
def test_native_automation_startup_error_survives_xcode_diagnostic_boilerplate():
    from iatest.mobile.devices import NativeDevice

    device = object.__new__(NativeDevice)
    device.token = "synthetic-runner-secret"
    actual = "Runner encountered an error (failed to initialize for UI testing: Timed out while enabling automation mode) synthetic-runner-secret"
    device.logs = [bytearray((actual + "\n" + "Automatic diagnostic collection information\n" * 150).encode())]
    result = device.diagnostics()
    assert "Timed out while enabling automation mode" in result
    assert "synthetic-runner-secret" not in result and "[REDACTED]" in result
