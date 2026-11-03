"""Versioned UI assets and deterministic, preview-first batch ingestion."""

from __future__ import annotations

import csv
import io
import json
import re
import uuid
import zipfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

from iatest.engines.contracts import ExecutionPlan
from iatest.engines.data import redact_asset
from iatest.engines.store import EngineStore, identifier
from iatest.engines.validation import validate_plan
from iatest.execution.control import ACTIVE_RUNTIME

COLUMNS = (
    "case_id",
    "title",
    "module",
    "platform",
    "goal",
    "expected",
    "preconditions",
    "path_hints",
    "required_steps",
    "context",
    "data_generation",
    "input_env",
    "plan",
    "tags",
    "priority",
)
JSON_FIELDS = {
    "context": {},
    "data_generation": {},
    "input_env": {},
    "required_steps": [],
    "plan": None,
}
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def now():
    return datetime.now(UTC).isoformat()


def template_rows(mode="goals"):
    if mode not in {"goals", "plans"}:
        raise ValueError("Choose goals or plans template")
    row = {k: "" for k in COLUMNS}
    row.update(
        case_id="nickname-persistence",
        title="Nickname persistence",
        platform="web",
        priority="P1",
    )
    if mode == "goals":
        row.update(
            goal="修改昵称并验证刷新后持久化",
            expected="保存并刷新后昵称等于 nickname",
            required_steps='["reload"]',
            data_generation=json.dumps(
                {
                    "nickname": {
                        "kind": "string",
                        "minLength": 16,
                        "maxLength": 20,
                        "unique": True,
                    }
                }
            ),
            context=json.dumps(
                {
                    "start_url": "http://127.0.0.1:8767/ui",
                    "allowed_origins": ["http://127.0.0.1:8767"],
                }
            ),
        )
    else:
        row["plan"] = json.dumps(
            {
                "id": "visible-heading",
                "kind": "web",
                "spec": {
                    "start_url": "http://127.0.0.1:8767/ui",
                    "allowed_origins": ["http://127.0.0.1:8767"],
                    "steps": [
                        {
                            "op": "expect",
                            "target": {"text": "Account"},
                            "assert": "exists",
                        }
                    ],
                },
            }
        )
    return [row]


def template_bytes(fmt="csv", mode="goals"):
    rows = template_rows(mode)
    if fmt == "json":
        return json.dumps(
            {
                "contract_version": 1,
                "cases": [
                    {
                        k: json.loads(v) if k in JSON_FIELDS and v else v
                        for k, v in rows[0].items()
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        ).encode()
    if fmt == "csv":
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
        return b"\xef\xbb\xbf" + output.getvalue().encode()
    if fmt != "xlsx":
        raise ValueError("Choose csv, json or xlsx")
    sheet = ET.Element(NS + "worksheet")
    data = ET.SubElement(sheet, NS + "sheetData")
    for i, values in enumerate([list(COLUMNS), [rows[0][k] for k in COLUMNS]], 1):
        row = ET.SubElement(data, NS + "row", r=str(i))
        for j, value in enumerate(values):
            cell = ET.SubElement(row, NS + "c", r=f"{chr(65 + j)}{i}", t="inlineStr")
            ET.SubElement(ET.SubElement(cell, NS + "is"), NS + "t").text = value
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        )
        archive.writestr(
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        )
        archive.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="cases" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            ET.tostring(sheet, encoding="utf-8", xml_declaration=True),
        )
    return output.getvalue()


def read_xlsx(content):
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        if (
            len(archive.infolist()) > 256
            or sum(i.file_size for i in archive.infolist()) > 16777216
        ):
            raise ValueError("Workbook exceeds archive budget")

        def xml(name):
            raw = archive.read(name)
            if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                raise ValueError("Workbook entities are not supported")
            return ET.fromstring(raw)

        strings = (
            [
                "".join(e.itertext())
                for e in xml("xl/sharedStrings.xml").findall(NS + "si")
            ]
            if "xl/sharedStrings.xml" in archive.namelist()
            else []
        )
        sheet = xml("xl/worksheets/sheet1.xml")
        grid = []
        for row in sheet.findall(f"{NS}sheetData/{NS}row"):
            if len(grid) > 1000:
                raise ValueError("At most 1000 cases per import")
            cells = {}
            for cell in row.findall(NS + "c"):
                if cell.find(NS + "f") is not None:
                    raise ValueError(
                        "Formula cells are not accepted; export their literal values"
                    )
                match = re.fullmatch(r"([A-Z]{1,2})[0-9]{1,7}", cell.get("r", ""))
                if not match:
                    raise ValueError("Invalid worksheet cell reference")
                column = 0
                for letter in match[1]:
                    column = column * 26 + ord(letter) - 64
                if column > 80:
                    raise ValueError("Workbook column budget exceeded")
                value = cell.findtext(NS + "v", "")
                if cell.get("t") == "s":
                    value = strings[int(value)]
                elif cell.get("t") == "inlineStr":
                    value = "".join(cell.find(NS + "is").itertext())
                cells[column - 1] = value
            if cells:
                grid.append([cells.get(i, "") for i in range(max(cells) + 1)])
        if not grid:
            return []
        header = grid[0]
        if len(set(header)) != len(header):
            raise ValueError("Duplicate workbook headers")
        return [
            dict(zip(header, row + [""] * (len(header) - len(row)))) for row in grid[1:]
        ]


def parse_cases(content: bytes, filename: str):
    if len(content) > 2097152:
        raise ValueError("Import file exceeds 2 MiB")
    suffix = Path(filename).suffix.lower()
    if suffix == ".xlsx":
        try:
            rows = read_xlsx(content)
        except (
            zipfile.BadZipFile,
            ET.ParseError,
            KeyError,
            IndexError,
            AttributeError,
        ) as exc:
            raise ValueError(
                "Invalid workbook structure or shared-string reference"
            ) from exc
    elif suffix == ".csv":
        reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(
            reader.fieldnames
        ):
            raise ValueError("CSV needs unique column names")
        try:
            rows = list(reader)
        except csv.Error as exc:
            raise ValueError("CSV field is too large or malformed") from exc
    elif suffix == ".json":
        value = json.loads(content)
        if isinstance(value, dict) and value.get("contract_version", 1) != 1:
            raise ValueError("Unsupported import contract version")
        rows = value.get("cases") if isinstance(value, dict) else value
    else:
        raise ValueError("Use csv, json or xlsx")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
        raise ValueError("Provide 1–1000 cases")
    return rows


class AssetRepository:
    def __init__(self, root, project):
        self.store = EngineStore(root, project)
        with self.store.connection() as conn:
            conn.execute(
                "create table if not exists case_assets(case_id text,version integer,content text not null,primary key(case_id,version))"
            )

    def list(self):
        with self.store.connection() as conn:
            rows = conn.execute(
                "select content from case_assets a where version=(select max(version) from case_assets b where a.case_id=b.case_id) order by case_id"
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def get(self, case_id, version=None):
        identifier(case_id)
        with self.store.connection() as conn:
            row = conn.execute(
                "select content from case_assets where case_id=? and (? is null or version=?) order by version desc limit 1",
                (case_id, version, version),
            ).fetchone()
        if not row:
            raise ValueError("Case asset not found")
        return json.loads(row[0])

    def update(self, asset):
        with self.store.connection() as conn:
            conn.execute(
                "update case_assets set content=? where case_id=? and version=?",
                (
                    json.dumps(
                        redact_asset(asset), ensure_ascii=False, allow_nan=False
                    ),
                    asset["case_id"],
                    asset["version"],
                ),
            )


def normalize_case(row, defaults):
    if not isinstance(row, dict) or set(row) - set(COLUMNS):
        raise ValueError("Unknown template columns")
    row = {k: v for k, v in row.items() if v not in (None, "")}
    for key, default in JSON_FIELDS.items():
        value = row.get(key, default)
        row[key] = json.loads(value) if isinstance(value, str) else value
    if row["plan"] is not None:
        plan = ExecutionPlan.from_dict(row["plan"])
        validate_plan(plan)
        if plan.kind not in {"web", "android", "ios", "desktop"}:
            raise ValueError("Execution templates currently accept UI plans")
        case_id = identifier(str(row.get("case_id", plan.id)))
        return {
            "case_id": case_id,
            "title": row.get("title", plan.title or case_id),
            "module": row.get("module", ""),
            "platform": plan.kind,
            "plan": asdict(plan),
            "goal": row.get("goal", plan.title),
            "state": "ready_for_replay",
        }
    case_id = identifier(str(row.get("case_id", "")))
    platform = row.get("platform", "web")
    if (
        platform not in {"web", "android", "ios", "desktop"}
        or not isinstance(row.get("goal"), str)
        or not row["goal"].strip()
    ):
        raise ValueError("Business cases require web/android/ios and a goal")
    if len(row["goal"]) > 8000 or len(str(row.get("expected", ""))) > 8000:
        raise ValueError("Goal/expected exceeds 8000 characters")
    context = {**defaults, **row["context"]}
    for key in ("data_generation", "input_env"):
        context[key] = {**context.get(key, {}), **row[key]}
    context["rules"] = {
        **context.get("rules", {}),
        **({"import_expected": row["expected"]} if row.get("expected") else {}),
    }
    if row.get("preconditions"):
        context["rules"]["import_preconditions"] = str(row["preconditions"])
    if row.get("path_hints"):
        context["path_hints"] = row["path_hints"]
    if row["required_steps"]:
        context["required_steps"] = row["required_steps"]
    from iatest.application.ui_exploration import _context

    checked = _context(platform, context)
    checked.pop("kind")
    # Preserve only environment references/generator definitions under the plan contract in storage.
    bindings = {k: checked.pop(k, {}) for k in ("data_generation", "input_env")}
    return {
        "case_id": case_id,
        "title": row.get("title", row["goal"][:300]),
        "module": row.get("module", ""),
        "platform": platform,
        "goal": row["goal"],
        "expected": row.get("expected", ""),
        "context": {"kind": platform, "spec": checked, **bindings},
        "state": "needs_exploration" if row.get("expected") else "needs_clarification",
        "tags": row.get("tags", ""),
        "priority": row.get("priority", "P2"),
    }


def preview_import(root, project, content, filename, *, defaults=None, policy="error"):
    if policy not in {"error", "skip", "version"}:
        raise ValueError("Conflict policy must be error, skip or version")
    repository = AssetRepository(root, project)
    existing = {a["case_id"]: a["version"] for a in repository.list()}
    rows, seen = [], set()
    for number, row in enumerate(
        parse_cases(content, filename),
        2 if filename.lower().endswith((".csv", ".xlsx")) else 1,
    ):
        result = {"row": number, "state": "valid", "error": ""}
        try:
            asset = normalize_case(row, defaults or {})
            if asset["case_id"] in seen:
                raise ValueError("Duplicate case ID in this file")
            seen.add(asset["case_id"])
            result.update(
                asset=asset, expected_version=existing.get(asset["case_id"], 0)
            )
            if result["expected_version"]:
                if policy == "error":
                    raise ValueError("Case already exists; choose skip or version")
                if policy == "skip":
                    result["state"] = "skipped"
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            result.update(state="invalid", error=str(exc))
        rows.append(result)
    batch = {
        "id": "import-" + uuid.uuid4().hex[:20],
        "contract_version": 1,
        "filename": Path(filename).name,
        "source_sha256": __import__("hashlib").sha256(content).hexdigest(),
        "policy": policy,
        "rows": rows,
        "created_at": now(),
        "state": "preview",
        "model_calls": 0,
    }
    repository.store.save_artifact("import_batch", batch, artifact_id=batch["id"])
    return batch


def commit_import(root, project, batch_id, *, valid_only=False):
    repository = AssetRepository(root, project)
    store = repository.store
    batch = store.get_artifact("import_batch", batch_id)
    if batch["state"] != "preview":
        return batch
    if not valid_only and any(r["state"] == "invalid" for r in batch["rows"]):
        raise ValueError(
            "Preview contains invalid rows; correct them or explicitly import valid rows only"
        )
    with store.connection() as conn:
        conn.execute("begin immediate")
        for row in batch["rows"]:
            if row["state"] != "valid":
                continue
            asset = row["asset"]
            current = (
                conn.execute(
                    "select max(version) from case_assets where case_id=?",
                    (asset["case_id"],),
                ).fetchone()[0]
                or 0
            )
            if current != row["expected_version"]:
                raise ValueError("Asset versions changed after preview; preview again")
            asset.update(
                version=current + 1,
                source={
                    "batch_id": batch_id,
                    "row": row["row"],
                    "file_sha256": batch["source_sha256"],
                },
                created_at=now(),
            )
            if asset.get("plan"):
                plan = {**asset["plan"], "id": "case-" + uuid.uuid4().hex[:24]}
                asset["plan"] = plan
                validate_plan(ExecutionPlan.from_dict(plan))
                conn.execute(
                    "insert into plans values(?,?,?)",
                    (plan["id"], plan["kind"], json.dumps(plan, ensure_ascii=False)),
                )
            conn.execute(
                "insert into case_assets values(?,?,?)",
                (
                    asset["case_id"],
                    asset["version"],
                    json.dumps(
                        redact_asset(asset), ensure_ascii=False, allow_nan=False
                    ),
                ),
            )
            row["state"] = "imported"
        batch["state"] = "imported"
        conn.execute(
            "update artifacts set content=? where kind='import_batch' and id=?",
            (json.dumps(redact_asset(batch), ensure_ascii=False), batch_id),
        )
    return batch


def process_asset(
    root, project, case_id, *, version=None, force=False, gateway_factory=None
):
    from iatest.application.testing_service import execute_test_plan
    from iatest.application.ui_exploration import explore_ui_goal

    repository = AssetRepository(root, project)
    asset = repository.get(case_id, version)
    if asset["state"] == "needs_clarification":
        return asset
    if asset.get("state") == "running":
        raise ValueError(
            "Prior attempt has unknown completion; inspect it before explicitly retrying"
        )
    previous_state = asset["state"]
    asset["state"] = "running"
    with repository.store.connection() as conn:
        conn.execute("begin immediate")
        current = conn.execute(
            "select content from case_assets where case_id=? and version=?",
            (asset["case_id"], asset["version"]),
        ).fetchone()
        if json.loads(current[0])["state"] != previous_state:
            raise ValueError("Case state changed; another attempt may own it")
        conn.execute(
            "update case_assets set content=? where case_id=? and version=?",
            (
                json.dumps(redact_asset(asset), ensure_ascii=False),
                asset["case_id"],
                asset["version"],
            ),
        )
    if asset.get("plan"):
        result = execute_test_plan(root, project, asset["plan"])
        asset.update(
            state="verified"
            if result["verdict"] == "passed"
            else "failed"
            if result["verdict"] == "failed"
            else "inconclusive",
            last_run_id=result["run_id"],
        )
    else:
        asset["state"] = "running"
        repository.update(asset)
        context = {
            **asset["context"]["spec"],
            **{
                k: asset["context"].get(k, {}) for k in ("input_env", "data_generation")
            },
        }
        try:
            result = explore_ui_goal(
                root,
                project,
                asset["platform"],
                asset["goal"],
                context,
                force=force,
                gateway_factory=gateway_factory,
            )
            asset.update(
                state=result["status"],
                exploration_id=result["id"],
                error=result.get("error", ""),
            )
            if result["status"].startswith("reproducible_"):
                asset["plan"] = result["plan"]
        except Exception as exc:  # noqa: BLE001 -- retain an attempt for operator reconciliation
            asset.update(state="inconclusive", error=str(exc))
    repository.update(asset)
    return redact_asset(asset)


def process_import(root, project, batch_id, *, gateway_factory=None, progress=None):
    store = EngineStore(root, project)
    batch = store.get_artifact("import_batch", batch_id)
    if batch["state"] != "imported":
        raise ValueError("Commit the preview before processing")
    parent = ACTIVE_RUNTIME.get()
    for row in batch["rows"]:
        if row["state"] != "imported":
            continue
        if parent:
            parent.control.check()
        asset = AssetRepository(root, project).get(
            row["asset"]["case_id"], row["asset"]["version"]
        )
        if asset["state"] not in {"needs_exploration", "ready_for_replay"}:
            continue
        if progress:
            progress(
                f"Processing {asset['case_id']} v{asset['version']}", stage="execute"
            )
        process_asset(
            root,
            project,
            asset["case_id"],
            version=asset["version"],
            gateway_factory=gateway_factory,
        )
    return {
        "batch_id": batch_id,
        "assets": [
            AssetRepository(root, project).get(
                r["asset"]["case_id"], r["asset"]["version"]
            )
            for r in batch["rows"]
            if r["state"] == "imported"
        ],
    }
