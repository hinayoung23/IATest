from __future__ import annotations
from contextvars import copy_context

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from ai_apitest.agents.identity import RoleRegistry, default_role_registry
from ai_apitest.ai.llm_config import is_llm_configured, normalize_llm_config
from ai_apitest.ai.model_gateway import ModelGatewayProtocol, ProviderModelGateway
from ai_apitest.application.governance import review_quality_with_agent_runtime
from ai_apitest.core.config import load_config
from ai_apitest.core.models import Endpoint
from ai_apitest.core.serde import case_from_dict, to_jsonable
from ai_apitest.data.agent_runs import redact_sensitive
from ai_apitest.data.quality_audits import QualityAuditRepository
from ai_apitest.domain.case_drafts import case_execution_signature, step_execution_payload
from ai_apitest.tools.builtin import build_case_generation_tool_registry
from ai_apitest.tools.registry import ToolRegistry
from ai_apitest.agents.token_optimization import TokenOptimization, canonical, content_hash, pack_evidence
from ai_apitest.application.incremental_audit import incremental_bundle


GatewayFactory = Callable[[dict[str, Any]], ModelGatewayProtocol]
DIMENSIONS = ("reliability", "completeness", "reasonableness", "non_redundancy")
RUBRIC_VERSION = "2026-10-14.17"

ARTIFACT_RUBRICS = {
    "endpoint_catalog": (
        "依据原始文档核对 method/path、参数约束、鉴权、响应状态码、完整 Schema 和可观察业务规则。"
        "不得凭常识补字段、接口路径、业务默认值或时间窗口。规则须有 source_evidence；"
        "不同来源条件即使响应相同也应保留血缘。跨接口后置行为可在产生状态转换的规则中以"
        "trigger.chain_steps 表达，但只能引用契约中真实存在的接口和字段。参数约束留在 Schema，"
        "不必重复为业务规则。规则标题不必重复所属接口名。未给出可读配置值的符号规则可标为"
        "testable=false；依赖外部测试能力的规则可标为 runnable=false，不能虚构能力或数值。"
        "逐步骤 assertions、risk=none/fixture_required/shared_rate_limit 和内置模板变量均为框架原生结构。"
        "sorted_desc_by、sorted_asc_by、is_positive_integer 均为原生 json_path 运算符。"
        "只审计文档中可观察的行为，不能要求直接校验未暴露的内部状态。"
    ),
    "test_cases": (
        "依据可信接口契约和 coverage_manifest 核对场景、请求、状态码、完整响应 Schema、变量流、"
        "setup/cleanup、覆盖缺口和执行语义重复。不得重新解释原始文档或虚构边界值。"
        "CHAIN 顶层 assertions 可为空，应检查 steps 逐步断言。主请求 JSON 顶层字段会进入 cleanup"
        "上下文；setup/CHAIN 的跨步骤变量应显式 extract。{{timestamp}} 是十进制数字字符串，"
        "{{uuid}}/{{random}} 在同条用例中保持稳定。集合可为空时应用 items Schema 校验现有元素，"
        "不能强制访问第一项；只有 required 声明的字段必须存在。依据 min/max 严格区分合法与非法边界。"
        "HTTP 类型转换与 URL encode 应依据参数序列化规则判断。明确依赖夹具的覆盖项允许"
        "runnable=false；实际执行覆盖和计划覆盖应区分。缺少安全触发条件的负向状态不能伪造请求。"
        "重复场景必须逐项比较 method/path、参数、断言、变量流和最终状态，并保留合并后的 rule_ids。"
        "同一前置步骤服务不同下游行为不构成重复。相对状态断言应以文档可观察字段和契约依据评判，"
        "不能把假设的额外字段作为必须校验项。发现契约缺失应报告上游缺陷。"
    ),
    "test_report": (
        "核对统计数字、执行证据、失败归因、用例错误与接口缺陷的区分、建议可执行性以及"
        "是否存在无证据结论。原始 pass/fail 与请求响应证据不可被改写。"
    ),
    "experiences": (
        "核对每条经验的来源可追溯性、结论是否能由证据推出、可复用范围、与已有经验的重复"
        "或冲突，以及是否把偶发现象错误提升为长期规则。"
    ),
}


@dataclass(frozen=True, slots=True)
class AuditModel:
    key: str
    provider: str
    base_url: str
    model: str
    config_roles: tuple[str, ...]
    config: dict[str, Any]


@dataclass(frozen=True, slots=True)
class QualityAuditPolicy:
    enabled: bool = True
    threshold: float = 85.0
    reliability_min: float = 90.0
    completeness_min: float = 85.0
    max_score_spread: float = 15.0
    max_rounds: int = 3
    judge_max_attempts: int = 2
    require_all_models: bool = True
    critical_veto: bool = True
    parallel_judges: bool = True
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "reliability": 0.40,
            "completeness": 0.30,
            "reasonableness": 0.20,
            "non_redundancy": 0.10,
        }
    )

    @classmethod
    def from_config(cls, value: dict[str, Any] | None) -> "QualityAuditPolicy":
        raw = value or {}
        weights = normalize_weights(raw.get("weights"))
        return cls(
            enabled=bool(raw.get("enabled", True)),
            threshold=bounded_float(raw.get("threshold"), 85.0, 0.0, 100.0),
            reliability_min=bounded_float(raw.get("reliability_min"), 90.0, 0.0, 100.0),
            completeness_min=bounded_float(raw.get("completeness_min"), 85.0, 0.0, 100.0),
            max_score_spread=bounded_float(raw.get("max_score_spread"), 15.0, 0.0, 100.0),
            max_rounds=bounded_int(raw.get("max_rounds"), 3, 1, 3),
            judge_max_attempts=bounded_int(raw.get("judge_max_attempts"), 2, 1, 3),
            # 这是交付准入条件，不允许通过配置绕过。
            require_all_models=True,
            critical_veto=True,
            parallel_judges=bool(raw.get("parallel_judges", True)),
            weights=weights,
        )


@dataclass(slots=True)
class JudgeScore:
    judge_key: str
    provider: str
    model: str
    status: str
    dimensions: dict[str, float] = field(default_factory=dict)
    total_score: float | None = None
    confidence: str = ""
    summary: str = ""
    issues: list[dict[str, Any]] = field(default_factory=list)
    agent_run_id: str = ""
    error: str = ""
    cached: bool = False
    audit_scope: dict[str, Any] = field(default_factory=lambda: {"mode": "full"})


@dataclass(slots=True)
class AuditRoundResult:
    session_id: str
    candidate_id: str
    artifact_type: str
    round_index: int
    artifact: Any
    hard_gate_pass: bool
    hard_gate_issues: list[dict[str, Any]]
    scores: list[JudgeScore]
    audit_complete: bool
    passed: bool
    average_score: float | None
    dimension_averages: dict[str, float]
    score_spread: float | None
    critical_issues: list[dict[str, Any]]
    decision_reasons: list[str]

    def repair_feedback(self) -> dict[str, Any]:
        issues = merge_issues(
            [
                *self.hard_gate_issues,
                *(issue for score in self.scores for issue in score.issues),
            ]
        )
        return {
            "quality_audit_round": self.round_index,
            "average_score": self.average_score,
            "dimension_averages": self.dimension_averages,
            "decision_reasons": self.decision_reasons,
            "issues": issues,
            "instruction": "只修复有证据的问题，保留已经正确的内容；不得为了提高分数增加无依据内容。",
        }

    @property
    def safe_for_fallback(self) -> bool:
        return self.hard_gate_pass and not self.critical_issues and self.audit_complete

    @property
    def rank(self) -> tuple[float, float, float, int, int]:
        return (
            float(self.average_score if self.average_score is not None else -1),
            float(self.dimension_averages.get("reliability", -1)),
            float(self.dimension_averages.get("completeness", -1)),
            -len(self.critical_issues),
            -sum(len(score.issues) for score in self.scores),
        )


class QualityAuditService:
    def __init__(
        self,
        root: Path,
        project: str,
        artifact_type: str,
        *,
        endpoints: list[Endpoint] | None = None,
        gateway_factory: GatewayFactory = ProviderModelGateway,
        role_registry: RoleRegistry | None = None,
        tool_registry: ToolRegistry | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if artifact_type not in ARTIFACT_RUBRICS:
            raise ValueError(f"不支持的质量审计产物类型：{artifact_type}")
        self.root = root
        self.project = project
        self.artifact_type = artifact_type
        self.endpoints = endpoints or []
        self.gateway_factory = gateway_factory
        self.roles = role_registry or default_role_registry()
        self.tools = tool_registry or build_case_generation_tool_registry()
        config = load_config(root)
        self.optimization = TokenOptimization.from_config(config.get("agents", {}))
        self.policy = QualityAuditPolicy.from_config(config.get("quality_audit"))
        artifact_enabled = bool(
            config.get("quality_audit", {}).get("artifacts", {}).get(artifact_type, True)
        )
        self.active = self.policy.enabled and artifact_enabled
        self.models = (
            unique_audit_models(config.get("llm", {}))
            if self.active
            else []
        )
        source = Path(__file__).resolve().parents[1]
        cache_context = content_hash({
            "rubric": RUBRIC_VERSION,
            "policy": asdict(self.policy),
            "role": asdict(self.roles.resolve("quality_reviewer")),
            "tools": [asdict(tool) | {"handler": getattr(tool.handler, "__qualname__", type(tool.handler).__qualname__)} for tool in self.tools.all() if "quality_reviewer" in tool.allowed_roles],
            "code": {str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(source.rglob("*.py"))},
            "optimization": asdict(self.optimization),
        })
        self.models = [replace(model, key=content_hash({"judge": model.key, "context": cache_context, "model_config": {key: value for key, value in model.config.items() if "key" not in key.lower()}})) for model in self.models]
        self.repository = QualityAuditRepository(root, project)
        self.session_id = self.repository.start_session(
            artifact_type=artifact_type,
            threshold=self.policy.threshold,
            max_rounds=self.policy.max_rounds,
            unique_model_count=len(self.models),
            metadata={**(metadata or {}), "rubric_version": RUBRIC_VERSION},
        )
        self.rounds: list[AuditRoundResult] = []

    def evaluate(
        self,
        artifact: Any,
        *,
        evidence: Any,
        round_index: int,
        hard_gate_issues: list[dict[str, Any]] | None = None,
        producer_model_key: str = "",
        producer_run_id: str = "",
        stream_callback: Callable[[str], None] | None = None,
    ) -> AuditRoundResult:
        gate_issues = normalize_issues(hard_gate_issues or [], default_dimension="reliability")
        hard_gate_pass = not any(issue["severity"] in {"critical", "high"} for issue in gate_issues)
        candidate_id = self.repository.add_candidate(
            self.session_id,
            round_index=round_index,
            artifact=to_jsonable(artifact),
            evidence=to_jsonable(evidence),
            rubric_version=RUBRIC_VERSION,
            producer_model_key=producer_model_key,
            producer_run_id=producer_run_id,
            hard_gate_pass=hard_gate_pass,
            hard_gate_issues=gate_issues,
        )
        if not hard_gate_pass:
            result = self._finish_round(
                candidate_id,
                artifact,
                round_index,
                gate_issues,
                [],
                audit_complete=False,
                decision_reasons=["本地确定性硬门禁未通过，跳过模型审计并进入定向修订。"],
            )
            self.rounds.append(result)
            return result

        if not self.active or not self.models:
            reason = (
                "该产物的质量审计已停用，仅依据本地确定性门禁交付。"
                if not self.active
                else "未配置可用的唯一审计模型，仅依据本地确定性门禁交付。"
            )
            result = self._finish_round(
                candidate_id,
                artifact,
                round_index,
                gate_issues,
                [],
                audit_complete=True,
                decision_reasons=[reason],
                force_pass=True,
            )
            self.rounds.append(result)
            return result

        if stream_callback:
            stream_callback(
                f"开始第 {round_index}/{self.policy.max_rounds} 轮{artifact_label(self.artifact_type)}质量审计："
                f"{len(self.models)} 个唯一模型。\n"
            )
        bundle = {
            "artifact_type": self.artifact_type,
            "round_index": round_index,
            "rubric_version": RUBRIC_VERSION,
            "rubric": ARTIFACT_RUBRICS[self.artifact_type],
            "artifact": to_jsonable(artifact),
            "evidence": to_jsonable(evidence),
            "hard_gate_issues": gate_issues,
        }
        safe_bundle = redact_sensitive(bundle)
        source_equivalent = canonical(safe_bundle) == canonical(bundle)
        artifact_text = json.dumps(
            safe_bundle,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        candidate = self.repository.get_candidate(candidate_id) or {}
        audit_input_hash = str(candidate.get("audit_input_hash") or "")
        scores = self._review_all(
            candidate_id,
            artifact_text,
            audit_input_hash,
            round_index,
            allow_incremental=source_equivalent,
        )
        audit_complete = all(score.status == "completed" for score in scores) and (
            len(scores) == len(self.models) if self.policy.require_all_models else bool(scores)
        )
        reasons: list[str] = []
        if not audit_complete:
            failed = [score.model for score in scores if score.status != "completed"]
            reasons.append("审计模型未全部完成：" + "、".join(failed or ["未知模型"]))
        result = self._finish_round(
            candidate_id,
            artifact,
            round_index,
            gate_issues,
            scores,
            audit_complete=audit_complete,
            decision_reasons=reasons,
        )
        self.rounds.append(result)
        if (
            result.passed
            and self.optimization.enabled
            and self.optimization.incremental_audit
            and self.artifact_type == "test_cases"
            and source_equivalent
        ):
            for score in scores:
                if (
                    score.status == "completed"
                    and score.audit_scope.get("verdict") == "approved"
                    and score.confidence == "high"
                    and not score.issues
                    and score.total_score > self.policy.threshold
                    and score.dimensions["reliability"] >= self.policy.reliability_min
                    and score.dimensions["completeness"] >= self.policy.completeness_min
                ):
                    self.repository.save_baseline(score.judge_key, self.artifact_type, {
                        "candidate_id": candidate_id,
                        "judge_key": score.judge_key,
                        "dimensions": score.dimensions,
                        "bundle": json.loads(artifact_text),
                        "depth": score.audit_scope.get("baseline_depth", 0) + 1 if score.audit_scope.get("mode") == "incremental" else 0,
                    })
        if stream_callback:
            score_text = f"{result.average_score:.2f}" if result.average_score is not None else "-"
            stream_callback(
                f"第 {round_index} 轮质量审计完成：平均分 {score_text}，"
                f"{'通过' if result.passed else '需要修订'}。\n"
            )
        return result

    def best_safe_round(self) -> AuditRoundResult | None:
        safe = [item for item in self.rounds if item.safe_for_fallback]
        return max(safe, key=lambda item: item.rank) if safe else None

    def finalize(
        self,
        selected: AuditRoundResult | None,
        *,
        decision: str,
        reason: str,
    ) -> None:
        self.repository.finish_session(
            self.session_id,
            status="completed" if selected is not None else "failed",
            selected_candidate_id=selected.candidate_id if selected else "",
            final_score=selected.average_score if selected else None,
            decision=decision,
            reason=reason,
        )

    def _review_all(
        self,
        candidate_id: str,
        artifact_text: str,
        audit_input_hash: str,
        round_index: int,
        *,
        allow_incremental: bool = True,
    ) -> list[JudgeScore]:
        def review(model: AuditModel) -> JudgeScore:
            cached = self.repository.cached_score(audit_input_hash, model.key)
            if cached:
                score = score_from_cache(cached, model, self.policy.weights)
                self._record_score(candidate_id, score)
                return score
            wire_bundle = json.loads(artifact_text)
            scope = {"mode": "full"}
            baseline = None
            if self.optimization.enabled and self.optimization.incremental_audit and allow_incremental:
                baseline = self.repository.get_baseline(model.key, self.artifact_type)
                delta = incremental_bundle(wire_bundle, baseline) if baseline else None
                if delta:
                    wire_bundle, scope = delta
            if self.optimization.enabled and self.optimization.evidence_dedup:
                wire_bundle, packing = pack_evidence(wire_bundle)
                scope = {**scope, "evidence_packing": packing}
            wire_text = canonical(wire_bundle)
            errors: list[str] = []
            score: JudgeScore | None = None
            for attempt in range(1, self.policy.judge_max_attempts + 1):
                try:
                    artifact = review_quality_with_agent_runtime(
                        self.root,
                        self.project,
                        review_type=f"{self.artifact_type}_round_{round_index}_attempt_{attempt}",
                        evidence={
                            "artifact_type": self.artifact_type,
                            "round_index": round_index,
                            "attempt": attempt,
                            "evidence_chars": len(wire_text),
                            "evidence_sha256": hashlib.sha256(wire_text.encode("utf-8")).hexdigest(),
                            "audit_scope": scope,
                        },
                        endpoints=self.endpoints,
                        llm={
                            **model.config,
                            "default_output_tokens": min(int(model.config.get("default_output_tokens") or 8000), self.optimization.audit_output_tokens * (2 ** (attempt - 1))) if self.optimization.enabled else max(16000, int(model.config.get("default_output_tokens") or 8000)),
                        },
                        rubric=ARTIFACT_RUBRICS[self.artifact_type],
                        calibration_feedback=errors[-1] if errors else "",
                        state={"audit_artifact_text": wire_text},
                        gateway_factory=self.gateway_factory,
                        role_registry=self.roles,
                        tool_registry=self.tools,
                    )
                    dimensions = normalize_dimensions(artifact.get("dimension_scores"))
                    issues = normalize_issues(artifact.get("issues") or [])
                    rubric_violations = review_rubric_violations(
                        artifact,
                        artifact_text,
                    )
                    if rubric_violations:
                        if attempt < self.policy.judge_max_attempts:
                            raise ValueError(
                                "审计裁决违反既定规则："
                                + "；".join(rubric_violations[:8])
                            )
                        dimensions, issues, calibrated_summary = calibrate_review_result(
                            artifact,
                            artifact_text,
                            dimensions,
                        )
                    else:
                        calibrated_summary = str(artifact.get("summary") or "")
                    if scope["mode"] == "incremental":
                        dimensions = {name: min(value, baseline["dimensions"][name]) for name, value in dimensions.items()}
                    score = JudgeScore(
                        judge_key=model.key,
                        provider=model.provider,
                        model=model.model,
                        status="completed",
                        dimensions=dimensions,
                        total_score=weighted_score(dimensions, self.policy.weights),
                        confidence=normalize_confidence(artifact.get("confidence")),
                        summary=calibrated_summary[:4000],
                        issues=issues,
                        agent_run_id=str(artifact.get("_agent_run_id") or ""),
                        audit_scope={**scope, "verdict": artifact.get("verdict")},
                    )
                    break
                except Exception as exc:
                    errors.append(f"第 {attempt} 次：{exc}")
            if score is None:
                score = JudgeScore(
                    judge_key=model.key,
                    provider=model.provider,
                    model=model.model,
                    status="failed",
                    error="；".join(errors),
                )
            self._record_score(candidate_id, score)
            return score

        if not self.policy.parallel_judges or len(self.models) <= 1:
            return [review(model) for model in self.models]
        results: dict[str, JudgeScore] = {}
        with ThreadPoolExecutor(max_workers=len(self.models), thread_name_prefix="quality-audit") as pool:
            futures = {pool.submit(copy_context().run, review, model): model for model in self.models}
            for future in as_completed(futures):
                model = futures[future]
                try:
                    results[model.key] = future.result()
                except Exception as exc:  # pragma: no cover - worker boundary
                    score = JudgeScore(
                        judge_key=model.key,
                        provider=model.provider,
                        model=model.model,
                        status="failed",
                        error=str(exc),
                    )
                    self._record_score(candidate_id, score)
                    results[model.key] = score
        return [results[model.key] for model in self.models]

    def _record_score(self, candidate_id: str, score: JudgeScore) -> None:
        self.repository.record_score(
            self.session_id,
            candidate_id,
            judge_key=score.judge_key,
            provider=score.provider,
            model=score.model,
            status=score.status,
            dimensions=score.dimensions,
            total_score=score.total_score,
            confidence=score.confidence,
            summary=score.summary,
            issues=score.issues,
            agent_run_id=score.agent_run_id,
            error=score.error,
            audit_scope=score.audit_scope,
        )

    def _finish_round(
        self,
        candidate_id: str,
        artifact: Any,
        round_index: int,
        gate_issues: list[dict[str, Any]],
        scores: list[JudgeScore],
        *,
        audit_complete: bool,
        decision_reasons: list[str],
        force_pass: bool = False,
    ) -> AuditRoundResult:
        completed = [score for score in scores if score.status == "completed" and score.total_score is not None]
        averages = {
            dimension: round(
                sum(score.dimensions[dimension] for score in completed) / len(completed),
                2,
            )
            for dimension in DIMENSIONS
        } if completed else {}
        total_scores = [float(score.total_score) for score in completed if score.total_score is not None]
        average_score = round(sum(total_scores) / len(total_scores), 2) if total_scores else None
        spread = round(max(total_scores) - min(total_scores), 2) if total_scores else None
        critical = [
            issue
            for issue in [*gate_issues, *(item for score in completed for item in score.issues)]
            if issue.get("severity") == "critical"
        ]
        reasons = list(decision_reasons)
        if average_score is not None and average_score <= self.policy.threshold:
            reasons.append(f"平均分 {average_score:.2f} 未严格高于 {self.policy.threshold:.2f}。")
        if averages and averages.get("reliability", 0) < self.policy.reliability_min:
            reasons.append(
                f"可靠性平均分 {averages.get('reliability', 0):.2f} 低于 {self.policy.reliability_min:.2f}。"
            )
        if averages and averages.get("completeness", 0) < self.policy.completeness_min:
            reasons.append(
                f"完整性平均分 {averages.get('completeness', 0):.2f} 低于 {self.policy.completeness_min:.2f}。"
            )
        if spread is not None and spread > self.policy.max_score_spread:
            reasons.append(
                f"模型评分差值 {spread:.2f} 超过允许的 {self.policy.max_score_spread:.2f}。"
            )
        if critical and self.policy.critical_veto:
            reasons.append(f"存在 {len(critical)} 个有证据的 critical 问题。")
        passed = force_pass or (
            audit_complete
            and average_score is not None
            and average_score > self.policy.threshold
            and averages.get("reliability", 0) >= self.policy.reliability_min
            and averages.get("completeness", 0) >= self.policy.completeness_min
            and (spread is None or spread <= self.policy.max_score_spread)
            and (not critical or not self.policy.critical_veto)
        )
        self.repository.finish_candidate(
            candidate_id,
            audit_complete=audit_complete,
            passed=passed,
            average_score=average_score,
            dimension_averages=averages,
            score_spread=spread,
            critical_issue_count=len(critical),
        )
        return AuditRoundResult(
            session_id=self.session_id,
            candidate_id=candidate_id,
            artifact_type=self.artifact_type,
            round_index=round_index,
            artifact=artifact,
            hard_gate_pass=not any(issue["severity"] in {"critical", "high"} for issue in gate_issues),
            hard_gate_issues=gate_issues,
            scores=scores,
            audit_complete=audit_complete,
            passed=passed,
            average_score=average_score,
            dimension_averages=averages,
            score_spread=spread,
            critical_issues=critical,
            decision_reasons=unique_strings(reasons),
        )


def unique_audit_models(value: dict[str, Any]) -> list[AuditModel]:
    normalized = normalize_llm_config(value)
    grouped: dict[str, dict[str, Any]] = {}
    role_names: dict[str, list[str]] = {}
    for role in ("master", "case_generator", "analyzer"):
        config = normalized.get(role) or {}
        if not bool(config.get("enabled", False)) or not is_llm_configured(config):
            continue
        provider = str(config.get("provider") or "openai-compatible").strip().lower()
        base_url = str(config.get("base_url") or "").strip().rstrip("/").lower()
        model = str(config.get("model") or "").strip()
        key = f"{provider}|{base_url}|{model.lower()}"
        grouped.setdefault(key, dict(config))
        role_names.setdefault(key, []).append(role)
    return [
        AuditModel(
            key=key,
            provider=str(grouped[key].get("provider") or "openai-compatible"),
            base_url=str(grouped[key].get("base_url") or ""),
            model=str(grouped[key].get("model") or ""),
            config_roles=tuple(role_names[key]),
            config=grouped[key],
        )
        for key in sorted(grouped)
    ]


def normalize_dimensions(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        raise ValueError("质量审计缺少 dimension_scores")
    return {
        name: bounded_float(value.get(name), -1, 0, 100, fail=True)
        for name in DIMENSIONS
    }


def weighted_score(dimensions: dict[str, float], weights: dict[str, float]) -> float:
    return round(sum(dimensions[name] * weights[name] for name in DIMENSIONS), 2)


def review_rubric_violations(
    review: dict[str, Any],
    artifact_text: str,
) -> list[str]:
    """Reject judge findings that directly contradict the fixed audit rubric."""
    try:
        bundle = json.loads(artifact_text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    entities: list[dict[str, Any]] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("rule_id") or value.get("id"):
                entities.append(value)
            for nested in value.values():
                collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)

    artifact = bundle.get("artifact") if isinstance(bundle, dict) else bundle
    collect(artifact)
    violations: list[str] = []
    artifact_items = artifact if isinstance(artifact, list) else []
    case_by_id = {}
    for item in artifact_items:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        try:
            case_by_id[str(item["id"]).lower()] = case_from_dict(item)
        except (KeyError, TypeError, ValueError):
            continue
    endpoint_items = [
        item
        for item in artifact_items
        if (
            isinstance(item, dict)
            and item.get("method")
            and item.get("path")
            and isinstance(item.get("business_rules"), list)
        )
    ]
    if endpoint_items:
        summary = str(review.get("summary") or "")
        actual_endpoint_count = len(endpoint_items)
        actual_rule_count = sum(
            len(item.get("business_rules") or [])
            for item in endpoint_items
            if isinstance(item.get("business_rules"), list)
        )
        for claimed in re.findall(r"(\d+)\s*个接口", summary, flags=re.I):
            if int(claimed) != actual_endpoint_count:
                violations.append(
                    f"审计摘要声称有 {claimed} 个接口，实际为 {actual_endpoint_count} 个"
                )
        for claimed in re.findall(
            r"(\d+)\s*条\s*(?:business_rules?|业务规则)",
            summary,
            flags=re.I,
        ):
            if int(claimed) != actual_rule_count:
                violations.append(
                    f"审计摘要声称有 {claimed} 条业务规则，实际为 {actual_rule_count} 条"
                )
    has_51_character_prefix = any(
        isinstance(item, dict)
        and isinstance(item.get("query"), dict)
        and isinstance(item["query"].get("prefix"), str)
        and len(item["query"]["prefix"]) == 51
        for item in artifact_items
    )
    artifact_serialized = json.dumps(artifact, ensure_ascii=False, default=str)
    for issue in review.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        location = str(issue.get("location") or "").lower()
        text = " ".join(
            str(issue.get(key) or "")
            for key in ("evidence", "reason", "suggested_fix")
        ).lower()
        if (
            re.search(r"(?:无需|不需要|不必).{0,12}修复", str(issue.get("suggested_fix") or ""))
            and re.search(
                r"符合.{0,20}(?:规则|要求|预期)|(?:断言|设计|实现).{0,12}(?:合理|正确)|"
                r"不是.{0,8}问题|无.{0,8}问题",
                str(issue.get("reason") or ""),
            )
        ):
            violations.append(
                f"{location or '未知位置'}：正向确认且无需修复的内容不能作为质量问题"
            )
        mentioned_case_ids = unique_strings(
            re.findall(r"tc-api-\d+", f"{location} {text}", flags=re.I)
        )
        mentioned_cases = [
            case_by_id[item.lower()]
            for item in mentioned_case_ids
            if item.lower() in case_by_id
        ]
        if (
            str(issue.get("dimension") or "").lower() == "non_redundancy"
            and len(mentioned_cases) >= 2
            and re.search(r"重复|合并|删除\s*tc-api|只保留", text)
            and not cases_are_duplicate_or_subsumed(
                mentioned_cases[0],
                mentioned_cases[1],
            )
        ):
            violations.append(
                f"{location or '未知位置'}：两条用例的 HTTP 步骤或最终状态不同，不能按执行语义重复合并"
            )
        if (
            str(issue.get("dimension") or "").lower() == "non_redundancy"
            and re.search(
                r"不构成(?:完全)?重复|仅.+部分重叠|只是.+共享.+前置|"
                r"最终断言不同|验证目标不同|按规则不应合并",
                text,
            )
        ):
            violations.append(
                f"{location or '未知位置'}：审计理由已确认执行语义不重复，不能作为冗余问题"
            )
        if (
            re.search(r"sorted_(?:desc|asc)_by", text, flags=re.I)
            and re.search(r"自定义断言|不支持|缺乏.+执行器|行为不确定", text)
        ):
            violations.append(
                f"{location or '未知位置'}：排序运算符由执行器原生支持，不能按未知自定义断言扣分"
            )
        if (
            "{{random}}" in artifact_serialized
            and re.search(
                r"random.{0,240}(?:不自动|不保证|可能|会产生).{0,120}"
                r"(?:持久|相同|不同|不一致|无法复用)",
                text,
            )
        ):
            violations.append(
                f"{location or '未知位置'}：内置 random 在单用例上下文稳定，不能要求额外提取"
            )
        if (
            has_51_character_prefix
            and "51" in text
            and re.search(r"缺少|未覆盖|没有.+越界|未测试.{0,12}边界", text)
        ):
            violations.append(
                f"{location or '未知位置'}：候选已包含 51 字符越界值，不能报告该边界缺失"
            )
        if (
            has_51_character_prefix
            and "50" in text
            and "51" in text
            and re.search(
                r"50.{0,80}(?:不是|非|内点|重复|删除|改为).{0,80}(?:边界|51)|"
                r"(?:边界|51).{0,80}(?:删除|合并).{0,80}50",
                text,
            )
        ):
            violations.append(
                f"{location or '未知位置'}：50 字符是合法上边界，不能因另有 51 字符非法边界而删除"
            )
        if (
            "{{timestamp}}" in artifact_serialized
            and re.search(
                r"timestamp.{0,160}(?:可能|也许|通常).{0,100}"
                r"(?:包含|产生).{0,80}(?:点|冒号|非法字符|不匹配|\\.|:)",
                text,
                flags=re.I,
            )
        ):
            violations.append(
                f"{location or '未知位置'}：内置 timestamp 是纯十进制 time_ns 字符串，不能假设含非法字符"
            )
        if (
            re.search(r"testable\s*[:=]\s*false", text, flags=re.I)
            and re.search(
                r"未生成用例|无对应\s*case|case_ids?.{0,20}(?:空|缺少)|"
                r"coverage_gap|disposition",
                text,
                flags=re.I,
            )
        ):
            violations.append(
                f"{location or '未知位置'}：testable=false 规则不得要求生成伪造用例或 case_id"
            )
        if re.search(
            r"rule_id.+(?:大小写|大写前缀|小写前缀|命名风格)|"
            r"(?:大小写|大写前缀|小写前缀).+rule_id",
            text,
        ):
            violations.append(
                f"{location or '未知位置'}：稳定 rule_id 的大小写风格不影响事实或血缘，不能据此扣分"
            )
        issue_haystack = f"{location} {text}"
        matched = [
            entity
            for entity in entities
            if any(
                identifier
                and identifier.lower() in issue_haystack
                for identifier in (
                    str(entity.get("rule_id") or ""),
                    str(entity.get("id") or ""),
                )
            )
        ]
        for entity in matched:
            fixtures = entity.get("fixture_requirements")
            if (
                isinstance(fixtures, list)
                and fixtures
                and entity.get("runnable") is False
                and (
                    (
                        re.search(r"缺少|补充|增加|无法验证|覆盖不完整", text)
                        and re.search(
                            r"chain_steps|等待|time_control|mail_injection|邮件注入|注入.+邮件|"
                            r"触发.+清理|超过.+保留窗口",
                            text,
                            flags=re.I,
                        )
                    )
                    or re.search(
                        r"testable\s*=\s*true.+runnable\s*=\s*false.+(?:矛盾|冲突)",
                        text,
                        flags=re.I,
                    )
                )
            ):
                violations.append(
                    f"{location or '未知位置'}：不可因明确的夹具依赖和不可直接运行状态扣分"
                )
            if (
                isinstance(fixtures, list)
                and "mail_injection" in fixtures
                and entity.get("runnable") is False
                and re.search(r"数组元素|item\s*schema|\$\[0\]|第一个元素", text, flags=re.I)
            ):
                violations.append(
                    f"{location or '未知位置'}：无可执行邮件注入时数组允许为空，不能强制访问首元素"
                )
            chain_steps = (
                entity.get("trigger", {}).get("chain_steps")
                if isinstance(entity.get("trigger"), dict)
                else entity.get("steps")
            )
            if (
                isinstance(chain_steps, list)
                and len(chain_steps) >= 2
                and any(
                    isinstance(step, dict) and step.get("assertions")
                    for step in chain_steps
                )
                and re.search(
                    r"缺少.+后置|后置验证.+缺失|将后置验证链移至\s*chain_steps|"
                    r"expected.+仅.+未.+后置",
                    text,
                    flags=re.I,
                )
            ):
                violations.append(
                    f"{location or '未知位置'}：已有逐步断言，不能按顶层 expected 为空扣分"
                )
            serialized = json.dumps(entity, ensure_ascii=False, default=str).lower()
            if (
                re.search(r"(?:缺少|未).{0,30}(?:断言|校验).{0,30}(?:email|expires_in)|"
                          r"(?:email|expires_in).{0,30}(?:缺少|未).{0,30}(?:断言|校验)", text)
                and all(
                    field in serialized
                    for field in (
                        '"target": "$.email"',
                        '"target": "$.expires_in"',
                    )
                )
            ):
                violations.append(
                    f"{location or '未知位置'}：候选已包含 email 与 expires_in 的目标断言，不能报告缺失"
                )
            if (
                re.search(r"删除.{0,80}(?:message).{0,80}(?:eq|具体|精确)", text, flags=re.I)
                and isinstance(entity.get("steps"), list)
                and any(
                    isinstance(step, dict)
                    and str(step.get("method") or "").upper() in {"GET", "POST", "DELETE"}
                    and any(
                        isinstance(assertion, dict)
                        and assertion.get("type") == "status_code"
                        and assertion.get("expected") in {404, 410}
                        for assertion in step.get("assertions") or []
                    )
                    for step in entity["steps"]
                )
            ):
                violations.append(
                    f"{location or '未知位置'}：前置删除已成功且后续直接验证目标状态，不要求重复精确断言展示 message"
                )
            if (
                '"operator": "gte"' in serialized
                and re.search(r"(?:改为|增加).{0,20}(?:gt|>|大于)", text, flags=re.I)
            ):
                violations.append(
                    f"{location or '未知位置'}：秒级时间戳允许相等，不能强制把 gte 改为 gt"
                )
    return unique_strings(violations)


def calibrate_review_result(
    review: dict[str, Any],
    artifact_text: str,
    dimensions: dict[str, float],
) -> tuple[dict[str, float], list[dict[str, Any]], str]:
    """Remove only findings that contradict deterministic facts and rescore affected dimensions."""
    retained: list[dict[str, Any]] = []
    rejected: list[tuple[dict[str, Any], list[str]]] = []
    for raw in review.get("issues") or []:
        if not isinstance(raw, dict):
            continue
        violations = review_rubric_violations(
            {"summary": "", "issues": [raw]},
            artifact_text,
        )
        normalized = normalize_issues([raw])
        if violations:
            if normalized:
                rejected.append((normalized[0], violations))
        elif normalized:
            retained.append(normalized[0])

    calibrated = dict(dimensions)
    invalid_dimensions = {
        item.get("dimension")
        for item, _ in rejected
        if item.get("dimension") in DIMENSIONS
    }
    deductions = {"critical": 30.0, "high": 15.0, "warning": 5.0, "info": 1.0}
    for dimension in invalid_dimensions:
        calibrated[dimension] = max(
            0.0,
            100.0
            - sum(
                deductions.get(str(item.get("severity") or "warning"), 5.0)
                for item in retained
                if item.get("dimension") == dimension
            ),
        )

    original_summary = str(review.get("summary") or "").strip()
    summary_violations = review_rubric_violations(
        {"summary": original_summary, "issues": []},
        artifact_text,
    )
    detail = unique_strings(
        [
            violation
            for _, violations in rejected
            for violation in violations
        ]
    )
    note = (
        f"固定事实校准：剔除 {len(rejected)} 条与契约或执行器语义冲突的问题，"
        f"保留 {len(retained)} 条有效问题；仅重算受误报影响的维度。"
    )
    if detail:
        note += " " + "；".join(detail[:4])
    if summary_violations:
        note += " 原摘要中的数量/事实冲突已移除。"
        original_summary = ""
    summary = note if not original_summary else f"{note} 原模型摘要：{original_summary}"
    return calibrated, retained, summary


def cases_are_duplicate_or_subsumed(first: Any, second: Any) -> bool:
    if case_execution_signature(first) == case_execution_signature(second):
        return True
    return chain_case_is_prefix(first, second) or chain_case_is_prefix(second, first)


def chain_case_is_prefix(shorter: Any, longer: Any) -> bool:
    if (
        str(shorter.method).upper() != "CHAIN"
        or str(longer.method).upper() != "CHAIN"
        or not 1 < len(shorter.steps) < len(longer.steps)
        or shorter.runnable != longer.runnable
        or sorted(shorter.fixture_requirements) != sorted(longer.fixture_requirements)
        or to_jsonable(shorter.assertions) != to_jsonable(longer.assertions)
    ):
        return False
    return all(
        step_execution_payload(short_step) == step_execution_payload(long_step)
        for short_step, long_step in zip(shorter.steps, longer.steps)
    )


def normalize_issues(
    value: Any,
    *,
    default_dimension: str = "reasonableness",
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value[:500]:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity") or "warning").strip().lower()
        aliases = {"error": "high", "warn": "warning", "medium": "warning", "low": "info"}
        severity = aliases.get(severity, severity)
        if severity not in {"critical", "high", "warning", "info"}:
            severity = "warning"
        dimension = str(item.get("dimension") or default_dimension)
        if dimension not in DIMENSIONS:
            dimension = default_dimension
        location = str(item.get("location") or item.get("scenario_id") or item.get("case_id") or "").strip()
        evidence = str(item.get("evidence") or "").strip()
        reason = str(item.get("reason") or item.get("detail") or "").strip()
        suggested_fix = str(item.get("suggested_fix") or "").strip()
        if severity == "critical" and (not location or not evidence):
            severity = "high"
        result.append(
            {
                "severity": severity,
                "dimension": dimension,
                "location": location or "产物整体",
                "evidence": evidence,
                "reason": reason or "审计模型未提供具体原因",
                "suggested_fix": suggested_fix,
            }
        )
    return result


def merge_issues(value: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    order = {"critical": 0, "high": 1, "warning": 2, "info": 3}
    for item in sorted(normalize_issues(value), key=lambda issue: order[issue["severity"]]):
        key = (
            str(item.get("dimension") or ""),
            str(item.get("location") or ""),
            str(item.get("reason") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result[:200]


def score_from_cache(
    value: dict[str, Any],
    model: AuditModel,
    weights: dict[str, float],
) -> JudgeScore:
    dimensions = {
        name: float(value[name])
        for name in DIMENSIONS
    }
    return JudgeScore(
        judge_key=model.key,
        provider=model.provider,
        model=model.model,
        status="completed",
        dimensions=dimensions,
        total_score=weighted_score(dimensions, weights),
        confidence=str(value.get("confidence") or ""),
        summary=str(value.get("summary") or ""),
        issues=normalize_issues(value.get("issues_json") or []),
        agent_run_id=str(value.get("agent_run_id") or ""),
        cached=True,
        audit_scope=value.get("audit_scope_json") or {"mode": "full"},
    )


def normalize_weights(value: Any) -> dict[str, float]:
    defaults = {
        "reliability": 0.40,
        "completeness": 0.30,
        "reasonableness": 0.20,
        "non_redundancy": 0.10,
    }
    if not isinstance(value, dict):
        return defaults
    parsed = {
        name: bounded_float(value.get(name), defaults[name], 0.0, 1.0)
        for name in DIMENSIONS
    }
    total = sum(parsed.values())
    if total <= 0:
        return defaults
    return {name: parsed[name] / total for name in DIMENSIONS}


def normalize_confidence(value: Any) -> str:
    text = str(value or "medium").lower()
    return text if text in {"high", "medium", "low"} else "medium"


def bounded_float(
    value: Any,
    default: float,
    minimum: float,
    maximum: float,
    *,
    fail: bool = False,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        if fail:
            raise ValueError(f"质量审计分数无效：{value}")
        parsed = default
    if fail and not minimum <= parsed <= maximum:
        raise ValueError(f"质量审计分数超出范围：{parsed}")
    return max(minimum, min(maximum, parsed))


def bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def artifact_label(value: str) -> str:
    return {
        "endpoint_catalog": "接口信息",
        "test_cases": "测试用例",
        "test_report": "测试报告",
        "experiences": "经验候选",
    }.get(value, value)


def unique_strings(value: list[str]) -> list[str]:
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result
