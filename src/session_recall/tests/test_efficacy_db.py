# src/session_recall/tests/test_efficacy_db.py
import threading
import time

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
    conn.execute("INSERT INTO telemetry(session_id,ts,cmd) VALUES('old', '2020-01-01T00:00:00.000000Z', 'list')")
    conn.commit()
    deleted = efficacy.prune(conn, retention_days=90, now="2020-04-01T00:00:00.000000Z")
    keys = {r[0] for r in conn.execute("SELECT key FROM surfaced").fetchall()}
    assert keys == {"k2"}
    assert deleted >= 1
    conn.close()


def test_prune_now_is_deterministic_and_coherent(tmp_path):
    conn = efficacy.init(str(tmp_path / "eff.db"))
    conn.execute(
        "INSERT INTO surfaced VALUES('s','old','file','files','2026-01-01T00:00:00.000000Z',1)"
    )
    conn.execute(
        "INSERT INTO surfaced VALUES('s','new','file','files','2026-03-05T00:00:00.000000Z',2)"
    )
    conn.execute("INSERT INTO touched VALUES('s','old','2026-03-10T00:00:00.000000Z',3)")
    conn.execute("INSERT INTO touched VALUES('s','new','2026-03-10T00:00:00.000000Z',3)")
    conn.commit()

    deleted = efficacy.prune_efficacy(conn, retention_days=30, now="2026-03-15T00:00:00.000000Z")
    remaining_keys = {r[0] for r in conn.execute("SELECT key FROM surfaced").fetchall()}
    remaining_touched = {r[0] for r in conn.execute("SELECT key FROM touched").fetchall()}

    assert deleted == 2
    assert remaining_keys == {"new"}
    assert remaining_touched == {"new"}
    conn.close()


def test_prune_efficacy_does_not_prune_telemetry(tmp_path):
    conn = efficacy.init(str(tmp_path / "eff.db"))
    conn.execute("INSERT INTO telemetry(session_id,ts,cmd) VALUES('s','2020-01-01T00:00:00.000000Z','list')")
    conn.execute("INSERT INTO surfaced VALUES('s','k','file','files','2020-01-01T00:00:00.000000Z',1)")
    conn.commit()

    efficacy.prune_efficacy(conn, retention_days=30, now="2020-03-01T00:00:00.000000Z")
    telemetry_count = conn.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0]
    surfaced_count = conn.execute("SELECT COUNT(*) FROM surfaced").fetchone()[0]

    assert telemetry_count == 1
    assert surfaced_count == 0
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


def test_init_retries_when_schema_db_is_temporarily_locked(tmp_path):
    db = str(tmp_path / "eff.db")
    efficacy.init(db).close()

    lock_ready = threading.Event()

    def hold_lock() -> None:
        conn = efficacy.connect(db)
        try:
            conn.execute("BEGIN IMMEDIATE")
            lock_ready.set()
            time.sleep(1.2)
            conn.commit()
        finally:
            conn.close()

    t = threading.Thread(target=hold_lock)
    t.start()
    lock_ready.wait(timeout=2)

    conn = None
    try:
        conn = efficacy.init(db)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == efficacy.SCHEMA_VERSION
    finally:
        if conn is not None:
            conn.close()
        t.join()


def test_check_health_reports_integrity_and_wal_checkpoint(tmp_path):
    db = str(tmp_path / "eff.db")
    conn = efficacy.init(db)
    try:
        health = efficacy.check_health(conn)
    finally:
        conn.close()

    assert health["ok"] is True
    assert health["integrity"]["ok"] is True
    assert health["integrity"]["detail"] == "ok"
    assert health["wal"]["ok"] is True
    assert "checkpointed_frames" in health["wal"]


def test_init_recovers_from_corrupt_db_non_destructively(tmp_path):
    db = tmp_path / "eff.db"
    db.write_bytes(b"this is not sqlite")

    conn = efficacy.init(str(db))
    conn.close()

    archived = list(tmp_path.glob("eff.db.corrupt-*"))
    assert len(archived) == 1
    assert archived[0].read_bytes() == b"this is not sqlite"

    conn = efficacy.connect(str(db))
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "telemetry" in names
    finally:
        conn.close()


def test_init_does_not_recover_on_transient_integrity_check_lock(tmp_path, monkeypatch):
    db = tmp_path / "eff.db"
    efficacy.init(str(db)).close()

    called = {"recover": 0}

    def fake_integrity(_conn, quick=True):
        assert quick is True
        return {"ok": False, "check": "quick_check", "detail": "database is locked"}

    def fake_recover(_path=None):
        called["recover"] += 1
        return []

    monkeypatch.setattr(efficacy, "check_integrity", fake_integrity)
    monkeypatch.setattr(efficacy, "recover_corrupt_store", fake_recover)

    try:
        efficacy.init(str(db))
        assert False, "expected init to fail on transient integrity-check lock"
    except Exception as exc:
        assert "integrity check failed: database is locked" in str(exc).lower()

    assert called["recover"] == 0
    assert list(tmp_path.glob("eff.db.corrupt-*")) == []


def test_init_recovers_on_non_transient_integrity_check_failure(tmp_path, monkeypatch):
    db = tmp_path / "eff.db"
    efficacy.init(str(db)).close()

    called = {"recover": 0}
    integrity_calls = {"count": 0}

    def fake_integrity(_conn, quick=True):
        assert quick is True
        integrity_calls["count"] += 1
        if integrity_calls["count"] == 1:
            return {"ok": False, "check": "quick_check", "detail": "row 12 missing from index idx_telemetry_ts"}
        return {"ok": True, "check": "quick_check", "detail": "ok"}

    def fake_recover(_path=None):
        called["recover"] += 1
        return []

    monkeypatch.setattr(efficacy, "check_integrity", fake_integrity)
    monkeypatch.setattr(efficacy, "recover_corrupt_store", fake_recover)

    conn = efficacy.init(str(db))
    conn.close()

    assert called["recover"] == 1
    assert integrity_calls["count"] == 2
