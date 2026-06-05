import io
import json
import types
from contextlib import redirect_stdout

from session_recall.db import efficacy
from session_recall.util import telemetry
from session_recall.commands import stats as stats_cmd
from session_recall.commands import doctor as doctor_cmd


def _seed(tmp_path, name="eff.db", commands=None):
    db = str(tmp_path / name)
    efficacy.init(db).close()
    telemetry.init(db)
    for cmd, duration, kwargs in commands or [
        ("list", 10, {"tier": 1}),
        ("search", 20, {"tier": 2, "query_hash": "abcd1234"}),
        ("show", 30, {"tier": 3, "session_id_prefix": "aaaa1111"}),
    ]:
        telemetry.record(cmd, duration, **kwargs)
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
    assert out["store_health"]["ok"] is True
    assert "status" in out
    telemetry.init(None)


def test_stats_db_path_isolation_does_not_rebind_global(tmp_path):
    db_a = _seed(tmp_path, name="eff_a.db", commands=[("list", 10, {"tier": 1})])
    db_b = _seed(tmp_path, name="eff_b.db", commands=[("search", 20, {"tier": 2})])
    telemetry.init(db_a)

    rc, out = _run(stats_cmd, db_b)

    assert rc == 0
    assert out["total"] == 1
    assert out["by_command"][0]["cmd"] == "search"
    assert telemetry.load_entries(limit=1)[0]["cmd"] == "list"
    telemetry.init(None)


def test_doctor_db_path_isolation_does_not_rebind_global(tmp_path):
    db_a = _seed(tmp_path, name="eff_a.db", commands=[("list", 10, {"tier": 1})])
    db_b = _seed(tmp_path, name="eff_b.db", commands=[("search", 20, {"tier": 2})])
    telemetry.init(db_a)

    rc, out = _run(doctor_cmd, db_b)

    assert rc == 0
    assert out["telemetry_rows"] == 1
    assert telemetry.load_entries(limit=1)[0]["cmd"] == "list"
    telemetry.init(None)
