from __future__ import annotations

import copy
from dataclasses import asdict
from urllib.parse import urlparse

import pytest

from iatest.ai.provider import ModelResponse, ModelToolCall, TokenUsage
from iatest.application.engine_analysis import (
    analyze_engine_run,
    comparison,
)
from iatest.application.ui_exploration import (
    Exploration,
    explore_ui_goal,
    validate_intent,
    verify_ui_case,
)
from iatest.core.config import init_project
from iatest.core.redaction import redact, secret_scope
from iatest.engines.contracts import EngineResult, ExecutionPlan
from iatest.engines.data import bind_values, materialize
from iatest.engines.store import EngineStore
from iatest.execution.control import RunControl


@pytest.fixture
def root(tmp_path):
    init_project(tmp_path, "suite")
    return tmp_path


def context():
    return {
        "kind": "web",
        "allowed_origins": ["http://localhost:8888"],
        "start_url": "http://localhost:8888/ui",
        "data_generation": {
            "nickname": {
                "kind": "string",
                "minLength": 16,
                "maxLength": 20,
                "unique": True,
            }
        },
        "rules": {"saved": "昵称应等于 nickname"},
    }


def intent():
    return {
        "purpose": "验证昵称保存",
        "checks": [
            {
                "id": "saved",
                "predicate": "value",
                "expected": {"$value": "nickname"},
                "source_ref": "saved",
                "source_quote": "昵称应等于 nickname",
            }
        ],
    }


class Backend:
    def __init__(self, *args):
        self.steps = []

    def find(self, target):
        return {
            "search_complete": True,
            "match_count": 1,
            "unique": True,
            "candidates": [{"text": "Nickname"}],
        }

    def outline(self, region=""):
        return {"controls": [{"target": {"id": "nickname"}}], "complete": False}

    def do(self, step):
        self.steps.append(step)
        return {"dispatch": "acknowledged"}

    def check(self, step):
        return True, {"matched": True}

    def close(self):
        pass


def runtime(goal="验证昵称保存"):
    return Exploration(
        goal,
        context(),
        Backend(),
        {"nickname": "random-input"},
        RunControl(timeout_seconds=10),
    )


def fill(r):
    r.observe({"id": "nickname"})
    r.act({"op": "fill", "target": {"id": "nickname"}, "value": {"$value": "nickname"}})


def test_frozen_safari_case_preserves_explicit_network_mode():
    r = runtime()
    r.context.update(browser="webkit", network_policy="navigation_only")
    r.submit_intent(intent())
    fill(r)
    r.check("saved", {"id": "nickname"})
    plan = r.compile()
    assert plan["spec"]["browser"] == "webkit"
    assert plan["spec"]["network_policy"] == "navigation_only"


def test_goal_oracle_freezes_before_writes_and_quotes_trusted_sources():
    r = runtime()
    with pytest.raises(ValueError, match="Freeze"):
        fill(r)
    r.submit_intent(intent())
    with pytest.raises(ValueError, match="already frozen"):
        r.submit_intent(intent())
    bad = intent()
    bad["checks"][0].update(
        expected="observed success", source_quote="observed success"
    )
    with pytest.raises(ValueError, match="quote"):
        validate_intent(bad, r.goal, r.context)
    bad["checks"][0].update(source_quote="昵称应等于 nickname")
    with pytest.raises(ValueError, match="Expected text"):
        validate_intent(bad, r.goal, r.context)


def test_oracle_source_error_names_exact_keys_without_accepting_fuzzy_references():
    value = intent()
    value["checks"][0]["source_ref"] = "saved rule"
    with pytest.raises(ValueError, match="exact source key: goal, saved"):
        validate_intent(value, "验证昵称保存", context())
    value["checks"][0]["source_ref"] = "saved"
    validate_intent(value, "验证昵称保存", context())
    value["checks"].append(copy.deepcopy(value["checks"][0]))
    with pytest.raises(ValueError, match="unique ID"):
        validate_intent(value, "验证昵称保存", context())


def test_unchanged_evidence_references_saved_full_row_without_losing_facts():
    r = runtime()
    first = r.observe({"id": "nickname"})
    second = r.observe({"id": "nickname"})
    assert "observation" in first and "observation" not in second
    assert (
        second["unchanged"]
        and second["observation_ref"]["trace_sequence"] == first["trace_sequence"]
    )
    assert (
        r.trace[first["trace_sequence"]]["result"]
        == r.trace[second["trace_sequence"]]["result"]
    )
    assert r.evidence_characters_saved > 0
    assert len(r.checkpoint("ui_find")["latest_observations"]) == 1
    r.observed.clear()
    assert r.checkpoint("ui_outline")["known_targets_needing_refresh"] == [
        {"id": "nickname"}
    ]
    r.submit_intent(intent())
    with pytest.raises(ValueError, match="Observe the target"):
        r.act(
            {
                "op": "fill",
                "target": {"id": "nickname"},
                "value": {"$value": "nickname"},
            }
        )
    # Different page/frame context is part of cycle and evidence identity.
    from iatest.application.ui_exploration import semantic_state

    assert semantic_state(
        {
            "contexts": [
                {
                    "kind": "frame",
                    "context": {
                        "frame": [{"selector": "#x", "origin": "https://x.example"}]
                    },
                }
            ]
        }
    )["contexts"]


def test_compiler_preserves_reload_and_parameterized_oracle():
    r = runtime("刷新后验证昵称保存")
    r.submit_intent(intent())
    fill(r)
    r.check("saved", {"id": "nickname"})
    with pytest.raises(ValueError, match="temporal"):
        r.compile()
    r.act({"op": "reload"})
    with pytest.raises(ValueError, match="Every"):
        r.compile()
    r.check("saved", {"id": "nickname"})
    plan = r.compile()
    assert [s["op"] for s in plan["spec"]["steps"]] == ["fill", "reload", "expect"]
    assert plan["spec"]["steps"][-1]["expected"] == {"$value": "nickname"}
    assert r.backend.steps[0]["value"] == "random-input"


def test_uncertain_delivery_blocks_all_further_actions_and_compilation():
    from iatest.mobile.protocol import DeliveryUnknown

    r = runtime()
    r.submit_intent(intent())

    def unknown(step):
        raise DeliveryUnknown("Lost injection acknowledgement")

    r.backend.do = unknown
    with pytest.raises(DeliveryUnknown):
        fill(r)
    with pytest.raises(ValueError, match="uncertain"):
        r.act({"op": "reload"})
    with pytest.raises(ValueError, match="uncertain"):
        r.compile()
    assert not any(row["event"] == "action" for row in r.trace)


def test_budget_and_unobserved_target_enforced():
    r = runtime()
    r.submit_intent(intent())
    with pytest.raises(ValueError, match="Observe"):
        r.act({"op": "click", "target": {"id": "unseen"}})
    r.context["max_actions"] = 1
    with pytest.raises(ValueError, match="action budget"):
        r.act({"op": "reload"})
    r.context["max_queries"] = 1
    r.observe()
    with pytest.raises(ValueError, match="query budget"):
        r.observe()


def test_cycle_detection_and_temporal_barrier_cannot_be_early():
    r = runtime("刷新后验证昵称保存")
    r.submit_intent(intent())
    r.act({"op": "reload"})
    fill(r)
    r.check("saved", {"id": "nickname"})
    with pytest.raises(ValueError, match="barrier"):
        r.compile()
    r = runtime()
    r.submit_intent(intent())
    for _ in range(2):
        r.observe({"id": "nickname"})
        r.act({"op": "click", "target": {"id": "nickname"}})
    r.observe({"id": "nickname"})
    with pytest.raises(ValueError, match="cycle"):
        r.act({"op": "click", "target": {"id": "nickname"}})


def test_saved_template_preserves_sensitive_variable_references(root, monkeypatch):
    from iatest.engines.data import redact_asset

    monkeypatch.setenv("TEST_CREDENTIAL", "credential-specific-value")
    plan = {
        "id": "binding",
        "kind": "web",
        "spec": {"value": {"$value": "password"}},
        "input_env": {"password": "TEST_CREDENTIAL"},
    }
    with secret_scope(["credential-specific-value"]):
        artifact = redact_asset({"plan": plan, "observed": "credential-specific-value"})
        store = EngineStore(root, "suite")
        key = store.save_artifact("ui_case", artifact)
    saved = store.get_artifact("ui_case", key)
    assert saved["plan"]["input_env"]["password"] == "TEST_CREDENTIAL"
    assert saved["observed"] == "[REDACTED]"


@pytest.mark.parametrize("platform", ["android", "ios"])
def test_mobile_adapter_native_boundary(root, monkeypatch, platform):
    from iatest.application.ui_adapters import MobileExplorer
    from iatest.mobile.runtime import MobileSession
    from iatest.mobile.store import MobileStore

    MobileStore(root, "suite").save_profile(
        {
            "id": "phone",
            "platform": platform,
            "device_id": "fake-phone",
            "app_id": "dev.example.app",
        }
    )

    class NativeBoundary:
        unique = staticmethod(MobileSession.unique)

        def __init__(self, profile, *, control, root=None, project=None):
            assert root is not None and project == "suite"
            self.profile, self.control, self.closed = profile, control, False
            self.complete = False

        def query(self, target, **kwargs):
            return {
                "complete": self.complete,
                "match_count": 1,
                "candidates": [{"text": "Saved", "enabled": True}],
            }

        def close(self):
            self.closed = True

    monkeypatch.setattr("iatest.mobile.runtime.MobileSession", NativeBoundary)
    adapter = MobileExplorer(
        root,
        "suite",
        {"profile": "phone", "kind": platform},
        RunControl(timeout_seconds=5),
        10,
    )
    step = {
        "op": "expect",
        "target": {"identifier": "status"},
        "attribute": "text",
        "expected": "Saved",
    }
    with pytest.raises(ValueError, match="Incomplete"):
        adapter.check(step)
    adapter.session.complete = True
    assert adapter.check(step)[0] is True
    adapter.close()
    assert adapter.session.closed


def test_random_values_change_per_execution_and_secrets_stay_scoped(monkeypatch):
    monkeypatch.setenv("IATEST_TEST_PASSWORD", "credential-specific-value")
    c = context()
    plan = ExecutionPlan(
        "data",
        "web",
        {"input": {"$value": "nickname"}, "credential": {"$value": "login"}},
        data_generation=c["data_generation"],
        input_env={"login": "IATEST_TEST_PASSWORD"},
    )
    a, metadata, hidden = materialize(plan)
    b, _, _ = materialize(plan)
    assert a.spec["input"] != b.spec["input"]
    assert metadata["variables"]["login"] == "[REDACTED]"
    with secret_scope(hidden):
        assert redact({"echo": "credential-specific-value"})["echo"] == "[REDACTED]"
    assert redact("credential-specific-value") == "credential-specific-value"
    with pytest.raises(ValueError):
        bind_values({"$value": []}, {})


def test_readiness_waits_survive_compilation_and_cannot_replace_oracles():
    r = runtime()
    r.submit_intent(intent())
    fill(r)
    r.wait_ready({"predicate": "text", "expected": "Saved"}, {"id": "status"})
    with pytest.raises(ValueError, match="Every"):
        r.compile()
    r.check("saved", {"id": "nickname"})
    assert [s["op"] for s in r.compile()["spec"]["steps"]] == ["fill", "wait", "expect"]

    def readiness_failed(*args):
        return {
            "kind": "web",
            "lifecycle": "completed",
            "verdict": "failed",
            "run_id": "wait-error",
            "steps": [{"index": 1, "op": "wait", "passed": False}],
        }

    assert (
        verify_ui_case(None, None, {}, {}, executor=readiness_failed)[0]
        == "inconclusive"
    )


def test_actual_browser_wait_reload_restart_and_fresh_replay(root):
    import runpy
    import threading
    from pathlib import Path

    from iatest.application.ui_adapters import WebExplorer
    from iatest.engines.data import plan_values
    from iatest.engines.web import chromium_path

    if not chromium_path():
        pytest.skip("Requires an installed Chromium")
    create_server = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "examples/ui_goal_lab.py")
    )["create_server"]
    server = create_server()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    backend = None
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        c = {**context(), "start_url": base + "/ui", "allowed_origins": [base]}
        control = RunControl(timeout_seconds=60)
        backend = WebExplorer(root, "suite", c, control, 100)
        values, _, _ = plan_values(
            ExecutionPlan(
                "browser-data", "web", {}, data_generation=c["data_generation"]
            )
        )
        r = Exploration("刷新和新会话后验证昵称保存", c, backend, values, control)
        r.submit_intent(intent())
        r.observe()
        r.act({"op": "click", "target": {"id": "settings"}})
        # Literal data selected by a model is rebound to its declared variable.
        r.act({"op": "fill", "target": {"id": "nickname"}, "value": values["nickname"]})
        r.act({"op": "click", "target": {"id": "save"}})
        assert r.wait_ready(
            {"predicate": "text", "expected": "Saved"}, {"id": "status"}
        )["ready"]
        r.act({"op": "reload"})
        r.act({"op": "restart_session"})
        r.act({"op": "click", "target": {"id": "settings"}})
        assert r.check("saved", {"id": "nickname"})["passed"]
        compiled = r.compile()
        assert any(s["op"] == "wait" for s in compiled["spec"]["steps"])
        assert compiled["spec"]["steps"][1]["value"] == {"$value": "nickname"}
        backend.close()
        backend = None
        status, runs = verify_ui_case(root, "suite", compiled, c)
        assert status == "reproducible_passed", runs
        assert len({r["runtime_data"]["namespace"] for r in runs}) == 2
    finally:
        if backend:
            backend.close()
        server.shutdown()
        server.server_close()


def scripted_gateway(calls):
    class Gateway:
        count = 0

        def __init__(self, config):
            self.model_config = config
            self.queue = iter(calls)

        def complete(self, **kwargs):
            Gateway.count += 1
            name, args = next(self.queue)
            return ModelResponse(
                tool_calls=[ModelToolCall("call-" + str(Gateway.count), name, args)],
                usage=TokenUsage(30, 10, 40),
            )

    return Gateway


def test_exploration_without_steps_compiles_replays_and_reuses_without_model(root):
    calls = [
        ("submit_test_intent", {"intent": intent()}),
        ("ui_find", {"target": {"id": "nickname"}}),
        (
            "ui_do",
            {
                "step": {
                    "op": "fill",
                    "target": {"id": "nickname"},
                    "value": {"$value": "nickname"},
                }
            },
        ),
        ("ui_check", {"check_id": "saved", "target": {"id": "nickname"}}),
        ("finish_exploration", {}),
    ]
    gateway = scripted_gateway(calls)
    executed = []

    def executor(root, project, plan):
        executed.append(copy.deepcopy(plan))
        return {
            "kind": "web",
            "lifecycle": "completed",
            "verdict": "passed",
            "run_id": "replay-" + str(len(executed)),
        }

    c = context()
    c.pop("kind")
    case = explore_ui_goal(
        root,
        "suite",
        "web",
        "验证昵称保存",
        c,
        gateway_factory=gateway,
        backend_factory=Backend,
        executor=executor,
    )
    assert case["status"] == "reproducible_passed", case
    assert len(executed) == 2 and gateway.count == 4
    assert EngineStore(root, "suite").get_plan(case["plan"]["id"])
    reused = explore_ui_goal(
        root,
        "suite",
        "web",
        "验证昵称保存",
        c,
        gateway_factory=gateway,
        executor=executor,
    )
    assert reused["model_calls"] == 0 and gateway.count == 4
    assert reused["reused_from"] == case["id"] and len(executed) == 4


def test_stable_business_failure_is_retained_but_cleanup_error_is_not_promoted():
    def failed(*args):
        return {
            "kind": "web",
            "lifecycle": "completed",
            "verdict": "failed",
            "run_id": "failure",
            "steps": [{"index": 2, "op": "expect", "passed": False}],
        }

    status, runs = verify_ui_case(None, None, {}, {}, executor=failed)
    assert status == "reproducible_failed" and len(runs) == 2

    def uncertain(*args):
        return {**failed(), "lifecycle": "error"}

    assert verify_ui_case(None, None, {}, {}, executor=uncertain)[0] == "inconclusive"

    def cleanup_error(*args):
        return {
            "kind": "workflow",
            "lifecycle": "completed",
            "verdict": "failed",
            "run_id": "failure",
            "steps": [
                {"result": failed()},
                {
                    "always": True,
                    "result": {"lifecycle": "error", "verdict": "inconclusive"},
                },
            ],
        }

    assert (
        verify_ui_case(None, None, {}, {}, executor=cleanup_error)[0] == "inconclusive"
    )


def test_analysis_links_existing_evidence_and_never_changes_verdict(root):
    store = EngineStore(root, "suite")
    original = EngineResult(
        "perf", "performance", verdict="failed", metrics={"p95_ms": 600}
    )
    store.save_result(original)
    advice = {
        "summary": "P95 超过门禁，需要复测",
        "interpretations": [
            {
                "explanation": "客户端延迟偏高，原因尚未定位",
                "uncertainty": "hypothesis",
                "evidence_refs": ["run.metrics.p95_ms"],
            }
        ],
        "recommendations": [
            {
                "priority": "P1",
                "action": "采集服务端分段耗时",
                "evidence_refs": ["run.metrics.p95_ms"],
                "expected_effect": "定位延迟来源",
                "verification": ["同负载重复三次并比对 P95"],
            }
        ],
        "limitations": ["无服务端遥测"],
    }
    gateway = scripted_gateway([("submit_engine_analysis", {"analysis": advice})])
    artifact = analyze_engine_run(
        root, "suite", original.run_id, gateway_factory=gateway
    )
    assert artifact["original_verdict"] == "failed"
    assert store.get_result(original.run_id)["verdict"] == "failed"
    assert store.get_artifact("analysis", artifact["id"])["analysis"] == advice


def test_analysis_rejects_fabricated_references(root):
    from iatest.agents.runtime import AgentLoopLimitExceeded

    store = EngineStore(root, "suite")
    run = EngineResult("sec", "security", verdict="failed")
    store.save_result(run)
    advice = {
        "summary": "Invalid",
        "interpretations": [
            {
                "explanation": "fabrication",
                "uncertainty": "hypothesis",
                "evidence_refs": ["invented.reference"],
            }
        ],
        "recommendations": [],
        "limitations": [],
    }
    gateway = scripted_gateway([("submit_engine_analysis", {"analysis": advice})] * 5)
    with pytest.raises(AgentLoopLimitExceeded):
        analyze_engine_run(root, "suite", run.run_id, gateway_factory=gateway)
    assert store.artifacts("analysis") == []


def test_baselines_require_provenance_and_matching_conditions(root):
    a = asdict(
        EngineResult("a", "performance", verdict="passed", metrics={"p95_ms": 2})
    )
    b = asdict(
        EngineResult("b", "performance", verdict="passed", metrics={"p95_ms": 1})
    )
    assert comparison(a, b, {}, {})["status"] == "not_comparable"
    ctx = {"comparison_key": "same"}
    assert comparison(a, b, ctx, ctx)["deltas"]["p95_ms"]["difference"] == 1
    assert comparison(a, b, ctx, {"comparison_key": "other"})["deltas"] == {}
    b["lifecycle"] = "cancelled"
    assert comparison(a, b, ctx, ctx)["status"] == "not_comparable"


@pytest.mark.parametrize("locale", ["zh", "en", "ja", "fr", "ru"])
def test_console_new_entries_render(root, locale):
    from iatest.web.i18n import set_current_locale
    from iatest.web.server import route_get

    set_current_locale(locale)
    try:
        html = route_get(root, "suite", urlparse("/testing?tab=exploration"))
        assert "/actions/testing-explore" in html and 'name="goal"' in html
        assert route_get(root, "suite", urlparse("/testing?tab=analysis"))
    finally:
        set_current_locale("zh")
