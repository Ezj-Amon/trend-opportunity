from __future__ import annotations

import json
from typing import Any, Protocol

from pydantic import BaseModel, Field, field_validator

from .amazon import is_supported_marketplace, marketplace_code, normalize_search_term
from .risk import assess_product_risk


HYPOTHESIS_STATUSES = {
    "draft": "草稿",
    "ready_for_validation": "待市场验证",
    "rejected": "已否决",
    "validated": "已验证",
}

HYPOTHESIS_JSON_FIELDS = (
    "target_users_json",
    "scenarios_json",
    "product_keywords_json",
    "query_terms_json",
    "evidence_ids_json",
    "risk_flags_json",
)

_NON_PHYSICAL_TERMS = {
    "软件", "订阅", "咨询", "课程", "资料包", "模板", "简报", "服务",
    "software", "subscription", "consulting", "course", "template", "service",
}


class ProductHypothesisInput(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    physical_form: str = Field(min_length=2, max_length=500)
    target_users: list[str] = Field(min_length=1, max_length=8)
    scenarios: list[str] = Field(min_length=1, max_length=8)
    problem: str = Field(min_length=2, max_length=1000)
    expected_difference: str = Field(min_length=2, max_length=1000)
    product_keywords: list[str] = Field(min_length=1, max_length=8)
    query_terms: list[str] = Field(default_factory=list, max_length=8)
    target_marketplace: str = Field(default="US", min_length=2, max_length=12)
    evidence_ids: list[int] = Field(min_length=1, max_length=30)

    @field_validator(
        "target_users", "scenarios", "product_keywords", "query_terms",
        mode="after",
    )
    @classmethod
    def clean_list(cls, values: list[str]) -> list[str]:
        cleaned = []
        for value in values:
            item = str(value).strip()
            if item and item not in cleaned:
                cleaned.append(item)
        return cleaned


class ProductHypothesisResult(BaseModel):
    value: ProductHypothesisInput
    generator_type: str
    provider: str
    model: str = ""
    version: str


class ProductHypothesisGenerator(Protocol):
    async def generate(
        self,
        signal: dict[str, Any],
        evidence: list[dict[str, Any]],
        draft: ProductHypothesisInput,
    ) -> ProductHypothesisResult: ...


class HumanProductHypothesisGenerator:
    async def generate(
        self,
        signal: dict[str, Any],
        evidence: list[dict[str, Any]],
        draft: ProductHypothesisInput,
    ) -> ProductHypothesisResult:
        return ProductHypothesisResult(
            value=draft,
            generator_type="human",
            provider="human-workbench",
            version="human-hypothesis-v1",
        )


def validate_physical_hypothesis(
    event: dict[str, Any], draft: ProductHypothesisInput
) -> tuple[str, list[dict[str, str]]]:
    corpus = " ".join(
        [draft.name, draft.physical_form, draft.problem, draft.expected_difference]
    ).casefold()
    hits = sorted(term for term in _NON_PHYSICAL_TERMS if term in corpus)
    if hits:
        return "blocking", [
            {
                "category": "non_physical",
                "severity": "blocking",
                "reason": "商品假设必须是可运输的实体消费品；命中非实体类型："
                + "、".join(hits),
            }
        ]
    marketplace = marketplace_code(draft.target_marketplace)
    if not is_supported_marketplace(marketplace):
        return "blocking", [
            {
                "category": "marketplace",
                "severity": "blocking",
                "reason": f"不支持的 Amazon 站点：{marketplace}",
            }
        ]
    proxy = type(
        "HypothesisRiskView",
        (),
        {
            "name": draft.name,
            "solution": draft.expected_difference,
            "mvp": draft.physical_form,
            "risks": [],
        },
    )()
    return assess_product_risk(event, proxy)


def normalized_query_terms(draft: ProductHypothesisInput) -> list[str]:
    values = []
    for raw in draft.query_terms:
        value = normalize_search_term(raw)
        if value and value not in values:
            values.append(value)
    return values


def decode_hypothesis(row: dict[str, Any]) -> dict[str, Any]:
    value = dict(row)
    for key in HYPOTHESIS_JSON_FIELDS:
        try:
            value[key.removesuffix("_json")] = json.loads(value.get(key) or "[]")
        except (TypeError, json.JSONDecodeError):
            value[key.removesuffix("_json")] = []
    return value
