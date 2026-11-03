import io
import json
import zipfile

import pytest

from iatest.application.test_assets import (
    AssetRepository,
    commit_import,
    parse_cases,
    preview_import,
    template_bytes,
)
from iatest.core.config import init_project
from iatest.engines.data import redact_asset


@pytest.fixture
def root(tmp_path):
    init_project(tmp_path, "suite")
    return tmp_path


def test_redaction_preserves_authentication_references_and_extraction_definitions():
    value = {
        "kind": "performance",
        "spec": {
            "scenarios": [
                {
                    "extract": {"token": "$.token"},
                    "request": {
                        "headers": {
                            "Authorization": "Bearer {{token}}",
                            "X-Api-Key": {"$env": "EXISTING_KEY"},
                        },
                        "json": {"password": {"$value": "password"}},
                    },
                }
            ]
        },
        "input_env": {"password": "EXISTING_PASSWORD"},
    }
    assert redact_asset(value) == value
    workflow = {
        "kind": "workflow",
        "spec": {
            "exports": {
                "token": {"path": "$.token", "type": "string", "sensitive": True}
            },
            "headers": {
                "Authorization": {
                    "$from": "login.token",
                    "type": "string",
                    "sensitive": True,
                }
            },
        },
    }
    assert redact_asset(workflow) == workflow
    assert (
        redact_asset({"Authorization": "inline-secret-credential"})["Authorization"]
        == "[REDACTED]"
    )


@pytest.mark.parametrize("fmt", ["csv", "json", "xlsx"])
@pytest.mark.parametrize("mode", ["goals", "plans"])
def test_templates_import_without_model_or_execution(root, fmt, mode):
    preview = preview_import(root, "suite", template_bytes(fmt, mode), "cases." + fmt)
    assert preview["rows"][0]["state"] == "valid", preview
    batch = commit_import(root, "suite", preview["id"])
    assert batch["state"] == "imported"
    assert AssetRepository(root, "suite").list()[0]["version"] == 1
    assert AssetRepository(root, "suite").store.runs() == []


def test_conflicts_previews_are_transactional_and_never_overwrite(root):
    data = template_bytes("csv")
    original = preview_import(root, "suite", data, "x.csv")
    commit_import(root, "suite", original["id"])
    error = preview_import(root, "suite", data, "x.csv")
    assert error["rows"][0]["state"] == "invalid"
    with pytest.raises(ValueError):
        commit_import(root, "suite", error["id"])
    a = preview_import(root, "suite", data, "x.csv", policy="version")
    b = preview_import(root, "suite", data, "x.csv", policy="version")
    commit_import(root, "suite", a["id"])
    with pytest.raises(ValueError, match="changed"):
        commit_import(root, "suite", b["id"])
    repository = AssetRepository(root, "suite")
    assert repository.get("nickname-persistence", 1)["version"] == 1
    assert repository.get("nickname-persistence")["version"] == 2


def test_missing_oracle_is_not_made_into_a_passing_case(root):
    row = parse_cases(template_bytes("json"), "cases.json")[0]
    row["expected"] = ""
    preview = preview_import(root, "suite", json.dumps([row]).encode(), "x.json")
    commit_import(root, "suite", preview["id"])
    assert AssetRepository(root, "suite").list()[0]["state"] == "needs_clarification"


def test_xlsx_formula_rejected():
    source = zipfile.ZipFile(io.BytesIO(template_bytes("xlsx")))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as target:
        for name in source.namelist():
            value = source.read(name)
            if name.endswith("sheet1.xml"):
                value = value.replace(b"</ns0:c>", b"<ns0:f>1+1</ns0:f></ns0:c>", 1)
            target.writestr(name, value)
    with pytest.raises(ValueError, match="Formula"):
        parse_cases(output.getvalue(), "x.xlsx")
