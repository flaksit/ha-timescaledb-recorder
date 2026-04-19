---
phase: 01-thread-worker-foundation
plan: "08"
subsystem: tests
tags: [tests, ingester, syncer, schema, lifecycle, psycopg3, queue]
dependency_graph:
  requires: [01-06]
  provides: [test_ingester, test_syncer, test_schema, test_init]
  affects: [tests/]
tech_stack:
  added: []
  patterns:
    - queue.Queue get_nowait() assertion pattern replaces asyncpg/buffer assertions
    - mock_psycopg_conn fixture (sync conn/cursor) for psycopg3 cursor-based tests
    - patch.object on class methods for lifecycle integration testing
key_files:
  created:
    - tests/test_init.py
  modified:
    - tests/test_ingester.py
    - tests/test_syncer.py
    - tests/test_schema.py
    - tests/conftest.py
decisions:
  - "test_schema.py: DDL count is 15 (6 hypertable + 4 dim DDL + 5 dim indexes); plan spec said 14 — corrected by counting schema.py calls directly"
  - "conftest.py: added mock_psycopg_conn (sync MagicMock pair) alongside existing mock_pool — both coexist for their respective test modules"
metrics:
  duration: "~12 minutes"
  completed: "2026-04-19T13:37:13Z"
  tasks_completed: 2
  files_changed: 5
---

# Phase 01 Plan 08: Test Suite Modernisation Summary

All existing tests ported from asyncpg/pool-based mocks to the psycopg3/queue-based APIs introduced in plans 02–06. One new test file created for the `__init__.py` lifecycle entry points.

## What Was Built

Four test files now cover the refactored v1.1 architecture with zero asyncpg references:

- `test_ingester.py` — 9 tests for `StateIngester` thin relay: queue enqueue, filter skip, attribute copy safety, `stop()` sync contract
- `test_syncer.py` — 9 tests for `MetadataSyncer` queue relay: `bind_queue()` API, `@callback` → `MetaCommand` enqueue, rename `old_id` field, area reorder skip, three `_*_row_changed` sync helpers
- `test_schema.py` — 7 tests for `sync_setup_schema()`: 15 DDL statements, ordering, parameter interpolation
- `test_init.py` — 4 tests for `async_setup_entry` / `async_unload_entry`: happy path, shutdown ordering, partial-start rollback, `bind_queue()` call contract

`conftest.py` gained a `mock_psycopg_conn` fixture (sync `MagicMock` conn/cursor pair) used by `test_syncer.py` and `test_schema.py`.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] DDL statement count corrected from 14 to 15**
- **Found during:** Task 2 (test_schema.py first run)
- **Issue:** Plan spec stated "14 DDL statements"; actual `sync_setup_schema` executes 15 (5 dimension indexes, not 4)
- **Fix:** Updated `test_create_schema_executes_all_statements` assertion to `== 15`; corrected docstring breakdown
- **Files modified:** `tests/test_schema.py`
- **Commit:** dd2525f

None of the other plan specifications required deviation.

## Threat Surface Scan

No new network endpoints, auth paths, file access patterns, or schema changes introduced — this plan is test-only.

## Self-Check: PASSED

All files verified present on disk. Both task commits confirmed in git log.
