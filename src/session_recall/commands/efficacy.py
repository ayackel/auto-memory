# src/session_recall/commands/efficacy.py
"""Recall efficacy report — recalled-then-used rate (Touched + Escalated)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..config import EFFICACY_DB_PATH
from ..db import efficacy
from ..util.format_output import output

_ESCALATE_CMDS = ("show", "export", "diff")
_MIN_SURFACES = 1


def _cutoff(days: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _filters(args):
    clauses, params = [], []
    if getattr(args, "session", None):
        clauses.append("s.session_id = ?")
        params.append(args.session)
    return clauses, params


def _counts(conn, cutoff, args):
    fclauses, fparams = _filters(args)
    where = " AND ".join(["s.first_ts >= ?"] + fclauses)
    base = [cutoff] + fparams

    file_surfaces = conn.execute(
        f"SELECT COUNT(*) FROM surfaced s WHERE s.kind='file' AND {where}", base
    ).fetchone()[0]
    file_hits = conn.execute(
        f"SELECT COUNT(*) FROM surfaced s WHERE s.kind='file' AND {where} "
        "AND EXISTS(SELECT 1 FROM touched t WHERE t.session_id=s.session_id AND t.key=s.key)",
        base,
    ).fetchone()[0]
    sess_surfaces = conn.execute(
        f"SELECT COUNT(*) FROM surfaced s WHERE s.kind='session' AND {where}", base
    ).fetchone()[0]
    placeholders = ",".join("?" * len(_ESCALATE_CMDS))
    sess_hits = conn.execute(
        f"SELECT COUNT(*) FROM surfaced s WHERE s.kind='session' AND {where} "
        f"AND EXISTS(SELECT 1 FROM telemetry t WHERE t.cmd IN ({placeholders}) "
        "AND t.session_id_prefix=s.key AND t.ts > s.first_ts)",
        base + list(_ESCALATE_CMDS),
    ).fetchone()[0]
    return file_surfaces, file_hits, sess_surfaces, sess_hits


def _rate(hits, surfaces):
    return (hits / surfaces) if surfaces else 0.0


def _macro(conn, cutoff, args):
    rows = conn.execute(
        "SELECT DISTINCT session_id FROM surfaced WHERE first_ts >= ?", (cutoff,)
    ).fetchall()
    table = []
    for r in rows:
        sub = type(args)(**{**vars(args), "session": r["session_id"]})
        fs, fh, ss, sh = _counts(conn, cutoff, sub)
        surf = fs + ss
        if surf == 0:
            continue
        table.append({
            "session_id": r["session_id"],
            "surfaces": surf,
            "hits": fh + sh,
            "rate": _rate(fh + sh, surf),
        })
    table.sort(key=lambda x: (x["rate"], -x["surfaces"]))
    return table[:10]


def run(args, db_path: str | None = None) -> int:
    db = db_path or EFFICACY_DB_PATH
    conn = efficacy.init(db)
    try:
        cutoff = _cutoff(getattr(args, "days", 30))
        fs, fh, ss, sh = _counts(conn, cutoff, args)
        total = fs + ss
        cap = conn.execute(
            "SELECT COUNT(*) AS runs, "
            "SUM(CASE WHEN status='timeout' THEN 1 ELSE 0 END) AS timeouts "
            "FROM capture_stat WHERE ts >= ?", (cutoff,)
        ).fetchone()
        capture_health = {"runs": cap["runs"] or 0, "timeouts": cap["timeouts"] or 0}

        if total < _MIN_SURFACES:
            output({
                "status": "insufficient_data",
                "unique_surfaces": total,
                "needed": _MIN_SURFACES,
                "window_days": getattr(args, "days", 30),
                "capture_health": capture_health,
            }, json_mode=getattr(args, "json", False))
            return 0

        report = {
            "status": "ok",
            "window_days": getattr(args, "days", 30),
            "blended": {"rate": _rate(fh + sh, fs + ss), "hits": fh + sh, "surfaces": fs + ss},
            "file_recall": {"rate": _rate(fh, fs), "hits": fh, "surfaces": fs},
            "session_recall": {"rate": _rate(sh, ss), "hits": sh, "surfaces": ss},
            "per_session_worst": _macro(conn, cutoff, args),
            "capture_health": capture_health,
        }
        output(report, json_mode=getattr(args, "json", False))
        return 0
    finally:
        conn.close()
