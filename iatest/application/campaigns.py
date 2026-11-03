"""Immutable version-test manifests and explicit, leased campaign attempts."""

from __future__ import annotations

import copy
import uuid
from contextlib import ExitStack

from iatest.application.engine_analysis import digest, run_context
from iatest.application.testing_service import execute_test_plan
from iatest.core.resources import resource_scope
from iatest.engines.contracts import EngineResult, ExecutionPlan
from iatest.engines.http import validate
from iatest.engines.store import EngineStore
from iatest.engines.validation import validate_plan
from iatest.execution.control import RunControl
from iatest.mobile.lease import device_scope
from iatest.mobile.store import MobileStore

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "build", "nodes"],
    "properties": {
        "id": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,60}$"},
        "build": {"type": "object", "minProperties": 1},
        "max_parallel": {"type": "integer", "minimum": 1, "maximum": 32},
        "resource_wait_seconds": {"type": "number", "minimum": 0, "maximum": 3600},
        "resources": {
            "type": "array",
            "uniqueItems": True,
            "maxItems": 50,
            "items": {"type": "string", "minLength": 1, "maxLength": 200},
        },
        "budget": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "timeout_seconds": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "maximum": 3600,
                },
                "max_requests": {"type": "integer", "minimum": 1, "maximum": 100000},
            },
        },
        "nodes": {
            "type": "array",
            "minItems": 1,
            "maxItems": 100,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id"],
                "properties": {
                    "id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,60}$"},
                    "plan_id": {"type": "string"},
                    "case_id": {"type": "string"},
                    "case_version": {"type": "integer", "minimum": 1},
                    "node_id": {"type": "string"},
                    "depends_on": {
                        "type": "array",
                        "uniqueItems": True,
                        "items": {"type": "string"},
                    },
                    "always": {"type": "boolean"},
                    "resources": {
                        "type": "array",
                        "uniqueItems": True,
                        "maxItems": 50,
                        "items": {"type": "string", "minLength": 1, "maxLength": 200},
                    },
                },
                "oneOf": [
                    {"required": ["plan_id"], "not": {"required": ["case_id"]}},
                    {"required": ["case_id"], "not": {"required": ["plan_id"]}},
                ],
            },
        },
    },
}


def local_profiles(value):
    if value["kind"] == "remote":
        return
    if value.get("preparation", {}).get("app"):
        yield value["preparation"]["app"]["profile"]
    if value["kind"] in {"android", "ios"}:
        yield value["spec"]["profile"]
    if value["kind"] == "client_performance" and "plan" in value["spec"]:
        yield from local_profiles(value["spec"]["plan"])
    if value["kind"] == "workflow":
        for node in value["spec"]["nodes"]:
            yield from local_profiles(node["plan"])


def create_campaign(root, project, spec):
    from iatest.application.test_assets import AssetRepository
    from iatest.nodes.store import plan_tree, require_credential_references

    validate(spec, SCHEMA)
    store = EngineStore(root, project)
    nodes, cases, builds = [], [], {}
    for node in spec["nodes"]:
        if node.get("case_id"):
            case = AssetRepository(root, project).get(
                node["case_id"], node.get("case_version")
            )
            if not case.get("plan"):
                raise ValueError(
                    "Explore and compile the case before adding it to a campaign"
                )
            plan = copy.deepcopy(case["plan"])
            cases.append(
                {
                    "case_id": case["case_id"],
                    "version": case["version"],
                    "digest": digest(plan),
                }
            )
        else:
            if "case_version" in node:
                raise ValueError("case_version requires case_id")
            plan = store.get_plan(node["plan_id"])
        require_credential_references(plan)
        for child in plan_tree(plan):
            preparation = child.get("preparation", {}).get("app", {})
            for key in ("artifact_id", "base_artifact_id"):
                if preparation.get(key):
                    artifact = store.get_artifact("app", preparation[key])
                    builds[artifact["id"]] = artifact["sha256"]
        if node.get("node_id"):
            plan = {
                "id": node["id"] + "-remote",
                "kind": "remote",
                "budget": plan.get("budget", {}),
                "spec": {"node_id": node["node_id"], "plan": plan},
            }
        nodes.append(
            {
                k: copy.deepcopy(v)
                for k, v in node.items()
                if k in {"id", "depends_on", "always", "resources"}
            }
            | {"plan": plan}
        )
    workflow = {
        "id": spec["id"],
        "kind": "workflow",
        "spec": {"nodes": nodes, "max_parallel": spec.get("max_parallel", 1)},
        "budget": spec.get("budget", {"timeout_seconds": 600, "max_requests": 10000}),
    }
    validate_plan(ExecutionPlan.from_dict(workflow))
    profiles = {
        name: MobileStore(root, project).get("profile", name)
        for name in set(local_profiles(workflow))
    }
    manifest = {
        "contract_version": 1,
        "build": spec["build"],
        "resources": spec.get("resources", []),
        "resource_wait_seconds": spec.get("resource_wait_seconds", 0),
        "workflow": workflow,
        "cases": cases,
        "app_builds": builds,
        "profiles": profiles,
        "source_digest": digest(spec),
    }
    manifest["digest"] = digest(manifest)
    manifest["id"] = store.save_artifact("campaign", manifest, artifact_id=spec["id"])
    return manifest


def failure_category(result):
    if result["verdict"] == "passed":
        return "passed"
    if result["verdict"] == "skipped":
        return "dependency_blocked"
    if result.get("metrics", {}).get("failure_category"):
        return result["metrics"]["failure_category"]
    if result["lifecycle"] == "environment_error":
        return "environment"
    if result["lifecycle"] != "completed":
        return "execution_incomplete"
    if result["kind"] == "remote" and result.get("steps"):
        return failure_category(result["steps"][0]["result"])
    if result["kind"] == "performance":
        if result["metrics"].get("dropped"):
            return "offered_load"
        return "performance_gate" if result["verdict"] == "failed" else "unassessed"
    if result["kind"] == "client_performance":
        return "unassessed" if result["metrics"].get("not_assessed") else "client_gate"
    if result["kind"] == "security":
        return "security_control" if result["verdict"] == "failed" else "unassessed"
    return "business_assertion" if result["verdict"] == "failed" else "unassessed"


def run_campaign(
    root, project, campaign_id, *, control=None, analyze=False, progress=None
):
    store = EngineStore(root, project)
    manifest = store.get_artifact("campaign", campaign_id)
    attempt = {
        "campaign_id": campaign_id,
        "manifest_digest": manifest["digest"],
        "build": manifest["build"],
        "state": "running",
        "run_id": "",
        "gates": [],
        "analyses": [],
    }
    attempt_id = "campaign-run-" + uuid.uuid4().hex[:20]
    store.save_artifact("campaign_run", attempt, artifact_id=attempt_id)
    try:
        budget = manifest["workflow"]["budget"]
        control = control or RunControl(
            timeout_seconds=budget.get("timeout_seconds", 600),
            max_requests=budget.get("max_requests", 10000),
        )
        with ExitStack() as stack:
            stack.enter_context(
                resource_scope(
                    manifest["resources"],
                    control,
                    wait_seconds=manifest.get("resource_wait_seconds", 0),
                    reserve=True,
                )
            )
            profiles = manifest["profiles"]
            for name in sorted(profiles):
                actual = MobileStore(root, project).get("profile", name)
                if digest(actual) != digest(profiles[name]):
                    raise ValueError(
                        "Device profile changed after the campaign snapshot"
                    )
                stack.enter_context(device_scope(actual))
            for artifact_id, sha in manifest["app_builds"].items():
                if store.get_artifact("app", artifact_id)["sha256"] != sha:
                    raise ValueError("App build changed after the campaign snapshot")
            if progress:
                progress("Executing frozen campaign plans", stage="execute")
            workflow = copy.deepcopy(manifest["workflow"])
            from iatest.core.config import load_config

            if load_config(root).get("resource_coordinator"):
                workflow["resources"] = sorted(
                    set(workflow.get("resources", [])) | set(manifest["resources"])
                )
            result = execute_test_plan(root, project, workflow, control)
            attempt.update(
                state="completed",
                run_id=result["run_id"],
                verdict=result["verdict"],
                lifecycle=result["lifecycle"],
            )
            for step in result["steps"]:
                child = step["result"]
                value = EngineResult(**child)
                store.save_result(value)
                node = next(
                    n
                    for n in manifest["workflow"]["spec"]["nodes"]
                    if n["id"] == step["node"]
                )
                store.save_artifact(
                    "run_context",
                    run_context(root, ExecutionPlan.from_dict(node["plan"])),
                    artifact_id="ctx-" + value.run_id,
                    replace=True,
                )
                attempt["gates"].append(
                    {
                        "node": step["node"],
                        "run_id": value.run_id,
                        "kind": child["kind"],
                        "verdict": child["verdict"],
                        "category": failure_category(child),
                    }
                )
    except Exception as exc:  # noqa: BLE001 -- persist an uncertain attempt, never auto replay it
        attempt.update(
            state="environment_error", verdict="inconclusive", error=str(exc)
        )
    store.save_artifact("campaign_run", attempt, artifact_id=attempt_id, replace=True)
    if analyze and attempt["state"] == "completed":
        from iatest.application.engine_analysis import analyze_engine_run

        for gate in attempt["gates"]:
            if gate["kind"] in {
                "performance",
                "security",
                "client_performance",
                "data",
                "protocol",
                "accessibility",
            }:
                try:
                    if progress:
                        progress("Analyzing immutable campaign evidence", stage="llm")
                    analysis = analyze_engine_run(root, project, gate["run_id"])
                    attempt["analyses"].append(
                        {"run_id": gate["run_id"], "analysis_id": analysis["id"]}
                    )
                except Exception as exc:  # noqa: BLE001 -- analysis cannot replace facts or fail cleanup
                    attempt["analyses"].append(
                        {"run_id": gate["run_id"], "error": str(exc)}
                    )
        store.save_artifact(
            "campaign_run", attempt, artifact_id=attempt_id, replace=True
        )
    return {**attempt, "id": attempt_id}
