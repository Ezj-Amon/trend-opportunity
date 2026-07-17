from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .db import Database
from .deduplication import opportunity_identity


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


def top_opportunities(db: Database, region: str, limit: int = 3) -> list[dict[str, Any]]:
    market_clause = "e.market='CN'" if region == "CN" else "e.market!='CN'"
    rows = db.all(
        f"""SELECT o.*, e.canonical_title event_title, e.market,
        e.trend_score, e.last_seen_at
        FROM product_opportunities o
        JOIN trend_events e ON e.id=o.event_id
        JOIN analyses a ON a.id=o.analysis_id
        WHERE {market_clause}
          AND o.review_status NOT IN ('rejected','superseded')
          AND a.status!='superseded'
          AND o.score_formula_version='opportunity-v2'
          AND o.risk_level!='blocking'
          AND o.opportunity_score>0
        ORDER BY o.opportunity_score DESC, o.evidence_confidence DESC,
                 e.trend_score DESC, o.id DESC
        LIMIT 50"""
    )
    selected: list[dict[str, Any]] = []
    seen_events: set[int] = set()
    seen_products: set[str] = set()
    for row in rows:
        event_id = int(row["event_id"])
        product_key = opportunity_identity(row)
        if event_id in seen_events or product_key in seen_products:
            continue
        selected.append(decode_opportunity_data(row))
        seen_events.add(event_id)
        seen_products.add(product_key)
        if len(selected) >= limit:
            break
    return selected


def build_daily_digest(db: Database) -> dict[str, Any]:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    return {
        "date": now.date().isoformat(),
        "generated_at": now.isoformat(),
        "cn_top3": top_opportunities(db, "CN"),
        "overseas_top3": top_opportunities(db, "OVERSEAS"),
        "policy": {
            "max_per_region": 3,
            "max_per_event": 1,
            "deduplicates_product_name": True,
            "allows_empty": True,
            "excludes": ["rejected", "superseded", "blocking-risk"],
        },
    }
