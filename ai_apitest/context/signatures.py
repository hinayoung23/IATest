from __future__ import annotations

import hashlib
import re
from typing import Any


def normalize_claim(content: str) -> str:
    text = content.lower()
    text = re.sub(r"https?://\S+", "<url>", text)
    text = re.sub(r"\b\d{4}-\d{1,2}-\d{1,2}(?:[t\s]\d{1,2}:\d{1,2}:\d{1,2})?\b", "<date>", text)
    text = re.sub(r"\b\d{8}-\d{6}\b", "<date>", text)
    text = re.sub(r"\b[0-9a-f]{8,}\b", "<hex>", text)
    text = re.sub(r"\b\d+\b", "<num>", text)
    text = re.sub(r"\s+", " ", text).strip()
    sentences = re.split(r"[。.!?！？\n]+", text)
    return "。".join(sentence.strip() for sentence in sentences[:3] if sentence.strip())


def compute_exact_key(lesson: dict[str, Any]) -> str:
    normalized = normalize_claim(str(lesson.get("content", "")))
    claim_hash = hashlib.md5(normalized.encode("utf-8")).hexdigest()[:12]
    return ":".join(
        [
            str(lesson.get("scope", "")),
            str(lesson.get("method", "")),
            str(lesson.get("path", "")),
            str(lesson.get("lesson_type", "")),
            str(lesson.get("environment", "")),
            claim_hash,
        ]
    )


def compute_semantic_key(lesson: dict[str, Any]) -> str:
    content = normalize_claim(str(lesson.get("content", "")))
    tokens = re.findall(r"[\w\u4e00-\u9fff]+", content)
    subject = "_".join(tokens[:4])
    return ":".join(
        [
            str(lesson.get("lesson_type", "")),
            str(lesson.get("lesson_subtype", "")),
            str(lesson.get("method", "")),
            str(lesson.get("path", "")),
            subject,
        ]
    )


def is_contradictory(left: str, right: str) -> bool:
    left_text = normalize_claim(left)
    right_text = normalize_claim(right)
    left_codes = extract_status_codes(left)
    right_codes = extract_status_codes(right)
    left_groups = {code // 100 for code in left_codes}
    right_groups = {code // 100 for code in right_codes}
    if left_codes and right_codes and left_groups.isdisjoint(right_groups):
        return True
    pairs = [
        ("不应", "应"),
        ("不能", "可以"),
        ("失败", "成功"),
        ("不存在", "存在"),
        ("false", "true"),
    ]
    for negative, positive in pairs:
        left_negative = contains_negative_marker(left_text, negative)
        right_negative = contains_negative_marker(right_text, negative)
        left_positive = contains_positive_marker(left_text, positive, negative)
        right_positive = contains_positive_marker(right_text, positive, negative)
        if left_negative and right_positive:
            return True
        if left_positive and right_negative:
            return True
    return False


def contains_negative_marker(text: str, marker: str) -> bool:
    return marker in text


def contains_positive_marker(text: str, positive: str, negative: str) -> bool:
    if positive not in text:
        return False
    if negative in text:
        return False
    if positive == "应" and "不应" in text:
        return False
    if positive == "可以" and "不可以" in text:
        return False
    return True


def extract_status_codes(content: str) -> set[int]:
    codes = {int(item) for item in re.findall(r"\b[1-5]\d\d\b", content)}
    return {code for code in codes if 100 <= code <= 599}
