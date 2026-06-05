"""Timestamp parsing + normalization helpers used across telemetry paths."""

from __future__ import annotations

from datetime import datetime, timezone


def _parse_epoch(raw: str) -> datetime | None:
    sign = -1 if raw.startswith("-") else 1
    digits = raw[1:] if sign < 0 else raw
    if not digits.isdigit():
        return None
    value = sign * int(digits)
    abs_value = abs(value)

    if abs_value >= 1_000_000_000_000_000:  # microseconds
        seconds = value / 1_000_000
    elif abs_value >= 1_000_000_000_000:  # milliseconds
        seconds = value / 1_000
    elif abs_value >= 1_000_000_000:  # seconds
        seconds = float(value)
    else:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def parse_timestamp(value: object) -> datetime | None:
    """Parse ISO8601 or epoch (seconds/millis/micros) into UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        parsed = _parse_epoch(str(int(value)))
        if parsed is None:
            return None
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        parsed = _parse_epoch(raw)
        if parsed is None:
            if raw.endswith("Z"):
                raw = f"{raw[:-1]}+00:00"
            try:
                parsed = datetime.fromisoformat(raw)
            except ValueError:
                return None
    else:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_timestamp_utc(dt: datetime) -> str:
    """Emit canonical UTC ISO8601 with microseconds and trailing Z."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def normalize_timestamp(value: object) -> str | None:
    parsed = parse_timestamp(value)
    if parsed is None:
        return None
    return format_timestamp_utc(parsed)
