# src/session_recall/tests/test_capture_stashes.py
import types
from session_recall.commands import files as files_cmd


def test_files_sets_capture(monkeypatch):
    captured = {}
    monkeypatch.setattr(files_cmd, "output", lambda data, **k: captured.update(data=data))
    # Adjust this monkeypatch to the actual provider accessor in files.py:
    monkeypatch.setattr(files_cmd, "get_active_providers",
                        lambda provider, **k: [types.SimpleNamespace(
                            provider_id="cli",
                            schema_problems=lambda: [],
                            recent_files=lambda **kk: [
                                {"file_path": "/a/b.py"}, {"file_path": "/c/d.py"}])])
    args = types.SimpleNamespace(command="files", json=True, repo=None,
                                 limit=10, days=None, provider="all")
    files_cmd.run(args)
    assert args._capture == {"files": ["/a/b.py", "/c/d.py"]}
