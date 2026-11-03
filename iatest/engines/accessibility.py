"""WCAG 2.2 checklist, bounded automated findings and explicit human review."""

from __future__ import annotations

import json
from pathlib import Path

from iatest.application.engine_analysis import digest
from iatest.engines.contracts import EngineResult
from iatest.engines.http import validate
from iatest.engines.store import EngineStore
from iatest.engines.web import BrowserSession

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["allowed_origins", "start_url", "audit"],
    "properties": {
        "allowed_origins": {
            "type": "array",
            "minItems": 1,
            "maxItems": 32,
            "items": {"type": "string"},
        },
        "start_url": {"type": "string", "maxLength": 8192},
        "audit": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "level": {"enum": ["A", "AA", "AAA"]},
                "max_nodes": {"type": "integer", "minimum": 1, "maximum": 5000},
                "review_artifact": {"type": "string"},
                "review_digest": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            },
        },
    },
}


def criteria():
    return json.loads(Path(__file__).with_name("wcag22.json").read_text())["criteria"]


def validate_audit(plan):
    validate(plan.spec, SCHEMA)
    if bool(plan.spec["audit"].get("review_artifact")) != bool(
        plan.spec["audit"].get("review_digest")
    ):
        raise ValueError("Manual review requires its immutable artifact and digest")


def import_review(root, project, value):
    allowed = {c["id"] for c in criteria() if not c["obsolete"]}
    if (
        not isinstance(value, dict)
        or set(value) != {"id", "start_url", "build_sha256", "reviews"}
        or not isinstance(value["reviews"], list)
        or len(value["reviews"]) > 86
    ):
        raise ValueError("Review must bind URL/build digest and bounded entries")
    if len(value["build_sha256"]) != 64 or any(
        c not in "0123456789abcdef" for c in value["build_sha256"]
    ):
        raise ValueError("Review build digest is invalid")
    seen = set()
    for row in value["reviews"]:
        if (
            not isinstance(row, dict)
            or set(row)
            != {"criterion", "status", "reviewer", "evidence_sha256", "notes"}
            or row["criterion"] not in allowed
            or row["criterion"] in seen
        ):
            raise ValueError("Unknown/duplicate criterion or missing evidence")
        if (
            row["status"] not in {"passed", "failed", "not_applicable"}
            or not isinstance(row["reviewer"], str)
            or not row["reviewer"].strip()
            or not isinstance(row["notes"], str)
            or not 10 <= len(row["notes"]) <= 2000
        ):
            raise ValueError("Human reviewer, decision and rationale are required")
        if len(row["evidence_sha256"]) != 64 or any(
            c not in "0123456789abcdef" for c in row["evidence_sha256"]
        ):
            raise ValueError("Manual evidence needs a SHA-256")
        seen.add(row["criterion"])
    value = {
        **value,
        "sha256": digest(value),
        "trust": "explicit human review; never an AI automatic pass",
    }
    EngineStore(root, project).save_artifact(
        "accessibility_review", value, artifact_id=value["id"]
    )
    return value


class AccessibilityAudit:
    def __init__(self, root, project):
        self.root, self.project = root, project

    def execute(self, plan, context):
        validate_audit(plan)
        spec = plan.spec
        options = spec["audit"]
        browser = None
        result = EngineResult(plan.id, plan.kind)
        try:
            browser = BrowserSession(
                self.root,
                self.project,
                {
                    "allowed_origins": spec["allowed_origins"],
                    "start_url": spec["start_url"],
                },
                context.control,
            )
            audit = browser.evaluate(
                f"({Path(__file__).with_name('a11y_audit.js').read_text()})({json.dumps({'max_nodes': options.get('max_nodes', 5000)})})"
            )
            build = digest(
                browser.evaluate(
                    "(()=>{const value=document.documentElement.outerHTML;if(value.length>1048576)throw Error('Review document digest byte budget exceeded');return value})()"
                )
            )
            reviews = {}
            if options.get("review_artifact"):
                frozen = EngineStore(self.root, self.project).get_artifact(
                    "accessibility_review", options["review_artifact"]
                )
                if (
                    frozen["sha256"] != options["review_digest"]
                    or digest(
                        {
                            k: v
                            for k, v in frozen.items()
                            if k not in {"sha256", "trust"}
                        }
                    )
                    != frozen["sha256"]
                    or frozen["start_url"] != spec["start_url"]
                    or frozen["build_sha256"] != build
                ):
                    raise ValueError(
                        "Manual review differs from current URL/build or digest"
                    )
                reviews = {r["criterion"]: r for r in frozen["reviews"]}
            failures = {f["criterion"] for f in audit["findings"]}
            required = [
                c
                for c in criteria()
                if not c["obsolete"]
                and len(c["level"]) <= len(options.get("level", "AA"))
            ]
            for criterion in required:
                cid = criterion["id"]
                review = reviews.get(cid)
                status = (
                    "failed"
                    if cid in failures
                    else review["status"]
                    if review
                    else "needs_review"
                )
                result.steps.append(
                    {
                        "criterion": cid,
                        "name": criterion["name"],
                        "level": criterion["level"],
                        "status": status,
                        "reviewer": review["reviewer"] if review else None,
                    }
                )
            pending = sum(r["status"] == "needs_review" for r in result.steps)
            result.verdict = (
                "failed"
                if failures or any(r["status"] == "failed" for r in result.steps)
                else "inconclusive"
                if pending or not audit["complete"]
                else "passed"
            )
            result.metrics = {
                "wcag": "2.2",
                "level": options.get("level", "AA"),
                "criteria": len(required),
                "needs_review": pending,
                "build_sha256": build,
                "automated_complete": audit["complete"],
                "visited_nodes": audit["visited_nodes"],
                "findings": audit["findings"],
                "manual_hints": audit["manual_hints"],
            }
            result.limitations = [
                "Automated findings do not prove WCAG; outstanding manual criteria block conformance"
            ]
        finally:
            if browser:
                browser.close()
        return result
