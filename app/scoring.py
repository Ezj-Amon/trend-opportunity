from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(slots=True)
class TrendScores:
    coverage: float
    rank: float
    velocity: float
    persistence: float
    freshness: float
    total: float


def calculate_trend_scores(
    ranks: list[tuple[int, int]],
    source_count: int,
    first_seen_at: str,
    last_seen_at: str,
    previous_rank_norm: float | None = None,
    rank_velocity_delta: float | None = None,
) -> TrendScores:
    rank_norms = [1 - ((rank - 1) / max(item_count - 1, 1)) for rank, item_count in ranks]
    rank = 100 * (sum(rank_norms) / len(rank_norms) if rank_norms else 0)
    coverage = 100 * min(source_count / 4, 1)
    latest_rank_norm = sum(rank_norms) / len(rank_norms) if rank_norms else 0
    velocity = 50.0
    if rank_velocity_delta is not None:
        velocity = 100 * max(0, min(1, 0.5 + rank_velocity_delta / 2))
    elif previous_rank_norm is not None:
        velocity = 100 * max(0, min(1, 0.5 + (latest_rank_norm - previous_rank_norm) / 2))
    first = datetime.fromisoformat(first_seen_at)
    last = datetime.fromisoformat(last_seen_at)
    duration_hours = max(0, (last - first).total_seconds() / 3600)
    persistence = 100 * min(duration_hours / 12, 1)
    age_hours = max(0, (datetime.now(timezone.utc) - last).total_seconds() / 3600)
    freshness = 100 * math.exp(-age_hours / 12)
    total = (
        0.30 * coverage
        + 0.25 * rank
        + 0.20 * velocity
        + 0.15 * persistence
        + 0.10 * freshness
    )
    return TrendScores(
        coverage=round(coverage, 1),
        rank=round(rank, 1),
        velocity=round(velocity, 1),
        persistence=round(persistence, 1),
        freshness=round(freshness, 1),
        total=round(total, 1),
    )


def calculate_opportunity_score(scores: dict[str, int]) -> float:
    """Score the product hypothesis before external market validation.

    Kept as a separate score so model/rules inference is never presented as
    marketplace evidence.
    """
    weights = {
        "pain_score": 0.25,
        "intent_score": 0.20,
        "segment_score": 0.15,
        "timing_score": 0.15,
        "feasibility_score": 0.15,
        "differentiation_score": 0.10,
    }
    total = 0.0
    for key, weight in weights.items():
        value = max(1, min(int(scores[key]), 5))
        total += ((value - 1) / 4) * weight
    return round(total * 100, 1)


MARKET_SCORE_WEIGHTS = {
    "search_demand_score": 0.20,
    "purchase_intent_score": 0.15,
    "competition_score": 0.15,
    "unit_economics_score": 0.20,
    "differentiation_score": 0.10,
    "execution_score": 0.10,
    "timing_score": 0.05,
    "evidence_score": 0.05,
}


def calculate_market_score(scores: dict[str, int | None]) -> float:
    """Calculate a conservative market score without filling missing fields.

    A missing dimension contributes zero instead of being reweighted away. This
    prevents a partial provider response from looking as strong as a complete
    validation.
    """
    total = 0.0
    for key, weight in MARKET_SCORE_WEIGHTS.items():
        value = scores.get(key)
        if value is None:
            continue
        normalized = (max(1, min(int(value), 5)) - 1) / 4
        total += normalized * weight
    return round(total * 100, 1)


def calculate_final_score(
    *,
    trend_score: float,
    hypothesis_score: float,
    market_score: float | None,
    validation_status: str,
    risk_level: str,
) -> tuple[float, float]:
    """Combine discovery and validation while making uncertainty visible.

    Until market evidence exists, the hypothesis score is only a provisional
    proxy and receives a large penalty. Blocking product risks always veto the
    candidate regardless of popularity.
    """
    if risk_level == "blocking":
        return 0.0, 100.0

    status_penalties = {
        "completed": 0.0,
        "partial": 15.0,
        "failed": 25.0,
        "unavailable": 30.0,
        "pending": 30.0,
    }
    risk_penalties = {"low": 0.0, "medium": 8.0, "high": 20.0}
    uncertainty_penalty = status_penalties.get(validation_status, 30.0)
    risk_penalty = risk_penalties.get(risk_level, 20.0)
    market_component = hypothesis_score if market_score is None else market_score
    value = 0.25 * trend_score + 0.75 * market_component
    value -= uncertainty_penalty + risk_penalty
    return round(max(0.0, min(value, 100.0)), 1), uncertainty_penalty


def calculate_evidence_confidence(
    evidence_count: float,
    independent_domains: float,
    consumer_voice_count: int,
    cited_claim_ratio: float,
) -> float:
    value = 100 * (
        0.30 * min(independent_domains / 4, 1)
        + 0.25 * min(evidence_count / 8, 1)
        + 0.25 * min(consumer_voice_count / max(evidence_count, 1), 1)
        + 0.20 * max(0, min(cited_claim_ratio, 1))
    )
    return round(value, 1)
