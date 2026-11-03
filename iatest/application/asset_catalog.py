"""Project-scoped inventory of reusable inputs, cases and plans across test types."""

from __future__ import annotations

import json
from dataclasses import asdict

from iatest.application.test_assets import AssetRepository
from iatest.core.paths import project_docs_file
from iatest.core.serde import read_json
from iatest.engines.data import redact_asset
from iatest.engines.store import EngineStore
from iatest.generator.cases import load_cases_from_markdown
from iatest.mobile.store import MobileStore

CATEGORIES = (
    "ui",
    "api",
    "performance",
    "security",
    "data",
    "accessibility",
    "automation",
)
PLAN_CATEGORIES = {
    "web": "ui",
    "desktop": "ui",
    "android": "ui",
    "ios": "ui",
    "api": "api",
    "performance": "performance",
    "client_performance": "performance",
    "security": "security",
    "data": "data",
    "protocol": "data",
    "accessibility": "accessibility",
    "workflow": "automation",
    "remote": "automation",
}
ARTIFACT_CATEGORIES = {
    "ui_case": ("ui", "goal_case"),
    "security_library": ("security", "rule_library"),
    "ios_measurement": ("performance", "measurement"),
    "ios_calibration": ("performance", "calibration"),
    "protobuf": ("data", "protocol_schema"),
    "accessibility_review": ("accessibility", "manual_review"),
    "campaign": ("automation", "release_suite"),
}


def catalog_assets(root, project):
    """Read existing stores without migrating, duplicating or executing assets."""
    assets = []

    def add(
        source,
        identifier,
        category,
        role,
        payload,
        *,
        state="saved",
        title="",
        kind="",
        created_at="",
    ):
        value = redact_asset(payload)
        assets.append(
            {
                "key": source + ":" + str(identifier),
                "source": source,
                "id": str(identifier),
                "category": category,
                "role": role,
                "kind": kind or value.get("kind", ""),
                "title": title
                or value.get("title")
                or value.get("goal")
                or str(identifier),
                "state": state,
                "payload": value,
                "created_at": created_at,
            }
        )

    for endpoint in read_json(project_docs_file(root, project), []):
        identity = endpoint["method"].upper() + " " + endpoint["path"]
        add(
            "api-contract",
            identity,
            "api",
            "contract",
            endpoint,
            title=endpoint.get("summary") or identity,
        )
    for case in load_cases_from_markdown(root, project):
        add(
            "api-case",
            case.id,
            "api",
            "case",
            asdict(case),
            title=case.title,
            state="ready" if case.runnable else "needs_review",
        )
    engine = EngineStore(root, project)
    for plan in engine.plans():
        add(
            "engine-plan",
            plan["id"],
            PLAN_CATEGORIES[plan["kind"]],
            "plan",
            plan,
            state="ready",
        )
    for plan in MobileStore(root, project).list("plan"):
        add("mobile-plan", plan["id"], "ui", "plan", plan, state="ready")
    for case in AssetRepository(root, project).list():
        add(
            "business-case",
            case["case_id"],
            "ui",
            "business_case",
            case,
            state=case.get("state", "saved"),
            kind=case.get("platform", ""),
        )
    # No per-kind 200-row cap: filtering/pagination must not hide older project assets.
    with engine.connection() as connection:
        placeholders = ",".join("?" for _ in ARTIFACT_CATEGORIES)
        for kind, identifier, created_at, content in connection.execute(
            f"select kind,id,created_at,content from artifacts where kind in ({placeholders}) order by created_at desc,id",
            tuple(ARTIFACT_CATEGORIES),
        ):
            value = json.loads(content)
            category, role = ARTIFACT_CATEGORIES[kind]
            add(
                "artifact-" + kind,
                identifier,
                category,
                role,
                value,
                state=value.get("status", "saved"),
                created_at=created_at,
            )
    return assets


def filter_assets(assets, *, category="", role="", query="", plans_only=False):
    query = query.casefold().strip()
    return [
        a
        for a in assets
        if (not category or a["category"] == category)
        and (not role or a["role"] == role)
        and (not plans_only or a["role"] == "plan")
        and (
            not query
            or query
            in " ".join(
                str(a[k]) for k in ("id", "title", "category", "role", "kind")
            ).casefold()
        )
    ]
