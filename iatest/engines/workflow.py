from __future__ import annotations

import copy
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextvars import ContextVar, copy_context
from dataclasses import asdict

from iatest.core.resources import resource_scope
from iatest.engines.contracts import (
    EngineContext,
    EngineResult,
    ExecutionPlan,
    PlanControl,
)
from iatest.engines.http import validate
from iatest.execution.control import RunCancelled, RunControl

_CLEANUP = ContextVar("iatest_nested_cleanup", default=None)

WORKFLOW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["nodes"],
    "properties": {
        "max_parallel": {"type": "integer", "minimum": 1, "maximum": 32},
        "resource_wait_seconds": {"type": "number", "minimum": 0, "maximum": 3600},
        "cleanup_max_requests": {"type": "integer", "minimum": 1, "maximum": 1000},
        "exports": {
            "type": "object",
            "maxProperties": 50,
            "additionalProperties": {
                "type": "object",
                "additionalProperties": False,
                "required": ["node", "output", "type", "sensitive"],
                "properties": {
                    "node": {"type": "string"},
                    "output": {"type": "string"},
                    "type": {
                        "enum": [
                            "string",
                            "integer",
                            "number",
                            "boolean",
                            "object",
                            "array",
                        ]
                    },
                    "sensitive": {"type": "boolean"},
                },
            },
        },
        "nodes": {
            "type": "array",
            "minItems": 1,
            "maxItems": 100,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "plan"],
                "properties": {
                    "id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,80}$"},
                    "depends_on": {
                        "type": "array",
                        "uniqueItems": True,
                        "items": {"type": "string"},
                    },
                    "always": {"type": "boolean"},
                    "resource_owner": {"type": "string", "minLength": 1},
                    "resources": {
                        "type": "array",
                        "maxItems": 50,
                        "uniqueItems": True,
                        "items": {"type": "string", "minLength": 1, "maxLength": 200},
                    },
                    "plan": {"type": "object"},
                },
            },
        },
    },
}
TYPE_EXAMPLES = {
    "string": "reference",
    "integer": 1,
    "number": 1.0,
    "boolean": True,
    "object": {},
    "array": [],
}


def references(value):
    if isinstance(value, dict):
        if "$from" in value:
            if (
                set(value) != {"$from", "type", "sensitive"}
                or value["type"] not in TYPE_EXAMPLES
                or type(value["sensitive"]) is not bool
                or not isinstance(value["$from"], str)
                or "." not in value["$from"]
            ):
                raise ValueError(
                    "References require $from=node.output, type and sensitive"
                )
            yield value
        else:
            scoped = (
                {
                    **value,
                    "spec": {
                        k: v
                        for k, v in value["spec"].items()
                        if k not in {"nodes", "exports"}
                    },
                }
                if value.get("kind") == "workflow" and "spec" in value
                else value
            )
            for child in scoped.values():
                yield from references(child)
    elif isinstance(value, list):
        for child in value:
            yield from references(child)


def resolve(value, outputs, *, examples=False):
    if isinstance(value, dict):
        if "$from" in value:
            if examples:
                return copy.deepcopy(TYPE_EXAMPLES[value["type"]])
            node, key = value["$from"].split(".", 1)
            if node not in outputs or key not in outputs[node]:
                raise ValueError("Required predecessor output is unavailable")
            output = outputs[node][key]
            if (
                output["type"] != value["type"]
                or output["sensitive"] != value["sensitive"]
            ):
                raise ValueError("Reference type or sensitivity mismatch")
            return copy.deepcopy(output["value"])
        if value.get("kind") == "workflow" and "spec" in value:
            return {
                k: (
                    {
                        n: copy.deepcopy(vv)
                        if n in {"nodes", "exports"}
                        else resolve(vv, outputs, examples=examples)
                        for n, vv in v.items()
                    }
                    if k == "spec"
                    else resolve(v, outputs, examples=examples)
                )
                for k, v in value.items()
            }
        return {k: resolve(v, outputs, examples=examples) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, outputs, examples=examples) for v in value]
    return value


def validate_workflow(plan):
    from iatest.engines.validation import validate_plan

    validate(plan.spec, WORKFLOW_SCHEMA)
    total = [0]

    def shape(value, depth=0):
        if depth > 5:
            raise ValueError("Workflow nesting exceeds six levels")
        if value.get("kind") != "workflow":
            return
        for node in value["spec"]["nodes"]:
            total[0] += 1
            if total[0] > 500:
                raise ValueError("Nested workflow exceeds 500 total nodes")
            shape(node["plan"], depth + 1)

    shape(asdict(plan))
    nodes = {n["id"]: n for n in plan.spec["nodes"]}
    if len(nodes) != len(plan.spec["nodes"]):
        raise ValueError("Duplicate workflow node ID")
    order, ancestors = [], {}
    remaining = dict(nodes)
    while remaining:
        ready = [
            n for n in remaining.values() if set(n.get("depends_on", [])) <= set(order)
        ]
        if not ready:
            raise ValueError("Cyclic or missing workflow dependency")
        for node in ready:
            key = node["id"]
            ancestors[key] = set(node.get("depends_on", []))
            for dependency in node.get("depends_on", []):
                ancestors[key].update(ancestors[dependency])
            if not node.get("always") and any(
                nodes[a].get("always") for a in ancestors[key]
            ):
                raise ValueError("A normal node cannot depend on final cleanup")
            if node.get("resource_owner", key) not in ancestors[key] | {key}:
                raise ValueError("Resource owner must be this node or a predecessor")
            for ref in references(node["plan"]):
                source, output_name = ref["$from"].split(".", 1)
                if source not in ancestors[key]:
                    raise ValueError("Data reference must name a declared predecessor")
                definition = (
                    nodes[source]["plan"]
                    .get("spec", {})
                    .get("exports", {})
                    .get(output_name)
                )
                if (
                    not definition
                    or definition.get("type") != ref["type"]
                    or definition.get("sensitive", False) != ref["sensitive"]
                ):
                    raise ValueError(
                        "Output reference lacks a matching type/sensitivity declaration"
                    )
            child = ExecutionPlan.from_dict(resolve(node["plan"], {}, examples=True))
            validate_plan(child)
            order.append(key)
            del remaining[key]
    for binding in plan.spec.get("exports", {}).values():
        source = (
            nodes.get(binding["node"], {})
            .get("plan", {})
            .get("spec", {})
            .get("exports", {})
            .get(binding["output"])
        )
        if (
            not source
            or source.get("type") != binding["type"]
            or source.get("sensitive", False) != binding["sensitive"]
        ):
            raise ValueError(
                "Workflow export requires a declared matching child output"
            )
    return [nodes[k] for k in order]


class WorkflowEngine:
    def __init__(self, root, project, registry):
        self.root, self.project, self.registry = root, project, registry

    def capabilities(self):
        return {
            "available": True,
            "kind": "workflow",
            "scheduling": "bounded-parallel-topological",
            "features": [
                "typed outputs",
                "failure propagation",
                "bounded always cleanup",
                "shared request budget",
                "cancellable resource reservations",
                "nested workflows and typed exports",
            ],
            "limitations": [
                "No automatic replay of interrupted writes; independent controllers require the configured shared authority"
            ],
        }

    def execute(self, plan: ExecutionPlan, context: EngineContext) -> EngineResult:
        holder = _CLEANUP.get() or {
            "control": None,
            "lock": threading.Lock(),
            "max_requests": plan.spec.get("cleanup_max_requests", 100),
        }
        token = _CLEANUP.set(holder)
        try:
            return self._execute(plan, context, holder)
        finally:
            _CLEANUP.reset(token)

    def _execute(self, plan, context, cleanup_holder):
        nodes = validate_workflow(plan)
        from iatest.application.campaigns import local_profiles
        from iatest.mobile.store import MobileStore

        # Aliases of one device and repeated remote nodes are serialized automatically.
        nodes = copy.deepcopy(nodes)
        for node in nodes:
            resources = set(node.get("resources", []))
            from iatest.nodes.store import plan_tree

            for nested in plan_tree(node["plan"]):
                if nested["kind"] == "desktop":
                    resources.add(
                        f"desktop:{nested['spec']['platform']}:{nested['spec']['process_id']}"
                    )
                if nested["kind"] == "workflow":
                    for branch in nested["spec"]["nodes"]:
                        resources.update(branch.get("resources", []))
            for name in local_profiles(node["plan"]):
                profile = MobileStore(self.root, self.project).get("profile", name)
                resources.add(
                    "device:" + profile["platform"] + ":" + profile["device_id"]
                )
            selected = []
            for remote in plan_tree(node["plan"]):
                if remote["kind"] == "remote":
                    selected.append(remote["spec"]["node_id"])
                elif remote["kind"] == "performance":
                    selected.extend(
                        remote["spec"].get("distribution", {}).get("nodes", [])
                    )
            for node_id in selected:
                resources.add(
                    "node:"
                    + str(self.root.resolve())
                    + ":"
                    + self.project
                    + ":"
                    + node_id
                )
            node["resources"] = sorted(resources)
        result = EngineResult(plan.id, "workflow")
        completed, outputs = {}, {}
        cleanup_control = None
        request_start = context.control.request_count()
        maximum = plan.spec.get("max_parallel", 1)
        wait_seconds = plan.spec.get("resource_wait_seconds", 30)
        peak, resource_wait = 0, 0.0

        def execute_node(node, snapshot, blocked, cleanup=False):
            name = node["id"]
            try:
                if not cleanup:
                    context.control.check()
                    if blocked:
                        raise ValueError(
                            "Blocked by failed/inconclusive predecessors: "
                            + ",".join(blocked)
                        )
                child = ExecutionPlan.from_dict(resolve(node["plan"], snapshot))
                parent = cleanup_control if cleanup else context.control
                control = PlanControl(
                    parent,
                    timeout_seconds=child.budget.get(
                        "timeout_seconds", parent.remaining()
                    ),
                    max_requests=child.budget.get(
                        "max_requests", 100 if cleanup else 10000
                    ),
                )
                with resource_scope(
                    node.get("resources", []),
                    control,
                    wait_seconds=wait_seconds,
                    reserve=child.kind == "workflow",
                ) as lease:
                    value = self.registry.execute(child, EngineContext(control))
                value.metrics["resource_wait_seconds"] = lease["wait_seconds"]
            except RunCancelled as exc:
                value = EngineResult(
                    node["plan"].get("id", name),
                    node["plan"].get("kind", ""),
                    lifecycle="cancelled",
                    verdict="inconclusive",
                    error=str(exc),
                )
            except Exception as exc:  # noqa: BLE001 -- report execution failure while retaining cleanup
                value = EngineResult(
                    node["plan"].get("id", name),
                    node["plan"].get("kind", ""),
                    lifecycle="error",
                    verdict="skipped" if blocked and not cleanup else "inconclusive",
                    error=str(exc),
                )
            return value

        pending = {n["id"]: n for n in nodes if not n.get("always")}
        running = {}
        # The scheduler alone owns dependency/output state; workers get immutable snapshots.
        with ThreadPoolExecutor(
            max_workers=maximum, thread_name_prefix="iatest-dag"
        ) as pool:
            while pending or running:
                busy = {r for n in running.values() for r in n.get("resources", [])}
                for name, node in list(pending.items()):
                    if len(running) >= maximum:
                        break
                    if not set(node.get("depends_on", [])) <= completed.keys():
                        continue
                    resources = set(node.get("resources", []))
                    if resources & busy:
                        continue
                    blocked = [
                        d
                        for d in node.get("depends_on", [])
                        if completed[d].verdict != "passed"
                    ]
                    future = pool.submit(
                        copy_context().run,
                        execute_node,
                        node,
                        copy.deepcopy(outputs),
                        blocked,
                    )
                    running[future] = node
                    del pending[name]
                    busy |= resources
                    peak = max(peak, len(running))
                if not running:
                    raise RuntimeError("Workflow scheduler made no progress")
                done, _ = wait(running, timeout=0.05, return_when=FIRST_COMPLETED)
                for future in done:
                    node = running.pop(future)
                    value = future.result()
                    completed[node["id"]] = value
                    if value.verdict == "passed":
                        outputs[node["id"]] = value.outputs

        for node in nodes:
            if not node.get("always"):
                continue
            if cleanup_control is None:
                with cleanup_holder["lock"]:
                    if cleanup_holder["control"] is None:
                        cleanup_holder["control"] = RunControl(
                            timeout_seconds=15,
                            max_requests=cleanup_holder["max_requests"],
                        )
                    cleanup_control = PlanControl(
                        cleanup_holder["control"],
                        timeout_seconds=15,
                        max_requests=plan.spec.get("cleanup_max_requests", 100),
                    )
            value = execute_node(node, copy.deepcopy(outputs), [], cleanup=True)
            completed[node["id"]] = value
            if value.verdict == "passed":
                outputs[node["id"]] = value.outputs

        for node in nodes:
            name = node["id"]
            value = completed[name]
            resource_wait += value.metrics.get("resource_wait_seconds", 0)
            result.steps.append(
                {
                    "node": name,
                    "always": node.get("always", False),
                    "resource_owner": node.get("resource_owner", name),
                    "result": asdict(value),
                }
            )
            result.evidence.extend(
                {**item, "run_id": value.run_id, "node": name}
                for item in value.evidence
            )
        verdicts = {value.verdict for value in completed.values()}
        result.verdict = (
            "failed"
            if "failed" in verdicts
            else "passed"
            if verdicts == {"passed"}
            else "inconclusive"
        )
        if reason := context.control.reason():
            result.lifecycle = "timed_out" if "总时限" in reason else "cancelled"
            result.error = reason
            if result.verdict == "passed":
                result.verdict = "inconclusive"
        result.metrics = {
            "nodes": len(nodes),
            "requests": context.control.request_count()
            - request_start
            + (
                cleanup_holder["control"].request_count()
                if cleanup_holder["control"]
                else 0
            ),
            "cleanup_requests": cleanup_holder["control"].request_count()
            if cleanup_holder["control"]
            else 0,
            "cleanup_budget_seconds": 15,
            "max_parallel": maximum,
            "peak_parallel": peak,
            "resource_wait_seconds": resource_wait,
        }
        if result.verdict == "passed":
            for name, binding in plan.spec.get("exports", {}).items():
                output = outputs.get(binding["node"], {}).get(binding["output"])
                if (
                    output is None
                    or output["type"] != binding["type"]
                    or output["sensitive"] != binding["sensitive"]
                ):
                    raise ValueError(
                        "Workflow output is absent or its type/sensitivity changed"
                    )
                result.outputs[name] = copy.deepcopy(output)
        return result
