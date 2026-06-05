# src/session_recall/commands/stats.py
"""Telemetry usage summary (adapted from theidledeveloper/auto-memory, MIT)."""
from __future__ import annotations

from collections import defaultdict

from ..config import EFFICACY_DB_PATH
from ..util import telemetry
from ..util.format_output import output


def run(args, db_path: str | None = None) -> int:
    entries = telemetry.load_entries(limit=500, db_path=db_path or EFFICACY_DB_PATH)
    agg = defaultdict(lambda: {"count": 0, "total_ms": 0})
    for e in entries:
        a = agg[e.get("cmd", "?")]
        a["count"] += 1
        a["total_ms"] += int(e.get("duration_ms") or 0)
    by_command = [
        {"cmd": cmd, "count": v["count"],
         "avg_ms": round(v["total_ms"] / v["count"], 1) if v["count"] else 0}
        for cmd, v in sorted(agg.items())
    ]
    output({"total": len(entries), "by_command": by_command},
           json_mode=getattr(args, "json", False))
    return 0
