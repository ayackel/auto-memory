"""Central, time-boxed, silent-fail capture for recall-efficacy measurement.

Called once per CLI invocation AFTER output has been flushed. Records what the
command surfaced and snapshots newly-used artifacts since a closed-turn watermark.
"""
from __future__ import annotations

import os
import subprocess
import time

from .. import config
from ..db.connect import connect_ro
from ..db import efficacy
from . import recall_key


def _current_root() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=1, cwd=os.getcwd(),
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except Exception:
        pass
    return None


def _closed_turn(ro, session_id: str) -> int:
    row = ro.execute(
        "SELECT MAX(turn_index) AS m FROM turns "
        "WHERE session_id=? AND assistant_response IS NOT NULL AND assistant_response<>''",
        (session_id,),
    ).fetchone()
    m = row["m"] if row is not None else None
    return m if m is not None else -1


def run(args) -> None:
    """Entry point. Never raises; bails silently on budget/errors."""
    if getattr(config, "NO_CAPTURE", False):
        return
    session_id = getattr(config, "AGENT_SESSION_ID", None)
    if not session_id:
        return

    t0 = time.monotonic()
    deadline = t0 + config.CAPTURE_BUDGET_MS / 1000.0
    status = "completed"
    surfaced_n = 0
    touched_n = 0
    conn = None
    ro = None
    try:
        conn = efficacy.connect(config.EFFICACY_DB_PATH)
        ro = connect_ro(config.DB_PATH)
        closed = _closed_turn(ro, session_id)
        ts = efficacy.now_iso()
        root = _current_root()

        cap = getattr(args, "_capture", None) or {}

        # 1) Record surfaced artifacts (deduped; earliest kept by INSERT OR IGNORE).
        for path in cap.get("files", []) or []:
            if time.monotonic() > deadline:
                status = "timeout"
                break
            key = recall_key.file_key(path, current_root=root)
            conn.execute(
                "INSERT OR IGNORE INTO surfaced(session_id,key,kind,cmd,first_ts,turn) "
                "VALUES(?,?,?,?,?,?)",
                (session_id, key, "file", getattr(args, "command", None), ts, closed),
            )
            surfaced_n += 1
        for sid in cap.get("sessions", []) or []:
            key = recall_key.session_key(sid)
            if not key:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO surfaced(session_id,key,kind,cmd,first_ts,turn) "
                "VALUES(?,?,?,?,?,?)",
                (session_id, key, "session", getattr(args, "command", None), ts, closed),
            )
            surfaced_n += 1

        # 2) Snapshot newly-used files since the watermark, up to the closed frontier.
        if status != "timeout":
            cur = conn.execute(
                "SELECT last_turn_seen FROM cursor WHERE session_id=?", (session_id,)
            ).fetchone()
            last_seen = cur["last_turn_seen"] if cur else -1
            used = ro.execute(
                "SELECT file_path, turn_index FROM session_files "
                "WHERE session_id=? AND turn_index>? AND turn_index<=?",
                (session_id, last_seen, closed),
            ).fetchall()
            for r in used:
                if time.monotonic() > deadline:
                    status = "timeout"
                    break
                key = recall_key.file_key(r["file_path"], current_root=root)
                s = conn.execute(
                    "SELECT turn FROM surfaced "
                    "WHERE session_id=? AND key=? AND kind='file'",
                    (session_id, key),
                ).fetchone()
                if s is not None and s["turn"] < r["turn_index"]:
                    conn.execute(
                        "INSERT OR IGNORE INTO touched(session_id,key,ts,turn) "
                        "VALUES(?,?,?,?)",
                        (session_id, key, ts, r["turn_index"]),
                    )
                    touched_n += 1

            # 3) Advance the watermark — forward only.
            conn.execute(
                "INSERT INTO cursor(session_id,last_turn_seen) VALUES(?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET last_turn_seen=excluded.last_turn_seen "
                "WHERE excluded.last_turn_seen>cursor.last_turn_seen",
                (session_id, closed),
            )

        # 4) Lazy retention (once/day).
        _maybe_prune(conn, session_id, ts)

        ms = int((time.monotonic() - t0) * 1000)
        conn.execute(
            "INSERT INTO capture_stat(session_id,ts,status,surfaced_n,touched_n,ms) "
            "VALUES(?,?,?,?,?,?)",
            (session_id, ts, status, surfaced_n, touched_n, ms),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        try:
            if ro is not None:
                ro.close()
        except Exception:
            pass
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _maybe_prune(conn, session_id: str, ts: str) -> None:
    try:
        row = conn.execute(
            "SELECT last_prune_ts FROM cursor WHERE session_id=?", (session_id,)
        ).fetchone()
        last = row["last_prune_ts"] if row else None
        if last and last[:10] == ts[:10]:
            return  # already pruned today (compare YYYY-MM-DD)
        efficacy.prune(conn, config.RETENTION_DAYS, now=ts)
        conn.execute(
            "INSERT INTO cursor(session_id,last_turn_seen,last_prune_ts) VALUES(?,-1,?) "
            "ON CONFLICT(session_id) DO UPDATE SET last_prune_ts=excluded.last_prune_ts",
            (session_id, ts),
        )
    except Exception:
        pass
