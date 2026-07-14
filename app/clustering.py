from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher


STOP_FRAGMENTS = {
    "最新",
    "回应",
    "官方",
    "热搜",
    "话题",
    "网友",
    "视频",
    "现场",
}


def normalize_title(title: str) -> str:
    value = unicodedata.normalize("NFKC", title).lower()
    value = re.sub(r"[#【】\[\]（）()《》“”‘’·|丨_—\-:：,，。！？!?、\s]", "", value)
    for fragment in STOP_FRAGMENTS:
        value = value.replace(fragment, "")
    return value[:180]


def title_similarity(left: str, right: str) -> float:
    a = normalize_title(left)
    b = normalize_title(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    shorter, longer = sorted((a, b), key=len)
    containment = len(shorter) / len(longer) if shorter in longer else 0.0
    sequence = SequenceMatcher(None, a, b, autojunk=False).ratio()
    a_pairs = {a[i : i + 2] for i in range(max(1, len(a) - 1))}
    b_pairs = {b[i : i + 2] for i in range(max(1, len(b) - 1))}
    union = a_pairs | b_pairs
    jaccard = len(a_pairs & b_pairs) / len(union) if union else 0.0
    return max(containment, sequence * 0.75 + jaccard * 0.25)


def should_merge(left: str, right: str) -> tuple[bool, float, str]:
    score = title_similarity(left, right)
    normalized_left = normalize_title(left)
    normalized_right = normalize_title(right)
    exact = normalized_left == normalized_right
    shorter, longer = sorted((normalized_left, normalized_right), key=len)
    containment = (
        len(shorter) >= 8
        and shorter in longer
        and len(shorter) / max(len(longer), 1) >= 0.6
    )
    match = SequenceMatcher(None, normalized_left, normalized_right, autojunk=False).find_longest_match()
    shared_text = normalized_left[match.a : match.a + match.size]
    left_prefix = normalized_left[: match.a]
    right_prefix = normalized_right[: match.b]
    shared_cjk_count = len(re.findall(r"[\u3400-\u9fff]", shared_text))
    left_numbers = set(re.findall(r"\d+", normalized_left))
    right_numbers = set(re.findall(r"\d+", normalized_right))
    number_conflict = bool(
        left_numbers and right_numbers and left_numbers != right_numbers
    )
    prefix_conflict = bool(
        left_prefix
        and right_prefix
        and left_prefix not in right_prefix
        and right_prefix not in left_prefix
    )
    generic_phrases = {"中国", "回应", "如何看待", "为什么", "最新消息"}
    shared_phrase = (
        match.size >= 5
        and min(len(normalized_left), len(normalized_right)) >= 8
        and shared_text not in generic_phrases
        and not prefix_conflict
        and not number_conflict
        and shared_cjk_count >= 5
    )
    structured_similarity = (
        score >= 0.70
        and match.size >= 4
        and shared_cjk_count >= 4
        and not prefix_conflict
        and not number_conflict
    )
    merged = exact or containment or shared_phrase or structured_similarity or (
        score >= 0.74
        and shared_cjk_count >= 4
        and not prefix_conflict
        and not number_conflict
    )
    method = (
        "exact"
        if exact
        else "containment"
        if containment
        else "shared_phrase"
        if shared_phrase
        else "structured_similarity"
        if structured_similarity
        else "similarity"
    )
    return merged, score, method
