import json
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

from session_recall.db import efficacy


def _seed_store(path):
    c = sqlite3.connect(path)
    c.executescript(
        "CREATE TABLE sessions(id TEXT, cwd TEXT, repository TEXT, branch TEXT,"
        " summary TEXT, created_at TEXT, updated_at TEXT, host_type TEXT);"
        "CREATE TABLE turns(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,"
        " turn_index INTEGER, user_message TEXT, assistant_response TEXT, timestamp TEXT);"
        "CREATE TABLE session_files(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,"
        " file_path TEXT, tool_name TEXT, turn_index INTEGER, first_seen_at TEXT);"
        "CREATE TABLE checkpoints(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,"
        " checkpoint_number INTEGER, title TEXT, overview TEXT, created_at TEXT);"
        "CREATE TABLE session_refs(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,"
        " ref_type TEXT, ref_value TEXT, turn_index INTEGER, created_at TEXT);"
    )
    c.execute("INSERT INTO sessions VALUES('sess-1','/x','acme/x','main','s','2099-01-01T00:00:00Z','2099-01-01T00:00:00Z','local')")
    c.execute("INSERT INTO turns(session_id,turn_index,assistant_response) VALUES('sess-1',0,'a')")
    c.execute("INSERT INTO session_files(session_id,file_path,turn_index,first_seen_at) "
              "VALUES('sess-1','/x/used.py',0,'2099-01-01T00:00:00Z')")
    c.commit(); c.close()


def _cli(store, eff, *cmd):
    env = dict(os.environ)
    src = str(Path(__file__).resolve().parents[3])
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env.update(SESSION_RECALL_DB=store, SESSION_RECALL_EFFICACY_DB=eff,
               COPILOT_AGENT_SESSION_ID="sess-1")
    return subprocess.run([sys.executable, "-m", "session_recall", *cmd],
                          capture_output=True, text=True, env=env)


def test_concurrent_capture_no_corruption(tmp_path):
    store = str(tmp_path / "store.db")
    eff = str(tmp_path / "eff.db")
    _seed_store(store)
    efficacy.init(eff).close()

    def worker():
        _cli(store, eff, "files", "--json")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    conn = efficacy.connect(eff)
    ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
    assert ok == "ok"
    assert conn.execute("SELECT COUNT(*) FROM capture_stat").fetchone()[0] >= 8
    conn.close()


def test_efficacy_command_runs_e2e(tmp_path):
    store = str(tmp_path / "store.db")
    eff = str(tmp_path / "eff.db")
    _seed_store(store)
    first = _cli(store, eff, "files", "--repo", "all", "--json")
    assert first.returncode == 0, first.stderr

    c = sqlite3.connect(store)
    c.execute(
        "INSERT INTO turns(session_id,turn_index,assistant_response) "
        "VALUES('sess-1',1,'used surfaced file')"
    )
    c.execute(
        "INSERT INTO session_files(session_id,file_path,turn_index,first_seen_at) "
        "VALUES('sess-1','/x/used.py',1,'2099-01-01T00:01:00Z')"
    )
    c.commit()
    c.close()

    second = _cli(store, eff, "files", "--repo", "all", "--json")
    assert second.returncode == 0, second.stderr

    report = _cli(store, eff, "efficacy", "--days", "3650", "--json")
    assert report.returncode == 0, report.stderr
    out = json.loads(report.stdout)

    assert out["status"] == "insufficient_data"
    assert out["unique_surfaces"] == 1
    assert out["needed"] == 20
    assert out["capture_health"]["runs"] >= 2
