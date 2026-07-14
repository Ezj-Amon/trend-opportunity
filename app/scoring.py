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
