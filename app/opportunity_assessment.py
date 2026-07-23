from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .db import Database
from .research import redact_tool_error
from .research_candidates import is_commercial_research_blocked


ASSESSMENT_STATUSES = {"worth_following", "abstained", "insufficient_evidence"}
ASSESSMENT_REVIEW_STATUSES = {
    "pending",
    "approved",
    "rejected",
    "needs_more_evidence",
    "superseded",
}


class CitedClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[int] = Field(min_length=1, max_length=100)


class ConsumerChangeJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["related", "unrelated", "uncertain"]
    rationale: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[int] = Field(min_length=1, max_length=100)


class ProblemJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["clear", "needs_evidence", "none"]
    rationale: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[int] = Field(min_length=1, max_length=100)


class ResearchRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["continue_research", "defer", "abandon"]
    rationale: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[int] = Field(min_length=1, max_length=100)


class OpportunityAssessmentDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assessment_status: str
    change_type: str = Field(default="", max_length=500)
    consumer_relevance: str = Field(default="", max_length=4000)
    durability: str = Field(default="", max_length=1000)
    lead_time_fit: str = Field(default="", max_length=1000)
    target_users: list[str] = Field(default_factory=list, max_length=20)
    new_scenarios: list[str] = Field(default_factory=list, max_length=20)
    existing_solutions: list[str] = Field(default_factory=list, max_length=20)
    solution_gaps: list[str] = Field(default_factory=list, max_length=20)
    unmet_needs: list[str] = Field(default_factory=list, max_length=20)
    related_product_categories: list[str] = Field(default_factory=list, max_length=20)
    consumer_change_judgment: ConsumerChangeJudgment | None = None
    problem_judgment: ProblemJudgment | None = None
    research_recommendation: ResearchRecommendation | None = None
    fact_claims: list[CitedClaim] = Field(default_factory=list, max_length=50)
    inferences: list[CitedClaim] = Field(default_factory=list, max_length=50)
    evidence_ids: list[int] = Field(default_factory=list, max_length=500)
    missing_evidence: list[str] = Field(default_factory=list, max_length=50)
    abstention_reason: str = Field(default="", max_length=4000)
    research_run_id: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_shape(self):
        if self.assessment_status not in ASSESSMENT_STATUSES:
            raise ValueError("invalid assessment status")
        if self.assessment_status == "worth_following":
            required = (
                self.change_type,
                self.consumer_relevance,
                self.durability,
                self.lead_time_fit,
            )
            if not all(value.strip() for value in required):
                raise ValueError("worth_following assessment requires structured judgments")
            if (
                not self.target_users
                or not self.new_scenarios
                or not self.unmet_needs
            ):
                raise ValueError("worth_following assessment requires users, scenarios and needs")
            if not self.fact_claims or not self.evidence_ids:
                raise ValueError("worth_following assessment requires cited facts and evidence")
        elif not self.abstention_reason.strip():
            raise ValueError("non-following assessment requires a reason")
        judgments = (
            self.consumer_change_judgment,
            self.problem_judgment,
            self.research_recommendation,
        )
        if any(item is not None for item in judgments):
            if not all(item is not None for item in judgments):
                raise ValueError("assessment v2 requires all three judgment levels")
            consumer = self.consumer_change_judgment
            problem = self.problem_judgment
            recommendation = self.research_recommendation
            assert consumer is not None and problem is not None and recommendation is not None
            expected = (
                "worth_following"
                if (
                    consumer.status == "related"
                    and problem.status == "clear"
                    and recommendation.status == "continue_research"
                )
                else "abstained"
                if (
                    consumer.status == "unrelated"
                    or problem.status == "none"
                    or recommendation.status == "abandon"
                )
                else "insufficient_evidence"
            )
            if self.assessment_status != expected:
                raise ValueError(
                    f"assessment status must be {expected} for the three-level judgment"
                )
        return self

    def require_v2(self) -> None:
        if not (
            self.consumer_change_judgment
            and self.problem_judgment
            and self.research_recommendation
        ):
            raise ValueError("model returned an incomplete three-level assessment")
        if self.related_product_categories:
            raise ValueError("assessment v2 must not output product categories")


@dataclass(slots=True)
class OpportunityAssessmentResult:
    draft: OpportunityAssessmentDraft
    engine: str
    model: str
    version: str
    generation_status: str = "completed"


class OpportunityAssessmentProvider(Protocol):
    async def assess(
        self,
        event: dict,
        bundle: dict,
        candidate: dict,
        evidence: list[dict],
    ) -> OpportunityAssessmentResult: ...


class HumanAssessmentProvider:
    name = "human"

    def __init__(
        self, draft: OpportunityAssessmentDraft, version: str = "human-assessment-v1"
    ):
        self.draft = draft
        self.version = version

    async def assess(
        self,
        event: dict,
        bundle: dict,
        candidate: dict,
        evidence: list[dict],
    ) -> OpportunityAssessmentResult:
        del event, candidate, evidence
        if (
            bundle["readiness_status"] != "ready_for_assessment"
            and self.draft.assessment_status == "worth_following"
        ):
            raise ValueError("evidence bundle is not ready for a worth-following assessment")
        return OpportunityAssessmentResult(
            draft=self.draft,
            engine=self.name,
            model="",
            version=self.version,
        )


class CloudOpportunityAssessmentProvider:
    name = "cloud-opportunity-assessment"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str | None = None,
        version: str = "cloud-assessment-v2",
        client=None,
    ):
        self.model = model
        self.version = version
        self.client = client or AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def assess(
        self,
        event: dict,
        bundle: dict,
        candidate: dict,
        evidence: list[dict],
    ) -> OpportunityAssessmentResult:
        del candidate
        if bundle["readiness_status"] != "ready_for_assessment":
            return OpportunityAssessmentResult(
                draft=OpportunityAssessmentDraft(
                    assessment_status="insufficient_evidence",
                    evidence_ids=list(bundle.get("evidence_ids") or []),
                    missing_evidence=list(bundle.get("missing_evidence") or []),
                    abstention_reason="EvidenceBundle 未达到大模型机会判断门槛，模型未调用。",
                ),
                engine=self.name,
                model=self.model,
                version=self.version,
            )
        if is_commercial_research_blocked(event):
            return OpportunityAssessmentResult(
                draft=OpportunityAssessmentDraft(
                    assessment_status="abstained",
                    evidence_ids=list(bundle.get("evidence_ids") or []),
                    missing_evidence=list(bundle.get("missing_evidence") or []),
                    abstention_reason="事件命中公共利益或高风险安全门，模型未调用。",
                ),
                engine=self.name,
                model=self.model,
                version=self.version,
            )
        evidence_payload = [
            {
                "id": int(item["id"]),
                "evidence_type": item.get("evidence_type"),
                "source_name": item.get("source_name"),
                "title": item.get("title"),
                "excerpt": str(item.get("excerpt") or "")[:4000],
                "is_consumer_voice": bool(item.get("is_consumer_voice")),
            }
            for item in evidence
            if int(item["id"]) in {int(value) for value in bundle.get("evidence_ids") or []}
        ]
        prompt_payload = {
            "event": {
                "id": event["id"],
                "title": event["canonical_title"],
                "market": event.get("market"),
                "language": event.get("language"),
                "signal_type": event.get("signal_type"),
            },
            "evidence_bundle": {
                key: bundle.get(key)
                for key in (
                    "readiness_status",
                    "readiness_score",
                    "full_text_count",
                    "title_only_count",
                    "independent_source_count",
                    "consumer_voice_count",
                    "missing_evidence",
                )
            },
            "evidence": evidence_payload,
        }
        try:
            response = await self.client.responses.parse(
                model=self.model,
                input=[
                    {
                        "role": "system",
                        "content": (
                            "你是趋势机会判断训练器，只能使用给定 EvidenceBundle。按顺序完成三级判断："
                            "1 是否属于普通消费者的真实变化；2 是否产生了具体新问题；"
                            "3 是否值得继续研究。聚焦一个主要用户和一个具体场景。"
                            "说明现有解决方式及其不足；证据没有覆盖时必须留空并写入 missing_evidence。"
                            "每一级判断、事实和推断都必须引用给定 evidence ID。"
                            "不得输出商品名、商品类目、价格、平台查询词、ProductHypothesis 或推荐。"
                            "不确定时使用 uncertain、needs_evidence 或 defer，不得补写无法访问的事实。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            prompt_payload, ensure_ascii=False, separators=(",", ":")
                        ),
                    },
                ],
                text_format=OpportunityAssessmentDraft,
            )
            parsed = getattr(response, "output_parsed", None)
            if parsed is None:
                for output in getattr(response, "output", []):
                    for content in getattr(output, "content", []):
                        if getattr(content, "parsed", None) is not None:
                            parsed = content.parsed
                            break
                    if parsed is not None:
                        break
            if parsed is None:
                raise ValueError("model returned no parsed assessment")
            draft = (
                parsed
                if isinstance(parsed, OpportunityAssessmentDraft)
                else OpportunityAssessmentDraft.model_validate(parsed)
            )
            draft.require_v2()
            return OpportunityAssessmentResult(
                draft=draft,
                engine=self.name,
                model=self.model,
                version=self.version,
            )
        except Exception as exc:
            return OpportunityAssessmentResult(
                draft=OpportunityAssessmentDraft(
                    assessment_status="abstained",
                    evidence_ids=list(bundle.get("evidence_ids") or []),
                    missing_evidence=list(bundle.get("missing_evidence") or []),
                    abstention_reason=(
                        "模型机会判断发生技术失败，未生成可审核判断卡："
                        f"{type(exc).__name__}: {redact_tool_error(str(exc))[:500]}"
                    ),
                ),
                engine=f"{self.name}-failed",
                model=self.model,
                version=self.version,
                generation_status="failed",
            )


def validate_assessment_evidence(
    candidate: dict,
    bundle: dict,
    evidence: list[dict],
    draft: OpportunityAssessmentDraft,
) -> None:
    event_id = int(candidate["event_id"])
    known_ids = {int(item["id"]) for item in evidence if int(item["event_id"]) == event_id}
    bundle_ids = {int(value) for value in bundle.get("evidence_ids") or []}
    referenced = {int(value) for value in draft.evidence_ids}
    declared = set(referenced)
    for claim in [*draft.fact_claims, *draft.inferences]:
        claim_ids = {int(value) for value in claim.evidence_ids}
        if not claim_ids.issubset(declared):
            raise ValueError("claim citations must be declared in assessment evidence_ids")
        referenced.update(claim_ids)
    for judgment in (
        draft.consumer_change_judgment,
        draft.problem_judgment,
        draft.research_recommendation,
    ):
        if judgment is None:
            continue
        judgment_ids = {int(value) for value in judgment.evidence_ids}
        if not judgment_ids.issubset(declared):
            raise ValueError("judgment citations must be declared in assessment evidence_ids")
        referenced.update(judgment_ids)
    if not referenced.issubset(known_ids):
        raise ValueError("assessment references unknown or cross-event evidence")
    if not referenced.issubset(bundle_ids):
        raise ValueError("assessment evidence is not part of the candidate EvidenceBundle")


def persist_opportunity_assessment(
    db: Database,
    candidate: dict,
    bundle: dict,
    result: OpportunityAssessmentResult,
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    draft = result.draft
    generation_status = getattr(result, "generation_status", "completed")
    assessment_id = db.execute(
        """INSERT INTO opportunity_assessments
        (candidate_id,evidence_bundle_id,research_run_id,assessment_status,
         change_type,consumer_relevance,durability,lead_time_fit,target_users_json,
         new_scenarios_json,existing_solutions_json,solution_gaps_json,
         unmet_needs_json,related_product_categories_json,
         consumer_change_judgment_json,problem_judgment_json,
         research_recommendation_json,
         fact_claims_json,inferences_json,evidence_ids_json,missing_evidence_json,
         abstention_reason,generation_status,review_status,review_details_json,
         engine,model,version,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'{}',?,?,?,?,?)""",
        (
            candidate["id"],
            bundle["id"],
            draft.research_run_id,
            draft.assessment_status,
            draft.change_type,
            draft.consumer_relevance,
            draft.durability,
            draft.lead_time_fit,
            db.json(draft.target_users),
            db.json(draft.new_scenarios),
            db.json(draft.existing_solutions),
            db.json(draft.solution_gaps),
            db.json(draft.unmet_needs),
            db.json(draft.related_product_categories),
            db.json(
                draft.consumer_change_judgment.model_dump()
                if draft.consumer_change_judgment
                else {}
            ),
            db.json(
                draft.problem_judgment.model_dump()
                if draft.problem_judgment
                else {}
            ),
            db.json(
                draft.research_recommendation.model_dump()
                if draft.research_recommendation
                else {}
            ),
            db.json([item.model_dump() for item in draft.fact_claims]),
            db.json([item.model_dump() for item in draft.inferences]),
            db.json(draft.evidence_ids),
            db.json(draft.missing_evidence),
            draft.abstention_reason,
            generation_status,
            "superseded" if generation_status == "failed" else "pending",
            result.engine,
            result.model,
            result.version,
            now,
            now,
        ),
    )
    row = db.one("SELECT * FROM opportunity_assessments WHERE id=?", (assessment_id,))
    if row is None:
        raise RuntimeError("failed to persist opportunity assessment")
    return decode_opportunity_assessment(row)


def decode_opportunity_assessment(row: dict) -> dict:
    decoded = dict(row)
    for column in (
        "target_users_json",
        "new_scenarios_json",
        "existing_solutions_json",
        "solution_gaps_json",
        "unmet_needs_json",
        "related_product_categories_json",
        "fact_claims_json",
        "inferences_json",
        "evidence_ids_json",
        "missing_evidence_json",
    ):
        decoded[column.removesuffix("_json")] = json.loads(decoded[column] or "[]")
    for column in (
        "consumer_change_judgment_json",
        "problem_judgment_json",
        "research_recommendation_json",
        "review_details_json",
    ):
        decoded[column.removesuffix("_json")] = json.loads(decoded.get(column) or "{}")
    decoded["generation_status"] = decoded.get("generation_status") or "completed"
    return decoded
