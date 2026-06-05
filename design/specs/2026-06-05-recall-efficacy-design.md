# Recall Efficacy Measurement — Design

**Date:** 2026-06-05
**Status:** Approved design (pre-implementation)
**Repo:** `ayackel/auto-memory` (fork of `dezgit2025/auto-memory`, MIT)

## Problem

We want to answer one question: **is `session-recall` actually helping?**

The existing tool, and the most substantive upstream fork (`theidledeveloper`),
instrument *tool usage* — which CLI command ran, at which "tier," how fast. That
measures activity, not value. It cannot tell us whether the context recall
surfaced was actually used.

This design adds a **recall-efficacy** measurement: of the artifacts recall
surfaces, what fraction does the session actually use — measured by correlating
what recall returned against what the session subsequently did.

## Scope

**Build on a clean base.** The fork sits on current upstream HEAD. We *borrow the
design* of the proven plumbing and *build* the efficacy subsystem on top.

- **Borrow (re-implemented on SQLite):** per-invocation telemetry (field design
  from `theidledeveloper` — `tier`, `query_hash`, `rows`, etc.), plus the
  `doctor` and `stats` commands.
- **Build:** worktree-stable key resolution, a capture-time snapshot subsystem,
  and an `efficacy` command.
- **Skip:** `theidledeveloper`'s `calibrate` + `dim_disclosure` scoring model. It
  scores tier-usage consistency (not efficacy), its transition logic ignores
  session boundaries, and its JSON writer has a read-modify-write race. We keep
  the telemetry *schema* idea, not that model.

## Core Concept: efficacy as recalled-then-used rate

Recall surfaces artifacts (files and sessions). A surfaced artifact is a **hit**
if the session subsequently *used* it. Two artifact tracks, three signals:

| Track | Surfaced by | Hit signal | Weight |
|-------|-------------|------------|--------|
| File | `files`, files-within-`show` | **Touched** — surfaced file path later first-appears in `session_files` | 1.0 |
| File | (same) | **Referenced** — surfaced file's ≥2-segment path fragment appears in later turn text | 0.4 |
| Session | `list`, `search`, `checkpoints` | **Escalated** — a later `show`/`export`/`diff` runs against the surfaced session-id | 1.0 |

`Touched` **subsumes** `Referenced` (a file that is both counts once, at 1.0 — max, never sum).

### Strict causal attribution

A hit requires the use to occur **strictly after** the artifact was first
surfaced (`use > first_surfaced`). Recall surfaces *recently touched* artifacts,
so without this rule we would credit recall for uses that predate it. Cross-session
uses are excluded by session scoping; the strict rule handles the intra-session case.

## Key Scheme — worktree-stable

Absolute paths break under git worktrees (same logical file at
`/projects/foo/src/main.py` and `/projects/foo-pr/src/main.py`). The correlation
key must be worktree-invariant.

- **Repo file:** `sha256(repo_id + "\0" + repo_relative_path)[:8]`
  - `repo_id`: layered resolution — live `detect_repo_for_cwd(dir)` →
    `sessions.repository` → absolute-path fallback. (Both store columns are often
    `None`, so live detection is primary.)
  - `repo_relative_path`: path relative to the worktree root. Identical across
    worktrees of the same repo. `repo_id` (origin `owner/repo`) is shared by all
    worktrees, so the key collides correctly across them.
- **Non-repo file** (dotfiles, etc.): `sha256(normpath(absolute_path))[:8]`.
- All keys are **hashed** — the sidecar never stores raw paths. Resolution is
  **memoized per directory** (≈1 git call per unique dir, not per file).

The **worktree root** for the current session comes from `os.getcwd()` resolved
to git toplevel **at capture time** (the CLI runs inside the session's worktree).
`sessions.cwd` is unreliable (often `None`) and is not trusted.

## Capture-Time Snapshot Model

Live path resolution is unstable across time: worktrees are ephemeral, so the
same path resolves differently (or not at all) at recall-time vs. a later
post-hoc analysis. Therefore **keys are resolved once, at capture time, while the
worktree is alive, and persisted.** The analyzer only ever compares
stored-keys to stored-keys — it never re-derives from live paths.

Recall runs **first on every prompt**, so capture piggybacks on it. Each
invocation, tagged with `COPILOT_AGENT_SESSION_ID` (verified equal to
`sessions.id`):

1. **Record surfaced keys** for what this command returned (resolved live).
2. **Snapshot newly-used artifacts** since the per-session watermark
   (`cursor.last_turn_seen`): files first-seen in `session_files`, ≥2-segment
   path tokens extracted from new turn text, and `show`/`export`/`diff`
   invocations from telemetry.
3. **Advance the watermark.**

`session_files` stores **one row per (session, file)** (first-seen only), so
`Touched` = a surfaced file whose first-seen `turn_index` > `surfaced_turn`
(where `surfaced_turn = MAX(turn_index)` at recall time). Re-touches of
already-open files are unmeasurable and, under strict attribution, correctly
excluded.

### Referenced — precise, hash-preserving

Bare basenames are unusable (`plan.md` ×67, `README.md` ×33; 24% of basenames map
to multiple files). So `Referenced`:

- Matches only **≥2-segment path fragments** (`foo/plan.md`, `src/client.py`) —
  never bare basenames.
- Preserves hash-only storage by **inverting the match**: extract path-like
  tokens from *new turn text*, normalize to repo-relative keys (using the current
  session's repo context), hash, and compare to stored surfaced hashes. Only the
  matched hash is persisted.
- Excludes text that merely echoes recall's own output (self-reference).
- Kept at low weight (0.4) and documented as a lower-confidence signal.

### Escalated — telemetry-only

Self-contained in our telemetry: surfaced session-id (sidecar) vs. a later
`show`/`export`/`diff` invocation (telemetry already records `session_id_prefix`).
Match on 8-char session-id prefix (UUID collisions negligible). Strict ordering by
invocation wall-clock timestamp — handles same-turn `list`→`show` sequences.

## Storage — unified SQLite sidecar

One SQLite DB (`~/.copilot/scripts/session-recall-efficacy.db`, WAL mode). WAL
eliminates the parallel-write corruption that the borrowed JSON ring-buffer
suffers (recall fires on every prompt across possibly-concurrent sessions).

Tables (indicative):
- `telemetry(session_id, ts, cmd, tier, query_hash, rows, duration_ms, exit_code, ...)`
- `surfaced(session_id, key, kind, tier, cmd, first_ts, turn)` — `kind` ∈ {file, session}
- `touched(session_id, key, ts, turn)`
- `referenced(session_id, key, ts, turn)`
- `escalated(session_id, target_session_prefix, ts, turn)`
- `cursor(session_id, last_turn_seen, last_prune_ts)`

Surfaced keys are **deduped to unique per session**; `first_ts`/`turn` = earliest.

## Capture Timing & Performance

Capture is on the hot path (every prompt), so:

- **Output-first:** print recall results and flush stdout *before* capturing — the
  agent's visible result is never delayed by capture.
- **Time-boxed:** capture runs under a hard wall-clock budget (≈150 ms); over
  budget, it bails silently.
- **Silent-fail:** capture must never crash *or block* the CLI past budget.
- **Memoized** git resolution; steady-state work is bounded by new-turns-since-watermark (usually 1).
- **Opt-out:** `SESSION_RECALL_NO_CAPTURE=1` disables capture entirely (default on).

## Retention

- **Age-based prune**, default 90 days (`SESSION_RECALL_RETENTION_DAYS`),
  pruning by **session age** so each session's records stay consistent.
- **Lazy execution** — at most once/day via `cursor.last_prune_ts`, never inside
  the hot-path budget.
- Manual `prune` command (concept borrowed from `osamarehman`) + `VACUUM`.
- No ring-buffer/fixed-count eviction on efficacy rows (would truncate mid-session
  and corrupt a session's hit math).

## The `efficacy` Command

Separate from `stats` (`stats` = usage; `efficacy` = outcome).

- **Flags:** `--days N` (default 30), `--repo <owner/repo>`, `--session <id>`, `--json`.
- **Headline (pooled / micro-average over the window):**
  - **Blended** = `(Σ file weights + Σ session weights) / (#file surfaces + #session surfaces)`
  - **File-recall efficacy** = `Σ file weights / #unique file surfaces`
  - **Session-recall efficacy** = `#escalated / #unique session surfaces`
  - Each rate printed **with raw counts** (`62% — 31/50`).
- **Also emitted:** Touched-vs-Referenced split inside file-recall; per-session
  rates (macro), sorted **worst-first**, capped ~10 rows.
- **Insufficient-data guard:** below 20 unique surfaces in the window, report
  `INSUFFICIENT DATA (n/20)` rather than a misleading rate.
- **No auto-verdict:** v1 reports numbers + sample sizes; the human judges.

## Components / Files

**Borrow (re-implement on SQLite, keep field/UX design):**
- `util/telemetry.py` → SQLite-backed writer (drop the JSON ring buffer + its race)
- `commands/doctor.py`, `commands/stats.py` → adopt; adapt readers to SQLite

**Build:**
- `util/recall_key.py` — worktree-stable key resolution (+ per-dir memoization)
- `util/capture.py` — central capture: resolve surfaced keys, snapshot used
  artifacts since watermark, time-boxed + silent-fail; called once from `__main__`
- `db/efficacy.py` — sidecar schema/init/connection (WAL), prune
- `commands/efficacy.py` — the analyzer/report
- `commands/prune.py` — manual prune + VACUUM

**Wire:**
- `__main__.py` — after output flush: `telemetry.record(...)` + `capture.run(args)`
- Each query command sets `args._capture = {"files": [...]} / {"sessions": [...]}`
  (and `show` sets both) — raw identities only; resolution is central.

## Borrow / Build / Skip summary

| Item | Disposition |
|------|-------------|
| Telemetry field design (tier, query_hash, rows…) | Borrow (re-impl on SQLite) |
| `doctor`, `stats` | Borrow |
| `calibrate`, `dim_disclosure` scoring | Skip |
| Worktree-stable keys, capture, efficacy, sidecar, prune | Build |

## Limitations (documented, not engineered around in v1)

- **Last-turn uses** aren't snapshotted (no subsequent invocation). ≤1 turn loss;
  closeable later with a session-end hook.
- A recalled file whose **origin worktree was already deleted**, then used under a
  *different* live worktree, can miss (no way to reconstruct a vanished path's
  repo-relative key). Degrades to abspath matching; same-worktree case still hits.
- **Referenced** only catches mentions with path context, and token-to-repo
  normalization assumes the current session's repo — cross-repo mentions may miss.
  Hence the low weight.
- **Flush-timing:** assumes Copilot flushes turn N-1's `session_files` before turn
  N's recall. Mild lag shifts the baseline, doesn't break correctness.

## Testing

- **Unit — key resolution:** repo file, nested repo under a non-repo cwd, non-repo
  file, two-worktree aliasing (same key), deleted-dir fallback.
- **Unit — scoring:** strict ordering, Touched-subsumes-Referenced (max),
  dedup, micro vs macro, insufficient-data guard.
- **Integration:** seed a temp Copilot-shaped SQLite store (sessions, turns,
  session_files) + telemetry; simulate turns through the capture hook; run
  `efficacy`; assert all three rates and counts.
- **Concurrency:** parallel sidecar writes under WAL don't corrupt.
- **Borrowed:** retain `doctor`/`stats` test intent against the SQLite backend.

## Attribution

Telemetry field design, `doctor`, and `stats` concepts are adapted from
`theidledeveloper/auto-memory` (MIT). Manual-prune concept from
`osamarehman/auto-memory` (MIT). Preserve the MIT `LICENSE` and credit in code.
