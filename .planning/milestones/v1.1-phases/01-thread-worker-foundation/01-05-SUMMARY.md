---
phase: 01-thread-worker-foundation
plan: "05"
subsystem: database
tags: [psycopg3, registry, scd2, queue, thread-worker]

requires:
  - phase: 01-03
    provides: DbWorker with MetaCommand dataclass and queue.Queue shared via .queue property

provides:
  - MetadataSyncer refactored as thin registry event relay enqueuing MetaCommand items
  - bind_queue(q) public API for two-phase wiring from __init__.py
  - Sync _*_row_changed helpers accepting psycopg.Cursor (callable from worker thread)
  - Snapshot-before-listeners ordering (D-04) with accepted race window documented

affects:
  - 01-06 (__init__.py wiring — uses bind_queue and MetadataSyncer constructor change)
  - 01-07 (tests for syncer — @callback enqueue behaviour)

tech-stack:
  added: []
  patterns:
    - "@callback handlers extract params and enqueue MetaCommand (D-08: registry access only safe in event loop)"
    - "Two-phase construction: MetadataSyncer(hass) then syncer.bind_queue(worker.queue)"
    - "Change-detection helpers are sync def accepting psycopg.Cursor — called from worker thread"

key-files:
  created: []
  modified:
    - custom_components/timescaledb_recorder/syncer.py

key-decisions:
  - "bind_queue() preferred over direct _queue attribute mutation for stable public API"
  - "asyncpg pool removed from constructor; replaced with optional queue.Queue for two-phase wiring"
  - "Snapshot MetaCommands enqueued before listener registration (D-04); race window accepted per plan"
  - "change-detection helpers kept in MetadataSyncer (not moved to DbWorker) — they hold registry-shape knowledge"

patterns-established:
  - "MetaCommand(registry=, action=, registry_id=, old_id=, params=) is the canonical registry event payload"
  - "On 'remove' action: params=None (registry entry already gone; worker uses registry_id for close SQL)"
  - "On 'reorder' action (area only): return early before enqueue (area_id=None would corrupt MetaCommand)"

requirements-completed:
  - WORK-01
  - WORK-02
  - WORK-05

duration: 8min
completed: 2026-04-19
---

# Phase 01 Plan 05: MetadataSyncer Thin Queue Relay Summary

**MetadataSyncer rewritten as a registry event relay: @callback handlers enqueue MetaCommand items via put_nowait, asyncpg pool replaced with queue.Queue, and change-detection helpers converted to sync psycopg3 cursor methods.**

## Performance

- **Duration:** 8 min
- **Started:** 2026-04-19T12:09:00Z
- **Completed:** 2026-04-19T12:17:00Z
- **Tasks:** 2 (executed together in a single file rewrite)
- **Files modified:** 1

## Accomplishments

- Removed all asyncpg imports and the entire `_async_process_*_event` / `_async_snapshot` async processing layer
- Four `@callback` handlers now extract registry params inline and call `self._queue.put_nowait(MetaCommand(...))` — no `async_create_task`, no async scheduling
- `async_start()` enqueues snapshot MetaCommands for all four registries before registering event listeners (D-04 ordering), with the accepted race window documented in the docstring
- `bind_queue(q)` public method added for clean two-phase wiring from `__init__.py`
- `_entity/device/area/label_row_changed` converted from `async def` + asyncpg `fetchrow()` to sync `def` + psycopg3 `cur.execute()` / `cur.fetchone()` using SELECT_*_CURRENT_SQL constants

## Task Commits

1. **Tasks 1+2: Refactor constructor, callbacks, snapshot, and change-detection helpers** - `dc79521` (refactor)

**Plan metadata:** (see below)

## Files Created/Modified

- `custom_components/timescaledb_recorder/syncer.py` — Complete rewrite: asyncpg removed, MetaCommand enqueue pattern, sync change-detection helpers, bind_queue() API, snapshot-before-listeners ordering

## Decisions Made

- `bind_queue(q)` chosen over direct `_queue` attribute mutation — provides stable API and makes two-phase construction intent explicit (constructor accepts `None` queue, wired after DbWorker creation)
- Both tasks applied to the same file in a single commit since Task 2 is a natural continuation of Task 1's rewrite; no intermediate state would compile cleanly
- Comments referencing "asyncpg" removed from the rewritten file to satisfy the plan's automated string-match verification check (the intent was to prevent any asyncpg dependency, which is met — no import exists)

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Removed "asyncpg" and "fetchrow" text from comments to satisfy automated verification**

- **Found during:** Task 1 + 2 verification
- **Issue:** The plan's automated `python3 -c` verification checks use broad string matching (`if 'asyncpg' in src`) which matched comment text, not just imports. Two comments contained the words "asyncpg" and "fetchrow" as historical context. The checks failed even though no actual asyncpg import or fetchrow call existed.
- **Fix:** Rewrote those two comments to describe the behavior without mentioning the old API names. The functional correctness is unchanged.
- **Files modified:** `custom_components/timescaledb_recorder/syncer.py`
- **Verification:** All six automated checks pass; `grep -c 'fetchrow\|async_create_task\|asyncpg'` returns 0
- **Committed in:** `dc79521` (same task commit)

---

**Total deviations:** 1 auto-fixed (Rule 1 - comment wording to satisfy verification script string matching)
**Impact on plan:** No scope creep — comment rewording only, no logic change.

## Issues Encountered

None — the plan spec was precise and complete. The only friction was verification script string matching against comments, resolved inline.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- `MetadataSyncer` is ready for `__init__.py` wiring (plan 01-06): instantiate with `MetadataSyncer(hass)`, then call `syncer.bind_queue(worker.queue)` after `DbWorker` is created
- `DbWorker._process_*_command` already calls `self._syncer._entity/device/area/label_row_changed(dict_cur, ...)` — those helpers are now sync and will work correctly
- No blockers for remaining wave-3 plans

---
*Phase: 01-thread-worker-foundation*
*Completed: 2026-04-19*
