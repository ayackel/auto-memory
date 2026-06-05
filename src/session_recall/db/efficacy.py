"""Sidecar SQLite store for telemetry + recall-efficacy data (WAL mode)."""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import EFFICACY_DB_PATH

SCHEMA_VERSION = 1
RW_BUSY_TIMEOUT_MS = 250
SCHEMA_LOCK_RETRY_DELAYS_MS = (0, 40, 80, 160, 320)

_SCHEMA_TABLES = (
    """
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
)
""",
    """
CREATE TABLE IF NOT EXISTS surfaced (
    session_id TEXT NOT NULL,
    key TEXT NOT NULL,
    kind TEXT NOT NULL,
    cmd TEXT,
    first_ts TEXT NOT NULL,
    turn INTEGER NOT NULL,
    PRIMARY KEY (session_id, key, kind)
)
""",
    """
CREATE TABLE IF NOT EXISTS touched (
    session_id TEXT NOT NULL,
    key TEXT NOT NULL,
    ts TEXT NOT NULL,
    turn INTEGER NOT NULL,
    PRIMARY KEY (session_id, key)
)
""",
    """
CREATE TABLE IF NOT EXISTS cursor (
    session_id TEXT PRIMARY KEY,
    last_turn_seen INTEGER NOT NULL DEFAULT -1,
    last_prune_ts TEXT
)
""",
    """
CREATE TABLE IF NOT EXISTS capture_stat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    ts TEXT NOT NULL,
    status TEXT NOT NULL,
    surfaced_n INTEGER DEFAULT 0,
    touched_n INTEGER DEFAULT 0,
    ms INTEGER DEFAULT 0
)
""",
)

_SCHEMA_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_surfaced_kind_ts ON surfaced(kind, first_ts)",
    "CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON telemetry(ts)",
    "CREATE INDEX IF NOT EXISTS idx_telemetry_cmd ON telemetry(cmd, session_id_prefix)",
)


def now_iso() -> str:
    """UTC timestamp with microseconds — sortable as TEXT, == chronological order."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """Open a read-write WAL connection. Assumes schema already initialized."""
    path = Path(db_path or EFFICACY_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=RW_BUSY_TIMEOUT_MS / 1000.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {RW_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    last_busy_error = None
    for delay_ms in SCHEMA_LOCK_RETRY_DELAYS_MS:
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)
        try:
            _ensure_schema_once(conn)
            return
        except sqlite3.OperationalError as exc:
            if not _is_busy_error(exc):
                raise
            last_busy_error = exc
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
    if last_busy_error is not None:
        raise last_busy_error


def _ensure_schema_once(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        current_version = conn.execute("PRAGMA user_version").fetchone()[0]
        if current_version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Unsupported efficacy sidecar schema version: {current_version} > {SCHEMA_VERSION}"
            )
        should_bump_version = current_version != SCHEMA_VERSION

        for statement in _SCHEMA_TABLES:
            conn.execute(statement)

        if current_version < 1:
            _migrate_to_v1(conn)
            current_version = 1

        for statement in _SCHEMA_INDEXES:
            conn.execute(statement)

        if should_bump_version:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _is_busy_error(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def init(db_path: str | None = None) -> sqlite3.Connection:
    """Connect AND ensure schema. Call once per process (or in tests)."""
    conn = connect(db_path)
    ensure_schema(conn)
    return conn


def _migrate_to_v1(conn: sqlite3.Connection) -> None:
    _add_column_if_missing(conn, "telemetry", "tier INTEGER")
    _add_column_if_missing(conn, "telemetry", "query_hash TEXT")
    _add_column_if_missing(conn, "telemetry", "session_id_prefix TEXT")
    _add_column_if_missing(conn, "telemetry", "window_tier TEXT")


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column_ddl: str) -> None:
    column_name = column_ddl.split()[0]
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column_name not in cols:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_ddl}")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if column_name not in cols:
                raise


def _parse_now(now: str | None) -> datetime:
    if not now:
        return datetime.now(timezone.utc)
    raw = now
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _cutoff(retention_days: int, now: str | None = None) -> str:
    cutoff_dt = _parse_now(now) - timedelta(days=retention_days)
    return cutoff_dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def prune_efficacy(conn: sqlite3.Connection, retention_days: int, now: str | None = None) -> int:
    """Prune efficacy tables with coherent surfaced/touched semantics."""
    cutoff = _cutoff(retention_days, now=now)
    deleted = 0
    deleted += conn.execute("DELETE FROM surfaced WHERE first_ts < ?", (cutoff,)).rowcount
    deleted += conn.execute(
        "DELETE FROM touched "
        "WHERE ts < ? "
        "OR NOT EXISTS ("
        "SELECT 1 FROM surfaced s "
        "WHERE s.session_id=touched.session_id AND s.key=touched.key"
        ")",
        (cutoff,),
    ).rowcount
    deleted += conn.execute("DELETE FROM capture_stat WHERE ts < ?", (cutoff,)).rowcount
    conn.commit()
    return deleted


def prune_telemetry(conn: sqlite3.Connection, retention_days: int, now: str | None = None) -> int:
    """Prune telemetry rows independently from efficacy retention policy."""
    cutoff = _cutoff(retention_days, now=now)
    deleted = conn.execute("DELETE FROM telemetry WHERE ts < ?", (cutoff,)).rowcount
    conn.commit()
    return deleted


def vacuum(conn: sqlite3.Connection) -> None:
    conn.execute("VACUUM")


def prune(
    conn: sqlite3.Connection,
    retention_days: int,
    now: str | None = None,
    telemetry_retention_days: int | None = None,
) -> int:
    """Backward-compatible prune for both efficacy and telemetry retention windows."""
    telemetry_days = retention_days if telemetry_retention_days is None else telemetry_retention_days
    deleted = prune_efficacy(conn, retention_days, now=now)
    deleted += prune_telemetry(conn, telemetry_days, now=now)
    vacuum(conn)
    return deleted
