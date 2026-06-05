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
    conn = efficacy.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM surfaced").fetchone()[0] == 1
    conn.close()
