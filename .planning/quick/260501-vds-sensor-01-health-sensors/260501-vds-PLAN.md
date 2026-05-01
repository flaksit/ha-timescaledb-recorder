---
quick_id: 260501-vds
slug: sensor-01-health-sensors
description: "SENSOR-01: 8 health sensor/binary_sensor entities for TimescaleDB Recorder"
status: planned
created: "2026-05-01"
---

# Quick Task 260501-vds: SENSOR-01 Health Sensors

## Objective

Add 8 diagnostic entities (5 sensors + 3 binary sensors) exposing the integration's
internal health state. All entities attach to a single virtual device under the config
entry. Push-updated where cost is low (overflow, worker liveness); polled at 5–30s
for everything else.

## Scope Decisions (locked)

| Decision | Value |
|----------|-------|
| Sensors | 5 `sensor` + 3 `binary_sensor` — separate entities, not attributes |
| Update: health / db_status | poll 5s — reads issue_registry |
| Update: queue metrics | poll 10s with peak/delta |
| Update: meta queue depth | poll 30s |
| Update: overflow, worker liveness | push via HA dispatcher signals |
| Device linkage | `DeviceEntryType.SERVICE` device, keyed by `entry.entry_id` |
| Entity category | `EntityCategory.DIAGNOSTIC` for all 8 |

## Entity Catalogue

### sensor platform

| Entity suffix | State | Source | Interval |
|--------------|-------|--------|---------|
| `health` | `ok` / `degraded` / `error` | issue_registry + worker liveness | 5s |
| `db_status` | `connected` / `disconnected` / `stalled` | issue_registry (db_unreachable, states_worker_stalled) | 5s |
| `queue_depth_peak` | int (peak live_queue qsize since last poll) | `live_queue.get_and_reset_peak()` | 10s |
| `events_dropped` | int (delta total drops since last poll) | `live_queue._total_dropped` delta | 10s |
| `meta_queue_depth` | int | `len(meta_queue)` | 30s |

Health rollup:
- `error` if any of: db_unreachable, states_worker_stalled, meta_worker_stalled issues active, OR either worker not alive
- `degraded` if any of: buffer_dropping, recorder_disabled issues active (workers alive, DB reachable)
- `ok` otherwise

DB status:
- `stalled` if states_worker_stalled issue active
- `disconnected` if db_unreachable issue active
- `connected` otherwise

### binary_sensor platform

| Entity suffix | On | Off | Device class | Push signal |
|--------------|-----|-----|-------------|-------------|
| `buffer_overflow` | overflowing | normal | `PROBLEM` | `SIGNAL_OVERFLOW_CHANGE` |
| `states_worker_alive` | alive | dead/restarting | `RUNNING` | `SIGNAL_WORKER_STATE_CHANGE` |
| `meta_worker_alive` | alive | dead/restarting | `RUNNING` | `SIGNAL_WORKER_STATE_CHANGE` |

Note: `BinarySensorDeviceClass.RUNNING` does not exist in HA. Use
`BinarySensorDeviceClass.CONNECTIVITY` for worker alive sensors (on = connected/alive).

---

## Task 1 — Backend instrumentation + platform wiring

### Files

- `custom_components/timescaledb_recorder/overflow_queue.py`
- `custom_components/timescaledb_recorder/persistent_queue.py`
- `custom_components/timescaledb_recorder/const.py`
- `custom_components/timescaledb_recorder/watchdog.py`
- `custom_components/timescaledb_recorder/__init__.py`

### overflow_queue.py changes

Add to `__init__`: `self._peak_size: int = 0`, `self._total_dropped: int = 0`.

In `put_nowait`:
- Track peak: `self._peak_size = max(self._peak_size, self.qsize())` after successful `super().put()` (inside try, before except)
- Track total drops: `self._total_dropped += 1` inside `except queue.Full` (alongside existing `self._dropped += 1`)

Add method `get_and_reset_peak(self) -> int`:
- Acquires `_overflow_lock`
- Stores `peak = self._peak_size`
- Resets `self._peak_size = self.qsize()` (not 0 — preserves current depth as new baseline)
- Returns `peak`

Do NOT touch `_dropped` or `clear_and_reset_overflow` — orchestrator behavior unchanged.

### persistent_queue.py changes

Add `self._size: int = 0` to `__init__`, initialised by counting non-empty lines in the
queue file if it exists (call a private `_count_lines_locked()` helper, no lock needed
at init time since consumer has not started yet).

Increment `_size` in `put()` and `put_many()` (under `_cond`).
Decrement `_size` in `task_done()` (under `_cond`). Guard: never go below 0.

Add `def __len__(self) -> int: return self._size`.

### const.py additions

```python
PLATFORMS: list[str] = ["sensor", "binary_sensor"]

# HA dispatcher signals for push-updated entities.
SIGNAL_OVERFLOW_CHANGE = f"{DOMAIN}_overflow_change"
SIGNAL_WORKER_STATE_CHANGE = f"{DOMAIN}_worker_state_change"
```

### watchdog.py changes

Import `async_dispatcher_send` from `homeassistant.helpers.dispatcher` and
`SIGNAL_WORKER_STATE_CHANGE` from `.const`.

In `_watchdog_respawn`, after `new_thread.start()`, add:
```python
async_dispatcher_send(hass, SIGNAL_WORKER_STATE_CHANGE)
```

Also send on dead-thread detection, before calling `_watchdog_respawn`:
In `watchdog_loop`, after `if not runtime.states_worker.is_alive()...` detection block,
the send is handled inside `_watchdog_respawn`. Add a second send AFTER the respawn
coroutine returns to reflect the newly-alive state.

Simplest correct sequence inside `_watchdog_respawn`:
1. Detect dead → `async_dispatcher_send(hass, SIGNAL_WORKER_STATE_CHANGE)` (worker dead)
2. 5s throttle sleep
3. `new_thread.start()`
4. `async_dispatcher_send(hass, SIGNAL_WORKER_STATE_CHANGE)` (worker respawned)

### __init__.py changes

Imports: add `PLATFORMS`, `SIGNAL_OVERFLOW_CHANGE` from `.const`; add
`async_dispatcher_send` from `homeassistant.helpers.dispatcher`.

Add `PLATFORMS` forward in `async_setup_entry` just before `return True`:
```python
await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
```

Add platform unload in `async_unload_entry`, after the worker joins and before `return True`:
```python
await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
```

Update `_overflow_watcher` to dispatch on BOTH state transitions:
```python
now = live_queue.overflowed
if now != last_seen:   # replaces: if now and not last_seen
    if now:
        create_buffer_dropping_issue(hass)
    async_dispatcher_send(hass, SIGNAL_OVERFLOW_CHANGE)
last_seen = now
```

### Acceptance

- `async_forward_entry_setups` call present, after all data fields assigned
- `async_unload_platforms` call present in unload
- `PLATFORMS`, `SIGNAL_*` constants in const.py
- `OverflowQueue._peak_size`, `_total_dropped`, `get_and_reset_peak()` present and correct
- `PersistentQueue.__len__()` returns accurate count
- `_overflow_watcher` dispatches signal on True→False AND False→True
- `_watchdog_respawn` dispatches signal before and after respawn

---

## Task 2 — Sensor entities + translations + tests

### Files

- `custom_components/timescaledb_recorder/sensor.py` (new)
- `custom_components/timescaledb_recorder/binary_sensor.py` (new)
- `custom_components/timescaledb_recorder/strings.json`
- `custom_components/timescaledb_recorder/translations/en.json`
- `tests/test_sensor.py` (new)
- `tests/test_binary_sensor.py` (new)

### sensor.py

Module-level `SCAN_INTERVAL_FAST = timedelta(seconds=5)`,
`SCAN_INTERVAL_METRICS = timedelta(seconds=10)`,
`SCAN_INTERVAL_META = timedelta(seconds=30)`.

All entities share:
```python
_attr_has_entity_name = True
_attr_entity_category = EntityCategory.DIAGNOSTIC
```

Device info shared constant:
```python
def _device_info(entry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="TimescaleDB Recorder",
        entry_type=DeviceEntryType.SERVICE,
    )
```

#### TimescaledbHealthSensor(SensorEntity)

```python
_attr_name = "Health"
_attr_icon = "mdi:database-check"
_attr_scan_interval = SCAN_INTERVAL_FAST
```

`unique_id = f"{entry.entry_id}_health"`

`async_update(self)`: reads `ir.async_get(self._hass).issues`. Filters to `domain=DOMAIN`.
Active issue keys: `{issue.issue_id for issue in issues.values() if issue.domain == DOMAIN}`.

```python
_ERROR_ISSUES = {"db_unreachable", "states_worker_stalled", "meta_worker_stalled"}
_DEGRADED_ISSUES = {"buffer_dropping", "recorder_disabled"}

def _compute_state(self, active_issue_ids, data) -> str:
    states_alive = data.states_worker is not None and data.states_worker.is_alive()
    meta_alive = data.meta_worker is not None and data.meta_worker.is_alive()
    if (active_issue_ids & _ERROR_ISSUES) or not states_alive or not meta_alive:
        return "error"
    if active_issue_ids & _DEGRADED_ISSUES:
        return "degraded"
    return "ok"
```

State stored in `self._attr_native_value`.

#### TimescaledbDbStatusSensor(SensorEntity)

```python
_attr_name = "DB Status"
_attr_icon = "mdi:database-sync"
_attr_scan_interval = SCAN_INTERVAL_FAST
```

`async_update`: reads active issue ids (same pattern as above).
- `stalled` if `states_worker_stalled` in active
- `disconnected` if `db_unreachable` in active
- `connected` otherwise

#### TimescaledbQueueDepthSensor(SensorEntity)

```python
_attr_name = "Queue Depth Peak"
_attr_icon = "mdi:buffer"
_attr_native_unit_of_measurement = "events"
_attr_state_class = SensorStateClass.MEASUREMENT
_attr_scan_interval = SCAN_INTERVAL_METRICS
```

`async_update`: `self._attr_native_value = self._entry.runtime_data.live_queue.get_and_reset_peak()`

#### TimescaledbEventsDroppedSensor(SensorEntity)

```python
_attr_name = "Events Dropped"
_attr_icon = "mdi:database-remove"
_attr_native_unit_of_measurement = "events"
_attr_state_class = SensorStateClass.MEASUREMENT
_attr_scan_interval = SCAN_INTERVAL_METRICS
```

Constructor sets `self._prev_total_dropped: int = 0`.

`async_update`:
```python
current = self._entry.runtime_data.live_queue._total_dropped
delta = current - self._prev_total_dropped
self._prev_total_dropped = current
self._attr_native_value = delta
```

#### TimescaledbMetaQueueDepthSensor(SensorEntity)

```python
_attr_name = "Meta Queue Depth"
_attr_icon = "mdi:database-clock"
_attr_native_unit_of_measurement = "items"
_attr_state_class = SensorStateClass.MEASUREMENT
_attr_scan_interval = SCAN_INTERVAL_META
```

`async_update`: `self._attr_native_value = len(self._entry.runtime_data.meta_queue)`

#### async_setup_entry

```python
async def async_setup_entry(
    hass: HomeAssistant,
    entry: TimescaledbRecorderConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([
        TimescaledbHealthSensor(hass, entry),
        TimescaledbDbStatusSensor(hass, entry),
        TimescaledbQueueDepthSensor(entry),
        TimescaledbEventsDroppedSensor(entry),
        TimescaledbMetaQueueDepthSensor(entry),
    ])
```

### binary_sensor.py

#### TimescaledbBufferOverflowSensor(BinarySensorEntity)

```python
_attr_name = "Buffer Overflow"
_attr_device_class = BinarySensorDeviceClass.PROBLEM
_attr_icon = "mdi:database-alert"
```

In `async_added_to_hass`: subscribe to `SIGNAL_OVERFLOW_CHANGE` via
`async_dispatcher_connect`. Callback reads `self._entry.runtime_data.live_queue.overflowed`
and calls `self.async_write_ha_state()`.

#### TimescaledbStatesWorkerAliveSensor(BinarySensorEntity)

```python
_attr_name = "States Worker"
_attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
_attr_icon = "mdi:database-arrow-up"
```

In `async_added_to_hass`: subscribe to `SIGNAL_WORKER_STATE_CHANGE`. Callback reads
`runtime_data.states_worker.is_alive()` (guard for None).

#### TimescaledbMetaWorkerAliveSensor(BinarySensorEntity)

```python
_attr_name = "Metadata Worker"
_attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
_attr_icon = "mdi:database-cog"
```

Same pattern as states worker but reads `runtime_data.meta_worker.is_alive()`.

All binary sensors: `unique_id`, `device_info`, `_attr_entity_category = EntityCategory.DIAGNOSTIC`.

Initial state in `async_added_to_hass`: set initial value before subscribing so entity is
not `unknown` until first dispatch arrives.

#### async_setup_entry

```python
async def async_setup_entry(
    hass: HomeAssistant,
    entry: TimescaledbRecorderConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([
        TimescaledbBufferOverflowSensor(entry),
        TimescaledbStatesWorkerAliveSensor(entry),
        TimescaledbMetaWorkerAliveSensor(entry),
    ])
```

### strings.json + translations/en.json

Add an `"entity"` section for both `"sensor"` and `"binary_sensor"` platforms with
human-readable names for all 8 entity suffixes. HA uses `entity.sensor.<key>.name` etc.

Example structure (add to both files, keeping existing keys intact):
```json
"entity": {
  "sensor": {
    "health": { "name": "Health" },
    "db_status": { "name": "DB Status" },
    "queue_depth_peak": { "name": "Queue Depth Peak" },
    "events_dropped": { "name": "Events Dropped" },
    "meta_queue_depth": { "name": "Meta Queue Depth" }
  },
  "binary_sensor": {
    "buffer_overflow": { "name": "Buffer Overflow" },
    "states_worker_alive": { "name": "States Worker" },
    "meta_worker_alive": { "name": "Metadata Worker" }
  }
}
```

### tests/test_sensor.py

Test scope — unit tests with mocked runtime_data:

1. `test_health_sensor_ok`: no active issues, both workers alive → state "ok"
2. `test_health_sensor_degraded`: buffer_dropping issue active → state "degraded"
3. `test_health_sensor_error_issue`: db_unreachable active → state "error"
4. `test_health_sensor_error_worker_dead`: states_worker not alive → state "error"
5. `test_db_status_connected`: no issues → "connected"
6. `test_db_status_disconnected`: db_unreachable active → "disconnected"
7. `test_db_status_stalled`: states_worker_stalled active → "stalled"
8. `test_queue_depth_peak`: `get_and_reset_peak()` returns 42 → native_value = 42
9. `test_events_dropped_delta`: _total_dropped goes 0→5 between polls → delta = 5
10. `test_events_dropped_wrap`: second poll has same total → delta = 0
11. `test_meta_queue_depth`: `len(meta_queue)` = 3 → native_value = 3

### tests/test_binary_sensor.py

1. `test_buffer_overflow_initial_state`: overflowed=False → is_on=False
2. `test_buffer_overflow_push`: dispatch SIGNAL_OVERFLOW_CHANGE with overflowed=True → is_on=True
3. `test_states_worker_alive_initial`: worker is_alive()=True → is_on=True
4. `test_states_worker_alive_dead`: worker is_alive()=False → is_on=False
5. `test_states_worker_push_update`: dispatch SIGNAL_WORKER_STATE_CHANGE, worker transitions to alive → is_on=True
6. `test_meta_worker_alive_initial`: worker is_alive()=True → is_on=True

### Acceptance

- `sensor.py` and `binary_sensor.py` importable; `async_setup_entry` in each
- All 8 entities: correct unique_id, device_info points to entry.entry_id device
- Push sensors subscribe in `async_added_to_hass`, unsubscribe on remove
- Health/db_status polled at 5s, queue metrics at 10s, meta at 30s
- strings.json + translations/en.json have `"entity"` section for all 8
- All tests pass: `uv run pytest tests/test_sensor.py tests/test_binary_sensor.py -v`

---

## must_haves

```yaml
truths:
  - 8 entities registered: 5 sensor + 3 binary_sensor
  - All attach to DeviceEntryType.SERVICE device keyed by entry.entry_id
  - overflow + worker sensors push-updated via dispatcher
  - health + db_status polled at 5s (reads issue_registry)
  - queue_depth_peak reports peak since last poll (get_and_reset_peak)
  - events_dropped reports delta (monotonic _total_dropped counter)
  - meta_queue_depth uses __len__ on PersistentQueue
  - All entities EntityCategory.DIAGNOSTIC
  - strings.json + en.json entity translations present
artifacts:
  - custom_components/timescaledb_recorder/sensor.py
  - custom_components/timescaledb_recorder/binary_sensor.py
  - tests/test_sensor.py
  - tests/test_binary_sensor.py
key_links:
  - custom_components/timescaledb_recorder/__init__.py (platform forward, overflow dispatcher)
  - custom_components/timescaledb_recorder/overflow_queue.py (_peak_size, _total_dropped)
  - custom_components/timescaledb_recorder/persistent_queue.py (__len__)
  - custom_components/timescaledb_recorder/watchdog.py (worker state dispatcher)
  - custom_components/timescaledb_recorder/const.py (PLATFORMS, SIGNAL_*)
```
