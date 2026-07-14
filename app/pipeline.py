from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from .analysis import Analyzer
from .clustering import normalize_title, should_merge
from .config import Settings
from .db import Database
from .evidence import fetch_evidence
from .scoring import (
    calculate_evidence_confidence,
    calculate_opportunity_score,
    calculate_trend_scores,
)
from .sources import (
    GoogleTrendsSource,
    NewsNowSource,
    RedditOAuthSource,
    SourceResult,
)


ANALYSIS_PROMPT_VERSION = "opportunity-prompt-v6-overseas"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Pipeline:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.source = NewsNowSource(settings.newsnow_base_url)
        self.google_trends_source = GoogleTrendsSource()
        self.reddit_source = (
            RedditOAuthSource(
                settings.reddit_client_id,
                settings.reddit_client_secret,
                settings.reddit_user_agent,
                settings.reddit_subreddits,
            )
            if settings.reddit_client_id and settings.reddit_client_secret
            else None
        )
        self.analyzer = Analyzer(settings)
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._lock.locked()

    @property
    def analysis_version(self) -> str:
        engine = (
            f"llm-{self.settings.openai_model}"
            if self.settings.openai_api_key
            else "local-rules-v1"
        )
        return f"{ANALYSIS_PROMPT_VERSION}:{engine}"

    async def run(self, trigger: str = "manual") -> str:
        if self._lock.locked():
            raise RuntimeError("a pipeline run is already active")
        async with self._lock:
            owner = str(uuid.uuid4())
            if not self.db.acquire_lease("pipeline", owner):
                raise RuntimeError("another process owns the pipeline lease")
            lease_lost = asyncio.Event()
            heartbeat = asyncio.create_task(self._lease_heartbeat(owner, lease_lost))
            try:
                return await self._run_locked(trigger, lease_lost)
            finally:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass
                self.db.release_lease("pipeline", owner)

    async def _lease_heartbeat(self, owner: str, lease_lost: asyncio.Event) -> None:
        while True:
            await asyncio.sleep(60)
            if not self.db.renew_lease("pipeline", owner):
                lease_lost.set()
                return

    @staticmethod
    def _ensure_lease(lease_lost: asyncio.Event) -> None:
        if lease_lost.is_set():
            raise RuntimeError("pipeline lease was lost during execution")

    async def _run_locked(self, trigger: str, lease_lost: asyncio.Event) -> str:
        self._invalidate_old_analysis_versions()
        run_id = str(uuid.uuid4())
        self.db.execute(
            """INSERT INTO pipeline_runs
            (id, trigger, status, stage, started_at, config_json)
            VALUES (?, ?, 'running', 'ingest', ?, ?)""",
            (
                run_id,
                trigger,
                utc_now(),
                self.db.json(
                    {
                        "sources": self.settings.source_ids,
                        "google_trends_geos": self.settings.google_trends_geos,
                        "reddit_configured": bool(self.reddit_source),
                        "newsnow_base_url": self.settings.newsnow_base_url,
                        "analysis_top_n": self.settings.analysis_top_n,
                        "overseas_analysis_top_n": self.settings.overseas_analysis_top_n,
                        "analysis_engine": "llm" if self.settings.openai_api_key else "local-rules",
                    }
                ),
            ),
        )
        try:
            fetches = [
                self.source.fetch(source_id) for source_id in self.settings.source_ids
            ]
            fetches.extend(
                self.google_trends_source.fetch(geo)
                for geo in self.settings.google_trends_geos
            )
            if self.reddit_source:
                fetches.append(self.reddit_source.fetch())
            results = await asyncio.gather(*fetches)
            self._ensure_lease(lease_lost)
            item_ids = self._persist_source_results(run_id, results)
            if not item_ids:
                errors = "; ".join(
                    f"{result.source}: {result.error}" for result in results if result.error
                )
                raise RuntimeError(f"all real data sources failed: {errors[:800]}")
            self._update_run(run_id, stage="cluster", items_count=len(item_ids))
            event_ids = self._cluster_items(item_ids)
            event_ids = self._consolidate_unanalyzed_events(event_ids)
            self._score_events(event_ids)
            self._update_run(run_id, stage="analyze", events_count=len(event_ids))
            selected_ids = self._select_events(event_ids)
            for event_id in selected_ids:
                await self._research_and_analyze(run_id, event_id)
                self._ensure_lease(lease_lost)
            self._update_run(
                run_id,
                status="completed",
                stage="completed",
                selected_count=len(selected_ids),
                finished_at=utc_now(),
            )
        except Exception as exc:
            self._update_run(
                run_id,
                status="failed",
                stage="failed",
                error_summary=f"{type(exc).__name__}: {str(exc)[:1000]}",
                finished_at=utc_now(),
            )
            raise
        return run_id

    def _invalidate_old_analysis_versions(self) -> None:
        self.db.execute(
            """UPDATE product_opportunities SET review_status='superseded', updated_at=?
            WHERE review_status!='superseded' AND analysis_id IN
            (SELECT id FROM analyses WHERE prompt_version!=?)""",
            (utc_now(), self.analysis_version),
        )
        self.db.execute(
            """UPDATE analyses SET status='superseded'
            WHERE prompt_version!=? AND status!='superseded'""",
            (self.analysis_version,),
        )

    def _update_run(self, run_id: str, **updates: Any) -> None:
        allowed = {
            "status",
            "stage",
            "finished_at",
            "items_count",
            "events_count",
            "selected_count",
            "error_summary",
        }
        values = [(key, value) for key, value in updates.items() if key in allowed]
        if not values:
            return
        assignments = ", ".join(f"{key} = ?" for key, _ in values)
        self.db.execute(
            f"UPDATE pipeline_runs SET {assignments} WHERE id = ?",
            tuple(value for _, value in values) + (run_id,),
        )

    def _persist_source_results(
        self, run_id: str, results: list[SourceResult]
    ) -> list[int]:
        item_ids: list[int] = []
        for result in results:
            snapshot_id = self.db.execute(
                """INSERT INTO source_snapshots
                (run_id, source, market, language, signal_type,
                 fetched_at, success, status_code, latency_ms,
                 error, payload_hash, raw_payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    result.source,
                    result.market,
                    result.language,
                    result.signal_type,
                    result.fetched_at,
                    int(result.success),
                    result.status_code,
                    result.latency_ms,
                    result.error,
                    result.payload_hash,
                    self.db.json(result.raw_payload) if result.raw_payload else None,
                ),
            )
            for item in result.items:
                item_id = self.db.execute(
                    """INSERT OR IGNORE INTO source_items
                    (snapshot_id, source, market, language, signal_type,
                     external_id, title, normalized_title, url,
                     rank, item_count, fetched_at, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        snapshot_id,
                        item.source,
                        item.market,
                        item.language,
                        item.signal_type,
                        item.external_id,
                        item.title,
                        normalize_title(item.title),
                        item.url,
                        item.rank,
                        item.item_count,
                        item.fetched_at,
                        self.db.json(item.raw),
                    ),
                )
                if item_id:
                    item_ids.append(item_id)
        return item_ids

    def _cluster_items(self, item_ids: list[int]) -> list[int]:
        if not item_ids:
            return []
        placeholders = ",".join("?" for _ in item_ids)
        items = self.db.all(
            f"SELECT * FROM source_items WHERE id IN ({placeholders}) ORDER BY rank",
            tuple(item_ids),
        )
        since = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        recent_events = self.db.all(
            "SELECT * FROM trend_events WHERE last_seen_at >= ? ORDER BY last_seen_at DESC LIMIT 500",
            (since,),
        )
        touched: set[int] = set()
        for item in items:
            best_event = None
            best_score = 0.0
            best_method = "new"
            for event in recent_events:
                if (
                    item["market"] != event["market"]
                    and item["market"] != "GLOBAL"
                    and event["market"] != "GLOBAL"
                ):
                    continue
                merged, score, method = should_merge(item["title"], event["canonical_title"])
                if merged and score > best_score:
                    best_event = event
                    best_score = score
                    best_method = method
            if best_event is None:
                now = item["fetched_at"]
                event_id = self.db.execute(
                    """INSERT INTO trend_events
                    (canonical_title, normalized_title, market, language, signal_type,
                     first_seen_at, last_seen_at,
                     created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        item["title"], item["normalized_title"], item["market"],
                        item["language"], item["signal_type"], now, now, now, now,
                    ),
                )
                best_event = self.db.one("SELECT * FROM trend_events WHERE id = ?", (event_id,))
                recent_events.append(best_event)
                best_score = 1.0
                best_method = "new"
            event_id = int(best_event["id"])
            self.db.execute(
                """INSERT OR IGNORE INTO event_members
                (event_id, source_item_id, match_method, match_score)
                VALUES (?, ?, ?, ?)""",
                (event_id, item["id"], best_method, round(best_score, 4)),
            )
            self.db.execute(
                """UPDATE trend_events SET
                first_seen_at = CASE WHEN first_seen_at > ? THEN ? ELSE first_seen_at END,
                last_seen_at = CASE WHEN last_seen_at < ? THEN ? ELSE last_seen_at END,
                market = CASE WHEN market = ? THEN market ELSE 'GLOBAL' END,
                language = CASE WHEN language = ? THEN language ELSE 'multi' END,
                signal_type = CASE WHEN signal_type = ? THEN signal_type ELSE 'mixed' END,
                updated_at = ? WHERE id = ?""",
                (
                    item["fetched_at"], item["fetched_at"],
                    item["fetched_at"], item["fetched_at"],
                    item["market"], item["language"], item["signal_type"],
                    utc_now(), event_id,
                ),
            )
            touched.add(event_id)
        return sorted(touched)

    def _score_events(self, event_ids: list[int]) -> None:
        for event_id in event_ids:
            event = self.db.one("SELECT * FROM trend_events WHERE id = ?", (event_id,))
            members = self.db.all(
                """SELECT i.* FROM source_items i
                JOIN event_members m ON m.source_item_id = i.id
                WHERE m.event_id = ? ORDER BY i.fetched_at DESC""",
                (event_id,),
            )
            active_cutoff = (
                datetime.now(timezone.utc)
                - timedelta(minutes=max(15, int(self.settings.schedule_minutes * 1.5)))
            ).isoformat()
            latest_by_source: dict[str, dict[str, Any]] = {}
            history_by_source: dict[str, list[dict[str, Any]]] = {}
            for member in members:
                history_by_source.setdefault(member["source"], []).append(member)
                if member["fetched_at"] >= active_cutoff:
                    latest_by_source.setdefault(member["source"], member)
            ranks = [
                (int(item["rank"]), int(item["item_count"]))
                for item in latest_by_source.values()
            ]
            rank_deltas: list[float] = []
            for source, latest in latest_by_source.items():
                previous = next(
                    (
                        item
                        for item in history_by_source[source]
                        if item["fetched_at"] < latest["fetched_at"]
                    ),
                    None,
                )
                if previous:
                    latest_norm = 1 - (int(latest["rank"]) - 1) / max(
                        int(latest["item_count"]) - 1, 1
                    )
                    previous_norm = 1 - (int(previous["rank"]) - 1) / max(
                        int(previous["item_count"]) - 1, 1
                    )
                    rank_deltas.append(latest_norm - previous_norm)
            rank_velocity_delta = sum(rank_deltas) / len(rank_deltas) if rank_deltas else None
            scores = calculate_trend_scores(
                ranks=ranks,
                source_count=len(latest_by_source),
                first_seen_at=event["first_seen_at"],
                last_seen_at=event["last_seen_at"],
                rank_velocity_delta=rank_velocity_delta,
            )
            self.db.execute(
                """UPDATE trend_events SET source_count=?, member_count=?,
                trend_score=?, coverage_score=?, rank_score=?, velocity_score=?,
                persistence_score=?, freshness_score=?, updated_at=? WHERE id=?""",
                (
                    len(latest_by_source),
                    len(members),
                    scores.total,
                    scores.coverage,
                    scores.rank,
                    scores.velocity,
                    scores.persistence,
                    scores.freshness,
                    utc_now(),
                    event_id,
                ),
            )

    def _consolidate_unanalyzed_events(self, event_ids: list[int]) -> list[int]:
        """Merge obvious duplicate clusters while preserving any analyzed event as keeper."""
        if len(event_ids) < 2:
            return event_ids
        placeholders = ",".join("?" for _ in event_ids)
        events = self.db.all(
            f"""SELECT e.*,
            (SELECT COUNT(*) FROM analyses a WHERE a.event_id=e.id) analysis_count,
            (SELECT COUNT(*) FROM evidence v WHERE v.event_id=e.id) evidence_count
            FROM trend_events e WHERE e.id IN ({placeholders}) ORDER BY e.id""",
            tuple(event_ids),
        )
        removed: set[int] = set()
        for index, left in enumerate(events):
            if left["id"] in removed:
                continue
            for right in events[index + 1 :]:
                if right["id"] in removed:
                    continue
                if (
                    left["market"] != right["market"]
                    and left["market"] != "GLOBAL"
                    and right["market"] != "GLOBAL"
                ):
                    continue
                merged, _, _ = should_merge(left["canonical_title"], right["canonical_title"])
                if not merged:
                    continue
                if left["analysis_count"] or left["evidence_count"]:
                    keeper, duplicate = left, right
                elif right["analysis_count"] or right["evidence_count"]:
                    keeper, duplicate = right, left
                else:
                    keeper, duplicate = left, right
                if duplicate["analysis_count"] or duplicate["evidence_count"]:
                    continue
                self.db.execute(
                    """INSERT OR IGNORE INTO event_members
                    (event_id, source_item_id, match_method, match_score)
                    SELECT ?, source_item_id, 'cluster_consolidation', match_score
                    FROM event_members WHERE event_id=?""",
                    (keeper["id"], duplicate["id"]),
                )
                self.db.execute(
                    """UPDATE trend_events SET
                    market=CASE WHEN market=? THEN market ELSE 'GLOBAL' END,
                    language=CASE WHEN language=? THEN language ELSE 'multi' END,
                    signal_type=CASE WHEN signal_type=? THEN signal_type ELSE 'mixed' END,
                    updated_at=? WHERE id=?""",
                    (
                        duplicate["market"], duplicate["language"],
                        duplicate["signal_type"], utc_now(), keeper["id"],
                    ),
                )
                self.db.execute("DELETE FROM event_members WHERE event_id=?", (duplicate["id"],))
                self.db.execute("DELETE FROM trend_events WHERE id=?", (duplicate["id"],))
                removed.add(int(duplicate["id"]))
                if duplicate is left:
                    left = keeper
        return [event_id for event_id in event_ids if event_id not in removed]

    def _select_events(self, event_ids: list[int]) -> list[int]:
        if not event_ids:
            return []
        placeholders = ",".join("?" for _ in event_ids)
        domestic = self.db.all(
            f"""SELECT id FROM trend_events
            WHERE id IN ({placeholders}) AND market='CN'
            ORDER BY trend_score DESC LIMIT ?""",
            tuple(event_ids) + (self.settings.analysis_top_n,),
        )
        overseas = self.db.all(
            f"""SELECT id FROM trend_events
            WHERE id IN ({placeholders}) AND market!='CN'
            ORDER BY trend_score DESC LIMIT ?""",
            tuple(event_ids) + (self.settings.overseas_analysis_top_n,),
        )
        return [int(row["id"]) for row in [*domestic, *overseas]]

    async def _research_and_analyze(self, run_id: str, event_id: int) -> None:
        event = self.db.one("SELECT * FROM trend_events WHERE id = ?", (event_id,))
        recent_cutoff = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
        recent_analysis = self.db.one(
            """SELECT id FROM analyses WHERE event_id=? AND prompt_version=?
            AND created_at>=? ORDER BY id DESC LIMIT 1""",
            (event_id, self.analysis_version, recent_cutoff),
        )
        if recent_analysis:
            return
        members = self.db.all(
            """SELECT i.* FROM source_items i
            JOIN event_members m ON m.source_item_id=i.id
            WHERE m.event_id=? ORDER BY i.rank LIMIT 5""",
            (event_id,),
        )
        unique_urls: list[tuple[str, str]] = []
        seen_urls: set[str] = set()
        for member in members:
            if member["url"] not in seen_urls:
                unique_urls.append((member["url"], member["title"]))
                seen_urls.add(member["url"])
                raw = json.loads(member["raw_json"])
                extra = raw.get("extra") if isinstance(raw.get("extra"), dict) else {}
                source_excerpt = " ".join(
                    str(value).strip()
                    for value in (member["title"], extra.get("hover"), extra.get("info"))
                    if value
                )[:1800]
                self.db.execute(
                    """INSERT INTO evidence
                    (event_id, kind, url, title, excerpt, fetched_at, http_status,
                     content_hash, is_consumer_voice, valid_for_analysis, error)
                    VALUES (?, 'hotlist', ?, ?, ?, ?, NULL, NULL, ?, 1, NULL)
                    ON CONFLICT(event_id, url) DO UPDATE SET
                      title=excluded.title,
                      excerpt=excluded.excerpt,
                      is_consumer_voice=MAX(evidence.is_consumer_voice, excluded.is_consumer_voice)""",
                    (
                        event_id,
                        member["url"],
                        member["title"],
                        source_excerpt,
                        member["fetched_at"],
                        int(
                            member["source"] in {"coolapk", "tieba"}
                            or member["source"].startswith("reddit-")
                        ),
                    ),
                )
        fetched = await asyncio.gather(
            *[fetch_evidence(url, title) for url, title in unique_urls[:3]]
        )
        for result in fetched:
            valid_article = (
                result.error is None
                and result.http_status == 200
                and len(result.excerpt.strip()) >= 20
            )
            self.db.execute(
                """INSERT INTO evidence
                (event_id, kind, url, title, excerpt, fetched_at, http_status,
                 content_hash, is_consumer_voice, valid_for_analysis, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(event_id, url) DO UPDATE SET
                  kind=CASE WHEN excluded.valid_for_analysis=1 THEN 'article' ELSE evidence.kind END,
                  title=CASE WHEN excluded.valid_for_analysis=1 THEN excluded.title ELSE evidence.title END,
                  excerpt=CASE WHEN excluded.valid_for_analysis=1 THEN excluded.excerpt ELSE evidence.excerpt END,
                  fetched_at=excluded.fetched_at,
                  http_status=excluded.http_status,
                  content_hash=CASE WHEN excluded.valid_for_analysis=1 THEN excluded.content_hash ELSE evidence.content_hash END,
                  error=excluded.error""",
                (
                    event_id,
                    "article" if valid_article else "fetch_failed",
                    result.url,
                    result.title,
                    result.excerpt,
                    result.fetched_at,
                    result.http_status,
                    result.content_hash,
                    int(valid_article),
                    result.error if result.error else (None if valid_article else "content too short"),
                ),
            )
        evidence = self.db.all(
            "SELECT * FROM evidence WHERE event_id=? AND valid_for_analysis=1", (event_id,)
        )
        result = await self.analyzer.analyze(event, evidence)
        created_at = utc_now()
        self.db.execute(
            "UPDATE analyses SET status='superseded' WHERE event_id=? AND status!='superseded'",
            (event_id,),
        )
        self.db.execute(
            """UPDATE product_opportunities SET review_status='superseded', updated_at=?
            WHERE event_id=? AND review_status!='superseded'""",
            (created_at, event_id),
        )
        analysis_id = self.db.execute(
            """INSERT INTO analyses
            (event_id, run_id, engine, model, prompt_version, output_json, status, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                run_id,
                result.engine,
                result.model,
                self.analysis_version,
                result.output.model_dump_json(),
                "degraded" if result.degraded_reason else "succeeded",
                result.degraded_reason,
                created_at,
            ),
        )
        article_domains = {
            urlparse(item["url"]).hostname
            for item in evidence
            if item["kind"] == "article" and urlparse(item["url"]).hostname
        }
        hotlist_domains = {
            urlparse(item["url"]).hostname
            for item in evidence
            if item["kind"] == "hotlist" and urlparse(item["url"]).hostname
        }
        article_count = sum(1 for item in evidence if item["kind"] == "article")
        hotlist_count = sum(1 for item in evidence if item["kind"] == "hotlist")
        for draft in result.output.opportunities:
            score_fields = {
                key: getattr(draft, key)
                for key in (
                    "pain_score",
                    "intent_score",
                    "segment_score",
                    "timing_score",
                    "feasibility_score",
                    "differentiation_score",
                )
            }
            opportunity_score = calculate_opportunity_score(score_fields)
            evidence_quality = (article_count + hotlist_count * 0.5) / max(len(evidence), 1)
            cited_ratio = min(
                len(set(draft.evidence_ids)) / max(len(evidence), 1), 1
            ) * evidence_quality
            confidence = calculate_evidence_confidence(
                evidence_count=article_count + hotlist_count * 0.5,
                independent_domains=len(article_domains) + 0.5 * len(hotlist_domains - article_domains),
                consumer_voice_count=sum(int(item["is_consumer_voice"]) for item in evidence),
                cited_claim_ratio=cited_ratio,
            )
            self.db.execute(
                """INSERT INTO product_opportunities
                (analysis_id, event_id, name, target_segment, scenario, jtbd,
                 pain_points_json, solution, mvp, price_band, marketplace,
                 channels_json, risks_json,
                 pain_score, intent_score, segment_score, timing_score, feasibility_score,
                 differentiation_score, opportunity_score, evidence_confidence,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    analysis_id,
                    event_id,
                    draft.name,
                    draft.target_segment,
                    draft.scenario,
                    draft.jtbd,
                    self.db.json(draft.pain_points),
                    draft.solution,
                    draft.mvp,
                    draft.price_band,
                    draft.marketplace,
                    self.db.json(draft.channels),
                    self.db.json(draft.risks),
                    draft.pain_score,
                    draft.intent_score,
                    draft.segment_score,
                    draft.timing_score,
                    draft.feasibility_score,
                    draft.differentiation_score,
                    opportunity_score,
                    confidence,
                    created_at,
                    created_at,
                ),
            )
