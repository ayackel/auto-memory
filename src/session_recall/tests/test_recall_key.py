# src/session_recall/tests/test_recall_key.py
from session_recall.util import recall_key


def test_repo_file_key_stable_across_worktrees():
    k1 = recall_key.file_key("/projects/foo/src/main.py",
                             repo_id="acme/foo", worktree_root="/projects/foo")
    k2 = recall_key.file_key("/projects/foo-pr/src/main.py",
                             repo_id="acme/foo", worktree_root="/projects/foo-pr")
    assert k1 == k2  # same repo + same relpath -> same key
    assert len(k1) == 16


def test_different_relpath_differs():
    k1 = recall_key.file_key("/p/foo/a.py", repo_id="acme/foo", worktree_root="/p/foo")
    k2 = recall_key.file_key("/p/foo/b.py", repo_id="acme/foo", worktree_root="/p/foo")
    assert k1 != k2


def test_current_root_fast_path_no_subprocess(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(recall_key, "_repo_for_dir", lambda d: "acme/foo")

    def boom(_):
        called["n"] += 1
        return None
    monkeypatch.setattr(recall_key, "_toplevel_for_dir", boom)
    k = recall_key.file_key("/wt/cur/src/x.py", current_root="/wt/cur")
    assert len(k) == 16
    assert called["n"] == 0  # fast path skipped git toplevel resolution


def test_non_repo_file_falls_back_to_abspath():
    k = recall_key.file_key("/home/me/.bashrc", repo_id=None, worktree_root=None)
    assert len(k) == 16


def test_session_key_is_8char_prefix():
    assert recall_key.session_key("3e29bafa-c533-4963") == "3e29bafa"
    assert recall_key.session_key("") == ""
