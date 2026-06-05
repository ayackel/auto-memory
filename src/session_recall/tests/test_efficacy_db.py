# src/session_recall/tests/test_efficacy_db.py
import threading

from session_recall.db import efficacy


def test_init_creates_all_tables(tmp_path):
    db = str(tmp_path / "eff.db")
    conn = efficacy.init(db)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"telemetry", "surfaced", "touched", "cursor", "capture_stat"} <= names
    assert conn.execute("PRAGMA user_version").fetchone()[0] == efficacy.SCHEMA_VERSION
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


def test_init_upgrades_old_schema_and_sets_user_version(tmp_path):
    db = str(tmp_path / "eff.db")
    conn = efficacy.connect(db)
    conn.executescript(
        """
        CREATE TABLE telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            ts TEXT NOT NULL,
            cmd TEXT,
            duration_ms INTEGER,
            busy_hits INTEGER DEFAULT 0,
            attempts INTEGER DEFAULT 1,
            rows_returned INTEGER DEFAULT 0,
            exit_code INTEGER DEFAULT 0,
            schema_ok INTEGER DEFAULT 1
        );
        """
    )
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()

    conn = efficacy.init(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(telemetry)").fetchall()}
    assert {"tier", "query_hash", "session_id_prefix", "window_tier"} <= cols
    assert conn.execute("PRAGMA user_version").fetchone()[0] == efficacy.SCHEMA_VERSION
    conn.close()


def test_init_concurrent_upgrade_from_v0_schema(tmp_path):
    db = str(tmp_path / "eff.db")
    conn = efficacy.connect(db)
    conn.executescript(
        """
        CREATE TABLE telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            ts TEXT NOT NULL,
            cmd TEXT,
            duration_ms INTEGER,
            busy_hits INTEGER DEFAULT 0,
            attempts INTEGER DEFAULT 1,
            rows_returned INTEGER DEFAULT 0,
            exit_code INTEGER DEFAULT 0,
            schema_ok INTEGER DEFAULT 1
        );
        """
    )
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()

    threads = []
    errors = []
    started = threading.Barrier(8)

    def worker() -> None:
        conn = None
        try:
            started.wait()
            conn = efficacy.init(db)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(telemetry)").fetchall()}
            assert {"tier", "query_hash", "session_id_prefix", "window_tier"} <= cols
            assert conn.execute("PRAGMA user_version").fetchone()[0] == efficacy.SCHEMA_VERSION
        except Exception as exc:  # pragma: no cover - asserted by test
            errors.append(exc)
        finally:
            if conn is not None:
                conn.close()

    for _ in range(8):
        thread = threading.Thread(target=worker)
        threads.append(thread)
        thread.start()

    for thread in threads:
        thread.join()

    assert not errors
