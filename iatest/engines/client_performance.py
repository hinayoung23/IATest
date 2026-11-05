"""Client observations with explicit source, capability and missing-metric handling."""

from __future__ import annotations

import re
import statistics
import time
from contextvars import ContextVar
from pathlib import Path

from iatest.engines.contracts import EngineContext, EngineResult, ExecutionPlan
from iatest.engines.cpu import (
    android_cpu,
    android_stat,
    chromium_counters,
    chromium_cpu,
)
from iatest.engines.http import validate
from iatest.engines.transactions import percentile
from iatest.mobile.devices import NativeDevice
from iatest.mobile.ios_capture import CAPTURE_SCHEMA
from iatest.mobile.ios_metrics import IOS_METRICS
from iatest.mobile.lease import device_scope
from iatest.mobile.store import MobileStore
from iatest.mobile.telemetry import METRICS as TELEMETRY_METRICS

ACTIVE_CLIENT_CAPTURE = ContextVar("iatest_client_capture", default=None)
METRICS = {
    **TELEMETRY_METRICS,
    **IOS_METRICS,
    "navigation_ms": "ms",
    "fcp_ms": "ms",
    "lcp_observed_ms": "ms",
    "long_task_max_ms": "ms",
    "event_max_ms": "ms",
    "action_max_ms": "ms",
    "android_start_total_ms": "ms",
    "android_start_wait_ms": "ms",
    "memory_pss_kb": "KiB",
    "jank_ratio": "ratio",
    "cpu_percent": "%",
    "app_estimated_battery_mah": "mAh (Android UID system model, observation interval; estimated charge, not exclusive physical joules)",
}
CLIENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["platform", "thresholds"],
    "oneOf": [
        {"required": ["plan"], "not": {"required": ["measurement"]}},
        {"required": ["measurement"], "not": {"required": ["plan"]}},
    ],
    "properties": {
        "platform": {"enum": ["web", "android", "ios"]},
        "plan": {"type": "object"},
        "ios_capture": CAPTURE_SCHEMA,
        "telemetry": {"type": "object", "additionalProperties": False,
            "required": ["artifact_id"], "properties": {
                "artifact_id": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,80}$"}}},
        "measurement": {
            "type": "object",
            "additionalProperties": False,
            "required": ["artifact_id"],
            "properties": {
                "artifact_id": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,80}$"},
                "source_digest": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            },
        },
        "iterations": {"type": "integer", "minimum": 1, "maximum": 20},
        "warmup": {"type": "integer", "minimum": 0, "maximum": 5},
        "startup": {"enum": ["cold", "warm"]},
        "thresholds": {
            "type": "object",
            "minProperties": 1,
            "additionalProperties": False,
            "properties": {
                "max_" + k: {"type": "number", "minimum": 0} for k in METRICS
            },
        },
    },
}


def validate_client(plan):
    from iatest.engines.validation import validate_plan

    validate(plan.spec, CLIENT_SCHEMA)
    if "telemetry" in plan.spec and (plan.spec["platform"] == "web" or "measurement" in plan.spec):
        raise ValueError("SDK telemetry requires a live mobile UI journey")
    if "measurement" in plan.spec:
        if (
            plan.spec["platform"] != "ios"
            or any(
                k in plan.spec
                for k in ("iterations", "warmup", "startup", "ios_capture")
            )
            or plan.preparation
            or plan.input_env
            or plan.data_generation
        ):
            raise ValueError(
                "Offline iOS measurement evaluation cannot execute, install, warm up, generate inputs or repeat a fresh UI journey"
            )
        return
    if "ios_capture" in plan.spec and plan.spec["platform"] != "ios":
        raise ValueError("ios_capture requires an iOS journey")
    if plan.spec.get("ios_capture") and any(
        step.get("op") in {"launch", "terminate"}
        for step in plan.spec["plan"].get("spec", {}).get("steps", [])
    ):
        raise ValueError(
            "An iOS process cannot be launched or terminated inside its recorded journey"
        )
    if plan.spec["platform"] == "ios" and "startup" in plan.spec:
        raise ValueError(
            "Live iOS recording does not measure launch duration; import measured launch metrics instead of requesting cold/warm startup"
        )
    child = ExecutionPlan.from_dict(plan.spec["plan"])
    if (
        plan.preparation.get("app")
        and child.kind in {"android", "ios"}
        and plan.preparation["app"]["profile"] != child.spec.get("profile")
    ):
        raise ValueError(
            "Client preparation and measured journey must use the same device profile"
        )
    if child.kind != plan.spec["platform"] or child.preparation:
        raise ValueError(
            "Client measurement requires a same-platform UI plan; put preparation on the outer plan"
        )
    validate_plan(child)
    if child.kind == "web" and "startup" in plan.spec:
        raise ValueError(
            "Web iterations use fresh browser profiles; App startup modes are not browser cache modes"
        )
    if (
        child.kind != "web"
        and plan.spec.get("startup")
        and any(s["op"] in {"launch", "terminate"} for s in child.spec["steps"])
    ):
        raise ValueError(
            "App startup measurement and explicit launch/terminate steps are separate measurement contracts"
        )


class WebCapture:
    def __init__(self):
        self.values, self.notes = {}, []

    def install(self, browser):
        self.cpu_baseline = None
        try:
            self.cpu_baseline = chromium_counters(
                browser.cdp.call("SystemInfo.getProcessInfo")
            )
            self.cpu_started = time.monotonic()
        except (ValueError, RuntimeError, KeyError, TypeError):
            self.notes.append("Chromium process CPU counters unavailable")
        script = Path(__file__).with_name("client_metrics.js").read_text()
        browser.call(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": script, "worldName": "iatest"},
        )

    def action(self, milliseconds):
        self.values["action_max_ms"] = max(
            milliseconds, self.values.get("action_max_ms", 0)
        )

    def capture(self, browser):
        try:
            if self.cpu_baseline is not None:
                counters = chromium_counters(
                    browser.cdp.call("SystemInfo.getProcessInfo")
                )
                self.values["cpu_percent"] = chromium_cpu(
                    self.cpu_baseline, counters, time.monotonic() - self.cpu_started
                )
        except (ValueError, RuntimeError, KeyError, TypeError):
            self.notes.append("CPU not assessed: Chromium process set/counters changed")
        # Browser-level navigation metrics belong to the main page, not the last frame queried.
        if "main" not in browser.pages:
            self.notes.append("Main page closed; navigation metrics are unavailable")
            return
        browser.select_context({"page": "main"})
        value = browser.evaluate("""(()=>{const d=globalThis.__iatestPerformance||{},n=performance.getEntriesByType('navigation')[0];
          return {data:d,navigation:n?{duration:n.duration,loadEnd:n.loadEventEnd,domEnd:n.domContentLoadedEventEnd}:null};})()""")
        data, navigation = value["data"], value["navigation"]
        if navigation and navigation["loadEnd"] > 0:
            self.values["navigation_ms"] = max(
                self.values.get("navigation_ms", 0), navigation["duration"]
            )
        for metric, field, attribute in (
            ("fcp_ms", "paints", "start"),
            ("lcp_observed_ms", "largest_paints", "start"),
            ("long_task_max_ms", "long_tasks", "duration"),
            ("event_max_ms", "events", "duration"),
        ):
            entries = data.get(field, [])
            if metric == "fcp_ms":
                entries = [e for e in entries if e["name"] == "first-contentful-paint"]
            if entries:
                self.values[metric] = max(
                    self.values.get(metric, 0), max(e[attribute] for e in entries)
                )
            elif field == "long_tasks" and "longtask" in data.get("supported", []):
                self.values[metric] = self.values.get(metric, 0)
        self.notes.extend(
            [
                "Observed LCP candidate is not finalized field LCP; event timing is not a complete INP measurement",
                "Action timing includes CDP checks and local synchronization",
                "Fresh browser profile per iteration; warmup does not establish a warm browser cache",
                "cpu_percent is interval average CPU for all processes of this dedicated Chromium instance; 100% is one logical core, not a peak or renderer-only metric",
            ]
        )


def android_metrics(memory, frames):
    result = {}
    pss = re.search(r"TOTAL PSS:\s*([0-9]+)", memory) or re.search(
        r"^\s*TOTAL\s+([0-9]+)", memory, re.MULTILINE
    )
    total = re.search(r"Total frames rendered:\s*([0-9]+)", frames)
    janky = re.search(r"Janky frames:\s*([0-9]+)", frames)
    if pss:
        result["memory_pss_kb"] = int(pss[1])
    if total and janky and int(total[1]) > 0:
        result["jank_ratio"] = int(janky[1]) / int(total[1])
    return result


class AndroidCapture:
    def __init__(self, profile, control, startup=None, *, energy=False):
        self.device = NativeDevice(profile, "client-metrics")
        self.device.control = control
        self.profile, self.startup = profile, startup
        self.values, self.notes = {}, []
        self.cpu_baseline = None
        self.energy = energy
        self.energy_baseline = None

    def prepare_ui_session(self, session):
        session.wait_android_ready(min(5, self.device.control.remaining() or 5))

    def cpu_snapshot(self):
        pids = self.device.adb("shell", "pidof", self.profile["app_id"]).strip().split()
        if len(pids) != 1 or not pids[0].isdigit():
            raise ValueError("App main process PID unavailable or ambiguous")
        pid = pids[0]
        name = (
            self.device.adb("shell", "cat", f"/proc/{pid}/cmdline")
            .split("\0")[0]
            .strip()
        )
        if name != self.profile["app_id"]:
            raise ValueError("App CPU PID identity mismatch")
        value = android_stat(self.device.adb("shell", "cat", f"/proc/{pid}/stat"))
        if value[0] != int(pid):
            raise ValueError("App CPU stat PID mismatch")
        return value

    def before(self):
        app = self.profile["app_id"]
        if self.energy:
            from iatest.mobile.android_energy import snapshot

            try:
                self.energy_baseline = snapshot(self.device)
            except (OSError, RuntimeError, ValueError) as exc:
                self.notes.append("UID battery estimate not assessed: " + str(exc))
        if self.startup:
            component = (
                self.device.adb(
                    "shell", "cmd", "package", "resolve-activity", "--brief", app
                )
                .strip()
                .splitlines()[-1]
            )
            if not re.fullmatch(r"[A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+", component):
                raise ValueError("App launch activity is unavailable")
            if self.startup == "warm":
                self.device.adb("shell", "input", "keyevent", "KEYCODE_HOME")
            output = self.device.adb(
                "shell",
                "am",
                "start",
                "-W",
                *(["-S"] if self.startup == "cold" else []),
                "-n",
                component,
            )
            value = re.search(r"TotalTime:\s*([0-9]+)", output)
            if value:
                self.values["android_start_total_ms"] = int(value[1])
            if waited := re.search(r"WaitTime:\s*([0-9]+)", output):
                self.values["android_start_wait_ms"] = int(waited[1])
            self.notes.append(
                "am start TotalTime is a platform launch timing, not full application readiness; warm resume depends on the actual process state"
            )
        self.device.adb("shell", "dumpsys", "gfxinfo", app, "reset")
        try:
            self.cpu_hz = int(self.device.adb("shell", "getconf", "CLK_TCK").strip())
            if not 1 <= self.cpu_hz <= 100000:
                raise ValueError("Invalid kernel tick rate")
            self.cpu_baseline = self.cpu_snapshot()
            self.cpu_started = time.monotonic()
        except (OSError, RuntimeError, ValueError, IndexError):
            self.notes.append(
                "App process CPU counters unavailable; CPU remains not_assessed"
            )

    def after(self):
        if self.energy_baseline is not None:
            from iatest.mobile.android_energy import measurements, snapshot

            try:
                self.values.update(measurements(self.energy_baseline, snapshot(self.device)))
                self.notes.append("app_estimated_battery_mah is a read-only since-charged model delta for a unique App UID. It includes platform attribution and is estimated charge in mAh, not measured exclusive energy or joules; no voltage conversion is assumed.")
            except (OSError, RuntimeError, ValueError) as exc:
                self.notes.append("UID battery estimate not assessed: " + str(exc))
        if self.cpu_baseline is not None:
            try:
                current = self.cpu_snapshot()
                self.values["cpu_percent"] = android_cpu(
                    self.cpu_baseline,
                    current,
                    self.cpu_hz,
                    time.monotonic() - self.cpu_started,
                )
            except (OSError, RuntimeError, ValueError, IndexError):
                self.notes.append(
                    "CPU not assessed: App process restarted or counters became unavailable"
                )
        app = self.profile["app_id"]
        memory = self.device.adb("shell", "dumpsys", "meminfo", app)
        frames = self.device.adb("shell", "dumpsys", "gfxinfo", app)
        self.values.update(android_metrics(memory, frames))
        self.notes.append(
            "PSS is a post-journey snapshot, not peak memory; frame stats require platform support and rendered frames"
        )
        self.notes.append(
            "cpu_percent is the main App process interval average, excluding secondary processes; 100% is one logical core. ADB collection overhead is included; physical precision remains unvalidated"
        )


class ClientPerformanceEngine:
    def __init__(self, root, project, registry):
        self.root, self.project, self.registry = root, project, registry

    def capabilities(self):
        return {
            "kind": "client_performance",
            "available": True,
            "platforms": ["web", "android", "ios"],
            "metrics": METRICS,
            "limitations": [
                "iOS explicit isolated_copy attach, device_processes and launch recording have physical-device evidence for the owned Lab; standard CLI attach and XCTest zero frame counts remain unresolved. Frame lifetimes are not render durations",
                "Missing metrics are not_assessed and cannot satisfy gates",
            ],
        }

    def execute(self, plan, context):
        from contextlib import ExitStack

        validate_client(plan)
        if "measurement" in plan.spec:
            from iatest.application.ios_measurements import evaluate_measurement

            return evaluate_measurement(self.root, self.project, plan)
        platform = plan.spec["platform"]
        result = EngineResult(
            plan.id,
            "client_performance",
            limitations=self.capabilities()["limitations"],
        )
        measurements, repetitions, missing, notes, capture_errors = (
            {},
            [],
            set(),
            [],
            [],
        )
        measurement_sources = []
        capture_failures = []
        from iatest.mobile.bridge import read_bridge
        from iatest.mobile.telemetry import measurements as telemetry_measurements
        with ExitStack() as stack:
            profile = None
            if platform != "web":
                profile = MobileStore(self.root, self.project).get(
                    "profile", plan.spec["plan"]["spec"]["profile"]
                )
                stack.enter_context(device_scope(profile))
            for iteration in range(
                plan.spec.get("iterations", 3) + plan.spec.get("warmup", 0)
            ):
                context.control.check()
                telemetry_before = None
                if plan.spec.get("telemetry"):
                    telemetry_before = read_bridge(self.root, self.project, profile["id"],
                        plan.spec["telemetry"]["artifact_id"], control=context.control)
                collector = (
                    WebCapture()
                    if platform == "web"
                    else AndroidCapture(
                        profile, context.control, plan.spec.get("startup"),
                        energy="max_app_estimated_battery_mah" in plan.spec["thresholds"],
                    )
                    if platform == "android"
                    else self.ios_collector(
                        profile, context.control, plan.spec["ios_capture"]
                    )
                    if plan.spec.get("ios_capture")
                    else None
                )
                token = ACTIVE_CLIENT_CAPTURE.set(
                    collector
                    if platform in {"web", "android"}
                    or getattr(collector, "session_lifecycle", False)
                    else None
                )
                try:
                    if (
                        platform == "android"
                        or plan.spec.get("ios_capture")
                        and not getattr(collector, "session_lifecycle", False)
                    ):
                        try:
                            collector.before()
                        except ValueError as exc:
                            from iatest.mobile.ios_capture import CaptureFailure

                            if not isinstance(exc, CaptureFailure):
                                raise
                            result.lifecycle, result.verdict, result.error = (
                                "execution_error",
                                "inconclusive",
                                str(exc),
                            )
                            result.metrics = {
                                "platform": platform,
                                "capture_diagnostics": {
                                    "code": exc.code,
                                    "phase": exc.phase,
                                },
                                "model_calls": 0,
                            }
                            return result
                    child = self.registry.execute(
                        ExecutionPlan.from_dict(plan.spec["plan"]),
                        EngineContext(context.control),
                    )
                    from dataclasses import asdict

                    from iatest.application.testing_service import safe_result
                    from iatest.engines.store import EngineStore

                    child = EngineResult(**safe_result(asdict(child)))
                    EngineStore(self.root, self.project).save_result(child)
                    result.evidence.extend(
                        {
                            **e,
                            "run_id": e.get("run_id") or child.run_id,
                            "iteration": iteration,
                        }
                        for e in child.evidence
                    )
                    if (
                        platform == "android"
                        or plan.spec.get("ios_capture")
                        and not getattr(collector, "session_lifecycle", False)
                    ):
                        try:
                            collector.after()
                        except (OSError, ValueError, RuntimeError) as exc:
                            collector.values = {}
                            collector.notes.append(
                                "Measurement unavailable: " + str(exc)
                            )
                            capture_errors.append(str(exc))
                            from iatest.mobile.ios_capture import CaptureFailure

                            if isinstance(exc, CaptureFailure):
                                capture_failures.append(
                                    {
                                        "code": exc.code,
                                        "phase": exc.phase,
                                        "iteration": iteration,
                                    }
                                )
                    if platform == "ios" and collector:
                        if diagnostic := child.metrics.get("capture_diagnostics"):
                            capture_failures.append(
                                {**diagnostic, "iteration": iteration}
                            )
                        capture_errors.extend(
                            getattr(collector, "measurement_errors", [])
                        )
                        if (
                            getattr(collector, "session_lifecycle", False)
                            and not collector.ui_session_completed
                            and not collector.measurement_errors
                        ):
                            capture_errors.append(
                                "The recorded UI session did not complete its measurement lifecycle"
                            )
                    values = dict(collector.values) if collector else {}
                    if telemetry_before:
                        try:
                            telemetry_after = read_bridge(self.root, self.project, profile["id"],
                                plan.spec["telemetry"]["artifact_id"], control=context.control)
                            values.update(telemetry_measurements(telemetry_before, telemetry_after))
                            notes.append("SDK telemetry is App-reported. app_metal_gpu_busy_ratio measures the interval union of explicitly registered completed Metal command buffers; it excludes unregistered work and other Apps. Device-prefixed metrics include other Apps; network counters are interval deltas.")
                        except (OSError, ValueError, RuntimeError) as exc:
                            capture_errors.append("SDK telemetry unavailable: " + str(exc))
                    timings = [
                        r["elapsed_ms"] for r in child.steps if "elapsed_ms" in r
                    ]
                    if timings:
                        values["action_max_ms"] = max(timings)
                    if collector:
                        notes.extend(collector.notes)
                        if (
                            platform == "ios"
                            and collector.source
                            and collector.source not in measurement_sources
                        ):
                            measurement_sources.append(collector.source)
                    if iteration >= plan.spec.get("warmup", 0):
                        repetitions.append(
                            {
                                "iteration": iteration,
                                "run_id": child.run_id,
                                "verdict": child.verdict,
                                "lifecycle": child.lifecycle,
                                "metrics": values,
                                "error": child.error,
                                **(
                                    {"measurement_id": collector.artifact_id}
                                    if platform == "ios"
                                    and collector
                                    and collector.artifact_id
                                    else {}
                                ),
                            }
                        )
                        for key, value in values.items():
                            measurements.setdefault(key, []).append(value)
                    if child.lifecycle != "completed" or child.verdict != "passed":
                        result.lifecycle, result.error = child.lifecycle, child.error
                        result.verdict = child.verdict
                        break
                finally:
                    ACTIVE_CLIENT_CAPTURE.reset(token)
                    if platform == "ios" and collector:
                        collector.close()
        summaries = {
            key: {
                "samples": len(values),
                "min": min(values),
                "median": statistics.median(values),
                "p95": percentile(values, 0.95),
                "max": max(values),
                "unit": METRICS[key],
            }
            for key, values in measurements.items()
        }
        for key, threshold in plan.spec["thresholds"].items():
            metric = key.removeprefix("max_")
            values = measurements.get(metric, [])
            assessed = len(values) == plan.spec.get("iterations", 3)
            if not assessed:
                missing.add(metric)
            actual = max(values) if assessed else None
            result.steps.append(
                {
                    "gate": key,
                    "actual": actual,
                    "expected": threshold,
                    "assessed": assessed,
                    "passed": assessed and actual <= threshold,
                }
            )
        result.metrics = {
            "platform": platform,
            "measurements": summaries,
            "iterations": len(repetitions),
            "warmup": plan.spec.get("warmup", 0),
            "repetitions": repetitions,
            "not_assessed": sorted(missing),
            "model_calls": 0,
            "capture_errors": capture_errors,
            **(
                {
                    "capture_diagnostics": capture_failures[0],
                    "capture_failures": capture_failures,
                }
                if capture_failures
                else {}
            ),
            "measurement_sources": measurement_sources,
            "fresh_ui_execution": True,
            "unconfigured_metrics": sorted(
                set(measurements)
                - {key.removeprefix("max_") for key in plan.spec["thresholds"]}
            ),
            "method": "native browser performance entries"
            if platform == "web"
            else "Android system reports + host UI timings"
            if platform == "android"
            else "iOS xctrace measured tables + host UI timings"
            if plan.spec.get("ios_capture")
            else "host UI timings only",
        }
        result.limitations.extend(sorted(set(notes)))
        if result.verdict == "not_evaluated":
            result.verdict = (
                "inconclusive"
                if missing or capture_errors or not result.steps
                else "passed"
                if all(s["passed"] for s in result.steps)
                else "failed"
            )
        from iatest.engines.verdicts import summarize_checks
        if result.verdict != "inconclusive":
            summarize_checks(result, len(plan.spec["thresholds"]), sum(bool(row.get("assessed")) for row in result.steps), sum(bool(row.get("assessed")) and row.get("passed") is False for row in result.steps))
        else:
            result.metrics.update(checks_planned=len(plan.spec["thresholds"]), checks_assessed=sum(bool(row.get("assessed")) for row in result.steps), checks_failed=sum(bool(row.get("assessed")) and row.get("passed") is False for row in result.steps))
        return result

    def ios_collector(self, profile, control, spec):
        from iatest.mobile.ios_capture import IOSCapture

        return IOSCapture(self.root, self.project, profile, control, spec)
