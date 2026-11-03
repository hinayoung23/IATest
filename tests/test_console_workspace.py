from contextlib import nullcontext
from urllib.parse import urlparse

import pytest

from iatest.core.config import init_project
from iatest.engines.store import EngineStore
from iatest.mobile import devices, usbmux
from iatest.mobile.protocol import ProtocolError
from iatest.mobile.store import MobileStore
from iatest.nodes.store import ControllerStore
from iatest.web.i18n import set_current_locale
from iatest.web.mobile import handle_mobile_action
from iatest.web.mobile_views import detection_results
from iatest.web.operations import handle_operation
from iatest.web.plan_forms import operation_payload
from iatest.web.server import route_get
from iatest.web.testing import build_plan


@pytest.fixture
def workspace(tmp_path):
    init_project(tmp_path, "demo")
    set_current_locale("zh")
    yield tmp_path
    set_current_locale("zh")


def fields(**values):
    return {k: [str(v)] for k, v in values.items()}


class Handler:
    def redirect(self, location):
        self.location = location


def test_navigation_groups_existing_pages_and_preserves_routes(workspace):
    for path, active in [
        ("/", "/"),
        ("/testing", "/testing"),
        ("/interfaces", "/assets"),
        ("/cases", "/assets"),
        ("/reports", "/results"),
        ("/mobile?tab=plans", "/testing"),
        ("/mobile?tab=runs", "/results"),
        ("/testing?tab=nodes", "/devices"),
    ]:
        content = route_get(workspace, "demo", urlparse(path))
        nav = content.split('class="site-nav"', 1)[1].split("</nav>", 1)[0]
        assert nav.count("<a ") == 7
        assert f'aria-current=page href="{active}?project=demo"' in nav
        assert "/interfaces?" not in nav and "/cases?" not in nav
        assert "仪表盘" not in content
    catalog = route_get(workspace, "demo", urlparse("/testing"))
    assert "界面与交互" in catalog and "服务与数据" in catalog
    assert "<textarea" not in catalog
    quality = route_get(workspace, "demo", urlparse("/testing?group=quality"))
    assert "tab=security" in quality and "tab=desktop" not in quality


@pytest.mark.parametrize(
    "kind,values",
    [
        ("data", {"csv": "id,name\n1,A\n2,B", "rule": "unique", "column": "id"}),
        (
            "protocol",
            {
                "url": "https://test.example/ping",
                "origins": "https://test.example",
                "transport": "http",
                "status": "204",
            },
        ),
        (
            "protocol",
            {
                "url": "ws://localhost:8000/echo",
                "origins": "ws://localhost:8000",
                "transport": "websocket",
                "send": "hello",
                "receive": "hello",
            },
        ),
        (
            "protocol",
            {
                "url": "mqtt://localhost:1883",
                "origins": "mqtt://localhost:1883",
                "transport": "mqtt",
                "send": "hello",
                "receive": "hello",
                "topic": "test/echo",
            },
        ),
        (
            "accessibility",
            {
                "url": "https://test.example",
                "origins": "https://test.example",
                "rule": "document_title",
                "expected": "Test",
            },
        ),
        (
            "accessibility",
            {
                "url": "https://test.example",
                "origins": "https://test.example",
                "rule": "accessible_name",
                "selector": "#submit",
            },
        ),
        (
            "desktop",
            {
                "platform": "macos",
                "process_id": "123",
                "app_id": "com.example.lab",
                "op": "fill",
                "identifier": "nickname",
                "value": "Test",
                "expected_identifier": "status",
                "expected": "Saved",
            },
        ),
        (
            "api",
            {
                "url": "https://test.example/ping",
                "origins": "https://test.example",
                "expected_status": "201",
            },
        ),
    ],
)
def test_basic_forms_produce_real_validated_plans_without_execution(
    workspace, kind, values
):
    store = EngineStore(workspace, "demo")
    data = fields(id="form-check", kind=kind, **values)
    plan = build_plan(store, data)
    store.save_plan(plan)
    saved = store.get_plan("form-check")
    assert saved["kind"] == kind
    assert store.runs() == []


@pytest.mark.parametrize(
    "csv,column",
    [("id,id\n1,2", "id"), ("id,name\n1", "id"), ("id\n1", "missing"), ("id\n", "id")],
)
def test_invalid_table_inputs_do_not_create_plans(workspace, csv, column):
    store = EngineStore(workspace, "demo")
    with pytest.raises(ValueError):
        build_plan(
            store, fields(id="bad", kind="data", csv=csv, rule="unique", column=column)
        )
    assert not store.plans()


def test_csv_empty_cells_fail_the_real_non_null_gate(workspace):
    from iatest.application.testing_service import execute_test_plan

    store = EngineStore(workspace, "demo")
    plan = build_plan(
        store,
        fields(
            id="null-check",
            kind="data",
            csv="编号,name\n1,A\n2,",
            rule="not_null",
            column="name",
        ),
    )
    result = execute_test_plan(workspace, "demo", plan)
    assert result["verdict"] == "failed"
    assert result["steps"][0]["failure_rows"] == [1]


@pytest.mark.parametrize("source", ['"id\n', 'id,name\n1,"unterminated'])
def test_malformed_csv_returns_a_user_error(workspace, source):
    with pytest.raises(ValueError, match="CSV"):
        build_plan(
            EngineStore(workspace, "demo"),
            fields(id="bad", kind="data", csv=source, rule="count"),
        )


def test_mobile_builder_saves_ordered_actions_and_independent_expectation(workspace):
    store = MobileStore(workspace, "demo")
    empty = route_get(workspace, "demo", urlparse("/mobile?tab=plans&method=manual"))
    assert "请先保存设备配置" in empty and "/actions/mobile-build-plan" not in empty
    store.save_profile(
        {
            "id": "phone",
            "platform": "android",
            "device_id": "test-device",
            "app_id": "com.example.lab",
        }
    )
    handler = Handler()
    handle_mobile_action(
        workspace,
        "demo",
        "/actions/mobile-build-plan",
        fields(
            profile="phone",
            id="save-name",
            step_count="2",
            op="text",
            identifier="nickname",
            value="测试",
            mode="semantic",
            step1_op="tap",
            step1_identifier="save",
            expected_identifier="status",
            expected_value="已保存",
        ),
        handler,
    )
    steps = store.get("plan", "save-name")["spec"]["steps"]
    assert [s["op"] for s in steps] == ["text", "tap", "expect"]
    assert steps[-1]["expected"] == "已保存"
    assert not store.runs()
    page = route_get(workspace, "demo", urlparse("/mobile?tab=plans&method=manual"))
    assert "data-add-mobile-step" in page and "最终必须满足的结果" in page
    assert "save-name" in page
    with pytest.raises(ValueError):
        handle_mobile_action(
            workspace,
            "demo",
            "/actions/mobile-build-plan",
            fields(profile="phone", id="bad", step_count="31"),
            handler,
        )


def test_mobile_plans_can_be_selected_for_measurement_and_workflows(workspace):
    from iatest.web.mobile_views import compile_mobile_steps
    from iatest.web.plan_forms import saved_options

    mobile = MobileStore(workspace, "demo")
    mobile.save_profile(
        {
            "id": "phone",
            "platform": "android",
            "device_id": "test-device",
            "app_id": "com.example.lab",
        }
    )
    mobile.save_plan(
        {
            "id": "journey",
            "kind": "android",
            "spec": {
                "profile": "phone",
                "steps": compile_mobile_steps(
                    fields(
                        op="tap",
                        identifier="save",
                        expected_identifier="status",
                        expected_value="Saved",
                    )
                ),
            },
        }
    )
    store = EngineStore(workspace, "demo")
    assert ("mobile:journey", "journey · android") in saved_options(
        store, include_mobile=True
    )
    client = build_plan(
        store,
        fields(
            id="measured",
            kind="client_performance",
            plan_id="mobile:journey",
            metric="max_cpu_percent",
            threshold="80",
        ),
    )
    store.save_plan(client)
    workflow = build_plan(
        store,
        {
            **fields(id="flow", kind="workflow"),
            "selected_plans": ["mobile:journey", "measured"],
        },
    )
    store.save_plan(workflow)
    assert workflow["spec"]["nodes"][1]["depends_on"] == ["step0"]
    assert store.runs() == []


@pytest.mark.parametrize(
    "error",
    [
        EOFError("daemon closed"),
        ProtocolError("invalid response"),
        OSError("unavailable"),
    ],
)
def test_doctor_retains_android_when_ios_daemon_fails(workspace, monkeypatch, error):
    monkeypatch.setattr(devices.shutil, "which", lambda name: "/test/" + name)
    monkeypatch.setattr(
        devices,
        "command",
        lambda *a, **kw: "List of devices attached\nserial1 device model:Test_Phone\n",
    )

    def unavailable():
        raise error

    monkeypatch.setattr(usbmux, "list_devices", unavailable)
    handler = Handler()
    handle_mobile_action(workspace, "demo", "/actions/mobile-doctor", {}, handler)
    report = MobileStore(workspace, "demo").list("doctor")[0]
    assert report["devices"][0]["device_id"] == "serial1"
    assert report["errors"] and handler.location.startswith("/mobile?")
    content = detection_results(report)
    assert "serial1" in content and "iOS 设备服务暂不可用" in content
    assert "可继续使用其它已发现设备" in content


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"DeviceList": None},
        {"DeviceList": [{}]},
        {"DeviceList": [{"DeviceID": True, "Properties": {"SerialNumber": "phone"}}]},
        {
            "DeviceList": [
                {
                    "DeviceID": 1,
                    "Properties": {"SerialNumber": "phone", "ConnectionType": []},
                }
            ]
        },
    ],
)
def test_usbmux_rejects_invalid_device_records_as_protocol_errors(
    monkeypatch, response
):
    monkeypatch.setattr(usbmux, "_daemon", lambda: nullcontext(None))
    monkeypatch.setattr(usbmux, "_exchange", lambda *a: response)
    with pytest.raises(ProtocolError):
        usbmux.list_devices()


def test_guided_node_and_release_preserve_existing_contracts(workspace):
    handle_operation(
        workspace,
        "demo",
        "guided-operation",
        fields(
            operation="node-enroll",
            id="worker",
            token_env="TEST_NODE_TOKEN",
            origins="https://test.example",
        ),
        Handler(),
    )
    nodes = ControllerStore(workspace, "demo").nodes()
    assert nodes[0]["id"] == "worker" and not nodes[0]["online"]
    payload = operation_payload(
        "campaign-create",
        {
            "id": ["release"],
            "plans": ["first", "second"],
            "service_version": ["v1"],
            "environment": ["test"],
        },
    )
    assert payload["nodes"][1]["depends_on"] == ["step1"]
    assert payload["max_parallel"] == 1
    with pytest.raises(ValueError):
        operation_payload("campaign-create", fields(id="empty"))
