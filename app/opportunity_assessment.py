from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

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


class OpportunityAssessmentDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assessment_status: str
    change_type: str = Field(default="", max_length=500)
    consumer_relevance: str = Field(default="", max_length=4000)
    durability: str = Field(default="", max_length=1000)
    lead_time_fit: str = Field(default="", max_length=1000)
    target_users: list[str] = Field(default_factory=list, max_length=20)
    new_scenarios: list[str] = Field(default_factory=list, max_length=20)
    unmet_needs: list[str] = Field(default_factory=list, max_length=20)
    related_product_categories: list[str] = Field(default_factory=list, max_length=20)
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
                or not self.related_product_categories
            ):
                raise ValueError("worth_following assessment requires users, scenarios and needs")
            if not self.fact_claims or not self.evidence_ids:
                raise ValueError("worth_following assessment requires cited facts and evidence")
        elif not self.abstention_reason.strip():
            raise ValueError("abstained assessment requires a reason")
        return self


@dataclass(slots=True)
class OpportunityAssessmentResult:
    draft: OpportunityAssessmentDraft
    engine: str
    model: str
    version: str


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
        version: str = "cloud-assessment-v1",
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
                            "Assess durable consumer change using only supplied evidence. "
                            "Every fact and inference must cite supplied evidence IDs. "
                            "Abstain when evidence is insufficient. Never output product names, "
                            "marketplace queries, prices, ProductHypotheses, or recommendations."
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
                        "模型机会判断失败，显式弃权："
                        f"{type(exc).__name__}: {redact_tool_error(str(exc))[:500]}"
                    ),
                ),
                engine=f"{self.name}-failed",
                model=self.model,
                version=self.version,
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
    assessment_id = db.execute(
        """INSERT INTO opportunity_assessments
        (candidate_id,evidence_bundle_id,research_run_id,assessment_status,
         change_type,consumer_relevance,durability,lead_time_fit,target_users_json,
         new_scenarios_json,unmet_needs_json,related_product_categories_json,
         fact_claims_json,inferences_json,evidence_ids_json,missing_evidence_json,
         abstention_reason,review_status,engine,model,version,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?,?)""",
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
            db.json(draft.unmet_needs),
            db.json(draft.related_product_categories),
            db.json([item.model_dump() for item in draft.fact_claims]),
            db.json([item.model_dump() for item in draft.inferences]),
            db.json(draft.evidence_ids),
            db.json(draft.missing_evidence),
            draft.abstention_reason,
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
        "unmet_needs_json",
        "related_product_categories_json",
        "fact_claims_json",
        "inferences_json",
        "evidence_ids_json",
        "missing_evidence_json",
    ):
        decoded[column.removesuffix("_json")] = json.loads(decoded[column] or "[]")
    return decoded
