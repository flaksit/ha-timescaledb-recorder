---
phase: 02-durability-story
plan: "06"
subsystem: states-worker
tags: [worker, state-machine, retry, psycopg3, backfill, thread]
dependency_graph:
  requires: [02-01, 02-03, 02-04]
  provides: [states_worker.TimescaledbStateRecorderThread, worker.StateRow.from_state]
  affects: [ingester.py, __init__.py]
tech_stack:
  added: []
  patterns:
    - three-mode state machine (MODE_INIT/MODE_BACKFILL/MODE_LIVE)
    - retry_until_success wrapping at __init__ time (bound-method pattern)
    - call_soon_threadsafe for asyncio.Event cross-thread wake
    - adaptive queue.get(timeout=remaining) for flush cadence
key_files:
  created:
    - custom_components/timescaledb_recorder/states_worker.py
  modified:
    - custom_components/timescaledb_recorder/worker.py
decisions:
  - StateRow.from_state copies HA attributes dict to plain dict, breaking MappingProxyType reference before Jsonb wrapping downstream
  - _insert_chunk wrapped at __init__ not class-body time; retry hooks must bind to live self instance
  - asyncio.Event.set crossed via hass.loop.call_soon_threadsafe, never called directly from worker thread
  - Full import of states_worker deferred until plan 08 delivers backfill.py (BACKFILL_DONE forward dependency)
metrics:
  duration: "~3 minutes"
  completed: "2026-04-22T17:34:31Z"
  tasks_completed: 2
  files_changed: 2
---

# Phase 2 Plan 06: Retire DbWorker/MetaCommand; Create states_worker.py — Summary

Three-mode state machine (MODE_INIT → MODE_BACKFILL → MODE_LIVE) with retry-wrapped chunked INSERT, adaptive flush timeout, and thread-safe asyncio.Event cross-boundary wake.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Gut worker.py — keep only StateRow + from_state | 8b8aede | worker.py |
| 2 | Create states_worker.py with TimescaledbStateRecorderThread | 003d199 | states_worker.py |

## What Was Built

### Task 1 — worker.py retirement (8b8aede)

`worker.py` was reduced from 392 lines to 50 lines. `DbWorker`, `MetaCommand`, and `_STOP` are removed. `StateRow` is retained with its original frozen/slots schema and gains one new classmethod:

- `StateRow.from_state(state)` — converts any HA `State`-compatible object to a `StateRow`, copying `attributes` from `MappingProxyType`/`ReadOnlyDict` into a plain dict (required before `Jsonb()` wrapping in the worker).

Existing consumers importing `from .worker import StateRow` continue to work unchanged. Consumers importing `DbWorker` or `MetaCommand` will fail until plans 09/10 fix their import sites — this is expected and documented in the plan.

### Task 2 — states_worker.py creation (003d199)

`TimescaledbStateRecorderThread` is a `threading.Thread` subclass implementing:

**D-04: Three-mode state machine**

- `MODE_INIT` → immediately requests backfill via `hass.loop.call_soon_threadsafe(backfill_request.set)` and transitions to `MODE_BACKFILL`. This is the first and only automatic trigger on startup.
- `MODE_BACKFILL` → drains `backfill_queue` (blocking with 5s timeout for stop_event interruptibility). Each item is a `dict[entity_id, list[HA State]]`; `_slice_to_rows()` merges, sorts by `last_updated`, and converts to `StateRow`. `BACKFILL_DONE` sentinel signals end of cycle → transition to `MODE_LIVE`.
- `MODE_LIVE` → adaptive `queue.get(timeout=remaining)` where `remaining = max(0.1, FLUSH_INTERVAL - elapsed)`. This fixes the Phase 1 latent bug (D-04-e): the old fixed 5s timeout never fired on busy HA instances because events arrived faster than the timeout; the adaptive calculation guarantees flush at or below FLUSH_INTERVAL cadence regardless of event pressure.

**D-06: Flush / chunk path**

`_flush()` slices the buffer into `INSERT_CHUNK_SIZE` chunks and calls the retry-wrapped `_insert_chunk` per chunk. Each chunk carries its own retry scope — a transient failure on chunk N doesn't re-attempt chunks 0..N-1.

**D-07: Retry integration**

`_insert_chunk = retry_until_success(stop_event=..., on_transient=self.reset_db_connection, notify_stall=self._notify_stall)(self._insert_chunk_raw)` is assigned at `__init__` time, not class-body time, so the hooks are bound to the live `self` instance. `on_transient` calls `reset_db_connection()` to drop the stale psycopg3 handle; `get_db_connection()` lazily reconnects on the next call. `_notify_stall` fires a `persistent_notification` via `hass.add_job` (the thread-safe bridge) after 5 consecutive failures.

**D-08-d / D-08-f: Orchestrator reads**

`read_watermark()` and `read_open_entities()` are public methods called by the backfill orchestrator via `hass.async_add_executor_job`. Both use the worker's connection — acceptable because the worker's main loop is blocked on `queue.get(timeout=5)` during the read, ensuring no concurrent cursor use.

**Thread safety**

`asyncio.Event.set()` is never called directly from the worker thread. The cross-thread wake uses `self._hass.loop.call_soon_threadsafe(self._backfill_request.set)` per the research finding that asyncio primitives are not thread-safe.

## Deviations from Plan

None — plan executed exactly as written. The import smoke test (`uv run python -c "from ... import states_worker"`) correctly fails because `.backfill` does not exist yet (plan 08 creates it). This was anticipated and documented in the plan's acceptance criteria; syntactic validity was verified via `ast.parse()` instead.

## Known Stubs

None. `states_worker.py` is complete with no placeholder data or hardcoded empty values. The `BACKFILL_DONE` import is a forward dependency resolved at runtime when plan 08 lands.

## Threat Flags

None. No new network endpoints, auth paths, or trust boundaries introduced. The module accepts a pre-constructed `psycopg3` connection DSN passed in by the caller (`__init__.py`) — no new credential handling surface.

## Self-Check: PASSED

| Item | Status |
|------|--------|
| custom_components/timescaledb_recorder/worker.py | FOUND |
| custom_components/timescaledb_recorder/states_worker.py | FOUND |
| .planning/phases/02-durability-story/02-06-SUMMARY.md | FOUND |
| Commit 8b8aede (Task 1) | FOUND |
| Commit 003d199 (Task 2) | FOUND |
