from __future__ import annotations

import re
from typing import Any

from ai_apitest.core.models import TestCase
from ai_apitest.core.artifact_language import (
    SUPPORTED_ARTIFACT_LANGUAGES,
    current_artifact_language,
    translate_generated_text,
)

PRIORITIES = ("P0", "P1", "P2", "P3")

PRIORITY_ALIASES = {
    "p0": "P0",
    "blocker": "P0",
    "critical": "P0",
    "fatal": "P0",
    "致命": "P0",
    "阻塞": "P0",
    "p1": "P1",
    "high": "P1",
    "major": "P1",
    "高": "P1",
    "重要": "P1",
    "p2": "P2",
    "medium": "P2",
    "normal": "P2",
    "中": "P2",
    "一般": "P2",
    "p3": "P3",
    "low": "P3",
    "minor": "P3",
    "低": "P3",
}

DESIGN_METHOD_ALIASES = {
    "happy": "happy_path",
    "happyPath": "happy_path",
    "normal": "happy_path",
    "success": "happy_path",
    "optional": "optional_field",
    "optionalField": "optional_field",
    "typeError": "type_error",
    "boundary": "boundary_value",
    "boundaryValue": "boundary_value",
    "pathParameterError": "path_parameter_error",
    "path_param_error": "path_parameter_error",
    "dependency": "dependency_chain",
    "dependencyChain": "dependency_chain",
    "auth": "auth_error",
    "authError": "auth_error",
    "equivalence": "equivalence_class",
    "equivalenceClass": "equivalence_class",
    "statusCodeCoverage": "status_code_coverage",
    "status_coverage": "status_code_coverage",
}

ALLOWED_DESIGN_METHODS = {
    "happy_path",
    "optional_field",
    "required_field",
    "type_error",
    "boundary_value",
    "path_parameter_error",
    "dependency_chain",
    "auth_error",
    "equivalence_class",
    "status_code_coverage",
}

RISK_ALIASES = {
    "": "none",
    "no": "none",
    "false": "none",
    "none": "none",
    "low": "none",
    "negative": "negative_behavior",
    "negative_case": "negative_behavior",
    "destructive": "destructive",
    "delete": "destructive",
    "data": "data_dependent",
    "data_dependent": "data_dependent",
    "data_dependent_without_fixture": "data_dependent_without_fixture",
    "behavior": "behavior_dependent",
    "behavior_dependent": "behavior_dependent",
    "env": "env_dependent",
    "environment": "env_dependent",
    "env_dependent": "env_dependent",
    "documented_response_without_fixture": "documented_response_without_fixture",
    "shared_rate_limit": "shared_rate_limit",
    "fixture_required": "fixture_required",
    "time_control": "fixture_required",
    "mail_injection": "fixture_required",
    "isolated_rate_limit": "shared_rate_limit",
}

EXTERNAL_TEST_CAPABILITY_LABELS = {
    "time_control": "服务端时间控制能力",
    "mail_injection": "受控邮件数据投递能力",
    "isolated_rate_limit": "隔离的限流测试环境",
    "reserved_mailbox_prefix": "真实保留前缀测试数据",
    "restricted_prefix": "当前受限前缀测试数据",
}

_EXTERNAL_TEST_CAPABILITY_TRANSLATIONS = {
    "time_control": {"en": "Server-side time control", "zh": "服务端时间控制能力", "ja": "サーバー時刻制御", "fr": "Contrôle du temps serveur", "ru": "Управление серверным временем"},
    "mail_injection": {"en": "Controlled email-data delivery", "zh": "受控邮件数据投递能力", "ja": "制御可能なメールデータ投入", "fr": "Injection contrôlée de données d’e-mail", "ru": "Управляемая доставка почтовых данных"},
    "isolated_rate_limit": {"en": "Isolated rate-limit environment", "zh": "隔离的限流测试环境", "ja": "分離されたレート制限環境", "fr": "Environnement de limitation isolé", "ru": "Изолированное окружение ограничения частоты"},
    "reserved_mailbox_prefix": {"en": "Real reserved-prefix test data", "zh": "真实保留前缀测试数据", "ja": "実際の予約済みプレフィックスデータ", "fr": "Donnée réelle de préfixe réservé", "ru": "Реальные данные зарезервированного префикса"},
    "restricted_prefix": {"en": "Current restricted-prefix test data", "zh": "当前受限前缀测试数据", "ja": "現在制限中のプレフィックスデータ", "fr": "Donnée actuelle de préfixe restreint", "ru": "Текущие данные запрещённого префикса"},
    "fallback": {"en": "Additional controlled scenario setup", "zh": "额外的受控场景准备能力", "ja": "追加の制御可能なシナリオ準備", "fr": "Préparation contrôlée supplémentaire", "ru": "Дополнительная управляемая подготовка сценария"},
}

_LEGACY_FIXTURE_INSTRUCTIONS = {
    "由 time_control 夹具冻结或推进服务端时间，建立用例所需的生命周期状态。",
    "由 mail_injection 夹具向目标邮箱投递受控邮件数据，再执行接口校验。",
    "由 isolated_rate_limit 夹具在隔离限流空间内构造请求频率，禁止污染共享 IP 配额。",
    "由 reserved_mailbox_prefix 夹具提供服务端真实保留前缀，并注入 query.prefix 参数。",
    "由 restricted_prefix 夹具提供服务端当前受限前缀，并注入 query.prefix 参数。",
    "夹具必须准备与当前 path_params 对应的同一资源；若夹具动态分配资源，执行前须用夹具实际值替换对应路径参数。",
}


def external_test_capability_labels(
    requirements: Any,
    language: str | None = None,
) -> list[str]:
    values = requirements if isinstance(requirements, list) else []
    locale = language or current_artifact_language()
    labels = []
    for requirement in values:
        translations = _EXTERNAL_TEST_CAPABILITY_TRANSLATIONS.get(
            str(requirement),
            _EXTERNAL_TEST_CAPABILITY_TRANSLATIONS["fallback"],
        )
        label = translations.get(locale, translations["en"])
        if label not in labels:
            labels.append(label)
    return labels


def external_test_capability_summary(
    requirements: Any,
    language: str | None = None,
) -> str:
    return ", ".join(external_test_capability_labels(requirements, language))


def apply_external_test_capability_guidance(case: TestCase) -> None:
    if not case.fixture_requirements:
        return
    case.fixture_requirements = list(
        dict.fromkeys(str(item) for item in case.fixture_requirements if str(item))
    )

    # Preserve the language already carried by historical guidance. Legacy
    # fixture wording predates language metadata and was always Chinese.
    suffix_source = "（依赖项目方提供的外部测试能力）"
    guidance_language = current_artifact_language()
    for language in SUPPORTED_ARTIFACT_LANGUAGES:
        if case.title.endswith(translate_generated_text(suffix_source, language)):
            guidance_language = language
            break
    legacy_fixture_wording = bool(
        re.search(r"（需 [^（）]+ 夹具验证）$", case.title)
        or any("测试夹具" in item for item in case.preconditions)
        or any(item in _LEGACY_FIXTURE_INSTRUCTIONS or " 夹具" in item for item in case.test_steps)
    )
    if legacy_fixture_wording:
        guidance_language = "zh"

    # 清理旧版本自动生成的夹具名称和启用说明，避免让用户误以为框架已内置这些组件。
    case.title = re.sub(r"\s*（需 [^（）]+ 夹具验证）$", "", case.title).strip()
    suffixes = generated_text_variants(suffix_source)
    if not any(case.title.endswith(suffix) for suffix in suffixes):
        suffix = translate_generated_text(
            suffix_source,
            guidance_language,
        )
        case.title = f"{case.title[: 120 - len(suffix)]}{suffix}"

    case.preconditions = [
        item
        for item in case.preconditions
        if not item.startswith("执行前必须启用以下测试夹具并建立标题所述状态：")
    ]
    precondition_source = (
        "本用例依赖项目方额外提供的受控测试能力；"
        "当前框架未内置该能力，项目未接入前不会自动执行。"
    )
    if not contains_generated_variant(case.preconditions, precondition_source):
        case.preconditions.append(
            translate_generated_text(
                precondition_source,
                guidance_language,
            )
        )

    case.test_steps = [
        item
        for item in case.test_steps
        if item not in _LEGACY_FIXTURE_INSTRUCTIONS
        and not re.fullmatch(r"由 .+ 夹具注入该场景所需的受控测试数据。", item)
    ]
    instruction_sources = {
        "time_control": "需由被测项目方提供服务端时间控制能力，以建立用例所需的生命周期状态。",
        "mail_injection": "需由被测项目方投递受控邮件数据，再执行接口校验。",
        "isolated_rate_limit": "需由被测项目方提供隔离的限流测试环境，避免污染共享 IP 配额。",
        "reserved_mailbox_prefix": "需由被测项目方提供真实保留前缀测试数据，并注入 query.prefix 参数。",
        "restricted_prefix": "需由被测项目方提供当前受限前缀测试数据，并注入 query.prefix 参数。",
    }
    guidance = []
    for requirement in case.fixture_requirements:
        source = instruction_sources.get(
            requirement,
            "需由被测项目方提供该场景所需的受控测试数据或环境能力。",
        )
        if contains_generated_variant(case.test_steps, source):
            continue
        instruction = translate_generated_text(
            source,
            guidance_language,
        )
        if instruction not in guidance:
            guidance.append(instruction)
    if case.path_params:
        path_guidance = (
            "项目方提供的外部测试能力必须准备与当前 path_params 对应的同一资源；"
            "如动态分配资源，执行前须用实际值替换对应路径参数。"
        )
        if not contains_generated_variant(case.test_steps, path_guidance):
            guidance.insert(
                0,
                translate_generated_text(
                    path_guidance,
                    guidance_language,
                ),
            )
    case.test_steps = [
        *(item for item in guidance if item not in case.test_steps),
        *case.test_steps,
    ]
    case.runnable = False
    if str(case.risk or "").strip().lower() in {"", "none", "low"}:
        case.risk = "fixture_required"


def generated_text_variants(source: str) -> set[str]:
    return {
        translate_generated_text(source, language)
        for language in SUPPORTED_ARTIFACT_LANGUAGES
    }


def contains_generated_variant(items: list[str], source: str) -> bool:
    variants = generated_text_variants(source)
    return any(str(item) in variants for item in items)


def normalize_case_id(value: Any, module: str, index: int, *, preserve_valid: bool = False) -> str:
    candidate = str(value or "").strip().upper()
    module = normalize_module(module)
    if preserve_valid and re.fullmatch(rf"TC-{re.escape(module)}-\d{{3,}}", candidate):
        return candidate
    return f"TC-{module}-{index:04d}"


def normalize_module(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "_", str(value or "API")).strip("_").upper()
    return text or "API"


def normalize_title(value: Any, fallback: str) -> str:
    title = re.sub(r"\s+", " ", str(value or fallback or "未命名用例")).strip()
    return title[:120]


def normalize_priority(value: Any, *, negative: bool = False, design_method: str = "") -> str:
    text = str(value or "").strip()
    key = text.lower().replace(" ", "_")
    if key in PRIORITY_ALIASES:
        return PRIORITY_ALIASES[key]
    if text.upper() in PRIORITIES:
        return text.upper()
    if design_method in {"happy_path", "dependency_chain"} and not negative:
        return "P0"
    if design_method in {"optional_field", "equivalence_class", "status_code_coverage", "auth_error"}:
        return "P1"
    if negative or design_method in {"type_error", "boundary_value", "path_parameter_error", "required_field"}:
        return "P2"
    return "P1"


def normalize_design_method(value: Any) -> str:
    raw = str(value or "happy_path").strip()
    mapped = DESIGN_METHOD_ALIASES.get(raw) or DESIGN_METHOD_ALIASES.get(raw.lower())
    if mapped:
        return mapped
    normalized = to_snake(raw)
    return normalized if normalized in ALLOWED_DESIGN_METHODS else "happy_path"


def normalize_tags(tags: Any, *, design_method: str, source_tag: str = "") -> list[str]:
    values = tags if isinstance(tags, list) else []
    normalized = []
    for item in values:
        tag = to_snake(str(item))
        if tag and tag not in normalized:
            normalized.append(tag)
    for required in [design_method, source_tag]:
        tag = to_snake(required)
        if tag and tag not in normalized:
            normalized.append(tag)
    return normalized


def normalize_risk(value: Any, *, runnable: bool, negative: bool) -> str:
    key = str(value or "").strip().lower().replace(" ", "_")
    if key in RISK_ALIASES:
        return RISK_ALIASES[key]
    if not runnable:
        return "data_dependent" if not negative else "negative_behavior"
    return "negative_behavior" if negative else "none"


def to_snake(value: str) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    text = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "_", text).strip("_").lower()
    return text


def normalize_test_case(
    case: TestCase,
    *,
    module: str = "",
    index: int = 1,
    source_tag: str = "",
    preserve_valid_id: bool = False,
) -> TestCase:
    apply_external_test_capability_guidance(case)
    design_method = normalize_design_method(case.design_method)
    negative = bool(case.negative)
    runnable = bool(case.runnable)
    module_name = module or case_module_from_path(case.path)
    case.id = normalize_case_id(case.id, module_name, index, preserve_valid=preserve_valid_id)
    case.title = normalize_title(case.title, f"{case.method} {case.path} - {design_method}")
    case.priority = normalize_priority(case.priority, negative=negative, design_method=design_method)
    case.design_method = design_method
    case.tags = normalize_tags(case.tags, design_method=design_method, source_tag=source_tag)
    if negative and "negative" not in case.tags:
        case.tags.append("negative")
    case.risk = normalize_risk(case.risk, runnable=runnable, negative=negative)
    return case


def normalize_cases(cases: list[TestCase], *, source_tag: str = "") -> list[TestCase]:
    counters: dict[str, int] = {}
    used_ids: set[str] = set()
    normalized = []
    for case in cases:
        module = case_module_from_path(case.path)
        counters[module] = counters.get(module, 0) + 1
        candidate = str(case.id or "").strip().upper()
        can_preserve = bool(
            candidate not in used_ids
            and re.fullmatch(rf"TC-{re.escape(module)}-\d{{3,}}", candidate)
        )
        if not can_preserve:
            while f"TC-{module}-{counters[module]:04d}" in used_ids:
                counters[module] += 1
        item = normalize_test_case(
            case,
            module=module,
            index=counters[module],
            source_tag=source_tag,
            preserve_valid_id=can_preserve,
        )
        used_ids.add(item.id)
        normalized.append(item)
    return normalized


def case_module_from_path(path: str) -> str:
    parts = [part for part in str(path or "").split("/") if part and not part.startswith("{")]
    if not parts:
        return "API"
    return normalize_module(parts[0])
