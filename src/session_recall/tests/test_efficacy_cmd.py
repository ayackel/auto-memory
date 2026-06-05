import io
import json
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
                 "VALUES('sess-9',?, 'show','aaaa1111')", (later,))
    conn.execute("INSERT INTO capture_stat(session_id,ts,status) VALUES('sess-1',?, 'completed')", (ts,))
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


def test_rates_and_counts(tmp_path):
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


def test_strict_ordering_excludes_pre_surface_escalation(tmp_path):
    db = str(tmp_path / "eff.db")
    conn = efficacy.init(db)
    conn.execute("INSERT INTO surfaced VALUES('s','cccc3333','session','list','2099-01-01T00:00:05.000000Z',0)")
    conn.execute("INSERT INTO telemetry(session_id,ts,cmd,session_id_prefix) "
                 "VALUES('s','2099-01-01T00:00:01.000000Z','show','cccc3333')")
    conn.commit(); conn.close()
    rc, out = _run(db)
    assert out["session_recall"]["hits"] == 0
