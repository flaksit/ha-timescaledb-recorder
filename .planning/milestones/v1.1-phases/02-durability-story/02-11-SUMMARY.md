---
phase: 02-durability-story
plan: 11
subsystem: testing
tags: [pytest, pytest-asyncio, overflow-queue, persistent-queue, retry, issues, unit-tests]

requires:
  - phase: 02-durability-story
    provides: overflow_queue.py, persistent_queue.py, retry.py, issues.py (Wave 1 primitives)

provides:
  - Unit test coverage for all four Wave 1 primitive modules
  - Contract lock for OverflowQueue (D-02, D-11), PersistentQueue (D-03), retry_until_success (D-07), and issue helpers (D-10)

affects: [02-durability-story Wave 2+, integration tests, CI pipeline]

tech-stack:
  added: []
  patterns:
    - Pure pytest tests with no HA runtime for queue/retry primitives
    - MagicMock + patch for HA issue_registry delegation tests
    - tmp_path fixture for file-backed queue isolation
    - asyncio_mode=auto enables async test functions without explicit @pytest.mark.asyncio

key-files:
  created:
    - tests/test_overflow_queue.py
    - tests/test_retry.py
    - tests/test_persistent_queue.py
    - tests/test_issues.py
  modified: []

key-decisions:
  - "asyncio_mode=auto (existing config) allows async test without explicit marker — test_put_async_and_join uses plain async def"
  - "test_clear_and_reset_overflow post-reset drop deliberately re-triggers warning log — intentional per D-11 semantics (warning fires on first drop per outage, reset arms it again)"
  - "test_notify_stall_rearms_after_success verifies decorator state reset by calling fn() twice with distinct failure sequences"

patterns-established:
  - "Wave 1 primitive tests: stdlib-only imports (threading, time, datetime), no HA fixtures, no psycopg"
  - "issues.py delegation tests: patch ir.async_create_issue / ir.async_delete_issue at the issues module level, not at the homeassistant package level"

requirements-completed: [BUF-01, BUF-02, META-01, META-02, META-03, SQLERR-01, SQLERR-02, SQLERR-05]

duration: 10min
completed: 2026-04-22
---

# Phase 02 Plan 11: Wave 1 Primitive Unit Tests Summary

**22 pure-pytest unit tests locking D-02/D-03/D-07/D-10 contracts for OverflowQueue, PersistentQueue, retry_until_success, and issue registry helpers**

## Performance

- **Duration:** ~10 min
- **Started:** 2026-04-22T17:35:00Z
- **Completed:** 2026-04-22T17:45:10Z
- **Tasks:** 2
- **Files modified:** 4 (created)

## Accomplishments

- 5 tests for OverflowQueue: FIFO semantics, drop-newest-on-full without raising, single-warning-per-outage (D-11-a/b), _dropped counter, clear_and_reset_overflow drain+rearm
- 8 tests for retry_until_success: success path, backoff with fast schedule, stop_event interrupt, on_transient hook, notify_stall fire-once and rearm-after-success, broad exception catch, default constants
- 6 tests for PersistentQueue: put/get/task_done roundtrip, crash-replay via new instance, atomic front-line removal file check, wake_consumer unblock, put_async+join, datetime serialization via default=str
- 3 tests for issues helpers: create delegation with correct kwargs, clear delegation with positional args, strings.json translation key regression guard

## Task Commits

Each task was committed atomically:

1. **Task 1: test_overflow_queue.py and test_retry.py** - `062686b` (test)
2. **Task 2: test_persistent_queue.py and test_issues.py** - `5bb8a7c` (test)

**Plan metadata:** committed with SUMMARY.md (docs)

## Files Created/Modified

- `tests/test_overflow_queue.py` - 5 tests covering D-02 and D-11 acceptance points
- `tests/test_retry.py` - 8 tests covering D-07 acceptance points including stall notification and rearm
- `tests/test_persistent_queue.py` - 6 tests covering D-03 crash-replay, atomicity, and async producer/join
- `tests/test_issues.py` - 3 tests covering D-10 delegation to homeassistant.helpers.issue_registry and strings.json guard

## Decisions Made

- Async test `test_put_async_and_join` uses plain `async def` — existing `asyncio_mode = "auto"` in pyproject.toml removes need for explicit marker
- `test_strings_json_has_matching_translation_key` uses a relative `pathlib.Path` — this is intentional; pytest runs from repo root so the path resolves correctly in CI

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

All four Wave 1 primitive module contracts are now locked by unit tests. Wave 2+ consumers (state_worker, meta_worker, backfill_orchestrator) can depend on these contracts without risk of silent regressions. No blockers.

## Self-Check

### Files exist

- tests/test_overflow_queue.py: FOUND
- tests/test_retry.py: FOUND
- tests/test_persistent_queue.py: FOUND
- tests/test_issues.py: FOUND

### Commits exist

- 062686b (Task 1): FOUND
- 5bb8a7c (Task 2): FOUND

### Test results

22 passed, 0 failed across all four files.

## Self-Check: PASSED

---
*Phase: 02-durability-story*
*Completed: 2026-04-22*
