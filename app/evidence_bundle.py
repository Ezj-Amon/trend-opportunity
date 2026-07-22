from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from .evidence_quality import (
    MIN_FULL_ARTICLE_CHARACTERS,
    MIN_SUMMARY_CHARACTERS,
    content_near_duplicate,
    is_signal_page_url,
    registrable_domain,
)
from .evidence_types import EvidenceType, FetchStatus, normalize_fetch_status

if TYPE_CHECKING:
    from .db import Database


EVIDENCE_QUALITY_VERSION = "evidence-quality-v2"
DEFAULT_BUNDLE_VERSION = "evidence-bundle-v2"
DEFAULT_READY_SCORE = 1.8

EVIDENCE_WEIGHTS = {
    EvidenceType.OFFICIAL_NOTICE: 1.00,
    EvidenceType.FULL_ARTICLE: 0.90,
    EvidenceType.CONSUMER_DISCUSSION: 0.85,
    EvidenceType.CONSUMER_COMMENT: 0.75,
    EvidenceType.MANUAL_EVIDENCE: 0.70,
    EvidenceType.ARTICLE_SUMMARY: 0.55,
    EvidenceType.SEARCH_SNIPPET: 0.30,
    EvidenceType.TITLE_ONLY: 0.10,
}


@dataclass(frozen=True, slots=True)
class EvidenceBundleResult:
    event_id: int
    input_hash: str
    version: str
    readiness_status: str
    readiness_score: float
    full_text_count: int
    title_only_count: int
    independent_source_count: int
    consumer_voice_count: int
    official_source_count: int
    evidence_ids: list[int]
    fetch_failure_reasons: list[dict[str, Any]]
    missing_evidence: list[str]


def classify_evidence_strength(row: dict) -> EvidenceType:
    kind = str(row.get("kind") or "").strip().casefold()
    explicit = str(row.get("evidence_type") or "").strip()
    status = normalize_fetch_status(row)
    excerpt = str(row.get("excerpt") or "").strip()
    valid = bool(row.get("valid_for_analysis", 1))

    if kind == "hotlist" or is_signal_page_url(str(row.get("url") or "")):
        return EvidenceType.TITLE_ONLY
    if status != FetchStatus.READY:
        return EvidenceType.TITLE_ONLY
    if kind in {"official", "official_notice"} and valid:
        return EvidenceType.OFFICIAL_NOTICE
    if kind in {"manual", "manual_evidence"}:
        return EvidenceType.MANUAL_EVIDENCE
    if explicit in EvidenceType._value2member_map_:
        evidence_type = EvidenceType(explicit)
        if evidence_type == EvidenceType.FULL_ARTICLE:
            if valid and len(excerpt) >= MIN_FULL_ARTICLE_CHARACTERS:
                return EvidenceType.FULL_ARTICLE
            if valid and len(excerpt) >= MIN_SUMMARY_CHARACTERS:
                return EvidenceType.ARTICLE_SUMMARY
            return EvidenceType.TITLE_ONLY
        if evidence_type == EvidenceType.ARTICLE_SUMMARY:
            return (
                EvidenceType.ARTICLE_SUMMARY
                if valid and len(excerpt) >= MIN_SUMMARY_CHARACTERS
                else EvidenceType.TITLE_ONLY
            )
        return evidence_type
    if kind == "article" and valid:
        if len(excerpt) >= MIN_FULL_ARTICLE_CHARACTERS:
            return EvidenceType.FULL_ARTICLE
        if len(excerpt) >= MIN_SUMMARY_CHARACTERS:
            return EvidenceType.ARTICLE_SUMMARY
    return EvidenceType.TITLE_ONLY


def calculate_evidence_quality(row: dict) -> float:
    evidence_type = classify_evidence_strength(row)
    status = normalize_fetch_status(row)
    if status != FetchStatus.READY and evidence_type != EvidenceType.TITLE_ONLY:
        return 0.0
    if evidence_type != EvidenceType.TITLE_ONLY and not bool(
        row.get("valid_for_analysis", 1)
    ):
        return 0.0
    return EVIDENCE_WEIGHTS[evidence_type]


def _source_identity(row: dict) -> str:
    url = str(row.get("url") or "").strip()
    domain = registrable_domain(url)
    if domain:
        return domain
    return str(row.get("source_name") or "unknown").strip().casefold() or "unknown"


def _independent_source_rows(typed: list[tuple[dict, EvidenceType]]) -> list[dict]:
    candidates = [
        row
        for row, evidence_type in typed
        if bool(row.get("valid_for_analysis"))
        and normalize_fetch_status(row) == FetchStatus.READY
        and evidence_type not in {EvidenceType.TITLE_ONLY, EvidenceType.SEARCH_SNIPPET}
    ]
    representatives: list[dict] = []
    used_domains: set[str] = set()
    for row in sorted(
        candidates,
        key=lambda item: calculate_evidence_quality(item),
        reverse=True,
    ):
        domain = _source_identity(row)
        if domain in used_domains:
            continue
        excerpt = str(row.get("excerpt") or "")
        if any(
            content_near_duplicate(excerpt, str(existing.get("excerpt") or ""))
            for existing in representatives
        ):
            continue
        representatives.append(row)
        used_domains.add(domain)
    return representatives


def _bundle_input_hash(evidence: list[dict]) -> str:
    payload = []
    for row in sorted(evidence, key=lambda item: int(item.get("id") or 0)):
        payload.append(
            {
                "id": int(row.get("id") or 0),
                "content_hash": row.get("content_hash") or "",
                "url": str(row.get("url") or ""),
                "evidence_type": classify_evidence_strength(row).value,
                "fetch_status": normalize_fetch_status(row).value,
                "source": _source_identity(row),
                "quality": calculate_evidence_quality(row),
                "consumer": bool(row.get("is_consumer_voice")),
                "title": str(row.get("title") or ""),
                "excerpt": str(row.get("excerpt") or ""),
            }
        )
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_evidence_bundle(
    event: dict,
    evidence: list[dict],
    version: str = DEFAULT_BUNDLE_VERSION,
    ready_score: float = DEFAULT_READY_SCORE,
) -> EvidenceBundleResult:
    usable = [row for row in evidence if int(row.get("id") or 0) > 0]
    typed = [(row, classify_evidence_strength(row)) for row in usable]
    readiness_score = round(sum(calculate_evidence_quality(row) for row in usable), 4)
    full_types = {EvidenceType.FULL_ARTICLE, EvidenceType.OFFICIAL_NOTICE}
    full_text_count = sum(
        evidence_type in full_types
        and bool(row.get("valid_for_analysis"))
        and normalize_fetch_status(row) == FetchStatus.READY
        for row, evidence_type in typed
    )
    title_only_count = sum(
        evidence_type == EvidenceType.TITLE_ONLY for _, evidence_type in typed
    )
    official_source_count = sum(
        evidence_type == EvidenceType.OFFICIAL_NOTICE for _, evidence_type in typed
    )
    consumer_voice_count = sum(
        bool(row.get("is_consumer_voice"))
        or evidence_type
        in {EvidenceType.CONSUMER_DISCUSSION, EvidenceType.CONSUMER_COMMENT}
        for row, evidence_type in typed
    )
    independent_source_count = len(_independent_source_rows(typed))

    ready = (
        independent_source_count >= 2
        and full_text_count >= 1
    )
    if ready:
        readiness_status = "ready_for_assessment"
    elif full_text_count >= 1 or readiness_score >= 0.55:
        readiness_status = "partial"
    else:
        readiness_status = "insufficient"

    missing_evidence: list[str] = []
    if independent_source_count < 2:
        missing_evidence.append("至少需要 2 个独立来源")
    if full_text_count < 1:
        missing_evidence.append("至少需要 1 条完整正文或官方公告")
    # ``ready_score`` is retained for API compatibility and as a diagnostic
    # display value. Readiness itself follows the product rule: two genuinely
    # independent sources, including at least one full article or official notice.
    # This deliberately permits a reliable summary to be the second source.
    _ = ready_score
    if consumer_voice_count < 1:
        missing_evidence.append("缺少公开消费者声音（非强制，但有助于验证具体痛点）")

    failures = []
    for row in usable:
        status = normalize_fetch_status(row)
        if status != FetchStatus.READY:
            failures.append(
                {
                    "evidence_id": int(row["id"]),
                    "status": status.value,
                    "source_name": str(row.get("source_name") or _source_identity(row)),
                    "url": str(row.get("url") or ""),
                    "detail": str(row.get("error") or ""),
                }
            )

    return EvidenceBundleResult(
        event_id=int(event["id"]),
        input_hash=_bundle_input_hash(usable),
        version=version,
        readiness_status=readiness_status,
        readiness_score=readiness_score,
        full_text_count=full_text_count,
        title_only_count=title_only_count,
        independent_source_count=independent_source_count,
        consumer_voice_count=consumer_voice_count,
        official_source_count=official_source_count,
        evidence_ids=[int(row["id"]) for row in usable],
        fetch_failure_reasons=failures,
        missing_evidence=missing_evidence,
    )


def persist_evidence_bundle(db: Database, bundle: EvidenceBundleResult) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        """INSERT OR IGNORE INTO evidence_bundles
        (event_id,input_hash,version,readiness_status,readiness_score,
         full_text_count,title_only_count,independent_source_count,
         consumer_voice_count,official_source_count,evidence_ids_json,
         fetch_failure_reasons_json,missing_evidence_json,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            bundle.event_id,
            bundle.input_hash,
            bundle.version,
            bundle.readiness_status,
            bundle.readiness_score,
            bundle.full_text_count,
            bundle.title_only_count,
            bundle.independent_source_count,
            bundle.consumer_voice_count,
            bundle.official_source_count,
            db.json(bundle.evidence_ids),
            db.json(bundle.fetch_failure_reasons),
            db.json(bundle.missing_evidence),
            now,
        ),
    )
    row = db.one(
        """SELECT * FROM evidence_bundles
        WHERE event_id=? AND input_hash=? AND version=?""",
        (bundle.event_id, bundle.input_hash, bundle.version),
    )
    if row is None:
        raise RuntimeError("failed to persist evidence bundle")
    return decode_evidence_bundle(row)


def decode_evidence_bundle(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    for column in (
        "evidence_ids_json",
        "fetch_failure_reasons_json",
        "missing_evidence_json",
    ):
        decoded[column.removesuffix("_json")] = json.loads(decoded[column] or "[]")
    return decoded


def bundle_as_dict(bundle: EvidenceBundleResult) -> dict[str, Any]:
    return asdict(bundle)
