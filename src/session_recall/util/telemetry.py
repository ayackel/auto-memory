"""Telemetry writer + reader — SQLite-backed (sidecar efficacy DB)."""
import hashlib

from ..db import efficacy

_DB_PATH = None

_OPTIONAL = ("tier", "query_hash", "session_id_prefix", "window_tier")


def query_hash(q: str) -> str:
    """8-char sha256 of whitespace-normalized, lowercased query. Not reversible."""
    normalized = " ".join(q.lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:8]


def init(db_path) -> None:
    global _DB_PATH
    _DB_PATH = db_path


def _resolve_db_path(db_path: str | None = None) -> str | None:
    if db_path is not None:
       return db_path
    return _DB_PATH


def record(cmd: str, duration_ms: int, busy_hits: int = 0, attempts: int = 1,
        rows: int = 0, exit_code: int = 0, schema_ok: bool = True,
         tier: int | None = None, query_hash: str | None = None,
         session_id_prefix: str | None = None, window_tier: str | None = None,
         session_id: str | None = None, db_path: str | None = None) -> None:
    """Append a telemetry row. Silent fail — telemetry must never crash the CLI."""
    target_db_path = _resolve_db_path(db_path)
    if not target_db_path:
       return
    try:
       conn = efficacy.connect(target_db_path)
       try:
           conn.execute(
             "INSERT INTO telemetry (session_id, ts, cmd, duration_ms, busy_hits, "
             "attempts, rows_returned, exit_code, schema_ok, tier, query_hash, "
             "session_id_prefix, window_tier) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
             (session_id, efficacy.now_iso(), cmd, duration_ms, busy_hits, attempts,
              rows, exit_code, 1 if schema_ok else 0, tier, query_hash,
              session_id_prefix, window_tier),
           )
           conn.commit()
       finally:
           conn.close()
    except Exception:
       pass


def load_entries(limit: int = 500, db_path: str | None = None) -> list[dict]:
    """Return up to `limit` most-recent rows as JSON-shaped dicts (chronological).

    Optional fields that are NULL are OMITTED, preserving the legacy JSON shape so
    consumers using `'tier' not in entry` keep working unchanged.
    """
    target_db_path = _resolve_db_path(db_path)
    if not target_db_path:
       return []
    try:
       conn = efficacy.connect(target_db_path)
       try:
           rows = conn.execute(
             "SELECT * FROM telemetry ORDER BY id DESC LIMIT ?", (limit,)
           ).fetchall()
       finally:
           conn.close()
    except Exception:
       return []
    out = []
    for r in reversed(rows):  # chronological
       d = {
           "ts": r["ts"], "cmd": r["cmd"], "duration_ms": r["duration_ms"],
           "busy_hits": r["busy_hits"], "attempts": r["attempts"],
           "rows_returned": r["rows_returned"], "exit_code": r["exit_code"],
           "schema_ok": bool(r["schema_ok"]),
       }
       for k in _OPTIONAL:
           if r[k] is not None:
             d[k] = r[k]
       out.append(d)
    return out
