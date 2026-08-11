from __future__ import annotations

import json

from ai_apitest.ai.prompting import render_prompt
from ai_apitest.ai.provider import chat_completion
from ai_apitest.core.artifact_language import analysis_headings, output_language_instruction
from ai_apitest.core.models import CaseResult
from ai_apitest.core.serde import to_jsonable


def analyze_results_with_llm(results: list[CaseResult], project: str, llm: dict, context_block: str = "", trend_context: dict | None = None) -> str:
    total = len(results)
    passed = sum(1 for item in results if item.status == "passed")
    failed = sum(1 for item in results if item.status == "failed")
    failures = [item for item in results if item.status == "failed"]
    slowest = sorted(results, key=lambda item: item.elapsed_ms, reverse=True)[:5]
    payload: dict[str, object] = {
        "project": project,
        "summary": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "success_rate": f"{(passed / total * 100):.1f}%" if total else "-",
        },
        "failed_cases": [case_summary(item) for item in failures],
        "slowest_cases": [case_summary(item) for item in slowest],
        "results": [to_jsonable(item) for item in results],
    }
    if trend_context:
        payload["trend_context"] = trend_context
    headings = analysis_headings()
    return chat_completion(
        base_url=llm.get("base_url", ""),
        model=llm.get("model", ""),
        api_key_env=llm.get("api_key_env", ""),
        provider=llm.get("provider", "openai-compatible"),
        messages=[
            {
                "role": "user",
                "content": render_prompt(
                    "result_analysis.md",
                    {
                        "CONTEXT_BLOCK": context_block.strip() or "## 项目历史上下文\n\n无可注入的已确认上下文。",
                        "RESULTS_JSON": json.dumps(payload, ensure_ascii=False, indent=2),
                        "OUTPUT_LANGUAGE_INSTRUCTION": output_language_instruction(),
                        "ONE_LINE_CONCLUSION": headings["one_line_conclusion"],
                        "RESULT_OVERVIEW": headings["result_overview"],
                        "MAIN_ISSUES": headings["main_issues"],
                        "RECOMMENDED_ACTIONS": headings["recommended_actions"],
                        "NOTES": headings["notes"],
                        "FAILURE_REVIEW": headings["failure_review"],
                    },
                ),
            }
        ],
    ).strip()


def review_failed_results_with_llm(results: list[CaseResult], project: str, llm: dict) -> str:
    failures = [item for item in results if item.status == "failed"]
    if not failures:
        return ""
    payload = {
        "project": project,
        "failed_count": len(failures),
        "failed_cases": [case_summary(item) for item in failures],
    }
    return chat_completion(
        base_url=llm.get("base_url", ""),
        model=llm.get("model", ""),
        api_key_env=llm.get("api_key_env", ""),
        provider=llm.get("provider", "openai-compatible"),
        messages=[
            {
                "role": "user",
                "content": output_language_instruction()
                + "\n\n"
                + render_prompt("result_review.md", {"FAILED_RESULTS_JSON": json.dumps(payload, ensure_ascii=False, indent=2)}),
            }
        ],
    ).strip()


def case_summary(result: CaseResult) -> dict:
    return {
        "case_id": result.case_id,
        "title": result.title,
        "status": result.status,
        "interface": f"{result.method} {result.path}",
        "status_code": result.status_code,
        "elapsed_ms": result.elapsed_ms,
        "design_method": result.design_method,
        "case_risk": result.case_risk,
        "preconditions": result.preconditions,
        "test_steps": result.test_steps,
        "expected_result": result.expected_result,
        "error": result.error,
        "assertion_errors": result.assertion_errors,
        "setup": result.setup,
        "cleanup": result.cleanup,
        "request": result.request,
        "response_preview": result.response,
    }
