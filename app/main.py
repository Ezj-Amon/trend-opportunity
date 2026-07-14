from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import Settings
from .db import Database
from .feishu import delivery_timestamp, send_opportunity
from .pipeline import Pipeline, utc_now


BASE_DIR = Path(__file__).resolve().parent
settings = Settings.from_env()
db = Database(settings.database_path)
pipeline = Pipeline(db, settings)
templates = Jinja2Templates(directory=BASE_DIR / "templates")
background_tasks: set[asyncio.Task] = set()


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
    for key in ("pain_points_json", "channels_json", "risks_json"):
        row[key.removesuffix("_json")] = json.loads(row[key])
    return row


def normalize_market(value: str) -> str:
    market = value.strip().upper()
    if market != "ALL" and not re.fullmatch(r"[A-Z]{2,12}", market):
        raise HTTPException(400, "invalid market")
    return market


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, market: str = "ALL"):
    market = normalize_market(market)
    where = "" if market == "ALL" else "WHERE e.market=?"
    params = () if market == "ALL" else (market,)
    events = db.all(
        f"""SELECT e.*,
        (SELECT COUNT(*) FROM product_opportunities o WHERE o.event_id=e.id) opportunity_count,
        (SELECT engine FROM analyses a WHERE a.event_id=e.id AND a.status!='superseded'
         ORDER BY a.id DESC LIMIT 1) latest_analysis_engine,
        (SELECT MAX(opportunity_score) FROM product_opportunities o
         WHERE o.event_id=e.id AND o.review_status!='superseded') best_opportunity_score
        FROM trend_events e {where}
        ORDER BY e.trend_score DESC, e.last_seen_at DESC LIMIT 80""",
        params,
    )
    market_counts = {
        row["market"]: row["count"]
        for row in db.all("SELECT market, COUNT(*) count FROM trend_events GROUP BY market")
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
    opportunities = [
        decode_opportunity(row)
        for row in db.all(
            """SELECT * FROM product_opportunities
            WHERE event_id=? AND analysis_id=? ORDER BY opportunity_score DESC""",
            (event_id, analysis["id"] if analysis else -1),
        )
    ]
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
    return JSONResponse({"status": "started"}, status_code=202)


@app.post("/api/opportunities/{opportunity_id}/review/{status}")
async def review_opportunity(opportunity_id: int, status: str):
    if status not in {"approved", "rejected", "pending"}:
        raise HTTPException(400, "invalid review status")
    opportunity = db.one(
        "SELECT id, review_status FROM product_opportunities WHERE id=?",
        (opportunity_id,),
    )
    if not opportunity:
        raise HTTPException(404, "opportunity not found")
    if opportunity["review_status"] == "superseded":
        raise HTTPException(409, "opportunity has been superseded by a newer analysis")
    db.execute(
        "UPDATE product_opportunities SET review_status=?, updated_at=? WHERE id=?",
        (status, utc_now(), opportunity_id),
    )
    return {"status": status}


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


@app.get("/api/events")
async def api_events(market: str = "ALL"):
    market = normalize_market(market)
    if market == "ALL":
        return db.all("SELECT * FROM trend_events ORDER BY trend_score DESC LIMIT 100")
    return db.all(
        "SELECT * FROM trend_events WHERE market=? ORDER BY trend_score DESC LIMIT 100",
        (market,),
    )


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
