# src/session_recall/commands/prune.py
"""Manually prune efficacy/telemetry data with independent retention windows."""
from __future__ import annotations

from ..config import EFFICACY_DB_PATH, EFFICACY_RETENTION_DAYS, TELEMETRY_RETENTION_DAYS
from ..db import efficacy
from ..util.format_output import output


def run(args, db_path: str | None = None) -> int:
    conn = efficacy.init(db_path or EFFICACY_DB_PATH)
    try:
        now = getattr(args, "now", None)
        efficacy_deleted = efficacy.prune_efficacy(conn, EFFICACY_RETENTION_DAYS, now=now)
        telemetry_deleted = efficacy.prune_telemetry(conn, TELEMETRY_RETENTION_DAYS, now=now)
        efficacy.vacuum(conn)
        output(
            {
                "deleted": efficacy_deleted + telemetry_deleted,
                "efficacy_deleted": efficacy_deleted,
                "telemetry_deleted": telemetry_deleted,
                "efficacy_retention_days": EFFICACY_RETENTION_DAYS,
                "telemetry_retention_days": TELEMETRY_RETENTION_DAYS,
            },
            json_mode=getattr(args, "json", False),
        )
        return 0
    finally:
        conn.close()
