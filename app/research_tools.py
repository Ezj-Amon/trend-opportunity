from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable

from pydantic import BaseModel, Field

from .db import Database
from .evidence import EvidenceResult, fetch_evidence
from .evidence_bundle import (
    build_evidence_bundle,
    decode_evidence_bundle,
    persist_evidence_bundle,
)
from .evidence_collectors import (
    CollectedEvidence,
    PublicNewsSearchCollector,
    RelatedNewsCollector,
    decode_evidence,
    persist_collected_evidence,
)
from .news_search import PublicNewsSearchProvider
from .research import (
    ResearchBudget,
    ResearchToolResultInput,
    decode_research_tool_call,
    record_research_tool_call,
    research_request_hash,
)
from .research_candidates import decode_research_candidate


EvidenceFetcher = Callable[[str, str], Awaitable[EvidenceResult]]
CONTROLLED_RESEARCH_TOOLS = {
    "get_context",
    "fetch_public_page",
    "collect_related_news",
    "rebuild_evidence_bundle",
}


class ResearchToolExecutionInput(BaseModel):
    request: dict = Field(default_factory=dict)


class ResearchToolExecutor:
    def __init__(
        self,
        db: Database,
        *,
        evidence_bundle_version: str = "evidence-bundle-v2",
        evidence_ready_score: float = 1.8,
        fetcher: EvidenceFetcher = fetch_evidence,
        news_search_provider: PublicNewsSearchProvider | None = None,
        public_news_max_results: int = 8,
    ):
        self.db = db
        self.evidence_bundle_version = evidence_bundle_version
        self.evidence_ready_score = evidence_ready_score
        self.fetcher = fetcher
        self.news_search_provider = news_search_provider
        self.public_news_max_results = public_news_max_results

    async def execute(self, run: dict, tool_name: str, request: dict) -> dict:
        if run["status"] != "running":
            raise ValueError("research run is not running")
        if tool_name not in CONTROLLED_RESEARCH_TOOLS:
            raise ValueError("unsupported research tool")
        request_hash = research_request_hash(request)
        existing = self.db.one(
            """SELECT * FROM research_tool_calls
            WHERE run_id=? AND tool_name=? AND request_hash=? ORDER BY id DESC LIMIT 1""",
            (run["id"], tool_name, request_hash),
        )
        if existing:
            return {
                "replayed": True,
                "tool_call": decode_research_tool_call(existing),
                "result": await self._current_result(run, tool_name),
            }
        budget = ResearchBudget.model_validate(run.get("budget") or {})
        self._check_budget(run, tool_name, budget)
        started = time.perf_counter()
        try:
            result, evidence_ids = await asyncio.wait_for(
                self._execute_once(run, tool_name, request, budget),
                timeout=budget.timeout_seconds,
            )
            status = "completed"
            error = ""
        except asyncio.TimeoutError:
            result, evidence_ids = {}, []
            status = "failed"
            error = "research tool timed out"
        except Exception as exc:
            result, evidence_ids = {}, []
            status = "failed"
            error = f"{type(exc).__name__}: {str(exc)[:1800]}"
        tool_call = record_research_tool_call(
            self.db,
            run,
            ResearchToolResultInput(
                tool_name=tool_name,
                request=request,
                status=status,
                result_evidence_ids=evidence_ids,
                latency_ms=round((time.perf_counter() - started) * 1000),
                error=error,
            ),
        )
        if status == "failed":
            raise ValueError(error)
        return {"replayed": False, "tool_call": tool_call, "result": result}

    def _context(self, run: dict) -> tuple[dict, dict, dict, list[dict]]:
        candidate_row = self.db.one(
            "SELECT * FROM research_candidates WHERE id=?", (run["candidate_id"],)
        )
        if not candidate_row:
            raise ValueError("research candidate not found")
        candidate = decode_research_candidate(candidate_row)
        event = self.db.one(
            "SELECT * FROM trend_events WHERE id=?", (candidate["event_id"],)
        )
        bundle_row = self.db.one(
            "SELECT * FROM evidence_bundles WHERE id=?",
            (candidate["evidence_bundle_id"],),
        )
        if not event or not bundle_row:
            raise ValueError("research context is incomplete")
        bundle = decode_evidence_bundle(bundle_row)
        evidence = [
            decode_evidence(row)
            for row in self.db.all(
                "SELECT * FROM evidence WHERE event_id=? ORDER BY id",
                (candidate["event_id"],),
            )
        ]
        return candidate, event, bundle, evidence

    async def _current_result(self, run: dict, tool_name: str) -> dict:
        candidate, event, bundle, evidence = self._context(run)
        if tool_name == "get_context":
            return {
                "candidate": candidate,
                "event": event,
                "evidence_bundle": bundle,
                "evidence": evidence,
            }
        latest = self.db.one(
            "SELECT * FROM evidence_bundles WHERE event_id=? ORDER BY id DESC LIMIT 1",
            (event["id"],),
        )
        return {
            "evidence_bundle": decode_evidence_bundle(latest) if latest else bundle,
            "evidence": evidence,
        }

    def _check_budget(
        self, run: dict, tool_name: str, budget: ResearchBudget
    ) -> None:
        if tool_name not in {"fetch_public_page", "collect_related_news"}:
            return
        if self._used_fetch_pages(run) >= budget.max_fetch_pages:
            raise ValueError("research fetch-page budget exhausted")

    def _used_fetch_pages(self, run: dict) -> int:
        used = 0
        for row in self.db.all(
            """SELECT tool_name,result_evidence_ids_json FROM research_tool_calls
            WHERE run_id=? AND tool_name IN ('fetch_public_page','collect_related_news')""",
            (run["id"],),
        ):
            if row["tool_name"] == "fetch_public_page":
                used += 1
                continue
            try:
                evidence_ids = json.loads(row["result_evidence_ids_json"] or "[]")
                used += max(1, len(evidence_ids))
            except (TypeError, ValueError):
                used += 1
        return used

    async def _execute_once(
        self,
        run: dict,
        tool_name: str,
        request: dict,
        budget: ResearchBudget,
    ) -> tuple[dict, list[int]]:
        candidate, event, bundle, evidence = self._context(run)
        if tool_name == "get_context":
            return (
                {
                    "candidate": candidate,
                    "event": event,
                    "evidence_bundle": bundle,
                    "evidence": evidence,
                },
                [],
            )
        if tool_name == "rebuild_evidence_bundle":
            rebuilt = self._rebuild(event, int(candidate["id"]))
            return {"evidence_bundle": rebuilt}, []
        if tool_name == "fetch_public_page":
            url = str(request.get("url") or "").strip()
            if not url:
                raise ValueError("fetch_public_page requires url")
            title = str(request.get("title") or event["canonical_title"])
            result = await self.fetcher(url, title)
            saved = persist_collected_evidence(
                self.db,
                int(event["id"]),
                CollectedEvidence(
                    evidence_type=result.evidence_type,
                    source_name=str(request.get("source_name") or "research"),
                    url=result.url,
                    title=result.title,
                    excerpt=result.excerpt,
                    fetch_method="research-tool-public-page",
                    fetch_status=result.fetch_status,
                    fetched_at=result.fetched_at,
                    http_status=result.http_status,
                    content_hash=result.content_hash,
                    error=result.error,
                    raw_metadata=result.raw_metadata,
                ),
                allow_upgrade=True,
            )
            rebuilt = self._rebuild(event, int(candidate["id"]))
            return {"evidence": saved, "evidence_bundle": rebuilt}, [int(saved["id"])]
        source_items = self.db.all(
            """SELECT i.* FROM source_items i JOIN event_members m ON m.source_item_id=i.id
            WHERE m.event_id=? ORDER BY i.rank""",
            (event["id"],),
        )
        used = self._used_fetch_pages(run)
        collector = RelatedNewsCollector(fetcher=self.fetcher)
        items = await collector.collect(
            {**event, "source_items": source_items},
            evidence,
            ResearchBudget(
                **{
                    **budget.model_dump(),
                    "max_fetch_pages": budget.max_fetch_pages - used,
                }
            ),
        )
        saved_items = [
            persist_collected_evidence(
                self.db, int(event["id"]), item, allow_upgrade=True
            )
            for item in items
        ]
        remaining = max(0, budget.max_fetch_pages - used - len(items))
        if remaining and self.news_search_provider is not None:
            current_evidence = self.db.all(
                "SELECT * FROM evidence WHERE event_id=? ORDER BY id",
                (event["id"],),
            )
            searched = await PublicNewsSearchCollector(
                self.news_search_provider,
                fetcher=self.fetcher,
                max_results=self.public_news_max_results,
            ).collect(
                {**event, "source_items": source_items},
                current_evidence,
                ResearchBudget(
                    **{
                        **budget.model_dump(),
                        "max_fetch_pages": remaining,
                    }
                ),
            )
            saved_items.extend(
                persist_collected_evidence(
                    self.db, int(event["id"]), item, allow_upgrade=True
                )
                for item in searched
            )
        rebuilt = self._rebuild(event, int(candidate["id"]))
        return {
            "evidence": saved_items,
            "evidence_bundle": rebuilt,
        }, [int(item["id"]) for item in saved_items]

    def _rebuild(self, event: dict, candidate_id: int) -> dict:
        evidence = self.db.all(
            "SELECT * FROM evidence WHERE event_id=? ORDER BY id", (event["id"],)
        )
        bundle = persist_evidence_bundle(
            self.db,
            build_evidence_bundle(
                event,
                evidence,
                self.evidence_bundle_version,
                self.evidence_ready_score,
            ),
        )
        self.db.execute(
            """UPDATE research_candidates
            SET evidence_bundle_id=?,missing_evidence_json=?,updated_at=?
            WHERE id=? AND status='researching'""",
            (
                bundle["id"],
                self.db.json(bundle["missing_evidence"]),
                datetime.now(timezone.utc).isoformat(),
                candidate_id,
            ),
        )
        return bundle
