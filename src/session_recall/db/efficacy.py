"""Sidecar SQLite store for telemetry + recall-efficacy data (WAL mode)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import EFFICACY_DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    ts TEXT NOT NULL,
    cmd TEXT,
    duration_ms INTEGER,
    busy_hits INTEGER DEFAULT 0,
    attempts INTEGER DEFAULT 1,
    rows_returned INTEGER DEFAULT 0,
    exit_code INTEGER DEFAULT 0,
    schema_ok INTEGER DEFAULT 1,
    tier INTEGER,
    query_hash TEXT,
    session_id_prefix TEXT,
    window_tier TEXT
);
CREATE TABLE IF NOT EXISTS surfaced (
    session_id TEXT NOT NULL,
    key TEXT NOT NULL,
    kind TEXT NOT NULL,
    cmd TEXT,
    first_ts TEXT NOT NULL,
    turn INTEGER NOT NULL,
    PRIMARY KEY (session_id, key, kind)
);
CREATE TABLE IF NOT EXISTS touched (
    session_id TEXT NOT NULL,
    key TEXT NOT NULL,
    ts TEXT NOT NULL,
    turn INTEGER NOT NULL,
    PRIMARY KEY (session_id, key)
);
CREATE TABLE IF NOT EXISTS cursor (
    session_id TEXT PRIMARY KEY,
    last_turn_seen INTEGER NOT NULL DEFAULT -1,
    last_prune_ts TEXT
);
CREATE TABLE IF NOT EXISTS capture_stat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    ts TEXT NOT NULL,
    status TEXT NOT NULL,
    surfaced_n INTEGER DEFAULT 0,
    touched_n INTEGER DEFAULT 0,
    ms INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_surfaced_kind_ts ON surfaced(kind, first_ts);
CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON telemetry(ts);
CREATE INDEX IF NOT EXISTS idx_telemetry_cmd ON telemetry(cmd, session_id_prefix);
"""


def now_iso() -> str:
    """UTC timestamp with microseconds — sortable as TEXT, == chronological order."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """Open a read-write WAL connection. Assumes schema already initialized."""
    path = Path(db_path or EFFICACY_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=1.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 1000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def init(db_path: str | None = None) -> sqlite3.Connection:
    """Connect AND ensure schema. Call once per process (or in tests)."""
    conn = connect(db_path)
    ensure_schema(conn)
    return conn


def prune(conn: sqlite3.Connection, retention_days: int, now: str | None = None) -> int:
    """Delete rows older than retention_days (by row timestamp) and VACUUM. Returns rows deleted."""
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=retention_days)
    cutoff = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    deleted = 0
    deleted += conn.execute("DELETE FROM surfaced WHERE first_ts < ?", (cutoff,)).rowcount
    deleted += conn.execute("DELETE FROM touched WHERE ts < ?", (cutoff,)).rowcount
    deleted += conn.execute("DELETE FROM telemetry WHERE ts < ?", (cutoff,)).rowcount
    deleted += conn.execute("DELETE FROM capture_stat WHERE ts < ?", (cutoff,)).rowcount
    conn.commit()
    conn.execute("VACUUM")
    return deleted
