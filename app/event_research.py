from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


FETCH_STATUS_LABELS = {
    "content_too_short": "正文过短",
    "login_required": "需要登录",
    "redirect_blocked": "重定向被安全策略阻止",
    "javascript_required": "页面需要 JavaScript 渲染",
    "not_html": "返回内容不是网页正文",
    "http_error": "网页请求失败",
    "timeout": "网页请求超时",
    "robots_or_access_denied": "站点拒绝公开访问",
    "unsupported": "暂不支持该地址",
    "failed": "抓取失败",
}

SEMANTIC_STATUS_LABELS = {
    "ready": "已就绪",
    "disabled": "已禁用",
    "unavailable": "模型不可用",
    "failed": "计算失败",
}

HUMAN_LABELS = {
    "positive": "值得跟进的实体新品机会",
    "no_physical_product": "无实体商品机会",
    "weak_consumer_relevance": "消费关联弱",
    "too_short_term": "短时热点",
    "high_risk": "高风险或悲剧事件",
    "software_service": "仅适合软件或服务",
    "insufficient_evidence": "证据不足",
}

EVIDENCE_TYPE_LABELS = {
    "full_article": "完整正文",
    "official_notice": "官方公告",
    "article_summary": "文章摘要",
    "consumer_discussion": "消费者讨论",
    "consumer_comment": "消费者评论",
    "search_snippet": "搜索摘要",
    "title_only": "仅标题",
    "manual_evidence": "人工证据",
}


class CategoryCandidateView(BaseModel):
    category: str
    similarity: float


class EvidenceSummaryView(BaseModel):
    readiness_status: str
    readiness_label: str
    readiness_score: float
    full_text_count: int
    title_only_count: int
    independent_source_count: int
    consumer_voice_count: int
    official_source_count: int


class FetchFailureView(BaseModel):
    evidence_id: int
    status: str
    status_label: str
    source_name: str = ""
    url: str = ""
    detail: str = ""


class EventResearchView(BaseModel):
    conclusion_code: str
    conclusion_label: str
    stop_reasons: list[str] = Field(default_factory=list)
    category_candidates: list[CategoryCandidateView] = Field(default_factory=list)
    positive_similarity: float | None = None
    negative_similarity: float | None = None
    opportunity_delta: float | None = None
    delta_explanation: str
    evidence_summary: EvidenceSummaryView
    fetch_failures: list[FetchFailureView] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    producer_status: str
    semantic_status: str
    human_label: str
    next_actions: list[str] = Field(default_factory=list)


def _delta_explanation(delta: float | None) -> str:
    if delta is None:
        return "尚无可比较的正向与负向语义特征。"
    if delta < -0.02:
        return "负向判断略强：该事件更接近短时噪声或不适合形成新品机会的原型。"
    if delta > 0.02:
        return "正向判断略强，但这只是探索性语义信号，不是需求概率或新品结论。"
    return "正向与负向判断接近，语义特征本身不足以支持机会结论。"


def _producer_status(analysis: dict | None) -> str:
    if not analysis:
        return "机会生产器尚未运行。"
    engine = str(analysis.get("engine") or "")
    status = str(analysis.get("status") or "")
    if engine in {"local-rules", "local-rules-fallback", "safety-gate"}:
        return "历史分析记录已停止生产；当前主链以 EvidenceBundle 和 ResearchCandidate 为准。"
    if status == "degraded":
        return "机会生产器已明确降级，当前结果不能作为已形成线索。"
    if status == "failed":
        return "机会生产器运行失败，未形成可用判断。"
    return "存在历史分析记录；是否形成线索仍以审核后的 OpportunitySignal 为准。"


def build_event_research_view(
    event: dict,
    bundle: dict[str, Any],
    semantic_feature: dict | None,
    human_label: dict | None,
    signals: list[dict],
    analysis: dict | None,
    candidate: dict | None = None,
    assessment: dict | None = None,
) -> EventResearchView:
    readiness_status = str(bundle.get("readiness_status") or "insufficient")
    if signals:
        conclusion_code = "signal_created"
        conclusion_label = "已形成机会线索，等待或已完成人工审核"
    elif assessment and assessment.get("review_status") == "pending":
        conclusion_code = "assessment_pending"
        conclusion_label = "机会评估已完成，等待人工审核"
    elif candidate:
        conclusion_code = "research_candidate"
        conclusion_label = "已形成待研究方向，尚未形成机会线索"
    elif readiness_status == "ready_for_assessment":
        conclusion_code = "ready_for_assessment"
        conclusion_label = "证据已准备，等待机会判断"
    elif bundle.get("evidence_ids"):
        conclusion_code = "insufficient_evidence"
        conclusion_label = "证据不足，暂不形成机会线索"
    else:
        conclusion_code = "no_evidence"
        conclusion_label = "尚无证据，暂不判断"

    missing = list(bundle.get("missing_evidence") or [])
    stop_reasons = [] if signals else list(missing)
    if candidate and not assessment:
        stop_reasons.append("ResearchCandidate 只保存研究方向，不能直接进入商品假设或推荐")
    if not signals and readiness_status == "ready_for_assessment":
        stop_reasons.append("证据已达到准备门槛，但当前生产器尚未形成经审核的机会线索")
    if not stop_reasons and not signals:
        stop_reasons.append("当前没有足够信息支持消费变化判断")

    matches = (semantic_feature or {}).get("category_matches") or []
    categories = []
    for match in matches:
        category = str(match.get("category") or "").strip()
        similarity = match.get("similarity")
        if category and similarity is not None:
            categories.append(
                CategoryCandidateView(category=category, similarity=float(similarity))
            )

    positive = (semantic_feature or {}).get("positive_similarity")
    negative = (semantic_feature or {}).get("negative_similarity")
    delta = (semantic_feature or {}).get("opportunity_similarity")
    if delta is None and positive is not None and negative is not None:
        delta = float(positive) - float(negative)

    failures = [
        FetchFailureView(
            evidence_id=int(item.get("evidence_id") or 0),
            status=str(item.get("status") or "failed"),
            status_label=FETCH_STATUS_LABELS.get(
                str(item.get("status") or "failed"), "抓取失败"
            ),
            source_name=str(item.get("source_name") or ""),
            url=str(item.get("url") or ""),
            detail=str(item.get("detail") or ""),
        )
        for item in bundle.get("fetch_failure_reasons") or []
    ]

    semantic_code = str((semantic_feature or {}).get("status") or "not_run")
    semantic_status = SEMANTIC_STATUS_LABELS.get(semantic_code, "尚未运行")
    label_code = str((human_label or {}).get("label") or "")
    translated_human_label = HUMAN_LABELS.get(label_code, "尚无人工标签")
    if human_label and human_label.get("note"):
        translated_human_label += f"：{human_label['note']}"

    next_actions = []
    if int(bundle.get("full_text_count") or 0) < 1:
        next_actions.append("补充至少一条无需登录的完整报道或官方公告")
    if int(bundle.get("independent_source_count") or 0) < 2:
        next_actions.append("从另一家独立来源交叉核对事件事实")
    if int(bundle.get("consumer_voice_count") or 0) < 1:
        next_actions.append("寻找公开消费者讨论，验证具体场景和痛点")
    if not next_actions and not signals:
        next_actions.append("基于当前证据进行人工机会判断，并保留逐条引用")

    readiness_labels = {
        "insufficient": "不足",
        "partial": "部分就绪",
        "ready_for_assessment": "可进入机会判断",
    }
    producer_status = _producer_status(analysis)
    if candidate and not assessment:
        producer_status = "定时管道已创建待研究候选，等待人工或受控 ResearchRun 补充证据。"
    elif assessment and not signals:
        producer_status = "OpportunityAssessment 已保存；只有人工批准后才会映射为机会线索。"
    return EventResearchView(
        conclusion_code=conclusion_code,
        conclusion_label=conclusion_label,
        stop_reasons=stop_reasons,
        category_candidates=categories,
        positive_similarity=float(positive) if positive is not None else None,
        negative_similarity=float(negative) if negative is not None else None,
        opportunity_delta=float(delta) if delta is not None else None,
        delta_explanation=_delta_explanation(float(delta) if delta is not None else None),
        evidence_summary=EvidenceSummaryView(
            readiness_status=readiness_status,
            readiness_label=readiness_labels.get(readiness_status, "不足"),
            readiness_score=float(bundle.get("readiness_score") or 0),
            full_text_count=int(bundle.get("full_text_count") or 0),
            title_only_count=int(bundle.get("title_only_count") or 0),
            independent_source_count=int(bundle.get("independent_source_count") or 0),
            consumer_voice_count=int(bundle.get("consumer_voice_count") or 0),
            official_source_count=int(bundle.get("official_source_count") or 0),
        ),
        fetch_failures=failures,
        missing_evidence=missing,
        producer_status=producer_status,
        semantic_status=semantic_status,
        human_label=translated_human_label,
        next_actions=next_actions,
    )
