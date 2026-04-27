# Phase 1: Thread Worker Foundation - Pattern Map

**Mapped:** 2026-04-19
**Files analyzed:** 6 (1 new, 5 modified)
**Analogs found:** 6 / 6

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|---|---|---|---|---|
| `custom_components/timescaledb_recorder/worker.py` | service | event-driven + batch | `custom_components/timescaledb_recorder/ingester.py` | role-match (owns flush loop; different execution model) |
| `custom_components/timescaledb_recorder/ingester.py` | event-relay | event-driven | `custom_components/timescaledb_recorder/ingester.py` (self) | exact — thin version of existing class |
| `custom_components/timescaledb_recorder/syncer.py` | event-relay + helper | event-driven | `custom_components/timescaledb_recorder/syncer.py` (self) | exact — thin version of existing class |
| `custom_components/timescaledb_recorder/__init__.py` | config | request-response | `custom_components/timescaledb_recorder/__init__.py` (self) | exact — same lifecycle skeleton |
| `custom_components/timescaledb_recorder/const.py` | config | transform | `custom_components/timescaledb_recorder/const.py` (self) | exact — placeholder syntax migration only |
| `custom_components/timescaledb_recorder/schema.py` | utility | CRUD | `custom_components/timescaledb_recorder/schema.py` (self) | exact — sync conversion of existing function |

## Pattern Assignments

### `worker.py` (new service, event-driven + batch)

**Analog:** `ingester.py` (flush loop structure) + `syncer.py` (SCD2 dispatch logic)

**Imports pattern** — use these exact imports, replacing asyncpg with psycopg:

```python
# From ingester.py lines 1-13 (adapted for worker.py)
import logging
import queue
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg
from psycopg.types.json import Jsonb

from homeassistant.core import HomeAssistant

from .const import (
    INSERT_SQL,
    SCD2_CLOSE_ENTITY_SQL, SCD2_CLOSE_DEVICE_SQL,
    SCD2_CLOSE_AREA_SQL,  SCD2_CLOSE_LABEL_SQL,
    SCD2_SNAPSHOT_ENTITY_SQL, SCD2_SNAPSHOT_DEVICE_SQL,
    SCD2_SNAPSHOT_AREA_SQL,  SCD2_SNAPSHOT_LABEL_SQL,
    SCD2_INSERT_ENTITY_SQL,  SCD2_INSERT_DEVICE_SQL,
    SCD2_INSERT_AREA_SQL,    SCD2_INSERT_LABEL_SQL,
    # Change-detection SELECT constants (new in Phase 1 — moved from syncer.py):
    SELECT_ENTITY_CURRENT_SQL, SELECT_DEVICE_CURRENT_SQL,
    SELECT_AREA_CURRENT_SQL,   SELECT_LABEL_CURRENT_SQL,
)
```

**Queue payload dataclasses** — define at module top level in `worker.py` so `ingester.py` and `syncer.py` can import them:

```python
# frozen=True, slots=True: immutable after enqueue, slots reduce per-object memory.
# slots=True requires Python 3.10+; project requires 3.14 so this is safe.
_STOP = object()  # singleton sentinel — identity check only, never compared by value

@dataclass(frozen=True, slots=True)
class StateRow:
    entity_id: str
    state: str
    attributes: dict          # worker wraps in Jsonb() before SQL; kept as dict at enqueue site
    last_updated: datetime
    last_changed: datetime

@dataclass(frozen=True, slots=True)
class MetaCommand:
    registry: str             # "entity" | "device" | "area" | "label"
    action: str               # "create" | "update" | "remove"
    registry_id: str          # entity_id / device_id / area_id / label_id
    old_id: str | None        # only on entity renames; None otherwise
    params: tuple | None      # pre-extracted by @callback; None on "remove"
```

**DbWorker class skeleton** — mirrors the lifecycle pattern in `ingester.py` lines 17-36:

```python
class DbWorker:
    def __init__(
        self,
        hass: HomeAssistant,
        dsn: str,
        chunk_interval_days: int,
        compress_after_hours: int,
        syncer: "MetadataSyncer",   # passed in to call change-detection helpers
    ) -> None:
        self._hass = hass
        self._dsn = dsn
        self._chunk_interval_days = chunk_interval_days
        self._compress_after_hours = compress_after_hours
        self._syncer = syncer
        # queue.Queue (not SimpleQueue) — get(timeout=) needed for 5-second flush interval
        self._queue: queue.Queue = queue.Queue()
        # daemon=True: if HA exits without calling async_stop(), thread dies immediately.
        # The in-memory buffer is lost in this case — accepted trade-off documented in CONTEXT D-19.
        self._thread = threading.Thread(target=self.run, daemon=True, name="timescaledb_worker")
        self._conn: psycopg.Connection | None = None
```

**Startup pattern** — mirrors `ingester.py` lines 37-48 (`async_start`), but sync:

```python
    def start(self) -> None:
        """Start the worker thread. Called from async_setup_entry (event loop)."""
        self._thread.start()

    def run(self) -> None:
        """Worker thread entry point. Owns the full DB lifecycle."""
        try:
            self._conn = psycopg.connect(self._dsn, autocommit=True)
        except psycopg.OperationalError as err:
            _LOGGER.warning("DB unreachable at worker startup: %s — buffering in memory", err)
            # D-03: enter flush loop anyway; buffered items accumulate in queue
        else:
            self._setup_schema()

        buffer: list[StateRow] = []
        while True:
            try:
                item = self._queue.get(timeout=5.0)
            except queue.Empty:
                # 5-second timeout expired — flush whatever is buffered (BUF-03)
                if buffer and self._conn is not None:
                    self._flush(buffer)
                    buffer.clear()
                continue

            if item is _STOP:
                if buffer and self._conn is not None:
                    self._flush(buffer)
                break
            elif isinstance(item, StateRow):
                buffer.append(item)
            elif isinstance(item, MetaCommand):
                # Flush pending states before processing meta — preserves ordering
                # (e.g., an entity rename must not overtake the last state for that entity)
                if buffer and self._conn is not None:
                    self._flush(buffer)
                    buffer.clear()
                if self._conn is not None:
                    self._process_meta_command(item)
```

**Flush pattern** — adapted from `ingester.py` lines 74-95 (`_async_flush`), now sync with psycopg3:

```python
    def _flush(self, buffer: list[StateRow]) -> None:
        """Batch-insert buffered StateRow items. Called from worker thread only."""
        rows = [
            (
                row.entity_id,
                row.state,
                Jsonb(row.attributes),   # WORK-04: dict must be wrapped for JSONB column
                row.last_updated,
                row.last_changed,
            )
            for row in buffer
        ]
        try:
            with self._conn.cursor() as cur:
                cur.executemany(INSERT_SQL, rows)
        except psycopg.OperationalError:
            _LOGGER.warning("Connection error during flush; %d rows dropped (Phase 2 adds retry)", len(rows))
        except psycopg.DatabaseError as err:
            _LOGGER.error("DB error during flush; %d rows dropped: %s", len(rows), err)
```

**Shutdown pattern** — mirrors `ingester.py` lines 101-109 (`async_stop`) but crosses thread boundary:

```python
    def stop(self) -> None:
        """Put _STOP sentinel. Thread-safe: called from event loop."""
        self._queue.put_nowait(_STOP)

    async def async_stop(self) -> None:
        """Async stop for use in async_unload_entry.

        Puts sentinel then awaits thread join via executor job.
        MUST NOT use run_coroutine_threadsafe().result() here — deadlocks on shutdown (WATCH-02).
        """
        self.stop()
        await self._hass.async_add_executor_job(self._thread.join)
```

**Error handling pattern** — copy error hierarchy from `ingester.py` lines 85-95, replacing asyncpg types:

```python
# asyncpg.PostgresConnectionError → psycopg.OperationalError  (connection-level)
# asyncpg.PostgresError           → psycopg.DatabaseError      (all DB errors)
# asyncpg.OSError                 → OSError                    (unchanged)
```

---

### `ingester.py` (modify — thin event relay)

**Analog:** `ingester.py` (self, lines 51-73)

The `@callback` handler and entity filter check are the only parts that survive unchanged. Everything else is removed.

**Pattern to preserve** (lines 51-73):

```python
# ingester.py lines 51-69 — the @callback boundary and entity filter stay exactly as-is.
# Only the append changes: buffer.append(tuple) → queue.put_nowait(StateRow(...))
@callback
def _handle_state_changed(self, event: Event) -> None:
    """Handle state_changed events — synchronous, never awaits."""
    new_state = event.data.get("new_state")
    if new_state is None:
        return

    entity_id = new_state.entity_id
    if not self._entity_filter(entity_id):
        return

    # Enqueue immutable payload. dict(new_state.attributes) copies the HA state
    # snapshot — avoids a reference to a mutable object that HA may update later.
    self._queue.put_nowait(StateRow(
        entity_id=entity_id,
        state=new_state.state,
        attributes=dict(new_state.attributes),
        last_updated=new_state.last_updated,
        last_changed=new_state.last_changed,
    ))
```

**Pattern to remove:**
- `_buffer: list[tuple]` — replaced by shared `queue.Queue` in `DbWorker`
- `_batch_size`, `_flush_interval` constructor args — no longer needed
- `_cancel_timer` and `async_track_time_interval` — worker's `queue.get(timeout=5)` replaces the timer
- `_async_flush()`, `_async_flush_timer()` — move to worker
- `async_stop()` final flush — worker handles the final flush on `_STOP`

**New constructor signature:**

```python
def __init__(
    self,
    hass: HomeAssistant,
    queue: queue.Queue,          # shared queue owned by DbWorker
    entity_filter: EntityFilter,
) -> None:
```

**New imports** (replacing asyncpg and async_track_time_interval):

```python
import queue
from homeassistant.core import HomeAssistant, Event, callback
from homeassistant.helpers.entityfilter import EntityFilter
from .worker import StateRow
```

---

### `syncer.py` (modify — thin relay + sync change-detection helpers)

**Analog:** `syncer.py` (self)

Three distinct transformation patterns apply:

**Pattern A: @callback handlers become pure enqueuers** (lines 337-346, 391-397, 435-442, 486-492)

Current pattern (lines 337-346):
```python
# CURRENT — schedules async task from @callback
@callback
def _handle_entity_registry_updated(self, event: Event) -> None:
    action = event.data["action"]
    entity_id = event.data["entity_id"]
    old_entity_id = event.data.get("old_entity_id")
    self._hass.async_create_task(
        self._async_process_entity_event(action, entity_id, old_entity_id)
    )
```

New pattern — extract params in `@callback`, enqueue `MetaCommand`:
```python
# NEW — all DB work moves to worker; @callback only extracts registry data
@callback
def _handle_entity_registry_updated(self, event: Event) -> None:
    """Extract registry params and enqueue MetaCommand. No DB access here (D-08)."""
    action = event.data["action"]
    entity_id = event.data["entity_id"]
    old_entity_id = event.data.get("old_entity_id")

    if action == "remove":
        # Registry entry already gone — do NOT call async_get (Pitfall 4 in syncer)
        params = None
    else:
        entry = self._entity_reg.async_get(entity_id)
        params = self._extract_entity_params(entry, datetime.now(timezone.utc))

    self._queue.put_nowait(MetaCommand(
        registry="entity",
        action=action,
        registry_id=entity_id,
        old_id=old_entity_id,
        params=params,
    ))
```

**Pattern B: _async_process_*_event methods are deleted** — their SCD2 logic moves to `DbWorker._process_meta_command()`.

**Pattern C: change-detection helpers become sync** (lines 249-331)

Current signature (line 249):
```python
async def _entity_row_changed(self, conn, entity_id: str, new_params: tuple) -> bool:
    row = await conn.fetchrow(
        "SELECT name, platform, device_id, area_id, labels, device_class,"
        " unit_of_measurement, disabled_by, extra"
        " FROM entities WHERE entity_id = $1 AND valid_to IS NULL",
        entity_id,
    )
```

New signature:
```python
def _entity_row_changed(self, cur: psycopg.Cursor, entity_id: str, new_params: tuple) -> bool:
    # SQL constant defined in const.py (CLAUDE.md: no inline SQL in business logic)
    cur.execute(SELECT_ENTITY_CURRENT_SQL, (entity_id,))
    row = cur.fetchone()   # psycopg3 uses fetchone(), NOT fetchrow() (asyncpg rename)
```

The comparison body (lines 267-275) is unchanged — column access switches from `row["name"]` to positional index matching the SELECT column order (psycopg3 returns tuples by default, not dicts — use `psycopg.rows.dict_row` cursor factory or access by index). Using `dict_row` matches current asyncpg row["name"] style most closely:

```python
# In DbWorker._process_meta_command(), create cursor with dict_row factory:
with self._conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
    changed = self._syncer._entity_row_changed(cur, entity_id, params)
```

**`async_start` pattern** (lines 120-148) — snapshot iteration changes from `await conn.execute()` to `queue.put_nowait(MetaCommand(...))`:

```python
async def async_start(self) -> None:
    """Iterate registries in event loop (fast, in-memory) and enqueue snapshots.

    D-04: enqueue snapshot MetaCommands before registering listeners so no
    registry events are missed and initial snapshot data is queued for the worker.
    """
    self._entity_reg = er.async_get(self._hass)
    # ... other registries ...

    now = datetime.now(timezone.utc)
    for entry in self._entity_reg.entities.values():
        params = self._extract_entity_params(entry, now)
        self._queue.put_nowait(MetaCommand(
            registry="entity", action="create",
            registry_id=entry.entity_id, old_id=None, params=params,
        ))
    # ... repeat for device, area, label ...

    # Register listeners AFTER snapshot enqueue (D-04: no missed events)
    self._cancel_listeners.append(
        self._hass.bus.async_listen(EVENT_ENTITY_REGISTRY_UPDATED, self._handle_entity_registry_updated)
    )
    # ... other listeners ...
```

**New constructor signature:**

```python
def __init__(self, hass: HomeAssistant, queue: queue.Queue) -> None:
```

**`async_stop` pattern** (lines 530-538) — unchanged in structure:

```python
async def async_stop(self) -> None:
    for cancel in self._cancel_listeners:
        cancel()
    self._cancel_listeners.clear()
```

---

### `__init__.py` (modify — lifecycle)

**Analog:** `__init__.py` (self, lines 32-114)

**`TimescaledbRecorderData` dataclass** — drop `pool: asyncpg.Pool`, add `worker: DbWorker` (lines 32-38):

```python
# CURRENT (lines 32-38):
@dataclass
class TimescaledbRecorderData:
    ingester: StateIngester
    syncer: MetadataSyncer
    pool: asyncpg.Pool

# NEW:
@dataclass
class TimescaledbRecorderData:
    worker: DbWorker
    ingester: StateIngester
    syncer: MetadataSyncer
```

**`async_setup_entry` pattern** (lines 59-100) — D-01 locked: no DB calls, returns True immediately:

```python
# CURRENT: creates pool, calls async_setup_schema, then starts ingester/syncer
# NEW: creates queue + worker, starts worker thread, then starts ingester/syncer

async def async_setup_entry(hass: HomeAssistant, entry: TimescaledbRecorderConfigEntry) -> bool:
    dsn = entry.data[CONF_DSN]
    options = entry.options
    chunk_interval_days = options.get(CONF_CHUNK_INTERVAL, DEFAULT_CHUNK_INTERVAL_DAYS)
    compress_after_hours = options.get(CONF_COMPRESS_AFTER, DEFAULT_COMPRESS_AFTER_HOURS)
    entity_filter = _get_entity_filter(entry)

    # MetadataSyncer must be created before DbWorker so worker can hold a reference
    syncer = MetadataSyncer(hass=hass, queue=_q)  # _q passed below
    worker = DbWorker(
        hass=hass,
        dsn=dsn,
        chunk_interval_days=chunk_interval_days,
        compress_after_hours=compress_after_hours,
        syncer=syncer,
    )
    ingester = StateIngester(hass=hass, queue=worker.queue, entity_filter=entity_filter)

    worker.start()                  # starts daemon thread; returns immediately
    ingester.async_start()          # registers STATE_CHANGED listener
    await syncer.async_start()      # enqueues snapshot, registers 4 registry listeners

    entry.runtime_data = TimescaledbRecorderData(worker=worker, ingester=ingester, syncer=syncer)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True
```

**`async_unload_entry` pattern** (lines 108-114) — WATCH-02 shutdown:

```python
# CURRENT: cancels timers, closes pool
# NEW: cancel event listeners, put _STOP, await thread.join

async def async_unload_entry(hass: HomeAssistant, entry: TimescaledbRecorderConfigEntry) -> bool:
    data: TimescaledbRecorderData = entry.runtime_data
    # Stop event listeners first so no new items are enqueued during shutdown
    ingester.async_stop()           # sync: just cancels listener (no final flush needed)
    await data.syncer.async_stop()  # cancels 4 registry listeners
    await data.worker.async_stop()  # puts _STOP sentinel, awaits thread.join
    return True
```

**Imports change** — remove asyncpg, add worker:

```python
# Remove: import asyncpg, from .schema import async_setup_schema
# Add:
from .worker import DbWorker
```

---

### `const.py` (modify — SQL placeholder migration)

**Analog:** `const.py` (self, lines 63-260)

**Migration rule:** Replace every `$N` with `%s`. The positional order is preserved — only the syntax token changes. The `{}` format strings in `CREATE_HYPERTABLE_SQL` and `ADD_COMPRESSION_POLICY_SQL` are Python `.format()` parameters and must NOT be changed.

**Example of the transformation** (lines 63-66):

```python
# CURRENT:
INSERT_SQL = f"""
INSERT INTO {TABLE_NAME} (entity_id, state, attributes, last_updated, last_changed)
VALUES ($1, $2, $3, $4, $5)
"""

# NEW:
INSERT_SQL = f"""
INSERT INTO {TABLE_NAME} (entity_id, state, attributes, last_updated, last_changed)
VALUES (%s, %s, %s, %s, %s)
"""
```

**Change-detection SELECT constants** — currently inline strings in `syncer.py`, must move here per CLAUDE.md convention "All SQL in `const.py`":

```python
# NEW constants — SQL for change-detection reads (moved from syncer.py inline strings)
SELECT_ENTITY_CURRENT_SQL = (
    "SELECT name, platform, device_id, area_id, labels, device_class,"
    " unit_of_measurement, disabled_by, extra"
    " FROM entities WHERE entity_id = %s AND valid_to IS NULL"
)

SELECT_DEVICE_CURRENT_SQL = (
    "SELECT name, manufacturer, model, area_id, labels, extra"
    " FROM devices WHERE device_id = %s AND valid_to IS NULL"
)

SELECT_AREA_CURRENT_SQL = (
    "SELECT name, extra FROM areas WHERE area_id = %s AND valid_to IS NULL"
)

SELECT_LABEL_CURRENT_SQL = (
    "SELECT name, color, extra FROM labels WHERE label_id = %s AND valid_to IS NULL"
)
```

**Full migration count per constant** (from RESEARCH.md pitfall inventory):

| Constant | Replacements |
|---|---|
| `INSERT_SQL` | 5 |
| `SCD2_CLOSE_ENTITY_SQL` | 2 |
| `SCD2_CLOSE_DEVICE_SQL` | 2 |
| `SCD2_CLOSE_AREA_SQL` | 2 |
| `SCD2_CLOSE_LABEL_SQL` | 2 |
| `SCD2_SNAPSHOT_ENTITY_SQL` | 14 (`$1` repeated in subquery) |
| `SCD2_SNAPSHOT_DEVICE_SQL` | 9 (`$1` repeated in subquery) |
| `SCD2_SNAPSHOT_AREA_SQL` | 5 (`$1` repeated in subquery) |
| `SCD2_SNAPSHOT_LABEL_SQL` | 6 (`$1` repeated in subquery) |
| `SCD2_INSERT_ENTITY_SQL` | 13 |
| `SCD2_INSERT_DEVICE_SQL` | 8 |
| `SCD2_INSERT_AREA_SQL` | 4 |
| `SCD2_INSERT_LABEL_SQL` | 5 |
| Subtotal existing | ~77 |
| 4 new SELECT constants | 4 (1 each) |
| **Total** | **~81** |

---

### `schema.py` (modify — sync conversion)

**Analog:** `schema.py` (self, lines 29-75)

The function signature and body change from asyncpg async pool-based to psycopg3 sync connection-based. The SQL statements and their execution order are unchanged.

**Current signature** (line 29):

```python
async def async_setup_schema(
    pool: asyncpg.Pool,
    chunk_interval_days: int = DEFAULT_CHUNK_INTERVAL_DAYS,
    compress_after_hours: int = DEFAULT_COMPRESS_AFTER_HOURS,
) -> None:
```

**New signature** (called from `DbWorker.run()` after `psycopg.connect()`):

```python
def sync_setup_schema(
    conn: psycopg.Connection,
    chunk_interval_days: int = DEFAULT_CHUNK_INTERVAL_DAYS,
    compress_after_hours: int = DEFAULT_COMPRESS_AFTER_HOURS,
) -> None:
```

**Body transformation pattern** (lines 43-68):

```python
# CURRENT (asyncpg):
async with pool.acquire() as conn:
    await conn.execute(CREATE_TABLE_SQL)
    await conn.execute(CREATE_HYPERTABLE_SQL.format(...))
    # ...

# NEW (psycopg3):
with conn.cursor() as cur:
    cur.execute(CREATE_TABLE_SQL)
    cur.execute(CREATE_HYPERTABLE_SQL.format(chunk_days=chunk_interval_days))
    # ...
```

**Imports change** — remove asyncpg, add psycopg:

```python
# Remove: import asyncpg
# Add: import psycopg
```

**Error handling** — per D-03, caller (`DbWorker._setup_schema`) catches `psycopg.OperationalError` and logs + continues. `sync_setup_schema` itself does not need to catch — let the caller decide.

---

## Shared Patterns

### @callback decorator (event loop boundary)

**Source:** `ingester.py` lines 51-52, `syncer.py` lines 337, 391, 435, 486

**Apply to:** All HA event handler methods in `ingester.py` and `syncer.py`

```python
from homeassistant.core import callback

@callback
def _handle_state_changed(self, event: Event) -> None:
    """Handle state_changed events — synchronous, never awaits."""
```

The `@callback` decorator asserts the function never awaits. It must be preserved on all `_handle_*` methods — these are the event loop boundary. Any blocking or async work crosses to the worker via `queue.put_nowait()`.

### queue.Queue as the thread boundary

**Source:** `ingester.py` line 33 (`self._buffer: list[tuple]` — replaced by queue) + RESEARCH.md Pattern 1

**Apply to:** `ingester.py` `_handle_state_changed`, `syncer.py` all `_handle_*` methods, `worker.py` `run()`

```python
# Producer side (event loop thread) — always put_nowait, never put()
# put_nowait is non-blocking and safe from @callback context
self._queue.put_nowait(StateRow(...))   # ingester.py
self._queue.put_nowait(MetaCommand(...))  # syncer.py

# Consumer side (worker thread)
item = self._queue.get(timeout=5.0)    # worker.py
```

### Event listener registration / cancellation

**Source:** `ingester.py` lines 39-48, `syncer.py` lines 129-148, 530-538

**Apply to:** `ingester.py` `async_start` / `async_stop`, `syncer.py` `async_start` / `async_stop`

```python
# Registration — returns a cancel callable
self._cancel_listener = self._hass.bus.async_listen(EVENT_STATE_CHANGED, self._handle_state_changed)

# Cancellation — call the returned callable
if self._cancel_listener is not None:
    self._cancel_listener()
    self._cancel_listener = None
```

For `syncer.py` with multiple listeners, use the list pattern (lines 113, 530-538):
```python
self._cancel_listeners: list[Callable] = []
self._cancel_listeners.append(self._hass.bus.async_listen(...))
# On stop:
for cancel in self._cancel_listeners:
    cancel()
self._cancel_listeners.clear()
```

### psycopg3 cursor-per-operation

**Source:** RESEARCH.md Pattern 2 (verified from psycopg3 docs)

**Apply to:** All DB operations in `worker.py` and `schema.py`

```python
# Create a new cursor per operation — cursors are not thread-safe to share
with conn.cursor() as cur:
    cur.executemany(INSERT_SQL, rows)     # batch insert

with conn.cursor() as cur:
    cur.execute(SELECT_SQL, (id_val,))
    row = cur.fetchone()                  # None if no row
```

For change-detection reads that use column names (matching current `row["name"]` pattern from asyncpg):
```python
import psycopg.rows
with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
    cur.execute(SELECT_ENTITY_CURRENT_SQL, (entity_id,))
    row = cur.fetchone()   # dict or None
```

### psycopg3 error hierarchy

**Source:** `ingester.py` lines 85-91, RESEARCH.md API migration table

**Apply to:** All `try/except` blocks in `worker.py`

```python
import psycopg

try:
    with self._conn.cursor() as cur:
        cur.executemany(INSERT_SQL, rows)
except psycopg.OperationalError:
    # Connection-level error (replaces asyncpg.PostgresConnectionError + OSError)
    _LOGGER.warning(...)
except psycopg.DatabaseError as err:
    # All other DB errors (replaces asyncpg.PostgresError)
    _LOGGER.error(...)
```

### Graceful shutdown (WATCH-02)

**Source:** `__init__.py` lines 108-114 (pattern to replace), RESEARCH.md Pattern 4

**Apply to:** `__init__.py` `async_unload_entry`, `worker.py` `async_stop`

```python
# In async_unload_entry — event loop side:
data.worker.stop()                             # puts _STOP (non-blocking)
await hass.async_add_executor_job(data.worker._thread.join)

# NEVER use this on shutdown path (deadlock if event loop is stopping):
# asyncio.run_coroutine_threadsafe(...).result()
```

### Logging pattern

**Source:** All existing modules, e.g. `ingester.py` line 14, `syncer.py` line 35

**Apply to:** `worker.py` (new file)

```python
_LOGGER = logging.getLogger(__name__)
```

## No Analog Found

All files in Phase 1 have close analogs in the existing codebase. No files require falling back to RESEARCH.md external patterns exclusively.

The `worker.py` file is the only genuinely new module; it is a synthesis of patterns from `ingester.py` (flush loop, lifecycle) and `syncer.py` (SCD2 dispatch). No external analog needed.

## Test Pattern

**Source:** `tests/test_ingester.py` + `tests/conftest.py`

**Apply to:** `tests/test_worker.py` (new, required by CLAUDE.md convention)

The test pattern uses plain `unittest.mock` (no pytest-homeassistant-custom-component for unit tests), with a `MagicMock` hass and explicit `_handle_*` method calls to simulate events. The `mock_conn` / `mock_pool` fixtures in `conftest.py` must be replaced with a mock `psycopg.Connection` for the worker tests.

Key patterns from `test_ingester.py`:
- `_make_state_event()` helper (lines 28-41) — reuse unchanged in `test_worker.py`
- Direct method calls instead of event bus: `ingester._handle_state_changed(event)` (line 76)
- Assert on internal state before flush: `assert len(ingester._buffer) == 1` (line 75)
- `async def test_*` for tests that call async methods (lines 147+)

New fixtures needed for `test_worker.py`:
```python
@pytest.fixture
def mock_psycopg_conn():
    """Return a mock psycopg3 connection with cursor context manager support."""
    conn = MagicMock(spec=psycopg.Connection)
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=cur)
    return conn, cur
```

## Metadata

**Analog search scope:** `custom_components/timescaledb_recorder/` (all 5 existing source files), `tests/` (4 test files + conftest)
**Files scanned:** 9
**Pattern extraction date:** 2026-04-19
