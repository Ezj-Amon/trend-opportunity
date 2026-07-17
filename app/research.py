from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from .db import Database


_SENSITIVE_REQUEST_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
}


class ResearchBudget(BaseModel):
    max_search_queries: int = Field(default=8, ge=0, le=100)
    max_fetch_pages: int = Field(default=15, ge=0, le=200)
    max_browser_pages: int = Field(default=3, ge=0, le=50)
    timeout_seconds: int = Field(default=300, ge=1, le=3600)
    markets: list[str] = Field(default_factory=list, max_length=20)
    languages: list[str] = Field(default_factory=list, max_length=20)


class ResearchRunInput(BaseModel):
    executor_type: str = Field(default="human", min_length=1, max_length=32)
    executor_name: str = Field(default="human-workbench", min_length=1, max_length=120)
    budget: ResearchBudget = Field(default_factory=ResearchBudget)


class ResearchRunCompleteInput(BaseModel):
    status: str = Field(default="completed", min_length=1, max_length=32)
    error: str = Field(default="", max_length=2000)


class ResearchToolResultInput(BaseModel):
    tool_name: str = Field(min_length=1, max_length=120)
    request: dict = Field(default_factory=dict)
    status: str = Field(min_length=1, max_length=32)
    result_evidence_ids: list[int] = Field(default_factory=list, max_length=500)
    latency_ms: int = Field(default=0, ge=0, le=3_600_000)
    error: str = Field(default="", max_length=2000)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def start_research_run(
    db: Database, candidate: dict, value: ResearchRunInput
) -> dict:
    if value.executor_type not in {"human", "rules", "agent"}:
        raise ValueError("invalid executor type")
    if candidate["status"] in {"completed", "superseded"}:
        raise ValueError("candidate is not runnable")
    existing = db.one(
        """SELECT * FROM research_runs
        WHERE candidate_id=? AND executor_type=? AND executor_name=? AND status='running'
        ORDER BY started_at DESC LIMIT 1""",
        (candidate["id"], value.executor_type, value.executor_name),
    )
    if existing:
        budget = json.loads(existing.get("budget_json") or "{}")
        lease_name = f"research-candidate:{candidate['id']}"
        ttl_seconds = int(budget.get("timeout_seconds") or 300) + 60
        renewed = db.renew_lease(lease_name, existing["id"], ttl_seconds)
        reacquired = renewed or db.acquire_lease(
            lease_name, existing["id"], ttl_seconds
        )
        if not reacquired:
            raise ValueError("research candidate already has an active executor")
        return decode_research_run(existing)
    run_id = str(uuid.uuid4())
    lease_name = f"research-candidate:{candidate['id']}"
    if not db.acquire_lease(
        lease_name, run_id, value.budget.timeout_seconds + 60
    ):
        raise ValueError("research candidate already has an active executor")
    now = utc_now()
    try:
        db.execute(
            """INSERT INTO research_runs
            (id,candidate_id,executor_type,executor_name,status,budget_json,started_at)
            VALUES (?,?,?,?, 'running',?,?)""",
            (
                run_id,
                candidate["id"],
                value.executor_type,
                value.executor_name,
                db.json(value.budget.model_dump()),
                now,
            ),
        )
    except Exception:
        db.release_lease(lease_name, run_id)
        raise
    db.execute(
        "UPDATE research_candidates SET status='researching',updated_at=? WHERE id=?",
        (now, candidate["id"]),
    )
    return decode_research_run(
        db.one("SELECT * FROM research_runs WHERE id=?", (run_id,))
    )


def complete_research_run(
    db: Database, run: dict, value: ResearchRunCompleteInput
) -> dict:
    if value.status not in {"completed", "failed"}:
        raise ValueError("invalid run completion status")
    if run["status"] != "running":
        db.release_lease(f"research-candidate:{run['candidate_id']}", run["id"])
        return decode_research_run(run)
    now = utc_now()
    db.execute(
        "UPDATE research_runs SET status=?,finished_at=?,error=? WHERE id=?",
        (value.status, now, value.error.strip() or None, run["id"]),
    )
    candidate_status = "failed"
    if value.status == "completed":
        bundle = db.one(
            """SELECT b.readiness_status FROM research_candidates c
            JOIN evidence_bundles b ON b.id=c.evidence_bundle_id
            WHERE c.id=?""",
            (run["candidate_id"],),
        )
        candidate_status = (
            "evidence_ready"
            if bundle and bundle["readiness_status"] == "ready_for_assessment"
            else "insufficient_evidence"
        )
    db.execute(
        """UPDATE research_candidates SET status=?,updated_at=?
        WHERE id=? AND status='researching'""",
        (candidate_status, now, run["candidate_id"]),
    )
    db.release_lease(f"research-candidate:{run['candidate_id']}", run["id"])
    return decode_research_run(
        db.one("SELECT * FROM research_runs WHERE id=?", (run["id"],))
    )


def record_research_tool_call(
    db: Database, run: dict, value: ResearchToolResultInput
) -> dict:
    if run["status"] != "running":
        raise ValueError("research run is not running")
    if value.status not in {"completed", "failed", "partial"}:
        raise ValueError("invalid tool call status")
    request_hash = research_request_hash(value.request)
    budget = json.loads(run.get("budget_json") or "{}")
    db.renew_lease(
        f"research-candidate:{run['candidate_id']}",
        run["id"],
        int(budget.get("timeout_seconds") or 300) + 60,
    )
    existing = db.one(
        """SELECT * FROM research_tool_calls
        WHERE run_id=? AND tool_name=? AND request_hash=?
        ORDER BY id DESC LIMIT 1""",
        (run["id"], value.tool_name, request_hash),
    )
    if existing:
        return decode_research_tool_call(existing)
    call_id = db.execute(
        """INSERT INTO research_tool_calls
        (run_id,tool_name,request_hash,status,result_evidence_ids_json,
         latency_ms,error,created_at) VALUES (?,?,?,?,?,?,?,?)""",
        (
            run["id"],
            value.tool_name,
            request_hash,
            value.status,
            db.json(value.result_evidence_ids),
            value.latency_ms,
            redact_tool_error(value.error.strip()) or None,
            utc_now(),
        ),
    )
    return decode_research_tool_call(
        db.one("SELECT * FROM research_tool_calls WHERE id=?", (call_id,))
    )


def research_request_hash(request: dict) -> str:
    _reject_sensitive_request(request)
    encoded = json.dumps(
        request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _reject_sensitive_request(value: object) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized in _SENSITIVE_REQUEST_KEYS or any(
                marker in normalized
                for marker in ("api_key", "access_token", "auth_token")
            ):
                raise ValueError("research tool request contains a sensitive credential field")
            _reject_sensitive_request(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_sensitive_request(nested)
    elif isinstance(value, str) and re.search(
        r"(?i)(?:[?&]|\b)(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)=[^&\s]+",
        value,
    ):
        raise ValueError("research tool request URL contains a sensitive credential")


def redact_tool_error(error: str) -> str:
    redacted = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [REDACTED]", error)
    return re.sub(
        r"(?i)(api[_-]?key|access[_-]?token|auth[_-]?token|token|password|secret|cookie)"
        r"(\s*[:=]\s*)([^\s,;&]+)",
        r"\1\2[REDACTED]",
        redacted,
    )


def decode_research_run(row: dict) -> dict:
    decoded = dict(row)
    decoded["budget"] = json.loads(decoded.get("budget_json") or "{}")
    return decoded


def decode_research_tool_call(row: dict) -> dict:
    decoded = dict(row)
    decoded["result_evidence_ids"] = json.loads(
        decoded.get("result_evidence_ids_json") or "[]"
    )
    return decoded
