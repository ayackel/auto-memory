from session_recall.util.timestamps import normalize_timestamp, parse_timestamp


def test_parse_timestamp_accepts_iso_precision_variants():
    assert normalize_timestamp("2026-01-01T00:00:00Z") == "2026-01-01T00:00:00.000000Z"
    assert normalize_timestamp("2026-01-01T00:00:00.123Z") == "2026-01-01T00:00:00.123000Z"
    assert normalize_timestamp("2026-01-01T00:00:00.123456Z") == "2026-01-01T00:00:00.123456Z"


def test_parse_timestamp_accepts_epoch_seconds_millis_and_micros():
    expected = "2025-01-01T00:00:00.000000Z"
    assert normalize_timestamp("1735689600") == expected
    assert normalize_timestamp("1735689600000") == expected
    assert normalize_timestamp("1735689600000000") == expected
    assert parse_timestamp("bad-input") is None
