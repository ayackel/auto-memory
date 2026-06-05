"""Tests for util/telemetry.py — SQLite-backed writer + compat loader."""
import pytest
from session_recall.util import telemetry
from session_recall.db import efficacy


@pytest.fixture
def tmp_db(tmp_path):
    db = str(tmp_path / "eff.db")
    efficacy.init(db).close()  # create schema once
    telemetry.init(db)
    yield db
    telemetry.init(None)


def test_query_hash_normalizes():
    qh = telemetry.query_hash("hello world")
    assert len(qh) == 8
    assert qh == telemetry.query_hash("HELLO   WORLD")


def test_record_writes_row(tmp_db):
    telemetry.record("list", 42, tier=1, session_id="sess-1")
    rows = telemetry.load_entries()
    assert rows[-1]["cmd"] == "list"
    assert rows[-1]["tier"] == 1


def test_record_omits_null_optionals_in_loader(tmp_db):
    """Compat loader must omit NULL optional fields so `'tier' not in e` works."""
    telemetry.record("list", 10)  # no tier
    e = telemetry.load_entries()[-1]
    assert "tier" not in e
    assert "query_hash" not in e
    assert "session_id_prefix" not in e


def test_record_search_query_hash(tmp_db):
    qh = telemetry.query_hash("auth bug")
    telemetry.record("search", 12, tier=2, query_hash=qh)
    assert telemetry.load_entries()[-1]["query_hash"] == qh


def test_record_show_session_prefix(tmp_db):
    telemetry.record("show", 12, tier=3, session_id_prefix="abcd1234")
    assert telemetry.load_entries()[-1]["session_id_prefix"] == "abcd1234"


def test_load_entries_limit_and_order(tmp_db):
    for i in range(10):
        telemetry.record("list", i)
    rows = telemetry.load_entries(limit=3)
    assert len(rows) == 3
    assert [r["duration_ms"] for r in rows] == [7, 8, 9]  # chronological tail


def test_record_silent_when_uninitialized():
    telemetry.init(None)
    telemetry.record("list", 1)  # must not raise
