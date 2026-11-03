import base64
import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

from iatest.engines.desktop import DesktopSession, validate_desktop
from iatest.execution.control import RunControl

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="


@pytest.mark.parametrize(
    "step",
    [
        {"op": "drag"},
        {"op": "scroll", "delta_y": 0},
        {"op": "visual_click"},
    ],
)
def test_extended_desktop_actions_require_explicit_arguments(step):
    with pytest.raises(ValueError):
        validate_desktop(
            SimpleNamespace(
                spec={
                    "platform": "linux",
                    "process_id": 123,
                    "app_id": "fixture",
                    "steps": [
                        {**step, "target": {"identifier": "pad"}},
                        {
                            "op": "expect",
                            "target": {"identifier": "status"},
                            "attribute": "exists",
                            "expected": True,
                        },
                    ],
                }
            )
        )


def test_desktop_visual_anchor_revalidates_pixels_before_native_pointer(
    tmp_path, monkeypatch
):
    # Controlled native IPC contract; real AX/UIAutomation/AT-SPI is validated
    # separately by scripts/validate_desktop_interactions.py.
    driver = tmp_path / "driver.py"
    log = tmp_path / "requests.jsonl"
    driver.write_text("""import json,sys
for line in sys.stdin:
 request=json.loads(line)
 with open(sys.argv[1],'a') as log:log.write(line)
 result={'data':sys.argv[2],'geometry':[20,30,1,1]} if request['op']=='screenshot' else {'dispatch':'acknowledged'}
 print(json.dumps({'sequence':request['sequence'],'result':result}),flush=True)
""")
    monkeypatch.setattr(
        "iatest.engines.desktop.desktop_command",
        lambda platform: [sys.executable, str(driver), str(log), PNG],
    )
    session = DesktopSession(
        {"platform": "macos", "process_id": 123, "app_id": "fixture"},
        RunControl(timeout_seconds=10),
    )
    try:
        region = session.visual_region({"identifier": "pad"})
        assert region["sha256"] == hashlib.sha256(base64.b64decode(PNG)).hexdigest()
        anchor = {k: region[k] for k in ("sha256", "width", "height", "viewport")}
        anchor.update(x=0.5, y=0.5)
        session.visual_click({"target": {"identifier": "pad"}, "visual_anchor": anchor})
        assert [json.loads(line)["op"] for line in log.read_text().splitlines()].count(
            "pointer_click"
        ) == 1
        with pytest.raises(ValueError, match="expired"):
            session.visual_click(
                {
                    "target": {"identifier": "pad"},
                    "visual_anchor": {**anchor, "sha256": "0" * 64},
                }
            )
        assert not session.delivery_started
        assert [json.loads(line)["op"] for line in log.read_text().splitlines()].count(
            "pointer_click"
        ) == 1
    finally:
        session.close()


def test_wayland_context_retains_explicit_portal_option():
    from iatest.application.ui_exploration import context_spec

    context = {
        "kind": "desktop",
        "platform": "linux",
        "process_id": 123,
        "app_id": "fixture",
        "wayland_portal": True,
    }
    assert context_spec(context)["wayland_portal"] is True
