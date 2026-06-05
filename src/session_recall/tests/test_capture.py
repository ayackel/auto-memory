# src/session_recall/tests/test_capture.py
import os
import sqlite3
import types

import pytest

from session_recall.db import efficacy
from session_recall.util import capture, recall_key


def _make_copilot_store(path, session_id, turns, files, cwd="/wt", repository="owner/repo"):
    """turns: list[(turn_index, assistant_response)]; files: list[(turn_index, abspath)]."""
    c = sqlite3.connect(path)
    c.executescript(
        "CREATE TABLE sessions(id TEXT, cwd TEXT, repository TEXT);"
        "CREATE TABLE turns(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,"
        " turn_index INTEGER, user_message TEXT, assistant_response TEXT, timestamp TEXT);"
        "CREATE TABLE session_files(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,"
        " file_path TEXT, tool_name TEXT, turn_index INTEGER, first_seen_at TEXT);"
    )
    c.execute("INSERT INTO sessions(id,cwd,repository) VALUES(?,?,?)", (session_id, cwd, repository))
    for ti, ar in turns:
        c.execute("INSERT INTO turns(session_id,turn_index,assistant_response) VALUES(?,?,?)",
                  (session_id, ti, ar))
    for ti, fp in files:
        c.execute("INSERT INTO session_files(session_id,file_path,turn_index,first_seen_at) "
                  "VALUES(?,?,?,?)", (session_id, fp, ti, "2026-01-01T00:00:00Z"))
    c.commit()
    c.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    copilot = str(tmp_path / "store.db")
    eff = str(tmp_path / "eff.db")
    efficacy.init(eff).close()
    monkeypatch.setattr(capture.config, "DB_PATH", copilot, raising=False)
    monkeypatch.setattr(capture.config, "EFFICACY_DB_PATH", eff, raising=False)
    monkeypatch.setattr(capture.config, "AGENT_SESSION_ID", "sess-1", raising=False)
    monkeypatch.setattr(capture.config, "NO_CAPTURE", False, raising=False)
    monkeypatch.setattr(capture.config, "CAPTURE_BUDGET_MS", 5000, raising=False)
    # deterministic keys: identity by filename
    monkeypatch.setattr(recall_key, "file_key",
                        lambda p, **k: "K-" + p.rsplit("/", 1)[-1])
    recall_key.reset_cache()
    return types.SimpleNamespace(copilot=copilot, eff=eff)


def _args(command, capture_payload):
    a = types.SimpleNamespace(command=command)
    a._capture = capture_payload
    return a


def test_surface_then_use_is_touched(env):
    _make_copilot_store(env.copilot, "sess-1",
                        turns=[(0, "a"), (1, "a")], files=[])
    capture.run(_args("files", {"files": ["/wt/foo.py"]}))  # surfaced_turn = 1
    c = sqlite3.connect(env.copilot)
    c.execute("INSERT INTO turns(session_id,turn_index,assistant_response) VALUES('sess-1',2,'a')")
    c.execute("INSERT INTO session_files(session_id,file_path,turn_index,first_seen_at) "
              "VALUES('sess-1','/wt/foo.py',2,'t')")
    c.commit(); c.close()
    capture.run(_args("list", {"sessions": []}))
    conn = efficacy.connect(env.eff)
    touched = conn.execute("SELECT key FROM touched WHERE session_id='sess-1'").fetchall()
    assert [r["key"] for r in touched] == ["K-foo.py"]
    conn.close()


def test_use_before_surface_not_touched(env):
    _make_copilot_store(env.copilot, "sess-1",
                        turns=[(0, "a"), (1, "a")],
                        files=[(1, "/wt/foo.py")])
    capture.run(_args("files", {"files": ["/wt/foo.py"]}))  # surfaced_turn = 1; used at turn 1
    capture.run(_args("list", {"sessions": []}))
    conn = efficacy.connect(env.eff)
    assert conn.execute("SELECT COUNT(*) FROM touched").fetchone()[0] == 0
    conn.close()


def test_in_progress_turn_not_counted(env):
    _make_copilot_store(env.copilot, "sess-1",
                        turns=[(0, "a"), (1, "a"), (2, "")],
                        files=[(2, "/wt/foo.py")])
    capture.run(_args("files", {"files": ["/wt/foo.py"]}))  # surfaced_turn = 1
    capture.run(_args("list", {"sessions": []}))
    conn = efficacy.connect(env.eff)
    assert conn.execute("SELECT COUNT(*) FROM touched").fetchone()[0] == 0
    conn.close()


def test_no_capture_env_disables(env, monkeypatch):
    monkeypatch.setattr(capture.config, "NO_CAPTURE", True, raising=False)
    _make_copilot_store(env.copilot, "sess-1", turns=[(0, "a")], files=[])
    capture.run(_args("files", {"files": ["/wt/foo.py"]}))
    conn = efficacy.connect(env.eff)
    assert conn.execute("SELECT COUNT(*) FROM surfaced").fetchone()[0] == 0
    conn.close()


def test_missing_session_id_is_silent(env, monkeypatch):
    monkeypatch.setattr(capture.config, "AGENT_SESSION_ID", None, raising=False)
    capture.run(_args("files", {"files": ["/wt/foo.py"]}))  # must not raise


def test_capture_stat_recorded(env):
    _make_copilot_store(env.copilot, "sess-1", turns=[(0, "a")], files=[])
    capture.run(_args("files", {"files": ["/wt/foo.py"]}))
    conn = efficacy.connect(env.eff)
    row = conn.execute("SELECT status, surfaced_n FROM capture_stat").fetchone()
    assert row["status"] == "completed"
    assert row["surfaced_n"] == 1
    conn.close()


def test_capture_payload_explicit_no_side_channel_required(env):
    _make_copilot_store(env.copilot, "sess-1", turns=[(0, "a")], files=[])
    args = types.SimpleNamespace(command="files")
    capture.run(args, capture_payload={"files": ["/wt/foo.py"]})
    conn = efficacy.connect(env.eff)
    row = conn.execute("SELECT surfaced_n FROM capture_stat").fetchone()
    assert row["surfaced_n"] == 1
    conn.close()


def test_surfaced_n_ignores_duplicate_insert_or_ignore(env):
    _make_copilot_store(env.copilot, "sess-1", turns=[(0, "a")], files=[])
    capture.run(_args("files", {"files": ["/wt/foo.py", "/wt/foo.py"], "sessions": ["s1", "s1"]}))
    conn = efficacy.connect(env.eff)
    row = conn.execute("SELECT surfaced_n FROM capture_stat").fetchone()
    assert row["surfaced_n"] == 2
    conn.close()


def test_session_context_supplies_root_and_repo_id(env, tmp_path, monkeypatch):
    root = tmp_path / "repo"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (root / ".git").mkdir()
    fpath = str(root / "foo.py")
    _make_copilot_store(
        env.copilot,
        "sess-1",
        turns=[(0, "a")],
        files=[],
        cwd=str(nested),
        repository="acme/tooling",
    )
    seen = []

    def fake_file_key(path, **kwargs):
        seen.append(kwargs)
        return "K-" + os.path.basename(path)

    monkeypatch.setattr(recall_key, "file_key", fake_file_key)
    capture.run(_args("files", {"files": [fpath]}))
    assert seen[0]["current_root"] == str(root)
    assert seen[0]["repo_id"] == "acme/tooling"


def test_degraded_capture_warns_with_rate_limit(env, monkeypatch, capsys):
    monkeypatch.setattr(capture, "_DEGRADE_WINDOW_RUNS", 4)
    monkeypatch.setattr(capture, "_DEGRADE_MIN_RUNS", 3)
    monkeypatch.setattr(capture, "_DEGRADE_RATIO_WARN", 0.5)
    monkeypatch.setattr(capture, "_WARN_COOLDOWN_SEC", 3600)
    monkeypatch.setattr(capture.config, "CAPTURE_BUDGET_MS", 0, raising=False)

    _make_copilot_store(env.copilot, "sess-1", turns=[(0, "a")], files=[])
    payload = {"files": ["/wt/a.py", "/wt/b.py"]}
    for _ in range(4):
        capture.run(_args("files", payload))

    err = capsys.readouterr().err
    assert err.count("warning: capture degraded") == 1


def test_capture_does_not_run_prune_inline(env, monkeypatch):
    _make_copilot_store(env.copilot, "sess-1", turns=[(0, "a")], files=[])
    calls = {"n": 0}

    def fake_prune(conn, retention_days, now=None):
        calls["n"] += 1
        return 0

    monkeypatch.setattr(capture.efficacy, "prune", fake_prune)
    capture.run(_args("files", {"files": ["/wt/foo.py"]}))
    assert calls["n"] == 0
