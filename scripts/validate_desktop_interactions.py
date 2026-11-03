"""Native desktop pointer, screenshot and stale-anchor acceptance in an owned Lab.

Requires an unlocked interactive session. No model calls or business App input.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from iatest.engines.desktop import DesktopSession
from iatest.execution.control import RunControl


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mac-app", type=Path)
    parser.add_argument("--gtk-python", default="/usr/bin/python3")
    parser.add_argument(
        "--wayland-gnome",
        action="store_true",
        help="Use the explicitly enabled GNOME adapter and two interactive consent dialogs",
    )
    args = parser.parse_args()
    report = {"passed": False, "synthetic_driver": False, "checks": []}
    app = session = None
    try:
        if args.wayland_gnome and not sys.platform.startswith("linux"):
            raise ValueError("--wayland-gnome requires Linux")
        if sys.platform == "darwin":
            if not args.mac_app:
                raise ValueError(
                    "--mac-app must name the built examples/desktop_controls_lab.swift App"
                )
            app = subprocess.Popen(
                [str(args.mac_app.resolve() / "Contents/MacOS/DesktopLab")]
            )
            spec = {
                "platform": "macos",
                "process_id": app.pid,
                "app_id": "dev.iatest.desktop.lab",
            }
        elif sys.platform.startswith("linux"):
            environment = os.environ.copy()
            if args.wayland_gnome:
                if not environment.get("WAYLAND_DISPLAY"):
                    raise ValueError("A live Wayland session is required")
                environment.pop("DISPLAY", None)
                environment["GDK_BACKEND"] = "wayland"
            app = subprocess.Popen(
                [
                    args.gtk_python,
                    str(
                        Path(__file__).resolve().parents[1]
                        / "examples/desktop_controls_lab.py"
                    ),
                ],
                env=environment,
            )
            spec = {
                "platform": "linux",
                "process_id": app.pid,
                "app_id": str(Path(f"/proc/{app.pid}/exe").resolve()),
            }
            if args.wayland_gnome:
                spec.update(wayland_portal=True, wayland_backend="gnome")
                report["real_wayland"] = True
        elif os.name == "nt":
            from validate_windows_desktop import LAB

            lab = (
                LAB.replace(
                    "public static class IATestDesktopLab",
                    """public class PointerPad : Panel {
 public Action<string> Report; Point began;
 protected override void OnMouseDown(MouseEventArgs e){base.OnMouseDown(e);began=e.Location;if(e.Button==MouseButtons.Right)Report("right_click");}
 protected override void OnMouseUp(MouseEventArgs e){base.OnMouseUp(e);if(e.Button==MouseButtons.Left)Report(Math.Abs(e.X-began.X)+Math.Abs(e.Y-began.Y)>20?"drag":"click");}
 protected override void OnMouseWheel(MouseEventArgs e){base.OnMouseWheel(e);Report("scroll");}
}
public static class IATestDesktopLab""",
                )
                .replace("Height=300", "Height=460")
                .replace(
                    "form.Shown+=",
                    """var events=new TextBox {Name="events",AccessibleName="events",Left=20,Top=340,Width=480,ReadOnly=true,Text="Waiting"};form.Controls.Add(events);
 for(int n=0;n<2;n++){var pad=new PointerPad {Name=n==0?"source":"destination",AccessibleName=n==0?"source":"destination",Left=20+n*240,Top=240,Width=200,Height=70,BackColor=Color.CornflowerBlue};pad.Report=s=>events.Text=s;form.Controls.Add(pad);}
 form.Shown+=""",
                )
            )
            shell = shutil.which("powershell") or shutil.which("pwsh")
            app = subprocess.Popen(
                [shell, "-NoProfile", "-NonInteractive", "-Command", lab],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            spec = {
                "platform": "windows",
                "process_id": app.pid,
                "app_id": Path(shell).stem,
            }
        else:
            raise ValueError("Unsupported platform")
        if args.wayland_gnome:
            time.sleep(2)
            print(
                "Waiting for system input and scoped window-capture consent", flush=True
            )
        session = DesktopSession(
            spec, RunControl(timeout_seconds=210 if args.wayland_gnome else 90)
        )

        def expect(identifier, expected):
            until = time.monotonic() + 4
            while True:
                result = session.call("query", {"identifier": identifier})
                if (
                    result["complete"]
                    and result["match_count"] == 1
                    and result["candidates"][0]["text"] == expected
                ):
                    return
                if time.monotonic() > until:
                    raise AssertionError(f"{identifier}: {result!r}")
                time.sleep(0.05)

        until = time.monotonic() + 15
        while True:
            try:
                value = session.call("query", {"identifier": "source"})
                if value["complete"] and value["match_count"] == 1:
                    break
            except ValueError:
                pass
            if time.monotonic() > until:
                raise TimeoutError("Owned Lab not ready")
            time.sleep(0.15)
        source = {"identifier": "source"}
        session.visual_region(source)
        report["checks"].append("screenshot_before_any_input")
        session.call("right_click", source)
        expect("events", "right_click")
        report["checks"].append("right_click")
        session.call("scroll", source, delta_y=2)
        expect("events", "scroll")
        report["checks"].append("scroll")
        session.call("drag", source, destination={"identifier": "destination"})
        expect("events", "drag")
        report["checks"].append("drag")
        session.call("fill", {"identifier": "nickname"}, value="abc")
        session.call("press", {"identifier": "nickname"}, key="End")
        session.call("press", {"identifier": "nickname"}, key="ArrowLeft")
        session.call("press", {"identifier": "nickname"}, key="Delete")
        expect("nickname", "ab")
        report["checks"].append("navigation_delete")
        region = session.visual_region(source)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.with_suffix(".png").write_bytes(region["data"])
        anchor = {k: region[k] for k in ("sha256", "width", "height", "viewport")}
        if "binding" in region:
            anchor["binding"] = region["binding"]
        anchor.update(x=region["width"] / 2, y=region["height"] / 2)
        session.visual_click({"target": source, "visual_anchor": anchor})
        expect("events", "click")
        report["checks"].append("owned_pixels_visual_click")
        # Tampered/expired pixels must never produce input.
        try:
            session.visual_click(
                {"target": source, "visual_anchor": {**anchor, "sha256": "0" * 64}}
            )
        except ValueError:
            pass
        else:
            raise AssertionError("Stale visual anchor accepted")
        expect("events", "click")
        report["checks"].append("stale_anchor_rejected")
        if "binding" in anchor:
            try:
                session.visual_click(
                    {"target": source, "visual_anchor": {**anchor, "binding": "0" * 64}}
                )
            except ValueError:
                pass
            else:
                raise AssertionError("Changed native window identity accepted")
            expect("events", "click")
            report["checks"].append("window_identity_rejected")
        report["passed"] = True
    except Exception as exc:  # noqa: BLE001 -- preserve native failure evidence
        report["error"] = repr(exc)
    finally:
        if session:
            session.close()
        if app and app.poll() is None:
            app.terminate()
            app.wait(timeout=5)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
