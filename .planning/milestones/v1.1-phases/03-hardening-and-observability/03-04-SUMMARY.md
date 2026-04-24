---
phase: 03-hardening-and-observability
plan: "04"
subsystem: worker-hooks
tags: [states_worker, meta_worker, retry, watchdog, tdd, hooks, _last_exception]
dependency_graph:
  requires:
    - create/clear_states_worker_stalled_issue (issues.py — Plan 01)
    - create/clear_meta_worker_stalled_issue (issues.py — Plan 01)
    - create/clear_db_unreachable_issue (issues.py — Plan 01)
    - retry_until_success with on_recovery, on_sustained_fail kwargs (retry.py — Plan 03)
  provides:
    - states_worker._stall_hook (bound to create_states_worker_stalled_issue via hass.add_job)
    - states_worker._recovery_hook (bound to clear_states_worker_stalled_issue + clear_db_unreachable_issue)
    - states_worker._sustained_fail_hook (bound to create_db_unreachable_issue)
    - states_worker._last_exception (readable by watchdog after thread exit)
    - states_worker._last_context (dict with at/mode/retry_attempt/last_op keys)
    - states_worker._run_main_loop (inner loop; run() is now a thin wrapper)
    - meta_worker._stall_hook (create_meta_worker_stalled_issue)
    - meta_worker._recovery_hook (clear_meta_worker_stalled_issue + clear_db_unreachable_issue)
    - meta_worker._sustained_fail_hook (create_db_unreachable_issue)
    - meta_worker._last_exception (readable by watchdog after thread exit)
    - meta_worker._last_context (dict with at/mode/retry_attempt/last_op keys)
    - meta_worker._run_main_loop
  affects:
    - 03-06 (watchdog reads _last_exception/_last_context + calls restart after is_alive() == False)
    - 03-07 (watchdog spawned async task reads _last_context["last_op"] for notification body)
tech_stack:
  added: []
  patterns:
    - TDD (RED/GREEN per task)
    - except-before-finally capture pattern (MEDIUM-8: teardown errors cannot overwrite _last_exception)
    - hass.add_job bridge for thread-safe HA API calls from worker threads
    - lazy import of persistent_notification inside hook body (avoids module-load cost)
key_files:
  created: []
  modified:
    - custom_components/ha_timescaledb_recorder/states_worker.py
    - custom_components/ha_timescaledb_recorder/meta_worker.py
    - tests/test_states_worker.py
    - tests/test_meta_worker.py
decisions:
  - "_last_context initialized with safe defaults in __init__ (not lazily on exception) — watchdog can always dereference without AttributeError (MEDIUM-8)"
  - "run() outer except captures _last_exception/_last_context BEFORE finally runs — conn.close() errors in finally cannot overwrite the original fault"
  - "Connection teardown moved exclusively to finally block in run() — _run_main_loop no longer closes the connection on exit (single teardown path, MEDIUM-8)"
  - "_stall_hook KEEPS the Phase 2 persistent_notification AND adds the repair issue — both fire on stall (D-02 requirement: repair issue is additive)"
  - "_recovery_hook clears BOTH worker_stalled AND db_unreachable unconditionally — ir.async_delete_issue is a no-op if issue absent, so no guard needed"
  - "persistent_notification imported lazily inside _stall_hook body to avoid module-load cost and keep test surface clean"
  - "_last_mode tracking added to states_worker (has mode machine); meta_worker sets mode=None in _last_context (no mode machine)"
metrics:
  duration: "~420 seconds"
  completed_date: "2026-04-23T00:00:00Z"
  tasks_completed: 2
  files_modified: 4
  files_created: 0
  tests_added: 22
---

# Phase 03 Plan 04: Worker Hook Wiring + Watchdog Context Summary

Both worker threads are now wired to the extended retry decorator and carry post-mortem context attributes that a watchdog-spawned async task can read after the thread exits.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 (RED) | states_worker failing tests | 3016126 | tests/test_states_worker.py |
| 1 (GREEN) | states_worker Phase 3 hooks + _last_* context | f014b02 | custom_components/ha_timescaledb_recorder/states_worker.py, tests/test_states_worker.py |
| 2 (RED) | meta_worker failing tests | 32e59c9 | tests/test_meta_worker.py |
| 2 (GREEN) | meta_worker Phase 3 hooks + _last_* context | adab7ee | custom_components/ha_timescaledb_recorder/meta_worker.py, tests/test_meta_worker.py |

## Watchdog-Readable Attributes

Both workers now expose the following attributes that Plan 06 (watchdog) will read after `is_alive() == False`:

### _last_exception

| State | Value |
|-------|-------|
| After `__init__` (run() never called) | `None` |
| After clean `run()` exit (no exception) | `None` |
| After unhandled exception escapes `_run_main_loop` | The `Exception` instance |

The watchdog pattern is: `if not thread.is_alive() and thread._last_exception is not None: restart()`.

### _last_context

| Key | Type | Value at `__init__` | Value after exception |
|-----|------|---------------------|----------------------|
| `at` | `str \| None` | `None` | ISO-8601 UTC string (e.g. `"2026-04-23T08:30:00.123456+00:00"`) |
| `mode` | `str \| None` | `"init"` (states) / `None` (meta) | Last mode at time of exception |
| `retry_attempt` | `int \| None` | `None` | `_last_retry_attempt` at time of exception |
| `last_op` | `str` | `"unknown"` | Last `_last_op` value at time of exception |

`_last_op` is updated at stable checkpoints in `_run_main_loop` so the watchdog notification can include a meaningful "last activity" string. Typical values: `"schema_setup"`, `"flush_backfill_chunk"`, `"flush_live_threshold"`, `"write_item"`.

## Hook Method Names (for Plan 06/07 reference)

Both workers expose identical hook method names bound to their respective issue IDs:

| Method | Fires when | Issues affected |
|--------|------------|-----------------|
| `_stall_hook(attempts)` | `attempts >= STALL_THRESHOLD` | Creates `worker_stalled` issue + fires persistent_notification |
| `_recovery_hook()` | First success after stall | Clears `worker_stalled` + `db_unreachable` |
| `_sustained_fail_hook()` | Fail duration >= 300s | Creates `db_unreachable` issue |

These are wired as `notify_stall=`, `on_recovery=`, `on_sustained_fail=` in `retry_until_success(...)`.

## Finally-Block Teardown Pattern

`run()` in both workers now has this structure:

```python
def run(self):
    try:
        self._run_main_loop()
    except Exception as err:           # capture FIRST
        self._last_exception = err
        self._last_context = { ... }   # populated here, not in finally
    finally:
        if self._conn is not None:     # teardown AFTER capture
            try:
                self._conn.close()
            except Exception:
                _LOGGER.debug(...)     # logged, not re-raised
```

Plan 06 (watchdog) and Plan 07 (restart) can rely on: connection is always closed after `run()` returns. They do NOT need to close the connection themselves.

`_run_main_loop` does NOT close the connection — teardown is the sole responsibility of the `finally` block in `run()`.

## Test Count Breakdown

### tests/test_states_worker.py

| Category | Tests |
|----------|-------|
| Phase 2 preserved | 9 |
| Phase 3 new (Plan 04) | 11 |
| Total | 20 |

New test functions:
1. `test_last_exception_initialized_to_none_in_init`
2. `test_last_context_initialized_with_safe_defaults_in_init`
3. `test_stall_hook_fires_persistent_notification_and_repair_issue`
4. `test_recovery_hook_clears_both_issues`
5. `test_sustained_fail_hook_creates_db_unreachable_issue`
6. `test_retry_decorator_wired_with_all_phase3_hooks`
7. `test_run_outer_except_captures_last_exception_and_context`
8. `test_run_outer_except_context_has_iso_at_timestamp`
9. `test_run_finally_closes_connection_on_exception`
10. `test_run_teardown_error_does_not_overwrite_last_exception`
11. `test_last_op_updated_before_retried_operations`

### tests/test_meta_worker.py

| Category | Tests |
|----------|-------|
| Phase 2 preserved | 10 |
| Phase 3 new (Plan 04) | 11 |
| Total | 21 |

New test functions mirror states_worker list with meta_worker naming. 11th test: `test_last_op_updated_before_write_item`.

Full suite: 149 passed (up from 127 at end of Plan 03).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Lazy import patch target for persistent_notification**
- **Found during:** Task 1 (GREEN)
- **Issue:** The plan's test spec called for patching `states_worker.persistent_notification` at the module level, but `persistent_notification` is imported lazily inside `_stall_hook` (not at module top). The module attribute did not exist, causing `AttributeError: ... does not have the attribute 'persistent_notification'`.
- **Fix:** Changed the test to patch at `homeassistant.components.persistent_notification` — the actual module location the lazy import resolves to. Same fix applied to meta_worker test.
- **Files modified:** `tests/test_states_worker.py`, `tests/test_meta_worker.py`
- **Commit:** f014b02 (fix included in GREEN commit)

## Threat Surface Scan

No new network endpoints, auth paths, or schema changes introduced.

T-03-04-03 mitigation applied: `_LOGGER.error(..., exc_info=True)` in the outer `except` block ensures full tracebacks appear in `home-assistant.log` before the exception is swallowed. The swallow is intentional — thread exits naturally; watchdog detects via `is_alive()`.

## Known Stubs

None — all hook methods are fully wired to real `hass.add_job` calls with the correct issue helper functions. No placeholder values or TODO stubs present.

## Self-Check: PASSED

Files exist:
- custom_components/ha_timescaledb_recorder/states_worker.py — FOUND (_stall_hook, _recovery_hook, _sustained_fail_hook, _last_exception, _run_main_loop, finally:)
- custom_components/ha_timescaledb_recorder/meta_worker.py — FOUND (same set of attributes and methods)
- tests/test_states_worker.py — FOUND (20 test functions)
- tests/test_meta_worker.py — FOUND (21 test functions)

Commits verified: 3016126, f014b02, 32e59c9, adab7ee
