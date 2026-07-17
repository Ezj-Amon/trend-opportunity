from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from .db import Database


_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)


def normalized_identity(value: Any) -> str:
    """Normalize display text for exact-content identity checks."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return _NON_WORD.sub("", text)


def opportunity_identity(row: dict[str, Any]) -> tuple[str, str]:
    name = normalized_identity(row.get("name"))
    marketplace = str(row.get("target_marketplace") or row.get("marketplace") or "").upper()
    return name or f"id:{row.get('id')}", marketplace


def event_identity(row: dict[str, Any]) -> tuple[str, str]:
    title = normalized_identity(row.get("canonical_title") or row.get("normalized_title"))
    market = str(row.get("market") or "").upper()
    return title or f"id:{row.get('id')}", market


def deduplicate_opportunities(
    rows: Iterable[dict[str, Any]], limit: int | None = None
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        key = opportunity_identity(row)
        if key in seen:
            continue
        seen.add(key)
        selected.append(row)
        if limit is not None and len(selected) >= limit:
            break
    return selected


def deduplicate_events(
    rows: Iterable[dict[str, Any]], limit: int | None = None
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        key = event_identity(row)
        if key in seen:
            continue
        seen.add(key)
        selected.append(row)
        if limit is not None and len(selected) >= limit:
            break
    return selected


def _has_manual_work(row: dict[str, Any]) -> bool:
    return bool(
        row.get("review_status") in {"approved", "rejected"}
        or str(row.get("reviewer_note") or "").strip()
        or row.get("validation_status") in {"partial", "completed"}
        or int(row.get("outcome_count") or 0)
        or int(row.get("delivery_count") or 0)
    )


def _keeper_rank(row: dict[str, Any]) -> tuple[Any, ...]:
    review_rank = {"approved": 3, "pending": 2, "rejected": 1}.get(
        str(row.get("review_status")), 0
    )
    validation_rank = {
        "completed": 4,
        "partial": 3,
        "pending": 2,
        "unavailable": 1,
    }.get(str(row.get("validation_status")), 0)
    return (
        int(_has_manual_work(row)),
        review_rank,
        int(row.get("delivery_count") or 0),
        int(row.get("outcome_count") or 0),
        validation_rank,
        float(row.get("opportunity_score") or 0),
        float(row.get("evidence_confidence") or 0),
        int(row.get("id") or 0),
    )


def collapse_unworked_duplicate_opportunities(db: Database) -> dict[str, Any]:
    """Supersede duplicate generated candidates without discarding manual work."""
    rows = db.all(
        """SELECT o.*,
        (SELECT COUNT(*) FROM opportunity_outcomes x WHERE x.opportunity_id=o.id) outcome_count,
        (SELECT COUNT(*) FROM notification_deliveries n WHERE n.opportunity_id=o.id) delivery_count
        FROM product_opportunities o
        JOIN analyses a ON a.id=o.analysis_id
        WHERE o.review_status!='superseded' AND a.status!='superseded'
        ORDER BY o.id"""
    )
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(opportunity_identity(row), []).append(row)

    superseded: list[int] = []
    keepers: dict[int, int] = {}
    now = datetime.now(timezone.utc).isoformat()
    for group in groups.values():
        if len(group) < 2:
            continue
        keeper = max(group, key=_keeper_rank)
        for duplicate in group:
            if duplicate["id"] == keeper["id"] or _has_manual_work(duplicate):
                continue
            note = f"自动合并：与保留机会 #{keeper['id']} 的产品名称和目标站点相同"
            db.execute(
                """UPDATE product_opportunities
                SET review_status='superseded',
                    reviewer_note=CASE WHEN trim(reviewer_note)='' THEN ? ELSE reviewer_note || char(10) || ? END,
                    updated_at=?
                WHERE id=? AND review_status='pending'""",
                (note, note, now, duplicate["id"]),
            )
            superseded.append(int(duplicate["id"]))
            keepers[int(duplicate["id"])] = int(keeper["id"])
    return {
        "superseded_count": len(superseded),
        "superseded_ids": superseded,
        "keepers": keepers,
    }
