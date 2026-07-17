from __future__ import annotations

import json
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from .db import Database


RESEARCH_CANDIDATE_STATUSES = {
    "pending",
    "researching",
    "evidence_ready",
    "insufficient_evidence",
    "awaiting_review",
    "completed",
    "failed",
    "superseded",
}

COMMERCIAL_RESEARCH_BLOCKLIST = {
    "遇害",
    "死亡",
    "去世",
    "伤亡",
    "谋杀",
    "枪击",
    "性侵",
    "战争",
    "自杀",
    "坠亡",
    "咬伤",
    "重伤",
    "刑事拘留",
    "警方通报",
    "公安通报",
    "立案调查",
    "严打",
    "谣言被拘",
    "破解",
    "绕过验证",
    "黑砖",
    "解锁bl",
    "远程测试",
    "概不负责",
    "killed",
    "dies",
    "dead",
    "death",
    "murder",
    "shooting",
    "injured",
    "fatal",
    "victim",
}


class ResearchCandidateDraft(BaseModel):
    event_id: int
    evidence_bundle_id: int
    semantic_feature_id: int | None = None
    candidate_reason: str
    category_candidates: list[dict] = Field(default_factory=list, max_length=8)
    positive_similarity: float | None = None
    negative_similarity: float | None = None
    opportunity_delta: float | None = None
    research_questions: list[str] = Field(default_factory=list, max_length=12)
    missing_evidence: list[str] = Field(default_factory=list, max_length=12)
    priority: float = Field(ge=0, le=100)
    engine: str
    version: str


def is_commercial_research_blocked(event: dict) -> bool:
    label = str(event.get("human_label") or "")
    if label == "high_risk":
        return True
    corpus = str(event.get("canonical_title") or "").casefold()
    return any(word.casefold() in corpus for word in COMMERCIAL_RESEARCH_BLOCKLIST)


def candidate_from_event(
    event: dict,
    bundle: dict,
    semantic_feature: dict | None,
    *,
    version: str = "research-candidate-v1",
) -> ResearchCandidateDraft | None:
    if is_commercial_research_blocked(event) or not semantic_feature:
        return None
    if str(semantic_feature.get("status") or "") != "ready":
        return None
    categories = semantic_feature.get("category_matches")
    if categories is None:
        try:
            categories = json.loads(semantic_feature.get("category_matches_json") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            categories = []
    normalized_categories = []
    for item in categories or []:
        category = str(item.get("category") or "").strip()
        similarity = item.get("similarity")
        if category and similarity is not None:
            normalized_categories.append(
                {"category": category, "similarity": round(float(similarity), 4)}
            )
    if not normalized_categories:
        return None

    readiness = str(bundle.get("readiness_status") or "insufficient")
    if readiness == "ready_for_assessment":
        reason = "证据已达到准备门槛，类目联想值得进行结构化机会判断。"
    else:
        reason = "当前证据不足，但类目联想提供了可核查的补证方向。"
    questions = [
        "该事件反映的是一次性热点，还是会持续影响消费者行为的变化？",
        "哪些具体用户和使用场景受到该变化影响？",
        "现有实体商品在哪些约束下无法满足需求？",
    ]
    for item in normalized_categories[:3]:
        questions.append(
            f"“{item['category']}”类目是否存在可重复验证的新场景或具体未满足需求？"
        )
    positive = semantic_feature.get("positive_similarity")
    negative = semantic_feature.get("negative_similarity")
    delta = semantic_feature.get("opportunity_similarity")
    if delta is None and positive is not None and negative is not None:
        delta = float(positive) - float(negative)
    top_similarity = max(float(item["similarity"]) for item in normalized_categories)
    trend_component = max(0.0, min(float(event.get("trend_score") or 0), 100.0)) * 0.4
    semantic_component = max(0.0, min(top_similarity, 1.0)) * 35
    evidence_component = {
        "ready_for_assessment": 20.0,
        "partial": 12.0,
        "insufficient": 6.0,
    }.get(readiness, 0.0)
    delta_component = max(-5.0, min(float(delta or 0) * 20, 5.0))
    priority = round(max(0.0, min(trend_component + semantic_component + evidence_component + delta_component, 100.0)), 2)
    return ResearchCandidateDraft(
        event_id=int(event["id"]),
        evidence_bundle_id=int(bundle["id"]),
        semantic_feature_id=int(semantic_feature["id"]),
        candidate_reason=reason,
        category_candidates=normalized_categories[:8],
        positive_similarity=float(positive) if positive is not None else None,
        negative_similarity=float(negative) if negative is not None else None,
        opportunity_delta=float(delta) if delta is not None else None,
        research_questions=questions,
        missing_evidence=list(bundle.get("missing_evidence") or []),
        priority=priority,
        engine="semantic-research-rules",
        version=version,
    )


def persist_research_candidate(db: Database, draft: ResearchCandidateDraft) -> dict:
    existing = db.one(
        """SELECT * FROM research_candidates
        WHERE event_id=? AND evidence_bundle_id=?
          AND COALESCE(semantic_feature_id,-1)=COALESCE(?,-1)
          AND version=? AND status!='superseded'
        ORDER BY id DESC LIMIT 1""",
        (
            draft.event_id,
            draft.evidence_bundle_id,
            draft.semantic_feature_id,
            draft.version,
        ),
    )
    if existing:
        return decode_research_candidate(existing)
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        """UPDATE research_candidates SET status='superseded',updated_at=?
        WHERE event_id=? AND status NOT IN ('completed','superseded')""",
        (now, draft.event_id),
    )
    candidate_id = db.execute(
        """INSERT INTO research_candidates
        (event_id,evidence_bundle_id,semantic_feature_id,candidate_reason,
         category_candidates_json,positive_similarity,negative_similarity,
         opportunity_delta,research_questions_json,missing_evidence_json,
         priority,status,engine,version,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?)""",
        (
            draft.event_id,
            draft.evidence_bundle_id,
            draft.semantic_feature_id,
            draft.candidate_reason,
            db.json(draft.category_candidates),
            draft.positive_similarity,
            draft.negative_similarity,
            draft.opportunity_delta,
            db.json(draft.research_questions),
            db.json(draft.missing_evidence),
            draft.priority,
            draft.engine,
            draft.version,
            now,
            now,
        ),
    )
    row = db.one("SELECT * FROM research_candidates WHERE id=?", (candidate_id,))
    if row is None:
        raise RuntimeError("failed to persist research candidate")
    return decode_research_candidate(row)


def decode_research_candidate(row: dict) -> dict:
    decoded = dict(row)
    for column in (
        "category_candidates_json",
        "research_questions_json",
        "missing_evidence_json",
    ):
        decoded[column.removesuffix("_json")] = json.loads(decoded[column] or "[]")
    return decoded
