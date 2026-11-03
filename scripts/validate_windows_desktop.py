"""Real UIAutomation acceptance; run in a logged-in Windows desktop session.

Creates one owned WinForms Lab and operates only its exact PID. No login, SSH,
execution-policy or firewall settings are changed. No model or business App used.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from iatest.application.testing_service import execute_test_plan
from iatest.core.config import init_project
from iatest.engines.desktop import DesktopSession
from iatest.execution.control import RunControl

LAB = r"""
$ErrorActionPreference='Stop'
[AppContext]::SetSwitch('Switch.UseLegacyAccessibilityFeatures',$false)
[AppContext]::SetSwitch('Switch.UseLegacyAccessibilityFeatures.2',$false)
[AppContext]::SetSwitch('Switch.UseLegacyAccessibilityFeatures.3',$false)
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
Add-Type -ReferencedAssemblies System.Windows.Forms,System.Drawing -TypeDefinition @'
using System;
using System.Drawing;
using System.Windows.Forms;
public static class IATestDesktopLab {
  [STAThread] public static void Main() {
    Application.EnableVisualStyles();
    var form=new Form {Text="IATest Desktop Lab",Width=560,Height=300,StartPosition=FormStartPosition.CenterScreen};
    var input=new TextBox {Name="nickname",AccessibleName="Nickname",Left=20,Top=20,Width=480};
    var status=new TextBox {Name="status",AccessibleName="Status",Left=20,Top=100,Width=480,ReadOnly=true,Text="Not saved"};
    var count=new TextBox {Name="count",AccessibleName="Action count",Left=20,Top=140,Width=480,ReadOnly=true,Text="0"};
    int saved=0;
    var save=new Button {Name="save",AccessibleName="Save",Text="Save",Left=20,Top=55};
    save.Click+=(s,e)=>{status.Text="Saved: "+input.Text;count.Text=(++saved).ToString();};
    form.Controls.Add(input);form.Controls.Add(save);form.Controls.Add(status);form.Controls.Add(count);
    for(int i=0;i<2;i++) {var duplicate=new Button {AccessibleName="Duplicate",Text="Duplicate",Left=20+i*120,Top=190};duplicate.Click+=(s,e)=>{count.Text=(++saved).ToString();};form.Controls.Add(duplicate);}
    form.Shown+=(s,e)=>{form.Activate();input.Focus();};
    Application.Run(form);
  }
}
'@
[IATestDesktopLab]::Main()
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("Run on Windows in its interactive user session")
    args.output = args.output.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "passed": False,
        "synthetic_driver": False,
        "model_calls": 0,
        "checks": {},
        "results": [],
    }
    app = None
    try:
        session_id = ctypes.c_ulong()
        if (
            not ctypes.windll.kernel32.ProcessIdToSessionId(
                os.getpid(), ctypes.byref(session_id)
            )
            or session_id.value == 0
        ):
            raise ValueError(
                "A logged-in interactive session is required; SSH session 0 cannot validate desktop input"
            )
        report["windows_session_id"] = session_id.value
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.OpenInputDesktop.restype = ctypes.c_void_p
        desktop = user32.OpenInputDesktop(0, False, 0x0001)
        if not desktop:
            raise RuntimeError(
                "Interactive input desktop is unavailable; unlock the Windows console"
            )
        try:
            desktop_name = ctypes.create_unicode_buffer(256)
            needed = ctypes.c_ulong()
            user32.GetUserObjectInformationW.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_ulong,
                ctypes.POINTER(ctypes.c_ulong),
            ]
            if not user32.GetUserObjectInformationW(
                desktop,
                2,
                desktop_name,
                ctypes.sizeof(desktop_name),
                ctypes.byref(needed),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            report["input_desktop"] = desktop_name.value
            if desktop_name.value.casefold() != "default":
                raise RuntimeError(
                    "Unlock the Windows console before desktop acceptance"
                )
        finally:
            user32.CloseDesktop.argtypes = [ctypes.c_void_p]
            user32.CloseDesktop(desktop)
        shell = shutil.which("powershell") or shutil.which("pwsh")
        if not shell:
            raise ValueError("Windows PowerShell is required")
        policy = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", "Get-ExecutionPolicy"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        report["execution_policy_before"] = policy
        app = subprocess.Popen(
            [shell, "-NoProfile", "-NonInteractive", "-Command", LAB],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        report["owned_lab_pid"] = app.pid
        root = args.output.parent / ("desktop-project-" + uuid.uuid4().hex)
        init_project(root, "desktop")
        spec = {
            "platform": "windows",
            "process_id": app.pid,
            "app_id": Path(shell).stem,
        }
        deadline = time.monotonic() + 20
        readiness = []
        report["readiness_observations"] = readiness
        while True:
            if app.poll() is not None:
                raise RuntimeError("Owned WinForms Lab exited before readiness")
            browser = DesktopSession(spec, RunControl(timeout_seconds=10))
            try:
                observed = browser.call("query", {"name": "Nickname"})
                if len(readiness) < 8:
                    readiness.append(observed)
                if not observed.get("match_count") and "lab_controls" not in report:
                    diagnostic = (
                        "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);"
                        "Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes;"
                        "Add-Type -AssemblyName UIAutomationClientsideProviders;"
                        "[void][Windows.Automation.AutomationElement]::RootElement;"
                        "[Windows.Automation.ClientSettings]::RegisterClientSideProviderAssembly([UIAutomationClientsideProviders.UIAutomationClientSideProviders].Assembly.GetName());"
                        f"$p=Get-Process -Id {app.pid};"
                        "$r=[Windows.Automation.AutomationElement]::FromHandle($p.MainWindowHandle);"
                        "$r.FindAll([Windows.Automation.TreeScope]::Subtree,[Windows.Automation.Condition]::TrueCondition) | "
                        "ForEach-Object { @{name=$_.Current.Name;identifier=$_.Current.AutomationId;role=$_.Current.ControlType.ProgrammaticName} } | ConvertTo-Json -Compress"
                    )
                    report["lab_controls"] = json.loads(
                        subprocess.run(
                            [
                                shell,
                                "-NoProfile",
                                "-NonInteractive",
                                "-Command",
                                diagnostic,
                            ],
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            check=True,
                            timeout=10,
                        ).stdout
                    )
                if observed.get("complete") and observed.get("match_count") == 1:
                    break
            except (OSError, RuntimeError, ValueError):
                if time.monotonic() >= deadline:
                    raise
            finally:
                browser.close()
            if time.monotonic() >= deadline:
                raise TimeoutError("Owned Lab controls did not become ready")
            time.sleep(0.1)
        for index in range(2):
            text = "iatest_中文_" + uuid.uuid4().hex[:10]
            steps = [
                {"op": "fill", "target": {"name": "Nickname"}, "value": text},
                {
                    "op": "expect",
                    "target": {"name": "Nickname"},
                    "attribute": "value",
                    "expected": text,
                },
                {"op": "click", "target": {"name": "Save"}},
                {
                    "op": "expect",
                    "target": {"name": "Status"},
                    "attribute": "value",
                    "expected": "Saved: " + text,
                },
                {"op": "press", "target": {"name": "Nickname"}, "key": "Tab"},
                {
                    "op": "expect",
                    "target": {"name": "Action count"},
                    "attribute": "value",
                    "expected": str(index + 1),
                },
            ]
            result = execute_test_plan(
                root,
                "desktop",
                {
                    "id": "desktop-replay-" + str(index),
                    "kind": "desktop",
                    "budget": {"timeout_seconds": 40},
                    "spec": {**spec, "steps": steps},
                },
            )
            report["results"].append(result)
            report["checks"]["unicode_save_and_keyboard_" + str(index)] = (
                result["verdict"] == "passed"
            )
            if result["verdict"] != "passed":
                raise AssertionError(
                    result.get("error", "Desktop business assertion failed")
                )
        duplicate = execute_test_plan(
            root,
            "desktop",
            {
                "id": "ambiguous-blocked",
                "kind": "desktop",
                "spec": {
                    **spec,
                    "steps": [
                        {"op": "click", "target": {"name": "Duplicate"}},
                        {
                            "op": "expect",
                            "target": {"name": "Action count"},
                            "attribute": "value",
                            "expected": "2",
                        },
                    ],
                },
            },
        )
        report["results"].append(duplicate)
        verifier = DesktopSession(spec, RunControl(timeout_seconds=10))
        try:
            observed = verifier.call("query", {"name": "Action count"})
            report["checks"]["ambiguous_input_refused_without_side_effect"] = (
                duplicate["verdict"] == "inconclusive"
                and observed.get("complete")
                and observed.get("match_count") == 1
                and observed["candidates"][0]["value"] == "2"
            )
        finally:
            verifier.close()
        after = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", "Get-ExecutionPolicy"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        report["checks"]["execution_policy_unchanged"] = after == policy
        report["passed"] = all(report["checks"].values())
    except Exception as exc:  # noqa: BLE001 -- Keep a bounded real-environment failure.
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if app and app.poll() is None:
            app.terminate()
            app.wait(timeout=5)
        report["checks"]["owned_lab_stopped"] = (
            app is not None and app.poll() is not None
        )
        report["passed"] = report["passed"] and all(report["checks"].values())
        if args.output.exists():
            args.output.with_name(
                args.output.stem + "-attempt-" + str(time.time_ns()) + ".json"
            ).write_bytes(args.output.read_bytes())
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(
            json.dumps(
                {k: v for k, v in report.items() if k != "results"}, ensure_ascii=True
            )
        )
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
