"""Actual HTTP/2 conversations: replies arrive before the client half-closes."""

import struct
import threading
import uuid

import pytest
from test_completion_protocol_data_desktop import descriptors
from test_wire_protocols import serve

from iatest.application.testing_service import execute_test_plan
from iatest.core.config import init_project, load_config, save_config
from iatest.engines.contracts import ExecutionPlan
from iatest.engines.protobuf import Messages
from iatest.engines.wire import validate_wire
from iatest.execution.control import RunControl


def conversation(sock, *, extra=False, missing=False):
    from h2.config import H2Configuration
    from h2.connection import H2Connection
    from h2.events import DataReceived, RequestReceived, StreamEnded

    conn = H2Connection(
        config=H2Configuration(client_side=False, header_encoding="utf-8")
    )
    conn.initiate_connection()
    sock.sendall(conn.data_to_send())
    codec = Messages(descriptors(True, True), "/test.Echo/Call")
    token = "nonce:" + uuid.uuid4().hex + "x" * 90000
    pending, outbound, pos, requests = bytearray(), bytearray(), 0, 0
    sid = None
    half_closed = False
    while data := sock.recv(65536):
        for event in conn.receive_data(data):
            if isinstance(event, RequestReceived):
                sid = event.stream_id
                conn.send_headers(
                    sid, [(":status", "200"), ("content-type", "application/grpc")]
                )
            if isinstance(event, DataReceived):
                assert not half_closed
                pending.extend(event.data)
                conn.acknowledge_received_data(event.flow_controlled_length, sid)
                while len(pending) >= 5:
                    size = struct.unpack(">I", pending[1:5])[0]
                    if len(pending) < size + 5:
                        break
                    message = codec.decode(bytes(pending[5 : size + 5]))
                    del pending[: size + 5]
                    assert message == {"text": "start" if requests == 0 else token}
                    requests += 1
                    body = codec.encode({"text": token if requests == 1 else "ack"})
                    outbound.extend(b"\0" + struct.pack(">I", len(body)) + body)
                    if extra and requests == 2:
                        outbound.extend(b"\0" + struct.pack(">I", len(body)) + body)
            if isinstance(event, StreamEnded):
                half_closed = True
        # Split the protobuf envelope itself as well as exercising h2 windows.
        while pos < len(outbound):
            available = min(
                conn.local_flow_control_window(sid), 8192, len(outbound) - pos
            )
            if not available:
                break
            if pos == 0:
                available = min(available, 2)
            conn.send_data(sid, bytes(outbound[pos : pos + available]))
            pos += available
        if half_closed and pos == len(outbound):
            if missing:
                conn.end_stream(sid)
            else:
                conn.send_headers(sid, [("grpc-status", "0")], end_stream=True)
            sock.sendall(conn.data_to_send())
            return
        sock.sendall(conn.data_to_send())


def plan(port, rounds=1):
    steps = [
        {"op": "send", "message": {"text": "start"}},
        {
            "op": "receive",
            "expected_schema": {
                "type": "object",
                "required": ["text"],
                "additionalProperties": False,
                "properties": {
                    "text": {
                        "type": "string",
                        "pattern": "^nonce:[a-f0-9]{32}x+$",
                        "maxLength": 100000,
                    }
                },
            },
        },
        {"op": "send", "message": {"text": {"$reply": "$.text"}}},
        {"op": "receive", "expected_message": {"text": "ack"}},
    ]
    # Every later send may still depend on the latest response.
    if rounds > 1:
        steps = (
            steps[:2]
            + [{"op": "send", "message": {"text": {"$reply": "$.text"}}}] * rounds
            + [steps[3]] * rounds
        )
    steps.append({"op": "half_close"})
    return {
        "id": "conversation",
        "kind": "protocol",
        "budget": {"timeout_seconds": 15, "max_requests": 1},
        "spec": {
            "transport": "grpc",
            "endpoint": f"grpc://127.0.0.1:{port}/test.Echo/Call",
            "allowed_origins": [f"http://127.0.0.1:{port}"],
            "protobuf": descriptors(True, True),
            "exchanges": [
                {
                    "op": "rpc",
                    "rpc_type": "bidi",
                    "dialogue_timeout_seconds": 10,
                    "expected_grpc_status": 0,
                    "dialogue": steps,
                }
            ],
        },
    }


@pytest.mark.parametrize("isolation", ["policy", "process"])
def test_real_response_driven_bidi_with_fragmentation_and_flow_control(
    tmp_path, isolation
):
    init_project(tmp_path, "suite")
    config = load_config(tmp_path)
    config["sandbox"]["mode"] = isolation
    save_config(tmp_path, config)
    with serve(conversation) as port:
        result = execute_test_plan(tmp_path, "suite", plan(port))
    assert result["verdict"] == "passed", result
    assert result["steps"][0]["sent_messages"] == 2
    assert result["steps"][0]["received_messages"] == 2
    assert "nonce:" not in str(result)


def test_real_conversation_can_exceed_legacy_128_message_limit(tmp_path):
    init_project(tmp_path, "suite")
    with serve(conversation) as port:
        result = execute_test_plan(tmp_path, "suite", plan(port, rounds=130))
    assert result["verdict"] == "passed", result
    assert result["steps"][0]["sent_messages"] == 131
    assert result["steps"][0]["received_messages"] == 131


@pytest.mark.parametrize("failure", ["extra", "missing"])
def test_extra_reply_and_missing_trailers_cannot_pass(tmp_path, failure):
    init_project(tmp_path, "suite")
    with serve(lambda sock: conversation(sock, **{failure: True})) as port:
        result = execute_test_plan(tmp_path, "suite", plan(port))
    assert result["verdict"] != "passed"
    assert result["steps"][0]["unknown_delivery"]


@pytest.mark.parametrize("parent_cancel", [False, True])
def test_dialogue_deadline_and_parent_cancellation_do_not_replay(
    tmp_path, parent_cancel
):
    from h2.config import H2Configuration
    from h2.connection import H2Connection
    from h2.events import RequestReceived

    cancelled = threading.Event()
    requests = []

    def silent(sock):
        connection = H2Connection(config=H2Configuration(client_side=False))
        connection.initiate_connection()
        sock.sendall(connection.data_to_send())
        while chunk := sock.recv(65536):
            for event in connection.receive_data(chunk):
                if isinstance(event, RequestReceived):
                    requests.append(event.stream_id)
                    if parent_cancel:
                        cancelled.set()
            sock.sendall(connection.data_to_send())

    init_project(tmp_path, "suite")
    with serve(silent) as port:
        value = plan(port)
        value["spec"]["exchanges"][0]["dialogue_timeout_seconds"] = (
            0.2 if not parent_cancel else 10
        )
        result = execute_test_plan(
            tmp_path,
            "suite",
            value,
            control=RunControl(timeout_seconds=5, cancellation_check=cancelled.is_set),
        )
    assert result["verdict"] != "passed", result
    assert len(requests) == 1
    if not parent_cancel:
        assert result["steps"][0]["unknown_delivery"]


@pytest.mark.parametrize("change", ["send_after_close", "early_reference", "quota"])
def test_dialogue_contract_rejects_invalid_order_and_budget(change):
    value = plan(1234)
    exchange = value["spec"]["exchanges"][0]
    if change == "send_after_close":
        exchange["dialogue"].append({"op": "send", "message": {}})
    elif change == "early_reference":
        exchange["dialogue"][0]["message"] = {"text": {"$reply": "$.text"}}
    else:
        exchange["max_messages"] = 1
    with pytest.raises(ValueError):
        validate_wire(ExecutionPlan.from_dict(value))
