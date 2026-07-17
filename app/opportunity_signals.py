from __future__ import annotations

import json
from typing import Any


SIGNAL_JSON_FIELDS = (
    "target_users_json",
    "new_scenarios_json",
    "unmet_needs_json",
    "related_product_categories_json",
    "evidence_ids_json",
    "missing_evidence_json",
)

SIGNAL_FEEDBACK_TYPES = {
    "follow_up": "值得跟进",
    "no_physical_product": "没有实体商品机会",
    "weak_consumer_relevance": "消费关联弱",
    "too_short_term": "过于短期",
    "wrong_category": "类目错误",
    "insufficient_evidence": "证据不足",
}


def decode_signal(row: dict[str, Any]) -> dict[str, Any]:
    value = dict(row)
    for key in SIGNAL_JSON_FIELDS:
        raw = value.get(key)
        try:
            value[key.removesuffix("_json")] = json.loads(raw) if raw else []
        except (TypeError, json.JSONDecodeError):
            value[key.removesuffix("_json")] = []
    return value
