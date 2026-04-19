---
phase: 01-thread-worker-foundation
plan: 04
subsystem: ingester
tags: [event-relay, queue, callback, thread-boundary, psycopg3]

requires:
  - plan: 01-03
    provides: StateRow dataclass and worker.py with queue.Queue ownership

provides:
  - ingester.py refactored as a thin @callback relay — enqueues StateRow via put_nowait()
  - stop() sync cancellation method (renamed from async_stop())
  - No DB logic, no buffer, no timer in ingester — all deferred to DbWorker

affects:
  - __init__.py (plan 06): calls data.ingester.stop() not async_stop()
  - worker.py: ingester is now the sole StateRow producer on the shared queue

tech-stack:
  added: []
  patterns:
    - "@callback handler as the event loop / worker thread boundary"
    - "dict(new_state.attributes) shallow copy at enqueue site — avoids mutable HA state reference aliasing"
    - "stop() as plain sync def on an ingester with async_ lifecycle prefix (async_start); only stop() needs no event loop"
    - "queue.put_nowait() from @callback — non-blocking, thread-safe, correct"

key-files:
  created: []
  modified:
    - custom_components/ha_timescaledb_recorder/ingester.py

key-decisions:
  - "stop() named without async_ prefix to prevent callers from incorrectly awaiting it — async_stop naming on a sync method is misleading"
  - "No final flush in stop() — DbWorker processes remaining buffer when it hits the _STOP sentinel enqueued by async_unload_entry"
  - "dict(new_state.attributes) copied at enqueue site, not in DbWorker._flush() — keeps Jsonb() wrapping centralised in worker but protects against mutable HA state aliasing before the row is processed"

requirements-completed:
  - WORK-01
  - WORK-02

duration: "~2 minutes"
completed: "2026-04-19"
---

# Phase 1 Plan 04: StateIngester Thin Queue Relay Summary

StateIngester rewritten as a minimal @callback event relay: filters by entity_id, copies attributes, enqueues a frozen StateRow via queue.put_nowait() — no buffer, no timer, no async flush, no asyncpg.

## Performance

- **Duration:** ~2 min
- **Started:** 2026-04-19T10:28:29Z
- **Completed:** 2026-04-19T10:29:59Z
- **Tasks:** 1
- **Files modified:** 1

## Accomplishments

- Removed all buffering, timer, and async flush logic from ingester.py
- Replaced asyncpg pool dependency with queue.Queue dependency (from DbWorker)
- Constructor now accepts (hass, queue, entity_filter) — no batch_size, flush_interval, pool
- Renamed async_stop() to stop() — sync method, no coroutine, clearer caller contract
- @callback handler now enqueues StateRow directly; attributes snapshot taken at enqueue site

## Task Commits

1. **Task 1: Refactor StateIngester to thin queue relay with sync stop()** - `40e048f` (refactor)

**Plan metadata:** committed with this SUMMARY.md

## Files Created/Modified

- `custom_components/ha_timescaledb_recorder/ingester.py` - Rewritten as thin relay; 81 lines down to ~80 lines but with all batch/flush/pool logic removed and replaced by one put_nowait() call

## Decisions Made

**stop() naming convention break:** The convention is `async_*` for async public methods. `stop()` is sync and intentionally does NOT use the `async_` prefix, even though `async_start()` does. This prevents callers from incorrectly awaiting it. Documented in the method docstring.

**No flush in stop():** Worker handles the remaining StateRow buffer when it processes the `_STOP` sentinel. This is correct because the sentinel is enqueued by `async_unload_entry` (plan 06), not by ingester.stop(). Ingester just cancels the listener.

## Deviations from Plan

None - plan executed exactly as written.

## Known Stubs

None.

## Threat Flags

No new trust boundaries beyond those in the plan's threat model.

- T-04-01 (unbounded queue DoS): accepted — Phase 2 adds BUF-01 bounded buffer with drop-oldest
- T-04-02 (attribute aliasing): mitigated — `dict(new_state.attributes)` copies the snapshot at enqueue site

## Self-Check: PASSED

- FOUND: `custom_components/ha_timescaledb_recorder/ingester.py`
- FOUND: commit `40e048f` (Task 1 — refactor StateIngester)
- ingester.py parses cleanly: `python3 -c "import ast; ast.parse(...)"` exits 0
- grep for `asyncpg|_buffer|_async_flush|async_stop` returns 0 matches
