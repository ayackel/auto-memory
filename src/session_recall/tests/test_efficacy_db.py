# src/session_recall/tests/test_efficacy_db.py
from session_recall.db import efficacy


def test_init_creates_all_tables(tmp_path):
    db = str(tmp_path / "eff.db")
    conn = efficacy.init(db)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"telemetry", "surfaced", "touched", "cursor", "capture_stat"} <= names
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    conn.close()


def test_now_iso_has_microseconds_and_sorts(tmp_path):
    a = efficacy.now_iso()
    b = efficacy.now_iso()
    assert a.endswith("Z") and "." in a
    assert a <= b  # lexicographic == chronological


def test_surfaced_dedup_keeps_earliest(tmp_path):
    conn = efficacy.init(str(tmp_path / "eff.db"))
    conn.execute("INSERT OR IGNORE INTO surfaced VALUES('s','k','file','files','2026-01-01T00:00:00.000000Z',3)")
    conn.execute("INSERT OR IGNORE INTO surfaced VALUES('s','k','file','files','2026-02-01T00:00:00.000000Z',9)")
    conn.commit()
    row = conn.execute("SELECT first_ts, turn FROM surfaced WHERE session_id='s' AND key='k' AND kind='file'").fetchone()
    assert row["turn"] == 3  # earliest retained
    conn.close()


def test_prune_deletes_old_rows(tmp_path):
    conn = efficacy.init(str(tmp_path / "eff.db"))
    conn.execute("INSERT INTO surfaced VALUES('old','k1','file','files','2020-01-01T00:00:00.000000Z',1)")
    conn.execute("INSERT INTO surfaced VALUES('new','k2','file','files',?,1)", (efficacy.now_iso(),))
    conn.commit()
    deleted = efficacy.prune(conn, retention_days=90)
    keys = {r[0] for r in conn.execute("SELECT key FROM surfaced").fetchall()}
    assert keys == {"k2"}
    assert deleted >= 1
    conn.close()
