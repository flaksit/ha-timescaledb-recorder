---
phase: 02-durability-story
plan: 12
subsystem: testing
tags: [pytest, psycopg3, asyncio, states-worker, meta-worker, backfill, scd2, mock]

requires:
  - phase: 02-durability-story
    provides: states_worker.py, meta_worker.py, backfill.py (Wave 2/3 services under test)

provides:
  - Unit test coverage for TimescaledbStateRecorderThread (9 tests)
  - Unit test coverage for TimescaledbMetaRecorderThread (10 tests)
  - Unit test coverage for backfill_orchestrator + _fetch_slice_raw (5 tests)

affects:
  - 02-13 (test_init.py refresh may reference these test patterns)
  - Any future refactor of states_worker, meta_worker, or backfill

tech-stack:
  added: []
  patterns:
    - Direct private-method testing of thread workers (no .run() calls — avoids flaky loop driving)
    - AsyncMock side_effect=lambda to synchronously proxy executor calls in orchestrator tests
    - asyncio.Event-based orchestrator lifecycle: cancel() after sentinel detection to bound test duration

key-files:
  created:
    - tests/test_states_worker.py
    - tests/test_meta_worker.py
    - tests/test_backfill.py
  modified: []

key-decisions:
  - "Test workers by exercising private methods directly rather than calling run() — avoids flaky loop-driving"
  - "Use AsyncMock(side_effect=lambda fn, *args: fn(*args)) to invoke executor jobs synchronously in async tests"
  - "Orchestrator tests cancel() the task after detecting BACKFILL_DONE to avoid waiting on blocked backfill_request.wait()"

patterns-established:
  - "Executor-job proxying: hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: fn(*args))"
  - "run() method testing by replacing retry-wrapped method (w._write_item = fake_fn) then calling w.run() directly"

requirements-completed: [BUF-01, BACK-01, BACK-02, BACK-03, BACK-04, BACK-05, BACK-06, META-01, META-02, META-03, SQLERR-01, SQLERR-02, SQLERR-05]

duration: 15min
completed: 2026-04-22
---

# Phase 02 Plan 12: Unit Tests for Wave 2/3 Services Summary

**24 unit tests covering three-mode state machine (states_worker), SCD2 registry dispatch (meta_worker), and per-entity backfill iteration invariant (backfill) using mock_psycopg_conn + direct method injection**

## Performance

- **Duration:** ~15 min
- **Started:** 2026-04-22T17:30:00Z
- **Completed:** 2026-04-22T17:48:08Z
- **Tasks:** 3
- **Files modified:** 3 (all created)

## Accomplishments

- Created `tests/test_states_worker.py` with 9 tests: mode constants, `_slice_to_rows` multi-entity sort, `_insert_chunk_raw` Jsonb wrapping + INSERT_SQL, watermark and open-entity SQL dispatch, `reset_db_connection` clearing, 200-row chunk splitting logic, and retry-wrapper `__qualname__` introspection
- Created `tests/test_meta_worker.py` with 10 tests: `_VALID_FROM_INDEX` constants, ISO-to-datetime rehydration at per-registry slots, SCD2 snapshot dispatch for all four registries, entity remove (close) and entity rename (close+insert in transaction), and shutdown-safe `task_done` skip (D-03-h)
- Created `tests/test_backfill.py` with 5 tests: per-entity iteration proof (never passes `entity_id=None`), empty-entities returns empty dict, orchestrator exits immediately on pre-set stop_event, empty hypertable pushes `BACKFILL_DONE` + clears live queue, and `clear_buffer_dropping_issue` called on every cycle (D-10-c)

## Task Commits

1. **Task 1: Create tests/test_states_worker.py** - `59614a5` (test)
2. **Task 2: Create tests/test_meta_worker.py** - `617bff1` (test)
3. **Task 3: Create tests/test_backfill.py** - `34f0900` (test)

## Files Created/Modified

- `tests/test_states_worker.py` - Unit tests for TimescaledbStateRecorderThread (9 tests)
- `tests/test_meta_worker.py` - Unit tests for TimescaledbMetaRecorderThread (10 tests)
- `tests/test_backfill.py` - Unit tests for backfill_orchestrator + _fetch_slice_raw (5 tests)

## Decisions Made

- Tested workers by calling private methods directly rather than driving the `run()` loop. Driving `run()` requires extensive mocking of queue blocking behavior and produces flaky tests with race conditions.
- Used `AsyncMock(side_effect=lambda fn, *args: fn(*args))` to proxy `hass.async_add_executor_job` calls synchronously in async tests — avoids real thread pool overhead while preserving call semantics.
- Orchestrator tests that loop (empty hypertable) use `cancel()` after the sentinel is detected via a helper coroutine, rather than relying on `stop_event` to interrupt `backfill_request.wait()` (which requires setting the event from outside — the orchestrator doesn't interrupt its `await backfill_request.wait()` on stop_event alone).

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None. All 24 tests passed on first run. The `[tool.pytest]` section in `pyproject.toml` (non-standard; should be `[tool.pytest.ini_options]`) is actually picked up correctly by pytest-homeassistant-custom-component as `asyncio_mode = "auto"`, confirmed by test session output showing "asyncio: mode=Mode.AUTO".

## Known Stubs

None - test files contain no stub patterns. All assertions are against real production behavior.

## Threat Flags

None - test-only files. No new network endpoints, auth paths, or trust boundaries introduced.

## Next Phase Readiness

- Wave 4 unit tests complete. Plan 13 (test_init.py refresh) can proceed.
- All three services are regression-protected: any change to mode transition logic, SCD2 dispatch, or per-entity backfill iteration will be caught immediately.

## Self-Check

Checking created files exist and commits are present:

- `tests/test_states_worker.py` — FOUND
- `tests/test_meta_worker.py` — FOUND
- `tests/test_backfill.py` — FOUND
- Commit 59614a5 — FOUND (test_states_worker.py)
- Commit 617bff1 — FOUND (test_meta_worker.py)
- Commit 34f0900 — FOUND (test_backfill.py)

## Self-Check: PASSED

---
*Phase: 02-durability-story*
*Completed: 2026-04-22*
