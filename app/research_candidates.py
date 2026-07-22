from __future__ import annotations

import json
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from .db import Database
from .research_screening import screen_research_event


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

RESEARCH_CANDIDATE_TRANSITIONS = {
    "pending": {"researching"},
    "researching": {"evidence_ready", "insufficient_evidence", "failed"},
    "evidence_ready": {"researching", "awaiting_review"},
    "insufficient_evidence": {"researching", "awaiting_review"},
    "awaiting_review": {"completed", "insufficient_evidence"},
    "failed": {"researching"},
    "completed": set(),
    "superseded": set(),
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
    "山体垮塌",
    "山体滑坡",
    "建筑坍塌",
    "房屋坍塌",
    "桥梁坍塌",
    "泥石流",
    "刑事拘留",
    "被双开",
    "开除党籍",
    "开除公职",
    "严重违纪违法",
    "审查调查",
    "入室盗窃",
    "盗窃案",
    "抢劫",
    "绑架",
    "拐卖",
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
    if any(word.casefold() in corpus for word in COMMERCIAL_RESEARCH_BLOCKLIST):
        return True
    return screen_research_event(event).decision == "rejected"


def candidate_from_event(
    event: dict,
    bundle: dict,
    semantic_feature: dict | None,
    *,
    version: str = "research-candidate-v2",
) -> ResearchCandidateDraft | None:
    if is_commercial_research_blocked(event):
        return None

    semantic_status = str((semantic_feature or {}).get("status") or "")
    semantic_ready = semantic_status == "ready"
    readiness = str(bundle.get("readiness_status") or "insufficient")
    if not semantic_ready and readiness == "insufficient":
        return None
    categories = (semantic_feature or {}).get("category_matches")
    if semantic_ready and categories is None:
        try:
            categories = json.loads(
                (semantic_feature or {}).get("category_matches_json") or "[]"
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            categories = []
    if not semantic_ready:
        categories = []
    normalized_categories = []
    for item in categories or []:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category") or "").strip()
        similarity = item.get("similarity")
        if category and similarity is not None:
            try:
                normalized_categories.append(
                    {"category": category, "similarity": round(float(similarity), 4)}
                )
            except (TypeError, ValueError):
                continue

    if normalized_categories:
        if readiness == "ready_for_assessment":
            reason = "证据已达到准备门槛，类目联想值得进行结构化机会判断。"
        else:
            reason = "当前证据不足，但类目联想提供了可核查的补证方向。"
    else:
        semantic_reason = (
            "当前语义特征没有可靠类目联想"
            if semantic_ready
            else "语义类目特征未启用或不可用"
        )
        if readiness == "ready_for_assessment":
            reason = (
                f"证据已达到准备门槛，但{semantic_reason}；"
                "先以无类目候选进入人工结构化判断。"
            )
        else:
            reason = f"当前证据不足，且{semantic_reason}；先以无类目候选进入补证队列。"
    questions = [
        "该事件反映的是一次性热点，还是会持续影响消费者行为的变化？",
        "哪些具体用户和使用场景受到该变化影响？",
        "现有实体商品在哪些约束下无法满足需求？",
    ]
    for item in normalized_categories[:3]:
        questions.append(
            f"“{item['category']}”类目是否存在可重复验证的新场景或具体未满足需求？"
        )
    if not normalized_categories:
        questions.append("该事件是否与任何低风险实体消费品类目存在可核查关联？")

    positive = (
        (semantic_feature or {}).get("positive_similarity") if semantic_ready else None
    )
    negative = (
        (semantic_feature or {}).get("negative_similarity") if semantic_ready else None
    )
    delta = (
        (semantic_feature or {}).get("opportunity_similarity")
        if semantic_ready
        else None
    )
    if delta is None and positive is not None and negative is not None:
        delta = float(positive) - float(negative)
    top_similarity = max(
        (float(item["similarity"]) for item in normalized_categories), default=0.0
    )
    trend_component = max(0.0, min(float(event.get("trend_score") or 0), 100.0)) * 0.4
    semantic_component = max(0.0, min(top_similarity, 1.0)) * 35
    evidence_component = {
        "ready_for_assessment": 20.0,
        "partial": 12.0,
        "insufficient": 6.0,
    }.get(readiness, 0.0)
    delta_component = max(-5.0, min(float(delta or 0) * 20, 5.0))
    priority = round(
        max(
            0.0,
            min(
                trend_component
                + semantic_component
                + evidence_component
                + delta_component,
                100.0,
            ),
        ),
        2,
    )
    missing_evidence = list(bundle.get("missing_evidence") or [])
    category_evidence = "可核查的实体商品类目关联证据"
    if not normalized_categories and category_evidence not in missing_evidence:
        missing_evidence.append(category_evidence)
    semantic_feature_id = (semantic_feature or {}).get("id") if semantic_ready else None
    return ResearchCandidateDraft(
        event_id=int(event["id"]),
        evidence_bundle_id=int(bundle["id"]),
        semantic_feature_id=(
            int(semantic_feature_id) if semantic_feature_id is not None else None
        ),
        candidate_reason=reason,
        category_candidates=normalized_categories[:8],
        positive_similarity=float(positive) if positive is not None else None,
        negative_similarity=float(negative) if negative is not None else None,
        opportunity_delta=float(delta) if delta is not None else None,
        research_questions=questions,
        missing_evidence=missing_evidence[:12],
        priority=priority,
        engine=(
            "semantic-research-rules"
            if semantic_ready
            else "deterministic-research-rules"
        ),
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


def transition_research_candidate(
    db: Database,
    candidate_id: int,
    target_status: str,
    *,
    now: str | None = None,
) -> dict:
    """Move a candidate only when the persisted workflow proves the transition."""
    if target_status not in RESEARCH_CANDIDATE_STATUSES - {"superseded"}:
        raise ValueError("invalid research candidate status")
    row = db.one("SELECT * FROM research_candidates WHERE id=?", (candidate_id,))
    if row is None:
        raise LookupError("research candidate not found")
    current_status = str(row["status"])
    if current_status == target_status:
        return decode_research_candidate(row)
    if target_status not in RESEARCH_CANDIDATE_TRANSITIONS.get(current_status, set()):
        raise ValueError(
            f"research candidate cannot move from {current_status} to {target_status}"
        )

    if target_status == "researching":
        proof = db.one(
            """SELECT id FROM research_runs
            WHERE candidate_id=? AND status='running' ORDER BY started_at DESC LIMIT 1""",
            (candidate_id,),
        )
        if not proof:
            raise ValueError("a running research run is required")
    elif target_status in {"evidence_ready", "failed"}:
        run_status = "completed" if target_status == "evidence_ready" else "failed"
        proof = db.one(
            """SELECT id FROM research_runs
            WHERE candidate_id=? AND status=? ORDER BY started_at DESC LIMIT 1""",
            (candidate_id, run_status),
        )
        if not proof:
            raise ValueError(f"a {run_status} research run is required")
        if target_status == "evidence_ready":
            bundle = db.one(
                """SELECT b.readiness_status FROM research_candidates c
                JOIN evidence_bundles b ON b.id=c.evidence_bundle_id WHERE c.id=?""",
                (candidate_id,),
            )
            if not bundle or bundle["readiness_status"] != "ready_for_assessment":
                raise ValueError("candidate evidence bundle is not ready for assessment")
    elif target_status == "insufficient_evidence":
        if current_status == "researching":
            proof = db.one(
                """SELECT id FROM research_runs
                WHERE candidate_id=? AND status='completed'
                ORDER BY started_at DESC LIMIT 1""",
                (candidate_id,),
            )
            bundle = db.one(
                """SELECT b.readiness_status FROM research_candidates c
                JOIN evidence_bundles b ON b.id=c.evidence_bundle_id WHERE c.id=?""",
                (candidate_id,),
            )
            if not proof or (
                bundle and bundle["readiness_status"] == "ready_for_assessment"
            ):
                raise ValueError("completed research must prove insufficient evidence")
        else:
            proof = db.one(
                """SELECT id FROM opportunity_assessments
                WHERE candidate_id=? AND review_status='needs_more_evidence'
                ORDER BY id DESC LIMIT 1""",
                (candidate_id,),
            )
            if not proof:
                raise ValueError("a reviewed more-evidence decision is required")
    elif target_status == "awaiting_review":
        proof = db.one(
            """SELECT id FROM opportunity_assessments
            WHERE candidate_id=? AND review_status='pending' ORDER BY id DESC LIMIT 1""",
            (candidate_id,),
        )
        if not proof:
            raise ValueError("a pending opportunity assessment is required")
    elif target_status == "completed":
        assessment = db.one(
            """SELECT * FROM opportunity_assessments
            WHERE candidate_id=? AND review_status IN ('approved','rejected')
            ORDER BY id DESC LIMIT 1""",
            (candidate_id,),
        )
        if not assessment:
            raise ValueError("a completed opportunity assessment review is required")
        if assessment["review_status"] == "approved" and not db.one(
            "SELECT id FROM opportunity_signals WHERE opportunity_assessment_id=?",
            (assessment["id"],),
        ):
            raise ValueError("an approved assessment must have an opportunity signal")

    updated_at = now or datetime.now(timezone.utc).isoformat()
    db.execute(
        "UPDATE research_candidates SET status=?,updated_at=? WHERE id=?",
        (target_status, updated_at, candidate_id),
    )
    updated = db.one("SELECT * FROM research_candidates WHERE id=?", (candidate_id,))
    if updated is None:
        raise RuntimeError("research candidate disappeared during transition")
    return decode_research_candidate(updated)


def decode_research_candidate(row: dict) -> dict:
    decoded = dict(row)
    for column in (
        "category_candidates_json",
        "research_questions_json",
        "missing_evidence_json",
    ):
        decoded[column.removesuffix("_json")] = json.loads(decoded[column] or "[]")
    return decoded
