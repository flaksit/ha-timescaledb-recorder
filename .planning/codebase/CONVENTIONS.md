---
generated: 2026-04-18
focus: quality
---

# Coding Conventions

**Analysis Date:** 2026-04-18

## Naming Patterns

**Files:**
- `snake_case.py` throughout — `ingester.py`, `config_flow.py`, `const.py`, `syncer.py`, `schema.py`
- Module names match the class they contain: `ingester.py` → `StateIngester`, `syncer.py` → `MetadataSyncer`
- Test files: `test_<module>.py` — `test_ingester.py`, `test_syncer.py`, `test_schema.py`, `test_config_flow.py`

**Classes:**
- `PascalCase` — `StateIngester`, `MetadataSyncer`, `TimescaleDBConfigFlow`, `TimescaleDBOptionsFlow`, `HaTimescaleDBData`
- HA integration class names are prefixed with the integration domain name where useful

**Functions:**
- `snake_case` everywhere
- Async functions are prefixed with `async_` — `async_start()`, `async_stop()`, `async_setup_entry()`, `async_setup_schema()`
- HA-event-level async methods are prefixed with `_async_` — `_async_flush()`, `_async_snapshot()`, `_async_process_entity_event()`
- Synchronous HA callbacks decorated with `@callback` follow `_handle_<event_name>` — `_handle_state_changed()`, `_handle_entity_registry_updated()`
- Private methods and attributes: single leading underscore — `_buffer`, `_pool`, `_cancel_listener`
- Module-level helper functions: `snake_case` with leading underscore — `_build_extra()`, `_extra_changed()`, `_get_entity_filter()`

**Variables:**
- `snake_case` — `entity_id`, `valid_from`, `compress_hours`, `mock_conn`
- Module-level logger: always `_LOGGER = logging.getLogger(__name__)`
- Config/option keys: `CONF_` prefix, `SCREAMING_SNAKE_CASE` — `CONF_DSN`, `CONF_BATCH_SIZE`, `CONF_FLUSH_INTERVAL`
- Default values: `DEFAULT_` prefix — `DEFAULT_BATCH_SIZE`, `DEFAULT_FLUSH_INTERVAL`
- SQL constants: `<VERB>_<TABLE>_SQL` pattern — `CREATE_TABLE_SQL`, `SCD2_CLOSE_ENTITY_SQL`, `INSERT_SQL`

**Types/Dataclasses:**
- HA type aliases use `PascalCase` — `HaTimescaleDBConfigEntry = ConfigEntry[HaTimescaleDBData]`
- `frozenset` for immutable key collections — `_ENTITY_TYPED_KEYS`, `_EXTRA_COMPARE_IGNORE`

## Code Style

**Formatter:**
- Ruff (configured as default Python formatter in `.vscode/settings.json`)
- No standalone `ruff.toml` or `[tool.ruff]` section — Ruff is applied via VSCode with `"ruff.configurationPreference": "filesystemFirst"`
- No `black` or `isort` — Ruff handles both

**Line length:**
- Not explicitly configured; Ruff defaults apply (88 characters)

**Trailing commas:**
- Used consistently in multi-line collections and function signatures:
  ```python
  return StateIngester(
      hass=hass,
      pool=pool,
      entity_filter=entity_filter,
      batch_size=batch_size,
      flush_interval=flush_interval,
  )
  ```

**`noqa` usage:**
- Minimal — only one instance in `config_flow.py:35`: `except Exception:  # noqa: BLE001`
- Broad-exception catches are intentional and annotated with `noqa` rather than silently ignored

## Import Organization

**Order (observed):**
1. Standard library — `json`, `logging`, `datetime`, `typing`, `dataclasses`
2. Third-party — `asyncpg`, `attrs`, `voluptuous`
3. Home Assistant framework — `homeassistant.core`, `homeassistant.config_entries`, `homeassistant.helpers.*`
4. Local (relative) — `.const`, `.ingester`, `.schema`, `.syncer`

**Relative vs absolute:**
- HA framework imports: absolute (`from homeassistant.core import ...`)
- Internal component imports: relative (`from .const import ...`)

**Deferred imports inside functions:**
- Used sparingly when a circular import would otherwise occur:
  ```python
  def async_start(self) -> None:
      from homeassistant.const import EVENT_STATE_CHANGED
  ```

**Flat import style:**
- Named imports from each module (`from homeassistant.core import HomeAssistant, Event, callback`)
- No wildcard imports

## Error Handling

**Two-tier asyncpg error strategy:**
- `asyncpg.PostgresConnectionError | OSError` → connection is recoverable; re-queue data, expire pool connections, log `WARNING`
- `asyncpg.PostgresError` → data error is unrecoverable; drop batch, log `ERROR` with count, log first failed row at `DEBUG`

```python
try:
    async with self._pool.acquire() as conn:
        await conn.executemany(INSERT_SQL, rows)
except (asyncpg.PostgresConnectionError, OSError):
    self._pool.expire_connections()
    self._buffer.extend(rows)
    _LOGGER.warning("Connection error during flush; %d rows re-queued", len(rows))
except asyncpg.PostgresError as err:
    _LOGGER.error("PostgresError during flush; %d rows dropped: %s", len(rows), err)
    if rows:
        _LOGGER.debug("First failed row: %r", rows[0])
```

- The same two-tier pattern is used consistently across `ingester.py` and `syncer.py`
- Config flow uses a bare `except Exception:` with `# noqa: BLE001` for DSN validation — intentional broad catch to surface any asyncpg or network error as "cannot_connect"

## Logging

**Logger declaration:** Always at module level immediately after imports:
```python
_LOGGER = logging.getLogger(__name__)
```

**Log levels used:**
- `DEBUG` — normal operation completions and no-op skips: schema setup complete, SCD2 write skipped for unchanged fields, first failed row detail
- `WARNING` — transient errors that are recoverable (connection failures, re-queued rows)
- `ERROR` — data loss events (dropped batch due to PostgresError)

**Message format:**
- `%`-style printf formatting, never f-strings in log calls (avoids eager string construction):
  ```python
  _LOGGER.warning("Connection error during flush; %d rows re-queued", len(rows))
  _LOGGER.debug("Schema setup complete (chunk=%d days, compress_after=%d hours, schedule=%d hours)", ...)
  ```

**What is NOT logged:**
- Happy-path success for every write — only setup completion and errors
- Individual state event processing — only batch-level outcomes

## Comments

**Philosophy:**
- Comments explain WHY, never restate what the code says
- SQL constants include inline comments describing parameter binding order: `# $1=entity_id, $2=...`
- Non-obvious decisions cite a design decision code (D-xx) or Pitfall number:
  ```python
  # Do NOT fetch from registry — entry is already gone (Pitfall 4)
  # Rename: always write — old and new entity_id are different rows (D-09)
  ```
- Section dividers use `# ----` visual separators for grouping related methods within a class

**Docstrings:**
- Every module has a module-level docstring describing its purpose
- Every class has a docstring describing its responsibility and lifecycle
- Every non-trivial method has a docstring explaining its contract, especially parameter indices for positional SQL args:
  ```python
  def _extract_entity_params(self, entry, valid_from: datetime) -> tuple:
      """Extract positional parameters for entity SQL (snapshot + insert).

      Parameter order matches SCD2_SNAPSHOT_ENTITY_SQL / SCD2_INSERT_ENTITY_SQL:
      $1=entity_id, $2=ha_entity_uuid, ...
      """
  ```
- Short single-line docstrings for obvious helpers: `"""Return a mock asyncpg connection."""`

## Function Design

**Parameter style:**
- Keyword arguments used in constructor calls for clarity — `StateIngester(hass=hass, pool=pool, ...)`
- Type annotations on public method signatures; private helpers often unannotated (`entry`, `valid_from` typed, `conn` left as `asyncpg.Connection` implied)

**Return values:**
- `bool` for lifecycle methods: `async_setup_entry() -> bool`, `async_unload_entry() -> bool`
- `None` explicitly annotated on void async methods: `async_start() -> None`, `async_stop() -> None`
- `tuple` for parameter extraction helpers — no named tuple; indices documented in docstring

**Size:**
- Methods are focused; event handler dispatches to named `_async_process_*_event()` coroutines
- Large registry handlers separated by `# ---` section dividers in class body

## Module Design

**No barrel files (`__init__.py` re-exports):**
- `custom_components/__init__.py` is empty
- `custom_components/ha_timescaledb_recorder/__init__.py` is the HA entry point (`async_setup_entry`, `async_unload_entry`), not a re-export barrel

**Constants isolation:**
- All SQL strings and configuration keys live in `const.py`; no SQL literals appear in `ingester.py`, `schema.py`, or `syncer.py`

**Dataclass for runtime state:**
- Shared runtime data packaged in `@dataclass HaTimescaleDBData` and stored on `entry.runtime_data`

---

*Convention analysis: 2026-04-18*
