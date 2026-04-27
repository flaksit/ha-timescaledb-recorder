---
generated: 2026-04-18
focus: arch
---

# Codebase Structure

## Directory Layout

```
ha-timescaledb-recorder/
├── custom_components/
│   ├── __init__.py                        # Empty package marker
│   └── timescaledb_recorder/
│       ├── __init__.py                    # Integration setup/teardown lifecycle
│       ├── const.py                       # All SQL strings and configuration constants
│       ├── schema.py                      # Idempotent DDL setup function
│       ├── ingester.py                    # Buffered state-change writer (StateIngester)
│       ├── syncer.py                      # SCD2 registry metadata syncer (MetadataSyncer)
│       ├── config_flow.py                 # HA config flow and options flow UI
│       └── manifest.json                  # HA integration manifest (version, dependencies)
├── tests/
│   ├── __init__.py                        # Empty package marker
│   ├── conftest.py                        # Shared pytest fixtures (mock pool, hass, registries)
│   ├── test_ingester.py                   # Unit tests for StateIngester
│   ├── test_syncer.py                     # Unit tests for MetadataSyncer
│   ├── test_schema.py                     # Unit tests for async_setup_schema
│   └── test_config_flow.py               # Unit tests for config/options flow
├── pyproject.toml                         # Project metadata and dev dependency group
├── hacs.json                              # HACS distribution metadata
└── .venv/                                 # uv-managed virtual environment (not committed)
```

## Directory Purposes

**`custom_components/timescaledb_recorder/`:**
- Purpose: The entire integration lives here; HA discovers integrations by `custom_components/<domain>/`
- Contains: All production Python source files, `manifest.json`
- Key files: `__init__.py` (lifecycle entry point), `const.py` (all SQL), `ingester.py`, `syncer.py`

**`tests/`:**
- Purpose: Pytest unit test suite; all tests use mocks — no real DB or HA instance required
- Contains: One test file per production module plus `conftest.py` for shared fixtures
- Key files: `conftest.py` (mock pool, mock hass, four mock registry fixtures)

## Key File Locations

**Integration Lifecycle:**
- `custom_components/timescaledb_recorder/__init__.py`: `async_setup_entry`, `async_unload_entry`, `TimescaledbRecorderData`, entity filter construction

**SQL and Constants:**
- `custom_components/timescaledb_recorder/const.py`: Every SQL string (DDL, DML, SCD2 variants) and every config key/default value — the single source of truth for all database interaction

**Schema Initialization:**
- `custom_components/timescaledb_recorder/schema.py`: `async_setup_schema(pool, chunk_interval_days, compress_after_hours)` — idempotent, called once per startup

**State Write Pipeline:**
- `custom_components/timescaledb_recorder/ingester.py`: `StateIngester` — in-memory buffer, event listener, periodic flush timer

**Metadata Write Pipeline:**
- `custom_components/timescaledb_recorder/syncer.py`: `MetadataSyncer` — startup snapshot, four registry event handlers, SCD2 change detection helpers

**Configuration UI:**
- `custom_components/timescaledb_recorder/config_flow.py`: `TimescaledbConfigFlow` (initial DSN), `TimescaledbOptionsFlow` (batch/flush/compression tuning)

**HA Integration Declaration:**
- `custom_components/timescaledb_recorder/manifest.json`: domain name, version (`0.3.6`), `requirements: ["asyncpg==0.31.0"]`, `config_flow: true`, `single_config_entry: true`

**Test Fixtures:**
- `tests/conftest.py`: `mock_conn`, `mock_pool`, `hass`, `mock_entity_registry`, `mock_device_registry`, `mock_area_registry`, `mock_label_registry`

## Naming Conventions

**Files:**
- Snake case: `config_flow.py`, `ingester.py`, `syncer.py`
- Test files mirror the module they test: `ingester.py` → `test_ingester.py`

**Classes:**
- PascalCase: `StateIngester`, `MetadataSyncer`, `TimescaledbConfigFlow`, `TimescaledbOptionsFlow`, `TimescaledbRecorderData`

**Functions:**
- Async public methods: `async_start`, `async_stop`, `async_setup_schema`, `async_setup_entry`
- Async private methods: `_async_flush`, `_async_flush_timer`, `_async_snapshot`, `_async_process_entity_event`
- Sync private callbacks: `_handle_state_changed`, `_handle_entity_registry_updated` (decorated `@callback`)
- Helper functions: `_build_extra`, `_extra_changed`, `_get_entity_filter`

**SQL Constants:**
- DDL: `CREATE_TABLE_SQL`, `CREATE_HYPERTABLE_SQL`, `CREATE_DIM_ENTITIES_SQL`, `CREATE_DIM_ENTITIES_IDX_SQL`
- DML: `INSERT_SQL`
- SCD2 operation prefix: `SCD2_CLOSE_*_SQL`, `SCD2_SNAPSHOT_*_SQL`, `SCD2_INSERT_*_SQL`
- Policy management: `ADD_COMPRESSION_POLICY_SQL`, `REMOVE_COMPRESSION_POLICY_SQL`, `SET_COMPRESSION_SQL`

**Config keys:**
- `CONF_*` for config entry keys (e.g., `CONF_DSN`, `CONF_BATCH_SIZE`)
- `DEFAULT_*` for default values (e.g., `DEFAULT_BATCH_SIZE = 200`)

## Where to Add New Code

**New time-series table (additional data type to record):**
- Add DDL SQL constants to `custom_components/timescaledb_recorder/const.py`
- Add DDL execution to `custom_components/timescaledb_recorder/schema.py:async_setup_schema`
- Add new writer class in a new file at `custom_components/timescaledb_recorder/<name>.py` following the `StateIngester` pattern (constructor, `async_start`, `async_stop`, private flush)
- Instantiate in `custom_components/timescaledb_recorder/__init__.py:async_setup_entry` and add to `TimescaledbRecorderData`
- Tests go in `tests/test_<name>.py`

**New dimension table (new registry type to track):**
- Add DDL and SCD2 SQL constants to `const.py` following the `dim_*` naming pattern
- Add DDL execution to `schema.py:async_setup_schema`
- Add typed-key frozenset, snapshot/event-handler methods to `custom_components/timescaledb_recorder/syncer.py:MetadataSyncer`
- Subscribe the new event in `MetadataSyncer.async_start`

**New config option:**
- Add `CONF_*` key and `DEFAULT_*` value to `const.py`
- Add field to `TimescaledbOptionsFlow.async_step_init` schema in `config_flow.py`
- Consume in `async_setup_entry` in `__init__.py`

**New utility/helper function:**
- If used by a single module, add it as a module-level private function in that module
- If used across modules, add to `const.py` (for SQL/data) or create a new `utils.py` file

## Special Directories

**`.venv/`:**
- Purpose: uv-managed virtual environment with development dependencies (asyncpg, pytest, pytest-asyncio, pytest-homeassistant-custom-component, full HA copy)
- Generated: Yes (by `uv sync`)
- Committed: No (in `.gitignore`)

**`.planning/`:**
- Purpose: GSD planning documents for AI-assisted development workflow
- Generated: Yes (by GSD tooling)
- Committed: Yes

---

*Structure analysis: 2026-04-18*
