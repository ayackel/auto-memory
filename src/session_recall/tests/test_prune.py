import io
import json
import types
from contextlib import redirect_stdout

from session_recall.db import efficacy
from session_recall.commands import prune as prune_cmd


def test_prune_removes_old_and_reports(tmp_path):
    db = str(tmp_path / "eff.db")
    conn = efficacy.init(db)
    conn.execute("INSERT INTO surfaced VALUES('s','k','file','files','2000-01-01T00:00:00.000000Z',0)")
    conn.execute("INSERT INTO surfaced VALUES('s','k2','file','files',?,0)", (efficacy.now_iso(),))
    conn.commit(); conn.close()
    args = types.SimpleNamespace(json=True)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = prune_cmd.run(args, db_path=db)
    out = json.loads(buf.getvalue())
    assert rc == 0
    assert out["deleted"] >= 1
    assert out["deleted"] == out["efficacy_deleted"] + out["telemetry_deleted"]
    assert out["efficacy_retention_days"] >= 1
    assert out["telemetry_retention_days"] >= 1
    conn = efficacy.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM surfaced").fetchone()[0] == 1
    conn.close()


def test_prune_honors_now_and_decoupled_retention_windows(tmp_path, monkeypatch):
    db = str(tmp_path / "eff.db")
    conn = efficacy.init(db)
    conn.execute(
        "INSERT INTO surfaced VALUES('s','old','file','files','2026-01-01T00:00:00.000000Z',0)"
    )
    conn.execute("INSERT INTO telemetry(session_id,ts,cmd) VALUES('s','2026-01-15T00:00:00.000000Z','list')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(prune_cmd, "EFFICACY_RETENTION_DAYS", 30)
    monkeypatch.setattr(prune_cmd, "TELEMETRY_RETENTION_DAYS", 90)

    args = types.SimpleNamespace(json=True, now="2026-03-01T00:00:00.000000Z")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = prune_cmd.run(args, db_path=db)
    out = json.loads(buf.getvalue())

    assert rc == 0
    assert out["efficacy_deleted"] == 1
    assert out["telemetry_deleted"] == 0

    conn = efficacy.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM surfaced").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0] == 1
    conn.close()
