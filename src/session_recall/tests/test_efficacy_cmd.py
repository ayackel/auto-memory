import io
import json
import sqlite3
import types
from contextlib import redirect_stdout

from session_recall.db import efficacy
from session_recall.commands import efficacy as eff_cmd


def _seed(db):
    conn = efficacy.init(db)
    ts = "2099-01-01T00:00:00.000000Z"
    later = "2099-01-01T00:00:05.000000Z"
    for i, hit in [(1, True), (2, True), (3, False)]:
        conn.execute("INSERT INTO surfaced VALUES('sess-1',?, 'file','files',?,0)", (f"f{i}", ts))
        if hit:
            conn.execute("INSERT INTO touched VALUES('sess-1',?,?,1)", (f"f{i}", later))
    conn.execute("INSERT INTO surfaced VALUES('sess-1','aaaa1111','session','list',?,0)", (ts,))
    conn.execute("INSERT INTO surfaced VALUES('sess-1','bbbb2222','session','list',?,0)", (ts,))
    conn.execute("INSERT INTO telemetry(session_id,ts,cmd,session_id_prefix) "
                 "VALUES('sess-1',?, 'show','aaaa1111')", (later,))
    conn.execute("INSERT INTO capture_stat(session_id,ts,status) VALUES('sess-1',?, 'completed')", (ts,))
    conn.commit()
    conn.close()


def _seed_file_surfaces(db, n):
    conn = efficacy.init(db)
    ts = "2099-01-01T00:00:00.000000Z"
    for i in range(n):
        conn.execute("INSERT INTO surfaced VALUES('sess-1',?, 'file','files',?,0)", (f"f{i}", ts))
    conn.commit()
    conn.close()


def _run(db, **kw):
    args = types.SimpleNamespace(days=3650, repo=None, session=None, json=True)
    for k, v in kw.items():
        setattr(args, k, v)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = eff_cmd.run(args, db_path=db)
    return rc, json.loads(buf.getvalue())


def _seed_repo_store(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY, repository TEXT)")
    conn.execute("INSERT INTO sessions VALUES('sess-1', 'acme/one')")
    conn.execute("INSERT INTO sessions VALUES('sess-2', 'acme/two')")
    conn.commit()
    conn.close()


def test_rates_and_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(eff_cmd, "_MIN_SURFACES", 1)
    db = str(tmp_path / "eff.db")
    _seed(db)
    rc, out = _run(db)
    assert rc == 0
    assert out["file_recall"]["hits"] == 2
    assert out["file_recall"]["surfaces"] == 3
    assert out["session_recall"]["hits"] == 1
    assert out["session_recall"]["surfaces"] == 2
    assert abs(out["blended"]["rate"] - 0.6) < 1e-9  # (2+1)/(3+2)


def test_insufficient_data_guard(tmp_path):
    db = str(tmp_path / "eff.db")
    efficacy.init(db).close()
    rc, out = _run(db)
    assert out["status"] == "insufficient_data"
    assert out["unique_surfaces"] == 0


def test_sample_size_guard_boundary_19_insufficient(tmp_path):
    db = str(tmp_path / "eff.db")
    _seed_file_surfaces(db, 19)
    rc, out = _run(db)
    assert rc == 0
    assert out["status"] == "insufficient_data"
    assert out["unique_surfaces"] == 19
    assert out["needed"] == 20


def test_sample_size_guard_boundary_20_ok(tmp_path):
    db = str(tmp_path / "eff.db")
    _seed_file_surfaces(db, 20)
    rc, out = _run(db)
    assert rc == 0
    assert out["status"] == "ok"
    assert out["blended"]["surfaces"] == 20


def test_strict_ordering_excludes_pre_surface_escalation(tmp_path, monkeypatch):
    monkeypatch.setattr(eff_cmd, "_MIN_SURFACES", 1)
    db = str(tmp_path / "eff.db")
    conn = efficacy.init(db)
    conn.execute("INSERT INTO surfaced VALUES('s','cccc3333','session','list','2099-01-01T00:00:05.000000Z',0)")
    conn.execute("INSERT INTO telemetry(session_id,ts,cmd,session_id_prefix) "
                 "VALUES('s','2099-01-01T00:00:01.000000Z','show','cccc3333')")
    conn.commit(); conn.close()
    rc, out = _run(db)
    assert out["session_recall"]["hits"] == 0


def test_escalation_attribution_does_not_cross_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(eff_cmd, "_MIN_SURFACES", 1)
    db = str(tmp_path / "eff.db")
    conn = efficacy.init(db)
    conn.execute("INSERT INTO surfaced VALUES('sess-1','aaaa1111','session','list','2099-01-01T00:00:00.000000Z',0)")
    conn.execute("INSERT INTO telemetry(session_id,ts,cmd,session_id_prefix) "
                 "VALUES('sess-2','2099-01-01T00:00:05.000000Z','show','aaaa1111')")
    conn.commit()
    conn.close()
    rc, out = _run(db, session="sess-1")
    assert rc == 0
    assert out["session_recall"]["hits"] == 0
    assert out["session_recall"]["surfaces"] == 1


def test_per_session_worst_honors_session_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(eff_cmd, "_MIN_SURFACES", 1)
    db = str(tmp_path / "eff.db")
    conn = efficacy.init(db)
    conn.execute("INSERT INTO surfaced VALUES('sess-1','f1','file','files','2099-01-01T00:00:00.000000Z',0)")
    conn.execute("INSERT INTO touched VALUES('sess-1','f1','2099-01-01T00:00:05.000000Z',1)")
    conn.execute("INSERT INTO surfaced VALUES('sess-2','f2','file','files','2099-01-01T00:00:00.000000Z',0)")
    conn.commit()
    conn.close()
    rc, out = _run(db, session="sess-1")
    assert rc == 0
    assert out["per_session_worst"] == [{
        "session_id": "sess-1",
        "surfaces": 1,
        "hits": 1,
        "rate": 1.0,
    }]


def test_repo_filter_changes_overall_metrics(tmp_path, monkeypatch):
    monkeypatch.setattr(eff_cmd, "_MIN_SURFACES", 1)
    db = str(tmp_path / "eff.db")
    store = str(tmp_path / "store.db")
    _seed_repo_store(store)
    monkeypatch.setattr(eff_cmd, "DB_PATH", store)
    conn = efficacy.init(db)
    conn.execute("INSERT INTO surfaced VALUES('sess-1','f1','file','files','2099-01-01T00:00:00.000000Z',0)")
    conn.execute("INSERT INTO touched VALUES('sess-1','f1','2099-01-01T00:00:05.000000Z',1)")
    conn.execute("INSERT INTO surfaced VALUES('sess-2','f2','file','files','2099-01-01T00:00:00.000000Z',0)")
    conn.commit()
    conn.close()

    _, all_out = _run(db)
    _, repo_out = _run(db, repo="acme/one")

    assert all_out["blended"]["surfaces"] == 2
    assert all_out["blended"]["hits"] == 1
    assert repo_out["blended"]["surfaces"] == 1
    assert repo_out["blended"]["hits"] == 1
    assert repo_out["file_recall"]["rate"] == 1.0


def test_repo_filter_changes_per_session_worst(tmp_path, monkeypatch):
    monkeypatch.setattr(eff_cmd, "_MIN_SURFACES", 1)
    db = str(tmp_path / "eff.db")
    store = str(tmp_path / "store.db")
    _seed_repo_store(store)
    monkeypatch.setattr(eff_cmd, "DB_PATH", store)
    conn = efficacy.init(db)
    conn.execute("INSERT INTO surfaced VALUES('sess-1','f1','file','files','2099-01-01T00:00:00.000000Z',0)")
    conn.execute("INSERT INTO touched VALUES('sess-1','f1','2099-01-01T00:00:05.000000Z',1)")
    conn.execute("INSERT INTO surfaced VALUES('sess-2','f2','file','files','2099-01-01T00:00:00.000000Z',0)")
    conn.commit()
    conn.close()

    _, repo_out = _run(db, repo="acme/one")

    assert repo_out["per_session_worst"] == [{
        "session_id": "sess-1",
        "surfaces": 1,
        "hits": 1,
        "rate": 1.0,
    }]
