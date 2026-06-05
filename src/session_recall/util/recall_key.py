# src/session_recall/util/recall_key.py
"""Worktree-stable, privacy-preserving artifact keys for recall efficacy."""
from __future__ import annotations

import hashlib
import os
import subprocess
from functools import lru_cache

from .detect_repo import detect_repo_for_cwd


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


@lru_cache(maxsize=1024)
def _repo_for_dir(directory: str) -> str | None:
    try:
        return detect_repo_for_cwd(directory, timeout=1)
    except Exception:
        return None


@lru_cache(maxsize=1024)
def _toplevel_for_dir(directory: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", directory, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=1,
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except Exception:
        pass
    return None


def reset_cache() -> None:
    _repo_for_dir.cache_clear()
    _toplevel_for_dir.cache_clear()


def session_key(session_id: str) -> str:
    """Session surface key = raw 8-char id prefix (system-native granularity)."""
    return (session_id or "")[:8]


def file_key(abs_path: str, *, repo_id: str | None = None,
             worktree_root: str | None = None, current_root: str | None = None) -> str:
    """Worktree-stable hashed key for a file path.

    Priority: explicit (repo_id+worktree_root) for tests -> current_root fast path
    -> the file's own git repo (memoized) -> abspath fallback.
    """
    norm = os.path.normpath(os.path.abspath(abs_path))

    if repo_id and worktree_root:
        rel = os.path.relpath(norm, os.path.normpath(worktree_root))
        return _h(repo_id + "\0" + rel)

    if current_root:
        croot = os.path.normpath(current_root)
        if norm == croot or norm.startswith(croot + os.sep):
            rid = repo_id or _repo_for_dir(croot)
            if rid:
                rel = os.path.relpath(norm, croot)
                return _h(rid + "\0" + rel)

    directory = os.path.dirname(norm)
    rid = _repo_for_dir(directory)
    root = _toplevel_for_dir(directory)
    if rid and root:
        rel = os.path.relpath(norm, os.path.normpath(root))
        return _h(rid + "\0" + rel)

    return _h(norm)
