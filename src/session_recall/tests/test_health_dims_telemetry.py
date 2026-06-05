"""Tests for health/dim_disclosure.py — telemetry compat loader adaptation.

This test verifies that health dimensions can read from the SQLite-backed telemetry
through a compat loader, instead of only reading from a JSON file.
"""
import json
import pytest
from pathlib import Path
from session_recall.health import dim_disclosure
from session_recall.util import telemetry
from session_recall.db import efficacy
from session_recall.config import TELEMETRY_PATH


@pytest.fixture
def tmp_telemetry_db(tmp_path):
    """Setup SQLite telemetry DB and compat JSON file."""
    db_path = str(tmp_path / "test_eff.db")
    json_path = str(tmp_path / "telemetry.json")
    
    # Initialize SQLite
    efficacy.init(db_path).close()
    telemetry.init(db_path)
    
    yield db_path, json_path
    telemetry.init(None)


def test_dims_read_json_not_sqlite(tmp_telemetry_db):
    """Without compat loader, dims read JSON. SQLite telemetry makes JSON empty.
    
    This is the FAILING test — it documents the current broken behavior:
    - Health dims call _load_entries() which reads TELEMETRY_PATH (JSON file)
    - We write telemetry to SQLite via telemetry.record()
    - JSON file is empty/nonexistent
    - dims.check() returns CALIBRATING because it sees no entries
    - After adaptation, dims should read from SQLite via compat loader
    
    EXPECTED FAILURE: This test documents that dim_disclosure is broken.
    It should fail because:
    1. We write 300 scored entries to SQLite
    2. dim_disclosure still reads from JSON (not SQLite)
    3. JSON is empty, so dim_disclosure sees 0 entries
    4. We EXPECT dims to see 300 entries from SQLite (via compat loader)
    5. But it doesn't yet, so this test FAILS
    """
    db_path, json_path = tmp_telemetry_db
    
    # Write entries to SQLite telemetry (the new way)
    for i in range(300):
        telemetry.record(
            "search",
            duration_ms=100 + i,
            tier=1 if i % 3 == 0 else 2 if i % 3 == 1 else 3,
            query_hash="abcd1234",
            session_id_prefix="test_"
        )
    
    # Verify SQLite has entries
    entries_from_sqlite = telemetry.load_entries()
    assert len(entries_from_sqlite) >= 300, "SQLite telemetry should have 300+ entries"
    
    # JSON file is empty (dims still read JSON, not SQLite) — THIS IS THE PROBLEM
    json_file = Path(json_path)
    assert not json_file.exists(), "JSON file should not exist (SQLite replaces it)"
    
    # When dims try to read, they currently get nothing because dims read JSON, not SQLite
    result = dim_disclosure.check()
    
    # FAILING ASSERTION: We expect dims to have adapted to read from SQLite via compat loader
    # But they haven't yet, so this will FAIL
    assert result["scored_entries"] > 0, (
        "FAILING: dims should have read 300+ entries from SQLite via compat loader, "
        "but currently dims only read JSON which is empty"
    )


def test_compat_loader_populates_json_from_sqlite(tmp_telemetry_db):
    """After compat loader is implemented, JSON should be populated from SQLite entries.
    
    This test will PASS after Han adapts dim_disclosure to use the compat loader
    to read from SQLite instead of JSON.
    """
    db_path, json_path = tmp_telemetry_db
    
    # Write entries to SQLite telemetry
    for i in range(250):
        telemetry.record(
            "list",
            duration_ms=50 + i,
            tier=2,
            session_id="sess-1"
        )
    
    # Get entries from SQLite (simulating what compat loader would do)
    entries_from_sqlite = telemetry.load_entries()
    assert len(entries_from_sqlite) >= 250
    
    # Manually write to JSON to simulate compat loader (for now, this is the failing case)
    json_data = {"entries": entries_from_sqlite}
    Path(json_path).write_text(json.dumps(json_data))
    
    # Now dims should read the JSON and return a proper result
    result = dim_disclosure.check()
    
    # After adaptation, this should NOT be CALIBRATING
    # (For now it might be CALIBRATING because SCORING_ACTIVE=False)
    # But it should have the actual entries, not be empty
    assert result["scored_entries"] == 250, "Dims should see all 250 scored entries from compat JSON"
    assert result["unknown_entries"] == 0
