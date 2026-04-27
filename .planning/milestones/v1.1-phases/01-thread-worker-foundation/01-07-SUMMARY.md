---
phase: 01-thread-worker-foundation
plan: "07"
subsystem: testing
tags: [tests, worker, psycopg3, unit-tests, tdd]
dependency_graph:
  requires: [01-03, 01-04, 01-05]
  provides: [test coverage for DbWorker, mock_psycopg_conn fixture]
  affects: [tests/conftest.py, tests/test_worker.py]
tech_stack:
  added: []
  patterns: [unittest.mock patching of psycopg.connect, isinstance-based Jsonb assertion, per-item exception boundary test pattern]
key_files:
  created:
    - tests/test_worker.py
  modified:
    - tests/conftest.py
decisions:
  - asyncio_mode=auto confirmed in pyproject.toml — @pytest.mark.asyncio decorator not required but kept as documentation
  - FrozenInstanceError caught via tuple (AttributeError, dataclasses.FrozenInstanceError) for Python version portability
  - Jsonb assertion uses isinstance() not type().__name__ per plan spec and future-subclass safety
metrics:
  duration: "~1 minute"
  completed: "2026-04-19"
  tasks_completed: 2
  files_changed: 2
---

# Phase 01 Plan 07: DbWorker Unit Tests Summary

DbWorker unit test suite with 13 tests covering flush loop, Jsonb wrapping, D-03 connect failure resilience, per-item exception boundary, graceful shutdown, and SCD2 dispatch — all without a live database.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Add mock_psycopg_conn to conftest.py | f3d33c4 | tests/conftest.py |
| 2 | Create tests/test_worker.py | 3a8cbfe | tests/test_worker.py |

## What Was Built

### tests/conftest.py (modified)

Appended `mock_psycopg_conn` fixture returning `(conn, cur)` — a mock psycopg3 `Connection` with cursor context manager support. All pre-existing asyncpg fixtures (`mock_conn`, `mock_pool`, `hass`, registry mocks) preserved unchanged. The `import psycopg` is scoped inside the fixture body to avoid `ImportError` in environments without psycopg3 installed.

### tests/test_worker.py (created)

13 tests covering:

| Test | Coverage |
|------|----------|
| `test_state_row_is_frozen` | `frozen=True` dataclass immutability |
| `test_meta_command_is_frozen` | `frozen=True` dataclass immutability |
| `test_meta_command_fields` | Field names `registry_id` and `old_id` (not `id`) |
| `test_stop_puts_sentinel` | `stop()` enqueues `_STOP` identity sentinel |
| `test_flush_calls_executemany_with_correct_sql` | `_flush()` calls `executemany(INSERT_SQL, ...)` |
| `test_flush_wraps_attributes_in_jsonb` | `isinstance(value, Jsonb)` — WORK-04 contract |
| `test_flush_operational_error_logs_warning` | `OperationalError` caught, "Connection error during flush" logged |
| `test_run_loop_processes_state_rows_and_stop` | Full flush loop: rows processed, thread exits on `_STOP` |
| `test_connect_failure_worker_enters_loop` | D-03: `OperationalError` on connect does not kill thread |
| `test_bad_meta_command_does_not_kill_loop` | Per-item exception boundary: `RuntimeError` skipped, next item processed |
| `test_graceful_shutdown_awaits_join` | `async_stop()` calls `async_add_executor_job` (not `run_coroutine_threadsafe`) |
| `test_process_entity_create_executes_snapshot_sql` | `_process_entity_command("create")` executes against `entities` |
| `test_process_entity_remove_executes_close_sql` | `_process_entity_command("remove")` executes `SET valid_to` close |

## Deviations from Plan

None — plan executed exactly as written. All 13 tests in acceptance criteria are present and pass.

Minor implementation note: `test_state_row_is_frozen` and `test_meta_command_is_frozen` catch `(AttributeError, dataclasses.FrozenInstanceError)` as a tuple to handle both the CPython 3.10+ explicit `FrozenInstanceError` and older `AttributeError` fallback. This is purely defensive — the project targets Python 3.14+ where `FrozenInstanceError` is always raised.

## Known Stubs

None.

## Threat Flags

None. Tests mock `psycopg.connect()` with `unittest.mock.patch` scoped to each test — no real DB connections, no new trust boundaries.

## Self-Check: PASSED

- `tests/test_worker.py`: FOUND
- `tests/conftest.py` (mock_psycopg_conn): FOUND
- Commit f3d33c4: FOUND (chore(01-07): add mock_psycopg_conn fixture to conftest.py)
- Commit 3a8cbfe: FOUND (test(01-07): create test_worker.py with 13 DbWorker unit tests)
- All 13 tests pass: CONFIRMED (uv run python -m pytest tests/test_worker.py -v → 13 passed)
