"""Real SQLite/HTTP2 checks plus explicit desktop exploration contract tests."""

import base64
import hashlib
import sqlite3
import struct
import threading
import time

import pytest
from test_wire_protocols import serve, websocket

from iatest.application.testing_service import execute_test_plan
from iatest.application.ui_exploration import _context, assertion_step, context_spec
from iatest.core.config import init_project, load_config, save_config
from iatest.engines.contracts import ExecutionPlan
from iatest.engines.protobuf import Messages
from iatest.engines.validation import validate_plan
from iatest.execution.control import RunControl
from iatest.nodes.wire_load import allocate_wire


def descriptors(client=False, server=False):
    from google.protobuf import descriptor_pb2

    file = descriptor_pb2.FileDescriptorProto(
        name="echo.proto", package="test", syntax="proto3"
    )
    message = file.message_type.add(name="Message")
    message.field.add(name="text", number=1, type=9, label=1)
    service = file.service.add(name="Echo")
    service.method.add(
        name="Call",
        input_type=".test.Message",
        output_type=".test.Message",
        client_streaming=client,
        server_streaming=server,
    )
    raw = descriptor_pb2.FileDescriptorSet(file=[file]).SerializeToString()
    return {
        "descriptor_set_base64": base64.b64encode(raw).decode(),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def echo_stream(sock, mode="echo"):
    from h2.config import H2Configuration
    from h2.connection import H2Connection
    from h2.events import DataReceived, StreamEnded

    conn = H2Connection(
        config=H2Configuration(client_side=False, header_encoding="utf-8")
    )
    conn.initiate_connection()
    sock.sendall(conn.data_to_send())
    data = bytearray()
    while packet := sock.recv(65536):
        for event in conn.receive_data(packet):
            if isinstance(event, DataReceived):
                data.extend(event.data)
                conn.acknowledge_received_data(
                    event.flow_controlled_length, event.stream_id
                )
            if isinstance(event, StreamEnded):
                conn.send_headers(
                    event.stream_id,
                    [(":status", "200"), ("content-type", "application/grpc")],
                )
                payload = bytes(data)
                if mode == "server":
                    payload += payload
                elif mode == "client":
                    payload = payload[: 5 + struct.unpack(">I", payload[1:5])[0]]
                conn.send_data(event.stream_id, payload)
                conn.send_headers(
                    event.stream_id, [("grpc-status", "0")], end_stream=True
                )
        sock.sendall(conn.data_to_send())


@pytest.mark.parametrize("isolation", ["policy", "process"])
@pytest.mark.parametrize(
    "client_stream,server_stream",
    [(False, False), (False, True), (True, False), (True, True)],
)
def test_real_structured_grpc_echo_with_independent_h2_server(
    tmp_path, isolation, client_stream, server_stream
):
    init_project(tmp_path, "suite")
    config = load_config(tmp_path)
    config["sandbox"]["mode"] = isolation
    save_config(tmp_path, config)
    mode = (
        "echo"
        if client_stream == server_stream
        else "client"
        if client_stream
        else "server"
    )
    with serve(lambda sock: echo_stream(sock, mode)) as port:
        inputs = (
            [{"text": "one"}, {"text": "two"}] if client_stream else [{"text": "one"}]
        )
        expected = (
            inputs
            if client_stream == server_stream
            else inputs[:1]
            if client_stream
            else inputs * 2
        )
        result = execute_test_plan(
            tmp_path,
            "suite",
            {
                "id": "structured",
                "kind": "protocol",
                "spec": {
                    "transport": "grpc",
                    "endpoint": f"grpc://127.0.0.1:{port}/test.Echo/Call",
                    "allowed_origins": [f"http://127.0.0.1:{port}"],
                    "protobuf": descriptors(client_stream, server_stream),
                    "exchanges": [
                        {"op": "rpc", "messages": inputs, "expected_messages": expected}
                    ],
                },
            },
        )
        assert result["verdict"] == "passed", result
        assert "one" not in str(result["steps"])


def test_descriptor_hash_and_required_method_are_enforced():
    spec = descriptors()
    with pytest.raises(ValueError, match="hash"):
        Messages({**spec, "sha256": "0" * 64}, "/test.Echo/Call")
    with pytest.raises(ValueError, match="method"):
        Messages(spec, "/test.Echo/Missing")
    with pytest.raises(ValueError, match="request"):
        Messages(spec, "/test.Echo/Call").encode({"missing": 1})


@pytest.mark.parametrize("isolation", ["policy", "process"])
def test_real_empty_bidi_stream_ends_with_headers_and_valid_trailers(
    tmp_path, isolation
):
    init_project(tmp_path, "suite")
    config = load_config(tmp_path)
    config["sandbox"]["mode"] = isolation
    save_config(tmp_path, config)
    with serve(echo_stream) as port:
        result = execute_test_plan(
            tmp_path,
            "suite",
            {
                "id": "empty-bidi",
                "kind": "protocol",
                "spec": {
                    "transport": "grpc",
                    "endpoint": f"grpc://127.0.0.1:{port}/test.Echo/Call",
                    "allowed_origins": [f"http://127.0.0.1:{port}"],
                    "protobuf": descriptors(True, True),
                    "exchanges": [
                        {"op": "rpc", "messages": [], "expected_messages": []}
                    ],
                },
            },
        )
    assert result["verdict"] == "passed", result


def test_real_two_worker_protocol_rate_and_raw_sample_aggregation(
    tmp_path, monkeypatch
):
    from iatest.nodes.protocol import make_controller
    from iatest.nodes.store import ControllerStore
    from iatest.nodes.worker import NodeWorker

    root = tmp_path / "controller"
    init_project(root, "suite")
    controller = make_controller(root, "suite")
    controller_thread = threading.Thread(target=controller.serve_forever, daemon=True)
    controller_thread.start()
    store = ControllerStore(root, "suite")
    workers, threads = [], []
    try:
        with serve(websocket) as port:
            origin = f"http://127.0.0.1:{port}"
            for node in ("a", "b"):
                env = "IATEST_TEST_WIRE_NODE_" + node.upper()
                monkeypatch.setenv(env, "local-test-credential-" + node)
                store.enroll(
                    {
                        "id": node,
                        "token_env": env,
                        "allowed_origins": [origin],
                        "allowed_kinds": ["performance"],
                    }
                )
                local = tmp_path / node
                init_project(local, "suite")
                worker = NodeWorker(
                    local,
                    "suite",
                    f"http://127.0.0.1:{controller.server_port}",
                    node,
                    env,
                    [origin],
                    lease_seconds=1,
                )
                thread = threading.Thread(target=worker.run, daemon=True)
                thread.start()
                workers.append(worker)
                threads.append(thread)
            deadline = time.monotonic() + 3
            while (
                not all(n["online"] for n in store.nodes())
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            result = execute_test_plan(
                root,
                "suite",
                {
                    "id": "global-wire",
                    "kind": "performance",
                    "budget": {"timeout_seconds": 15, "max_requests": 12},
                    "spec": {
                        "transport": "websocket",
                        "endpoint": f"ws://127.0.0.1:{port}/",
                        "allowed_origins": [origin],
                        "load": {"users": 2, "rate": 10, "duration_seconds": 0.6},
                        "distribution": {"nodes": ["a", "b"]},
                        "thresholds": {"max_error_ratio": 0, "max_dropped_ratio": 0},
                        "exchanges": [
                            {"op": "send", "text": "echo", "expect_text": "echo"}
                        ],
                    },
                },
            )
            assert result["verdict"] == "passed", result
            assert result["metrics"]["complete"] is True
            assert result["metrics"]["sessions"] == result["metrics"]["scheduled"] == 6
            assert result["metrics"]["dropped"] == 0
            assert result["metrics"]["p95_ms"] > 0
    finally:
        for worker in workers:
            worker.stop.set()
        for thread in threads:
            thread.join(timeout=5)
        for worker in workers:
            worker.close()
        controller.shutdown()
        controller.server_close()
        controller_thread.join(2)


def test_known_protocol_assertion_failures_respect_declared_error_tolerance(tmp_path):
    init_project(tmp_path, "suite")
    with serve(websocket) as port:
        plan = {
            "id": "tolerance",
            "kind": "protocol",
            "spec": {
                "transport": "websocket",
                "endpoint": f"ws://127.0.0.1:{port}/",
                "allowed_origins": [f"http://127.0.0.1:{port}"],
                "thresholds": {"max_error_ratio": 1},
                "exchanges": [
                    {"op": "send", "text": "echo", "expect_text": "different"}
                ],
            },
        }
        assert execute_test_plan(tmp_path, "suite", plan)["verdict"] == "passed"
        plan["spec"]["thresholds"]["max_error_ratio"] = 0
        assert execute_test_plan(tmp_path, "suite", plan)["verdict"] == "failed"


def test_protobuf_expectations_use_canonical_numeric_and_default_field_semantics():
    from google.protobuf import descriptor_pb2

    spec = descriptors()
    file = descriptor_pb2.FileDescriptorSet.FromString(
        base64.b64decode(spec["descriptor_set_base64"])
    )
    file.file[0].message_type[0].field.add(name="count", number=2, type=3, label=1)
    raw = file.SerializeToString()
    codec = Messages(
        {
            "descriptor_set_base64": base64.b64encode(raw).decode(),
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
        "/test.Echo/Call",
    )
    assert codec.expected({"text": "", "count": 42}) == {"count": "42"}
    with pytest.raises(ValueError, match="expected"):
        codec.expected({"unknown": 1})


@pytest.mark.parametrize("isolation", ["policy", "process"])
def test_real_database_readonly_and_no_partial_pass(tmp_path, monkeypatch, isolation):
    init_project(tmp_path, "suite")
    config = load_config(tmp_path)
    config["sandbox"]["mode"] = isolation
    save_config(tmp_path, config)
    path = tmp_path / "business.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("create table accounts(id integer, email text)")
        db.executemany("insert into accounts values (?,?)", [(1, "a"), (2, "b")])
        db.execute("create table secrets(value text)")
    monkeypatch.setenv("IATEST_DATABASE_TEST", str(path))
    source = {
        "driver": "sqlite",
        "path_env": "IATEST_DATABASE_TEST",
        "query": "select id from accounts where id > ?",
        "parameters": [0],
        "allowed_tables": ["accounts"],
    }
    plan = {
        "id": "db",
        "kind": "data",
        "spec": {
            "database": source,
            "checks": [{"id": "ids", "rule": "unique", "path": "$.id"}],
        },
    }
    result = execute_test_plan(tmp_path, "suite", plan)
    assert result["verdict"] == "passed", result
    from iatest.engines.database import read_records

    for query in [
        "delete from accounts",
        "select * from secrets",
        "attach database ':memory:' as other",
        "select load_extension('x')",
        "pragma database_list",
    ]:
        with pytest.raises(ValueError):
            read_records(
                {**source, "query": query, "parameters": []},
                RunControl(timeout_seconds=5),
            )
    with pytest.raises(ValueError, match="quota"):
        read_records({**source, "max_rows": 1}, RunControl(timeout_seconds=5))
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from accounts").fetchone()[0] == 2
    assert read_records(
        {**source, "query": "select count(*) as total from accounts", "parameters": []},
        RunControl(timeout_seconds=5),
    ) == [{"total": 2}]
    with pytest.raises(ValueError):
        read_records(
            {
                **source,
                "query": "select count(*) as total from secrets",
                "parameters": [],
            },
            RunControl(timeout_seconds=5),
        )


def test_desktop_goal_context_compiles_to_same_deterministic_engine():
    context = _context(
        "desktop", {"platform": "macos", "process_id": 123, "app_id": "dev.iatest.lab"}
    )
    step = assertion_step(
        "desktop", {"predicate": "text", "expected": "Saved"}, {"identifier": "status"}
    )
    assert step["attribute"] == "text"
    plan = ExecutionPlan(
        "desktop", "desktop", {**context_spec(context), "steps": [step]}
    )
    validate_plan(plan)
    assert context["token_budget"] == 48000
    assert (
        _context(
            "desktop",
            {
                "platform": "macos",
                "process_id": 123,
                "app_id": "dev.iatest.lab",
                "token_budget": 64000,
            },
        )["token_budget"]
        == 64000
    )
    with pytest.raises(ValueError, match="token_budget"):
        _context(
            "desktop",
            {
                "platform": "macos",
                "process_id": 123,
                "app_id": "dev.iatest.lab",
                "token_budget": 200001,
            },
        )


def test_protocol_global_allocation_does_not_multiply_load():
    plan = ExecutionPlan(
        "load",
        "performance",
        {
            "transport": "websocket",
            "endpoint": "ws://localhost:8888/",
            "allowed_origins": ["http://localhost:8888"],
            "distribution": {"nodes": ["one", "two"]},
            "load": {"rate": 10, "users": 3, "duration_seconds": 1},
            "exchanges": [{"op": "send", "text": "ok", "expect_text": "ok"}],
        },
        budget={"max_requests": 11},
    )
    parts = allocate_wire(plan)
    assert sum(p["spec"]["load"]["rate"] for p in parts.values()) == 10
    assert sum(p["spec"]["load"]["users"] for p in parts.values()) == 3
    assert sum(p["budget"]["max_requests"] for p in parts.values()) == 11
    for part in parts.values():
        validate_plan(ExecutionPlan.from_dict(part))


def test_real_protocol_rate_is_counted_as_sessions(tmp_path):
    init_project(tmp_path, "suite")
    with serve(websocket) as port:
        result = execute_test_plan(
            tmp_path,
            "suite",
            {
                "id": "rate",
                "kind": "performance",
                "spec": {
                    "transport": "websocket",
                    "endpoint": f"ws://127.0.0.1:{port}/",
                    "allowed_origins": [f"http://127.0.0.1:{port}"],
                    "exchanges": [
                        {"op": "send", "text": "hello", "expect_text": "hello"}
                    ],
                    "load": {"rate": 5, "users": 2, "duration_seconds": 1},
                    "thresholds": {"max_dropped_ratio": 0},
                },
            },
        )
        assert result["verdict"] == "passed", result
        assert result["metrics"]["scheduled"] == result["metrics"]["sessions"] == 5
