from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator, Awaitable, Callable, Protocol

import httpx

from .db import Database
from .evidence import EvidenceResult, fetch_evidence
from .evidence_bundle import (
    EVIDENCE_QUALITY_VERSION,
    calculate_evidence_quality,
    classify_evidence_strength,
)
from .evidence_quality import registrable_domain
from .evidence_types import EvidenceType, FetchStatus, ManualEvidenceInput
from .news_search import PublicNewsSearchProvider, build_fact_search_queries
from .research import ResearchBudget


EvidenceFetcher = Callable[[str, str], Awaitable[EvidenceResult]]


@dataclass(slots=True)
class CollectedEvidence:
    evidence_type: str
    source_name: str
    url: str
    title: str
    excerpt: str
    fetch_method: str
    fetch_status: str
    fetched_at: str
    http_status: int | None = None
    content_hash: str | None = None
    is_consumer_voice: bool = False
    error: str | None = None
    raw_metadata: dict = field(default_factory=dict)


class EvidenceCollector(Protocol):
    name: str

    async def collect(
        self,
        event: dict,
        current_evidence: list[dict],
        budget: ResearchBudget,
    ) -> list[CollectedEvidence]: ...


def _from_result(result: EvidenceResult, source_name: str, fetch_method: str) -> CollectedEvidence:
    return CollectedEvidence(
        evidence_type=result.evidence_type,
        source_name=source_name,
        url=result.url,
        title=result.title,
        excerpt=result.excerpt,
        fetch_method=fetch_method,
        fetch_status=result.fetch_status,
        fetched_at=result.fetched_at,
        http_status=result.http_status,
        content_hash=result.content_hash,
        error=result.error,
        raw_metadata=result.raw_metadata,
    )


class DirectPublicPageCollector:
    name = "direct-public-page"

    def __init__(self, fetcher: EvidenceFetcher = fetch_evidence):
        self.fetcher = fetcher

    async def collect(
        self,
        event: dict,
        current_evidence: list[dict],
        budget: ResearchBudget,
    ) -> list[CollectedEvidence]:
        collected: list[CollectedEvidence] = []
        targets = [
            row
            for row in current_evidence
            if row.get("url")
            and str(row.get("evidence_type") or "title_only") == EvidenceType.TITLE_ONLY.value
        ]
        for row in targets[: budget.max_fetch_pages]:
            result = await self.fetcher(str(row["url"]), str(row.get("title") or event["canonical_title"]))
            collected.append(
                _from_result(
                    result,
                    str(row.get("source_name") or "direct"),
                    self.name,
                )
            )
        return collected


def related_news_targets(source_items: list[dict]) -> list[tuple[str, str, str]]:
    targets: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for item in source_items:
        raw = item.get("raw")
        if raw is None:
            try:
                raw = json.loads(item.get("raw_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                raw = {}
        if not isinstance(raw, dict):
            continue
        urls = raw.get("news_urls") or raw.get("related_urls") or []
        titles = raw.get("news_titles") or raw.get("related_titles") or []
        if not isinstance(urls, list):
            continue
        for index, url in enumerate(urls):
            normalized_url = str(url or "").strip()
            if not normalized_url or normalized_url in seen:
                continue
            title = (
                str(titles[index]).strip()
                if isinstance(titles, list) and index < len(titles) and titles[index]
                else str(item.get("title") or "")
            )
            targets.append((normalized_url, title, str(item.get("source") or "related-news")))
            seen.add(normalized_url)
    return targets


class RelatedNewsCollector:
    name = "related-news"

    def __init__(self, fetcher: EvidenceFetcher = fetch_evidence):
        self.fetcher = fetcher

    async def collect(
        self,
        event: dict,
        current_evidence: list[dict],
        budget: ResearchBudget,
    ) -> list[CollectedEvidence]:
        return [
            item
            async for item in self.iter_collect(event, current_evidence, budget)
        ]

    async def iter_collect(
        self,
        event: dict,
        current_evidence: list[dict],
        budget: ResearchBudget,
    ) -> AsyncIterator[CollectedEvidence]:
        existing_urls = {str(row.get("url") or "") for row in current_evidence}
        targets = [
            item
            for item in related_news_targets(event.get("source_items") or [])
            if item[0] not in existing_urls
        ]
        for url, title, source_name in targets[: budget.max_fetch_pages]:
            result = await self.fetcher(url, title or str(event["canonical_title"]))
            yield _from_result(result, source_name, self.name)


class PublicNewsSearchCollector:
    name = "public-news-search"

    def __init__(
        self,
        provider: PublicNewsSearchProvider,
        fetcher: EvidenceFetcher = fetch_evidence,
        *,
        max_results: int = 8,
    ):
        self.provider = provider
        self.fetcher = fetcher
        self.max_results = max_results

    async def collect(
        self,
        event: dict,
        current_evidence: list[dict],
        budget: ResearchBudget,
    ) -> list[CollectedEvidence]:
        return [
            item
            async for item in self.iter_collect(event, current_evidence, budget)
        ]

    async def iter_collect(
        self,
        event: dict,
        current_evidence: list[dict],
        budget: ResearchBudget,
    ) -> AsyncIterator[CollectedEvidence]:
        if budget.max_search_queries <= 0 or budget.max_fetch_pages <= 0:
            return
        source_items = event.get("source_items") or []
        queries = build_fact_search_queries(
            event, source_items, budget.max_search_queries
        )
        existing_urls = {str(row.get("url") or "") for row in current_evidence}
        existing_domains = {
            registrable_domain(str(row.get("url") or ""))
            for row in current_evidence
            if bool(row.get("valid_for_analysis"))
        }
        existing_domains.discard("")
        selected = []
        selected_domains: set[str] = set()
        for query in queries:
            try:
                hits = await self.provider.search(
                    query,
                    market=str(event.get("market") or "US"),
                    language=str(event.get("language") or "all"),
                    limit=max(self.max_results, budget.max_fetch_pages),
                )
            except (httpx.HTTPError, ValueError, OSError):
                continue
            for hit in hits:
                domain = registrable_domain(hit.url)
                if (
                    not domain
                    or hit.url in existing_urls
                    or domain in existing_domains
                    or domain in selected_domains
                ):
                    continue
                selected.append((hit, query))
                selected_domains.add(domain)
                if len(selected) >= budget.max_fetch_pages:
                    break
            if len(selected) >= budget.max_fetch_pages:
                break

        for hit, _query in selected:
            result = await self.fetcher(hit.url, hit.title)
            metadata = {
                **result.raw_metadata,
                "search_provider": hit.provider,
                "search_query_hash": hit.query_hash,
                "search_rank": hit.rank,
                "search_published_at": hit.published_at,
                "search_provider_url": hit.provider_url,
            }
            yield CollectedEvidence(
                evidence_type=result.evidence_type,
                source_name=(
                    registrable_domain(result.url)
                    or hit.source_name
                    or hit.provider
                ),
                url=result.url,
                title=result.title,
                excerpt=result.excerpt,
                fetch_method=self.name,
                fetch_status=result.fetch_status,
                fetched_at=result.fetched_at,
                http_status=result.http_status,
                content_hash=result.content_hash,
                error=result.error,
                raw_metadata=metadata,
            )


class ManualEvidenceCollector:
    name = "manual"

    def __init__(self, values: list[ManualEvidenceInput]):
        self.values = values

    async def collect(
        self,
        event: dict,
        current_evidence: list[dict],
        budget: ResearchBudget,
    ) -> list[CollectedEvidence]:
        del event, current_evidence, budget
        now = datetime.now(timezone.utc).isoformat()
        collected = []
        for value in self.values:
            content_hash = hashlib.sha256(
                f"{value.source_name}\n{value.url}\n{value.title}\n{value.excerpt}".encode("utf-8")
            ).hexdigest()
            collected.append(
                CollectedEvidence(
                    evidence_type=value.evidence_type.value,
                    source_name=value.source_name.strip(),
                    url=value.url.strip(),
                    title=value.title.strip(),
                    excerpt=value.excerpt.strip(),
                    fetch_method=self.name,
                    fetch_status=FetchStatus.READY.value,
                    fetched_at=now,
                    content_hash=content_hash,
                    is_consumer_voice=value.is_consumer_voice,
                    raw_metadata={"note": value.note.strip()},
                )
            )
        return collected


def persist_collected_evidence(
    db: Database,
    event_id: int,
    item: CollectedEvidence,
    *,
    allow_upgrade: bool,
) -> dict:
    content_hash = item.content_hash or hashlib.sha256(
        f"{item.title}\n{item.excerpt}".encode("utf-8")
    ).hexdigest()
    url = item.url.strip() or f"manual://{content_hash}"
    evidence_type = EvidenceType(item.evidence_type)
    fetch_status = FetchStatus(item.fetch_status)
    kind = {
        EvidenceType.FULL_ARTICLE: "article",
        EvidenceType.ARTICLE_SUMMARY: "article",
        EvidenceType.OFFICIAL_NOTICE: "official_notice",
        EvidenceType.CONSUMER_DISCUSSION: "consumer_discussion",
        EvidenceType.CONSUMER_COMMENT: "consumer_comment",
    }.get(evidence_type, evidence_type.value)
    row_for_quality = {
        "kind": kind,
        "evidence_type": evidence_type.value,
        "fetch_status": fetch_status.value,
        "excerpt": item.excerpt,
        "valid_for_analysis": int(
            fetch_status == FetchStatus.READY and evidence_type != EvidenceType.TITLE_ONLY
        ),
    }
    classified_type = classify_evidence_strength(row_for_quality)
    if classified_type != evidence_type:
        item.raw_metadata = {
            **item.raw_metadata,
            "declared_evidence_type": evidence_type.value,
            "classification_note": "content strength normalized during persistence",
        }
        evidence_type = classified_type
        row_for_quality["evidence_type"] = evidence_type.value
    quality = calculate_evidence_quality(row_for_quality)
    valid_for_analysis = int(
        fetch_status == FetchStatus.READY
        and evidence_type not in {EvidenceType.TITLE_ONLY, EvidenceType.SEARCH_SNIPPET}
    )
    params = (
        event_id,
        kind,
        url,
        item.title[:1000],
        item.excerpt[:50_000],
        item.fetched_at,
        item.http_status,
        content_hash,
        int(item.is_consumer_voice),
        valid_for_analysis,
        item.error,
        evidence_type.value,
        item.source_name[:200],
        item.fetch_method[:100],
        fetch_status.value,
        quality,
        EVIDENCE_QUALITY_VERSION,
        db.json(item.raw_metadata),
    )
    if allow_upgrade:
        db.execute(
            """INSERT INTO evidence
            (event_id,kind,url,title,excerpt,fetched_at,http_status,content_hash,
             is_consumer_voice,valid_for_analysis,error,evidence_type,source_name,
             fetch_method,fetch_status,quality_score,quality_version,raw_metadata_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(event_id,url) DO UPDATE SET
              kind=CASE WHEN excluded.quality_score>=evidence.quality_score THEN excluded.kind ELSE evidence.kind END,
              title=CASE WHEN excluded.quality_score>=evidence.quality_score THEN excluded.title ELSE evidence.title END,
              excerpt=CASE WHEN excluded.quality_score>=evidence.quality_score THEN excluded.excerpt ELSE evidence.excerpt END,
              fetched_at=excluded.fetched_at,
              http_status=excluded.http_status,
              content_hash=CASE WHEN excluded.quality_score>=evidence.quality_score THEN excluded.content_hash ELSE evidence.content_hash END,
              is_consumer_voice=MAX(evidence.is_consumer_voice,excluded.is_consumer_voice),
              valid_for_analysis=MAX(evidence.valid_for_analysis,excluded.valid_for_analysis),
              error=CASE WHEN excluded.quality_score>=evidence.quality_score THEN excluded.error ELSE evidence.error END,
              evidence_type=CASE WHEN excluded.quality_score>=evidence.quality_score THEN excluded.evidence_type ELSE evidence.evidence_type END,
              source_name=CASE WHEN evidence.source_name='' THEN excluded.source_name ELSE evidence.source_name END,
              fetch_method=excluded.fetch_method,
              fetch_status=CASE WHEN excluded.quality_score>=evidence.quality_score THEN excluded.fetch_status ELSE evidence.fetch_status END,
              quality_score=MAX(evidence.quality_score,excluded.quality_score),
              quality_version=excluded.quality_version,
              raw_metadata_json=excluded.raw_metadata_json""",
            params,
        )
    else:
        db.execute(
            """INSERT OR IGNORE INTO evidence
            (event_id,kind,url,title,excerpt,fetched_at,http_status,content_hash,
             is_consumer_voice,valid_for_analysis,error,evidence_type,source_name,
             fetch_method,fetch_status,quality_score,quality_version,raw_metadata_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            params,
        )
    row = db.one("SELECT * FROM evidence WHERE event_id=? AND url=?", (event_id, url))
    if row is None:
        raise RuntimeError("failed to persist collected evidence")
    return decode_evidence(row)


def decode_evidence(row: dict) -> dict:
    decoded = dict(row)
    try:
        decoded["raw_metadata"] = json.loads(decoded.get("raw_metadata_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        decoded["raw_metadata"] = {}
    return decoded
