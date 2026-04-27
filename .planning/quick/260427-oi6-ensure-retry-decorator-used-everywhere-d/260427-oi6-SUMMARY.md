---
phase: quick-260427-oi6
plan: 01
subsystem: states_worker
tags: [retry, issue-12, psycopg3, db-resilience]
dependency_graph:
  requires: []
  provides: [retry-wrapped schema setup, retry-wrapped read_all_known_entities]
  affects: [custom_components/timescaledb_recorder/states_worker.py]
tech_stack:
  added: []
  patterns: [retry_until_success decorator, raw/wrapped method split]
key_files:
  modified:
    - custom_components/timescaledb_recorder/states_worker.py
    - tests/test_states_worker.py
decisions:
  - _setup_schema_raw placed in Main loop section alongside run()/_run_main_loop() for locality
  - retry wrappers for schema+read_all_known_entities use same minimal shape as read_watermark (no stall/recovery hooks)
  - test wiring tests use all_calls list pattern (not captured{} overwrite) to allow index-based call verification
metrics:
  duration: "~15 min"
  completed: "2026-04-27"
  tasks_completed: 2
  files_modified: 2
---

# Quick Task 260427-oi6: Ensure retry decorator used everywhere — SUMMARY

One-liner: Wrapped `_setup_schema_raw` and `_read_all_known_entities_raw` with `retry_until_success` in `__init__`, eliminating the bare try/except for schema setup and the unprotected `read_all_known_entities` DB call (Issue #12).

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Refactor states_worker.py — extract raw methods, add retry wrappers | 7f02a4f | states_worker.py |
| 2 | Add retry-wiring and retry-behaviour tests | ed9c869 | test_states_worker.py |

## What Changed

### states_worker.py

- Extracted `_setup_schema_raw(self)` method from the inline try/except block in `_run_main_loop` (the block that called `get_db_connection()` + `sync_setup_schema()`).
- Renamed `read_all_known_entities` → `_read_all_known_entities_raw` with updated docstring.
- Added two new `retry_until_success` wrappers in `__init__` (after the existing `read_watermark` wrapper):
  - `self._setup_schema` wrapping `_setup_schema_raw`
  - `self.read_all_known_entities` wrapping `_read_all_known_entities_raw`
  - Both use `on_transient=self.reset_db_connection`; no stall/recovery hooks (same shape as `read_watermark`).
- Replaced the 13-line try/except schema block in `_run_main_loop` with `self._setup_schema()`.
- Added motivating comment to `reset_db_connection` bare except.

### tests/test_states_worker.py

Six new tests added (29 total, all passing):
1. `test_setup_schema_is_retry_wrapped` — checks `_setup_schema is not _setup_schema_raw`
2. `test_setup_schema_retries_on_transient_error` — fail-once/succeed-second with zero backoff
3. `test_setup_schema_retry_wiring_captures_on_transient` — all_calls list, index 2, verifies `on_transient.__func__`
4. `test_read_all_known_entities_is_retry_wrapped` — checks wrapper identity
5. `test_read_all_known_entities_retries_on_transient_error` — fail-once/succeed-second, checks return value
6. `test_read_all_known_entities_retry_wiring_captures_on_transient` — all_calls list, index 3

## Deviations from Plan

None — plan executed exactly as written.

## Known Stubs

None.

## Threat Flags

None — no new network endpoints, auth paths, or trust boundaries introduced.

## Self-Check: PASSED

- states_worker.py modified: FOUND
- tests/test_states_worker.py modified: FOUND
- Commit 7f02a4f: FOUND
- Commit ed9c869: FOUND
- 29 tests passing: CONFIRMED
