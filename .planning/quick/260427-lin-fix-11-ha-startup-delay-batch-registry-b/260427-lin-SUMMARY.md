---
phase: 260427-lin
plan: 01
subsystem: persistent_queue + setup_entry + manifest
tags:
  - issue-11
  - startup-performance
  - crash-recovery
  - thread-safety
requires: []
provides:
  - PersistentQueue.put_many
  - PersistentQueue.put_many_async
  - PersistentQueue.get tolerates malformed JSON lines
  - async_at_started-driven orchestrator + meta_init dispatch
  - manifest after_dependencies = [recorder]
affects:
  - HA bootstrap timing on installs with hundreds of registry entries
  - Meta worker resilience to torn JSONL writes after crash
  - Reload behaviour (no longer racing EVENT_HOMEASSISTANT_STARTED)
tech_stack_added: []
tech_stack_patterns:
  - "homeassistant.helpers.start.async_at_started for once-on-RUNNING dispatch"
  - "Single fsync per batch for file-backed FIFO"
key_files_created: []
key_files_modified:
  - custom_components/timescaledb_recorder/persistent_queue.py
  - custom_components/timescaledb_recorder/__init__.py
  - custom_components/timescaledb_recorder/manifest.json
  - tests/test_persistent_queue.py
  - tests/test_init.py
decisions:
  - "Defer orchestrator AND meta_init via async_at_started rather than splitting on hass.is_running — handles fresh-boot and reload uniformly with no listen_once foot-gun."
  - "Tolerant get() drops malformed front line via the same atomic temp+fsync+os.replace path used by task_done — no duplicated rewrite logic."
  - "put_many([]) early-returns to avoid spurious notify_all on a blocked consumer."
metrics:
  duration: ~30 minutes
  completed: 2026-04-27
requirements:
  - GH-ISSUE-11
---

# Quick Task 260427-lin: Fix #11 (HA startup delay) Summary

## One-liner

Issue #11 fix: collapse N per-entry fsyncs into one batched write for the
initial registry backfill, defer orchestrator + meta_init until HA is RUNNING
via `async_at_started`, declare `after_dependencies: [recorder]`, and make
`PersistentQueue.get()` tolerate torn-write JSON lines.

## Tasks Completed

| # | Task | Commit | Files |
|---|------|--------|-------|
| 1 | Add `put_many` / `put_many_async` and tolerant `get()` (TDD) | `fb95433` (RED), `447d7d8` (GREEN) | `persistent_queue.py`, `tests/test_persistent_queue.py` |
| 2 | Batch initial registry backfill into one `put_many_async` call | `381c774` | `__init__.py` |
| 3 | Defer orchestrator + meta_init via `async_at_started`; manifest `after_dependencies` + version bump | `56216b3` | `__init__.py`, `manifest.json`, `tests/test_init.py` |
| 4 | Repo-wide sanity checks + full test run | (no code commit — verification only) | — |

## What Changed

### `persistent_queue.py`

- New `put_many(items)`: appends N JSONL lines in a single open + write +
  flush + `os.fsync` + `notify_all`. Empty input is a no-op (no file open,
  no fsync, no notify).
- New `put_many_async(items)`: mirrors `put_async` via
  `loop.run_in_executor(None, self.put_many, items)`. Early-returns on
  empty input without scheduling executor work.
- `get()` is now tolerant: a malformed JSON front line is logged at WARNING
  with the queue path and a truncated repr (≤120 chars), then dropped from
  disk via the same tempfile + fsync + `os.replace` pattern as `task_done`.
  `_in_flight` is only assigned when parsing succeeds, so the consumer
  contract `get → process → task_done` is preserved.
- New private `_rewrite_without_front_line_locked()`: extracted from
  `task_done` so both the post-success drop and the post-malformed drop
  share one rewrite path (no duplication).

### `__init__.py`

- `_async_initial_registry_backfill`: builds a single ordered `items` list
  (area → label → entity → device) and calls `put_many_async` once.
  Preserves the FK-like ordering and the per-item dict shape.
- `async_setup_entry`:
  - `_async_meta_init` is now spawned from a `@callback` registered with
    `async_at_started(hass, _start_meta_init)` instead of being spawned
    directly from `setup_entry`.
  - The orchestrator is also spawned from a `@callback` registered with
    `async_at_started`, replacing the prior
    `if hass.is_running: _spawn_orchestrator() else:
    bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, ...)` split. Both
    callbacks use sync `@callback` wrappers because `async_at_started`
    invokes the callback synchronously when state is already RUNNING
    (reload path).
- Removed `from homeassistant.const import EVENT_HOMEASSISTANT_STARTED`
  (now unused in this module).
- Added `from homeassistant.helpers.start import async_at_started`.

### `manifest.json`

- `after_dependencies: ["recorder"]` added — HA loads recorder's
  `setup_entry` before this integration's, so `ha_recorder.get_instance(hass)`
  no longer races recorder bootstrap.
- `version` bumped `2.0.0` → `2.0.1`.

### Tests

- `tests/test_persistent_queue.py`: 7 new tests covering `put_many`
  (order, single-fsync, empty no-op), `put_many_async` (drain via get,
  empty no-op), and tolerant `get()` (malformed front line skipped and
  warned, partial trailing line recovered).
- `tests/test_init.py`: two pre-existing tests updated to reflect the
  switch from `bus.async_listen_once` to `async_at_started`. The D-12
  ordering test now expects step8 to fire **twice** (once for meta_init,
  once for orchestrator), and the threading-stop-event test fires both
  registered callbacks instead of the single old listener.

## Verification Results

| Step | Command | Result |
|------|---------|--------|
| Task 1 | `uv run pytest tests/test_persistent_queue.py -x -q` | 13 passed |
| Task 2 | `uv run pytest tests/test_init.py -x -q` (after Task 3 test fix) | 27 passed |
| Task 3 | `uv run pytest tests/test_init.py tests/test_backfill.py -x -q` | 40 passed |
| Task 4 | `uv run pytest --ignore=tests/test_states_worker.py --ignore=tests/test_config_flow.py -q` | 183 passed |
| Manifest JSON | `uv run python -c "import json; json.load(open('.../manifest.json'))"` | valid |
| Grep | `grep EVENT_HOMEASSISTANT_STARTED hass.is_running put_async` in production | only intended residue (rationale comment) |

### Sanity grep results

- `grep -rn "EVENT_HOMEASSISTANT_STARTED" custom_components/timescaledb_recorder/`:
  - `__init__.py:553` — inside the rationale comment that documents the
    migration FROM the constant TO `async_at_started`. Per CLAUDE.md
    "document non-obvious decisions"; the comment names `async_at_started`
    as the replacement.
  - `backfill.py:82` — pre-existing comment, out of scope (not modified).
- `grep -rn "hass.is_running" custom_components/timescaledb_recorder/`: clean.
- `grep -n "put_async" custom_components/timescaledb_recorder/__init__.py`:
  clean (the loop-driven `put_async` calls have all been replaced by a
  single `put_many_async`).

## Deviations from Plan

### Auto-fixed Issues

None — production-code changes followed the plan exactly.

### Test fix-ups (in scope)

- Plan Task 3 explicitly said tests for `test_init.py` had to be aligned
  with the `async_at_started` migration. Updated
  `test_async_setup_entry_runs_d12_steps_in_order` and
  `test_setup_passes_threading_stop_event_to_orchestrator` to patch
  `async_at_started` instead of `bus.async_listen_once`. Step8 now fires
  twice (one per registration: meta_init + orchestrator).

### Pre-existing failures (out of scope)

Two test modules failed during the full-suite run that are unrelated to
issue #11. Logged in `deferred-items.md` per executor SCOPE BOUNDARY rule:

- `tests/test_states_worker.py` — `ImportError: SELECT_OPEN_ENTITIES_SQL`
  (stale const reference left over from rename commit `6411d6b`).
- `tests/test_config_flow.py::test_valid_dsn` — assertion expects
  `create_entry` but flow returns `form`. Predates this plan.

Both reproduce on a tree without our changes; we ignored them with
`pytest --ignore=...` for the green-light verification run.

## TDD Gate Compliance

Task 1 was `tdd="true"`. Both gates committed in order:

- `fb95433` `test(260427-lin-01): add failing tests for put_many,
  put_many_async, tolerant get` — RED gate (verified failing before
  implementation).
- `447d7d8` `feat(260427-lin-01): add put_many / put_many_async and
  tolerant get() to PersistentQueue` — GREEN gate (all 13 tests pass).
- No REFACTOR commit needed; the extracted
  `_rewrite_without_front_line_locked` helper landed in the same GREEN
  commit as the only natural place to introduce it (Task 1's `<action>`
  block explicitly authorised this).

## Self-Check: PASSED

Verified files exist:

- `custom_components/timescaledb_recorder/persistent_queue.py` — FOUND
- `custom_components/timescaledb_recorder/__init__.py` — FOUND
- `custom_components/timescaledb_recorder/manifest.json` — FOUND (version
  `2.0.1`, `after_dependencies: ["recorder"]`)
- `tests/test_persistent_queue.py` — FOUND
- `tests/test_init.py` — FOUND

Verified commits exist in `git log`:

- `fb95433` — FOUND (RED gate)
- `447d7d8` — FOUND (GREEN gate)
- `381c774` — FOUND (Task 2 — backfill batched)
- `56216b3` — FOUND (Task 3 — async_at_started + manifest)
