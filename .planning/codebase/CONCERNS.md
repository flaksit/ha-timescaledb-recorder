---
generated: 2026-04-18
focus: concerns
---

# Codebase Concerns

**Analysis Date:** 2026-04-18

## Security Considerations

### SSL disabled unconditionally on the connection pool

- Risk: All traffic between HA and PostgreSQL is unencrypted, including the DSN credentials that pass through the connection. If the HA instance and TimescaleDB are on separate hosts (e.g. HA Core vs. a remote VPS), credentials and state data are transmitted in the clear.
- Files: `custom_components/ha_timescaledb_recorder/__init__.py` line 71 (`ssl=False`)
- Current mitigation: README assumes the intended deployment is the companion TimescaleDB HA add-on, which runs on localhost inside the Supervisor network. In that topology the risk is low.
- Recommendations: Change `ssl=False` to `ssl="prefer"` or make it user-configurable in the options flow. At minimum document the constraint explicitly so users who point the DSN at a remote host understand the exposure.

### DSN stored in config entry data (plaintext in HA storage)

- Risk: The DSN contains the PostgreSQL password and is stored by HA in `config/.storage/core.config_entries` with no additional encryption layer beyond whatever HA applies to its storage files.
- Files: `custom_components/ha_timescaledb_recorder/__init__.py` line 61, `custom_components/ha_timescaledb_recorder/config_flow.py` line 31
- Current mitigation: This is the standard HA pattern for storing credentials; HA itself does not encrypt secrets in config entries. The risk is the same as any other HA integration that stores a password.
- Recommendations: Document the storage behaviour so users understand the risk. No code change is strictly required, but a note in README.md would help.

### Config flow catches bare `Exception` during DSN validation

- Risk: A broad `except Exception` in `config_flow.py` line 35 silently swallows all exceptions (including programming errors) and maps them all to the single "cannot_connect" user-facing error. This makes debugging failed setups harder and could mask non-connection errors.
- Files: `custom_components/ha_timescaledb_recorder/config_flow.py` line 35 (`except Exception:  # noqa: BLE001`)
- Current mitigation: The `# noqa: BLE001` annotation is intentional; HA config flows conventionally suppress the error detail from the UI. However no log entry is emitted, so the actual exception is lost entirely.
- Recommendations: Add `_LOGGER.debug("Config flow DSN validation failed", exc_info=True)` inside the except block so the exception is at least recoverable from logs.

## Tech Debt

### README documents stale option name

- Issue: README.md line 54 lists the option as `compress_after_days` with default 7, but the actual constant is `CONF_COMPRESS_AFTER = "compress_after_hours"` with default 2 hours. The description is wrong on both the unit and the default.
- Files: `README.md` line 54, `custom_components/ha_timescaledb_recorder/const.py` lines 11 and 15
- Impact: Users following the README will be confused by the mismatch between the UI (which shows hours) and the docs (which say days).
- Fix approach: Update README.md option table to reflect `compress_after_hours` with default `2`.

### Unanswered `TODO` in production code

- Issue: `ingester.py` line 65 contains `# TODO do we need this call to dict()?` inside the event handler that runs on every state change. The `dict()` copy is almost certainly unnecessary because `json.dumps` accepts `MappingProxyType` directly, and removing it would eliminate one allocation per event.
- Files: `custom_components/ha_timescaledb_recorder/ingester.py` line 65–66
- Impact: Minor per-event overhead; negligible in practice but the TODO creates noise and uncertainty.
- Fix approach: Test `json.dumps(new_state.attributes, default=str)` directly; if it serializes correctly, drop the `dict()` wrapper and remove the TODO.

### `dataclasses` imported inside a function on every call

- Issue: `syncer.py` line 63 performs `import dataclasses` inside the `_build_extra()` function, which is called for every SCD2 write. Python caches module imports in `sys.modules` so the actual cost is a dict lookup per call, not a true import, but it is an unusual pattern that can confuse readers and static analysis tools.
- Files: `custom_components/ha_timescaledb_recorder/syncer.py` line 63
- Impact: Negligible runtime cost; mild code smell.
- Fix approach: Move `import dataclasses` to the module top-level.

### `_config_entry` attribute stored on `OptionsFlow` even though HA 2024+ provides `self.config_entry`

- Issue: `config_flow.py` line 60 stores `self._config_entry = config_entry` in `__init__`. Current HA versions expose `self.config_entry` directly on `OptionsFlow`; the manual assignment is redundant and could go stale if HA changes how the entry is passed.
- Files: `custom_components/ha_timescaledb_recorder/config_flow.py` lines 59–60 and 68
- Impact: No current breakage, but creates a divergence from the HA convention that could cause confusion in future HA upgrades.
- Fix approach: Remove `self._config_entry` and use `self.config_entry` throughout `TimescaleDBOptionsFlow`.

## Missing Error Handling

### Schema setup errors are unhandled in `async_setup_entry`

- Issue: `async_setup_schema()` executes multiple DDL statements (CREATE TABLE, SELECT create_hypertable, compression policy, indexes) with no try/except in `async_setup_entry`. Any `asyncpg.PostgresError` during setup (e.g. insufficient privileges, TimescaleDB extension missing) will propagate uncaught, crashing the integration setup with an unhelpful traceback rather than a clean HA error.
- Files: `custom_components/ha_timescaledb_recorder/__init__.py` lines 76–80, `custom_components/ha_timescaledb_recorder/schema.py`
- Impact: Poor user experience during initial setup when the database is misconfigured. The config flow validates the DSN but does not check for TimescaleDB extension or DDL privileges.
- Fix approach: Wrap `async_setup_schema()` call in a try/except that raises `ConfigEntryNotReady` on connection errors and logs a clear human-readable message for permission or extension errors.

### Pool creation failure crashes setup silently

- Issue: `asyncpg.create_pool()` in `async_setup_entry` line 64 is awaited with no error handling. If pool creation fails (e.g. DB is down when HA restarts), the exception propagates and HA marks the entry as failed with a generic error, losing the specific asyncpg exception in the log noise.
- Files: `custom_components/ha_timescaledb_recorder/__init__.py` lines 64–72
- Impact: On restarts where the DB is temporarily unavailable, the integration fails hard instead of retrying. HA's `ConfigEntryNotReady` mechanism would allow automatic retry.
- Fix approach: Catch `asyncpg.PostgresConnectionError` and `OSError` from both `create_pool` and `async_setup_schema` and re-raise as `ConfigEntryNotReady` to trigger HA's built-in retry logic.

### `MetadataSyncer` non-connection DB errors are silently swallowed

- Issue: The four `_async_process_*_event` methods in `syncer.py` only catch `(asyncpg.PostgresConnectionError, OSError)`. Any other `asyncpg.PostgresError` (e.g. constraint violation, type mismatch) is not caught, causing an unhandled exception in an `async_create_task`-spawned coroutine, which HA will log at ERROR level but the SCD2 write will simply not happen with no retry or alerting.
- Files: `custom_components/ha_timescaledb_recorder/syncer.py` lines 381, 425, 476, 520
- Impact: Silent metadata corruption — a registry event that fails partway through the SCD2 cycle (close succeeds, insert fails) leaves the dimension table without a current row for that entity/device/area/label.
- Fix approach: Add a second `except asyncpg.PostgresError` branch (mirroring the pattern in `StateIngester._async_flush`) that logs the error and the event parameters at ERROR level.

### `_async_snapshot` has no error handling

- Issue: `syncer.py` `_async_snapshot()` issues dozens of SQL statements (one per registered entity/device/area/label) with no error handling. Any DB error during the initial snapshot will propagate to `async_start()` which is called from `async_setup_entry` — same unhandled failure path as above.
- Files: `custom_components/ha_timescaledb_recorder/syncer.py` lines 150–174
- Impact: If the schema was partially created (e.g. `dim_entities` exists but `dim_devices` does not), the snapshot will fail partway through, leaving an inconsistent dimension table state.
- Fix approach: Wrap the snapshot in a try/except and decide whether a snapshot failure should abort integration setup or continue with a warning.

## Performance Bottlenecks

### Initial snapshot uses per-row `conn.execute`, not `executemany`

- Issue: `MetadataSyncer._async_snapshot()` calls `await conn.execute(SQL, *params)` in a loop — one round trip per entity, device, area, and label. A large HA instance with 500+ entities will generate 500+ sequential DB round trips on every restart.
- Files: `custom_components/ha_timescaledb_recorder/syncer.py` lines 160–174
- Impact: HA restarts are noticeably slow on large instances; the pool is held for the full duration of the snapshot loop, blocking other writes during that window.
- Fix approach: Collect all params into lists and use `conn.executemany(SNAPSHOT_SQL, params_list)` per table. Requires adapting the idempotent `WHERE NOT EXISTS` SQL for batch use (e.g. via `ON CONFLICT DO NOTHING` after adding unique indexes).

### Change detection issues a SELECT per SCD2 update event

- Issue: All four `_*_row_changed()` methods in `syncer.py` perform a `fetchrow` SELECT before deciding whether to write. Under high registry activity (e.g. many devices reconnecting simultaneously after a HA restart), this generates a SELECT + UPDATE + INSERT per event — three round trips per event.
- Files: `custom_components/ha_timescaledb_recorder/syncer.py` lines 249–331
- Impact: Modest overhead in normal operation; could cause DB connection pool exhaustion during bulk registry updates.
- Fix approach: Consider using a combined `UPDATE ... RETURNING` or an upsert pattern that avoids the pre-read entirely. Alternatively, cache the last-seen hash of each row's key fields in memory to skip the DB read when HA fires spurious no-op events.

### Buffer unbounded growth on sustained connection failure

- Issue: `StateIngester._async_flush()` re-queues rows into `self._buffer` on connection errors. If the DB is down for an extended period, the buffer grows without limit, potentially exhausting HA's memory.
- Files: `custom_components/ha_timescaledb_recorder/ingester.py` lines 85–89
- Impact: On a large HA instance with many entities and a prolonged DB outage (hours), unbounded buffer growth could contribute to HA memory pressure.
- Fix approach: Add a configurable `max_buffer_size` (e.g. 10× `batch_size`) and drop the oldest rows with a warning when the buffer exceeds this limit. Log the drop count clearly.

## Fragile Areas

### SCD2 close-and-insert is not atomic

- Issue: The close-and-insert SCD2 cycle in `_async_process_entity_event` and peers executes as two separate `await conn.execute()` calls (lines 369–374 in `syncer.py`). If HA crashes or the connection drops between the close and the insert, the dimension table is left with a closed row but no successor — the entity/device/area/label loses its "current" row permanently.
- Files: `custom_components/ha_timescaledb_recorder/syncer.py` lines 369–374, 416–418, 466–469, 509–512
- Why fragile: The snapshot's WHERE NOT EXISTS guard only inserts if no open row exists; it does not recover from orphaned closed rows. After a crash mid-SCD2, the entity is invisible to `WHERE valid_to IS NULL` queries and the next registry event for that entity will insert a new row correctly — but the gap in history is permanent.
- Safe modification: Wrap the close+insert pair in a transaction: `async with conn.transaction(): ...` This is supported by asyncpg and costs no extra round trips.
- Test coverage: No test covers the crash-between-close-and-insert scenario.

### Entity filter is read from `entry.data` not `entry.options`

- Issue: `_get_entity_filter()` in `__init__.py` reads `entry.data.get("filter", {})`. The options flow in `config_flow.py` does not expose a filter UI at all, and the README says filters are configured via `configuration.yaml`. This means the filter is stored in `entry.data` (set at config flow time) while all other runtime parameters are in `entry.options`. The `_async_options_updated` listener reloads the integration on options change but the filter can never be changed without re-adding the integration.
- Files: `custom_components/ha_timescaledb_recorder/__init__.py` lines 44–56, `custom_components/ha_timescaledb_recorder/config_flow.py`
- Why fragile: The `configuration.yaml` filter mechanism is inconsistent with the UI-driven options approach for the other settings. It is also not tested in the test suite.
- Safe modification: Either expose the filter in the options flow (like batch_size) or document clearly that changing the filter requires removing and re-adding the integration.

## Test Coverage Gaps

### Integration-level `async_setup_entry` is untested

- What's not tested: The full setup path — pool creation, schema setup, filter construction, ingester/syncer wiring — has no integration test. Tests for each component exist in isolation but the composition in `async_setup_entry` is not exercised.
- Files: `custom_components/ha_timescaledb_recorder/__init__.py` lines 59–100
- Risk: Regressions in how the components are wired together (e.g. wrong option key, wrong filter source) would not be caught.
- Priority: Medium

### `async_unload_entry` is untested

- What's not tested: The unload path (`async_unload_entry`) is not covered. In particular, whether the pool is closed after the ingester and syncer stop is not verified.
- Files: `custom_components/ha_timescaledb_recorder/__init__.py` lines 108–114
- Risk: A regression where the pool is not closed on unload would silently leak connections.
- Priority: Low

### Options flow happy path is untested

- What's not tested: `TimescaleDBOptionsFlow.async_step_init` with valid input — only the config flow is tested. The options flow schema validation (min/max ranges) has no test.
- Files: `custom_components/ha_timescaledb_recorder/config_flow.py` lines 56–88, `tests/test_config_flow.py`
- Risk: Invalid option ranges or key mismatches would only be caught at runtime.
- Priority: Low

### SCD2 mid-cycle crash recovery has no test

- What's not tested: Behaviour when the DB connection drops between the SCD2 close and insert (leaving no open row). The snapshot's WHERE NOT EXISTS guard is tested for idempotency but not for partial-write recovery.
- Files: `tests/test_syncer.py`
- Risk: Silent history gaps after crashes; hard to detect without careful monitoring.
- Priority: Medium

### Device/area/label SCD2 create/update/remove cycles are not tested

- What's not tested: `test_syncer.py` covers entity registry events thoroughly but has zero tests for `_async_process_device_event`, `_async_process_area_event` (beyond reorder), and `_async_process_label_event`.
- Files: `tests/test_syncer.py`
- Risk: Bugs in device/area/label SCD2 handling would go undetected.
- Priority: High

## Operational Concerns

### No health check or integration status surface

- Problem: Once running, there is no way for a user or monitoring system to verify that the integration is actively writing. A sustained DB connection failure causes rows to re-queue silently (up to `DEFAULT_BATCH_SIZE` events) before warnings appear in the log.
- Files: `custom_components/ha_timescaledb_recorder/ingester.py`
- Impact: Silent data loss during extended outages; the user may not notice for hours.
- Recommendations: Expose a sensor entity (e.g. `sensor.timescaledb_recorder_status`) showing last_flush_time, buffer depth, and error count. This is the conventional HA pattern for integration health monitoring.

### No migration path for schema changes

- Problem: `async_setup_schema` is entirely additive and idempotent for new installs. There is no mechanism for ALTER TABLE migrations. If a future version needs to add a column to `ha_states` or change a dim_* column type, it would require manual SQL or a schema version table.
- Files: `custom_components/ha_timescaledb_recorder/schema.py`
- Impact: Future breaking schema changes have no upgrade path without manual user intervention.
- Recommendations: Add a `schema_version` table and a migration registry (even a simple dict of version → SQL list) to handle ALTER TABLE statements idempotently on startup.

### `pyproject.toml` version is stale

- Problem: `pyproject.toml` line 3 declares `version = "0.1.0"` while `manifest.json` declares `0.3.6`. These two are out of sync. The `pyproject.toml` version is used by development tooling but not by HA; however the discrepancy is confusing.
- Files: `pyproject.toml` line 3, `custom_components/ha_timescaledb_recorder/manifest.json` line 4
- Impact: Minor; no functional impact.
- Fix approach: Keep both files in sync as part of the version bump process.

### No `asyncio_mode` in `pyproject.toml` pytest config

- Problem: `pyproject.toml` uses `[tool.pytest]` (missing the `.ini_options` suffix) which is not the correct section name for pytest configuration. The correct key is `[tool.pytest.ini_options]`. As a result, `asyncio_mode = "auto"` and `testpaths = ["tests"]` are silently ignored by pytest and must be supplied another way (e.g. via conftest or CLI flags).
- Files: `pyproject.toml` lines 6–8
- Impact: Tests may require explicit `--asyncio-mode=auto` on the CLI to run correctly; CI could silently skip async tests if the flag is not set.
- Fix approach: Rename `[tool.pytest]` to `[tool.pytest.ini_options]`.
