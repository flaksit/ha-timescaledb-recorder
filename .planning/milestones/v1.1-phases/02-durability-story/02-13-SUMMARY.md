---
phase: 02-durability-story
plan: 13
subsystem: tests
tags: [test-retargeting, phase-2-api, overflow-queue, persistent-queue, state-row]
dependency_graph:
  requires: [02-01, 02-02, 02-06, 02-09, 02-10]
  provides: [green-test-suite]
  affects: [all-test-files]
tech_stack:
  added: []
  patterns: [AsyncMock-for-PersistentQueue, OverflowQueue-in-test-fixtures, dict-schema-assertions]
key_files:
  created: []
  modified:
    - tests/test_worker.py
    - tests/test_ingester.py
    - tests/test_schema.py
    - tests/test_syncer.py
    - tests/test_init.py
decisions:
  - "Accept (AttributeError, TypeError) for slots violation test — Python 3.14 raises TypeError instead of AttributeError on frozen+slots dataclass attribute assignment"
  - "Updated schema statement count 15->16 to account for CREATE_UNIQUE_INDEX_SQL added in Phase 2 (D-09-a)"
  - "RuntimeWarning about unawaited coroutines in AsyncMock is expected and acceptable — production code uses hass.async_create_task(q.put_async(item)) which schedules without awaiting from @callback context"
metrics:
  duration_seconds: 256
  completed_date: "2026-04-22"
  tasks_completed: 3
  files_modified: 5
---

# Phase 2 Plan 13: Retarget Phase 1 Tests Summary

One-liner: Retargeted five Phase 1 test files to Phase 2 API — StateRow-only worker, OverflowQueue ingester, PersistentQueue mock syncer, unique-index schema, D-12/D-13 lifecycle — restoring a green 44-test suite.

## What Was Built

Five test files fully retargeted from Phase 1 API (DbWorker/MetaCommand/bind_queue/queue.Queue) to Phase 2 API (StateRow/OverflowQueue/PersistentQueue/dict-schema/D-12 lifecycle). No production code was changed.

## Tasks Completed

| Task | Description | Commit | Files |
|------|-------------|--------|-------|
| 1 | Rewrite test_worker, update test_ingester + test_schema | dcc8b9c | test_worker.py, test_ingester.py, test_schema.py |
| 2 | Rewrite test_syncer and test_init | 60a31d5 | test_syncer.py, test_init.py |
| 3 | Full suite green check | (no code change) | — |

## Test Suite Results

- **Total tests:** 44 passed, 0 failed
- **Acceptance criterion:** ≥40 tests — met (44)
- **Warnings:** 5 RuntimeWarnings about unawaited coroutines from AsyncMock in @callback context — expected, not failures

## File Changes

### tests/test_worker.py
- Removed: all DbWorker, MetaCommand, _STOP tests (9 tests deleted)
- Added: StateRow-only tests — immutability, slots, from_ha_state copy semantics, "does not export DbWorker/MetaCommand/_STOP" assertion
- Net: 5 tests replacing 9

### tests/test_ingester.py
- Changed: `shared_queue` fixture from `queue.Queue()` to `OverflowQueue(maxsize=10000)`
- Added: `test_ingester_enqueue_never_raises_when_queue_full` — verifies D-02-b drop-newest semantics and `overflowed` flag
- Retained: all 8 existing ingester tests (filter, attributes copy, stop, etc.)

### tests/test_schema.py
- Updated: `test_create_schema_executes_all_statements` count 15→16 (Phase 2 adds unique index)
- Updated: `test_create_schema_order` index positions shifted +1 from position 6 onward; added assertion for UNIQUE INDEX at position 6
- Added: `test_sync_setup_schema_executes_unique_index` — asserts CREATE_UNIQUE_INDEX_SQL is in executed statements

### tests/test_syncer.py
- Removed: all MetaCommand/bind_queue/queue.Queue references
- Added: `mock_meta_queue` fixture (`MagicMock(spec=PersistentQueue)` with `put_async = AsyncMock()`)
- Rewrote: entity/area callback tests to assert dict schema via `put_async.call_args.args[0]` instead of MetaCommand fields
- Added: `_to_json_safe` tests (datetime-to-ISO, None passthrough, non-datetime passthrough)
- Retained: all change-detection helper tests (`_entity_row_changed`, `_device_row_changed`) — unchanged

### tests/test_init.py
- Removed: all DbWorker/MetaCommand/bind_queue/HaTimescaleDBData(worker=...) Phase 1 tests
- Added: `test_async_setup_entry_runs_d12_steps_in_order` — records side-effect sequence to verify steps 2-8 ordering
- Added: `test_setup_entry_returns_true` — basic return value check
- Added: `test_setup_entry_runtime_data_has_all_fields` — asserts all Phase 2 HaTimescaleDBData fields populated
- Added: `test_async_unload_entry_runs_d13_sequence` — asserts stop_event/loop_stop_event/ingester/syncer/meta_queue/joins
- Added: `test_async_unload_entry_returns_true` — basic return value check
- Added: `test_partial_start_rollback_on_syncer_failure` — verifies worker joins on rollback

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Python 3.14 raises TypeError (not AttributeError) for slots violation**
- **Found during:** Task 1, test_state_row_slots_saves_memory
- **Issue:** `dataclass(frozen=True, slots=True)` on Python 3.14 raises `TypeError: super(type, obj): obj is not an instance or subtype of type` instead of `AttributeError` when assigning an unknown attribute
- **Fix:** Changed `pytest.raises(AttributeError)` to `pytest.raises((AttributeError, TypeError))` with explanatory comment
- **Files modified:** tests/test_worker.py

**2. [Rule 1 - Bug] Schema statement count was 15 (Phase 1) not 16 (Phase 2)**
- **Found during:** Task 1, test_create_schema_executes_all_statements
- **Issue:** Phase 2 added `CREATE_UNIQUE_INDEX_SQL` at position 6 in `sync_setup_schema`, raising total from 15 to 16. Old test asserted exactly 15.
- **Fix:** Updated docstring and assertion to 16; updated `test_create_schema_order` to reflect the new index positions (dim table DDL shifted from indices 6-9 to 7-10)
- **Files modified:** tests/test_schema.py

## Known Stubs

None. All tests assert real behaviour paths; no placeholder data flows to UI.

## Threat Flags

None. Test files only — no new network endpoints, auth paths, file access patterns, or schema changes introduced.

## Self-Check: PASSED

| Item | Result |
|------|--------|
| tests/test_worker.py exists | FOUND |
| tests/test_ingester.py exists | FOUND |
| tests/test_schema.py exists | FOUND |
| tests/test_syncer.py exists | FOUND |
| tests/test_init.py exists | FOUND |
| commit dcc8b9c exists | FOUND |
| commit 60a31d5 exists | FOUND |
| uv run pytest tests/ -x -q | 44 passed, 0 failed |
