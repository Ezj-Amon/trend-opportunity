from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .clustering import title_similarity
from .db import Database
from .semantic import cosine_similarity


DUPLICATE_FEEDBACK_TYPES = {
    "same_event": "同一事件，应合并",
    "related_not_same": "相关但不是同一事件",
    "not_duplicate": "不是重复事件",
    "insufficient_evidence": "证据不足",
}


def _embedding(raw: str | None) -> list[float]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    try:
        return [float(item) for item in value]
    except (TypeError, ValueError):
        return []


def create_duplicate_candidates(
    db: Database,
    feature_id: int,
    *,
    threshold: float = 0.84,
    window: int = 500,
) -> int:
    """Create review candidates for one ready feature; never merge events."""
    current = db.one(
        """SELECT f.*, e.canonical_title, e.market, e.language
        FROM semantic_event_features f
        JOIN trend_events e ON e.id=f.event_id
        WHERE f.id=? AND f.status='ready'""",
        (feature_id,),
    )
    if not current:
        return 0
    current_vector = _embedding(current.get("embedding_json"))
    if not current_vector:
        return 0
    peers = db.all(
        """SELECT f.*, e.canonical_title, e.market, e.language
        FROM semantic_event_features f
        JOIN trend_events e ON e.id=f.event_id
        WHERE f.status='ready' AND f.event_id!=?
          AND f.model_id=? AND f.model_version=? AND f.feature_version=?
        ORDER BY f.id DESC LIMIT ?""",
        (
            current["event_id"],
            current["model_id"],
            current["model_version"],
            current["feature_version"],
            window,
        ),
    )
    created = 0
    now = datetime.now(timezone.utc).isoformat()
    for peer in peers:
        peer_vector = _embedding(peer.get("embedding_json"))
        similarity = cosine_similarity(current_vector, peer_vector)
        if similarity < threshold:
            continue
        if int(current["event_id"]) < int(peer["event_id"]):
            left, right = current, peer
        else:
            left, right = peer, current
        before = db.one(
            """SELECT id FROM semantic_duplicate_candidates
            WHERE event_a_id=? AND event_b_id=? AND model_id=? AND model_version=?
              AND feature_version=? AND event_a_input_hash=? AND event_b_input_hash=?""",
            (
                left["event_id"], right["event_id"], current["model_id"],
                current["model_version"], current["feature_version"],
                left["input_hash"], right["input_hash"],
            ),
        )
        if before:
            continue
        db.execute(
            """INSERT INTO semantic_duplicate_candidates
            (event_a_id,event_b_id,semantic_similarity,lexical_similarity,
             model_id,model_version,feature_version,event_a_input_hash,event_b_input_hash,
             event_a_market,event_b_market,event_a_language,event_b_language,
             review_status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)""",
            (
                left["event_id"], right["event_id"], round(similarity, 4),
                round(title_similarity(left["canonical_title"], right["canonical_title"]), 4),
                current["model_id"], current["model_version"], current["feature_version"],
                left["input_hash"], right["input_hash"],
                left.get("market", ""), right.get("market", ""),
                left.get("language", ""), right.get("language", ""), now, now,
            ),
        )
        created += 1
    return created


def duplicate_candidate_snapshot(db: Database, candidate_id: int) -> dict[str, Any] | None:
    candidate = db.one(
        """SELECT c.*, a.canonical_title event_a_title, b.canonical_title event_b_title,
        a.trend_score event_a_trend_score, b.trend_score event_b_trend_score
        FROM semantic_duplicate_candidates c
        JOIN trend_events a ON a.id=c.event_a_id
        JOIN trend_events b ON b.id=c.event_b_id
        WHERE c.id=?""",
        (candidate_id,),
    )
    if not candidate:
        return None
    candidate["event_a_evidence"] = db.all(
        "SELECT id,kind,url,title,excerpt FROM evidence WHERE event_id=? ORDER BY id",
        (candidate["event_a_id"],),
    )
    candidate["event_b_evidence"] = db.all(
        "SELECT id,kind,url,title,excerpt FROM evidence WHERE event_id=? ORDER BY id",
        (candidate["event_b_id"],),
    )
    return candidate
