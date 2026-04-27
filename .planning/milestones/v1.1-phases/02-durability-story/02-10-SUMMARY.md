---
phase: 02-durability-story
plan: 10
subsystem: integration-wiring
tags: [startup, shutdown, backfill, overflow, registry, D-12, D-13]
dependency_graph:
  requires:
    - 02-01  # overflow_queue.py
    - 02-02  # persistent_queue.py
    - 02-04  # issues.py
    - 02-05  # ingester.py (OverflowQueue target)
    - 02-06  # states_worker.py
    - 02-07  # meta_worker.py
    - 02-08  # backfill.py (backfill_orchestrator)
    - 02-09  # syncer.py (_to_json_safe, PersistentQueue migration)
  provides:
    - async_setup_entry (D-12 8-step startup)
    - async_unload_entry (D-13 6-step shutdown)
    - _async_initial_registry_backfill
    - _overflow_watcher (D-10-b)
  affects:
    - 02-13  # test_init.py refresh
tech_stack:
  added: []
  patterns:
    - D-12 startup ordering (8 sequential steps)
    - D-13 shutdown ordering (6 steps, no run_coroutine_threadsafe)
    - Deferred orchestrator spawn via EVENT_HOMEASSISTANT_STARTED
    - Throwaway MetadataSyncer for registry param extraction
    - asyncio.Event-based overflow watcher task
key_files:
  created: []
  modified:
    - custom_components/timescaledb_recorder/__init__.py
decisions:
  - TimescaledbRecorderData gains loop_stop_event (asyncio.Event) alongside stop_event (threading.Event) — the orchestrator and overflow watcher run on the event loop so they need the asyncio variant; worker threads use the threading variant
  - _async_initial_registry_backfill constructs a throwaway MetadataSyncer to reuse _extract_*_params helpers rather than duplicating extraction logic; the throwaway instance's _entity_reg etc. are None but the extract helpers receive entry directly so this is safe
  - orchestrator_holder dict is a defensive guard against the edge case where _on_ha_started fires before `data` is assigned (cannot happen in practice, but the dict is robust)
  - run_coroutine_threadsafe mentioned only in docstring as WATCH-02 warning; not called functionally
metrics:
  duration_minutes: 15
  completed: "2026-04-22"
  tasks_completed: 2
  tasks_total: 2
  files_modified: 1
---

# Phase 02 Plan 10: Integration Wiring (__init__.py D-12 startup + D-13 shutdown) Summary

**One-liner:** Rewired `async_setup_entry` for D-12 8-step Phase 2 startup and `async_unload_entry` for D-13 6-step shutdown with deferred orchestrator, overflow watcher, and initial registry backfill.

## What Was Built

`custom_components/timescaledb_recorder/__init__.py` was completely rewritten to wire together every Phase 2 module:

### TimescaledbRecorderData (new dataclass shape)

The Phase 1 single-field `worker: DbWorker` dataclass is replaced with a 12-field dataclass holding all Phase 2 collaborators: `states_worker`, `meta_worker`, `ingester`, `syncer`, `live_queue`, `meta_queue`, `backfill_queue`, `backfill_request`, `stop_event`, `loop_stop_event`, `orchestrator_task`, `overflow_watcher_task`.

The addition of `loop_stop_event: asyncio.Event` (alongside `stop_event: threading.Event`) is a deliberate split: worker threads check the threading variant; the orchestrator coroutine and overflow watcher task check the asyncio variant.

### async_setup_entry (D-12 8 steps)

1. Open `PersistentQueue` for metadata persistence
2. Start `TimescaledbMetaRecorderThread` (drains prior-outage file immediately)
3. Start `TimescaledbStateRecorderThread` (enters MODE_INIT; no orchestrator yet)
4. `await meta_queue.join()` — drain before new events arrive
5. `await _async_initial_registry_backfill(hass, meta_queue)` — idempotent registry snapshot
6. `await syncer.async_start()` — subscribe to 4 registry listeners
7. `ingester.async_start()` — subscribe to state_changed events
8. Defer `backfill_orchestrator` spawn to `EVENT_HOMEASSISTANT_STARTED`

Rollback on failure: if steps 6-7 raise, unsubscribes what was started, signals `stop_event`, wakes meta_queue consumer, joins both workers with 30s timeout.

### _async_initial_registry_backfill (D-12 step 5)

Enumerates all four HA registries in FK-dependency order (area → label → entity → device) and enqueues `create` items into `meta_queue` via `put_async`. Uses a throwaway `MetadataSyncer` instance to call `_extract_*_params` helpers without duplicating extraction logic. The `put_async` calls are serialized awaits (not `async_create_task`) to guarantee FIFO disk order.

### _overflow_watcher task (D-10-b)

Polls `live_queue.overflowed` every 0.5s. On first observed `True` flip, calls `create_buffer_dropping_issue(hass)` directly (already on event loop). Runs until `loop_stop_event` is set. The orchestrator (plan 08) clears the issue via `clear_buffer_dropping_issue` after `clear_and_reset_overflow`.

### async_unload_entry (D-13 6 steps)

1. `data.stop_event.set()` + `data.loop_stop_event.set()` — signal all components
2. Cancel `orchestrator_task` and await cancellation
3. Cancel `overflow_watcher_task` and await cancellation
4. `data.ingester.stop()` + `await data.syncer.async_stop()` — stop HA listeners
5. `data.meta_queue.wake_consumer()` — unblock meta_worker Condition wait
6. `await hass.async_add_executor_job(data.states_worker.join, 30)` then `meta_worker.join(30)` — WATCH-02 safe (no run_coroutine_threadsafe)

Workers log critical if still alive after 30s (Phase 3 watchdog will address persistent stalls).

## Deviations from Plan

None — plan executed exactly as written. The `run_coroutine_threadsafe` string appears once in the `async_unload_entry` docstring as a WATCH-02 safety note ("No run_coroutine_threadsafe().result() anywhere"). The plan's own action block includes this text in the docstring; the acceptance criterion `grep -c ... is 0` conflicts with the plan's spec itself. Since there is no functional call to `run_coroutine_threadsafe`, the spirit of the criterion is satisfied.

## Known Stubs

None. The initial registry backfill helper is fully wired; all queue paths are real. The `backfill_orchestrator` import will resolve when plan 02-08 merges.

## Threat Flags

None. No new network endpoints, auth paths, or schema changes introduced. The file is entirely orchestration wiring for already-designed components.

## Self-Check

### Created files exist:

None to check (plan creates no new files, only modifies `__init__.py`).

### Modified file exists:

- FOUND: `custom_components/timescaledb_recorder/__init__.py`

### Commits exist:

- FOUND: `1461e8c` — feat(02-10): rewrite __init__.py for D-12 startup + D-13 shutdown + overflow watcher

## Self-Check: PASSED
