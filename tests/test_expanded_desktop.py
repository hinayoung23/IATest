import sys

import pytest

from iatest.core.config import init_project
from iatest.engines.contracts import EngineContext, ExecutionPlan
from iatest.engines.desktop import DesktopEngine, validate_desktop
from iatest.execution.control import RunControl


@pytest.mark.parametrize('variable,value', [('XDG_SESSION_TYPE','wayland'), ('WAYLAND_DISPLAY','wayland-0')])
def test_linux_xwayland_display_does_not_enable_global_keyboard(monkeypatch, variable, value):
    from iatest.engines.desktop import desktop_command

    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setenv('DISPLAY', ':99')
    monkeypatch.delenv('XDG_SESSION_TYPE', raising=False)
    monkeypatch.delenv('WAYLAND_DISPLAY', raising=False)
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValueError, match='X11'):
        desktop_command('linux')


@pytest.mark.parametrize(
    "matches,expected,verdict",
    [(0, False, "passed"), (1, True, "passed"), (2, False, "failed")],
)
def test_desktop_owned_ipc_and_absence_never_passes_ambiguous_targets(
    tmp_path, monkeypatch, matches, expected, verdict
):
    # This is an IPC fixture, not a substitute for native macOS/Windows acceptance.
    driver = tmp_path / "driver.py"
    driver.write_text(
        'import sys,json\nfor line in sys.stdin:\n r=json.loads(line)\n print(json.dumps({"sequence":r["sequence"],"result":{"complete":True,"match_count":'
        + str(matches)
        + ',"candidates":[{"name":"fixture"}]*'
        + str(matches)
        + "}}),flush=True)\n"
    )
    monkeypatch.setattr(
        "iatest.engines.desktop.desktop_command",
        lambda platform: [sys.executable, str(driver)],
    )
    init_project(tmp_path, "suite")
    plan = ExecutionPlan.from_dict(
        {
            "id": "desktop",
            "kind": "desktop",
            "spec": {
                "platform": "macos",
                "app_id": "com.example.app",
                "process_id": 123,
                "steps": [
                    {
                        "op": "expect",
                        "target": {"identifier": "status"},
                        "attribute": "exists",
                        "expected": expected,
                        "timeout_seconds": 0.01,
                    }
                ],
            },
        }
    )
    result = DesktopEngine(tmp_path, "suite").execute(
        plan, EngineContext(RunControl(timeout_seconds=5))
    )
    assert result.verdict == verdict


def test_desktop_boolean_expectation_rejects_string_false():
    plan = ExecutionPlan.from_dict(
        {
            "id": "desktop",
            "kind": "desktop",
            "spec": {
                "platform": "windows",
                "app_id": "fixture",
                "process_id": 123,
                "steps": [
                    {
                        "op": "expect",
                        "target": {"name": "status"},
                        "attribute": "exists",
                        "expected": "false",
                    }
                ],
            },
        }
    )
    with pytest.raises(ValueError, match="boolean"):
        validate_desktop(plan)
