from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .analysis import OpportunitySignalDraft
from .amazon import (
    AMAZON_MARKETPLACES,
    is_search_term_ready,
    is_supported_marketplace,
    marketplace_code,
    marketplace_name,
    normalize_search_term,
    pick_search_term,
)
from .amazon_validation import (
    build_raw_amazon_validation,
    build_template,
    parse_validation_csv,
)
from .config import Settings
from .db import Database
from .deduplication import (
    collapse_unworked_duplicate_opportunities,
    deduplicate_events,
    deduplicate_opportunities,
)
from .feishu import delivery_timestamp, send_daily_digest, send_opportunity
from .market_evidence import ManualMarketplaceDataProvider, SellerCentralCsvProvider
from .market_validation import (
    MarketScores,
    MarketValidationInput,
    MarketValidationResult,
    result_from_input,
)
from .opportunity_signals import (
    SIGNAL_FEEDBACK_TYPES,
    decode_signal,
)
from .pipeline import Pipeline, utc_now
from .product_hypotheses import (
    HYPOTHESIS_STATUSES,
    HumanProductHypothesisGenerator,
    ProductHypothesisInput,
    decode_hypothesis,
    normalized_query_terms,
    validate_physical_hypothesis,
)
from .reports import (
    build_daily_digest,
    decode_opportunity_data,
    is_validated_recommendation,
    top_trend_signals,
)
from .scoring import calculate_final_score, calculate_market_score
from .semantic import opportunity_precision_at_k
from .semantic_duplicates import (
    DUPLICATE_FEEDBACK_TYPES,
    duplicate_candidate_snapshot,
)


BASE_DIR = Path(__file__).resolve().parent
settings = Settings.from_env()
db = Database(settings.database_path)
pipeline = Pipeline(db, settings)
templates = Jinja2Templates(directory=BASE_DIR / "templates")
background_tasks: set[asyncio.Task] = set()
PRODUCT_HYPOTHESIS_WORKBENCH_ENABLED = False

RUN_STAGE_LABELS = {
    "ingest": "连接数据源并采集",
    "cluster": "整理并合并相似趋势",
    "analyze": "识别趋势与新品机会线索",
    "completed": "采集完成",
    "failed": "采集未完成",
}


class ReviewInput(BaseModel):
    status: str
    note: str = Field(default="", max_length=2000)


class OutcomeInput(BaseModel):
    result: str
    metrics: dict[str, Any] = Field(default_factory=dict)
    note: str = Field(default="", max_length=2000)


class TargetMarketplaceInput(BaseModel):
    target_marketplace: str = Field(min_length=2, max_length=12)


class AmazonSearchTermInput(BaseModel):
    search_term: str = Field(min_length=1, max_length=120)


class AmazonRawImportInput(BaseModel):
    product_opportunity_csv: str = Field(min_length=1, max_length=15_000_000)
    hot_search_terms_csv: str = Field(min_length=1, max_length=15_000_000)


class SignalFeedbackInput(BaseModel):
    feedback_type: str = Field(min_length=1, max_length=64)
    note: str = Field(default="", max_length=2000)


class SemanticEvaluationInput(BaseModel):
    label: str = Field(min_length=1, max_length=64)
    expected_category: str = Field(default="", max_length=120)
    note: str = Field(default="", max_length=2000)


class DuplicateFeedbackInput(BaseModel):
    feedback_type: str = Field(min_length=1, max_length=64)
    note: str = Field(default="", max_length=2000)


class HypothesisReviewInput(BaseModel):
    status: str = Field(min_length=1, max_length=64)
    note: str = Field(default="", max_length=2000)


def opportunity_signal_rows(review_status: str | None = None, limit: int = 200) -> list[dict]:
    params: list[Any] = []
    status_clause = ""
    if review_status:
        status_clause = "AND s.review_status=?"
        params.append(review_status)
    params.append(limit)
    return [
        decode_signal(row)
        for row in db.all(
            f"""SELECT s.*, e.canonical_title event_title, e.market,
            e.signal_type, e.trend_score, e.last_seen_at
            FROM opportunity_signals s
            JOIN trend_events e ON e.id=s.event_id
            WHERE s.review_status!='superseded' {status_clause}
            ORDER BY s.product_opportunity_score DESC, s.confidence DESC,
                     e.trend_score DESC, s.id DESC LIMIT ?""",
            tuple(params),
        )
    ]


def semantic_duplicate_rows(
    review_status: str | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    params: list[Any] = []
    status_clause = ""
    if review_status:
        status_clause = "AND c.review_status=?"
        params.append(review_status)
    params.append(limit)
    return db.all(
        f"""SELECT c.*, a.canonical_title event_a_title,
        b.canonical_title event_b_title, a.trend_score event_a_trend_score,
        b.trend_score event_b_trend_score, a.last_seen_at event_a_last_seen_at,
        b.last_seen_at event_b_last_seen_at
        FROM semantic_duplicate_candidates c
        JOIN trend_events a ON a.id=c.event_a_id
        JOIN trend_events b ON b.id=c.event_b_id
        WHERE 1=1 {status_clause}
        ORDER BY CASE WHEN c.review_status='pending' THEN 0 ELSE 1 END,
                 c.semantic_similarity DESC, c.id DESC LIMIT ?""",
        tuple(params),
    )


def product_hypothesis_rows(
    status: str | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    params: list[Any] = []
    status_clause = ""
    if status:
        status_clause = "AND h.status=?"
        params.append(status)
    params.append(limit)
    rows = db.all(
        f"""SELECT h.*, s.event_id, s.change_type, s.review_status signal_review_status,
        e.canonical_title event_title, e.market signal_market, e.trend_score
        FROM product_hypotheses h
        JOIN opportunity_signals s ON s.id=h.opportunity_signal_id
        JOIN trend_events e ON e.id=s.event_id
        WHERE s.review_status!='superseded' {status_clause}
        ORDER BY CASE h.status WHEN 'ready_for_validation' THEN 0 WHEN 'draft' THEN 1
                 WHEN 'validated' THEN 2 ELSE 3 END, h.id DESC LIMIT ?""",
        tuple(params),
    )
    return [decode_hypothesis(row) for row in rows]


def decode_market_evidence(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    value = dict(row)
    defaults = {
        "query_json": {},
        "scores_json": {},
        "metrics_json": {},
        "sources_json": [],
        "missing_fields_json": [],
    }
    for key, default in defaults.items():
        try:
            fallback = "[]" if isinstance(default, list) else "{}"
            value[key.removesuffix("_json")] = json.loads(value.get(key) or fallback)
        except (TypeError, json.JSONDecodeError):
            value[key.removesuffix("_json")] = default
    return value


def validated_recommendation_rows(limit: int = 200) -> list[dict[str, Any]]:
    rows = db.all(
        """SELECT r.*, h.name hypothesis_name, h.physical_form,
        h.target_marketplace, s.id opportunity_signal_id, s.change_type,
        e.id event_id, e.canonical_title event_title, e.market signal_market,
        me.provider, me.provider_version, me.market_score, me.collected_at
        FROM validated_recommendations r
        JOIN product_hypotheses h ON h.id=r.product_hypothesis_id
        JOIN opportunity_signals s ON s.id=h.opportunity_signal_id
        JOIN trend_events e ON e.id=s.event_id
        JOIN market_evidence me ON me.id=r.market_evidence_id
        WHERE r.status='active'
        ORDER BY r.recommendation_score DESC, r.id DESC LIMIT ?""",
        (limit,),
    )
    return rows


def persist_hypothesis_market_evidence(
    hypothesis: dict[str, Any], result: MarketValidationResult
) -> dict[str, Any]:
    if hypothesis["status"] not in {"ready_for_validation", "validated"}:
        raise HTTPException(409, "hypothesis is not ready for market validation")
    marketplace = str(result.query.get("marketplace") or hypothesis["target_marketplace"]).upper()
    if marketplace != str(hypothesis["target_marketplace"]).upper():
        raise HTTPException(409, "market evidence marketplace does not match hypothesis")
    if result.raw_response_hash:
        duplicate = db.one(
            """SELECT * FROM market_evidence
            WHERE product_hypothesis_id=? AND raw_response_hash=?
            ORDER BY id DESC LIMIT 1""",
            (hypothesis["id"], result.raw_response_hash),
        )
        if duplicate:
            recommendation = db.one(
                """SELECT * FROM validated_recommendations
                WHERE product_hypothesis_id=? AND status='active'
                ORDER BY id DESC LIMIT 1""",
                (hypothesis["id"],),
            )
            return {
                "market_evidence": decode_market_evidence(duplicate),
                "recommendation": recommendation,
                "duplicate": True,
            }
    now = utc_now()
    market_evidence_id = db.execute(
        """INSERT INTO market_evidence
        (product_hypothesis_id,provider,provider_version,status,marketplace,
         query_json,scores_json,metrics_json,sources_json,missing_fields_json,
         market_score,raw_response_hash,note,error,collected_at,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            hypothesis["id"], result.provider, result.provider_version, result.status,
            marketplace, db.json(result.query), db.json(result.scores),
            db.json(result.metrics), db.json(result.sources),
            db.json(result.missing_fields), result.score, result.raw_response_hash,
            result.note, result.error, now, now,
        ),
    )
    base_rows = [
        decode_market_evidence(item)
        for item in db.all(
            """SELECT * FROM market_evidence
            WHERE product_hypothesis_id=? AND provider!='evidence-composite'
            ORDER BY id""",
            (hypothesis["id"],),
        )
    ]
    combined_scores = {key: None for key in result.scores}
    combined_sources: list[str] = []
    for item in base_rows:
        for key, score in item["scores"].items():
            if score is not None:
                combined_scores[key] = score
        for source in item["sources"]:
            if source not in combined_sources:
                combined_sources.append(source)
    combined_missing = [key for key, score in combined_scores.items() if score is None]
    if len(base_rows) > 1 and combined_scores != result.scores:
        component_ids = [int(item["id"]) for item in base_rows]
        composite_payload = {
            "marketplace": marketplace,
            "component_market_evidence_ids": component_ids,
            "scores": combined_scores,
        }
        composite_hash = hashlib.sha256(
            json.dumps(
                composite_payload, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        composite_status = "completed" if not combined_missing else "partial"
        composite_score = calculate_market_score(combined_scores)
        market_evidence_id = db.execute(
            """INSERT INTO market_evidence
            (product_hypothesis_id,provider,provider_version,status,marketplace,
             query_json,scores_json,metrics_json,sources_json,missing_fields_json,
             market_score,raw_response_hash,note,collected_at,created_at)
            VALUES (?,'evidence-composite','composite-v1',?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                hypothesis["id"], composite_status, marketplace,
                db.json({"marketplace": marketplace, "component_ids": component_ids}),
                db.json(combined_scores),
                db.json({"component_market_evidence_ids": component_ids}),
                db.json(combined_sources), db.json(combined_missing), composite_score,
                composite_hash, "由多份独立 MarketEvidence 按字段最新非空值组合", now, now,
            ),
        )
        result = MarketValidationResult(
            provider="evidence-composite",
            provider_version="composite-v1",
            status=composite_status,
            query={"marketplace": marketplace, "component_ids": component_ids},
            scores=combined_scores,
            metrics={"component_market_evidence_ids": component_ids},
            sources=combined_sources,
            missing_fields=combined_missing,
            score=composite_score,
            raw_response_hash=composite_hash,
            note="由多份独立 MarketEvidence 按字段最新非空值组合",
        )
    recommendation = None
    economics_ok = (result.scores.get("unit_economics_score") or 0) >= 3
    evidence_ok = (result.scores.get("evidence_score") or 0) >= 3
    risk_ok = hypothesis["risk_level"] in {"low", "medium"}
    if result.status == "completed" and not (economics_ok and evidence_ok and risk_ok):
        db.execute(
            """UPDATE validated_recommendations SET status='superseded',updated_at=?
            WHERE product_hypothesis_id=? AND status='active'""",
            (now, hypothesis["id"]),
        )
        db.execute(
            """UPDATE product_hypotheses SET status='ready_for_validation',updated_at=?
            WHERE id=?""",
            (now, hypothesis["id"]),
        )
    if result.status == "completed" and result.score is not None and economics_ok and evidence_ok and risk_ok:
        signal = decode_signal(
            db.one(
                "SELECT * FROM opportunity_signals WHERE id=?",
                (hypothesis["opportunity_signal_id"],),
            )
        )
        event = db.one("SELECT * FROM trend_events WHERE id=?", (signal["event_id"],))
        recommendation_score, _ = calculate_final_score(
            trend_score=float(event["trend_score"]), hypothesis_score=0,
            market_score=result.score, validation_status=result.status,
            risk_level=hypothesis["risk_level"],
        )
        if recommendation_score is not None:
            evidence_ids = hypothesis["evidence_ids"]
            cited_evidence = []
            if evidence_ids:
                placeholders = ",".join("?" for _ in evidence_ids)
                cited_evidence = db.all(
                    f"SELECT * FROM evidence WHERE id IN ({placeholders}) ORDER BY id",
                    tuple(evidence_ids),
                )
            market_row = decode_market_evidence(
                db.one("SELECT * FROM market_evidence WHERE id=?", (market_evidence_id,))
            )
            snapshot = {
                "event": event,
                "opportunity_signal": signal,
                "product_hypothesis": hypothesis,
                "trend_evidence": cited_evidence,
                "market_evidence": market_row,
                "gates": {
                    "market_complete": True,
                    "unit_economics_score_at_least_3": economics_ok,
                    "evidence_score_at_least_3": evidence_ok,
                    "risk_low_or_medium": risk_ok,
                },
            }
            db.execute(
                """UPDATE validated_recommendations SET status='superseded',updated_at=?
                WHERE product_hypothesis_id=? AND status='active'""",
                (now, hypothesis["id"]),
            )
            recommendation_id = db.execute(
                """INSERT INTO validated_recommendations
                (product_hypothesis_id,market_evidence_id,recommendation_score,
                 risk_level,status,snapshot_json,created_at,updated_at)
                VALUES (?,?,?,?,'active',?,?,?)""",
                (
                    hypothesis["id"], market_evidence_id, recommendation_score,
                    hypothesis["risk_level"], db.json(snapshot), now, now,
                ),
            )
            db.execute(
                "UPDATE product_hypotheses SET status='validated',updated_at=? WHERE id=?",
                (now, hypothesis["id"]),
            )
            recommendation = db.one(
                "SELECT * FROM validated_recommendations WHERE id=?",
                (recommendation_id,),
            )
    return {
        "market_evidence": decode_market_evidence(
            db.one("SELECT * FROM market_evidence WHERE id=?", (market_evidence_id,))
        ),
        "recommendation": recommendation,
        "duplicate": False,
        "gates": {
            "complete": result.status == "completed",
            "economics_ok": economics_ok,
            "evidence_ok": evidence_ok,
            "risk_ok": risk_ok,
        },
    }


async def scheduler_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.schedule_minutes * 60)
        except TimeoutError:
            if not pipeline.is_running:
                with suppress(Exception):
                    await pipeline.run("scheduler")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.initialize()
    collapse_unworked_duplicate_opportunities(db)
    stop = asyncio.Event()
    task = None
    if settings.enable_scheduler:
        task = asyncio.create_task(scheduler_loop(stop))
    yield
    stop.set()
    for active in list(background_tasks):
        active.cancel()
    if background_tasks:
        await asyncio.gather(*background_tasks, return_exceptions=True)
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="全球趋势驱动新品机会系统", version="0.2.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.middleware("http")
async def protect_write_api(request: Request, call_next):
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path.startswith("/api/"):
        origin = request.headers.get("origin")
        allowed_origins = {
            settings.public_base_url,
            "http://127.0.0.1:8000",
            "http://localhost:8000",
        }
        if origin and origin.rstrip("/") not in allowed_origins:
            return JSONResponse({"detail": "origin is not allowed"}, status_code=403)
        if settings.admin_token:
            supplied = request.headers.get("x-admin-token", "")
            if not hmac.compare_digest(supplied, settings.admin_token):
                return JSONResponse({"detail": "invalid admin token"}, status_code=401)
        else:
            client_host = request.client.host if request.client else ""
            if client_host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
                return JSONResponse(
                    {"detail": "write API is local-only unless ADMIN_TOKEN is configured"},
                    status_code=403,
                )
    return await call_next(request)


def decode_opportunity(row: dict | None) -> dict | None:
    if not row:
        return None
    return decode_opportunity_data(row)


def normalize_market(value: str) -> str:
    market = value.strip().upper()
    if market != "ALL" and not re.fullmatch(r"[A-Z]{2,12}", market):
        raise HTTPException(400, "invalid market")
    return market


def normalize_target_marketplace(value: str) -> str:
    code = marketplace_code(value)
    if not is_supported_marketplace(code):
        raise HTTPException(400, f"unsupported Amazon marketplace: {code}")
    return code


def _seconds_between(start: str | None, end: str | None = None) -> int:
    if not start:
        return 0
    try:
        started = datetime.fromisoformat(start)
        finished = datetime.fromisoformat(end) if end else datetime.now(timezone.utc)
        return max(0, round((finished - started).total_seconds()))
    except (TypeError, ValueError):
        return 0


def _estimated_run_seconds() -> int:
    completed = db.all(
        """SELECT started_at, finished_at FROM pipeline_runs
        WHERE status='completed' AND finished_at IS NOT NULL
        ORDER BY started_at DESC LIMIT 5"""
    )
    durations = [
        _seconds_between(item["started_at"], item["finished_at"])
        for item in completed
    ]
    durations = [value for value in durations if value > 0]
    return round(sum(durations) / len(durations)) if durations else 120


def current_run_status() -> dict[str, Any]:
    live = pipeline.progress
    if live:
        result = live
    else:
        latest = db.one("SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT 1")
        if not latest:
            return {
                "status": "idle",
                "stage": "idle",
                "stage_label": "尚未运行",
                "progress_percent": 0,
                "elapsed_seconds": 0,
                "estimated_remaining_seconds": None,
                "source_results": [],
            }
        result = dict(latest)
        result["run_id"] = result.pop("id")
        result["progress_percent"] = {
            "ingest": 20,
            "cluster": 45,
            "analyze": 70,
            "completed": 100,
            "failed": 100,
        }.get(result["stage"], 0)
        result["source_results"] = [
            {
                "source": item["source"],
                "market": item["market"],
                "success": bool(item["success"]),
                "items_count": db.one(
                    "SELECT COUNT(*) count FROM source_items WHERE snapshot_id=?",
                    (item["id"],),
                )["count"],
                "latency_ms": item["latency_ms"],
                "error": item["error"],
            }
            for item in db.all(
                "SELECT * FROM source_snapshots WHERE run_id=? ORDER BY id",
                (result["run_id"],),
            )
        ]
        result["sources_completed"] = len(result["source_results"])
        result["sources_total"] = len(result["source_results"])
        result["sources_succeeded"] = sum(
            1 for item in result["source_results"] if item["success"]
        )
        result["sources_failed"] = (
            result["sources_completed"] - result["sources_succeeded"]
        )
        result["analyzed_count"] = result.get("selected_count", 0)

    result = dict(result)
    result["stage_label"] = RUN_STAGE_LABELS.get(
        result.get("stage", ""), "准备开始"
    )
    result["elapsed_seconds"] = _seconds_between(
        result.get("started_at"), result.get("finished_at")
    )
    if result.get("status") == "running":
        estimate = _estimated_run_seconds()
        fraction_left = max(0.05, 1 - result.get("progress_percent", 0) / 100)
        result["estimated_remaining_seconds"] = max(
            5, round(estimate * fraction_left)
        )
    else:
        result["estimated_remaining_seconds"] = 0
    return result


def pending_validation_rows(target_marketplace: str = "ALL", limit: int = 20) -> list[dict]:
    if limit < 1 or limit > 200:
        raise HTTPException(400, "limit must be between 1 and 200")
    # Phase 0 safety gate: there is no trustworthy ProductHypothesis object yet.
    # Keep the import/provider APIs intact, but do not build a work queue from
    # legacy rule-generated product rows.
    if not PRODUCT_HYPOTHESIS_WORKBENCH_ENABLED:
        return []
    params: list[Any] = []
    marketplace_clause = ""
    if target_marketplace != "ALL":
        marketplace_clause = "AND o.target_marketplace=?"
        params.append(target_marketplace)
    params.append(max(200, limit * 10))
    rows = db.all(
        f"""SELECT o.*, e.canonical_title event_title, e.market signal_market,
        e.signal_type, e.trend_score
        FROM product_opportunities o
        JOIN trend_events e ON e.id=o.event_id
        JOIN analyses a ON a.id=o.analysis_id
        WHERE o.validation_status IN ('unavailable','pending','partial')
          AND o.review_status NOT IN ('rejected','superseded')
          AND o.risk_level!='blocking'
          AND o.score_formula_version='opportunity-v2'
          AND a.status!='superseded'
          {marketplace_clause}
        ORDER BY o.hypothesis_score DESC, e.trend_score DESC, o.id DESC
        LIMIT ?""",
        tuple(params),
    )
    decoded = deduplicate_opportunities(
        [decode_opportunity(row) for row in rows], limit=limit
    )
    for item in decoded:
        term = pick_search_term(
            item.get("amazon_search_term"), (), item.get("target_marketplace") or "US"
        )
        item["amazon_search_term"] = term
        item["query_readiness"] = (
            "ready"
            if is_search_term_ready(term, item.get("target_marketplace") or "US")
            else "needs_keyword"
        )
    decoded.sort(key=lambda item: item["query_readiness"] != "ready")
    for index, item in enumerate(decoded, 1):
        item["queue_position"] = index
    return decoded


def persist_market_validation(opportunity: dict, value: MarketValidationInput) -> dict:
    result = result_from_input(value)
    final_score, uncertainty_penalty = calculate_final_score(
        trend_score=float(opportunity["trend_score"]),
        hypothesis_score=float(opportunity["hypothesis_score"]),
        market_score=result.score,
        validation_status=result.status,
        risk_level=opportunity["risk_level"],
    )
    now = utc_now()
    validation_id = db.execute(
        """INSERT INTO market_validations
        (opportunity_id,provider,provider_version,status,query_json,scores_json,
         metrics_json,sources_json,missing_fields_json,market_score,
         raw_response_hash,note,error,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            opportunity["id"], result.provider, result.provider_version, result.status,
            db.json(result.query), db.json(result.scores), db.json(result.metrics),
            db.json(result.sources), db.json(result.missing_fields), result.score,
            result.raw_response_hash, result.note, result.error, now,
        ),
    )
    db.execute(
        """UPDATE product_opportunities SET market_score=?, final_score=?,
        validated_recommendation_score=?,
        validation_status=?, uncertainty_penalty=?, opportunity_score=?,
        score_formula_version='opportunity-v2', updated_at=? WHERE id=?""",
        (
            result.score, final_score if final_score is not None else 0.0, final_score,
            result.status, uncertainty_penalty, opportunity["hypothesis_score"],
            now, opportunity["id"],
        ),
    )
    return {
        "validation_id": validation_id,
        "opportunity_id": opportunity["id"],
        "status": result.status,
        "market_score": result.score,
        "final_score": final_score,
        "missing_fields": result.missing_fields,
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, market: str = "ALL"):
    market = normalize_market(market)
    where = "" if market == "ALL" else "WHERE e.market=?"
    params = () if market == "ALL" else (market,)
    events = deduplicate_events(db.all(
        f"""SELECT e.*,
        (SELECT COUNT(*) FROM opportunity_signals s
         WHERE s.event_id=e.id AND s.review_status!='superseded') signal_count,
        (SELECT engine FROM analyses a WHERE a.event_id=e.id AND a.status!='superseded'
         ORDER BY a.id DESC LIMIT 1) latest_analysis_engine,
        (SELECT MAX(product_opportunity_score) FROM opportunity_signals s
         WHERE s.event_id=e.id AND s.review_status!='superseded') best_signal_score
        FROM trend_events e {where}
        ORDER BY e.trend_score DESC, e.last_seen_at DESC LIMIT 800""",
        params,
    ), limit=80)
    market_counts = {
        row["market"]: row["count"]
        for row in db.all(
            """SELECT market, COUNT(DISTINCT normalized_title) count
            FROM trend_events GROUP BY market"""
        )
    }
    market_options = (
        "ALL",
        *sorted({"CN", "GLOBAL", *settings.google_trends_geos, *market_counts}),
    )
    runs = db.all("SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT 10")
    source_health = db.all(
        """SELECT s.* FROM source_snapshots s
        JOIN (SELECT source, MAX(id) id FROM source_snapshots GROUP BY source) latest
        ON latest.id=s.id ORDER BY s.source"""
    )
    latest_analysis = db.one("SELECT engine, model, status FROM analyses ORDER BY id DESC LIMIT 1")
    digest = build_daily_digest(db)
    if market == "CN":
        digest["overseas_top3"] = []
    elif market != "ALL":
        digest["cn_top3"] = []
        digest["overseas_top3"] = top_trend_signals(db, market)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "events": events,
            "runs": runs,
            "source_health": source_health,
            "pipeline_running": pipeline.is_running,
            "settings": settings,
            "latest_analysis": latest_analysis,
            "selected_market": market,
            "market_counts": market_counts,
            "market_options": market_options,
            "digest": digest,
        },
    )


@app.get("/events/{event_id}", response_class=HTMLResponse)
async def event_detail(request: Request, event_id: int):
    event = db.one("SELECT * FROM trend_events WHERE id=?", (event_id,))
    if not event:
        raise HTTPException(404, "event not found")
    members = db.all(
        """SELECT i.*, m.match_method, m.match_score FROM source_items i
        JOIN event_members m ON m.source_item_id=i.id
        WHERE m.event_id=? ORDER BY i.fetched_at DESC, i.rank""",
        (event_id,),
    )
    evidence = db.all("SELECT * FROM evidence WHERE event_id=? ORDER BY id", (event_id,))
    analysis = db.one(
        "SELECT * FROM analyses WHERE event_id=? ORDER BY id DESC LIMIT 1", (event_id,)
    )
    opportunities = deduplicate_opportunities([
        decode_opportunity(row)
        for row in db.all(
            """SELECT * FROM product_opportunities
            WHERE event_id=? AND analysis_id=? ORDER BY opportunity_score DESC""",
            (event_id, analysis["id"] if analysis else -1),
        )
    ])
    for opportunity in opportunities:
        validation = db.one(
            "SELECT * FROM market_validations WHERE opportunity_id=? ORDER BY id DESC LIMIT 1",
            (opportunity["id"],),
        )
        if validation:
            for key in ("query_json", "scores_json", "metrics_json", "sources_json", "missing_fields_json"):
                validation[key.removesuffix("_json")] = json.loads(validation[key])
        opportunity["market_validation"] = validation
        opportunity["outcomes"] = db.all(
            "SELECT * FROM opportunity_outcomes WHERE opportunity_id=? ORDER BY horizon_days",
            (opportunity["id"],),
        )
    analysis_output = json.loads(analysis["output_json"]) if analysis else None
    semantic_feature = db.one(
        """SELECT * FROM semantic_event_features
        WHERE event_id=? ORDER BY id DESC LIMIT 1""",
        (event_id,),
    )
    if semantic_feature:
        semantic_feature["category_matches"] = json.loads(
            semantic_feature["category_matches_json"] or "[]"
        )
    signals = [
        decode_signal(row)
        for row in db.all(
            """SELECT * FROM opportunity_signals
            WHERE event_id=? AND review_status!='superseded'
            ORDER BY product_opportunity_score DESC, id DESC""",
            (event_id,),
        )
    ]
    return templates.TemplateResponse(
        request,
        "event.html",
        {
            "event": event,
            "members": members,
            "evidence": evidence,
            "opportunities": opportunities,
            "analysis": analysis,
            "analysis_output": analysis_output,
            "signals": signals,
            "product_hypotheses": [
                item
                for item in product_hypothesis_rows(limit=500)
                if int(item["event_id"]) == event_id
            ],
            "semantic_feature": semantic_feature,
            "feishu_configured": bool(settings.feishu_webhook_url),
            "marketplaces": AMAZON_MARKETPLACES,
        },
    )


@app.get("/signals", response_class=HTMLResponse)
async def opportunity_signals_page(request: Request):
    signals = opportunity_signal_rows()
    return templates.TemplateResponse(
        request,
        "signals.html",
        {
            "signals": signals,
            "feedback_types": SIGNAL_FEEDBACK_TYPES,
        },
    )


@app.get("/feedback", response_class=HTMLResponse)
async def feedback_queue_page(request: Request):
    pending = opportunity_signal_rows("pending")
    history = db.all(
        """SELECT f.*, s.change_type, e.canonical_title event_title
        FROM opportunity_signal_feedback f
        JOIN opportunity_signals s ON s.id=f.signal_id
        JOIN trend_events e ON e.id=s.event_id
        ORDER BY f.id DESC LIMIT 100"""
    )
    return templates.TemplateResponse(
        request,
        "feedback.html",
        {
            "signals": pending,
            "history": history,
            "feedback_types": SIGNAL_FEEDBACK_TYPES,
        },
    )


@app.get("/api/opportunity-signals")
async def api_opportunity_signals(review_status: str | None = None, limit: int = 200):
    if limit < 1 or limit > 500:
        raise HTTPException(400, "limit must be between 1 and 500")
    if review_status and review_status not in {"pending", *SIGNAL_FEEDBACK_TYPES}:
        raise HTTPException(400, "invalid review status")
    return opportunity_signal_rows(review_status, limit)


@app.post("/api/opportunity-signals/{signal_id}/feedback")
async def save_opportunity_signal_feedback(signal_id: int, value: SignalFeedbackInput):
    if value.feedback_type not in SIGNAL_FEEDBACK_TYPES:
        raise HTTPException(400, "invalid feedback type")
    signal = db.one("SELECT * FROM opportunity_signals WHERE id=?", (signal_id,))
    if not signal:
        raise HTTPException(404, "opportunity signal not found")
    if signal["review_status"] == "superseded":
        raise HTTPException(409, "opportunity signal has been superseded")
    event = db.one("SELECT * FROM trend_events WHERE id=?", (signal["event_id"],))
    evidence_ids = decode_signal(signal)["evidence_ids"]
    evidence = []
    if evidence_ids:
        placeholders = ",".join("?" for _ in evidence_ids)
        evidence = db.all(
            f"SELECT * FROM evidence WHERE id IN ({placeholders}) ORDER BY id",
            tuple(evidence_ids),
        )
    snapshot = {
        "signal": decode_signal(signal),
        "event": event,
        "evidence": evidence,
        "feedback_type": value.feedback_type,
        "note": value.note.strip(),
    }
    now = utc_now()
    feedback_id = db.execute(
        """INSERT INTO opportunity_signal_feedback
        (signal_id,feedback_type,note,snapshot_json,created_at)
        VALUES (?,?,?,?,?)""",
        (signal_id, value.feedback_type, value.note.strip(), db.json(snapshot), now),
    )
    db.execute(
        "UPDATE opportunity_signals SET review_status=?, updated_at=? WHERE id=?",
        (value.feedback_type, now, signal_id),
    )
    return {
        "status": value.feedback_type,
        "label": SIGNAL_FEEDBACK_TYPES[value.feedback_type],
        "feedback_id": feedback_id,
    }


@app.post("/api/events/{event_id}/opportunity-signals")
async def create_manual_opportunity_signal(
    event_id: int, value: OpportunitySignalDraft
):
    event = db.one("SELECT * FROM trend_events WHERE id=?", (event_id,))
    if not event:
        raise HTTPException(404, "event not found")
    valid_evidence = {
        int(item["id"])
        for item in db.all("SELECT id FROM evidence WHERE event_id=?", (event_id,))
    }
    if not set(value.evidence_ids).issubset(valid_evidence):
        raise HTTPException(400, "signal cited evidence outside this event")
    provenance = db.one(
        """SELECT run_id FROM analyses WHERE event_id=? ORDER BY id DESC LIMIT 1""",
        (event_id,),
    ) or db.one(
        """SELECT ss.run_id FROM event_members em
        JOIN source_items i ON i.id=em.source_item_id
        JOIN source_snapshots ss ON ss.id=i.snapshot_id
        WHERE em.event_id=? ORDER BY ss.id DESC LIMIT 1""",
        (event_id,),
    )
    if not provenance:
        raise HTTPException(409, "event has no pipeline provenance")
    now = utc_now()
    output = {
        "event_summary": event["canonical_title"],
        "inference_notice": "人工创建的 OpportunitySignal；仍需独立审核和后续市场验证。",
        "signals": [value.model_dump()],
        "opportunities": [],
    }
    analysis_id = db.execute(
        """INSERT INTO analyses
        (event_id,run_id,engine,model,prompt_version,output_json,status,created_at)
        VALUES (?,?,'human','human-workbench','human-signal-v1',?,'succeeded',?)""",
        (event_id, provenance["run_id"], db.json(output), now),
    )
    signal_id = db.execute(
        """INSERT INTO opportunity_signals
        (event_id,analysis_id,change_type,consumer_relevance_score,
         product_opportunity_score,target_users_json,new_scenarios_json,
         unmet_needs_json,related_product_categories_json,durability,lead_time_fit,
         evidence_ids_json,confidence,missing_evidence_json,review_status,
         engine,model,version,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending','human','human-workbench',
                'human-signal-v1',?,?)""",
        (
            event_id, analysis_id, value.change_type,
            value.consumer_relevance_score, value.product_opportunity_score,
            db.json(value.target_users), db.json(value.new_scenarios),
            db.json(value.unmet_needs), db.json(value.related_product_categories),
            value.durability, value.lead_time_fit, db.json(value.evidence_ids),
            value.confidence, db.json(value.missing_evidence), now, now,
        ),
    )
    return {"status": "created", "signal_id": signal_id, "review_status": "pending"}


@app.post("/api/events/{event_id}/semantic-label")
async def save_semantic_evaluation_label(event_id: int, value: SemanticEvaluationInput):
    allowed = {
        "positive",
        "no_physical_product",
        "weak_consumer_relevance",
        "too_short_term",
        "high_risk",
        "software_service",
        "insufficient_evidence",
    }
    if value.label not in allowed:
        raise HTTPException(400, "invalid semantic evaluation label")
    if not db.one("SELECT id FROM trend_events WHERE id=?", (event_id,)):
        raise HTTPException(404, "event not found")
    db.execute(
        """INSERT INTO semantic_evaluation_labels
        (event_id,label,expected_category,note,created_at) VALUES (?,?,?,?,?)
        ON CONFLICT(event_id) DO UPDATE SET label=excluded.label,
        expected_category=excluded.expected_category,note=excluded.note,
        created_at=excluded.created_at""",
        (
            event_id,
            value.label,
            value.expected_category.strip(),
            value.note.strip(),
            utc_now(),
        ),
    )
    return {"status": "saved", "event_id": event_id, "label": value.label}


@app.get("/semantic-review", response_class=HTMLResponse)
async def semantic_review_page(request: Request):
    candidates = semantic_duplicate_rows(limit=200)
    samples = db.all(
        """SELECT e.*, l.label, l.expected_category, l.note label_note,
        f.status semantic_status, f.opportunity_similarity, f.category_matches_json
        FROM trend_events e
        LEFT JOIN semantic_evaluation_labels l ON l.event_id=e.id
        LEFT JOIN semantic_event_features f ON f.id=(
          SELECT sf.id FROM semantic_event_features sf
          WHERE sf.event_id=e.id ORDER BY sf.id DESC LIMIT 1
        )
        ORDER BY CASE WHEN l.id IS NULL THEN 0 ELSE 1 END,
                 e.trend_score DESC, e.id DESC LIMIT 100"""
    )
    for sample in samples:
        try:
            sample["category_matches"] = json.loads(
                sample.get("category_matches_json") or "[]"
            )
        except (TypeError, json.JSONDecodeError):
            sample["category_matches"] = []
    return templates.TemplateResponse(
        request,
        "semantic_review.html",
        {
            "candidates": candidates,
            "samples": samples,
            "duplicate_feedback_types": DUPLICATE_FEEDBACK_TYPES,
        },
    )


@app.get("/api/semantic/duplicate-candidates")
async def api_semantic_duplicate_candidates(
    review_status: str | None = None, limit: int = 200
):
    if limit < 1 or limit > 500:
        raise HTTPException(400, "limit must be between 1 and 500")
    if review_status and review_status not in {"pending", *DUPLICATE_FEEDBACK_TYPES}:
        raise HTTPException(400, "invalid review status")
    return semantic_duplicate_rows(review_status, limit)


@app.post("/api/semantic/duplicate-candidates/{candidate_id}/feedback")
async def save_semantic_duplicate_feedback(
    candidate_id: int, value: DuplicateFeedbackInput
):
    if value.feedback_type not in DUPLICATE_FEEDBACK_TYPES:
        raise HTTPException(400, "invalid feedback type")
    snapshot = duplicate_candidate_snapshot(db, candidate_id)
    if not snapshot:
        raise HTTPException(404, "semantic duplicate candidate not found")
    now = utc_now()
    feedback_snapshot = {
        "candidate": snapshot,
        "feedback_type": value.feedback_type,
        "note": value.note.strip(),
    }
    feedback_id = db.execute(
        """INSERT INTO semantic_duplicate_feedback
        (candidate_id,feedback_type,note,snapshot_json,created_at)
        VALUES (?,?,?,?,?)""",
        (
            candidate_id,
            value.feedback_type,
            value.note.strip(),
            db.json(feedback_snapshot),
            now,
        ),
    )
    db.execute(
        """UPDATE semantic_duplicate_candidates SET review_status=?,
        reviewer_note=?, reviewed_at=?, updated_at=? WHERE id=?""",
        (value.feedback_type, value.note.strip(), now, now, candidate_id),
    )
    return {
        "status": value.feedback_type,
        "label": DUPLICATE_FEEDBACK_TYPES[value.feedback_type],
        "feedback_id": feedback_id,
        "merged": False,
    }


@app.get("/api/semantic/evaluation")
async def semantic_evaluation(k: int = 10):
    if k < 1 or k > 200:
        raise HTTPException(400, "k must be between 1 and 200")
    labels = db.all("SELECT * FROM semantic_evaluation_labels ORDER BY event_id")
    latest = db.all(
        """SELECT f.*, e.normalized_title FROM semantic_event_features f
        JOIN (SELECT event_id, MAX(id) id FROM semantic_event_features GROUP BY event_id) x
          ON x.id=f.id
        JOIN trend_events e ON e.id=f.event_id
        ORDER BY CASE WHEN f.status='ready' THEN 0 ELSE 1 END,
                 f.opportunity_similarity DESC, f.id DESC"""
    )
    ready = [item for item in latest if item["status"] == "ready"]
    positive_ids = {
        int(item["event_id"]) for item in labels if item["label"] == "positive"
    }
    category_checks = []
    labels_by_event = {int(item["event_id"]): item for item in labels}
    for feature in ready:
        label = labels_by_event.get(int(feature["event_id"]))
        if (
            not label
            or label["label"] != "positive"
            or not label["expected_category"]
        ):
            continue
        matches = json.loads(feature["category_matches_json"] or "[]")
        category_checks.append(
            bool(matches and matches[0]["category"] == label["expected_category"])
        )
    negative_labels = {
        "no_physical_product", "weak_consumer_relevance", "too_short_term",
        "high_risk", "software_service",
    }
    trend_baseline = db.all(
        """SELECT e.id event_id FROM trend_events e
        JOIN semantic_evaluation_labels l ON l.event_id=e.id
        ORDER BY e.trend_score DESC, e.id DESC"""
    )
    reviewed_duplicates = db.all(
        """SELECT review_status FROM semantic_duplicate_candidates
        WHERE review_status!='pending'"""
    )
    model_versions = []
    identities = db.all(
        """SELECT DISTINCT model_id,model_version,feature_version
        FROM semantic_event_features WHERE status='ready'
        ORDER BY model_id,model_version,feature_version"""
    )
    for identity in identities:
        ranked = db.all(
            """SELECT f.event_id FROM semantic_event_features f
            JOIN semantic_evaluation_labels l ON l.event_id=f.event_id
            WHERE f.status='ready' AND f.model_id=? AND f.model_version=?
              AND f.feature_version=?
            ORDER BY f.opportunity_similarity DESC, f.id DESC""",
            (
                identity["model_id"], identity["model_version"],
                identity["feature_version"],
            ),
        )
        model_versions.append(
            {
                **identity,
                "labeled_ready_count": len(ranked),
                "opportunity_precision_at_k": opportunity_precision_at_k(
                    [int(item["event_id"]) for item in ranked], positive_ids, k
                ),
            }
        )
    return {
        "k": k,
        "labeled_count": len(labels),
        "ready_feature_count": len(ready),
        "opportunity_precision_at_k": opportunity_precision_at_k(
            [int(item["event_id"]) for item in ready if int(item["event_id"]) in labels_by_event],
            positive_ids,
            k,
        ),
        "trend_rule_baseline_precision_at_k": opportunity_precision_at_k(
            [int(item["event_id"]) for item in trend_baseline], positive_ids, k
        ),
        "category_accuracy": (
            round(sum(category_checks) / len(category_checks), 4)
            if category_checks else None
        ),
        "abstention_rate": (
            round(sum(item["status"] != "ready" for item in latest) / len(latest), 4)
            if latest else 0.0
        ),
        "non_consumer_label_share": (
            round(sum(item["label"] in negative_labels for item in labels) / len(labels), 4)
            if labels else 0.0
        ),
        "duplicate_candidate_count": db.one(
            "SELECT COUNT(*) n FROM semantic_duplicate_candidates"
        )["n"],
        "duplicate_reviewed_count": len(reviewed_duplicates),
        "duplicate_candidate_precision": (
            round(
                sum(item["review_status"] == "same_event" for item in reviewed_duplicates)
                / len(reviewed_duplicates),
                4,
            )
            if reviewed_duplicates else None
        ),
        "model_versions": model_versions,
    }


@app.get("/hypotheses", response_class=HTMLResponse)
async def product_hypotheses_page(request: Request):
    return templates.TemplateResponse(
        request,
        "hypotheses.html",
        {
            "hypotheses": product_hypothesis_rows(limit=300),
            "statuses": HYPOTHESIS_STATUSES,
        },
    )


@app.get("/api/product-hypotheses")
async def api_product_hypotheses(status: str | None = None, limit: int = 200):
    if limit < 1 or limit > 500:
        raise HTTPException(400, "limit must be between 1 and 500")
    if status and status not in HYPOTHESIS_STATUSES:
        raise HTTPException(400, "invalid hypothesis status")
    return product_hypothesis_rows(status, limit)


@app.post("/api/opportunity-signals/{signal_id}/product-hypotheses")
async def create_product_hypothesis(signal_id: int, value: ProductHypothesisInput):
    signal_raw = db.one("SELECT * FROM opportunity_signals WHERE id=?", (signal_id,))
    if not signal_raw:
        raise HTTPException(404, "opportunity signal not found")
    if signal_raw["review_status"] != "follow_up":
        raise HTTPException(409, "only a reviewed follow-up signal can create a hypothesis")
    signal = decode_signal(signal_raw)
    event = db.one("SELECT * FROM trend_events WHERE id=?", (signal["event_id"],))
    allowed_evidence = set(int(item) for item in signal["evidence_ids"])
    if not set(value.evidence_ids).issubset(allowed_evidence):
        raise HTTPException(400, "hypothesis evidence must be cited by its signal")
    risk_level, risk_flags = validate_physical_hypothesis(event or {}, value)
    generator = HumanProductHypothesisGenerator()
    evidence = db.all(
        "SELECT * FROM evidence WHERE event_id=? ORDER BY id", (signal["event_id"],)
    )
    result = await generator.generate(signal, evidence, value)
    now = utc_now()
    hypothesis_id = db.execute(
        """INSERT INTO product_hypotheses
        (opportunity_signal_id,name,physical_form,target_users_json,scenarios_json,
         problem,expected_difference,product_keywords_json,query_terms_json,
         target_marketplace,evidence_ids_json,generator_type,provider,model,version,
         risk_level,risk_flags_json,status,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'draft',?,?)""",
        (
            signal_id, result.value.name.strip(), result.value.physical_form.strip(),
            db.json(result.value.target_users), db.json(result.value.scenarios),
            result.value.problem.strip(), result.value.expected_difference.strip(),
            db.json(result.value.product_keywords),
            db.json(normalized_query_terms(result.value)),
            marketplace_code(result.value.target_marketplace),
            db.json(result.value.evidence_ids), result.generator_type, result.provider,
            result.model, result.version, risk_level, db.json(risk_flags), now, now,
        ),
    )
    return {
        "status": "draft",
        "hypothesis_id": hypothesis_id,
        "risk_level": risk_level,
        "risk_flags": risk_flags,
    }


@app.post("/api/product-hypotheses/{hypothesis_id}/review")
async def review_product_hypothesis(
    hypothesis_id: int, value: HypothesisReviewInput
):
    if value.status not in {"draft", "ready_for_validation", "rejected"}:
        raise HTTPException(400, "invalid hypothesis review status")
    raw = db.one("SELECT * FROM product_hypotheses WHERE id=?", (hypothesis_id,))
    if not raw:
        raise HTTPException(404, "product hypothesis not found")
    hypothesis = decode_hypothesis(raw)
    if value.status == "ready_for_validation":
        if hypothesis["risk_level"] == "blocking":
            raise HTTPException(409, "blocking risk must be resolved before validation")
        if not hypothesis["query_terms"]:
            raise HTTPException(409, "a reviewed product query term is required")
    snapshot = {
        "hypothesis": hypothesis,
        "signal": decode_signal(
            db.one(
                "SELECT * FROM opportunity_signals WHERE id=?",
                (hypothesis["opportunity_signal_id"],),
            )
        ),
        "status": value.status,
        "note": value.note.strip(),
    }
    now = utc_now()
    feedback_id = db.execute(
        """INSERT INTO product_hypothesis_feedback
        (hypothesis_id,status,note,snapshot_json,created_at) VALUES (?,?,?,?,?)""",
        (hypothesis_id, value.status, value.note.strip(), db.json(snapshot), now),
    )
    db.execute(
        """UPDATE product_hypotheses SET status=?,reviewer_note=?,updated_at=?
        WHERE id=?""",
        (value.status, value.note.strip(), now, hypothesis_id),
    )
    return {"status": value.status, "feedback_id": feedback_id}


@app.get("/validation", response_class=HTMLResponse)
async def validation_workbench(request: Request, marketplace: str = "ALL"):
    target = (
        "ALL"
        if marketplace.strip().upper() == "ALL"
        else normalize_target_marketplace(marketplace)
    )
    rows = pending_validation_rows(target, 100)
    hypotheses = product_hypothesis_rows("ready_for_validation", 200)
    if target != "ALL":
        hypotheses = [
            item for item in hypotheses if item["target_marketplace"] == target
        ]
    for item in hypotheses:
        item["market_evidence"] = decode_market_evidence(
            db.one(
                """SELECT * FROM market_evidence WHERE product_hypothesis_id=?
                ORDER BY id DESC LIMIT 1""",
                (item["id"],),
            )
        )
    return templates.TemplateResponse(
        request,
        "validation.html",
        {
            "opportunities": rows,
            "hypotheses": hypotheses,
            "selected_marketplace": target,
            "marketplaces": AMAZON_MARKETPLACES,
            "ready_count": sum(
                item["query_readiness"] == "ready" for item in rows
            ),
            "waiting_count": sum(
                item["query_readiness"] != "ready" for item in rows
            ),
        },
    )


@app.get("/api/product-hypotheses/{hypothesis_id}/market-evidence")
async def api_product_hypothesis_market_evidence(hypothesis_id: int):
    if not db.one("SELECT id FROM product_hypotheses WHERE id=?", (hypothesis_id,)):
        raise HTTPException(404, "product hypothesis not found")
    return [
        decode_market_evidence(item)
        for item in db.all(
            """SELECT * FROM market_evidence WHERE product_hypothesis_id=?
            ORDER BY id DESC""",
            (hypothesis_id,),
        )
    ]


@app.post("/api/product-hypotheses/{hypothesis_id}/market-evidence")
async def save_product_hypothesis_market_evidence(
    hypothesis_id: int, value: MarketValidationInput
):
    raw = db.one("SELECT * FROM product_hypotheses WHERE id=?", (hypothesis_id,))
    if not raw:
        raise HTTPException(404, "product hypothesis not found")
    hypothesis = decode_hypothesis(raw)
    provider = ManualMarketplaceDataProvider(value)
    result = await provider.validate(hypothesis)
    return persist_hypothesis_market_evidence(hypothesis, result)


@app.post("/api/product-hypotheses/{hypothesis_id}/amazon-raw-import")
async def import_product_hypothesis_amazon_raw_files(
    hypothesis_id: int, value: AmazonRawImportInput
):
    raw = db.one("SELECT * FROM product_hypotheses WHERE id=?", (hypothesis_id,))
    if not raw:
        raise HTTPException(404, "product hypothesis not found")
    hypothesis = decode_hypothesis(raw)
    provider = SellerCentralCsvProvider(
        value.product_opportunity_csv, value.hot_search_terms_csv
    )
    try:
        result = await provider.validate(hypothesis)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return persist_hypothesis_market_evidence(hypothesis, result)


@app.get("/recommendations", response_class=HTMLResponse)
async def validated_recommendations_page(request: Request):
    return templates.TemplateResponse(
        request,
        "recommendations.html",
        {"recommendations": validated_recommendation_rows(300)},
    )


@app.get("/api/validated-recommendations")
async def api_validated_recommendations(limit: int = 200):
    if limit < 1 or limit > 500:
        raise HTTPException(400, "limit must be between 1 and 500")
    return validated_recommendation_rows(limit)


@app.post("/api/run")
async def start_run():
    if pipeline.is_running:
        raise HTTPException(409, "pipeline is already running")

    async def execute() -> None:
        with suppress(Exception):
            await pipeline.run("manual")

    task = asyncio.create_task(execute())
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    # Let the task initialize its run id and progress snapshot before the UI polls.
    await asyncio.sleep(0)
    return JSONResponse({"status": "started"}, status_code=202)


@app.get("/api/run/status")
async def run_status():
    return current_run_status()


@app.post("/api/opportunities/{opportunity_id}/review/{status}")
async def review_opportunity(opportunity_id: int, status: str):
    if status not in {"approved", "rejected", "pending"}:
        raise HTTPException(400, "invalid review status")
    opportunity = db.one(
        "SELECT id, review_status, risk_level FROM product_opportunities WHERE id=?",
        (opportunity_id,),
    )
    if not opportunity:
        raise HTTPException(404, "opportunity not found")
    if opportunity["review_status"] == "superseded":
        raise HTTPException(409, "opportunity has been superseded by a newer analysis")
    if status == "approved" and opportunity["risk_level"] == "blocking":
        raise HTTPException(409, "blocking-risk opportunities cannot be approved")
    db.execute(
        "UPDATE product_opportunities SET review_status=?, updated_at=? WHERE id=?",
        (status, utc_now(), opportunity_id),
    )
    return {"status": status}


@app.post("/api/opportunities/{opportunity_id}/review")
async def review_opportunity_with_note(opportunity_id: int, value: ReviewInput):
    if value.status not in {"approved", "rejected", "pending"}:
        raise HTTPException(400, "invalid review status")
    opportunity = db.one(
        "SELECT id, review_status, risk_level FROM product_opportunities WHERE id=?",
        (opportunity_id,),
    )
    if not opportunity:
        raise HTTPException(404, "opportunity not found")
    if opportunity["review_status"] == "superseded":
        raise HTTPException(409, "opportunity has been superseded by a newer analysis")
    if value.status == "approved" and opportunity["risk_level"] == "blocking":
        raise HTTPException(409, "blocking-risk opportunities cannot be approved")
    db.execute(
        """UPDATE product_opportunities SET review_status=?, reviewer_note=?, updated_at=?
        WHERE id=?""",
        (value.status, value.note.strip(), utc_now(), opportunity_id),
    )
    return {"status": value.status, "note": value.note.strip()}


@app.post("/api/opportunities/{opportunity_id}/validation")
async def save_market_validation(opportunity_id: int, value: MarketValidationInput):
    opportunity = db.one(
        """SELECT o.*, e.trend_score FROM product_opportunities o
        JOIN trend_events e ON e.id=o.event_id WHERE o.id=?""",
        (opportunity_id,),
    )
    if not opportunity:
        raise HTTPException(404, "opportunity not found")
    if opportunity["review_status"] == "superseded":
        raise HTTPException(409, "opportunity has been superseded by a newer analysis")
    if value.marketplace:
        submitted_marketplace = normalize_target_marketplace(value.marketplace)
        if submitted_marketplace != opportunity["target_marketplace"]:
            raise HTTPException(409, "validation marketplace does not match opportunity target")
    return persist_market_validation(opportunity, value)


@app.post("/api/opportunities/{opportunity_id}/target-marketplace")
async def change_target_marketplace(opportunity_id: int, value: TargetMarketplaceInput):
    opportunity = db.one(
        """SELECT o.*, e.trend_score FROM product_opportunities o
        JOIN trend_events e ON e.id=o.event_id WHERE o.id=?""",
        (opportunity_id,),
    )
    if not opportunity:
        raise HTTPException(404, "opportunity not found")
    if opportunity["review_status"] == "superseded":
        raise HTTPException(409, "opportunity has been superseded by a newer analysis")
    target = normalize_target_marketplace(value.target_marketplace)
    if target == opportunity["target_marketplace"]:
        return {"status": "unchanged", "target_marketplace": target}
    final_score, penalty = calculate_final_score(
        trend_score=float(opportunity["trend_score"]),
        hypothesis_score=float(opportunity["hypothesis_score"]),
        market_score=None,
        validation_status="pending",
        risk_level=opportunity["risk_level"],
    )
    now = utc_now()
    search_term = pick_search_term(opportunity["amazon_search_term"], (), target)
    db.execute(
        """UPDATE product_opportunities SET target_marketplace=?, marketplace=?,
        amazon_search_term=?,
        market_score=NULL, validation_status='pending', final_score=0,
        validated_recommendation_score=NULL, opportunity_score=?,
        uncertainty_penalty=?, review_status='pending', reviewer_note='', updated_at=? WHERE id=?""",
        (
            target, marketplace_name(target), search_term, opportunity["hypothesis_score"],
            penalty, now, opportunity_id,
        ),
    )
    db.execute(
        """INSERT INTO market_validations
        (opportunity_id,provider,provider_version,status,query_json,scores_json,
         metrics_json,sources_json,missing_fields_json,market_score,note,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            opportunity_id, "amazon-first-party", "target-change-v1", "pending",
            db.json({"target_marketplace": target}),
            db.json({key: None for key in MarketScores.model_fields}), "{}", "[]",
            db.json(list(MarketScores.model_fields)), None,
            "目标站点变化，旧站点的验证结果不再用于当前评分", now,
        ),
    )
    return {"status": "updated", "target_marketplace": target, "marketplace": marketplace_name(target)}


@app.post("/api/opportunities/{opportunity_id}/search-term")
async def change_amazon_search_term(opportunity_id: int, value: AmazonSearchTermInput):
    opportunity = db.one(
        """SELECT o.*, e.trend_score FROM product_opportunities o
        JOIN trend_events e ON e.id=o.event_id WHERE o.id=?""",
        (opportunity_id,),
    )
    if not opportunity:
        raise HTTPException(404, "opportunity not found")
    if opportunity["review_status"] == "superseded":
        raise HTTPException(409, "opportunity has been superseded by a newer analysis")
    term = normalize_search_term(value.search_term)
    target = opportunity["target_marketplace"] or "US"
    if not is_search_term_ready(term, target):
        raise HTTPException(
            400,
            "搜索词必须是目标站点买家会输入的具体商品词；美国站请使用 3-120 字符的英文商品词",
        )
    if term == opportunity["amazon_search_term"]:
        return {"status": "unchanged", "search_term": term, "query_readiness": "ready"}
    final_score, penalty = calculate_final_score(
        trend_score=float(opportunity["trend_score"]),
        hypothesis_score=float(opportunity["hypothesis_score"]),
        market_score=None,
        validation_status="pending",
        risk_level=opportunity["risk_level"],
    )
    now = utc_now()
    db.execute(
        """UPDATE product_opportunities SET amazon_search_term=?, market_score=NULL,
        validation_status='pending', final_score=0, validated_recommendation_score=NULL,
        opportunity_score=?, uncertainty_penalty=?,
        review_status='pending', reviewer_note='', updated_at=? WHERE id=?""",
        (term, opportunity["hypothesis_score"], penalty, now, opportunity_id),
    )
    db.execute(
        """INSERT INTO market_validations
        (opportunity_id,provider,provider_version,status,query_json,scores_json,
         metrics_json,sources_json,missing_fields_json,market_score,note,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            opportunity_id, "amazon-first-party", "query-change-v1", "pending",
            db.json({"target_marketplace": target, "search_term": term}),
            db.json({key: None for key in MarketScores.model_fields}), "{}", "[]",
            db.json(list(MarketScores.model_fields)), None,
            "Amazon 查询词变化，旧查询的验证结果不再用于当前评分", now,
        ),
    )
    return {"status": "updated", "search_term": term, "query_readiness": "ready"}


@app.get("/api/opportunities/pending-validation")
async def pending_validations(marketplace: str = "ALL", limit: int = 20):
    target = "ALL" if marketplace.strip().upper() == "ALL" else normalize_target_marketplace(marketplace)
    return pending_validation_rows(target, limit)


@app.get("/api/market-validations/template.csv")
async def validation_template(marketplace: str = "ALL"):
    target = "ALL" if marketplace.strip().upper() == "ALL" else normalize_target_marketplace(marketplace)
    content = build_template(pending_validation_rows(target, 100))
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="amazon-validation-template.csv"'},
    )


@app.post("/api/market-validations/import")
async def import_market_validations(request: Request):
    body = await request.body()
    if len(body) > 2_000_000:
        raise HTTPException(413, "CSV must be smaller than 2 MB")
    try:
        content = body.decode("utf-8-sig")
        parsed = parse_validation_csv(content)
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc

    prepared: list[tuple[dict, MarketValidationInput]] = []
    seen_ids: set[int] = set()
    for row in parsed:
        if row.opportunity_id in seen_ids:
            raise HTTPException(400, f"第 {row.line_number} 行 opportunity_id 重复")
        seen_ids.add(row.opportunity_id)
        opportunity = db.one(
            """SELECT o.*, e.trend_score FROM product_opportunities o
            JOIN trend_events e ON e.id=o.event_id WHERE o.id=?""",
            (row.opportunity_id,),
        )
        if not opportunity:
            raise HTTPException(400, f"第 {row.line_number} 行机会不存在：{row.opportunity_id}")
        if opportunity["review_status"] == "superseded":
            raise HTTPException(409, f"第 {row.line_number} 行机会已过期")
        if row.target_marketplace != opportunity["target_marketplace"]:
            raise HTTPException(
                409,
                f"第 {row.line_number} 行站点 {row.target_marketplace} 与机会目标站点 {opportunity['target_marketplace']} 不一致",
            )
        prepared.append((opportunity, row.value))
    results = [persist_market_validation(opportunity, value) for opportunity, value in prepared]
    return {"status": "imported", "count": len(results), "results": results}


@app.post("/api/opportunities/{opportunity_id}/amazon-raw-import")
async def import_amazon_raw_files(opportunity_id: int, value: AmazonRawImportInput):
    opportunity = db.one(
        """SELECT o.*, e.trend_score FROM product_opportunities o
        JOIN trend_events e ON e.id=o.event_id WHERE o.id=?""",
        (opportunity_id,),
    )
    if not opportunity:
        raise HTTPException(404, "opportunity not found")
    if opportunity["review_status"] == "superseded":
        raise HTTPException(409, "opportunity has been superseded by a newer analysis")
    search_term = opportunity["amazon_search_term"]
    if not is_search_term_ready(search_term, opportunity["target_marketplace"]):
        raise HTTPException(409, "请先保存可用于目标站点的具体商品查询词")
    try:
        validation = build_raw_amazon_validation(
            opportunity_id=opportunity_id,
            marketplace=opportunity["target_marketplace"],
            search_term=search_term,
            product_opportunity_csv=value.product_opportunity_csv,
            hot_search_terms_csv=value.hot_search_terms_csv,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    saved = persist_market_validation(opportunity, validation)
    return {
        **saved,
        "provider": validation.provider,
        "search_term": search_term,
        "scores": validation.scores.model_dump(),
        "score_explanations": validation.metrics.get("score_explanations", {}),
        "source_rows_scanned": validation.metrics.get("source_rows_scanned", {}),
    }


@app.post("/api/opportunities/{opportunity_id}/outcomes/{horizon_days}")
async def save_outcome(opportunity_id: int, horizon_days: int, value: OutcomeInput):
    if horizon_days not in {7, 30}:
        raise HTTPException(400, "horizon_days must be 7 or 30")
    if value.result not in {"unknown", "positive", "negative", "mixed", "abandoned"}:
        raise HTTPException(400, "invalid outcome result")
    if not db.one("SELECT id FROM product_opportunities WHERE id=?", (opportunity_id,)):
        raise HTTPException(404, "opportunity not found")
    now = utc_now()
    db.execute(
        """INSERT INTO opportunity_outcomes
        (opportunity_id,horizon_days,result,metrics_json,note,updated_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(opportunity_id,horizon_days) DO UPDATE SET
        result=excluded.result,metrics_json=excluded.metrics_json,
        note=excluded.note,updated_at=excluded.updated_at""",
        (opportunity_id, horizon_days, value.result, db.json(value.metrics), value.note, now),
    )
    return {"status": "saved", "horizon_days": horizon_days, "result": value.result}


@app.post("/api/opportunities/{opportunity_id}/push")
async def push_opportunity(opportunity_id: int):
    opportunity = db.one(
        """SELECT o.* FROM product_opportunities o
        JOIN analyses a ON a.id=o.analysis_id
        WHERE o.id=? AND o.review_status!='superseded' AND a.status!='superseded'""",
        (opportunity_id,),
    )
    if not opportunity:
        exists = db.one("SELECT id FROM product_opportunities WHERE id=?", (opportunity_id,))
        if exists:
            raise HTTPException(409, "opportunity has been superseded by a newer analysis")
        raise HTTPException(404, "opportunity not found")
    if opportunity["review_status"] != "approved":
        raise HTTPException(409, "only approved opportunities can be pushed")
    if opportunity["risk_level"] == "blocking":
        raise HTTPException(409, "blocking-risk opportunities cannot be pushed")
    if not is_validated_recommendation(opportunity):
        raise HTTPException(
            409,
            "only completed, market-evidence-backed recommendations can be pushed",
        )
    event = db.one("SELECT * FROM trend_events WHERE id=?", (opportunity["event_id"],))
    destination_hash = hashlib.sha256(
        (settings.feishu_webhook_url or "unconfigured").encode("utf-8")
    ).hexdigest()[:16]
    idempotency_key = f"feishu:{opportunity_id}:{destination_hash}"
    claimed, delivery = db.claim_notification(opportunity_id, idempotency_key)
    if not claimed:
        if delivery["status"] == "unknown":
            raise HTTPException(
                409,
                "delivery result is unknown; confirm in Feishu before retrying",
            )
        status = "already_sent" if delivery["status"] == "sent" else "in_progress"
        code = 200 if status == "already_sent" else 202
        return JSONResponse(
            {"status": status, "delivery_id": delivery["id"]}, status_code=code
        )
    result = await send_opportunity(settings, opportunity, event)
    db.execute(
        """UPDATE notification_deliveries SET status=?, attempted_at=?,
        http_status=?, response_excerpt=?, error=? WHERE id=?""",
        (
            "sent" if result.success else "failed",
            delivery_timestamp(),
            result.status_code,
            result.response_excerpt,
            result.error,
            delivery["id"],
        ),
    )
    if not result.success:
        raise HTTPException(502, result.error or "Feishu delivery failed")
    return {"status": "sent", "delivery_id": delivery["id"]}


@app.get("/api/digest")
async def api_digest():
    return build_daily_digest(db)


@app.post("/api/digest/push")
async def push_digest():
    digest = build_daily_digest(db)
    item_ids = [
        item["id"] for group in (digest["cn_top3"], digest["overseas_top3"])
        for item in group
    ]
    destination_hash = hashlib.sha256(
        (settings.feishu_webhook_url or "unconfigured").encode("utf-8")
    ).hexdigest()[:16]
    identity = json.dumps(
        {"date": digest["date"], "ids": item_ids, "destination": destination_hash},
        sort_keys=True,
    )
    digest_key = "feishu-digest:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    claimed, delivery = db.claim_digest(digest_key, digest)
    if not claimed:
        if delivery["status"] == "unknown":
            raise HTTPException(
                409, "digest delivery result is unknown; confirm in Feishu before retrying"
            )
        status = "already_sent" if delivery["status"] == "sent" else "in_progress"
        return JSONResponse(
            {"status": status, "delivery_id": delivery["id"]},
            status_code=200 if status == "already_sent" else 202,
        )
    result = await send_daily_digest(settings, digest)
    db.execute(
        """UPDATE digest_deliveries SET status=?,attempted_at=?,http_status=?,
        response_excerpt=?,error=? WHERE id=?""",
        (
            "sent" if result.success else "failed", delivery_timestamp(),
            result.status_code, result.response_excerpt, result.error, delivery["id"],
        ),
    )
    if not result.success:
        raise HTTPException(502, result.error or "Feishu digest delivery failed")
    return {"status": "sent", "delivery_id": delivery["id"]}


@app.get("/api/events")
async def api_events(market: str = "ALL"):
    market = normalize_market(market)
    if market == "ALL":
        rows = db.all("SELECT * FROM trend_events ORDER BY trend_score DESC LIMIT 1000")
    else:
        rows = db.all(
            "SELECT * FROM trend_events WHERE market=? ORDER BY trend_score DESC LIMIT 1000",
            (market,),
        )
    return deduplicate_events(rows, limit=100)


@app.get("/healthz")
async def healthz():
    latest_run = db.one("SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT 1")
    latest_analysis = db.one("SELECT engine, model, status, error FROM analyses ORDER BY id DESC LIMIT 1")
    return {
        "status": "ok",
        "database": str(settings.database_path),
        "pipeline_running": pipeline.is_running,
        "analysis_engine": "llm" if settings.openai_api_key else "local-rules-v2",
        "feishu_configured": bool(settings.feishu_webhook_url),
        "reddit_configured": bool(
            settings.reddit_client_id and settings.reddit_client_secret
        ),
        "latest_run": latest_run,
        "latest_analysis": latest_analysis,
    }
