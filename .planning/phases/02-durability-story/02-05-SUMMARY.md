---
phase: 02-durability-story
plan: "05"
subsystem: database
tags: [persistent-queue, file-backed-fifo, json-lines, threading, condition-variable, crash-safety, durability]

requires:
  - phase: 01-thread-worker-foundation
    provides: psycopg3 worker thread pattern, SCD2 idempotency guarantee (D-03-h depends on this)

provides:
  - PersistentQueue class: file-backed FIFO for metadata events with crash-safe delivery
  - Atomic task_done() rewrite via tempfile + os.replace (POSIX-atomic)
  - Blocking get() with Condition-based wait and wake_consumer() shutdown unblock
  - put_async() for event-loop callers to offload blocking fsync to executor
  - join() coroutine for startup drain before new events arrive

affects:
  - 02-07: meta_worker.py (consumes PersistentQueue via get/task_done/wake_consumer)
  - 02-08: syncer.py (produces to PersistentQueue via put_async from @callback handlers)
  - 02-11: __init__.py (constructs PersistentQueue, calls join() at step 4, wake_consumer() at shutdown)

tech-stack:
  added: []
  patterns:
    - "Module-level sentinel object (_NO_IN_FLIGHT) to distinguish in-flight from falsy values"
    - "Condition wrapping Lock — single lock used by both producers and consumer"
    - "fsync on append (D-03-b) + tempfile + os.replace on removal (D-03-e) for crash durability"
    - "put_async offloads blocking I/O to default executor via loop.run_in_executor"
    - "get() never removes from disk — task_done() is the sole deletion path (crash safety)"

key-files:
  created:
    - custom_components/ha_timescaledb_recorder/persistent_queue.py
  modified: []

key-decisions:
  - "Module-level _NO_IN_FLIGHT sentinel (not per-instance) distinguishes unset in-flight from None/{} payloads"
  - "get() re-reads disk after every Condition.wait() wake; tolerates spurious wakes by returning None to caller"
  - "task_done() atomically rewrites via .tmp sibling file + os.replace; partial rewrite never visible"
  - "No TTL or max-size cap: 10-day outage at 100 events/day ~ few KB/day; acceptable per D-03"
  - "Module imports pure stdlib + asyncio only; no HA dependency allows independent unit testing"

patterns-established:
  - "File-backed FIFO pattern: append-only put, read-without-remove get, atomic-rewrite task_done"
  - "Producer/consumer Condition pattern: notify_all on put, wait on empty, wake on shutdown"

requirements-completed: [META-01, META-02, META-03]

duration: 1min
completed: "2026-04-22"
---

# Phase 02 Plan 05: PersistentQueue Summary

**File-backed FIFO queue with JSON-lines format, fsync on write, and atomic tempfile+os.replace removal — ensuring metadata events survive process restarts between get() and task_done()**

## Performance

- **Duration:** 1 min
- **Started:** 2026-04-22T17:25:53Z
- **Completed:** 2026-04-22T17:27:07Z
- **Tasks:** 1 of 1
- **Files modified:** 1

## Accomplishments

- Delivered `PersistentQueue` implementing all D-03 sub-decisions (a through h)
- Crash-replay smoke test passes: item persists on disk when task_done() is not called, replays correctly on new instance
- All eight public interface methods present and verified: `put`, `put_async`, `get`, `task_done`, `join`, `wake_consumer`, `_join_blocking`, `_read_first_line_locked`

## Task Commits

Each task was committed atomically:

1. **Task 1: Create persistent_queue.py with PersistentQueue class** - `73f612c` (feat)

**Plan metadata:** (committed with SUMMARY below)

## Files Created/Modified

- `custom_components/ha_timescaledb_recorder/persistent_queue.py` - PersistentQueue class: file-backed FIFO, JSON-lines, single Condition lock, atomic rewrite on task_done

## Decisions Made

- Used module-level `_NO_IN_FLIGHT = object()` sentinel (not per-instance `self._SENTINEL`) as specified in the plan's action block. This is cleaner and avoids the sentinel being tied to a specific instance lifetime — important for crash-replay where a new instance reads an item in-flight from a prior instance.
- `_read_first_line_locked` strips trailing `\n` with `rstrip("\n")` before returning, so `json.loads` receives clean input without an extra newline-handling step in callers.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None. uv created a fresh virtual environment on first run (no project venv existed in worktree), completed install in 1.41s, smoke test passed immediately.

## Known Stubs

None. `PersistentQueue` is a self-contained utility with no data wired to external systems yet. The consuming plans (02-07 meta_worker, 02-08 syncer) will wire it into the HA event flow.

## Threat Flags

The file path passed to `PersistentQueue.__init__` comes from `hass.config.path(DOMAIN, "metadata_queue.jsonl")` (as documented in 02-PATTERNS.md). This resolves inside HA's config directory — no path traversal surface since the caller controls the path. No new network endpoints, auth paths, or trust boundary crossings introduced.

## Next Phase Readiness

- `PersistentQueue` is ready for use by `meta_worker.py` (plan 02-07) and `syncer.py` (plan 02-08)
- No blockers

## Self-Check: PASSED

- File exists: FOUND `custom_components/ha_timescaledb_recorder/persistent_queue.py`
- Commit exists: FOUND `73f612c`
- Smoke test: PASSED (crash-replay scenario verified)
- All 10 grep acceptance criteria: PASSED

---
*Phase: 02-durability-story*
*Completed: 2026-04-22*
