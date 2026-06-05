import types

from session_recall.commands import checkpoints as checkpoints_cmd
from session_recall.commands import files as files_cmd
from session_recall.commands import list_sessions as list_cmd
from session_recall.commands import search as search_cmd


def _fake_provider(**methods):
    defaults = {
        "provider_id": "cli",
        "uses_jsonl_scan": lambda: False,
        "schema_problems": lambda: [],
        "recent_files": lambda **_: [],
        "list_sessions": lambda **_: [],
        "list_checkpoints": lambda **_: [],
        "search": lambda *_args, **_kwargs: [],
    }
    defaults.update(methods)
    return types.SimpleNamespace(**defaults)


def test_files_stashes_surfaced_file_paths(monkeypatch):
    monkeypatch.setattr(
        files_cmd,
        "get_active_providers",
        lambda provider, **k: [
            _fake_provider(
                recent_files=lambda **_: [
                    {"file_path": "/a/b.py"},
                    {"file_path": ""},
                    {"file_path": "/c/d.py"},
                ]
            )
        ],
    )
    monkeypatch.setattr(files_cmd, "output", lambda *_args, **_kwargs: None)
    args = types.SimpleNamespace(command="files", json=True, repo=None, limit=10, days=None, provider="all")

    assert files_cmd.run(args) == 0
    assert args._capture == {"files": ["/a/b.py", "/c/d.py"]}


def test_list_stashes_full_and_short_session_ids(monkeypatch):
    monkeypatch.setattr(
        list_cmd,
        "get_active_providers",
        lambda provider, **k: [
            _fake_provider(
                list_sessions=lambda **_: [
                    {"id_full": "sess-full-1", "id": "sess-short-ignored"},
                    {"id": "sess-short-2"},
                    {"id_full": ""},
                ]
            )
        ],
    )
    monkeypatch.setattr(list_cmd, "detect_repo", lambda: "acme/repo")
    monkeypatch.setattr(list_cmd, "output", lambda *_args, **_kwargs: None)
    args = types.SimpleNamespace(command="list", json=True, repo=None, limit=10, days=None, provider="all")

    assert list_cmd.run(args) == 0
    assert args._capture == {"sessions": ["sess-full-1", "sess-short-2"]}


def test_search_stashes_session_id_when_full_id_missing(monkeypatch):
    monkeypatch.setattr(
        search_cmd,
        "get_active_providers",
        lambda provider, **k: [
            _fake_provider(
                search=lambda *_, **__: [
                    {"session_id_full": "sess-full-1"},
                    {"session_id": "sess-short-2"},
                    {"session_id_full": ""},
                ]
            )
        ],
    )
    monkeypatch.setattr(search_cmd, "detect_repo", lambda: "acme/repo")
    monkeypatch.setattr(search_cmd, "output", lambda *_args, **_kwargs: None)
    args = types.SimpleNamespace(
        command="search",
        query="auth",
        json=True,
        repo=None,
        limit=10,
        days=None,
        provider="all",
    )

    assert search_cmd.run(args) == 0
    assert args._capture == {"sessions": ["sess-full-1", "sess-short-2"]}


def test_checkpoints_stashes_full_and_short_session_ids(monkeypatch):
    monkeypatch.setattr(
        checkpoints_cmd,
        "get_active_providers",
        lambda provider, **k: [
            _fake_provider(
                list_checkpoints=lambda **_: [
                    {"session_id_full": "sess-full-1", "session_id": "sess-short-ignored"},
                    {"session_id": "sess-short-2"},
                    {"session_id": ""},
                ]
            )
        ],
    )
    monkeypatch.setattr(checkpoints_cmd, "detect_repo", lambda: "acme/repo")
    monkeypatch.setattr(checkpoints_cmd, "output", lambda *_args, **_kwargs: None)
    args = types.SimpleNamespace(command="checkpoints", json=True, repo=None, limit=10, days=None, provider="all")

    assert checkpoints_cmd.run(args) == 0
    assert args._capture == {"sessions": ["sess-full-1", "sess-short-2"]}
