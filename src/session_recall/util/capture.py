"""Central, time-boxed, silent-fail capture for recall-efficacy measurement.

Called once per CLI invocation AFTER output has been flushed. Records what the
command surfaced and snapshots newly-used artifacts since a closed-turn watermark.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime

from .. import config
from ..db.connect import connect_ro
from ..db import efficacy
from . import recall_key
from .timestamps import parse_timestamp

_ROOT_CACHE: dict[str, str | None] = {}
_SESSION_CTX_CACHE: dict[str, tuple[str, str | None, str | None]] = {}
_WARN_STATUSES = {"timeout", "failed"}
_DEGRADE_WINDOW_RUNS = 20
_DEGRADE_MIN_RUNS = 10
_DEGRADE_RATIO_WARN = 0.35
_WARN_COOLDOWN_SEC = 600


def _find_git_root(cwd: str) -> str | None:
    cur = os.path.normpath(os.path.abspath(cwd))
    while True:
        if os.path.isdir(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _cached_root(cwd: str) -> str | None:
    norm = os.path.normpath(os.path.abspath(cwd))
    if norm in _ROOT_CACHE:
        cached = _ROOT_CACHE[norm]
        if cached is None or os.path.isdir(os.path.join(cached, ".git")):
            return cached
        _ROOT_CACHE.pop(norm, None)
    root = _find_git_root(norm)
    if len(_ROOT_CACHE) >= 64:
        _ROOT_CACHE.clear()
    _ROOT_CACHE[norm] = root
    return root


def _session_context(ro, session_id: str) -> tuple[str | None, str | None]:
    cwd = os.getcwd()
    repo = None
    try:
        row = ro.execute(
            "SELECT cwd, repository FROM sessions WHERE id=?",
            (session_id,),
        ).fetchone()
        if row is not None:
            cwd = row["cwd"] or cwd
            repository = row["repository"] if "repository" in row.keys() else None
            if repository and "/" in repository and " " not in repository:
                repo = repository
    except Exception:
        pass
    cached = _SESSION_CTX_CACHE.get(session_id)
    if cached and cached[0] == cwd and cached[2] == repo:
        return cached[1], cached[2]

    root = _cached_root(cwd)
    if len(_SESSION_CTX_CACHE) >= 128:
        _SESSION_CTX_CACHE.clear()
    _SESSION_CTX_CACHE[session_id] = (cwd, root, repo)
    return root, repo


def _parse_iso(ts: str) -> datetime | None:
    return parse_timestamp(ts)


def _warn(msg: str) -> None:
    print(msg, file=sys.stderr)


def _capture_payload(args, capture_payload: dict | None = None) -> dict:
    if capture_payload is not None:
        return capture_payload
    payload = getattr(args, "capture", None)
    if payload is not None:
        return payload
    return getattr(args, "_capture", None) or {}


def _closed_turn(ro, session_id: str) -> int:
    row = ro.execute(
        "SELECT MAX(turn_index) AS m FROM turns "
        "WHERE session_id=? AND assistant_response IS NOT NULL AND assistant_response<>''",
        (session_id,),
    ).fetchone()
    m = row["m"] if row is not None else None
    return m if m is not None else -1


def _latest_session_id(ro) -> str | None:
    try:
        row = ro.execute(
            "SELECT id FROM sessions "
            "ORDER BY COALESCE(updated_at, created_at) DESC, id DESC LIMIT 1"
        ).fetchone()
    except Exception:
        row = ro.execute(
            "SELECT id FROM sessions ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    return row["id"] or None


def _validated_session_id(ro, configured_session_id: str | None) -> tuple[str | None, str | None]:
    if not configured_session_id:
        return None, "identity_missing"
    try:
        current = ro.execute(
            "SELECT id FROM sessions WHERE id=? LIMIT 1",
            (configured_session_id,),
        ).fetchone()
        if current is None:
            return None, "identity_unverifiable"
        live_session_id = _latest_session_id(ro)
        if not live_session_id:
            return None, "identity_unverifiable"
        if live_session_id != configured_session_id:
            return None, "identity_mismatch"
        return configured_session_id, None
    except Exception:
        return None, "identity_unverifiable"


def run(args, capture_payload: dict | None = None) -> None:
    """Entry point. Never raises; bails silently on budget/errors."""
    if getattr(config, "NO_CAPTURE", False):
        return
    session_id = getattr(config, "AGENT_SESSION_ID", None)

    t0 = time.monotonic()
    deadline = t0 + config.CAPTURE_BUDGET_MS / 1000.0
    status = "completed"
    ts = efficacy.now_iso()
    surfaced_n = 0
    touched_n = 0
    identity_status = "identity_missing" if not session_id else None
    conn = None
    ro = None
    try:
        conn = efficacy.connect(config.EFFICACY_DB_PATH)
        if identity_status is not None:
            status = identity_status
            return
        ro = connect_ro(config.DB_PATH)
        if time.monotonic() > deadline:
            status = "timeout"
            return
        session_id, identity_status = _validated_session_id(ro, session_id)
        if identity_status is not None:
            status = identity_status
            return
        if not session_id:
            status = "identity_unverifiable"
            return
        if time.monotonic() > deadline:
            status = "timeout"
            return
        closed = _closed_turn(ro, session_id)
        if time.monotonic() > deadline:
            status = "timeout"
            return
        root, repo_id = _session_context(ro, session_id)
        if time.monotonic() > deadline:
            status = "timeout"
            return

        cap = _capture_payload(args, capture_payload)
        if time.monotonic() > deadline:
            status = "timeout"
            return

        # 1) Record surfaced artifacts (deduped; earliest kept by INSERT OR IGNORE).
        for path in cap.get("files", []) or []:
            if time.monotonic() > deadline:
                status = "timeout"
                break
            key = recall_key.file_key(path, current_root=root, repo_id=repo_id)
            inserted = conn.execute(
                "INSERT OR IGNORE INTO surfaced(session_id,key,kind,cmd,first_ts,turn) "
                "VALUES(?,?,?,?,?,?)",
                (session_id, key, "file", getattr(args, "command", None), ts, closed),
            )
            if inserted.rowcount > 0:
                surfaced_n += 1
        if status != "timeout":
            for sid in cap.get("sessions", []) or []:
                if time.monotonic() > deadline:
                    status = "timeout"
                    break
                key = recall_key.session_key(sid)
                if not key:
                    continue
                inserted = conn.execute(
                    "INSERT OR IGNORE INTO surfaced(session_id,key,kind,cmd,first_ts,turn) "
                    "VALUES(?,?,?,?,?,?)",
                    (session_id, key, "session", getattr(args, "command", None), ts, closed),
                )
                if inserted.rowcount > 0:
                    surfaced_n += 1

        # 2) Snapshot newly-used files since the watermark, up to the closed frontier.
        if status != "timeout":
            if time.monotonic() > deadline:
                status = "timeout"
                return
            cur = conn.execute(
                "SELECT last_turn_seen FROM cursor WHERE session_id=?", (session_id,)
            ).fetchone()
            last_seen = cur["last_turn_seen"] if cur else -1
            if time.monotonic() > deadline:
                status = "timeout"
                return
            used = ro.execute(
                "SELECT file_path, turn_index FROM session_files "
                "WHERE session_id=? AND turn_index>? AND turn_index<=?",
                (session_id, last_seen, closed),
            ).fetchall()
            for r in used:
                if time.monotonic() > deadline:
                    status = "timeout"
                    break
                key = recall_key.file_key(r["file_path"], current_root=root, repo_id=repo_id)
                s = conn.execute(
                    "SELECT turn FROM surfaced "
                    "WHERE session_id=? AND key=? AND kind='file'",
                    (session_id, key),
                ).fetchone()
                if s is not None and s["turn"] < r["turn_index"]:
                    inserted = conn.execute(
                        "INSERT OR IGNORE INTO touched(session_id,key,ts,turn) "
                        "VALUES(?,?,?,?)",
                        (session_id, key, ts, r["turn_index"]),
                    )
                    if inserted.rowcount > 0:
                        touched_n += 1

            # 3) Advance the watermark — forward only.
            conn.execute(
                "INSERT INTO cursor(session_id,last_turn_seen) VALUES(?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET last_turn_seen=excluded.last_turn_seen "
                "WHERE excluded.last_turn_seen>cursor.last_turn_seen",
                (session_id, closed),
            )

    except Exception:
        status = "failed"
    finally:
        try:
            if conn is not None:
                ms = int((time.monotonic() - t0) * 1000)
                conn.execute(
                    "INSERT INTO capture_stat(session_id,ts,status,surfaced_n,touched_n,ms) "
                    "VALUES(?,?,?,?,?,?)",
                    (session_id, ts, status, surfaced_n, touched_n, ms),
                )
                _maybe_warn_degradation(conn, session_id, ts, status)
                conn.commit()
        except Exception:
            pass
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


def _maybe_warn_degradation(conn, session_id: str, ts: str, status: str) -> None:
    if status not in _WARN_STATUSES:
        return
    rows = conn.execute(
        "SELECT status FROM capture_stat WHERE session_id=? ORDER BY id DESC LIMIT ?",
        (session_id, _DEGRADE_WINDOW_RUNS),
    ).fetchall()
    runs = len(rows)
    if runs < _DEGRADE_MIN_RUNS:
        return
    degraded = sum(1 for r in rows if r["status"] in _WARN_STATUSES)
    ratio = degraded / runs if runs else 0.0
    if ratio < _DEGRADE_RATIO_WARN:
        return

    warn_key = f"__capture_warn__:{session_id}"
    last_row = conn.execute(
        "SELECT last_prune_ts FROM cursor WHERE session_id=?",
        (warn_key,),
    ).fetchone()
    last_warn = _parse_iso(last_row["last_prune_ts"]) if last_row and last_row["last_prune_ts"] else None
    now = _parse_iso(ts)
    if last_warn and now and (now - last_warn).total_seconds() < _WARN_COOLDOWN_SEC:
        return

    _warn(
        f"warning: capture degraded for {session_id[:8]} "
        f"({degraded}/{runs} recent runs timeout/failed)"
    )
    conn.execute(
        "INSERT INTO cursor(session_id,last_turn_seen,last_prune_ts) VALUES(?,-1,?) "
        "ON CONFLICT(session_id) DO UPDATE SET last_prune_ts=excluded.last_prune_ts",
        (warn_key, ts),
    )
