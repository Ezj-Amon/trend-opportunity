from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, Field

from .scoring import MARKET_SCORE_WEIGHTS, calculate_market_score


class MarketScores(BaseModel):
    search_demand_score: int | None = Field(default=None, ge=1, le=5)
    purchase_intent_score: int | None = Field(default=None, ge=1, le=5)
    competition_score: int | None = Field(default=None, ge=1, le=5)
    unit_economics_score: int | None = Field(default=None, ge=1, le=5)
    differentiation_score: int | None = Field(default=None, ge=1, le=5)
    execution_score: int | None = Field(default=None, ge=1, le=5)
    timing_score: int | None = Field(default=None, ge=1, le=5)
    evidence_score: int | None = Field(default=None, ge=1, le=5)


class MarketValidationInput(BaseModel):
    provider: str = Field(default="manual", min_length=1, max_length=80)
    provider_version: str = Field(default="manual-v1", min_length=1, max_length=80)
    marketplace: str = Field(default="", max_length=12)
    query: dict[str, Any] = Field(default_factory=dict)
    scores: MarketScores
    metrics: dict[str, Any] = Field(default_factory=dict)
    sources: list[str] = Field(default_factory=list, max_length=20)
    note: str = Field(default="", max_length=2000)


@dataclass(slots=True)
class MarketValidationResult:
    provider: str
    provider_version: str
    status: str
    query: dict[str, Any]
    scores: dict[str, int | None]
    metrics: dict[str, Any]
    sources: list[str]
    missing_fields: list[str]
    score: float | None
    raw_response_hash: str | None = None
    note: str = ""
    error: str | None = None


class MarketValidator(Protocol):
    async def validate(
        self, opportunity: dict[str, Any], event: dict[str, Any]
    ) -> MarketValidationResult: ...


class UnavailableMarketValidator:
    """Explicit fallback until Amazon first-party evidence is entered or imported."""

    async def validate(
        self, opportunity: dict[str, Any], event: dict[str, Any]
    ) -> MarketValidationResult:
        query = {
            "marketplace": opportunity.get("marketplace", ""),
            "target_marketplace": opportunity.get("target_marketplace", ""),
            "keywords": opportunity.get("product_keywords", []),
            "event": event.get("canonical_title", ""),
        }
        return MarketValidationResult(
            provider="unconfigured",
            provider_version="none",
            status="unavailable",
            query=query,
            scores={key: None for key in MARKET_SCORE_WEIGHTS},
            metrics={},
            sources=[],
            missing_fields=list(MARKET_SCORE_WEIGHTS),
            score=None,
            error="尚未录入 Product Opportunity Explorer / Brand Analytics 等市场证据；未生成或猜测市场数据",
        )


def result_from_input(value: MarketValidationInput) -> MarketValidationResult:
    scores = value.scores.model_dump()
    missing = [key for key, score in scores.items() if score is None]
    status = "completed" if not missing else "partial"
    canonical = json.dumps(
        {
            "marketplace": value.marketplace,
            "query": value.query,
            "scores": scores,
            "metrics": value.metrics,
            "sources": value.sources,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return MarketValidationResult(
        provider=value.provider,
        provider_version=value.provider_version,
        status=status,
        query={"marketplace": value.marketplace, **value.query},
        scores=scores,
        metrics=value.metrics,
        sources=value.sources,
        missing_fields=missing,
        score=calculate_market_score(scores),
        raw_response_hash=hashlib.sha256(canonical).hexdigest(),
        note=value.note,
    )
