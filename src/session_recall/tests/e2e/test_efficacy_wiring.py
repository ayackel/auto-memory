# src/session_recall/tests/e2e/test_efficacy_wiring.py
import os
import sqlite3
import subprocess
import sys


def _seed(path):
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
    c.execute("INSERT INTO session_files(session_id,file_path,turn_index,first_seen_at) VALUES('sess-1','/x/a.py',0,'2099-01-01T00:00:00Z')")
    c.commit(); c.close()


def test_capture_runs_via_main(tmp_path):
    store = str(tmp_path / "store.db")
    eff = str(tmp_path / "eff.db")
    _seed(store)
    env = dict(os.environ)
    env.update(SESSION_RECALL_DB=store, SESSION_RECALL_EFFICACY_DB=eff,
               COPILOT_AGENT_SESSION_ID="sess-1")
    r = subprocess.run([sys.executable, "-m", "session_recall", "files", "--json"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    conn = sqlite3.connect(eff)
    n = conn.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0]
    cs = conn.execute("SELECT COUNT(*) FROM capture_stat").fetchone()[0]
    conn.close()
    assert n >= 1
    assert cs >= 1
