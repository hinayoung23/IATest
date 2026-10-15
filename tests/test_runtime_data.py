import json
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import httpx
import pytest

from ai_apitest.core.models import Assertion, TestCase as Case, TestStep as Step
from ai_apitest.core.serde import case_from_dict
from ai_apitest.core.schema import schema_errors
from ai_apitest.execution.data_generation import generate_values
from ai_apitest.execution.datasets import expand_cases
from ai_apitest.execution.options import RunOptions
from ai_apitest.execution.runner import run_ordered_cases


@pytest.fixture
def echo_service():
    received = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def handle_request(self):
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b'{}')
            received.append((self.command, self.path, data))
            status = 200
            value = {**data, "ready": False}
            if self.path == "/mcp":
                if self.command == "DELETE":
                    status, value = 204, None
                elif data["method"] == "notifications/initialized":
                    status, value = 202, None
                else:
                    result = {"protocolVersion": "2025-11-25", "capabilities": {}, "serverInfo": {"name": "data", "version": "1"}} if data["method"] == "initialize" else {"structuredContent": {"id": "entity-1"}, "isError": False}
                    value = {"jsonrpc": "2.0", "id": data["id"], "result": result}
            content = json.dumps(value).encode() if value is not None else b''
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            if self.path == "/mcp":
                self.send_header("MCP-Session-Id", "test-session")
            self.end_headers()
            self.wfile.write(content)
        do_POST = do_GET = do_DELETE = handle_request
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def runtime_project(root, url, case, *, mode="policy", max_requests=None, schema=None):
    from ai_apitest.core.config import load_config, save_config
    from ai_apitest.core.paths import project_docs_file
    from ai_apitest.core.serde import write_json
    from ai_apitest.generator.cases import save_cases
    config = load_config(root)
    config["environments"]["local"]["base_url"] = url
    config["execution"].update(parallel=1, max_requests=max_requests)
    config["sandbox"]["mode"] = mode
    save_config(root, config)
    write_json(project_docs_file(root, "demo"), [{"method": case.method, "path": case.path, "request_body": {"required": True, "content": {"application/json": {"schema": schema}}}, "responses": {"200": {}}}] if schema else [])
    save_cases(root, [case], project="demo")


@pytest.mark.parametrize("mode", ["policy", "process"])
def test_real_http_generation_contracts_and_failed_replay(tmp_path, echo_service, mode):
    from ai_apitest.execution.runner import run_cases
    from ai_apitest.report.storage import current_run_results
    from ai_apitest.core.paths import project_docs_file
    from ai_apitest.core.serde import write_json
    url, received = echo_service
    rules = {"name": {"kind": "string", "minLength": 12, "maxLength": 12}, "age": {"kind": "integer", "minimum": 0, "maximum": 1000}}
    schema = {"type": "object", "required": ["name", "age"], "properties": {"name": {"type": "string", "minLength": 12, "maxLength": 12}, "age": {"type": "integer", "minimum": 18, "maximum": 65}}, "additionalProperties": False}
    case = Case("TC-USER-0001", "user", "POST", "/echo", data_generation=rules, samples=2, body={"name": "{{name}}", "age": "{{age}}"}, assertions=[Assertion("status_code", expected=201)])
    runtime_project(tmp_path, url, case, mode=mode, schema=schema)
    first = run_cases(tmp_path, project="demo")
    second = run_cases(tmp_path, project="demo")
    assert all(row.status == "failed" for row in first + second)
    assert first[0].generated_data != second[0].generated_data
    assert len(received) == 4 and all(18 <= data["age"] <= 65 for _, _, data in received)
    replay = run_cases(tmp_path, project="demo", options=RunOptions(replay_run=first[0].run_id))
    assert [row.generated_data for row in replay] == [row.generated_data for row in first]
    assert [data for _, _, data in received[4:]] == [row.generated_data for row in first]
    from ai_apitest.execution.runner import load_cases
    source_cases = load_cases(tmp_path, "demo")
    assert current_run_results(tmp_path, "demo", source_cases)[source_cases[0].id]["status"] == "failed"
    write_json(project_docs_file(tmp_path, "demo"), [])
    with pytest.raises(ValueError, match="契约"):
        run_cases(tmp_path, project="demo", options=RunOptions(replay_run=first[0].run_id))
    assert len(received) == 6


def test_generated_cross_field_violation_blocks_http_and_negative_is_preserved(tmp_path, echo_service):
    from ai_apitest.execution.runner import run_cases
    url, received = echo_service
    schema = {"type": "object", "properties": {"age": {"type": "integer"}}, "required": ["age"], "not": {"properties": {"age": {"const": 18}}}}
    case = Case("TC-USER-0001", "user", "POST", "/echo", data_generation={"age": {"kind": "integer", "const": 18}}, body={"age": "{{age}}"})
    runtime_project(tmp_path, url, case, schema=schema)
    result = run_cases(tmp_path, project="demo")[0]
    assert result.status == "failed" and "可信契约" in result.error and received == []
    case.negative = True
    runtime_project(tmp_path, url, case, schema=schema)
    assert run_cases(tmp_path, project="demo")[0].status == "passed"
    assert received[0][2]["age"] == 18


@pytest.mark.parametrize("mode", ["policy", "process"])
def test_real_http_request_budget_includes_polling_and_preserves_cleanup(tmp_path, echo_service, mode):
    from ai_apitest.execution.runner import run_cases
    url, received = echo_service
    case = Case("TC-USER-0001", "poll", "GET", "/poll", poll={"interval_seconds": 0.001, "timeout_seconds": 1}, assertions=[Assertion("json_path", "$.ready", expected=True)], cleanup_steps=[Step("delete", "DELETE", "/cleanup")])
    runtime_project(tmp_path, url, case, mode=mode, max_requests=2)
    result = run_cases(tmp_path, project="demo")[0]
    assert result.status == "cancelled" and "预算" in result.error
    assert [path for _, path, _ in received] == ["/poll", "/poll", "/cleanup"]
    assert result.cleanup["status"] == "passed"


def test_partitions_rotate_record_failures_and_fixed_seed_is_history_independent(tmp_path, echo_service):
    from ai_apitest.execution.runner import run_cases
    from ai_apitest.execution.sampling_plan import plan_samples
    url, received = echo_service
    rules = {"age": {"kind": "integer", "minimum": 18, "maximum": 65, "partitions": [{"name": "young", "schema": {"maximum": 25}}, {"name": "old", "schema": {"minimum": 50}}]}}
    case = Case("TC-USER-0001", "partition", "POST", "/echo", data_generation=rules, body={"age": "{{age}}"}, assertions=[Assertion("status_code", expected=201)])
    runtime_project(tmp_path, url, case)
    first = run_cases(tmp_path, project="demo")[0]
    second = run_cases(tmp_path, project="demo")[0]
    assert first.generation_meta["plan"]["age"]["partition"] != second.generation_meta["plan"]["age"]["partition"]
    replay = run_cases(tmp_path, project="demo", options=RunOptions(replay_run=first.run_id))[0]
    assert replay.generated_data == first.generated_data and replay.generation_meta["plan"] == first.generation_meta["plan"]
    expanded = expand_cases([case], tmp_path, {}, samples=4)
    cfg = {"execution": {"_fixed_sampling": True}}
    all_planned = plan_samples(expanded, tmp_path, "demo", cfg)
    part_planned = plan_samples(expanded[2:], tmp_path, "demo", cfg)
    assert [row.sample_plan for row in all_planned[2:]] == [row.sample_plan for row in part_planned]
    fixed1 = run_cases(tmp_path, project="demo", options=RunOptions(seed="fixed", samples=3))
    fixed2 = run_cases(tmp_path, project="demo", options=RunOptions(seed="fixed", samples=3))
    assert [row.generated_data for row in fixed1] == [row.generated_data for row in fixed2]


def test_dataset_values_override_generation_and_sensitive_replay_is_refused(tmp_path, echo_service):
    from ai_apitest.execution.runner import run_cases
    url, received = echo_service
    case = Case("TC-USER-0001", "dataset", "POST", "/echo", data_generation={"token": {"kind": "string", "minLength": 12}}, dataset=[{"_id": "sample-0001", "token": "fixed-secret-value"}], samples=2, body={"token": "{{token}}"}, assertions=[Assertion("status_code", expected=201)])
    runtime_project(tmp_path, url, case)
    results = run_cases(tmp_path, project="demo")
    assert all(row.generated_data["token"] == "fixed-secret-value" for row in results)
    assert len({row.case_id for row in results}) == 2
    with pytest.raises(ValueError, match="脱敏"):
        run_cases(tmp_path, project="demo", options=RunOptions(replay_run=results[0].run_id))


def test_short_emails_and_constant_unique_are_checked():
    value = generate_values({"email": {"kind": "email", "minLength": 12, "maxLength": 20}}, "seed", "C")["email"]
    assert 12 <= len(value) <= 20 and value.endswith(".invalid")
    with pytest.raises(ValueError):
        generate_values({"id": {"kind": "string", "pattern": "^constantvalue$", "maxLength": 20, "unique": True}}, "seed", "C", "run")


def test_prefix_suffix_count_towards_generated_string_constraints():
    spec = {"kind": "string", "minLength": 12, "maxLength": 12, "prefix": "user_", "suffix": "_x"}
    value = generate_values({"name": spec}, "seed", "C")["name"]
    assert value.startswith("user_") and value.endswith("_x") and len(value) == 12
    with pytest.raises(ValueError):
        generate_values({"name": {"kind": "string", "prefix": "x" * 4097}}, "seed", "C")


@pytest.mark.parametrize("mode", ["policy", "process"])
def test_native_mcp_fixture_real_http_under_execution_policy(tmp_path, echo_service, mode):
    from ai_apitest.core.config import load_config, save_config
    from ai_apitest.execution.runner import run_cases
    url, received = echo_service
    case = Case("TC-USER-0001", "fixture", "POST", "/echo", fixture_requirements=["data"], body={"id": "{{entity_id}}"}, assertions=[Assertion("json_path", "$.id", expected="entity-1")])
    runtime_project(tmp_path, url, case, mode=mode)
    config = load_config(tmp_path)
    config["fixtures"]["providers"]["data"] = {"enabled": True, "type": "mcp", "url": url + "/mcp", "prepare": {"tool": "create", "arguments": {"namespace": "{{iatest_namespace}}"}, "extract": {"entity_id": "$.id"}}, "cleanup": {"tool": "delete", "arguments": {"id": "{{entity_id}}"}}}
    save_config(tmp_path, config)
    result = run_cases(tmp_path, project="demo")[0]
    assert result.status == "passed" and result.cleanup["external_capability_cleanup"]["status"] == "passed"
    assert [(method, path) for method, path, _ in received] == [("POST", "/mcp")] * 3 + [("POST", "/echo"), ("POST", "/mcp"), ("DELETE", "/mcp")]
    assert received[4][2]["params"]["arguments"]["id"] == "entity-1"


def test_runtime_generation_survives_scenario_compilation():
    from ai_apitest.core.models import Endpoint
    from ai_apitest.domain.case_drafts import compile_scenario_drafts, validate_compiled_cases
    endpoint = Endpoint("POST", "/users", request_body={"required": True, "content": {"application/json": {"schema": {"type": "object", "required": ["age"], "properties": {"age": {"type": "integer", "minimum": 18, "maximum": 65}}}}}}, responses={"200": {}})
    draft = {"scenario_id": "SCN-001", "endpoint_method": "POST", "endpoint_path": "/users", "intent": "合法年龄", "design_method": "happy_path", "request_overrides": {"body": {"age": "{{age}}"}}, "data_generation": {"age": {"kind": "integer", "minimum": 18, "maximum": 65}}, "samples": 3, "expected_outcome": "返回 200", "expected_basis": "responses.200", "assertions": [{"type": "status_code", "expected": 200}]}
    compiled = compile_scenario_drafts([draft], [endpoint])
    assert compiled.accepted and compiled.cases[0].data_generation == draft["data_generation"]
    assert compiled.cases[0].samples == 3 and not validate_compiled_cases(compiled.cases, [endpoint])


RULES = {"name": {"kind": "string", "minLength": 12, "maxLength": 12}, "age": {"kind": "integer", "minimum": 18, "maximum": 65}, "flag": {"kind": "boolean"}}


def test_generators_vary_replay_and_preserve_types():
    a = generate_values(RULES, "A", "C1")
    assert a == generate_values(dict(reversed(list(RULES.items()))), "A", "C1")
    assert a != generate_values(RULES, "B", "C1")
    assert type(a["age"]) is int and type(a["flag"]) is bool
    assert 18 <= a["age"] <= 65
    unique = {"id": {"kind": "uuid", "unique": True}}
    assert generate_values(unique, "A", "C1", "run1") != generate_values(unique, "A", "C1", "run2")


@pytest.mark.parametrize("schema", [
    {"type": "string", "pattern": "^[A-Z]{3}[0-9]{4}$"},
    {"type": "number", "minimum": 1, "maximum": 2, "multipleOf": 0.25},
    {"type": "array", "minItems": 3, "maxItems": 3, "uniqueItems": True, "items": {"type": "integer", "minimum": 1, "maximum": 20}},
    {"type": "object", "required": ["a", "b"], "properties": {"a": {"type": "integer", "minimum": 1, "maximum": 4}, "b": {"type": "integer", "minimum": 1, "maximum": 4}}, "not": {"properties": {"a": {"const": 1}, "b": {"const": 1}}}},
    {"$defs": {"id": {"type": "integer", "minimum": 1}}, "type": "object", "properties": {"id": {"$ref": "#/$defs/id"}}},
])
def test_generated_schema_is_valid(schema):
    for seed in range(10):
        assert not schema_errors(generate_values({"value": {"schema": schema}}, seed, "C")["value"], schema)


def test_impossible_and_reserved_generators_fail_before_execution():
    with pytest.raises(ValueError):
        generate_values({"value": {"kind": "integer", "minimum": 5, "maximum": 3}}, "A", "C")
    with pytest.raises(ValueError):
        generate_values({"uuid": {"kind": "uuid"}}, "A", "C")
    with pytest.raises(ValueError):
        generate_values({"value": {"kind": "string", "minLength": 100000}}, "A", "C")


def test_samples_dataset_shards_roundtrip_and_caps(tmp_path):
    case = Case("TC-DATA-0001", "data", "POST", "/", data_generation=RULES, samples=3, dataset=[{"_id": "a"}, {"_id": "b"}])
    assert case_from_dict(asdict(case)).data_generation == RULES
    expanded = expand_cases([case], tmp_path, {})
    assert len(expanded) == 6 and len({item.id for item in expanded}) == 6
    assert all(item.origin_case_id == case.id for item in expanded)
    a = RunOptions(shard_index=1, shard_count=2).select(expanded)
    b = RunOptions(shard_index=2, shard_count=2).select(expanded)
    assert {item.id for item in a} | {item.id for item in b} == {item.id for item in expanded}
    with pytest.raises(ValueError):
        expand_cases([case], tmp_path, {"sandbox": {"max_cases": 5}})


def test_data_is_stable_across_setup_retry_assertions_and_cleanup():
    received = []
    main_count = 0
    def respond(request):
        nonlocal main_count
        value = json.loads(request.content)
        received.append((request.url.path, value))
        if request.url.path == "/main":
            main_count += 1
        return httpx.Response(500 if request.url.path == "/main" and main_count == 1 else 200, json=value)
    case = Case("TC-DATA-0001", "runtime", "POST", "/main", data_generation=RULES, body={"name": "{{name}}", "age": "{{age}}", "fixed_invalid": "@@"}, assertions=[Assertion("status_code", expected=200), Assertion("json_path", "$.name", expected="{{name}}")], retry_safe=True, setup_steps=[Step("prepare", "POST", "/setup", body={"name": "{{name}}"})], cleanup_steps=[Step("cleanup", "POST", "/cleanup", body={"name": "{{name}}"})])
    config = {"execution": {"parallel": 1, "random_seed": "fixed", "namespace": "run", "failure_retries": 1}}
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = run_ordered_cases([case], {"base_url": "https://example.invalid"}, config, client)[0]
    assert result.status == "passed" and result.flaky
    assert len({value["name"] for _, value in received}) == 1
    assert result.generated_data["name"] == received[0][1]["name"]
    assert all(value["fixed_invalid"] == "@@" for path, value in received if path == "/main")
    assert len(result.attempt_history) == 1
