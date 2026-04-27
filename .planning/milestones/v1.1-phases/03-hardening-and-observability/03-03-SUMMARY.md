---
phase: 03-hardening-and-observability
plan: "03"
subsystem: retry-decorator
tags: [retry, observability, tdd, on_recovery, on_sustained_fail, monotonic]
dependency_graph:
  requires:
    - STALL_THRESHOLD (const.py — Plan 01)
  provides:
    - retry_until_success with on_recovery, on_sustained_fail, sustained_fail_seconds kwargs
    - closure state variables: stalled, first_fail_ts, sustained_notified
    - time.monotonic() based threshold timing (MEDIUM-7 fix)
    - event-loop blocking constraint documented in docstring (HIGH-3)
  affects:
    - 03-04 (states worker wires on_recovery=clear_states_worker_stalled_issue, on_sustained_fail=create_db_unreachable_issue)
    - 03-05 (meta worker wires on_recovery=clear_meta_worker_stalled_issue, on_sustained_fail=create_db_unreachable_issue)
    - 03-06 (backfill _fetch_slice_raw wrapped with on_transient=None)
tech_stack:
  added: []
  patterns:
    - TDD (RED/GREEN per plan type: tdd)
    - time.monotonic() for all elapsed-duration checks (immune to wall-clock jumps)
    - Best-effort hooks: exceptions logged and swallowed (threat T-03-03-02 mitigate)
key_files:
  created:
    - tests/test_retry.py (new test functions added to existing file)
  modified:
    - custom_components/timescaledb_recorder/retry.py
    - tests/test_retry.py
decisions:
  - "time.monotonic() used for all threshold timing — immune to NTP adjustments, DST, VM migration (MEDIUM-7)"
  - "on_recovery arms via stalled flag at stall_threshold crossing (not on first failure) — avoids spurious recovery fires on brief transients"
  - "on_sustained_fail guards with sustained_notified bool — fires exactly once per streak regardless of how long failure continues"
  - "_STALL_NOTIFY_THRESHOLD local constant removed; stall_threshold default is now STALL_THRESHOLD imported from const.py (D-03-d)"
  - "Test file updated to remove _STALL_NOTIFY_THRESHOLD import (constant no longer exported by retry.py)"
metrics:
  duration: "183 seconds"
  completed_date: "2026-04-23T08:16:22Z"
  tasks_completed: 2
  files_modified: 2
  files_created: 0
  tests_added: 12
---

# Phase 03 Plan 03: Extended Retry Decorator (on_recovery + on_sustained_fail) Summary

Extended `retry_until_success` with three new keyword-only hooks and monotonic-time closure state, enabling auto-clear of repair issues on DB recovery (OBS-03) and db_unreachable detection after sustained failure (OBS-02).

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 (RED) | Failing tests for on_recovery + on_sustained_fail | c3a005c | tests/test_retry.py |
| 2 (GREEN) | Extend retry.py; update test import | 5c51151 | custom_components/timescaledb_recorder/retry.py, tests/test_retry.py |

## New Signature

```python
def retry_until_success(
    *,
    stop_event: threading.Event,
    on_transient: Callable[[], None] | None = None,        # unchanged (optional)
    notify_stall: Callable[[int], None] | None = None,      # unchanged
    on_recovery: Callable[[], None] | None = None,          # NEW D-03-a
    on_sustained_fail: Callable[[], None] | None = None,    # NEW D-11
    sustained_fail_seconds: float = 300.0,                   # NEW D-11
    backoff_schedule: tuple[int, ...] = _BACKOFF_SCHEDULE,
    stall_threshold: int = STALL_THRESHOLD,                  # now from const.py
) -> Callable:
```

## Closure State Variables (for Plan 04/05 hook wiring reference)

| Variable | Type | Role |
|----------|------|------|
| `attempts` | `int` | consecutive failure counter; reset to 0 on success |
| `notified` | `bool` | stall-notified flag; set True at stall_threshold, reset on success |
| `stalled` | `bool` | arms `on_recovery`; set True at stall_threshold, set False after on_recovery fires |
| `first_fail_ts` | `float | None` | `time.monotonic()` timestamp of first failure in current streak; None on success |
| `sustained_notified` | `bool` | guards `on_sustained_fail` from firing twice in same streak; reset on success |

## Hook Firing Rules (living documentation)

### on_recovery
- **Arms:** when `attempts >= stall_threshold` (same condition as `notify_stall`)
- **Fires:** exactly once, on the first successful call AFTER stall threshold was crossed
- **Rearms:** after each success resets `stalled = False`; a new stall cycle in the same wrapped function will re-arm and fire again
- **Does NOT fire:** on success that occurs before any stall (attempts never reached threshold)

### on_sustained_fail
- **Timer:** `time.monotonic()` — immune to NTP adjustments, DST, VM migration (MEDIUM-7)
- **Fires:** exactly once per streak when `now_mono - first_fail_ts >= sustained_fail_seconds`
- **Does NOT fire twice:** `sustained_notified = True` after first firing; guards repeated calls in the same streak
- **Resets:** `first_fail_ts = None; sustained_notified = False` on any successful call

### All hooks (including on_transient, notify_stall)
- **Best-effort:** exceptions inside hooks are caught, logged (`_LOGGER.exception`), and swallowed
- **Non-blocking:** hooks run synchronously in the worker thread; they should be fast (e.g., `hass.add_job(...)`)

## Event-Loop Blocking Constraint (HIGH-3)

Documented in the decorator docstring:

> IMPORTANT: This decorator uses threading.Event.wait(backoff) which is a
> SYNCHRONOUS blocking call. It is intended for use in dedicated OS threads
> or executor jobs (async_add_executor_job) ONLY. Never call a wrapped
> function directly from the HA event loop — it will block the loop.
> (Cross-AI review 2026-04-23, concern HIGH-3.)

All current call sites satisfy this constraint (states_worker thread, meta_worker thread, backfill via `recorder_instance.async_add_executor_job`). This constraint applies to future users of the decorator.

## Plan 04/05 Worker Hook Wiring Pattern

```python
# states_worker.py — inside __init__ or where retry wrapper is created
@retry_until_success(
    stop_event=self._stop_event,
    on_transient=self._reset_db_connection,
    notify_stall=lambda n: hass.add_job(create_states_worker_stalled_issue, hass),
    on_recovery=lambda: hass.add_job(clear_states_worker_stalled_issue, hass),
    on_sustained_fail=lambda: hass.add_job(create_db_unreachable_issue, hass),
    sustained_fail_seconds=DB_UNREACHABLE_THRESHOLD_SECONDS,
)
def _insert_chunk(...): ...
```

## Test Functions Added (12 new)

1. `test_on_recovery_fires_once_after_stall_then_success`
2. `test_on_recovery_not_fired_on_success_without_stall`
3. `test_on_recovery_rearms_after_recovery_and_new_stall`
4. `test_on_sustained_fail_fires_once_when_duration_exceeded`
5. `test_on_sustained_fail_not_called_twice_in_same_streak`
6. `test_on_sustained_fail_resets_after_success`
7. `test_on_transient_none_is_ok`
8. `test_stall_threshold_default_imports_from_const`
9. `test_on_recovery_hook_exception_logged_and_swallowed`
10. `test_on_sustained_fail_hook_exception_logged_and_swallowed`
11. `test_per_wrapper_independent_state`
12. `test_on_sustained_fail_uses_monotonic_not_wall_clock`

Full suite: 127 passed (up from 106 at end of Plan 01).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Updated test import after _STALL_NOTIFY_THRESHOLD removal**
- **Found during:** Task 2 (GREEN)
- **Issue:** Plan Task 2 action only specified modifying `retry.py`, but removing `_STALL_NOTIFY_THRESHOLD` from `retry.py` would cause the existing `test_default_backoff_constants` test to fail with `ImportError` (it imported the constant by name). Plan acceptance criteria requires "all existing tests still pass."
- **Fix:** Removed `_STALL_NOTIFY_THRESHOLD` from the test's import statement; updated `test_default_backoff_constants` to assert `const.STALL_THRESHOLD == 5` instead.
- **Files modified:** `tests/test_retry.py`
- **Commit:** `5c51151`

## TDD Gate Compliance

Gate sequence in git log:
1. RED gate: `c3a005c` — `test(03-03): add failing tests for on_recovery + on_sustained_fail hooks`
2. GREEN gate: `5c51151` — `feat(03-03): extend retry decorator with on_recovery + on_sustained_fail hooks`

Both gates present and in correct order.

## Threat Surface Scan

No new network endpoints, auth paths, file access patterns, or schema changes introduced. All new code is closure state + hook dispatch inside the existing retry decorator. Threat T-03-03-02 (hook exception swallowing) is mitigated: all hook exceptions are logged via `_LOGGER.exception` before swallowing, making misbehaving hooks visible in the HA log.

## Known Stubs

None — all new code paths are fully implemented. Hook callables are passed by the caller at wrapper construction time; no placeholder values or TODO stubs present.

## Self-Check: PASSED

Files exist:
- custom_components/timescaledb_recorder/retry.py — FOUND (on_recovery, on_sustained_fail, time.monotonic)
- tests/test_retry.py — FOUND (20 test functions, 12 new)

Commits verified: c3a005c, 5c51151
