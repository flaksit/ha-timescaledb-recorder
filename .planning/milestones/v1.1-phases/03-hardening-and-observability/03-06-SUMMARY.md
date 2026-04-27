---
phase: 03-hardening-and-observability
plan: "06"
subsystem: watchdog
tags: [watchdog, async, supervisor, respawn, notifications, tdd]
dependency_graph:
  requires:
    - notify_watchdog_recovery (notifications.py — Plan 02)
    - _last_exception/_last_context on both workers (states_worker.py, meta_worker.py — Plan 04)
    - WATCHDOG_INTERVAL_S (const.py — Plan 01)
  provides:
    - watchdog_loop (async coroutine — supervisor polling both workers)
    - spawn_states_worker (factory — constructs TimescaledbStateRecorderThread from runtime)
    - spawn_meta_worker (factory — constructs TimescaledbMetaRecorderThread from runtime)
    - _watchdog_respawn (async helper — notify + throttle + respawn)
  affects:
    - 03-07 (async_setup_entry wires watchdog_task; TimescaledbRecorderData gains dsn/queue fields that factories read)
tech_stack:
  added: []
  patterns:
    - TDD (RED gate aaf6e65 / GREEN gate a6212ce)
    - asyncio.wait_for interruptible sleep pattern (mirrors _overflow_watcher)
    - getattr defensive reads for post-mortem thread attributes
    - try/except Exception poll-body guard (MEDIUM-9)
    - asyncio.sleep(5) restart throttle (MEDIUM-5)
key_files:
  created:
    - custom_components/timescaledb_recorder/watchdog.py
    - tests/test_watchdog.py
  modified: []
decisions:
  - "_watchdog_respawn is async def (not sync) — it must await asyncio.sleep(5) for the restart throttle; making it async is correct even though the sleep is the only await"
  - "Restart throttle placed between notify and respawn — this ensures the notification is visible to the user before the potentially-crashing worker is restarted, preventing silent crash-loop churn (MEDIUM-5)"
  - "Poll body try/except catches all Exception — a monitoring bug (e.g. AttributeError on runtime) must not kill the watchdog; WARNING is logged and loop continues (MEDIUM-9)"
  - "Factories use TYPE_CHECKING import of TimescaledbRecorderData — avoids circular import of __init__.py at module load time; fields are accessed at runtime, not import time"
  - "Test async functions use async def + @pytest.mark.asyncio pattern; asyncio.wait_for is patched with side_effect to simulate cadence without real timers"
metrics:
  duration: "176 seconds"
  completed_date: "2026-04-23T00:00:00Z"
  tasks_completed: 1
  files_modified: 0
  files_created: 2
  tests_added: 14
---

# Phase 03 Plan 06: Watchdog Module Summary

New module `watchdog.py` — async supervisor turning "thread silently dies" into "notify + respawn within WATCHDOG_INTERVAL_S seconds." Implements the D-05 watchdog_loop coroutine plus two spawn factories that will be used by Plan 07's `async_setup_entry` for initial thread creation as well as post-crash respawn.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 (RED) | Failing tests for watchdog_loop + factories | aaf6e65 | tests/test_watchdog.py |
| 1 (GREEN) | watchdog.py implementation | a6212ce | custom_components/timescaledb_recorder/watchdog.py |

## Watchdog Loop Architecture

### Cadence and Interrupt Mechanism

`watchdog_loop(hass, runtime)` runs an infinite loop interrupted by `runtime.loop_stop_event`:

```python
await asyncio.wait_for(runtime.loop_stop_event.wait(), timeout=WATCHDOG_INTERVAL_S)
return  # loop_stop_event set — clean shutdown
# asyncio.TimeoutError → normal cadence tick; poll body runs
```

This mirrors the `_overflow_watcher` pattern from `__init__.py`. Normal exit is always via `loop_stop_event.set()` — the loop never needs a separate break condition. A secondary guard checks `runtime.stop_event.is_set()` after the timeout to catch shutdown signals that arrive between the wait and the poll.

### Restart Throttle (MEDIUM-5)

`_watchdog_respawn` awaits `asyncio.sleep(5)` **between** calling `notify_watchdog_recovery` and calling `new_thread.start()`. Call order is enforced in tests:

```
notify_watchdog_recovery → asyncio.sleep(5) → new_thread.start()
```

A worker that crashes immediately on start would otherwise create a tight crash/notify/respawn storm. With the 5s delay and WATCHDOG_INTERVAL_S=10s, worst-case detection-to-respawn is ~15s — acceptable for a self-healing integration.

### Poll-Body Guard (MEDIUM-9)

The entire `is_alive()` check block runs inside `try/except Exception`:

```python
try:
    if not runtime.states_worker.is_alive() and ...:
        await _watchdog_respawn(...)
    if not runtime.meta_worker.is_alive() and ...:
        await _watchdog_respawn(...)
except Exception:  # noqa: BLE001
    _LOGGER.warning("Watchdog poll cycle raised unexpected exception — loop continues", exc_info=True)
```

A bug in one monitoring cycle (AttributeError, unexpected RuntimeError, etc.) logs a WARNING and continues. The watchdog task itself never terminates due to a monitoring bug.

### Dead-Thread Detection

Detection criteria: `not thread.is_alive() and not runtime.stop_event.is_set()`.

- `is_alive() == False` → thread has fully exited; Python memory model guarantees `_last_exception` / `_last_context` are visible.
- `stop_event.is_set()` guard → during HA shutdown the workers are stopped intentionally; watchdog must not re-raise them.

Post-mortem attributes are read with `getattr(..., default)` to handle threads constructed without Phase 3 hooks:

```python
exc = getattr(dead_thread, "_last_exception", None)
ctx = getattr(dead_thread, "_last_context", {}) or {}
```

## Spawn Factories

### spawn_states_worker

Constructs `TimescaledbStateRecorderThread` from runtime fields. Returns unstarted thread. Caller (watchdog or Plan 07 setup) calls `.start()`.

Required runtime fields consumed:
- `runtime.dsn` — DSN string for psycopg3 connection
- `runtime.live_queue` — OverflowQueue for state events
- `runtime.backfill_queue` — queue.Queue for backfill chunks
- `runtime.backfill_request` — asyncio.Event signaling backfill needed
- `runtime.stop_event` — shared threading.Event for graceful stop
- `runtime.chunk_interval_days` — TimescaleDB chunk interval
- `runtime.compress_after_hours` — TimescaleDB compression policy

### spawn_meta_worker

Constructs `TimescaledbMetaRecorderThread` from runtime fields. Returns unstarted thread.

Required runtime fields consumed:
- `runtime.dsn` — DSN string
- `runtime.meta_queue` — PersistentQueue for registry metadata
- `runtime.syncer` — MetadataSyncer instance
- `runtime.stop_event` — shared threading.Event

### Plan 07 Note

TimescaledbRecorderData currently does NOT have the fields `dsn`, `chunk_interval_days`, `compress_after_hours`, `live_queue`, `backfill_queue`, `backfill_request`, `meta_queue`, `syncer` as dataclass fields — they are passed directly to worker constructors in `async_setup_entry`. **Plan 07 must add these fields to TimescaledbRecorderData** before calling the factories. The factories here have been written against the expected Plan 07 runtime shape.

## Test Coverage (14 tests)

| Test | Behaviour |
|------|-----------|
| `test_watchdog_loop_is_coroutine` | inspect.iscoroutinefunction returns True |
| `test_watchdog_returns_immediately_when_loop_stop_event_preset` | Early exit with no polling |
| `test_watchdog_no_action_when_both_workers_alive` | No notify, no sleep when both alive |
| `test_watchdog_respawns_dead_states_worker` | runtime.states_worker replaced; start() called |
| `test_watchdog_respawns_dead_meta_worker` | runtime.meta_worker replaced; start() called |
| `test_watchdog_fires_notify_with_last_exception_and_context` | notify called with correct exc + ctx |
| `test_watchdog_sleeps_5s_before_respawn` | Call order: notify → sleep(5) → start() (MEDIUM-5) |
| `test_watchdog_skips_respawn_when_stop_event_set` | Shutdown guard: no notify, no respawn |
| `test_watchdog_defensive_none_exc_and_missing_context` | Missing attributes → exc=None, ctx={} |
| `test_watchdog_poll_body_exception_does_not_kill_loop` | AttributeError in poll → loop continues (MEDIUM-9) |
| `test_watchdog_polls_cadence_interruptible_by_loop_stop_event` | is_alive polled >= 2 times before stop |
| `test_spawn_states_worker_uses_runtime_fields` | All 8 constructor args sourced from runtime |
| `test_spawn_meta_worker_uses_runtime_fields` | All 5 constructor args sourced from runtime |
| `test_spawn_factories_do_not_start_thread` | .start() not called by factory |

Full suite: 163 passed (14 new + 149 pre-existing).

## Deviations from Plan

None — plan executed exactly as written. Test functions use `async def` with `@pytest.mark.asyncio` (required for async coroutines under pytest-asyncio), which is consistent with the plan spec that described async test functions.

## Known Stubs

None — `watchdog_loop` is fully implemented and functional. The `spawn_*` factories reference `runtime.dsn` etc. which do not yet exist on `TimescaledbRecorderData` — but this is intentional and documented above: Plan 07 adds those fields. The factories are not stubs; they are correctly implemented ahead of Plan 07's dataclass extension.

## Threat Surface Scan

No new network endpoints, file access patterns, or auth paths introduced.

T-03-06-01 mitigation applied: `asyncio.sleep(5)` in `_watchdog_respawn` throttles crash-loop respawn rate.
T-03-06-02 mitigation applied: `try/except Exception` wraps poll body; WARNING logged; loop continues.
T-03-06-03 accepted: `_last_exception` appears in notification body — operational data for HA admin; no PII expected.
T-03-06-04 mitigation applied: factories source all args from `runtime` — same queues/stop_event guaranteed.

## TDD Gate Compliance

- RED gate: commit `aaf6e65` — `test(03-06): add failing tests for watchdog_loop + spawn factories`
- GREEN gate: commit `a6212ce` — `feat(03-06): watchdog.py with async watchdog_loop + spawn factories`
- REFACTOR gate: not needed — implementation was clean on first pass

## Self-Check: PASSED

- FOUND: `custom_components/timescaledb_recorder/watchdog.py`
- FOUND: `tests/test_watchdog.py`
- FOUND: commit `aaf6e65` (RED gate)
- FOUND: commit `a6212ce` (GREEN gate)
- 163 tests pass; no regressions
