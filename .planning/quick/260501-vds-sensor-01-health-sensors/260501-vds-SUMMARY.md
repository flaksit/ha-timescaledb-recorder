---
quick_id: 260501-vds
slug: sensor-01-health-sensors
status: complete
completed: "2026-05-01"
duration_seconds: 382
tasks_completed: 2
tasks_total: 2
commits:
  - 4c49f88
  - 7fecb8b
key_files_created:
  - custom_components/timescaledb_recorder/sensor.py
  - custom_components/timescaledb_recorder/binary_sensor.py
  - tests/test_sensor.py
  - tests/test_binary_sensor.py
key_files_modified:
  - custom_components/timescaledb_recorder/const.py
  - custom_components/timescaledb_recorder/overflow_queue.py
  - custom_components/timescaledb_recorder/persistent_queue.py
  - custom_components/timescaledb_recorder/watchdog.py
  - custom_components/timescaledb_recorder/__init__.py
  - custom_components/timescaledb_recorder/strings.json
  - custom_components/timescaledb_recorder/translations/en.json
  - tests/test_init.py
decisions:
  - "Use BinarySensorDeviceClass.CONNECTIVITY (not RUNNING, which does not exist) for worker alive sensors"
  - "get_and_reset_peak() resets to qsize() not 0 to avoid reporting zero during steady-state queue depth"
  - "_all_domain_issue_ids() used (not filtered by is_persistent) since active raised issues are what health/db_status needs"
---

# Quick Task 260501-vds: SENSOR-01 Health Sensors — Summary

8 diagnostic sensor/binary_sensor entities exposing TimescaleDB Recorder internal health via issue_registry polling and dispatcher push-updates.

## What Was Built

### Task 1 — Backend instrumentation + platform wiring (commit 4c49f88)

**const.py**: Added `PLATFORMS = ["sensor", "binary_sensor"]`, `SIGNAL_OVERFLOW_CHANGE`, `SIGNAL_WORKER_STATE_CHANGE` dispatcher signal constants.

**overflow_queue.py**: Added `_peak_size: int` (peak depth since last `get_and_reset_peak()` call), `_total_dropped: int` (monotonically increasing drop counter across all outages). Both updated under `_overflow_lock`. Added `get_and_reset_peak()` method that resets baseline to `qsize()` (not 0) so sensors report meaningful deltas.

**persistent_queue.py**: Added `_size: int` counter, initialised by `_count_lines()` at startup. Incremented in `put()` and `put_many()`, decremented in `task_done()` with underflow guard. Added `__len__()`.

**watchdog.py**: Imported `async_dispatcher_send`. `_watchdog_respawn` now dispatches `SIGNAL_WORKER_STATE_CHANGE` before the 5s throttle sleep (worker dead) and after `new_thread.start()` (worker respawned).

**__init__.py**: Imported `PLATFORMS`, `SIGNAL_OVERFLOW_CHANGE`, `async_dispatcher_send`. Updated `_overflow_watcher` from `if now and not last_seen` to `if now != last_seen` — dispatches `SIGNAL_OVERFLOW_CHANGE` on both transitions; `create_buffer_dropping_issue` only on False→True. Added `async_forward_entry_setups(entry, PLATFORMS)` before `return True` in `async_setup_entry`. Added `async_unload_platforms(entry, PLATFORMS)` before `return True` in `async_unload_entry`.

### Task 2 — Sensor entities, translations, tests (commit 7fecb8b)

**sensor.py**: Five polled `SensorEntity` subclasses — `TimescaledbHealthSensor` (5s, ok/degraded/error rollup), `TimescaledbDbStatusSensor` (5s, connected/disconnected/stalled), `TimescaledbQueueDepthSensor` (10s, calls `get_and_reset_peak()`), `TimescaledbEventsDroppedSensor` (10s, delta of `_total_dropped`), `TimescaledbMetaQueueDepthSensor` (30s, `len(meta_queue)`). All use `_attr_has_entity_name = True`, `EntityCategory.DIAGNOSTIC`, `DeviceEntryType.SERVICE` device.

**binary_sensor.py**: Three push-updated `BinarySensorEntity` subclasses — `TimescaledbBufferOverflowSensor` (PROBLEM class, `SIGNAL_OVERFLOW_CHANGE`), `TimescaledbStatesWorkerAliveSensor` (CONNECTIVITY class, `SIGNAL_WORKER_STATE_CHANGE`), `TimescaledbMetaWorkerAliveSensor` (CONNECTIVITY class, `SIGNAL_WORKER_STATE_CHANGE`). All set initial state in `async_added_to_hass` before subscribing and unsubscribe via `async_on_remove`.

**strings.json + translations/en.json**: Added `"entity"` section with human-readable names for all 8 entity keys.

**tests/test_sensor.py**: 11 unit tests covering all health/db_status states, queue peak, events dropped delta and zero, meta queue depth.

**tests/test_binary_sensor.py**: 8 unit tests covering initial state (true/false) and push dispatch for all three sensors.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] test_init.py broke on new async_forward_entry_setups/async_unload_platforms calls**

- **Found during:** Task 2 full-suite regression run
- **Issue:** Existing tests in test_init.py created `hass = MagicMock()` without making `config_entries.async_forward_entry_setups` and `config_entries.async_unload_platforms` awaitable. Added platform calls in __init__.py caused `TypeError: 'MagicMock' object can't be awaited`.
- **Fix:** Updated the shared `hass` fixture in test_init.py to set `h.config_entries.async_forward_entry_setups = AsyncMock(return_value=None)` and `h.config_entries.async_unload_platforms = AsyncMock(return_value=None)`. Also patched three inline `hass = MagicMock()` blocks in tests that call `async_unload_entry` directly.
- **Files modified:** tests/test_init.py
- **Commit:** 7fecb8b

## Test Results

```
tests/test_sensor.py: 11 passed
tests/test_binary_sensor.py: 8 passed
Full suite: 234 passed, 19 warnings (pre-existing)
```

## Self-Check: PASSED

Files created:
- /custom_components/timescaledb_recorder/sensor.py — FOUND
- /custom_components/timescaledb_recorder/binary_sensor.py — FOUND
- /tests/test_sensor.py — FOUND
- /tests/test_binary_sensor.py — FOUND

Commits:
- 4c49f88 — FOUND
- 7fecb8b — FOUND
