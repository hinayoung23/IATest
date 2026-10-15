from __future__ import annotations

import re
from typing import Any

from ai_apitest.data.agent_runs import redact_text
from ai_apitest.tools.registry import ToolExecutionContext, ToolExecutionResult, ToolRegistry, ToolSpec


READ_ROLES = ("test_planner", "case_designer", "case_maintainer", "quality_reviewer", "api_analyst")
ALL_AGENT_ROLES = (
    "test_planner",
    "case_designer",
    "case_maintainer",
    "quality_reviewer",
    "api_analyst",
    "context_curator",
    "result_analyst",
)


def build_case_generation_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    from ai_apitest.agents.recipes import register_recipe_tools
    register_recipe_tools(registry)
    registry.register(
        ToolSpec(
            name="read_task_evidence",
            description=(
                "读取当前 Agent 运行中因上下文恢复而移出 Prompt 的原始工具证据。"
                "只能读取恢复检查点 evidence_index 中列出的 evidence_id，并支持分页。"
            ),
            input_schema=object_schema(
                {
                    "evidence_id": {"type": "string", "minLength": 1, "maxLength": 100},
                    "offset": {"type": "integer", "minimum": 0},
                    "max_chars": {"type": "integer", "minimum": 500, "maximum": 16000},
                },
                required=["evidence_id"],
            ),
            handler=read_task_evidence,
            allowed_roles=ALL_AGENT_ROLES,
            result_limit_chars=18000,
        )
    )
    registry.register(
        ToolSpec(
            name="list_endpoints",
            description="分页查询当前项目接口摘要。先用摘要确定范围，需要 Schema 时再调用 get_endpoint_detail。",
            input_schema=object_schema(
                {
                    "query": {"type": "string", "maxLength": 200},
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                }
            ),
            handler=list_endpoints,
            allowed_roles=("test_planner", "case_designer", "case_maintainer", "api_analyst"),
        )
    )
    registry.register(
        ToolSpec(
            name="get_endpoint_detail",
            description="按 method 和 path 读取一个真实接口的完整定义。接口不存在时返回错误。",
            input_schema=object_schema(
                {"method": {"type": "string", "minLength": 1}, "path": {"type": "string", "minLength": 1}},
                required=["method", "path"],
            ),
            handler=get_endpoint_detail,
            allowed_roles=READ_ROLES,
        )
    )
    registry.register(
        ToolSpec(
            name="list_business_rules",
            description=(
                "分页读取已通过接口识别质量审计的业务规则。用例规划和设计只能以这些规则及接口定义为事实，"
                "不得重新读取接口文档原文。"
            ),
            input_schema=object_schema(
                {
                    "method": {"type": "string", "maxLength": 16},
                    "path": {"type": "string", "maxLength": 500},
                    "query": {"type": "string", "maxLength": 300},
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                }
            ),
            handler=list_business_rules,
            allowed_roles=READ_ROLES,
        )
    )
    registry.register(
        ToolSpec(
            name="get_business_rule_detail",
            description="按 rule_id 读取一条可信业务规则的完整触发条件、预期、夹具要求和接口血缘。",
            input_schema=object_schema(
                {"rule_id": {"type": "string", "minLength": 1, "maxLength": 200}},
                required=["rule_id"],
            ),
            handler=get_business_rule_detail,
            allowed_roles=READ_ROLES,
        )
    )
    registry.register(
        ToolSpec(
            name="list_existing_cases",
            description="分页查询当前项目已有用例摘要，用于去重、补充生成和维护定位。",
            input_schema=object_schema(
                {
                    "query": {"type": "string", "maxLength": 200},
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                }
            ),
            handler=list_existing_cases,
            allowed_roles=("case_designer", "case_maintainer", "quality_reviewer"),
        )
    )
    registry.register(
        ToolSpec(
            name="get_existing_case_detail",
            description="按用例 ID 读取一条已有用例详情；不存在时返回错误。",
            input_schema=object_schema(
                {"case_id": {"type": "string", "minLength": 1, "maxLength": 200}},
                required=["case_id"],
            ),
            handler=get_existing_case_detail,
            allowed_roles=("case_designer", "case_maintainer", "quality_reviewer"),
        )
    )
    registry.register(
        ToolSpec(
            name="get_generation_strategies",
            description="读取当前项目已启用的默认和自定义用例生成策略。",
            input_schema=object_schema({}),
            handler=get_generation_strategies,
            allowed_roles=READ_ROLES,
        )
    )
    registry.register(
        ToolSpec(
            name="search_confirmed_lessons",
            description="检索已确认且满足注入门槛的项目经验；候选、驳回或未验证依赖不会返回。",
            input_schema=object_schema(
                {
                    "query": {"type": "string", "maxLength": 500},
                    "method": {"type": "string", "maxLength": 16},
                    "path": {"type": "string", "maxLength": 500},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                }
            ),
            handler=search_confirmed_lessons,
            allowed_roles=("test_planner", "case_designer", "case_maintainer", "quality_reviewer", "context_curator", "result_analyst"),
        )
    )
    registry.register(
        ToolSpec(
            name="get_dependency_graph",
            description="读取当前项目已经保存的接口数据依赖图；只作为数据准备和链路设计依据。",
            input_schema=object_schema({}),
            handler=get_dependency_graph,
            allowed_roles=READ_ROLES,
        )
    )
    registry.register(
        ToolSpec(
            name="submit_coverage_plan",
            description="提交本地标准覆盖之外的业务语义增量计划并结束规划任务；没有增量时提交空 items。",
            input_schema=object_schema(
                {
                    "items": {
                        "type": "array",
                        "minItems": 0,
                        "maxItems": 1000,
                        "items": {
                            "type": "object",
                            "required": [
                                "scenario_id",
                                "endpoint_method",
                                "endpoint_path",
                                "design_method",
                                "priority",
                                "title_hint",
                                "coverage_tags",
                            ],
                            "properties": {
                                "scenario_id": {"type": "string", "minLength": 1},
                                "endpoint_method": {"type": "string", "minLength": 1},
                                "endpoint_path": {"type": "string"},
                                "design_method": {"type": "string", "minLength": 1},
                                "priority": {"type": "string", "minLength": 1},
                                "title_hint": {"type": "string", "minLength": 1},
                                "coverage_tags": {"type": "array", "items": {"type": "string"}},
                                "expected_basis": {"type": "string"},
                                "data_dependency": {},
                                "chain_steps": {"type": "array", "items": {"type": "object"}},
                                "rule_ids": {"type": "array", "items": {"type": "string"}},
                                "coverage_partition_ids": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "runnable": {"type": "boolean"},
                                "risk": {"type": "string"},
                                "fixture_requirements": {"type": "array", "items": {"type": "string"}},
                            },
                        },
                    }
                },
                required=["items"],
            ),
            handler=submit_coverage_plan,
            allowed_roles=("test_planner",),
            side_effect="transform",
            terminal=True,
            result_limit_chars=2000,
        )
    )
    registry.register(
        ToolSpec(
            name="submit_case_drafts",
            description="提交覆盖计划对应的完整 ScenarioDraft 列表并结束当前设计任务。",
            input_schema=object_schema(
                {
                    "drafts": {
                        "type": "array",
                        "minItems": 0,
                        "maxItems": 1000,
                        "items": {
                            "type": "object",
                            "required": [
                                "scenario_id",
                                "endpoint_method",
                                "endpoint_path",
                                "intent",
                                "design_method",
                                "expected_outcome",
                            ],
                            "properties": {
                                "variables": {"type": "object"},
                                "data_generation": {"type": "object"},
                                "samples": {"type": "integer", "minimum": 1, "maximum": 10000},
                                "scenario_id": {"type": "string", "minLength": 1},
                                "endpoint_method": {"type": "string", "minLength": 1},
                                "endpoint_path": {"type": "string"},
                                "intent": {"type": "string", "minLength": 1},
                                "design_method": {"type": "string", "minLength": 1},
                                "priority_hint": {"type": "string"},
                                "coverage_tags": {"type": "array", "items": {"type": "string"}},
                                "request_overrides": {"type": "object"},
                                "omit_fields": {"type": "array", "items": {"type": "string"}},
                                "expected_outcome": {"type": "string", "minLength": 1},
                                "expected_basis": {"type": "string"},
                                "assertions": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "description": "使用标准断言字段，例如 {type: status_code, operator: eq, expected: 200} 或 {type: json_path, target: $.id, operator: exists}。",
                                        "properties": {
                                            "type": {"type": "string", "enum": ["status_code", "json_path", "header", "body_contains", "json_schema", "response_time", "custom"]},
                                            "target": {"type": "string"},
                                            "operator": {"type": "string"},
                                            "expected": {},
                                        },
                                        "if": {"properties": {"type": {"const": "json_schema"}}, "required": ["type"]},
                                        "then": {
                                            "properties": {"expected": {"type": ["object", "boolean"]}, "value": {"type": ["object", "boolean"]}},
                                            "anyOf": [{"required": ["expected"]}, {"required": ["value"]}],
                                        },
                                    },
                                },
                                "chain_steps": {"type": "array", "items": {"type": "object"}},
                                "rule_ids": {"type": "array", "items": {"type": "string"}},
                                "coverage_partition_ids": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "runnable": {"type": "boolean"},
                                "risk": {"type": "string"},
                                "fixture_requirements": {"type": "array", "items": {"type": "string"}},
                            },
                        },
                    }
                },
                required=["drafts"],
            ),
            handler=submit_case_drafts,
            allowed_roles=("case_designer",),
            side_effect="transform",
            terminal=True,
            result_limit_chars=2000,
        )
    )
    registry.register(
        ToolSpec(
            name="submit_case_maintenance",
            description="提交受影响用例的逐项更新或退役决策并结束维护任务。update 决策必须携带 draft。",
            input_schema=object_schema(
                {
                    "decisions": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 200,
                        "items": {
                            "type": "object",
                            "required": ["target_case_id", "action", "reason"],
                            "properties": {
                                "target_case_id": {"type": "string", "minLength": 1},
                                "action": {"type": "string", "enum": ["update", "retire"]},
                                "reason": {"type": "string", "minLength": 1},
                                "draft": {"type": "object"},
                            },
                        },
                    }
                },
                required=["decisions"],
            ),
            handler=submit_case_maintenance,
            allowed_roles=("case_maintainer",),
            side_effect="transform",
            terminal=True,
            result_limit_chars=2000,
        )
    )
    registry.register(
        ToolSpec(
            name="get_document_info",
            description="读取当前待分析接口文档的长度、分块大小和内容哈希，不返回正文。",
            input_schema=object_schema({}),
            handler=get_document_info,
            allowed_roles=("api_analyst",),
        )
    )
    registry.register(
        ToolSpec(
            name="read_document_chunk",
            description="按字符偏移读取当前接口文档正文。长文档必须分页读取，直到 has_more=false。",
            input_schema=object_schema(
                {
                    "offset": {"type": "integer", "minimum": 0},
                    "max_chars": {"type": "integer", "minimum": 1000, "maximum": 16000},
                },
                required=["offset"],
            ),
            handler=read_document_chunk,
            allowed_roles=("api_analyst",),
            result_limit_chars=18000,
        )
    )
    registry.register(
        ToolSpec(
            name="submit_endpoint_drafts",
            description=(
                "提交从文档证据中识别出的一个接口草稿。工具会累计已提交接口，"
                "只有覆盖任务给出的确定性接口清单后才结束识别任务。"
            ),
            input_schema=object_schema(
                {
                    "endpoints": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 1,
                        "items": {
                            "type": "object",
                            "required": ["method", "path", "summary"],
                            "properties": {
                                "method": {"type": "string", "minLength": 1},
                                "path": {"type": "string", "minLength": 1},
                                "summary": {"type": "string", "minLength": 1},
                                "description": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                                "parameters": {
                                    "anyOf": [
                                        {"type": "array", "items": {"type": "object"}},
                                        {"type": "null"},
                                    ]
                                },
                                "request_body": {},
                                "responses": {"anyOf": [{"type": "object"}, {"type": "null"}]},
                                "tags": {
                                    "anyOf": [
                                        {"type": "array", "items": {"type": "string"}},
                                        {"type": "null"},
                                    ]
                                },
                                "operation_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                                "deprecated": {"anyOf": [{"type": "boolean"}, {"type": "null"}]},
                                "security": {
                                    "anyOf": [
                                        {"type": "array", "items": {"type": "object"}},
                                        {"type": "null"},
                                    ]
                                },
                                "business_rules": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "rule_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                                            "title": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                                            "description": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                                            "category": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                                            "strategy": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                                            "testable": {"anyOf": [{"type": "boolean"}, {"type": "null"}]},
                                            "preconditions": {
                                                "anyOf": [
                                                    {"type": "array", "items": {"type": "string"}},
                                                    {"type": "string"},
                                                    {"type": "null"},
                                                ]
                                            },
                                            "lifecycle_state": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                                            "trigger": {"anyOf": [{"type": "object"}, {"type": "null"}]},
                                            "expected": {"anyOf": [{"type": "object"}, {"type": "null"}]},
                                            "runnable": {"anyOf": [{"type": "boolean"}, {"type": "null"}]},
                                            "risk": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                                            "fixture_requirements": {
                                                "anyOf": [
                                                    {"type": "array", "items": {"type": "string"}},
                                                    {"type": "string"},
                                                    {"type": "null"},
                                                ]
                                            },
                                            "source_section": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                                            "source_evidence": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                                        },
                                    },
                                },
                            },
                        },
                    }
                },
                required=["endpoints"],
            ),
            handler=submit_endpoint_drafts,
            allowed_roles=("api_analyst",),
            side_effect="transform",
            terminal=True,
            result_limit_chars=2000,
        )
    )
    registry.register(
        ToolSpec(
            name="submit_dependency_graph",
            description="提交接口间数据依赖边并结束依赖分析任务。边必须引用真实接口。",
            input_schema=object_schema(
                {
                    "edges": {
                        "type": "array",
                        "maxItems": 5000,
                        "items": {
                            "type": "object",
                            "required": ["from", "to", "type", "safe_to_prepare", "confidence", "reason"],
                            "properties": {
                                "from": {"type": "object"},
                                "to": {"type": "object"},
                                "type": {"type": "string"},
                                "produced_field": {"type": "string"},
                                "extract": {"type": "object"},
                                "inject": {"type": "object"},
                                "safe_to_prepare": {"type": "boolean"},
                                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                                "reason": {"type": "string"},
                            },
                        },
                    }
                },
                required=["edges"],
            ),
            handler=submit_dependency_graph,
            allowed_roles=("api_analyst",),
            side_effect="transform",
            terminal=True,
            result_limit_chars=2000,
        )
    )
    registry.register(
        ToolSpec(
            name="submit_result_analysis",
            description="提交执行分析正文、经验候选和已知问题更新建议并结束分析任务。",
            input_schema=object_schema(
                {
                    "markdown": {"type": "string", "minLength": 1, "maxLength": 50000},
                    "lessons": {
                        "type": "array",
                        "maxItems": 100,
                        "items": object_schema(
                            {
                                "title": {"type": "string", "minLength": 1, "maxLength": 200},
                                "content": {"type": "string", "minLength": 1, "maxLength": 10000},
                                "scope": {"type": "string"},
                                "method": {"type": "string"},
                                "path": {"type": "string"},
                                "lesson_type": {"type": "string"},
                                "lesson_subtype": {"type": "string"},
                                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                                "tags": {"type": "array", "items": {"type": "string"}},
                                "environment": {"type": "string"},
                            },
                            required=["title", "content"],
                        ),
                    },
                    "known_issue_updates": {
                        "type": "array",
                        "maxItems": 100,
                        "items": object_schema(
                            {
                                "id": {"type": "string"},
                                "lesson_id": {"type": "string"},
                                "lifecycle_status": {
                                    "type": "string",
                                    "enum": [
                                        "active",
                                        "reopened",
                                        "resolved_candidate",
                                        "resolved",
                                        "ignored",
                                        "archived",
                                    ],
                                },
                                "status": {"type": "string"},
                                "reason": {"type": "string"},
                            }
                        ),
                    },
                },
                required=["markdown", "lessons", "known_issue_updates"],
            ),
            handler=submit_result_analysis,
            allowed_roles=("result_analyst",),
            side_effect="transform",
            terminal=True,
            result_limit_chars=2000,
        )
    )
    registry.register(
        ToolSpec(
            name="submit_result_review",
            description="提交失败用例复核 Markdown 并结束复核任务。",
            input_schema=object_schema(
                {"markdown": {"type": "string", "minLength": 1, "maxLength": 50000}},
                required=["markdown"],
            ),
            handler=submit_result_review,
            allowed_roles=("result_analyst",),
            side_effect="transform",
            terminal=True,
            result_limit_chars=2000,
        )
    )
    registry.register(
        ToolSpec(
            name="get_audit_artifact_info",
            description="读取当前待审计产物证据包的长度、哈希和建议分块大小，不返回正文。",
            input_schema=object_schema({}),
            handler=get_audit_artifact_info,
            allowed_roles=("quality_reviewer",),
        )
    )
    registry.register(
        ToolSpec(
            name="read_audit_artifact_chunk",
            description="分页读取当前待审计产物、事实依据和硬门禁结果；提交评分前必须读完整个证据包。",
            input_schema=object_schema(
                {
                    "offset": {"type": "integer", "minimum": 0},
                    "max_chars": {"type": "integer", "minimum": 1, "maximum": 180000},
                },
                required=["offset"],
            ),
            handler=read_audit_artifact_chunk,
            allowed_roles=("quality_reviewer",),
            result_limit_chars=185000,
        )
    )
    registry.register(
        ToolSpec(
            name="submit_quality_review",
            description="提交质量审查裁决并结束当前审查任务。",
            input_schema=object_schema(
                {
                    "verdict": {"type": "string", "enum": ["approved", "needs_revision", "rejected"]},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "dimension_scores": {
                        "type": "object",
                        "properties": {
                            "reliability": {"type": "number", "minimum": 0, "maximum": 100},
                            "completeness": {"type": "number", "minimum": 0, "maximum": 100},
                            "reasonableness": {"type": "number", "minimum": 0, "maximum": 100},
                            "non_redundancy": {"type": "number", "minimum": 0, "maximum": 100},
                        },
                        "required": [
                            "reliability",
                            "completeness",
                            "reasonableness",
                            "non_redundancy",
                        ],
                        "additionalProperties": False,
                    },
                    "summary": {"type": "string", "maxLength": 2000},
                    "issues": {
                        "type": "array",
                        "maxItems": 8,
                        "items": {
                            "type": "object",
                            "properties": {
                                "severity": {
                                    "type": "string",
                                    "enum": ["critical", "high", "medium", "low"],
                                },
                                "dimension": {
                                    "type": "string",
                                    "enum": [
                                        "reliability",
                                        "completeness",
                                        "reasonableness",
                                        "non_redundancy",
                                    ],
                                },
                                "location": {"type": "string", "maxLength": 500},
                                "evidence": {"type": "string", "maxLength": 800},
                                "reason": {"type": "string", "maxLength": 800},
                                "suggested_fix": {"type": "string", "maxLength": 800},
                            },
                            "required": [
                                "severity",
                                "dimension",
                                "location",
                                "evidence",
                                "reason",
                                "suggested_fix",
                            ],
                            "additionalProperties": False,
                        },
                    },
                    "suggestions": {
                        "type": "array",
                        "maxItems": 8,
                        "items": {"type": "string", "maxLength": 800},
                    },
                    "coverage_gaps": {
                        "type": "array",
                        "maxItems": 8,
                        "items": {
                            "type": "object",
                            "properties": {
                                "location": {"type": "string", "maxLength": 500},
                                "description": {"type": "string", "maxLength": 800},
                            },
                            "required": ["location", "description"],
                            "additionalProperties": False,
                        },
                    },
                },
                required=[
                    "verdict",
                    "confidence",
                    "dimension_scores",
                    "summary",
                    "issues",
                    "suggestions",
                    "coverage_gaps",
                ],
            ),
            handler=submit_quality_review,
            allowed_roles=("quality_reviewer",),
            side_effect="transform",
            terminal=True,
            result_limit_chars=2000,
        )
    )
    registry.register(
        ToolSpec(
            name="submit_context_decisions",
            description="提交每条经验候选的创建、合并、拒绝、冲突或人工复核决策。",
            input_schema=object_schema(
                {
                    "decisions": {
                        "type": "array",
                        "maxItems": 200,
                        "items": {
                            "type": "object",
                            "required": ["candidate_id", "decision", "confidence", "reason"],
                            "properties": {
                                "candidate_id": {"type": "string", "minLength": 1},
                                "decision": {"type": "string", "enum": ["create", "merge", "reject", "needs_review", "conflict"]},
                                "target_lesson_id": {},
                                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                                "reason": {"type": "string"},
                            },
                        },
                    }
                },
                required=["decisions"],
            ),
            handler=submit_context_decisions,
            allowed_roles=("context_curator",),
            side_effect="transform",
            terminal=True,
            result_limit_chars=2000,
        )
    )
    registry.register(
        ToolSpec(
            name="submit_conflict_reviews",
            description="提交疑似经验冲突的逐项复核结论。",
            input_schema=object_schema(
                {
                    "reviews": {
                        "type": "array",
                        "maxItems": 200,
                        "items": {
                            "type": "object",
                            "required": ["relation_id", "decision", "reason"],
                            "properties": {
                                "relation_id": {"type": "integer"},
                                "decision": {"type": "string", "enum": ["conflict", "not_conflict", "uncertain"]},
                                "reason": {"type": "string"},
                            },
                        },
                    }
                },
                required=["reviews"],
            ),
            handler=submit_conflict_reviews,
            allowed_roles=("context_curator",),
            side_effect="transform",
            terminal=True,
            result_limit_chars=2000,
        )
    )
    return registry


def read_task_evidence(arguments: dict[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
    if context.evidence_reader is None or not context.run_id:
        raise KeyError("当前任务没有可读取的上下文恢复证据")
    evidence_id = str(arguments["evidence_id"])
    if not re.fullmatch(r"evidence-[0-9a-f]{20}", evidence_id):
        raise KeyError(
            "evidence_id 格式无效；只能原样使用恢复检查点或已压缩工具结果中的 "
            "evidence-xxxxxxxxxxxxxxxxxxxx，不能使用 sha256/content_hash"
        )
    return context.evidence_reader(
        evidence_id,
        max(0, int(arguments.get("offset") or 0)),
        max(500, min(16000, int(arguments.get("max_chars") or 8000))),
    )


def list_endpoints(arguments: dict[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
    return context.data.endpoints.list_summaries(
        query=str(arguments.get("query") or ""),
        offset=int(arguments.get("offset") or 0),
        limit=int(arguments.get("limit") or 50),
    )


def get_endpoint_detail(arguments: dict[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
    return context.data.endpoints.detail(str(arguments["method"]), str(arguments["path"]))


def list_business_rules(arguments: dict[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
    return context.data.endpoints.list_business_rules(
        method=str(arguments.get("method") or ""),
        path=str(arguments.get("path") or ""),
        query=str(arguments.get("query") or ""),
        offset=int(arguments.get("offset") or 0),
        limit=int(arguments.get("limit") or 100),
    )


def get_business_rule_detail(arguments: dict[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
    return context.data.endpoints.business_rule_detail(str(arguments["rule_id"]))


def list_existing_cases(arguments: dict[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
    return context.data.cases.list_summaries(
        query=str(arguments.get("query") or ""),
        offset=int(arguments.get("offset") or 0),
        limit=int(arguments.get("limit") or 50),
    )


def get_existing_case_detail(arguments: dict[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
    return context.data.cases.detail(str(arguments["case_id"]))


def get_generation_strategies(arguments: dict[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
    return context.data.config.generation_strategies()


def search_confirmed_lessons(arguments: dict[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
    items = context.data.context.search_confirmed(
        query=str(arguments.get("query") or ""),
        method=str(arguments.get("method") or ""),
        path=str(arguments.get("path") or ""),
        limit=int(arguments.get("limit") or 10),
    )
    return {"items": items, "total": len(items)}


def get_dependency_graph(arguments: dict[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
    return context.data.endpoints.dependency_graph()


def submit_coverage_plan(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolExecutionResult:
    items = arguments["items"]
    context.state["coverage_plan"] = items
    return ToolExecutionResult(
        content=f"已接收覆盖计划，共 {len(items)} 个场景。",
        value=items,
        terminal=True,
    )


def submit_case_drafts(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolExecutionResult:
    drafts = arguments["drafts"]
    context.state["case_drafts"] = drafts
    return ToolExecutionResult(
        content=f"已接收语义草稿，共 {len(drafts)} 个场景。",
        value=drafts,
        terminal=True,
    )


def submit_case_maintenance(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolExecutionResult:
    decisions = arguments["decisions"]
    return ToolExecutionResult(
        content=f"已接收用例维护决策，共 {len(decisions)} 条。",
        value=decisions,
        terminal=True,
    )


def get_document_info(arguments: dict[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
    import hashlib

    text = str(context.state.get("document_text") or "")
    context.state["forced_next_tool"] = "read_document_chunk"
    return {
        "length_chars": len(text),
        "recommended_chunk_chars": min(180000, max(12000, len(text))),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def read_document_chunk(arguments: dict[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
    text = str(context.state.get("document_text") or "")
    offset = max(0, int(arguments.get("offset") or 0))
    # Some models keep asking for conservative 12k chunks even when the info
    # tool recommends a larger page. Returning at least 60k keeps a complete
    # review within the role's bounded tool rounds and avoids repeatedly
    # re-sending the growing conversation.
    max_chars = max(60000, min(180000, int(arguments.get("max_chars") or 12000)))
    chunk = text[offset : offset + max_chars]
    next_offset = offset + len(chunk)
    ranges = context.state.setdefault("document_read_ranges", [])
    if isinstance(ranges, list) and next_offset > offset:
        ranges.append((offset, next_offset))
    if next_offset < len(text):
        context.state["forced_next_tool"] = "read_document_chunk"
    else:
        context.state["forced_terminal_tool"] = "submit_endpoint_drafts"
    return {
        "offset": offset,
        "next_offset": next_offset,
        "has_more": next_offset < len(text),
        "content": redact_text(chunk),
    }


def submit_endpoint_drafts(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolExecutionResult:
    document_text = str(context.state.get("document_text") or "")
    if document_text:
        next_unread = first_unread_offset(
            context.state.get("document_read_ranges"),
            len(document_text),
        )
        if next_unread < len(document_text):
            return ToolExecutionResult(
                content=(
                    '{"error":"document_not_fully_read","detail":"接口草稿提交前必须读取完整文档",'
                    f'"next_offset":{next_unread},"document_length":{len(document_text)}}}'
                ),
                is_error=True,
            )
    submitted = context.state.setdefault("endpoint_drafts_by_key", {})
    if not isinstance(submitted, dict):
        submitted = {}
        context.state["endpoint_drafts_by_key"] = submitted
    for endpoint in arguments["endpoints"]:
        method = str(endpoint.get("method") or "").strip().upper()
        path = str(endpoint.get("path") or "").strip()
        if not method or not path:
            continue
        normalized = dict(endpoint)
        normalized["method"] = method
        normalized["path"] = path
        submitted[f"{method} {path}"] = normalized

    expected_keys = {
        f"{str(item.get('method') or '').strip().upper()} {str(item.get('path') or '').strip()}"
        for item in context.state.get("expected_endpoint_keys") or []
        if isinstance(item, dict) and item.get("method") and item.get("path")
    }
    missing = sorted(expected_keys - set(submitted))
    if missing:
        import json

        context.state["endpoint_submission_checkpoint"] = {
            "accepted": sorted(submitted),
            "missing": missing,
        }
        return ToolExecutionResult(
            content=json.dumps(
                {
                    "error": "incomplete_endpoint_inventory",
                    "detail": "本批接口草稿已保留，请只补交 missing 中的接口，不必重复已接收接口。",
                    "accepted_count": len(submitted),
                    "missing": missing,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            is_error=True,
        )
    endpoints = list(submitted.values())
    return ToolExecutionResult(
        content=f"已接收接口草稿，共 {len(endpoints)} 个接口。",
        value=endpoints,
        terminal=True,
    )


def submit_dependency_graph(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolExecutionResult:
    edges = arguments["edges"]
    return ToolExecutionResult(
        content=f"已接收依赖图，共 {len(edges)} 条边。",
        value={"edges": edges},
        terminal=True,
    )


def submit_result_analysis(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolExecutionResult:
    return ToolExecutionResult(content="已接收执行分析。", value=arguments, terminal=True)


def submit_result_review(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolExecutionResult:
    return ToolExecutionResult(content="已接收失败复核。", value=arguments, terminal=True)


def get_audit_artifact_info(arguments: dict[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
    import hashlib

    text = str(context.state.get("audit_artifact_text") or "")
    return {
        "length_chars": len(text),
        "recommended_chunk_chars": min(180000, max(12000, len(text))),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def read_audit_artifact_chunk(arguments: dict[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
    text = str(context.state.get("audit_artifact_text") or "")
    ranges = context.state.setdefault("audit_artifact_read_ranges", [])
    next_unread = first_unread_offset(ranges, len(text))
    if next_unread >= len(text):
        context.state["forced_terminal_tool"] = "submit_quality_review"
        return {
            "offset": len(text),
            "next_offset": len(text),
            "has_more": False,
            "already_fully_read": True,
            "required_next_action": "submit_quality_review",
            "content": "",
        }
    # 审计必须连续读完整包；忽略重复或跳页 offset，直接推进到首个未读位置，
    # 避免模型反复读取同一证据导致上下文和 Token 浪费。
    offset = next_unread
    max_chars = max(60000, min(180000, int(arguments.get("max_chars") or 12000)))
    chunk = text[offset : offset + max_chars]
    next_offset = offset + len(chunk)
    if isinstance(ranges, list) and next_offset > offset:
        ranges.append((offset, next_offset))
    if next_offset >= len(text):
        context.state["forced_terminal_tool"] = "submit_quality_review"
    return {
        "offset": offset,
        "next_offset": next_offset,
        "has_more": next_offset < len(text),
        "content": redact_text(chunk),
    }


def submit_quality_review(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolExecutionResult:
    artifact_text = str(context.state.get("audit_artifact_text") or "")
    if artifact_text:
        next_unread = first_unread_offset(
            context.state.get("audit_artifact_read_ranges"),
            len(artifact_text),
        )
        if next_unread < len(artifact_text):
            return ToolExecutionResult(
                content=(
                    '{"error":"audit_artifact_not_fully_read",'
                    '"detail":"提交质量评分前必须读取完整审计证据包",'
                    f'"next_offset":{next_unread},"artifact_length":{len(artifact_text)}}}'
                ),
                is_error=True,
            )
    return ToolExecutionResult(content="已接收质量审查裁决。", value=arguments, terminal=True)


def submit_context_decisions(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolExecutionResult:
    return ToolExecutionResult(content="已接收经验治理决策。", value=arguments, terminal=True)


def submit_conflict_reviews(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolExecutionResult:
    return ToolExecutionResult(content="已接收冲突复核结论。", value=arguments["reviews"], terminal=True)


def object_schema(properties: dict[str, Any], *, required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def first_unread_offset(value: Any, total: int) -> int:
    ranges = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                try:
                    start, end = max(0, int(item[0])), min(total, int(item[1]))
                except (TypeError, ValueError):
                    continue
                if end > start:
                    ranges.append((start, end))
    cursor = 0
    for start, end in sorted(ranges):
        if start > cursor:
            break
        cursor = max(cursor, end)
        if cursor >= total:
            return total
    return cursor
