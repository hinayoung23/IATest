from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
import xml.etree.ElementTree as ET

from ai_apitest.core.files import atomic_write_text
from ai_apitest.core.models import CaseResult
from ai_apitest.core.redaction import redact


def execution_metrics(results: list[CaseResult]) -> dict:
    total = len(results)
    passed = sum(item.status == "passed" for item in results)
    failed = sum(item.status == "failed" for item in results)
    skipped = sum(item.status == "skipped" for item in results)
    cancelled = sum(item.status == "cancelled" for item in results)
    executed = passed + failed
    return {
        "total": total, "executed": executed, "passed": passed, "failed": failed,
        "skipped": skipped, "cancelled": cancelled, "flaky": sum(item.flaky for item in results),
        "execution_pass_rate": round(passed / executed, 4) if executed else None,
        "overall_pass_rate": round(passed / total, 4) if total else None,
        "skip_rate": round(skipped / total, 4) if total else None,
    }


def write_json_results(path: Path, results: list[CaseResult], metadata: dict | None = None) -> None:
    payload = {"version": 1, "metadata": metadata or {}, "summary": execution_metrics(results), "results": [asdict(item) for item in results]}
    atomic_write_text(path, json.dumps(redact(payload), ensure_ascii=False, indent=2) + "\n")


def write_junit(path: Path, results: list[CaseResult], *, project: str = "default", metadata: dict | None = None) -> None:
    metrics = execution_metrics(results)
    suite = ET.Element("testsuite", name=project, tests=str(len(results)), failures=str(metrics["failed"]), errors=str(metrics["cancelled"]), skipped=str(metrics["skipped"]), time=str(sum(item.elapsed_ms for item in results) / 1000))
    properties = ET.SubElement(suite, "properties")
    for key, value in redact(metadata or {}).items():
        ET.SubElement(properties, "property", name=str(key), value=json.dumps(value, ensure_ascii=False))
    for item in results:
        evidence = redact(asdict(item))
        case = ET.SubElement(suite, "testcase", classname=project, name=evidence["case_id"] + " " + evidence["title"], time=str(item.elapsed_ms / 1000))
        message = evidence["error"] or "; ".join(evidence["assertion_errors"])
        if item.status in {"failed", "cancelled", "skipped"}:
            tag = {"failed": "failure", "cancelled": "error", "skipped": "skipped"}[item.status]
            ET.SubElement(case, tag, message=message).text = json.dumps(evidence, ensure_ascii=False)
        elif item.flaky:
            ET.SubElement(case, "system-out").text = json.dumps({"flaky": True, "attempt_history": evidence["attempt_history"]}, ensure_ascii=False)
    ET.indent(suite)
    def xml_text(value):
        return "".join(char for char in str(value) if char in "\t\n\r" or 0x20 <= ord(char) <= 0xD7FF or 0xE000 <= ord(char) <= 0xFFFD or 0x10000 <= ord(char) <= 0x10FFFF)
    for node in suite.iter():
        if node.text:
            node.text = xml_text(node.text)
        for key, value in node.attrib.items():
            node.set(key, xml_text(value))
    atomic_write_text(path, ET.tostring(suite, encoding="unicode", xml_declaration=True) + "\n")
