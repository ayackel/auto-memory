# Recall Efficacy Measurement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `session-recall efficacy` subsystem that measures whether artifacts (files + past sessions) surfaced by recall are actually USED afterward — a "recalled-then-used rate."

**Architecture:** A central, time-boxed, silent-fail capture hook piggybacks on every recall invocation. It records what each command *surfaced* (worktree-stable hashed file keys; raw session-id prefixes) and snapshots what was newly *used* since a per-session closed-turn watermark, into a WAL-mode SQLite sidecar. The existing JSON telemetry ring-buffer is migrated into that same sidecar (single source of truth). An `efficacy` command analyzes stored-keys-vs-stored-keys and reports pooled + per-session rates with sample sizes. No live path re-derivation at analysis time.

**Tech Stack:** Python 3.11+, stdlib `sqlite3` (WAL), `argparse`, `pytest`. No new dependencies.

---

## Baseline (verified before planning)

- Test command: `python -m pytest -q` from repo root. Package must be installed editable once: `pip install -e . --break-system-packages -q`.
- **Baseline result: 260 passed, 8 pre-existing failures.** The 8 failures (`test_e2e_list.py`, `test_e2e_search.py`) are caused by stale fixture dates (sessions dated 2026-04 fall outside the default 30-day window). They are unrelated to this work. **Do not "fix" them.** Verification steps below target the new tests plus "no NEW failures beyond those 8."
- Copilot store schema (read-only source): `sessions(id, cwd, repository, branch, summary, created_at, updated_at, host_type)`, `turns(id, session_id, turn_index, user_message, assistant_response, timestamp)`, `session_files(id, session_id, file_path, tool_name, turn_index, first_seen_at)`.
- `session_files` is one row per (session, file), first-seen only; `file_path` is always absolute. `turns.turn_index` and `session_files.turn_index` share one per-session counter.
- Current live session id: env `COPILOT_AGENT_SESSION_ID` (== `sessions.id`).
- `detect_repo_for_cwd(cwd, timeout=5)` → `"owner/repo"` or `None` (origin-based, worktree-stable).

## Design decisions adopted (post rubber-duck)

- **D1 — Closed-turn watermark (correctness linchpin):** `surfaced_turn` and the touched-scan upper bound use `closed_turn = MAX(turn_index) FROM turns WHERE assistant_response IS NOT NULL AND assistant_response <> ''`. The in-progress turn (no assistant response yet) is excluded, so a file surfaced at turn N's recall (`surfaced_turn = N-1`) and opened during turn N (`turn_index = N`) is correctly credited at turn N+1's capture. The cursor only ever advances forward, so no turn's uses are skipped.
- **D2 — Touched only in v1.** `Referenced` (path-fragment text matching, weight 0.4) is **deferred** — too noisy without self-reference exclusion, and subsumed by Touched. File-recall efficacy = Touched rate (weight 1.0). Documented as future work.
- **D3 — Escalation computed at report time** via a join of `surfaced(kind='session')` against `telemetry` rows for `show`/`export`/`diff`, not a capture-side table. Microsecond-resolution timestamps make the strict `t.ts > s.first_ts` ordering reliable (no same-second ambiguity). Labeled as *correlation* ("surfaced → escalated"), not causation.
- **D4 — Key lengths & identity:** file keys = `sha256(...)[:16]` (64-bit, collision-safe for attribution). Session surface keys = raw 8-char session-id prefix (system-native; the agent and telemetry already use 8-char prefixes; collision negligible at personal scale).
- **D5 — file_key resolves the file's OWN repo context** with a current-worktree-root fast path: paths under the live session root skip git subprocesses; foreign paths use memoized `git -C`. The 150 ms time-box bounds cold-cache cost.
- **D6 — Telemetry migration uses a compat loader.** `telemetry.load_entries()` returns JSON-shaped dicts that **omit NULL optional fields**, preserving the existing `"tier" not in e` semantics so health-dim logic changes minimally. Schema is initialized **once per process** (not per write). Health dims preserve last-500 ordering via `ORDER BY id DESC LIMIT 500`.
- **D7 — Capture-health counters.** A `capture_stat` table records attempted/completed/timeout + counts so a `0%` efficacy reading can be distinguished from "capture never ran."

## File structure

**Create:**
- `src/session_recall/db/efficacy.py` — sidecar schema, WAL connection, schema-init, prune, `now_iso()`.
- `src/session_recall/util/recall_key.py` — worktree-stable file keys + session keys (memoized).
- `src/session_recall/util/capture.py` — central time-boxed silent-fail capture.
- `src/session_recall/commands/efficacy.py` — analyzer/report.
- `src/session_recall/commands/prune.py` — manual prune + VACUUM.
- `src/session_recall/commands/stats.py` — telemetry usage summary (borrowed concept, SQLite).
- `src/session_recall/commands/doctor.py` — telemetry/store health (borrowed concept, SQLite).
- Tests: `src/session_recall/tests/test_efficacy_db.py`, `test_recall_key.py`, `test_capture.py`, `test_efficacy_cmd.py`, `test_prune.py`, `test_stats_doctor.py`, `tests/e2e/test_efficacy_e2e.py`.

**Modify:**
- `src/session_recall/config.py` — new paths/env vars.
- `src/session_recall/util/telemetry.py` — JSON → SQLite writer + `load_entries()` compat loader.
- `src/session_recall/tests/test_telemetry.py` — rewrite for SQLite.
- `src/session_recall/health/dim_disclosure.py` — read via `telemetry.load_entries()`.
- `src/session_recall/health/dim_concurrency.py` — read via `telemetry.load_entries()`.
- `src/session_recall/__main__.py` — schema-init once, `session_id` in telemetry, `capture.run(args)`, register `efficacy`/`prune`/`stats`/`doctor`.
- `src/session_recall/commands/files.py`, `list_sessions.py`, `search.py`, `checkpoints.py` — set `args._capture` before `output(...)`.

---

## Task 1: Config additions

**Files:**
- Modify: `src/session_recall/config.py`
- Test: `src/session_recall/tests/test_config_efficacy.py`

- [ ] **Step 1: Write the failing test**

```python
# src/session_recall/tests/test_config_efficacy.py
import importlib


def test_efficacy_config_defaults(monkeypatch):
    for var in ("SESSION_RECALL_EFFICACY_DB", "SESSION_RECALL_RETENTION_DAYS",
                "SESSION_RECALL_CAPTURE_BUDGET_MS", "SESSION_RECALL_NO_CAPTURE",
                "COPILOT_AGENT_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    import session_recall.config as cfg
    importlib.reload(cfg)
    assert cfg.EFFICACY_DB_PATH.endswith("session-recall-efficacy.db")
    assert cfg.RETENTION_DAYS == 90
    assert cfg.CAPTURE_BUDGET_MS == 150
    assert cfg.NO_CAPTURE is False
    assert cfg.AGENT_SESSION_ID is None


def test_efficacy_config_env_override(monkeypatch):
    monkeypatch.setenv("SESSION_RECALL_NO_CAPTURE", "1")
    monkeypatch.setenv("SESSION_RECALL_RETENTION_DAYS", "30")
    monkeypatch.setenv("COPILOT_AGENT_SESSION_ID", "abc")
    import session_recall.config as cfg
    importlib.reload(cfg)
    assert cfg.NO_CAPTURE is True
    assert cfg.RETENTION_DAYS == 30
    assert cfg.AGENT_SESSION_ID == "abc"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest src/session_recall/tests/test_config_efficacy.py -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'EFFICACY_DB_PATH'`

- [ ] **Step 3: Add config constants**

Append to `src/session_recall/config.py`:

```python
EFFICACY_DB_PATH = os.environ.get(
    "SESSION_RECALL_EFFICACY_DB",
    str(Path.home() / ".copilot" / "scripts" / "session-recall-efficacy.db"),
)

RETENTION_DAYS = int(os.environ.get("SESSION_RECALL_RETENTION_DAYS", "90"))

CAPTURE_BUDGET_MS = int(os.environ.get("SESSION_RECALL_CAPTURE_BUDGET_MS", "150"))

NO_CAPTURE = _truthy("SESSION_RECALL_NO_CAPTURE")

AGENT_SESSION_ID = os.environ.get("COPILOT_AGENT_SESSION_ID")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest src/session_recall/tests/test_config_efficacy.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/session_recall/config.py src/session_recall/tests/test_config_efficacy.py
git commit -m "feat(efficacy): add config for sidecar DB, retention, capture budget"
```

---

## Task 2: Sidecar SQLite store (`db/efficacy.py`)

**Files:**
- Create: `src/session_recall/db/efficacy.py`
- Test: `src/session_recall/tests/test_efficacy_db.py`

- [ ] **Step 1: Write the failing test**

```python
# src/session_recall/tests/test_efficacy_db.py
from session_recall.db import efficacy


def test_init_creates_all_tables(tmp_path):
    db = str(tmp_path / "eff.db")
    conn = efficacy.init(db)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"telemetry", "surfaced", "touched", "cursor", "capture_stat"} <= names
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    conn.close()


def test_now_iso_has_microseconds_and_sorts(tmp_path):
    a = efficacy.now_iso()
    b = efficacy.now_iso()
    assert a.endswith("Z") and "." in a
    assert a <= b  # lexicographic == chronological


def test_surfaced_dedup_keeps_earliest(tmp_path):
    conn = efficacy.init(str(tmp_path / "eff.db"))
    conn.execute("INSERT OR IGNORE INTO surfaced VALUES('s','k','file','files','2026-01-01T00:00:00.000000Z',3)")
    conn.execute("INSERT OR IGNORE INTO surfaced VALUES('s','k','file','files','2026-02-01T00:00:00.000000Z',9)")
    conn.commit()
    row = conn.execute("SELECT first_ts, turn FROM surfaced WHERE session_id='s' AND key='k' AND kind='file'").fetchone()
    assert row["turn"] == 3  # earliest retained
    conn.close()


def test_prune_deletes_old_rows(tmp_path):
    conn = efficacy.init(str(tmp_path / "eff.db"))
    conn.execute("INSERT INTO surfaced VALUES('old','k1','file','files','2020-01-01T00:00:00.000000Z',1)")
    conn.execute("INSERT INTO surfaced VALUES('new','k2','file','files',?,1)", (efficacy.now_iso(),))
    conn.commit()
    deleted = efficacy.prune(conn, retention_days=90)
    keys = {r[0] for r in conn.execute("SELECT key FROM surfaced").fetchall()}
    assert keys == {"k2"}
    assert deleted >= 1
    conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest src/session_recall/tests/test_efficacy_db.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'session_recall.db.efficacy'`

- [ ] **Step 3: Implement the store**

```python
# src/session_recall/db/efficacy.py
"""Sidecar SQLite store for telemetry + recall-efficacy data (WAL mode)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import EFFICACY_DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    ts TEXT NOT NULL,
    cmd TEXT,
    duration_ms INTEGER,
    busy_hits INTEGER DEFAULT 0,
    attempts INTEGER DEFAULT 1,
    rows_returned INTEGER DEFAULT 0,
    exit_code INTEGER DEFAULT 0,
    schema_ok INTEGER DEFAULT 1,
    tier INTEGER,
    query_hash TEXT,
    session_id_prefix TEXT,
    window_tier TEXT
);
CREATE TABLE IF NOT EXISTS surfaced (
    session_id TEXT NOT NULL,
    key TEXT NOT NULL,
    kind TEXT NOT NULL,
    cmd TEXT,
    first_ts TEXT NOT NULL,
    turn INTEGER NOT NULL,
    PRIMARY KEY (session_id, key, kind)
);
CREATE TABLE IF NOT EXISTS touched (
    session_id TEXT NOT NULL,
    key TEXT NOT NULL,
    ts TEXT NOT NULL,
    turn INTEGER NOT NULL,
    PRIMARY KEY (session_id, key)
);
CREATE TABLE IF NOT EXISTS cursor (
    session_id TEXT PRIMARY KEY,
    last_turn_seen INTEGER NOT NULL DEFAULT -1,
    last_prune_ts TEXT
);
CREATE TABLE IF NOT EXISTS capture_stat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    ts TEXT NOT NULL,
    status TEXT NOT NULL,
    surfaced_n INTEGER DEFAULT 0,
    touched_n INTEGER DEFAULT 0,
    ms INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_surfaced_kind_ts ON surfaced(kind, first_ts);
CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON telemetry(ts);
CREATE INDEX IF NOT EXISTS idx_telemetry_cmd ON telemetry(cmd, session_id_prefix);
"""


def now_iso() -> str:
    """UTC timestamp with microseconds — sortable as TEXT, == chronological order."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """Open a read-write WAL connection. Assumes schema already initialized."""
    path = Path(db_path or EFFICACY_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=1.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 1000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def init(db_path: str | None = None) -> sqlite3.Connection:
    """Connect AND ensure schema. Call once per process (or in tests)."""
    conn = connect(db_path)
    ensure_schema(conn)
    return conn


def prune(conn: sqlite3.Connection, retention_days: int, now: str | None = None) -> int:
    """Delete rows older than retention_days (by row timestamp) and VACUUM. Returns rows deleted."""
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=retention_days)
    cutoff = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    deleted = 0
    deleted += conn.execute("DELETE FROM surfaced WHERE first_ts < ?", (cutoff,)).rowcount
    deleted += conn.execute("DELETE FROM touched WHERE ts < ?", (cutoff,)).rowcount
    deleted += conn.execute("DELETE FROM telemetry WHERE ts < ?", (cutoff,)).rowcount
    deleted += conn.execute("DELETE FROM capture_stat WHERE ts < ?", (cutoff,)).rowcount
    conn.commit()
    conn.execute("VACUUM")
    return deleted
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest src/session_recall/tests/test_efficacy_db.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/session_recall/db/efficacy.py src/session_recall/tests/test_efficacy_db.py
git commit -m "feat(efficacy): add WAL sidecar SQLite store with schema and prune"
```

---

## Task 3: Migrate telemetry to SQLite + compat loader

**Files:**
- Modify: `src/session_recall/util/telemetry.py`
- Test (rewrite): `src/session_recall/tests/test_telemetry.py`

- [ ] **Step 1: Rewrite the test for SQLite + compat loader**

Replace the entire contents of `src/session_recall/tests/test_telemetry.py`:

```python
"""Tests for util/telemetry.py — SQLite-backed writer + compat loader."""
import pytest
from session_recall.util import telemetry
from session_recall.db import efficacy


@pytest.fixture
def tmp_db(tmp_path):
    db = str(tmp_path / "eff.db")
    efficacy.init(db).close()  # create schema once
    telemetry.init(db)
    yield db
    telemetry.init(None)


def test_query_hash_normalizes():
    qh = telemetry.query_hash("hello world")
    assert len(qh) == 8
    assert qh == telemetry.query_hash("HELLO   WORLD")


def test_record_writes_row(tmp_db):
    telemetry.record("list", 42, tier=1, session_id="sess-1")
    rows = telemetry.load_entries()
    assert rows[-1]["cmd"] == "list"
    assert rows[-1]["tier"] == 1


def test_record_omits_null_optionals_in_loader(tmp_db):
    """Compat loader must omit NULL optional fields so `'tier' not in e` works."""
    telemetry.record("list", 10)  # no tier
    e = telemetry.load_entries()[-1]
    assert "tier" not in e
    assert "query_hash" not in e
    assert "session_id_prefix" not in e


def test_record_search_query_hash(tmp_db):
    qh = telemetry.query_hash("auth bug")
    telemetry.record("search", 12, tier=2, query_hash=qh)
    assert telemetry.load_entries()[-1]["query_hash"] == qh


def test_record_show_session_prefix(tmp_db):
    telemetry.record("show", 12, tier=3, session_id_prefix="abcd1234")
    assert telemetry.load_entries()[-1]["session_id_prefix"] == "abcd1234"


def test_load_entries_limit_and_order(tmp_db):
    for i in range(10):
        telemetry.record("list", i)
    rows = telemetry.load_entries(limit=3)
    assert len(rows) == 3
    assert [r["duration_ms"] for r in rows] == [7, 8, 9]  # chronological tail


def test_record_silent_when_uninitialized():
    telemetry.init(None)
    telemetry.record("list", 1)  # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest src/session_recall/tests/test_telemetry.py -v`
Expected: FAIL (`load_entries` missing / writer still JSON)

- [ ] **Step 3: Rewrite `util/telemetry.py`**

Replace the entire contents of `src/session_recall/util/telemetry.py`:

```python
"""Telemetry writer + reader — SQLite-backed (sidecar efficacy DB)."""
import hashlib

from ..db import efficacy

_DB_PATH = None

_OPTIONAL = ("tier", "query_hash", "session_id_prefix", "window_tier")


def query_hash(q: str) -> str:
    """8-char sha256 of whitespace-normalized, lowercased query. Not reversible."""
    normalized = " ".join(q.lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:8]


def init(db_path) -> None:
    global _DB_PATH
    _DB_PATH = db_path


def record(cmd: str, duration_ms: int, busy_hits: int = 0, attempts: int = 1,
           rows: int = 0, exit_code: int = 0, schema_ok: bool = True,
           tier: int | None = None, query_hash: str | None = None,
           session_id_prefix: str | None = None, window_tier: str | None = None,
           session_id: str | None = None) -> None:
    """Append a telemetry row. Silent fail — telemetry must never crash the CLI."""
    if not _DB_PATH:
        return
    try:
        conn = efficacy.connect(_DB_PATH)
        try:
            conn.execute(
                "INSERT INTO telemetry (session_id, ts, cmd, duration_ms, busy_hits, "
                "attempts, rows_returned, exit_code, schema_ok, tier, query_hash, "
                "session_id_prefix, window_tier) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (session_id, efficacy.now_iso(), cmd, duration_ms, busy_hits, attempts,
                 rows, exit_code, 1 if schema_ok else 0, tier, query_hash,
                 session_id_prefix, window_tier),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def load_entries(limit: int = 500) -> list[dict]:
    """Return up to `limit` most-recent rows as JSON-shaped dicts (chronological).

    Optional fields that are NULL are OMITTED, preserving the legacy JSON shape so
    consumers using `'tier' not in entry` keep working unchanged.
    """
    if not _DB_PATH:
        return []
    try:
        conn = efficacy.connect(_DB_PATH)
        try:
            rows = conn.execute(
                "SELECT * FROM telemetry ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    out = []
    for r in reversed(rows):  # chronological
        d = {
            "ts": r["ts"], "cmd": r["cmd"], "duration_ms": r["duration_ms"],
            "busy_hits": r["busy_hits"], "attempts": r["attempts"],
            "rows_returned": r["rows_returned"], "exit_code": r["exit_code"],
            "schema_ok": bool(r["schema_ok"]),
        }
        for k in _OPTIONAL:
            if r[k] is not None:
                d[k] = r[k]
        out.append(d)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest src/session_recall/tests/test_telemetry.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/session_recall/util/telemetry.py src/session_recall/tests/test_telemetry.py
git commit -m "feat(efficacy): migrate telemetry to SQLite with compat loader"
```

---

## Task 4: Adapt health dims to the compat loader

**Files:**
- Modify: `src/session_recall/health/dim_disclosure.py`
- Modify: `src/session_recall/health/dim_concurrency.py`
- Test: `src/session_recall/tests/test_health_dims_telemetry.py`

> Read both dim files FIRST. Preserve their exact return-dict keys/status strings; only swap the data source to `telemetry.load_entries()`.

- [ ] **Step 1: Write the failing test**

```python
# src/session_recall/tests/test_health_dims_telemetry.py
from session_recall.util import telemetry
from session_recall.db import efficacy
from session_recall.health import dim_disclosure, dim_concurrency


def _seed(tmp_path):
    db = str(tmp_path / "eff.db")
    efficacy.init(db).close()
    telemetry.init(db)
    return db


def test_disclosure_reads_sqlite(tmp_path):
    _seed(tmp_path)
    telemetry.record("list", 5, tier=1)
    telemetry.record("search", 7, tier=2)
    telemetry.record("list", 5)  # legacy-style (no tier)
    res = dim_disclosure.check()
    assert isinstance(res, dict)
    telemetry.init(None)


def test_concurrency_reads_sqlite(tmp_path):
    _seed(tmp_path)
    telemetry.record("list", 5, busy_hits=0)
    telemetry.record("list", 6, busy_hits=2)
    res = dim_concurrency.check()
    assert isinstance(res, dict)
    telemetry.init(None)
```

> After reading each dim's `check()` contract, tighten these asserts to check the actual status field (e.g. `res["status"] in {...}`).

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest src/session_recall/tests/test_health_dims_telemetry.py -v`
Expected: FAIL (dims still read JSON `TELEMETRY_PATH`; with telemetry pointed at SQLite the JSON path is empty/missing)

- [ ] **Step 3: Update `dim_disclosure.py`**

Change the import `from ..config import TELEMETRY_PATH` to `from ..util import telemetry`, and replace the body of `_load_entries`:

```python
def _load_entries() -> list[dict]:
    try:
        return telemetry.load_entries()
    except Exception:
        return []
```

Leave all downstream logic (`"tier" not in e`, etc.) unchanged — the compat loader omits NULL `tier`, preserving meaning.

- [ ] **Step 4: Update `dim_concurrency.py`**

Replace its direct JSON read (`path = Path(TELEMETRY_PATH)` / `json.loads(...)`) with:

```python
from ..util import telemetry
...
    entries = telemetry.load_entries()
    if not entries:
        return {"status": "info", "detail": "No telemetry data yet",
                "hint": "Run session-recall a few times first"}
    total_busy = sum(e.get("busy_hits", 0) for e in entries)
    busy_rate = (total_busy / len(entries)) * 100
```

Remove now-unused `json`/`Path`/`TELEMETRY_PATH` imports. Preserve the dim's existing return keys/status strings exactly.

- [ ] **Step 5: Run tests to verify they pass (no new regressions)**

Run: `python -m pytest src/session_recall/tests/test_health_dims_telemetry.py src/session_recall/tests/ -k "disclosure or concurrency or health" -v`
Expected: PASS; no new failures vs baseline.

- [ ] **Step 6: Commit**

```bash
git add src/session_recall/health/dim_disclosure.py src/session_recall/health/dim_concurrency.py src/session_recall/tests/test_health_dims_telemetry.py
git commit -m "refactor(health): read telemetry via SQLite compat loader"
```

---

## Task 5: Worktree-stable keys (`util/recall_key.py`)

**Files:**
- Create: `src/session_recall/util/recall_key.py`
- Test: `src/session_recall/tests/test_recall_key.py`

> Verify the import path for `detect_repo_for_cwd` (base: `src/session_recall/util/detect_repo.py`). Adjust the import if the symbol lives elsewhere.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest src/session_recall/tests/test_recall_key.py -v`
Expected: FAIL `ModuleNotFoundError: ... recall_key`

- [ ] **Step 3: Implement `recall_key.py`**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest src/session_recall/tests/test_recall_key.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/session_recall/util/recall_key.py src/session_recall/tests/test_recall_key.py
git commit -m "feat(efficacy): add worktree-stable file/session keys"
```

---

## Task 6: Central capture (`util/capture.py`)

This is the correctness core: closed-turn watermark (D1), strict-after attribution, time-box, silent-fail.

**Files:**
- Create: `src/session_recall/util/capture.py`
- Test: `src/session_recall/tests/test_capture.py`

> Verify the read-only Copilot-store connection helper. Base file: `src/session_recall/db/connect.py`. Use whatever symbol it exports (e.g. `connect_ro(db_path)`); adjust the import in capture.py accordingly.

- [ ] **Step 1: Write the failing test**

```python
# src/session_recall/tests/test_capture.py
import sqlite3
import types

import pytest

from session_recall.db import efficacy
from session_recall.util import capture, recall_key


def _make_copilot_store(path, session_id, turns, files):
    """turns: list[(turn_index, assistant_response)]; files: list[(turn_index, abspath)]."""
    c = sqlite3.connect(path)
    c.executescript(
        "CREATE TABLE sessions(id TEXT, cwd TEXT, repository TEXT);"
        "CREATE TABLE turns(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,"
        " turn_index INTEGER, user_message TEXT, assistant_response TEXT, timestamp TEXT);"
        "CREATE TABLE session_files(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,"
        " file_path TEXT, tool_name TEXT, turn_index INTEGER, first_seen_at TEXT);"
    )
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest src/session_recall/tests/test_capture.py -v`
Expected: FAIL `ModuleNotFoundError: ... capture`

- [ ] **Step 3: Implement `capture.py`**

```python
# src/session_recall/util/capture.py
"""Central, time-boxed, silent-fail capture for recall-efficacy measurement.

Called once per CLI invocation AFTER output has been flushed. Records what the
command surfaced and snapshots newly-used artifacts since a closed-turn watermark.
"""
from __future__ import annotations

import os
import subprocess
import time

from .. import config
from ..db.connect import connect_ro
from ..db import efficacy
from . import recall_key


def _current_root() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=1, cwd=os.getcwd(),
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except Exception:
        pass
    return None


def _closed_turn(ro, session_id: str) -> int:
    row = ro.execute(
        "SELECT MAX(turn_index) AS m FROM turns "
        "WHERE session_id=? AND assistant_response IS NOT NULL AND assistant_response<>''",
        (session_id,),
    ).fetchone()
    m = row["m"] if row is not None else None
    return m if m is not None else -1


def run(args) -> None:
    """Entry point. Never raises; bails silently on budget/errors."""
    if getattr(config, "NO_CAPTURE", False):
        return
    session_id = getattr(config, "AGENT_SESSION_ID", None)
    if not session_id:
        return

    t0 = time.monotonic()
    deadline = t0 + config.CAPTURE_BUDGET_MS / 1000.0
    status = "completed"
    surfaced_n = 0
    touched_n = 0
    conn = None
    ro = None
    try:
        conn = efficacy.connect(config.EFFICACY_DB_PATH)
        ro = connect_ro(config.DB_PATH)
        closed = _closed_turn(ro, session_id)
        ts = efficacy.now_iso()
        root = _current_root()

        cap = getattr(args, "_capture", None) or {}

        # 1) Record surfaced artifacts (deduped; earliest kept by INSERT OR IGNORE).
        for path in cap.get("files", []) or []:
            if time.monotonic() > deadline:
                status = "timeout"
                break
            key = recall_key.file_key(path, current_root=root)
            conn.execute(
                "INSERT OR IGNORE INTO surfaced(session_id,key,kind,cmd,first_ts,turn) "
                "VALUES(?,?,?,?,?,?)",
                (session_id, key, "file", getattr(args, "command", None), ts, closed),
            )
            surfaced_n += 1
        for sid in cap.get("sessions", []) or []:
            key = recall_key.session_key(sid)
            if not key:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO surfaced(session_id,key,kind,cmd,first_ts,turn) "
                "VALUES(?,?,?,?,?,?)",
                (session_id, key, "session", getattr(args, "command", None), ts, closed),
            )
            surfaced_n += 1

        # 2) Snapshot newly-used files since the watermark, up to the closed frontier.
        if status != "timeout":
            cur = conn.execute(
                "SELECT last_turn_seen FROM cursor WHERE session_id=?", (session_id,)
            ).fetchone()
            last_seen = cur["last_turn_seen"] if cur else -1
            used = ro.execute(
                "SELECT file_path, turn_index FROM session_files "
                "WHERE session_id=? AND turn_index>? AND turn_index<=?",
                (session_id, last_seen, closed),
            ).fetchall()
            for r in used:
                if time.monotonic() > deadline:
                    status = "timeout"
                    break
                key = recall_key.file_key(r["file_path"], current_root=root)
                s = conn.execute(
                    "SELECT turn FROM surfaced "
                    "WHERE session_id=? AND key=? AND kind='file'",
                    (session_id, key),
                ).fetchone()
                if s is not None and s["turn"] < r["turn_index"]:
                    conn.execute(
                        "INSERT OR IGNORE INTO touched(session_id,key,ts,turn) "
                        "VALUES(?,?,?,?)",
                        (session_id, key, ts, r["turn_index"]),
                    )
                    touched_n += 1

            # 3) Advance the watermark — forward only.
            conn.execute(
                "INSERT INTO cursor(session_id,last_turn_seen) VALUES(?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET last_turn_seen=excluded.last_turn_seen "
                "WHERE excluded.last_turn_seen>cursor.last_turn_seen",
                (session_id, closed),
            )

        # 4) Lazy retention (once/day).
        _maybe_prune(conn, session_id, ts)

        ms = int((time.monotonic() - t0) * 1000)
        conn.execute(
            "INSERT INTO capture_stat(session_id,ts,status,surfaced_n,touched_n,ms) "
            "VALUES(?,?,?,?,?,?)",
            (session_id, ts, status, surfaced_n, touched_n, ms),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        try:
            if ro is not None:
                ro.close()
        except Exception:
            pass
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _maybe_prune(conn, session_id: str, ts: str) -> None:
    try:
        row = conn.execute(
            "SELECT last_prune_ts FROM cursor WHERE session_id=?", (session_id,)
        ).fetchone()
        last = row["last_prune_ts"] if row else None
        if last and last[:10] == ts[:10]:
            return  # already pruned today (compare YYYY-MM-DD)
        efficacy.prune(conn, config.RETENTION_DAYS, now=ts)
        conn.execute(
            "INSERT INTO cursor(session_id,last_turn_seen,last_prune_ts) VALUES(?,-1,?) "
            "ON CONFLICT(session_id) DO UPDATE SET last_prune_ts=excluded.last_prune_ts",
            (session_id, ts),
        )
    except Exception:
        pass
```

> If `db/connect.py` exposes a different read-only helper name (e.g. `get_connection`), import that instead. The connection must yield `sqlite3.Row` rows (capture uses `r["file_path"]`). If it returns tuple rows, set `ro.row_factory = sqlite3.Row` after connecting.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest src/session_recall/tests/test_capture.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/session_recall/util/capture.py src/session_recall/tests/test_capture.py
git commit -m "feat(efficacy): add closed-turn capture with strict attribution"
```

---

## Task 7: Command capture stashes

Each surfacing command stores raw identities in `args._capture` BEFORE calling `output(...)`. Resolution stays central (capture.py).

**Files:**
- Modify: `src/session_recall/commands/files.py`
- Modify: `src/session_recall/commands/list_sessions.py`
- Modify: `src/session_recall/commands/search.py`
- Modify: `src/session_recall/commands/checkpoints.py`
- Test: `src/session_recall/tests/test_capture_stashes.py`

> Read each command's `run()` first to confirm the local variable names (`files`, `sessions`, `results`, `checkpoints`) and the exact pre-`output(...)` insertion point. The test below mirrors `files.py`; adjust monkeypatch targets to the real provider accessor used by that file.

- [ ] **Step 1: Write the failing test**

```python
# src/session_recall/tests/test_capture_stashes.py
import types
from session_recall.commands import files as files_cmd


def test_files_sets_capture(monkeypatch):
    captured = {}
    monkeypatch.setattr(files_cmd, "output", lambda data, **k: captured.update(data=data))
    # Adjust this monkeypatch to the actual provider accessor in files.py:
    monkeypatch.setattr(files_cmd, "get_active_providers",
                        lambda **k: [types.SimpleNamespace(
                            provider_id="cli",
                            schema_problems=lambda: [],
                            recent_files=lambda **kk: [
                                {"file_path": "/a/b.py"}, {"file_path": "/c/d.py"}])])
    args = types.SimpleNamespace(command="files", json=True, repo=None,
                                 limit=10, days=None, provider="all")
    files_cmd.run(args)
    assert args._capture == {"files": ["/a/b.py", "/c/d.py"]}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest src/session_recall/tests/test_capture_stashes.py -v`
Expected: FAIL (`AttributeError: ... '_capture'`)

- [ ] **Step 3: Add the stash lines**

In `files.py`, immediately BEFORE the `output(` call:

```python
    args._capture = {"files": [f["file_path"] for f in files if f.get("file_path")]}
```

In `list_sessions.py`, immediately before its `output(` call:

```python
    args._capture = {"sessions": [s.get("id_full") or s.get("id")
                                  for s in sessions if (s.get("id_full") or s.get("id"))]}
```

In `search.py`, immediately before the results `output(` call (non-empty branch):

```python
    args._capture = {"sessions": [r.get("session_id_full")
                                  for r in results if r.get("session_id_full")]}
```

In `checkpoints.py`, immediately before its `output(` call:

```python
    args._capture = {"sessions": [c.get("session_id")
                                  for c in checkpoints if c.get("session_id")]}
```

> `show_session.py` is intentionally NOT stashed in v1: a `show` is the *escalation event*, captured via telemetry (`session_id_prefix`). Surfacing files-within-show is deferred (documented future work).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest src/session_recall/tests/test_capture_stashes.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite (no new regressions)**

Run: `python -m pytest -q`
Expected: same 8 pre-existing failures; everything else passing.

- [ ] **Step 6: Commit**

```bash
git add src/session_recall/commands/files.py src/session_recall/commands/list_sessions.py src/session_recall/commands/search.py src/session_recall/commands/checkpoints.py src/session_recall/tests/test_capture_stashes.py
git commit -m "feat(efficacy): stash surfaced identities for central capture"
```

---

## Task 8: Wire capture + telemetry into `__main__.py`

**Files:**
- Modify: `src/session_recall/__main__.py`
- Test: `src/session_recall/tests/e2e/test_efficacy_wiring.py`

> Read `__main__.py` first. Match the EXISTING `telemetry.record(...)` call signature and the existing subparser/dispatch style. The snippets below are the shape; adapt arg names to what's actually there.

- [ ] **Step 1: Write the failing test (subprocess e2e)**

```python
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
```

> Confirm the env var name the base uses for the Copilot store path (likely `SESSION_RECALL_DB`, mapped to `config.DB_PATH`). Fix the test/env if it differs.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest src/session_recall/tests/e2e/test_efficacy_wiring.py -v`
Expected: FAIL (no `capture_stat` rows; telemetry still JSON / efficacy DB not initialized)

- [ ] **Step 3: Edit `__main__.py`**

3a. Imports near the top:

```python
from .config import EFFICACY_DB_PATH, AGENT_SESSION_ID
from .util import telemetry, capture
from .db import efficacy
```

(Remove the old `from .config import TELEMETRY_PATH` import if present.)

3b. Replace `telemetry.init(TELEMETRY_PATH)` at the start of `main()` with schema-init + telemetry pointer:

```python
    efficacy.init(EFFICACY_DB_PATH).close()  # ensure schema once per process
    telemetry.init(EFFICACY_DB_PATH)
```

3c. Register the four new subparsers (beside existing `add_parser` calls):

```python
    p_eff = sub.add_parser("efficacy", help="Recall efficacy report (recalled-then-used rate)")
    p_eff.add_argument("--days", type=int, default=30)
    p_eff.add_argument("--repo", default=None)
    p_eff.add_argument("--session", default=None)
    p_eff.add_argument("--json", action="store_true")

    p_prune = sub.add_parser("prune", help="Delete efficacy data older than retention window")
    p_prune.add_argument("--json", action="store_true")

    p_stats = sub.add_parser("stats", help="Telemetry usage summary")
    p_stats.add_argument("--json", action="store_true")

    p_doctor = sub.add_parser("doctor", help="Telemetry/store health check")
    p_doctor.add_argument("--json", action="store_true")
```

3d. Dispatch branches (beside the existing `elif args.command == ...`):

```python
    elif args.command == "efficacy":
        from .commands.efficacy import run
        exit_code = run(args)
    elif args.command == "prune":
        from .commands.prune import run
        exit_code = run(args)
    elif args.command == "stats":
        from .commands.stats import run
        exit_code = run(args)
    elif args.command == "doctor":
        from .commands.doctor import run
        exit_code = run(args)
```

3e. Add the four commands to `TIER_MAP` as tier 0:

```python
    "efficacy": 0,
    "prune": 0,
    "stats": 0,
    "doctor": 0,
```

3f. Pass the live session id into the existing `telemetry.record(...)` call (add `session_id=AGENT_SESSION_ID,`) and run capture AFTER it, before the process exits:

```python
    telemetry.record(
        ...existing kwargs...,
        session_id=AGENT_SESSION_ID,
    )
    try:
        sys.stdout.flush()
    except Exception:
        pass
    capture.run(args)
```

> `args._capture` is unset for non-surfacing commands; `capture.run` reads it via `getattr(args, "_capture", None)` and treats missing as empty.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest src/session_recall/tests/e2e/test_efficacy_wiring.py -v`
Expected: PASS

- [ ] **Step 5: Full suite check**

Run: `python -m pytest -q`
Expected: no new failures beyond the 8 pre-existing.

- [ ] **Step 6: Commit**

```bash
git add src/session_recall/__main__.py src/session_recall/tests/e2e/test_efficacy_wiring.py
git commit -m "feat(efficacy): wire telemetry+capture and register efficacy/prune/stats/doctor"
```

---

## Task 9: The `efficacy` command (analyzer/report)

Metrics (all weight 1.0 in v1; Referenced deferred):
- File-recall efficacy = file_hits / unique_file_surfaces (Touched).
- Session-recall efficacy = escalated / unique_session_surfaces (report-time join).
- Blended = (file_hits + session_hits) / (file_surfaces + session_surfaces).
- Insufficient-data guard: total unique surfaces < 20 → `INSUFFICIENT DATA (n/20)`.
- Per-session macro table, worst-first, cap 10. Capture-health footer.

**Files:**
- Create: `src/session_recall/commands/efficacy.py`
- Test: `src/session_recall/tests/test_efficacy_cmd.py`

> Confirm `output(data, json_mode=...)`'s real signature in `util/format_output.py` and match it. If it's `output(data, args)` or similar, adapt the calls below.

- [ ] **Step 1: Write the failing test**

```python
# src/session_recall/tests/test_efficacy_cmd.py
import io
import json
import types
from contextlib import redirect_stdout

from session_recall.db import efficacy
from session_recall.commands import efficacy as eff_cmd


def _seed(db):
    conn = efficacy.init(db)
    ts = "2099-01-01T00:00:00.000000Z"
    later = "2099-01-01T00:00:05.000000Z"
    for i, hit in [(1, True), (2, True), (3, False)]:
        conn.execute("INSERT INTO surfaced VALUES('sess-1',?, 'file','files',?,0)", (f"f{i}", ts))
        if hit:
            conn.execute("INSERT INTO touched VALUES('sess-1',?,?,1)", (f"f{i}", later))
    conn.execute("INSERT INTO surfaced VALUES('sess-1','aaaa1111','session','list',?,0)", (ts,))
    conn.execute("INSERT INTO surfaced VALUES('sess-1','bbbb2222','session','list',?,0)", (ts,))
    conn.execute("INSERT INTO telemetry(session_id,ts,cmd,session_id_prefix) "
                 "VALUES('sess-9',?, 'show','aaaa1111')", (later,))
    conn.execute("INSERT INTO capture_stat(session_id,ts,status) VALUES('sess-1',?, 'completed')", (ts,))
    conn.commit()
    conn.close()


def _run(db, **kw):
    args = types.SimpleNamespace(days=3650, repo=None, session=None, json=True)
    for k, v in kw.items():
        setattr(args, k, v)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = eff_cmd.run(args, db_path=db)
    return rc, json.loads(buf.getvalue())


def test_rates_and_counts(tmp_path):
    db = str(tmp_path / "eff.db")
    _seed(db)
    rc, out = _run(db)
    assert rc == 0
    assert out["file_recall"]["hits"] == 2
    assert out["file_recall"]["surfaces"] == 3
    assert out["session_recall"]["hits"] == 1
    assert out["session_recall"]["surfaces"] == 2
    assert abs(out["blended"]["rate"] - 0.6) < 1e-9  # (2+1)/(3+2)


def test_insufficient_data_guard(tmp_path):
    db = str(tmp_path / "eff.db")
    efficacy.init(db).close()
    rc, out = _run(db)
    assert out["status"] == "insufficient_data"
    assert out["unique_surfaces"] == 0


def test_strict_ordering_excludes_pre_surface_escalation(tmp_path):
    db = str(tmp_path / "eff.db")
    conn = efficacy.init(db)
    conn.execute("INSERT INTO surfaced VALUES('s','cccc3333','session','list','2099-01-01T00:00:05.000000Z',0)")
    conn.execute("INSERT INTO telemetry(session_id,ts,cmd,session_id_prefix) "
                 "VALUES('s','2099-01-01T00:00:01.000000Z','show','cccc3333')")
    conn.commit(); conn.close()
    rc, out = _run(db)
    assert out["session_recall"]["hits"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest src/session_recall/tests/test_efficacy_cmd.py -v`
Expected: FAIL `ModuleNotFoundError: ... commands.efficacy`

- [ ] **Step 3: Implement `commands/efficacy.py`**

```python
# src/session_recall/commands/efficacy.py
"""Recall efficacy report — recalled-then-used rate (Touched + Escalated)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..config import EFFICACY_DB_PATH
from ..db import efficacy
from ..util.format_output import output

_ESCALATE_CMDS = ("show", "export", "diff")
_MIN_SURFACES = 20


def _cutoff(days: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _filters(args):
    clauses, params = [], []
    if getattr(args, "session", None):
        clauses.append("s.session_id = ?")
        params.append(args.session)
    return clauses, params


def _counts(conn, cutoff, args):
    fclauses, fparams = _filters(args)
    where = " AND ".join(["s.first_ts >= ?"] + fclauses)
    base = [cutoff] + fparams

    file_surfaces = conn.execute(
        f"SELECT COUNT(*) FROM surfaced s WHERE s.kind='file' AND {where}", base
    ).fetchone()[0]
    file_hits = conn.execute(
        f"SELECT COUNT(*) FROM surfaced s WHERE s.kind='file' AND {where} "
        "AND EXISTS(SELECT 1 FROM touched t WHERE t.session_id=s.session_id AND t.key=s.key)",
        base,
    ).fetchone()[0]
    sess_surfaces = conn.execute(
        f"SELECT COUNT(*) FROM surfaced s WHERE s.kind='session' AND {where}", base
    ).fetchone()[0]
    placeholders = ",".join("?" * len(_ESCALATE_CMDS))
    sess_hits = conn.execute(
        f"SELECT COUNT(*) FROM surfaced s WHERE s.kind='session' AND {where} "
        f"AND EXISTS(SELECT 1 FROM telemetry t WHERE t.cmd IN ({placeholders}) "
        "AND t.session_id_prefix=s.key AND t.ts > s.first_ts)",
        base + list(_ESCALATE_CMDS),
    ).fetchone()[0]
    return file_surfaces, file_hits, sess_surfaces, sess_hits


def _rate(hits, surfaces):
    return (hits / surfaces) if surfaces else 0.0


def _macro(conn, cutoff, args):
    rows = conn.execute(
        "SELECT DISTINCT session_id FROM surfaced WHERE first_ts >= ?", (cutoff,)
    ).fetchall()
    table = []
    for r in rows:
        sub = type(args)(**{**vars(args), "session": r["session_id"]})
        fs, fh, ss, sh = _counts(conn, cutoff, sub)
        surf = fs + ss
        if surf == 0:
            continue
        table.append({
            "session_id": r["session_id"],
            "surfaces": surf,
            "hits": fh + sh,
            "rate": _rate(fh + sh, surf),
        })
    table.sort(key=lambda x: (x["rate"], -x["surfaces"]))
    return table[:10]


def run(args, db_path: str | None = None) -> int:
    db = db_path or EFFICACY_DB_PATH
    conn = efficacy.init(db)
    try:
        cutoff = _cutoff(getattr(args, "days", 30))
        fs, fh, ss, sh = _counts(conn, cutoff, args)
        total = fs + ss
        cap = conn.execute(
            "SELECT COUNT(*) AS runs, "
            "SUM(CASE WHEN status='timeout' THEN 1 ELSE 0 END) AS timeouts "
            "FROM capture_stat WHERE ts >= ?", (cutoff,)
        ).fetchone()
        capture_health = {"runs": cap["runs"] or 0, "timeouts": cap["timeouts"] or 0}

        if total < _MIN_SURFACES:
            output({
                "status": "insufficient_data",
                "unique_surfaces": total,
                "needed": _MIN_SURFACES,
                "window_days": getattr(args, "days", 30),
                "capture_health": capture_health,
            }, json_mode=getattr(args, "json", False))
            return 0

        report = {
            "status": "ok",
            "window_days": getattr(args, "days", 30),
            "blended": {"rate": _rate(fh + sh, fs + ss), "hits": fh + sh, "surfaces": fs + ss},
            "file_recall": {"rate": _rate(fh, fs), "hits": fh, "surfaces": fs},
            "session_recall": {"rate": _rate(sh, ss), "hits": sh, "surfaces": ss},
            "per_session_worst": _macro(conn, cutoff, args),
            "capture_health": capture_health,
        }
        output(report, json_mode=getattr(args, "json", False))
        return 0
    finally:
        conn.close()
```

> `type(args)(**{**vars(args), ...})` clones a `SimpleNamespace`/`argparse.Namespace` with one field overridden. Both support `vars()` and keyword construction.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest src/session_recall/tests/test_efficacy_cmd.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/session_recall/commands/efficacy.py src/session_recall/tests/test_efficacy_cmd.py
git commit -m "feat(efficacy): add efficacy report command"
```

---

## Task 10: The `prune` command

**Files:**
- Create: `src/session_recall/commands/prune.py`
- Test: `src/session_recall/tests/test_prune.py`

- [ ] **Step 1: Write the failing test**

```python
# src/session_recall/tests/test_prune.py
import io
import json
import types
from contextlib import redirect_stdout

from session_recall.db import efficacy
from session_recall.commands import prune as prune_cmd


def test_prune_removes_old_and_reports(tmp_path):
    db = str(tmp_path / "eff.db")
    conn = efficacy.init(db)
    conn.execute("INSERT INTO surfaced VALUES('s','k','file','files','2000-01-01T00:00:00.000000Z',0)")
    conn.execute("INSERT INTO surfaced VALUES('s','k2','file','files',?,0)", (efficacy.now_iso(),))
    conn.commit(); conn.close()
    args = types.SimpleNamespace(json=True)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = prune_cmd.run(args, db_path=db)
    out = json.loads(buf.getvalue())
    assert rc == 0
    assert out["deleted"] >= 1
    conn = efficacy.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM surfaced").fetchone()[0] == 1
    conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest src/session_recall/tests/test_prune.py -v`
Expected: FAIL `ModuleNotFoundError: ... commands.prune`

- [ ] **Step 3: Implement `commands/prune.py`**

```python
# src/session_recall/commands/prune.py
"""Manually prune efficacy data older than the retention window."""
from __future__ import annotations

from ..config import EFFICACY_DB_PATH, RETENTION_DAYS
from ..db import efficacy
from ..util.format_output import output


def run(args, db_path: str | None = None) -> int:
    conn = efficacy.init(db_path or EFFICACY_DB_PATH)
    try:
        deleted = efficacy.prune(conn, RETENTION_DAYS)
        output({"deleted": deleted, "retention_days": RETENTION_DAYS},
               json_mode=getattr(args, "json", False))
        return 0
    finally:
        conn.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest src/session_recall/tests/test_prune.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/session_recall/commands/prune.py src/session_recall/tests/test_prune.py
git commit -m "feat(efficacy): add manual prune command"
```

---

## Task 11: `stats` and `doctor` commands (telemetry usage + health)

Borrowed concepts (theidledeveloper/auto-memory, MIT), re-implemented on the SQLite backend via `telemetry.load_entries()`. `stats` = usage; `doctor` = quick health.

**Files:**
- Create: `src/session_recall/commands/stats.py`
- Create: `src/session_recall/commands/doctor.py`
- Test: `src/session_recall/tests/test_stats_doctor.py`

- [ ] **Step 1: Write the failing test**

```python
# src/session_recall/tests/test_stats_doctor.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest src/session_recall/tests/test_stats_doctor.py -v`
Expected: FAIL (modules missing)

- [ ] **Step 3: Implement `commands/stats.py`**

```python
# src/session_recall/commands/stats.py
"""Telemetry usage summary (adapted from theidledeveloper/auto-memory, MIT)."""
from __future__ import annotations

from collections import defaultdict

from ..config import EFFICACY_DB_PATH
from ..util import telemetry
from ..util.format_output import output


def run(args, db_path: str | None = None) -> int:
    telemetry.init(db_path or EFFICACY_DB_PATH)
    entries = telemetry.load_entries(limit=500)
    agg = defaultdict(lambda: {"count": 0, "total_ms": 0})
    for e in entries:
        a = agg[e.get("cmd", "?")]
        a["count"] += 1
        a["total_ms"] += int(e.get("duration_ms") or 0)
    by_command = [
        {"cmd": cmd, "count": v["count"],
         "avg_ms": round(v["total_ms"] / v["count"], 1) if v["count"] else 0}
        for cmd, v in sorted(agg.items())
    ]
    output({"total": len(entries), "by_command": by_command},
           json_mode=getattr(args, "json", False))
    return 0
```

- [ ] **Step 4: Implement `commands/doctor.py`**

```python
# src/session_recall/commands/doctor.py
"""Quick telemetry/store health (adapted from theidledeveloper/auto-memory, MIT)."""
from __future__ import annotations

from ..config import EFFICACY_DB_PATH
from ..util import telemetry
from ..util.format_output import output


def run(args, db_path: str | None = None) -> int:
    telemetry.init(db_path or EFFICACY_DB_PATH)
    entries = telemetry.load_entries(limit=500)
    rows = len(entries)
    errors = sum(1 for e in entries if int(e.get("exit_code") or 0) != 0)
    busy = sum(int(e.get("busy_hits") or 0) for e in entries)
    status = "ok"
    if rows == 0:
        status = "info"
    elif errors / rows > 0.2 or busy / rows > 0.5:
        status = "warn"
    output({"status": status, "telemetry_rows": rows,
            "error_rows": errors, "busy_hits": busy},
           json_mode=getattr(args, "json", False))
    return 0
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest src/session_recall/tests/test_stats_doctor.py -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Commit**

```bash
git add src/session_recall/commands/stats.py src/session_recall/commands/doctor.py src/session_recall/tests/test_stats_doctor.py
git commit -m "feat(efficacy): add stats and doctor commands on SQLite telemetry"
```

---

## Task 12: End-to-end integration + concurrency

**Files:**
- Test: `src/session_recall/tests/e2e/test_efficacy_e2e.py`

- [ ] **Step 1: Write the integration test (full hook → report)**

```python
# src/session_recall/tests/e2e/test_efficacy_e2e.py
import json
import os
import sqlite3
import subprocess
import sys
import threading

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
    )
    c.execute("INSERT INTO sessions VALUES('sess-1','/x','acme/x','main','s','2099-01-01T00:00:00Z','2099-01-01T00:00:00Z','local')")
    c.execute("INSERT INTO turns(session_id,turn_index,assistant_response) VALUES('sess-1',0,'a')")
    c.execute("INSERT INTO session_files(session_id,file_path,turn_index,first_seen_at) "
              "VALUES('sess-1','/x/used.py',0,'2099-01-01T00:00:00Z')")
    c.commit(); c.close()


def _cli(store, eff, *cmd):
    env = dict(os.environ)
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
    r = _cli(store, eff, "efficacy", "--days", "3650", "--json")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["status"] in {"ok", "insufficient_data"}
```

- [ ] **Step 2: Run test to verify it passes**

Run: `python -m pytest src/session_recall/tests/e2e/test_efficacy_e2e.py -v`
Expected: PASS (2 tests) — WAL handles concurrent writers; `integrity_check` returns `ok`.

- [ ] **Step 3: Full suite final check**

Run: `python -m pytest -q`
Expected: all new tests pass; only the 8 pre-existing stale-fixture failures remain.

- [ ] **Step 4: Commit**

```bash
git add src/session_recall/tests/e2e/test_efficacy_e2e.py
git commit -m "test(efficacy): end-to-end + WAL concurrency coverage"
```

---

## Task 13: Docs + attribution

**Files:**
- Modify: `README.md` (or the tool's command reference).

- [ ] **Step 1: Add a "Recall efficacy" section** documenting:

```text
session-recall efficacy [--days N] [--repo owner/repo] [--session ID] [--json]
session-recall prune [--json]
session-recall stats [--json]
session-recall doctor [--json]
```

- New env vars: `SESSION_RECALL_EFFICACY_DB`, `SESSION_RECALL_RETENTION_DAYS` (90), `SESSION_RECALL_CAPTURE_BUDGET_MS` (150), `SESSION_RECALL_NO_CAPTURE` (opt-out).
- Capture is automatic, time-boxed (~150 ms), silent-fail.
- v1 limitations: Referenced signal deferred; Touched measures first-use-after-surface (not reuse); session escalation is *correlation*; deleted-worktree paths degrade to abspath keys.

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs(efficacy): document efficacy subsystem, env vars, and limitations"
```

---

## Self-review checklist (run before execution)

1. **Spec coverage** — Surfaced/Touched/Escalated (Tasks 6,9); strict attribution (Task 6 D1, Task 9 ordering test); worktree-stable keys (Task 5); capture-time snapshot + watermark (Task 6); unified SQLite + telemetry migration (Tasks 2–4); retention/prune (Tasks 2,6,10); `efficacy` command with blended + per-track rates + counts + macro table + insufficient-data guard (Task 9); borrow doctor/stats (Task 11). **Deferred (documented):** Referenced signal, files-within-`show` surfacing — noted in Task 13.
2. **Placeholder scan** — every code step contains complete code; no TBD/TODO.
3. **Type consistency** — `efficacy.now_iso()`, `efficacy.connect/init/prune`, `telemetry.record(..., session_id=)`, `telemetry.load_entries(limit=)`, `recall_key.file_key(..., current_root=)`, `recall_key.session_key()`, `capture.run(args)`, `commands.*.run(args, db_path=None)` used consistently.
4. **Base-code verification gates** (do these while executing, not assume): real `output()` signature; real `db/connect.py` read-only helper name + row factory; real `telemetry.record()` call site kwargs in `__main__.py`; real provider accessor in `files.py`; `detect_repo` import path; the Copilot-store env var name.

## Future work (explicitly out of v1 scope)

- **Referenced** signal (path-fragment text matching, weight 0.4) with self-reference exclusion.
- **Files-within-`show`** surfacing for the file track.
- **Last-turn uses** via a session-end hook.
- **Persistent `dir_repo_cache`** table if cold-cache git resolution exceeds budget in practice.
