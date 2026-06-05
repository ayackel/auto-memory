import pytest

from session_recall.db import efficacy
from session_recall.health import dim_disclosure
from session_recall.util import telemetry


@pytest.fixture
def telemetry_db(tmp_path):
    db_path = str(tmp_path / "eff.db")
    efficacy.init(db_path).close()
    telemetry.init(db_path)
    yield db_path
    telemetry.init(None)


def test_dim_disclosure_reads_sqlite_telemetry_with_schema_counts(telemetry_db):
    for i in range(210):
        telemetry.record("list", duration_ms=10 + i, tier=1)
    for i in range(20):
        telemetry.record("health", duration_ms=10 + i, tier=0)
    for i in range(10):
        telemetry.record("list", duration_ms=10 + i)  # no tier => unknown entry

    result = dim_disclosure.check()

    assert result["zone"] == "CALIBRATING"
    assert result["score"] is None
    assert result["scored_entries"] == 210
    assert result["meta_entries"] == 20
    assert result["unknown_entries"] == 10
    assert "n=210" in result["detail"]
    assert "unknown=10" in result["detail"]


def test_dim_disclosure_flags_mixed_schema_when_unknown_majority(telemetry_db):
    for i in range(200):
        telemetry.record("search", duration_ms=20 + i, tier=2)
    for i in range(201):
        telemetry.record("search", duration_ms=20 + i)  # unknown

    result = dim_disclosure.check()

    assert result["zone"] == "CALIBRATING"
    assert result["score"] is None
    assert result["scored_entries"] == 200
    assert result["unknown_entries"] == 201
    assert "legacy entries draining" in result["detail"]
    assert result["hint"] == "mixed-schema telemetry — waiting for legacy entries to drain"
