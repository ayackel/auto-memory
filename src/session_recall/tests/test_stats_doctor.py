import io
import json
import types
from contextlib import redirect_stdout

from session_recall.db import efficacy
from session_recall.util import telemetry
from session_recall.commands import stats as stats_cmd
from session_recall.commands import doctor as doctor_cmd


def _seed(tmp_path):
    db = str(tmp_path / "eff.db")
    efficacy.init(db).close()
    telemetry.init(db)
    telemetry.record("list", 10, tier=1)
    telemetry.record("search", 20, tier=2, query_hash="abcd1234")
    telemetry.record("show", 30, tier=3, session_id_prefix="aaaa1111")
    return db


def _run(cmd_mod, db):
    args = types.SimpleNamespace(json=True)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cmd_mod.run(args, db_path=db)
    return rc, json.loads(buf.getvalue())


def test_stats_summarizes_by_command(tmp_path):
    db = _seed(tmp_path)
    rc, out = _run(stats_cmd, db)
    assert rc == 0
    assert out["total"] == 3
    by_cmd = {c["cmd"]: c for c in out["by_command"]}
    assert by_cmd["list"]["count"] == 1
    assert by_cmd["search"]["count"] == 1
    telemetry.init(None)


def test_doctor_reports_ok(tmp_path):
    db = _seed(tmp_path)
    rc, out = _run(doctor_cmd, db)
    assert rc == 0
    assert out["telemetry_rows"] == 3
    assert "status" in out
    telemetry.init(None)
