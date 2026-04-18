---
generated: 2026-04-18
focus: arch
---

# Architecture

## Pattern Overview

**Overall:** Event-driven plugin (Home Assistant custom component)

**Key Characteristics:**
- Runs entirely inside the Home Assistant event loop as an async custom component
- No HTTP endpoints or inter-process communication — all data flows through HA's internal event bus
- Two parallel write paths: one for time-series state data (buffered/batched), one for dimensional metadata (immediate/event-driven)
- Schema setup is idempotent and runs on every startup; no migration tooling

## Layers

**Integration Entrypoint:**
- Purpose: HA lifecycle hooks — setup, teardown, options update
- Location: `custom_components/ha_timescaledb_recorder/__init__.py`
- Contains: `async_setup_entry`, `async_unload_entry`, `HaTimescaleDBData` dataclass, entity filter construction
- Depends on: `StateIngester`, `MetadataSyncer`, `async_setup_schema`, `asyncpg.Pool`
- Used by: Home Assistant config entry machinery

**Schema Layer:**
- Purpose: Idempotent DDL execution — creates hypertable, compression policy, indexes, and all dimension tables on startup
- Location: `custom_components/ha_timescaledb_recorder/schema.py`
- Contains: `async_setup_schema()` function
- Depends on: SQL constants from `const.py`, `asyncpg.Pool`
- Used by: `async_setup_entry` in `__init__.py`

**State Ingester:**
- Purpose: Buffered, batched writer for time-series state change data into `ha_states` hypertable
- Location: `custom_components/ha_timescaledb_recorder/ingester.py`
- Contains: `StateIngester` class with in-memory list buffer, HA event listener, periodic flush timer
- Depends on: `asyncpg.Pool`, HA event bus (`EVENT_STATE_CHANGED`), `async_track_time_interval`
- Used by: `__init__.py` (holds reference on `HaTimescaleDBData`)

**Metadata Syncer:**
- Purpose: SCD2 (Slowly Changing Dimension Type 2) writer for HA registry metadata into four dimension tables
- Location: `custom_components/ha_timescaledb_recorder/syncer.py`
- Contains: `MetadataSyncer` class — full snapshot on startup, incremental close-and-insert on registry events
- Depends on: `asyncpg.Pool`, HA entity/device/area/label registries and their update events
- Used by: `__init__.py` (holds reference on `HaTimescaleDBData`)

**Configuration Layer:**
- Purpose: HA config flow for initial DSN entry and options (batch size, flush interval, compression parameters)
- Location: `custom_components/ha_timescaledb_recorder/config_flow.py`
- Contains: `TimescaleDBConfigFlow` (initial setup), `TimescaleDBOptionsFlow` (runtime tuning)
- Depends on: `asyncpg` (connection test), `voluptuous` (schema validation)
- Used by: HA config entry UI

**Constants & SQL:**
- Purpose: All SQL strings and configuration constants in one place; no inline SQL in business logic
- Location: `custom_components/ha_timescaledb_recorder/const.py`
- Contains: DDL for `ha_states` hypertable, four dimension tables (`dim_entities`, `dim_devices`, `dim_areas`, `dim_labels`), SCD2 SQL (snapshot, close, insert), index SQL, `INSERT_SQL` for state ingestion
- Used by: all other modules

## Data Flow

**State Change Write Path:**

1. HA fires `EVENT_STATE_CHANGED` on its event bus
2. `StateIngester._handle_state_changed` (synchronous `@callback`) filters by entity filter and appends `(entity_id, state, attributes_json, last_updated, last_changed)` to `_buffer`
3. If buffer reaches `batch_size`, `hass.async_create_task(_async_flush())` is scheduled
4. Periodic timer (`flush_interval` seconds) triggers `_async_flush_timer` which flushes if buffer is non-empty
5. `_async_flush` acquires a pool connection and calls `conn.executemany(INSERT_SQL, rows)` against `ha_states`
6. On `PostgresConnectionError`, rows are re-queued and pool connections expired; on other `PostgresError`, batch is dropped with error log

**Metadata Sync Write Path (startup snapshot):**

1. `MetadataSyncer.async_start()` resolves all four HA registry references
2. `_async_snapshot()` iterates all registry entries and executes idempotent `WHERE NOT EXISTS` snapshot inserts for entities, devices, areas, and labels with a shared `valid_from` timestamp

**Metadata Sync Write Path (incremental, event-driven):**

1. HA fires a registry update event (`EVENT_ENTITY_REGISTRY_UPDATED` etc.)
2. Synchronous `@callback` handler extracts `action` and ID, schedules an async coroutine via `hass.async_create_task`
3. Async handler acquires a pool connection and dispatches on `action`:
   - `create`: plain `SCD2_INSERT_*_SQL`
   - `remove`: `SCD2_CLOSE_*_SQL` (sets `valid_to = now`)
   - `update`: fetch current open row, compare fields (ignoring `modified_at` in `extra`), and if changed: close current row then insert new row (no-op otherwise)
4. On entity renames (`old_entity_id` present), always closes old row and inserts new without field comparison

**Schema Setup Path:**

1. Called once in `async_setup_entry` before creating `StateIngester` or `MetadataSyncer`
2. `async_setup_schema` runs all DDL in sequence on one connection: `CREATE TABLE IF NOT EXISTS`, `create_hypertable`, `SET compression`, `remove_compression_policy` + `add_compression_policy`, `CREATE INDEX IF NOT EXISTS`, then all four dimension table DDLs and indexes

**Options Change Path:**

1. User modifies options in HA UI
2. `_async_options_updated` triggers `hass.config_entries.async_reload(entry_id)`
3. Full `async_unload_entry` → `async_setup_entry` cycle; new `batch_size`, `flush_interval`, `chunk_interval`, and `compress_after` values take effect

## Key Abstractions

**`HaTimescaleDBData` (dataclass):**
- Purpose: Runtime state attached to the config entry; holds references to both workers and the pool
- Location: `custom_components/ha_timescaledb_recorder/__init__.py`
- Pattern: HA `ConfigEntry[HaTimescaleDBData]` typed alias (`HaTimescaleDBConfigEntry`)

**Entity Filter:**
- Purpose: Declarative include/exclude filter deciding which `entity_id`s are recorded
- Construction: `_get_entity_filter(entry)` in `__init__.py`, built from `entry.data["filter"]` using HA's `convert_include_exclude_filter` / `convert_filter`
- Used by: `StateIngester` — checked synchronously in `_handle_state_changed` before buffering

**SCD2 Pattern:**
- Purpose: Full temporal history of HA registry metadata — every rename, reconfiguration, or removal is preserved with `valid_from`/`valid_to` timestamps
- Key design: `valid_to IS NULL` marks the currently-active row; `valid_to` is set to `now` on close
- Idempotency: snapshot uses `WHERE NOT EXISTS` so HA restarts do not create duplicate open rows
- Change detection: `_entity_row_changed`, `_device_row_changed`, etc. fetch the current open row and compare field-by-field, excluding `modified_at` from `extra` to suppress HA-internal no-op writes

**`_build_extra` helper:**
- Purpose: Serialize all non-typed registry entry fields into the `extra JSONB` catch-all column, preserving future-proofing without schema changes
- Strategy: tries `attrs.asdict()` first (HA `EntityEntry`/`DeviceEntry`), then `dataclasses.fields()` (HA `AreaEntry`/`LabelEntry`), then `vars()` as fallback; strips underscore-prefixed keys (HA internal caches)

## Entry Points

**`async_setup_entry(hass, entry)`:**
- Location: `custom_components/ha_timescaledb_recorder/__init__.py:59`
- Triggers: HA loading the integration (on startup or after config flow)
- Responsibilities: create asyncpg pool, run schema setup, build entity filter, instantiate and start `StateIngester` and `MetadataSyncer`, attach `runtime_data`, register options listener

**`async_unload_entry(hass, entry)`:**
- Location: `custom_components/ha_timescaledb_recorder/__init__.py:108`
- Triggers: HA unloading the integration (shutdown, reload, or user removal)
- Responsibilities: stop ingester (final flush), stop syncer (cancel listeners), close pool

**`TimescaleDBConfigFlow.async_step_user`:**
- Location: `custom_components/ha_timescaledb_recorder/config_flow.py:26`
- Triggers: User adds the integration via HA UI
- Responsibilities: validate DSN by opening a test connection, persist `{dsn}` to `entry.data`

## Error Handling

**Strategy:** Differentiated by error class — connection errors are retried/re-queued; data errors are logged and dropped.

**Patterns:**
- `PostgresConnectionError` / `OSError`: pool connections expired (`pool.expire_connections()`), re-queue buffered rows for state ingestion, log warning for metadata syncer
- `PostgresError` (other): batch dropped with `_LOGGER.error`, first failed row logged at debug level
- Schema setup has no error handling — failures propagate to `async_setup_entry` and prevent the integration from loading

## Cross-Cutting Concerns

**Logging:** `logging.getLogger(__name__)` in every module; debug level for skipped SCD2 writes, warning for connection errors, error for dropped batches.

**Validation:** `voluptuous` schemas in `config_flow.py` with range constraints on all numeric options; DSN validated by attempting a live `asyncpg.connect`.

**Authentication:** No application-level auth — all access control delegated to PostgreSQL connection string (DSN).

**Async model:** All writes are async; event handlers are synchronous `@callback` functions that schedule coroutines via `hass.async_create_task` — HA's event loop is never blocked.

**Connection pool:** Single shared `asyncpg.Pool` (min=1, max=3, max_queries=50,000, command_timeout=30s) used by both `StateIngester` and `MetadataSyncer`.

---

*Architecture analysis: 2026-04-18*
