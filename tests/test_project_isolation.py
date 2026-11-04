"""Business contracts must come from the selected project, not framework recipes."""

import copy
from pathlib import Path

import pytest

from iatest.ai.workflows import _build_dependency_graph
from iatest.core.models import Assertion, Endpoint
from iatest.discovery.document import parse_api_document
from iatest.domain.contracts import infer_assertion_obligations, normalize_endpoint_contract
from iatest.domain.coverage import fill_deterministic_coverage_gaps
from iatest.domain.scenario_titles import dependency_chain_title
from iatest.execution.assertions import evaluate
from iatest.generator.cases import build_dependency_chain_case


def test_runtime_sources_have_no_local_debug_project_bindings():
    runtime = Path(__file__).resolve().parents[1] / "iatest"
    text_suffixes = {".py", ".md", ".yaml", ".json", ".html", ".js", ".css", ".java", ".swift", ".m", ".h", ".ps1", ".xml", ".pbxproj", ".xcscheme"}
    violations = [str(path.relative_to(runtime)) for path in runtime.rglob("*")
                  if path.is_file() and path.suffix in text_suffixes
                  and ("tempbox" in path.name.lower()
                       or "tempbox" in path.read_text(encoding="utf-8").lower())]
    assert not violations, violations


@pytest.mark.parametrize("method,path,text", [
    ("DELETE", "/api/delete/{email}", "删除成功后不能再查询邮件和续期"),
    ("GET", "/api/generate", "同一个前缀的邮箱已经存在时刷新创建时间"),
    ("POST", "/api/extend/{email}", "续期成功后创建时间更新为当前时间并返回新的剩余有效期"),
])
def test_matching_route_names_do_not_inject_other_application_operations(method, path, text):
    endpoint = normalize_endpoint_contract(Endpoint(
        method=method, path=path, responses={"200": {}},
        business_rules=[{"rule_id":"rule", "description":text, "expected_status":200}],
    ))
    assert endpoint.business_rules[0]["trigger"]["chain_steps"] == []
    plan = fill_deterministic_coverage_gaps([], [endpoint])
    assert all(not item.get("chain_steps") for item in plan)


def test_global_rules_require_explicit_operation_scope_and_keep_unscoped_evidence():
    endpoints, metadata = parse_api_document("""
## 概览
- 默认有效期 12 分钟。
- POST /v2/tickets 默认有效期 5 分钟。
## 生成报表
GET /api/generate
## 创建工单
POST /v2/tickets
## 查询工单
GET /v2/tickets
""")
    assert len(metadata["global_rule_candidates"]) == 2
    assert endpoints[0].business_rules == []
    assert endpoints[2].business_rules == []
    assert len(endpoints[1].business_rules) == 1
    assert endpoints[1].business_rules[0]["expected_status"] is None


@pytest.mark.parametrize("domain,count", [("mail.orders.example", 12), ("qa.accounts.invalid", 6)])
def test_email_constraints_use_only_documented_domain_and_length(domain, count):
    text = f"随机生成 {count} 位邮箱前缀，邮箱域名固定为 {domain}"
    assertions = [Assertion(**item) for item in infer_assertion_obligations(text, status=None)]
    assert assertions
    good = {"json":{"email":"a" * count + "@" + domain}}
    wrong_domain = {"json":{"email":"a" * count + "@other.invalid"}}
    wrong_length = {"json":{"email":"a" * (count + 1) + "@" + domain}}
    assert all(evaluate(assertion, good) is None for assertion in assertions)
    assert any(evaluate(assertion, wrong_domain) for assertion in assertions)
    assert any(evaluate(assertion, wrong_length) for assertion in assertions)
    assert not any(item.get("target") == "$.email" for item in
                   infer_assertion_obligations("随机生成 8 位订单密码", status=None))


def test_prefix_template_does_not_invent_an_undocumented_ascii_restriction():
    assertions = infer_assertion_obligations("生成邮箱地址为 {prefix}@mail.example.invalid", status=None)
    assert assertions
    assert all(evaluate(Assertion(**item), {"json":{"email":"中文@mail.example.invalid"}}) is None
               for item in assertions)


def test_lifetime_examples_do_not_override_the_current_contract():
    responses = {"200":{"content":{"application/json":{
        "schema":{"type":"object", "properties":{"expires_in":{"type":"integer"}}},
        "example":{"expires_in":600},
    }}}}
    for text, expected_values in [
        ("续期成功后返回新的剩余有效期 expires_in", []),
        ("续期成功后返回 expires_in=240", [240]),
    ]:
        endpoint = normalize_endpoint_contract(Endpoint(
            method="POST", path="/licenses/renew", responses=responses,
            business_rules=[{"description":text, "expected_status":200}],
        ))
        values = [item["expected"] for item in endpoint.business_rules[0]["expected"]["assertions"]
                  if item.get("target") == "$.expires_in" and item.get("operator") == "eq"]
        assert values == expected_values


def test_dependency_graph_requires_a_successful_documented_output():
    consumers = [Endpoint(method="GET", path="/records/{record_id}"),
                 Endpoint(method="DELETE", path="/records/{record_id}")]
    unrelated = Endpoint(method="GET", path="/health")
    assert _build_dependency_graph([unrelated, *consumers]) == {}
    producer = Endpoint(method="POST", path="/records", summary="创建订单记录", responses={
        "201":{"schema":{"type":"object", "properties":{"record_id":{"type":"string"}}}}
    })
    expected = {"POST /records":{"GET /records/{record_id}", "DELETE /records/{record_id}"}}
    assert _build_dependency_graph([unrelated, producer, *consumers]) == expected
    echo = copy.deepcopy(consumers[0])
    echo.responses = copy.deepcopy(producer.responses)
    assert _build_dependency_graph([unrelated, producer, echo, consumers[1]]) == expected
    failed_only = copy.deepcopy(producer)
    failed_only.responses = {"400":producer.responses["201"]}
    assert _build_dependency_graph([failed_only, *consumers]) == {}


def test_explicit_non_mail_chain_and_titles_keep_the_projects_own_operations():
    create = Endpoint(method="POST", path="/records", summary="创建订单记录", responses={"201":{}})
    delete = normalize_endpoint_contract(Endpoint(
        method="DELETE", path="/records/{record_id}", summary="删除订单记录", responses={"204":{}},
        business_rules=[{
            "rule_id":"lifecycle", "description":"创建记录后删除记录", "expected_status":204,
            "trigger":{"chain_steps":[
                {"method":"POST", "path":"/records", "extract":{"record_id":"$.record_id"}},
                {"method":"DELETE", "path":"/records/{record_id}",
                 "path_params":{"record_id":"{{record_id}}"},
                 "assertions":[{"type":"status_code", "operator":"eq", "expected":204}]},
            ]},
        }],
    ))
    declared = delete.business_rules[0]["trigger"]["chain_steps"]
    rule_plan = next(item for item in fill_deterministic_coverage_gaps([], [create, delete])
                     if item.get("rule_ids") == ["lifecycle"])
    assert rule_plan["chain_steps"] == declared
    assert dependency_chain_title(declared, [create, delete]) == "创建订单记录后删除订单记录"
    assert "邮箱" not in build_dependency_chain_case(create, delete, "record_id", {}).title
