---
generated: 2026-04-18
focus: quality
---

# Testing Patterns

**Analysis Date:** 2026-04-18

## Test Framework

**Runner:**
- pytest >= 9.0
- Config: `pyproject.toml` under `[tool.pytest]`
- `asyncio_mode = "auto"` — all `async def test_*` functions run as coroutines without needing `@pytest.mark.asyncio`

**Key plugins:**
- `pytest-asyncio >= 0.24` — async test support
- `pytest-homeassistant-custom-component >= 0.13.320` — HA test helpers and fixtures

**Assertion library:**
- Python built-in `assert` — no third-party assertion library

**Run Commands:**
```bash
uv run pytest              # Run all tests
uv run pytest -x           # Stop on first failure
uv run pytest -v           # Verbose output
uv run pytest tests/test_ingester.py   # Run single module
```

## Test File Organization

**Location:**
- Flat `tests/` directory at project root — all test files co-located, not co-located with source

**Naming:**
- `test_<module>.py` matching the source module — `test_ingester.py` tests `ingester.py`, etc.
- Helper functions: `_make_<thing>()` prefix with leading underscore (private to module)

**Structure:**
```
tests/
├── __init__.py
├── conftest.py          # Shared fixtures: mock_conn, mock_pool, hass, registry mocks
├── test_config_flow.py  # 3 tests for TimescaledbConfigFlow
├── test_ingester.py     # 14 tests for StateIngester
├── test_schema.py       # 8 tests for async_setup_schema
└── test_syncer.py       # 8 tests for MetadataSyncer
```

## Test Structure

**Suite organization — no class grouping:**
All tests are module-level functions; no `class Test*` wrappers used. Tests are grouped by file (one file per source module).

**Typical synchronous test:**
```python
def test_buffer_appends_state(ingester):
    event = _make_state_event("sensor.temp", "21.5", {"unit_of_measurement": "°C"})
    ingester._handle_state_changed(event)
    assert len(ingester._buffer) == 1
    row = ingester._buffer[0]
    assert row[0] == "sensor.temp"
```

**Typical async test (no decorator needed — asyncio_mode=auto):**
```python
async def test_flush_calls_executemany(ingester, mock_conn):
    ingester._handle_state_changed(_make_state_event("sensor.temp", "22"))
    await ingester._async_flush()
    mock_conn.executemany.assert_called_once()
```

**Docstrings on tests:**
- Non-obvious tests carry a docstring explaining the invariant being verified and the Pitfall/Design note it covers:
  ```python
  async def test_registry_event_remove(hass, mock_pool, mock_conn, mock_entity_registry):
      """A remove event must close the open row without fetching from registry.

      Registry.async_get must NOT be called — the entry is already gone (Pitfall 4).
      """
  ```

## Mocking

**Framework:** `unittest.mock` — `MagicMock`, `AsyncMock`, `patch`, `call`

**asyncpg connection mock pattern:**
```python
@pytest.fixture
def mock_conn():
    """Return a mock asyncpg connection."""
    conn = AsyncMock()
    return conn

@pytest.fixture
def mock_pool(mock_conn):
    """Return a mock asyncpg pool whose acquire() context manager yields mock_conn."""
    pool = MagicMock()
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=mock_conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=cm)
    return pool
```
This pattern is shared via `conftest.py` and used in all four test modules.

**HA registry mock pattern (`conftest.py`):**
- A `_make_registry_entry(**attrs)` helper creates `MagicMock` objects with attributes set via `setattr`
- Registry mocks expose `.entities.values()`, `.async_get()`, `.async_list_areas()`, `.async_list_labels()` matching HA's API

**External service mock with `patch`:**
```python
with patch("asyncpg.connect", return_value=mock_conn) as mock_connect:
    result = await flow.async_step_user({CONF_DSN: "postgresql://user:pass@localhost/db"})
```

**What to mock:**
- `asyncpg.Pool` and `asyncpg.Connection` — always mocked, never real DB connections in tests
- HA `HomeAssistant` (`hass`) — `MagicMock` with `async_create_task` and `bus.async_listen` configured
- HA registries — `MagicMock` with the specific methods each test needs

**What NOT to mock:**
- Business logic under test — `StateIngester`, `MetadataSyncer`, `async_setup_schema` are instantiated for real
- SQL string constants — tests assert on the actual SQL constant values (`assert calls[0].args[0] == SCD2_CLOSE_ENTITY_SQL`)

## Fixtures and Factories

**Shared fixtures in `conftest.py`:**
- `mock_conn` — bare `AsyncMock` acting as asyncpg connection
- `mock_pool(mock_conn)` — pool mock wired to yield `mock_conn` from `acquire()`
- `hass` — `MagicMock` with `async_create_task` and `bus.async_listen`
- `mock_entity_registry` — 2 `EntityEntry`-like mocks (living room temp sensor + kettle switch)
- `mock_device_registry` — 2 `DeviceEntry`-like mocks
- `mock_area_registry` — 2 `AreaEntry`-like mocks
- `mock_label_registry` — 2 `LabelEntry`-like mocks

**Local fixtures per test module:**
- `test_ingester.py` defines its own `hass` fixture (shadows `conftest.hass`), `entity_filter`, and `ingester`
- Local fixtures take precedence over conftest fixtures within their module (standard pytest scoping)

**Test helper factory functions:**
```python
def _make_state_event(entity_id, state_val, attributes=None):
    """Create a mock state_changed event."""
    ...

def _make_filter(**kwargs):
    """Build an EntityFilter from flat keyword args."""
    ...

def _make_syncer(hass, mock_pool):
    """Return a MetadataSyncer with hass and pool; registries not yet set."""
    ...

def _patch_registries(syncer, mock_entity_registry, device_reg=None, area_reg=None, label_reg=None):
    """Directly inject mock registries so tests skip async_start() wiring."""
    ...
```
These are module-level private functions (leading underscore), not fixtures.

**Fixed timestamps in tests:**
```python
now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
```
Tests use hardcoded UTC datetimes for reproducibility.

## Test Types

**Unit Tests:**
- All tests are unit tests — no integration tests against a real database
- Each test module exercises one source module in isolation
- `test_ingester.py` — buffer logic, flush behavior, error recovery, entity filter
- `test_schema.py` — SQL statement order, count, parameter formatting, default values
- `test_syncer.py` — SCD2 event handling (create/update/remove/rename), snapshot idempotency, label serialization
- `test_config_flow.py` — form rendering, DSN validation, error state

**Integration Tests:**
- None — no real asyncpg/PostgreSQL connections used in any test

**E2E Tests:**
- Not used

## Coverage

**Requirements:** None enforced — no coverage threshold in `pyproject.toml` or CI config

**Observable coverage from test count:**
- `ingester.py` — 14 tests covering all public methods and both error paths
- `schema.py` — 8 tests covering statement count, order, parameter interpolation, and defaults
- `syncer.py` — 8 tests covering all four event actions (create/update/remove/rename) plus edge cases (reorder no-op, labels as list, idempotent snapshot)
- `config_flow.py` — 3 tests covering form display, success, and failure

## Common Patterns

**Asserting SQL calls by inspecting call_args:**
```python
executed_sql = [c.args[0] for c in mock_conn.execute.call_args_list]
snapshot_calls = [s for s in executed_sql if s == SCD2_SNAPSHOT_ENTITY_SQL]
assert len(snapshot_calls) == 2
```

**Asserting positional SQL parameter by index:**
```python
# args: (sql, entity_id, ha_entity_uuid, name, domain, platform, device_id, area_id, labels, ...)
labels_arg = calls[0].args[8]
assert isinstance(labels_arg, list)
```

**Resetting mock state between repeated calls in one test:**
```python
mock_conn.execute.reset_mock()
await async_setup_schema(mock_pool, compress_after_hours=48)
```

**Testing error recovery:**
```python
async def test_connection_error_requeues(ingester, mock_conn, mock_pool):
    mock_conn.executemany.side_effect = OSError("connection refused")
    mock_pool.expire_connections = MagicMock()
    ingester._handle_state_changed(_make_state_event("sensor.temp", "22"))
    await ingester._async_flush()
    mock_pool.expire_connections.assert_called_once()
    assert len(ingester._buffer) == 1
```

**Failure message on assertion:**
- Tests include custom messages on multi-condition assertions to aid debugging:
  ```python
  assert len(snapshot_calls) == 2, (
      f"Expected 2 snapshot entity calls, got {len(snapshot_calls)}. "
      f"All calls: {executed_sql}"
  )
  ```

## CI/CD Integration

- No CI configuration file detected (no `.github/workflows/`, no `Makefile`, no `tox.ini`)
- Tests are run locally via `uv run pytest`

---

*Testing analysis: 2026-04-18*
