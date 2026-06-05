import sqlite3
import types

from session_recall.db import efficacy
from session_recall.util import capture


def _seed_store(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE sessions(id TEXT, cwd TEXT, repository TEXT);"
        "CREATE TABLE turns(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,"
        " turn_index INTEGER, user_message TEXT, assistant_response TEXT, timestamp TEXT);"
        "CREATE TABLE session_files(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,"
        " file_path TEXT, tool_name TEXT, turn_index INTEGER, first_seen_at TEXT);"
    )
    conn.execute("INSERT INTO sessions(id,cwd,repository) VALUES('sess-1','/wt','owner/repo')")
    conn.execute("INSERT INTO turns(session_id,turn_index,assistant_response) VALUES('sess-1',0,'a')")
    conn.commit()
    conn.close()


def test_budget_timeout_blocks_late_lifecycle_phases(tmp_path, monkeypatch):
    copilot = str(tmp_path / "store.db")
    eff = str(tmp_path / "eff.db")
    _seed_store(copilot)
    efficacy.init(eff).close()

    monkeypatch.setattr(capture.config, "DB_PATH", copilot, raising=False)
    monkeypatch.setattr(capture.config, "EFFICACY_DB_PATH", eff, raising=False)
    monkeypatch.setattr(capture.config, "AGENT_SESSION_ID", "sess-1", raising=False)
    monkeypatch.setattr(capture.config, "NO_CAPTURE", False, raising=False)
    monkeypatch.setattr(capture.config, "CAPTURE_BUDGET_MS", -1, raising=False)

    args = types.SimpleNamespace(command="list", _capture={"sessions": ["abcd1234"]})
    capture.run(args)

    conn = efficacy.connect(eff)
    row = conn.execute("SELECT status, surfaced_n FROM capture_stat").fetchone()
    assert row["status"] == "timeout"
    assert row["surfaced_n"] == 0
    conn.close()
