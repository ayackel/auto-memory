# src/session_recall/commands/prune.py
"""Manually prune efficacy data older than the retention window."""
from __future__ import annotations

from ..config import EFFICACY_DB_PATH, RETENTION_DAYS
from ..db import efficacy
from ..util.format_output import output


def run(args, db_path: str | None = None) -> int:
    conn = efficacy.init(db_path or EFFICACY_DB_PATH)
    try:
        deleted = efficacy.prune(conn, RETENTION_DAYS)
        output({"deleted": deleted, "retention_days": RETENTION_DAYS},
               json_mode=getattr(args, "json", False))
        return 0
    finally:
        conn.close()
