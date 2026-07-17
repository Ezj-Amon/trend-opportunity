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
from .market_validation import MarketScores, MarketValidationInput, result_from_input
from .pipeline import Pipeline, utc_now
from .reports import build_daily_digest, decode_opportunity_data
from .scoring import calculate_final_score


BASE_DIR = Path(__file__).resolve().parent
settings = Settings.from_env()
db = Database(settings.database_path)
pipeline = Pipeline(db, settings)
templates = Jinja2Templates(directory=BASE_DIR / "templates")
background_tasks: set[asyncio.Task] = set()

RUN_STAGE_LABELS = {
    "ingest": "连接数据源并采集",
    "cluster": "整理并合并相似趋势",
    "analyze": "生成可验证的产品机会",
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


app = FastAPI(title="趋势新闻自动选品助手", version="0.1.0", lifespan=lifespan)
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
            item.get("amazon_search_term"),
            item.get("product_keywords") or [],
            item.get("target_marketplace") or "US",
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
        validation_status=?, uncertainty_penalty=?, opportunity_score=?,
        score_formula_version='opportunity-v2', updated_at=? WHERE id=?""",
        (
            result.score, final_score, result.status, uncertainty_penalty,
            final_score, now, opportunity["id"],
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
        (SELECT COUNT(*) FROM product_opportunities o
         WHERE o.event_id=e.id AND o.review_status!='superseded') opportunity_count,
        (SELECT engine FROM analyses a WHERE a.event_id=e.id AND a.status!='superseded'
         ORDER BY a.id DESC LIMIT 1) latest_analysis_engine,
        (SELECT MAX(opportunity_score) FROM product_opportunities o
         WHERE o.event_id=e.id AND o.review_status!='superseded') best_opportunity_score
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
            "feishu_configured": bool(settings.feishu_webhook_url),
            "marketplaces": AMAZON_MARKETPLACES,
        },
    )


@app.get("/validation", response_class=HTMLResponse)
async def validation_workbench(request: Request, marketplace: str = "ALL"):
    target = (
        "ALL"
        if marketplace.strip().upper() == "ALL"
        else normalize_target_marketplace(marketplace)
    )
    rows = pending_validation_rows(target, 100)
    return templates.TemplateResponse(
        request,
        "validation.html",
        {
            "opportunities": rows,
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
    keywords = json.loads(opportunity["product_keywords_json"] or "[]")
    search_term = pick_search_term(opportunity["amazon_search_term"], keywords, target)
    db.execute(
        """UPDATE product_opportunities SET target_marketplace=?, marketplace=?,
        amazon_search_term=?,
        market_score=NULL, validation_status='pending', final_score=?, opportunity_score=?,
        uncertainty_penalty=?, review_status='pending', reviewer_note='', updated_at=? WHERE id=?""",
        (
            target, marketplace_name(target), search_term, final_score, final_score,
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
        validation_status='pending', final_score=?, opportunity_score=?, uncertainty_penalty=?,
        review_status='pending', reviewer_note='', updated_at=? WHERE id=?""",
        (term, final_score, final_score, penalty, now, opportunity_id),
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
        "analysis_engine": "llm" if settings.openai_api_key else "local-rules-v1",
        "feishu_configured": bool(settings.feishu_webhook_url),
        "reddit_configured": bool(
            settings.reddit_client_id and settings.reddit_client_secret
        ),
        "latest_run": latest_run,
        "latest_analysis": latest_analysis,
    }
