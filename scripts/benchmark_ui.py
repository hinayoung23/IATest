"""Interleaved three-way evidence comparison. Estimates are not model token usage."""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from iatest.agents.runtime import estimate_tokens
from iatest.application.ui_exploration import semantic_state
from iatest.core.config import init_project
from iatest.engines.contracts import ExecutionPlan
from iatest.engines.validation import validate_plan
from iatest.engines.web import BrowserSession
from iatest.execution.control import RunControl


def monitored_run(command, value, env):
    """100 ms process-tree samples; CPU is a lower bound when processes exit."""
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    stopped = threading.Event()
    observation = {
        "sampled_peak_tree_rss_kib": None,
        "observed_tree_cpu_seconds": None,
        "resource_samples": 0,
    }
    counters = {}

    def sample():
        while not stopped.is_set():
            try:
                output = subprocess.check_output(
                    ["ps", "-axo", "pid=,ppid=,rss=,time="], text=True, timeout=2
                )
                rows = {}
                for line in output.splitlines():
                    pid, parent, rss, cpu = line.split()
                    days, _, clock = cpu.rpartition("-")
                    seconds = 0.0
                    for part in clock.split(":"):
                        seconds = seconds * 60 + float(part)
                    seconds += float(days or 0) * 86400
                    rows[int(pid)] = int(parent), int(rss), seconds
                owned = {process.pid}
                while True:
                    larger = owned | {p for p, data in rows.items() if data[0] in owned}
                    if larger == owned:
                        break
                    owned = larger
                live = {p: rows[p] for p in owned if p in rows}
                if live:
                    observation["resource_samples"] += 1
                    observation["sampled_peak_tree_rss_kib"] = max(
                        observation["sampled_peak_tree_rss_kib"] or 0,
                        sum(r[1] for r in live.values()),
                    )
                    for pid, data in live.items():
                        counters[pid] = max(counters.get(pid, 0), data[2])
                    observation["observed_tree_cpu_seconds"] = sum(counters.values())
            except (OSError, subprocess.SubprocessError, ValueError):
                pass
            stopped.wait(0.1)

    monitor = threading.Thread(target=sample, daemon=True)
    monitor.start()
    try:
        output, error = process.communicate(json.dumps(value), timeout=45)
        return process.returncode, output, error, observation
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=6)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        stopped.set()
        monitor.join(3)


def worker():
    value = json.loads(sys.stdin.read())
    with tempfile.TemporaryDirectory(prefix="iatest-ui-bench-worker-") as directory:
        root = Path(directory)
        init_project(root, "benchmark")
        plan = value["plan"]
        evidence = []
        start, cpu = time.monotonic(), time.process_time()
        browser = BrowserSession(
            root, "benchmark", plan["spec"], RunControl(timeout_seconds=30)
        )
        try:
            for step in plan["spec"]["steps"]:
                browser.select_context(step["target"])
                evidence.append(
                    browser.evaluate(
                        "JSON.stringify({html:document.documentElement.outerHTML,open_shadow:Array.from(document.querySelectorAll('*')).filter(e=>e.shadowRoot).map(e=>({host:e.id,html:e.shadowRoot.innerHTML}))})"
                    )
                    if value["strategy"] == "native-full"
                    else json.dumps(
                        semantic_state(browser.query(step["target"])),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                if step["op"] in {"click", "fill"}:
                    browser.act(step)
                else:
                    deadline = time.monotonic() + 2
                    while True:
                        passed, _ = browser.assertion(step)
                        if passed:
                            break
                        if time.monotonic() > deadline:
                            raise ValueError("Business postcondition mismatch")
                        time.sleep(0.03)
            print(
                json.dumps(
                    {
                        "passed": True,
                        "wall_ms": (time.monotonic() - start) * 1000,
                        "host_cpu_seconds": time.process_time() - cpu,
                        "evidence": evidence,
                        "model_calls": 0,
                        "protocol_bytes": browser.cdp.sent + browser.cdp.received,
                    }
                )
            )
        finally:
            browser.close()


def fixtures(count):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            if self.path.startswith("/frame"):
                html = "<input id=entry><button id=save onclick=\"document.querySelector('#status').textContent=document.querySelector('#entry').value\">Save</button><p id=status>Ready</p>"
            else:
                noise = "".join(
                    f"<p data-noise='{i}'>Unrelated catalogue entry {i}</p>"
                    for i in range(300)
                )
                html = (
                    """<title>Evidence benchmark</title><section id=region>
                <label for=entry>Fixture field</label><input id=entry>
                <button id=save aria-label=Save onclick="document.querySelector('#status').textContent=document.querySelector('#entry').value">Save</button><p id=status>Ready</p>
                <input id=toggle type=checkbox onchange="document.querySelector('#checked').textContent=this.checked?'On':'Off'"><p id=checked>Off</p>
                <div id=shadow></div><iframe id=frame src=/frame></iframe></section>
                <script>const s=document.querySelector('#shadow').attachShadow({mode:'open'});s.innerHTML='<input id=shadow-entry><button id=shadow-save>Save</button><p id=shadow-status>Ready</p>';s.querySelector('#shadow-save').onclick=()=>s.querySelector('#shadow-status').textContent=s.querySelector('#shadow-entry').value;</script>"""
                    + noise
                )
            data = ("<html><body>" + html + "</body></html>").encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    tasks = []
    for index in range(count):
        mode = index % 5
        context = (
            {"shadow": ["#shadow"]}
            if mode == 1
            else {"frame": [{"selector": "#frame", "origin": base}]}
            if mode == 2
            else {"scope": "#region"}
            if mode == 3
            else {}
        )
        target = lambda name, context=context, mode=mode: {
            **context,
            "id": ("shadow-" if mode == 1 else "") + name,
        }
        expected = f"fixture-{index}"
        steps = [
            {"op": "fill", "target": target("entry"), "value": expected},
            {"op": "click", "target": target("save")},
            {
                "op": "expect",
                "target": target("status"),
                "assert": "text",
                "expected": expected,
            },
        ]
        if mode == 4:
            steps = [
                {"op": "click", "target": {"id": "toggle"}},
                {
                    "op": "expect",
                    "target": {"id": "checked"},
                    "assert": "text",
                    "expected": "On",
                },
            ]
        tasks.append(
            {
                "id": "fixture-" + str(index),
                "kind": "web",
                "spec": {
                    "start_url": base + "/case/" + str(index),
                    "allowed_origins": [base],
                    "steps": steps,
                },
            }
        )
    return tasks, server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--browser", default=os.environ.get("IATEST_CHROMIUM_PATH", ""))
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--tasks", type=int, default=40)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--output",
        type=Path,
        required=False,
        default=Path("/private/tmp/iatest-ui-benchmark.json"),
    )
    args = parser.parse_args()
    if args.worker:
        worker()
        return
    if not args.browser or not 1 <= args.tasks <= 100 or not 3 <= args.repeats <= 10:
        raise ValueError("Provide Chromium, 1–100 fixtures and 3–10 repetitions")
    os.environ["IATEST_CHROMIUM_PATH"] = args.browser
    server = None
    tasks, server = (
        (json.loads(args.manifest.read_text()), None)
        if args.manifest
        else fixtures(args.tasks)
    )
    try:
        if not isinstance(tasks, list) or not 1 <= len(tasks) <= 100:
            raise ValueError("Benchmark manifest requires 1–100 explicit plans")
        for plan in tasks:
            validate_plan(ExecutionPlan.from_dict(plan))
        node = shutil.which("node")
        if not node:
            raise ValueError("Development-only Node/Playwright baseline is required")
        env = dict(os.environ)
        env.setdefault(
            "NODE_PATH", str(Path(node).resolve().parents[1] / "lib/node_modules")
        )
        jobs = [
            (round_id, strategy, plan)
            for round_id in range(args.repeats)
            for strategy in ("native-full", "native-targeted", "playwright-targeted")
            for plan in tasks
        ]
        random.Random(args.seed).shuffle(jobs)
        rows = []
        for index, (round_id, strategy, plan) in enumerate(jobs):
            command = (
                [node, str(Path(__file__).with_suffix(".cjs"))]
                if strategy == "playwright-targeted"
                else [sys.executable, str(Path(__file__)), "--worker"]
            )
            value = {"browser": args.browser, "strategy": strategy, "plan": plan}
            exit_code, output, error, resources = monitored_run(command, value, env)
            if exit_code:
                raise RuntimeError(
                    f"Benchmark failed for {strategy}/{plan['id']}: " + error[-1000:]
                )
            row = json.loads(output)
            row.update(resources)
            evidence = row.pop("evidence")
            row.update(
                round=round_id,
                strategy=strategy,
                task_id=plan["id"],
                evidence_chars=sum(len(s) for s in evidence),
                estimated_evidence_tokens=sum(estimate_tokens(s) for s in evidence),
            )
            rows.append(row)
            if index % 10 == 0:
                print(
                    f"Completed {index + 1}/{len(jobs)} interleaved trials", flush=True
                )
        summary = {
            strategy: {
                "trials": len(
                    selected := [r for r in rows if r["strategy"] == strategy]
                ),
                "passed": all(r["passed"] for r in selected),
                "median_wall_ms": statistics.median(r["wall_ms"] for r in selected),
                "median_host_cpu_seconds": statistics.median(
                    r["host_cpu_seconds"] for r in selected
                ),
                "mean_estimated_evidence_tokens": statistics.mean(
                    r["estimated_evidence_tokens"] for r in selected
                ),
            }
            for strategy in ("native-full", "native-targeted", "playwright-targeted")
        }
        report = {
            "passed": all(r["passed"] for r in rows),
            "tasks": len(tasks),
            "repeats": args.repeats,
            "seed": args.seed,
            "scope": "caller manifest"
            if args.manifest
            else "controlled synthetic variants across five interaction patterns; not forty independent real business processes",
            "token_accounting": "character-based evidence estimates; all model calls are zero, so no claimed model token saving",
            "resource_accounting": "100ms ps process-tree samples include host and descendant browsers; sampled RSS is not an absolute peak, observed CPU omits exited-process tails and may be a lower bound. Background host load is uncontrolled.",
            "unmeasured": [
                "exact browser total CPU and absolute peak RSS",
                "Playwright protocol bytes",
                "real-model exploration comparison",
            ],
            "summary": summary,
            "trials": rows,
        }
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(summary))
    finally:
        if server:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
