from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .db import Database


JSON_FIELDS = (
    "product_keywords_json",
    "pain_points_json",
    "channels_json",
    "risks_json",
    "risk_flags_json",
)


def decode_opportunity_data(row: dict[str, Any]) -> dict[str, Any]:
    value = dict(row)
    for key in JSON_FIELDS:
        raw = value.get(key)
        try:
            value[key.removesuffix("_json")] = json.loads(raw) if raw else []
        except (TypeError, json.JSONDecodeError):
            value[key.removesuffix("_json")] = []
    return value


def is_validated_recommendation(item: dict[str, Any]) -> bool:
    """Return whether a legacy product row has crossed every recommendation gate."""
    return (
        item.get("validation_status") == "completed"
        and item.get("market_score") is not None
        and item.get("validated_recommendation_score") is not None
        and item.get("review_status") == "approved"
        and item.get("risk_level") != "blocking"
    )


def top_trend_signals(db: Database, region: str, limit: int = 3) -> list[dict[str, Any]]:
    """Build a fact-layer digest while OpportunitySignal does not exist yet."""
    if region == "CN":
        market_clause, params = "e.market='CN'", ()
    elif region == "OVERSEAS":
        market_clause, params = "e.market!='CN'", ()
    else:
        market_clause, params = "e.market=?", (region,)
    rows = db.all(
        f"""SELECT e.id, e.id event_id, e.canonical_title, e.canonical_title event_title,
        e.market, e.signal_type, e.trend_score, e.source_count, e.member_count,
        e.last_seen_at
        FROM trend_events e
        WHERE {market_clause}
        ORDER BY e.trend_score DESC, e.last_seen_at DESC, e.id DESC
        LIMIT 50""",
        params,
    )
    selected: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for row in rows:
        title_key = "".join(character.casefold() for character in row["canonical_title"] if character.isalnum())
        if title_key in seen_titles:
            continue
        selected.append(dict(row))
        seen_titles.add(title_key)
        if len(selected) >= limit:
            break
    return selected


def build_daily_digest(db: Database) -> dict[str, Any]:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    return {
        "date": now.date().isoformat(),
        "generated_at": now.isoformat(),
        "cn_top3": top_trend_signals(db, "CN"),
        "overseas_top3": top_trend_signals(db, "OVERSEAS"),
        "policy": {
            "content_type": "trend_event",
            "max_per_region": 3,
            "max_per_event": 1,
            "deduplicates_normalized_title": True,
            "allows_empty": True,
            "not_a_product_recommendation": True,
        },
    }
