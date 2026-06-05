# src/session_recall/commands/doctor.py
"""Quick telemetry/store health (adapted from theidledeveloper/auto-memory, MIT)."""
from __future__ import annotations

from ..db import efficacy
from ..config import EFFICACY_DB_PATH
from ..util import telemetry
from ..util.format_output import output


def run(args, db_path: str | None = None) -> int:
    target = db_path or EFFICACY_DB_PATH
    entries = telemetry.load_entries(limit=500, db_path=target)
    rows = len(entries)
    errors = sum(1 for e in entries if int(e.get("exit_code") or 0) != 0)
    busy = sum(int(e.get("busy_hits") or 0) for e in entries)
    store_health = efficacy.check_store_health(target)
    status = "ok"
    if rows == 0:
        status = "info"
    elif errors / rows > 0.2 or busy / rows > 0.5:
        status = "warn"
    if not store_health["ok"]:
        status = "warn"
    output({"status": status, "telemetry_rows": rows,
            "error_rows": errors, "busy_hits": busy, "store_health": store_health},
           json_mode=getattr(args, "json", False))
    return 0
