---
phase: 03-hardening-and-observability
plan: "07"
subsystem: init-wiring
tags: [watchdog, orchestrator, recorder_disabled, async_setup_entry, async_unload_entry, tdd]
dependency_graph:
  requires:
    - watchdog_loop / spawn_states_worker / spawn_meta_worker (watchdog.py — Plan 06)
    - notify_watchdog_recovery (notifications.py — Plan 02)
    - create_recorder_disabled_issue / clear_recorder_disabled_issue (issues.py — Plan 01)
    - backfill_orchestrator with threading_stop_event kwarg (backfill.py — Plan 05)
    - HaTimescaleDBData (self — Phase 2)
  provides:
    - HaTimescaleDBData.watchdog_task (asyncio.Task | None)
    - HaTimescaleDBData.dsn (str)
    - HaTimescaleDBData.chunk_interval_days (int)
    - HaTimescaleDBData.compress_after_hours (int)
    - HaTimescaleDBData.entity_filter (Callable[[str], bool])
    - _make_orchestrator_done_callback (module-level closure factory)
    - _wait_for_recorder_and_clear (module-level async coroutine)
    - watchdog task spawned in async_setup_entry
    - orchestrator add_done_callback wired in _on_ha_started
    - recorder_disabled check + auto-clear background task in async_setup_entry
    - watchdog cancel+await before orchestrator cancel in async_unload_entry
  affects:
    - 03-08 (backfill orchestrator plan — async_setup_entry now fully wired)
tech_stack:
  added: []
  patterns:
    - TDD (RED gate d50c0c8 / GREEN gate 5b6d135)
    - asyncio.Task.add_done_callback with closure factory (_make_orchestrator_done_callback)
    - background coroutine task for auto-clear (async_wait_recorder pattern)
    - spawn-factory pattern for shared initial/respawn code path (D-05-c)
    - _TrackedFuture test helper for awaitable order-tracking in unload tests
key_files:
  created: []
  modified:
    - custom_components/ha_timescaledb_recorder/__init__.py
    - tests/test_init.py
decisions:
  - "async_wait_recorder chosen over async_track_state_change_event — HA 2026.3.4 has async_wait_recorder; hasattr guard added for forward-compat with future HA versions that might remove it"
  - "_make_orchestrator_done_callback is a module-level closure factory (not nested in async_setup_entry) to keep the done_callback accessible from tests and keep async_setup_entry body readable"
  - "_wait_for_recorder_and_clear is a module-level async function so it can be imported and tested in isolation, and patched by name in async_setup_entry tests"
  - "Phase 2 tests updated to patch spawn factories instead of direct constructors — factories now own worker construction; the observable behaviour is identical (workers started, joined on failure)"
  - "MagicMock.__await__ assignment is unreliable for async protocol; tests use real cancelled asyncio.Future objects or _TrackedFuture wrappers for awaitable task fields in unload tests"
metrics:
  duration: "~430 seconds"
  completed_date: "2026-04-23T08:54:45Z"
  tasks_completed: 1
  files_modified: 2
  files_created: 0
  tests_added: 21
---

# Phase 03 Plan 07: Integration Wiring Summary

`__init__.py` is now the live integration of every Phase 3 capability. Starting HA with this integration: spawns the watchdog supervisor, attaches crash recovery to the orchestrator task, checks for recorder availability at setup with automatic self-healing when the recorder loads, and unloads in the correct order (watchdog first, then orchestrator, then workers).

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 (RED) | Failing tests for Phase 3 __init__.py wiring | d50c0c8 | tests/test_init.py |
| 1 (GREEN) | Wire watchdog + done_callback + recorder_disabled + factories | 5b6d135 | custom_components/ha_timescaledb_recorder/__init__.py, tests/test_init.py |

## Final HaTimescaleDBData Field List

| Field | Type | Phase | Purpose |
|-------|------|-------|---------|
| `states_worker` | `TimescaledbStateRecorderThread \| None` | 2 | States DB worker thread |
| `meta_worker` | `TimescaledbMetaRecorderThread \| None` | 2 | Metadata DB worker thread |
| `ingester` | `StateIngester \| None` | 2 | HA state_changed event subscriber |
| `syncer` | `MetadataSyncer` | 2 | HA registry event subscriber |
| `live_queue` | `OverflowQueue` | 2 | Bounded live event buffer |
| `meta_queue` | `PersistentQueue` | 2 | File-persisted metadata queue |
| `backfill_queue` | `queue.Queue` | 2 | Backfill chunk dispatch queue |
| `backfill_request` | `asyncio.Event` | 2 | Backfill trigger signal |
| `stop_event` | `threading.Event` | 2 | Worker thread shutdown signal |
| `loop_stop_event` | `asyncio.Event` | 2 | Event-loop task shutdown signal |
| `orchestrator_task` | `asyncio.Task \| None` | 2 | Backfill orchestrator coroutine task |
| `overflow_watcher_task` | `asyncio.Task \| None` | 2 | Buffer-overflow repair-issue watcher |
| `watchdog_task` | `asyncio.Task \| None` | 3 | Worker thread supervisor task (D-05-a) |
| `dsn` | `str` | 3 | DB DSN for psycopg3 — factory input |
| `chunk_interval_days` | `int` | 3 | TimescaleDB chunk interval — factory input |
| `compress_after_hours` | `int` | 3 | TimescaleDB compression — factory input |
| `entity_filter` | `Callable[[str], bool]` | 3 | Entity filter — orchestrator/factory input |

## Final async_setup_entry Step Ordering

| Step | Action | Phase |
|------|--------|-------|
| 1 | Open PersistentQueue | 2 |
| 2 | `spawn_meta_worker(hass, data)` + `.start()` | 3 (was direct ctor in Ph2) |
| 3 | `spawn_states_worker(hass, data)` + `.start()` | 3 (was direct ctor in Ph2) |
| 4 | `await meta_queue.join()` (drain persisted items) | 2 |
| 5 | `await _async_initial_registry_backfill(...)` | 2 |
| 6 | `await syncer.async_start()` | 2 |
| 7 | `ingester.async_start()` | 2 |
| 7b | recorder_disabled check + spawn `_wait_for_recorder_and_clear` background task if needed | 3 |
| 7c | Spawn `overflow_watcher_task` | 2 |
| 7d | Spawn `watchdog_task = hass.async_create_task(watchdog_loop(hass, data))` | 3 |
| 8 | Register `_on_ha_started` listener (defers orchestrator spawn to HA start) | 2 |
| 8b | In `_on_ha_started`: create orchestrator task + attach `_make_orchestrator_done_callback` | 3 |

## Final async_unload_entry Step Ordering

| Step | Action | Phase | Note |
|------|--------|-------|------|
| 1 | `stop_event.set()` + `loop_stop_event.set()` | 2 | |
| 1b | `watchdog_task.cancel()` + `await watchdog_task` | 3 | **MEDIUM-12: must precede orchestrator cancel** |
| 2 | `orchestrator_task.cancel()` + `await` | 2 | |
| 2b | `overflow_watcher_task.cancel()` + `await` | 2 | |
| 3 | `ingester.stop()` + `await syncer.async_stop()` | 2 | None-guarded (Phase 3 workers may be None) |
| 4 | `meta_queue.wake_consumer()` | 2 | |
| 5 | `await async_add_executor_job(states_worker.join, 30)` | 2 | None-guarded |
| 5b | `await async_add_executor_job(meta_worker.join, 30)` | 2 | None-guarded |
| 6 | Connections closed inside worker `run()` — no action | 2 | |

## Recorder Auto-Clear Approach

**Approach chosen:** `ha_recorder.async_wait_recorder(hass)` (Option A from the plan).

`async_wait_recorder` is available in HA 2026.3.4 and resolves when the recorder becomes ready. A `hasattr(ha_recorder, "async_wait_recorder")` guard is included for forward-compatibility — if HA ever removes this API, `_wait_for_recorder_and_clear` logs a debug message and returns without clearing (the issue remains raised, requiring manual dismissal).

The `hasattr` check was **not** strictly needed for the target HA version (2026.3.4) but is required for the plan spec's "if absent, fall back" requirement and for long-term robustness.

When `async_wait_recorder` returns `None`, it signals the recorder integration is not configured at all (not just not-yet-ready), so the issue is intentionally not cleared.

## Behavioural Impact at Runtime (After This Plan Lands)

1. **Worker crash self-healing** — if `states_worker` or `meta_worker` dies unexpectedly, `watchdog_loop` detects it within `WATCHDOG_INTERVAL_S` seconds (10s), fires a `persistent_notification` with the exception traceback, waits 5s (MEDIUM-5 throttle), and spawns a fresh worker via the same factory used at startup.

2. **Orchestrator crash recovery** — if `backfill_orchestrator` raises an unhandled exception, `_on_orchestrator_done` fires `notify_watchdog_recovery(component="orchestrator")` and schedules `_relaunch()` which sleeps 5s (MEDIUM-6 throttle) before creating a new orchestrator task with `add_done_callback` re-attached. A fast-crashing orchestrator generates at most one notification per 5s.

3. **Recorder-disabled repair issue with auto-clear** — if HA's sqlite recorder is not loaded at setup time, a `recorder_disabled` Repairs entry appears immediately. When the recorder loads later (typical: recorder always loads, just after this component), `_wait_for_recorder_and_clear` resolves via `async_wait_recorder` and removes the issue automatically. No user action required.

4. **Safe shutdown** — `async_unload_entry` now cancels and fully awaits `watchdog_task` before cancelling the orchestrator and workers. The watchdog cannot attempt a mid-teardown respawn.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Phase 2 tests patched direct constructors, factories bypassed mocks**
- **Found during:** Task 1 (GREEN)
- **Issue:** `test_async_setup_entry_runs_d12_steps_in_order` and the other three Phase 2 setup/unload tests patched `TimescaledbMetaRecorderThread` / `TimescaledbStateRecorderThread` in the `__init__` module namespace. The factories in `watchdog.py` import those classes from their own module namespace, so the patches had no effect. Workers were constructed with real classes, causing errors.
- **Fix:** Updated all four Phase 2 tests to instead patch `spawn_states_worker` / `spawn_meta_worker` (which are imported into `__init__`'s namespace), returning controllable `MagicMock` workers. The observable assertions are identical — workers are started and joined on rollback.
- **Files modified:** `tests/test_init.py`
- **Commit:** 5b6d135

**2. [Rule 1 - Bug] AsyncMock wraps return_value in a coroutine — sentinel identity check failed**
- **Found during:** Task 1 (GREEN)
- **Issue:** Tests that verify "was coroutine X passed to async_create_task?" used sentinel `object()` as `mock_fn.return_value`. When `patch()` detects an async target, it creates `AsyncMock` which wraps any `return_value` in a new coroutine on each call — so the sentinel was never the object in `created_tasks`.
- **Fix:** Two tests (`test_setup_creates_watchdog_task`, `test_recorder_disabled_spawns_wait_task_on_issue_raised`) changed to `new=MagicMock(return_value=sentinel)` to force a sync mock whose call directly returns the sentinel.
- **Files modified:** `tests/test_init.py`
- **Commit:** 5b6d135

**3. [Rule 1 - Bug] MagicMock.__await__ assignment unreliable for async protocol**
- **Found during:** Task 1 (GREEN)
- **Issue:** Tests `test_unload_cancels_and_awaits_watchdog_before_orchestrator` and `test_unload_preserves_phase2_behaviour` tried to set `mock_obj.__await__ = real_future.__await__` on `MagicMock` instances. Python's `await` protocol looks up `__await__` on the type, not the instance — MagicMock's class doesn't propagate the assignment, so `await mock_obj` raised `TypeError: MagicMock object can't be awaited`.
- **Fix:** `test_unload_cancels_and_awaits_watchdog_before_orchestrator` uses a `_TrackedFuture` helper class with proper `__await__` and cancellation order tracking. `test_unload_preserves_phase2_behaviour` uses real cancelled `asyncio.Future` objects directly.
- **Files modified:** `tests/test_init.py`
- **Commit:** 5b6d135

## Test Count Breakdown

### tests/test_init.py

| Category | Tests |
|----------|-------|
| Phase 2 preserved (updated for factory patches) | 6 |
| Phase 3 new (Plan 07) | 21 |
| Total | 27 |

New test functions (21):
1. `test_data_dataclass_has_phase3_fields`
2. `test_data_dataclass_preserves_phase2_fields`
3. `test_setup_spawns_workers_via_factories`
4. `test_setup_creates_watchdog_task`
5. `test_setup_passes_threading_stop_event_to_orchestrator`
6. `test_orchestrator_done_callback_returns_on_cancelled_task`
7. `test_orchestrator_done_callback_returns_on_stop_event_set`
8. `test_orchestrator_done_callback_returns_on_loop_stop_event_set`
9. `test_orchestrator_done_callback_returns_on_clean_exit`
10. `test_orchestrator_done_callback_fires_notify_and_schedules_relaunch`
11. `test_orchestrator_relaunch_sleeps_5s_before_new_task`
12. `test_orchestrator_relaunch_aborts_if_stop_event_set_during_sleep`
13. `test_recorder_disabled_check_fires_issue_on_keyerror`
14. `test_recorder_disabled_check_fires_issue_when_enabled_false`
15. `test_recorder_disabled_check_silent_when_enabled_true`
16. `test_recorder_disabled_spawns_wait_task_on_issue_raised`
17. `test_wait_for_recorder_and_clear_calls_clear_when_recorder_enabled`
18. `test_wait_for_recorder_and_clear_does_not_clear_when_recorder_none`
19. `test_unload_cancels_and_awaits_watchdog_before_orchestrator`
20. `test_unload_handles_none_watchdog_task_gracefully`
21. `test_unload_preserves_phase2_behaviour`

Full suite: 194 passed (27 new + 167 pre-existing).

## Threat Surface Scan

No new network endpoints, file access patterns, or auth paths introduced.

T-03-07-01 mitigation applied: `await asyncio.sleep(5)` in `_relaunch()` throttles crash-loop notification rate.
T-03-07-02 mitigation applied: `await data.watchdog_task` after `cancel()` in `async_unload_entry` ensures watchdog fully stops before worker teardown.
T-03-07-03 accepted: `_wait_for_recorder_and_clear` is a background task that blocks until recorder is ready — does not block setup; HA manages lifecycle via `async_wait_recorder`.
T-03-07-04 mitigation applied: `stop_event.is_set()` and `loop_stop_event.is_set()` re-checked inside `_relaunch()` after the 5s sleep before spawning the new orchestrator.

## Known Stubs

None — all wiring is fully implemented. `_wait_for_recorder_and_clear` has a `hasattr` fallback path that is a no-op (logs debug), but this is an intentional forward-compatibility guard, not a stub.

## TDD Gate Compliance

- RED gate: commit `d50c0c8` — `test(03-07): add failing tests for Phase 3 __init__.py wiring`
- GREEN gate: commit `5b6d135` — `feat(03-07): wire watchdog + orchestrator done_callback + recorder_disabled check + auto-clear in __init__.py`
- REFACTOR gate: not needed — implementation was clean on first pass

## Self-Check: PASSED

Files exist:
- `custom_components/ha_timescaledb_recorder/__init__.py` — FOUND
- `tests/test_init.py` — FOUND (27 test functions)

Acceptance criteria verified:
- `watchdog_task: asyncio.Task | None` field: 1 occurrence
- `dsn: str` field: 1
- `chunk_interval_days: int` field: 1
- `compress_after_hours: int` field: 1
- `entity_filter: Callable` field: 1
- `spawn_states_worker(hass, data)`: 1
- `spawn_meta_worker(hass, data)`: 1
- `hass.async_create_task(watchdog_loop`: 1
- `add_done_callback(`: 2 (initial attach + recursive in done_callback)
- `_make_orchestrator_done_callback` def: 1
- `component="orchestrator"`: 1
- `await asyncio.sleep(5)`: 1
- `threading_stop_event=stop_event`: 1
- `ha_recorder.get_instance(hass)`: 1
- `create_recorder_disabled_issue(hass)`: 2 (KeyError branch + enabled=False branch)
- `clear_recorder_disabled_issue`: 2
- `_wait_for_recorder_and_clear` def: 1
- `async_wait_recorder`: 6
- `data.watchdog_task.cancel()`: 1
- `await data.watchdog_task`: 1
- Watchdog cancel (line 476) precedes orchestrator cancel (line 484): VERIFIED
- 27 test functions: VERIFIED
- 194 tests pass: VERIFIED
- Smoke test: PASSED

Commits verified: d50c0c8 (RED), 5b6d135 (GREEN)
