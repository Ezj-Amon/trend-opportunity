from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline_runs (
  id TEXT PRIMARY KEY,
  trigger TEXT NOT NULL,
  status TEXT NOT NULL,
  stage TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  items_count INTEGER NOT NULL DEFAULT 0,
  events_count INTEGER NOT NULL DEFAULT 0,
  selected_count INTEGER NOT NULL DEFAULT 0,
  error_summary TEXT,
  config_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES pipeline_runs(id),
  source TEXT NOT NULL,
  market TEXT NOT NULL DEFAULT 'CN',
  language TEXT NOT NULL DEFAULT 'zh',
  signal_type TEXT NOT NULL DEFAULT 'news',
  fetched_at TEXT NOT NULL,
  success INTEGER NOT NULL,
  status_code INTEGER,
  latency_ms INTEGER NOT NULL,
  error TEXT,
  payload_hash TEXT,
  raw_payload_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshots_source_time
  ON source_snapshots(source, fetched_at DESC);
CREATE TABLE IF NOT EXISTS source_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_id INTEGER NOT NULL REFERENCES source_snapshots(id),
  source TEXT NOT NULL,
  market TEXT NOT NULL DEFAULT 'CN',
  language TEXT NOT NULL DEFAULT 'zh',
  signal_type TEXT NOT NULL DEFAULT 'news',
  external_id TEXT NOT NULL,
  title TEXT NOT NULL,
  normalized_title TEXT NOT NULL,
  url TEXT NOT NULL,
  rank INTEGER NOT NULL,
  item_count INTEGER NOT NULL,
  fetched_at TEXT NOT NULL,
  raw_json TEXT NOT NULL,
  UNIQUE(snapshot_id, source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_items_time ON source_items(fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_normalized ON source_items(normalized_title);
CREATE TABLE IF NOT EXISTS trend_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  canonical_title TEXT NOT NULL,
  normalized_title TEXT NOT NULL,
  market TEXT NOT NULL DEFAULT 'CN',
  language TEXT NOT NULL DEFAULT 'zh',
  signal_type TEXT NOT NULL DEFAULT 'news',
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  source_count INTEGER NOT NULL DEFAULT 1,
  member_count INTEGER NOT NULL DEFAULT 1,
  trend_score REAL NOT NULL DEFAULT 0,
  coverage_score REAL NOT NULL DEFAULT 0,
  rank_score REAL NOT NULL DEFAULT 0,
  velocity_score REAL NOT NULL DEFAULT 50,
  persistence_score REAL NOT NULL DEFAULT 0,
  freshness_score REAL NOT NULL DEFAULT 100,
  score_formula_version TEXT NOT NULL DEFAULT 'trend-v1',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_score ON trend_events(trend_score DESC);
CREATE TABLE IF NOT EXISTS event_members (
  event_id INTEGER NOT NULL REFERENCES trend_events(id),
  source_item_id INTEGER NOT NULL REFERENCES source_items(id),
  match_method TEXT NOT NULL,
  match_score REAL NOT NULL,
  PRIMARY KEY(event_id, source_item_id)
);
CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER NOT NULL REFERENCES trend_events(id),
  kind TEXT NOT NULL,
  url TEXT NOT NULL,
  title TEXT NOT NULL,
  excerpt TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  http_status INTEGER,
  content_hash TEXT,
  is_consumer_voice INTEGER NOT NULL DEFAULT 0,
  valid_for_analysis INTEGER NOT NULL DEFAULT 1,
  error TEXT,
  UNIQUE(event_id, url)
);
CREATE TABLE IF NOT EXISTS analyses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER NOT NULL REFERENCES trend_events(id),
  run_id TEXT NOT NULL REFERENCES pipeline_runs(id),
  engine TEXT NOT NULL,
  model TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  output_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'succeeded',
  error TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS product_opportunities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  analysis_id INTEGER NOT NULL REFERENCES analyses(id),
  event_id INTEGER NOT NULL REFERENCES trend_events(id),
  name TEXT NOT NULL,
  target_segment TEXT NOT NULL,
  scenario TEXT NOT NULL,
  jtbd TEXT NOT NULL,
  pain_points_json TEXT NOT NULL,
  solution TEXT NOT NULL,
  mvp TEXT NOT NULL,
  price_band TEXT NOT NULL,
  marketplace TEXT NOT NULL DEFAULT '',
  channels_json TEXT NOT NULL,
  risks_json TEXT NOT NULL,
  pain_score INTEGER NOT NULL,
  intent_score INTEGER NOT NULL,
  segment_score INTEGER NOT NULL,
  timing_score INTEGER NOT NULL,
  feasibility_score INTEGER NOT NULL,
  differentiation_score INTEGER NOT NULL,
  opportunity_score REAL NOT NULL,
  evidence_confidence REAL NOT NULL,
  review_status TEXT NOT NULL DEFAULT 'pending',
  reviewer_note TEXT NOT NULL DEFAULT '',
  score_formula_version TEXT NOT NULL DEFAULT 'opportunity-v1',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_opportunities_score
  ON product_opportunities(opportunity_score DESC);
CREATE TABLE IF NOT EXISTS notification_deliveries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  opportunity_id INTEGER NOT NULL REFERENCES product_opportunities(id),
  channel TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL,
  attempted_at TEXT NOT NULL,
  http_status INTEGER,
  response_excerpt TEXT,
  error TEXT
);
CREATE TABLE IF NOT EXISTS job_leases (
  name TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._write_lock = threading.RLock()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._ensure_column(conn, "evidence", "valid_for_analysis", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(conn, "evidence", "error", "TEXT")
            self._ensure_column(conn, "analyses", "status", "TEXT NOT NULL DEFAULT 'succeeded'")
            self._ensure_column(conn, "analyses", "error", "TEXT")
            for table in ("source_snapshots", "source_items", "trend_events"):
                self._ensure_column(conn, table, "market", "TEXT NOT NULL DEFAULT 'CN'")
                self._ensure_column(conn, table, "language", "TEXT NOT NULL DEFAULT 'zh'")
                self._ensure_column(
                    conn, table, "signal_type", "TEXT NOT NULL DEFAULT 'news'"
                )
            self._ensure_column(
                conn,
                "product_opportunities",
                "marketplace",
                "TEXT NOT NULL DEFAULT ''",
            )

    @staticmethod
    def _ensure_column(
        conn: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self._write_lock, self.connect() as conn:
            cursor = conn.execute(sql, params)
            return int(cursor.lastrowid or 0)

    def executemany(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        with self._write_lock, self.connect() as conn:
            conn.executemany(sql, rows)

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def acquire_lease(self, name: str, owner: str, ttl_seconds: int = 1800) -> bool:
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(seconds=ttl_seconds)).isoformat()
        with self._write_lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT owner, expires_at FROM job_leases WHERE name=?", (name,)).fetchone()
            if row and row["owner"] != owner and row["expires_at"] > now.isoformat():
                return False
            conn.execute(
                "INSERT OR REPLACE INTO job_leases(name, owner, expires_at) VALUES (?, ?, ?)",
                (name, owner, expires),
            )
            return True

    def release_lease(self, name: str, owner: str) -> None:
        self.execute("DELETE FROM job_leases WHERE name=? AND owner=?", (name, owner))

    def renew_lease(self, name: str, owner: str, ttl_seconds: int = 1800) -> bool:
        expires = (
            datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        ).isoformat()
        with self._write_lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE job_leases SET expires_at=? WHERE name=? AND owner=?",
                (expires, name, owner),
            )
            return cursor.rowcount == 1

    def clear_derived_data(self) -> None:
        """Reset rebuildable results while retaining immutable source snapshots/items."""
        with self._write_lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for table in (
                "notification_deliveries",
                "product_opportunities",
                "analyses",
                "evidence",
                "event_members",
                "trend_events",
            ):
                conn.execute(f"DELETE FROM {table}")

    def claim_notification(self, opportunity_id: int, idempotency_key: str) -> tuple[bool, dict[str, Any]]:
        now = datetime.now(timezone.utc).isoformat()
        with self._write_lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM notification_deliveries WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if row and row["status"] == "sent":
                return False, dict(row)
            if row and row["status"] == "sending":
                attempted = datetime.fromisoformat(row["attempted_at"])
                if attempted > datetime.now(timezone.utc) - timedelta(minutes=10):
                    return False, dict(row)
                conn.execute(
                    "UPDATE notification_deliveries SET status='unknown', error=? WHERE id=?",
                    ("发送进程中断，结果未知；请在飞书确认后再人工处理", row["id"]),
                )
                unknown = conn.execute(
                    "SELECT * FROM notification_deliveries WHERE id=?", (row["id"],)
                ).fetchone()
                return False, dict(unknown)
            if row:
                conn.execute(
                    """UPDATE notification_deliveries SET status='sending', attempted_at=?,
                    http_status=NULL, response_excerpt=NULL, error=NULL WHERE id=?""",
                    (now, row["id"]),
                )
                claimed = conn.execute(
                    "SELECT * FROM notification_deliveries WHERE id=?", (row["id"],)
                ).fetchone()
            else:
                cursor = conn.execute(
                    """INSERT INTO notification_deliveries
                    (opportunity_id, channel, idempotency_key, status, attempted_at)
                    VALUES (?, 'feishu', ?, 'sending', ?)""",
                    (opportunity_id, idempotency_key, now),
                )
                claimed = conn.execute(
                    "SELECT * FROM notification_deliveries WHERE id=?", (cursor.lastrowid,)
                ).fetchone()
            return True, dict(claimed)

    @staticmethod
    def json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
