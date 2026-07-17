from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field


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


class OpportunitySignalInput(BaseModel):
    """Human-authored signal input; automated research uses OpportunityAssessment."""

    change_type: str
    consumer_relevance_score: float = Field(ge=0, le=100)
    product_opportunity_score: float = Field(ge=0, le=100)
    target_users: list[str] = Field(min_length=1, max_length=8)
    new_scenarios: list[str] = Field(min_length=1, max_length=8)
    unmet_needs: list[str] = Field(min_length=1, max_length=8)
    related_product_categories: list[str] = Field(default_factory=list, max_length=8)
    durability: str
    lead_time_fit: str
    evidence_ids: list[int] = Field(min_length=1)
    confidence: float = Field(ge=0, le=100)
    missing_evidence: list[str] = Field(default_factory=list, max_length=8)


def decode_signal(row: dict[str, Any]) -> dict[str, Any]:
    value = dict(row)
    for key in SIGNAL_JSON_FIELDS:
        raw = value.get(key)
        try:
            value[key.removesuffix("_json")] = json.loads(raw) if raw else []
        except (TypeError, json.JSONDecodeError):
            value[key.removesuffix("_json")] = []
    return value
