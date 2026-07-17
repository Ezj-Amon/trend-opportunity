from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from .analysis import Analyzer
from .amazon import marketplace_code, marketplace_name
from .clustering import normalize_title, should_merge
from .config import Settings
from .db import Database
from .deduplication import collapse_unworked_duplicate_opportunities
from .evidence import fetch_evidence
from .market_validation import UnavailableMarketValidator
from .risk import assess_product_risk
from .semantic import (
    EmbeddingUnavailable,
    SemanticFeatureExtractor,
    SentenceTransformerEmbedder,
    semantic_input,
    semantic_input_hash,
)
from .semantic_duplicates import create_duplicate_candidates
from .scoring import (
    calculate_evidence_confidence,
    calculate_final_score,
    calculate_opportunity_score,
    calculate_trend_scores,
)
from .sources import (
    GoogleTrendsSource,
    NewsNowSource,
    RedditOAuthSource,
    SourceResult,
)


ANALYSIS_PROMPT_VERSION = "opportunity-prompt-v7-market-validation"


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
        self.market_validator = UnavailableMarketValidator()
        self.semantic_extractor = (
            SemanticFeatureExtractor(
                SentenceTransformerEmbedder(
                    settings.embedding_model_id,
                    settings.embedding_model_revision,
                    settings.embedding_cache_dir,
                    settings.embedding_local_files_only,
                )
            )
            if settings.enable_embeddings
            else None
        )
        self._lock = asyncio.Lock()
        self._progress: dict[str, Any] | None = None

    @property
    def is_running(self) -> bool:
        return self._lock.locked()

    @property
    def progress(self) -> dict[str, Any] | None:
        """Return a copy of the current/latest run progress for the UI."""
        return dict(self._progress) if self._progress else None

    def _set_progress(self, **updates: Any) -> None:
        if self._progress is None:
            self._progress = {}
        self._progress.update(updates)
        self._progress["updated_at"] = utc_now()

    @property
    def analysis_version(self) -> str:
        engine = (
            f"llm-{self.settings.openai_model}"
            if self.settings.openai_api_key
            else "local-rules-v2"
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
        source_total = (
            len(self.settings.source_ids)
            + len(self.settings.google_trends_geos)
            + int(self.reddit_source is not None)
        )
        self._progress = {
            "run_id": run_id,
            "status": "running",
            "stage": "ingest",
            "progress_percent": 5,
            "started_at": utc_now(),
            "updated_at": utc_now(),
            "sources_total": source_total,
            "sources_completed": 0,
            "sources_succeeded": 0,
            "sources_failed": 0,
            "items_count": 0,
            "events_count": 0,
            "selected_count": 0,
            "analyzed_count": 0,
            "source_results": [],
        }
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
            results: list[SourceResult] = []
            item_ids: list[int] = []
            for completed, future in enumerate(asyncio.as_completed(fetches), 1):
                result = await future
                results.append(result)
                item_ids.extend(self._persist_source_results(run_id, [result]))
                source_results = [
                    *self._progress.get("source_results", []),
                    {
                        "source": result.source,
                        "market": result.market,
                        "success": result.success,
                        "items_count": len(result.items),
                        "latency_ms": result.latency_ms,
                        "error": result.error,
                    },
                ]
                succeeded = sum(1 for item in results if item.success)
                self._set_progress(
                    sources_completed=completed,
                    sources_succeeded=succeeded,
                    sources_failed=completed - succeeded,
                    items_count=len(item_ids),
                    source_results=source_results,
                    progress_percent=5 + round(35 * completed / max(source_total, 1)),
                )
                self._update_run(run_id, items_count=len(item_ids))
            self._ensure_lease(lease_lost)
            if not item_ids:
                errors = "; ".join(
                    f"{result.source}: {result.error}" for result in results if result.error
                )
                raise RuntimeError(f"all real data sources failed: {errors[:800]}")
            self._set_progress(stage="cluster", progress_percent=45)
            self._update_run(run_id, stage="cluster", items_count=len(item_ids))
            event_ids = self._cluster_items(item_ids)
            event_ids = self._consolidate_unanalyzed_events(event_ids)
            self._score_events(event_ids)
            self._set_progress(
                stage="analyze",
                progress_percent=55,
                events_count=len(event_ids),
            )
            self._update_run(run_id, stage="analyze", events_count=len(event_ids))
            selected_ids = self._select_events(event_ids)
            self._set_progress(selected_count=len(selected_ids))
            for analyzed_count, event_id in enumerate(selected_ids, 1):
                await self._research_and_analyze(run_id, event_id)
                self._ensure_lease(lease_lost)
                self._set_progress(
                    analyzed_count=analyzed_count,
                    progress_percent=55
                    + round(40 * analyzed_count / max(len(selected_ids), 1)),
                )
            self._update_run(
                run_id,
                status="completed",
                stage="completed",
                selected_count=len(selected_ids),
                finished_at=utc_now(),
            )
            self._set_progress(
                status="completed",
                stage="completed",
                progress_percent=100,
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
            self._set_progress(
                status="failed",
                stage="failed",
                progress_percent=100,
                error_summary=f"{type(exc).__name__}: {str(exc)[:1000]}",
                finished_at=utc_now(),
            )
            raise
        return run_id

    def _invalidate_old_analysis_versions(self) -> None:
        self.db.execute(
            """UPDATE opportunity_signals SET review_status='superseded', updated_at=?
            WHERE review_status!='superseded' AND analysis_id IN
            (SELECT id FROM analyses WHERE prompt_version!=?)""",
            (utc_now(), self.analysis_version),
        )
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
        await self._persist_semantic_features(event, evidence)
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
        self.db.execute(
            """UPDATE opportunity_signals SET review_status='superseded', updated_at=?
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
        for signal in result.output.signals:
            self.db.execute(
                """INSERT INTO opportunity_signals
                (event_id,analysis_id,change_type,consumer_relevance_score,
                 product_opportunity_score,target_users_json,new_scenarios_json,
                 unmet_needs_json,related_product_categories_json,durability,
                 lead_time_fit,evidence_ids_json,confidence,missing_evidence_json,
                 review_status,engine,model,version,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?,?)""",
                (
                    event_id,
                    analysis_id,
                    signal.change_type,
                    signal.consumer_relevance_score,
                    signal.product_opportunity_score,
                    self.db.json(signal.target_users),
                    self.db.json(signal.new_scenarios),
                    self.db.json(signal.unmet_needs),
                    self.db.json(signal.related_product_categories),
                    signal.durability,
                    signal.lead_time_fit,
                    self.db.json(signal.evidence_ids),
                    signal.confidence,
                    self.db.json(signal.missing_evidence),
                    result.engine,
                    result.model,
                    self.analysis_version,
                    created_at,
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
            hypothesis_score = calculate_opportunity_score(score_fields)
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
            risk_level, risk_flags = assess_product_risk(event, draft)
            signal_market = str(event.get("market") or "").upper()
            target_marketplace = marketplace_code(
                draft.target_marketplace or draft.marketplace,
                default=(
                    signal_market
                    if signal_market in {"US", "GB", "DE", "JP", "CA", "FR", "IT", "ES"}
                    else self.settings.amazon_default_marketplace
                ),
            )
            marketplace = marketplace_name(target_marketplace)
            # A query must come from an explicit, reviewed product hypothesis;
            # news/rule keywords are not buyer-facing Amazon search terms.
            amazon_search_term = ""
            validation = await self.market_validator.validate(
                {
                    "name": draft.name,
                    "marketplace": marketplace,
                    "target_marketplace": target_marketplace,
                    "product_keywords": draft.product_keywords,
                },
                event,
            )
            final_score, uncertainty_penalty = calculate_final_score(
                trend_score=float(event["trend_score"]),
                hypothesis_score=hypothesis_score,
                market_score=validation.score,
                validation_status=validation.status,
                risk_level=risk_level,
            )
            opportunity_id = self.db.execute(
                """INSERT INTO product_opportunities
                (analysis_id, event_id, name, product_keywords_json, category,
                 target_segment, scenario, jtbd, purchase_motivation,
                 pain_points_json, solution, mvp, price_band, marketplace, target_marketplace,
                 amazon_search_term,
                 channels_json, risks_json, next_action, risk_level, risk_flags_json,
                 pain_score, intent_score, segment_score, timing_score, feasibility_score,
                  differentiation_score, hypothesis_score, market_score, final_score,
                  validated_recommendation_score,
                 validation_status, uncertainty_penalty, opportunity_score,
                 evidence_confidence, score_formula_version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?,
                        ?, ?,
                        ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    analysis_id,
                    event_id,
                    draft.name,
                    self.db.json(draft.product_keywords),
                    draft.category,
                    draft.target_segment,
                    draft.scenario,
                    draft.jtbd,
                    draft.purchase_motivation,
                    self.db.json(draft.pain_points),
                    draft.solution,
                    draft.mvp,
                    draft.price_band,
                    marketplace,
                    target_marketplace,
                    amazon_search_term,
                    self.db.json(draft.channels),
                    self.db.json(draft.risks),
                    draft.next_action,
                    risk_level,
                    self.db.json(risk_flags),
                    draft.pain_score,
                    draft.intent_score,
                    draft.segment_score,
                    draft.timing_score,
                    draft.feasibility_score,
                    draft.differentiation_score,
                    hypothesis_score,
                    validation.score,
                    final_score if final_score is not None else 0.0,
                    final_score,
                    validation.status,
                    uncertainty_penalty,
                    hypothesis_score,
                    confidence,
                    "opportunity-v2",
                    created_at,
                    created_at,
                ),
            )
            self.db.execute(
                """INSERT INTO market_validations
                (opportunity_id,provider,provider_version,status,query_json,
                 scores_json,metrics_json,sources_json,missing_fields_json,
                 market_score,raw_response_hash,note,error,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    opportunity_id,
                    validation.provider,
                    validation.provider_version,
                    validation.status,
                    self.db.json(validation.query),
                    self.db.json(validation.scores),
                    self.db.json(validation.metrics),
                    self.db.json(validation.sources),
                    self.db.json(validation.missing_fields),
                    validation.score,
                    validation.raw_response_hash,
                    validation.note,
                    validation.error,
                    created_at,
                ),
            )
        collapse_unworked_duplicate_opportunities(self.db)

    async def _persist_semantic_features(
        self, event: dict[str, Any], evidence: list[dict[str, Any]]
    ) -> None:
        text = semantic_input(
            event["canonical_title"],
            [str(item.get("excerpt") or "") for item in evidence],
        )
        input_hash = semantic_input_hash(text)
        identity = (
            event["id"],
            self.settings.embedding_model_id,
            self.settings.embedding_model_revision,
            input_hash,
            self.settings.semantic_feature_version,
        )
        existing = self.db.one(
            """SELECT id FROM semantic_event_features
            WHERE event_id=? AND model_id=? AND model_version=?
              AND input_hash=? AND feature_version=?""",
            identity,
        )
        if existing:
            current = self.db.one(
                "SELECT status FROM semantic_event_features WHERE id=?", (existing["id"],)
            )
            if current["status"] == "ready" or (
                self.semantic_extractor is None and current["status"] == "disabled"
            ):
                return
            self.db.execute("DELETE FROM semantic_event_features WHERE id=?", (existing["id"],))
        status = "disabled"
        result = None
        error = "embedding baseline is disabled"
        if self.semantic_extractor is not None:
            try:
                result = await asyncio.to_thread(self.semantic_extractor.extract, text)
                status = "ready"
                error = None
            except EmbeddingUnavailable as exc:
                status = "unavailable"
                error = str(exc)[:1000]
            except Exception as exc:
                status = "failed"
                error = f"{type(exc).__name__}: {str(exc)[:900]}"
        feature_id = self.db.execute(
            """INSERT INTO semantic_event_features
            (event_id,model_id,model_version,input_hash,feature_version,status,
             embedding_json,category_matches_json,positive_similarity,
             negative_similarity,opportunity_similarity,error,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                *identity,
                status,
                self.db.json(result.embedding) if result else None,
                self.db.json(result.category_matches) if result else "[]",
                result.positive_similarity if result else None,
                result.negative_similarity if result else None,
                result.opportunity_similarity if result else None,
                error,
                utc_now(),
            ),
        )
        if status == "ready":
            create_duplicate_candidates(
                self.db,
                feature_id,
                threshold=self.settings.semantic_duplicate_threshold,
                window=self.settings.semantic_duplicate_window,
            )
