# Phase 1: Thread Worker Foundation - Research

**Researched:** 2026-04-19
**Domain:** Python threading, psycopg3 sync connection lifecycle, HA async/thread boundary
**Confidence:** HIGH

## Summary

Phase 1 replaces the async, event-loop-resident asyncpg write path with a dedicated OS thread (`DbWorker` in `worker.py`) that owns all psycopg3 DB writes. The architecture is well-understood and mirrors the pattern HA's own recorder uses. The primary technical risks are the async/thread boundary (calling HA APIs incorrectly from the worker) and the shutdown sequence (deadlock if `.result()` is called after the event loop stops).

All locked design decisions (D-01 through D-19) are consistent with the HA recorder source and psycopg3 documentation. The research confirms every decision and adds concrete implementation details the planner needs: exact method signatures, placeholder syntax migration counts, psycopg3 error types to catch, the `queue.Queue.get(timeout=5)` flush loop pattern, and the exact `fetchone()` → not `fetchrow()` rename required when porting syncer change-detection methods.

One important correction to prior documentation: `hass.async_add_job` is deprecated (scheduled to break in HA 2025.4; still present but logs a warning in 2026.3.4). The correct thread-safe dispatcher from a worker thread is `hass.add_job` (sync method, no deprecation warning) for fire-and-forget, and `asyncio.run_coroutine_threadsafe(coro, hass.loop)` for cases where you need to wait on a result.

**Primary recommendation:** Build `DbWorker` in `worker.py` with a `queue.Queue.get(timeout=5)` flush loop; migrate all `$N` placeholders in `const.py` to `%s`; rename `fetchrow()` to `fetchone()` in the syncer change-detection helpers; use `hass.add_job` (not `async_add_job`) from the worker thread.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

**Schema Setup**
- D-01: `async_setup_entry` does minimal work — creates queue, starts worker thread, registers event listeners, returns True immediately. No DB calls in `async_setup_entry`.
- D-02: Schema DDL runs in the worker thread as its first act, before entering the flush loop.
- D-03: If DB unreachable at worker startup, schema setup fails silently (logged); worker enters main loop anyway. Phase 1 basic handling is sufficient.
- D-04: `MetadataSyncer.async_start()` iterates registries in the event loop (fast, in-memory), enqueues `MetaCommand` snapshot items, then registers four registry event listeners.

**Queue Payload**
- D-05: Single `queue.Queue` for all worker items — preserves ordering.
- D-06: Two frozen dataclasses: `StateRow(entity_id, state, attributes, last_updated, last_changed)` and `MetaCommand(registry, action, registry_id, old_id, params)`. `_STOP = object()` sentinel.
- D-07: `MetaCommand.registry` is a string literal. `MetaCommand.action` is `"create"`, `"update"`, or `"remove"`. `params` is a pre-extracted positional tuple.
- D-08: Registry param extraction happens in the `@callback` handler (event loop, thread-safe), not in the worker thread.

**Worker Module Structure**
- D-09: New `worker.py` module containing `DbWorker` class. `DbWorker` owns: `threading.Thread`, `queue.Queue`, `psycopg.Connection`, schema setup at startup, flush loop, SCD2 dispatch logic.
- D-10: `ingester.py` becomes a thin event relay: no async flush, no asyncpg.
- D-11: `syncer.py` becomes a thin event relay: extracts params in `@callbacks`, enqueues `MetaCommand`. SCD2 DB ops move to `DbWorker`.
- D-12: `TimescaledbRecorderData` drops `pool: asyncpg.Pool`; holds `worker: DbWorker` instead.

**MetadataSyncer Architecture**
- D-13: MetadataSyncer retains field extraction helpers and change detection helpers — these become sync methods using a psycopg3 cursor passed in from the worker.
- D-14: SCD2 write operations called by worker thread via `DbWorker._process_meta_command(cmd)`. Worker holds psycopg3 connection; MetadataSyncer methods receive a cursor/connection as argument.
- D-15: `_build_extra` / field extraction helpers stay as static or instance methods; do not touch the DB.

**psycopg3 SQL Adaptation**
- D-16: All SQL in `const.py` migrates from `$1, $2` to `%s` positional syntax.
- D-17: Dict attributes use `Jsonb(attrs_dict)` wrapper. `list[str]` labels adapt to `TEXT[]` automatically.

**HA Thread-Safety Bridges**
- D-18: Any HA coroutine called from worker uses `hass.add_job(coro)` or `asyncio.run_coroutine_threadsafe(coro, hass.loop).result()` with timeout. No `hass.async_*` calls directly from worker.
- D-19: Graceful shutdown: `async_unload_entry` puts `_STOP` in queue, then `await hass.async_add_executor_job(thread.join)`.

### Claude's Discretion

- Worker module naming: `DbWorker` in `worker.py`
- Queue item dataclasses defined in `worker.py` (or a new `models.py` — researcher/planner to decide)
- MetadataSyncer change-detection methods receive psycopg3 cursor; signature change from `async def _entity_row_changed(conn) -> bool` to `def _entity_row_changed(cur, ...) -> bool`
- Schema setup on worker startup uses a short-lived setup phase; if DB unreachable, logs warning and continues

### Deferred Ideas (OUT OF SCOPE)

None — discussion stayed within Phase 1 scope.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| WORK-01 | Integration uses a dedicated OS thread for all DB writes; HA event loop never blocked or crashed by DB errors | `threading.Thread(daemon=True)` owned by `DbWorker`; no DB calls in `async_setup_entry` |
| WORK-02 | HA event bus callback enqueues to `queue.Queue` (sync, safe from asyncio `@callback`); worker thread dequeues and owns all DB writes | `queue.Queue.put_nowait()` from `@callback` is thread-safe; worker calls `queue.get(timeout=5)` |
| WORK-03 | asyncpg replaced by `psycopg[binary]==3.3.3`; single `psycopg.Connection` owned by worker thread for its lifetime | `psycopg.connect(dsn)` called inside `DbWorker.run()`; connection not shared across threads |
| WORK-04 | psycopg3 JSONB writes wrap Python dicts in `Jsonb()`; `list[str]` adapts to `TEXT[]` automatically | `from psycopg.types.json import Jsonb`; `Jsonb(attrs_dict)` in `StateRow` enqueue site |
| WORK-05 | All `hass.async_*` calls from worker use thread-safe bridges | `hass.add_job(coro)` for fire-and-forget; `asyncio.run_coroutine_threadsafe(coro, hass.loop).result(timeout=N)` for blocking waits |
| BUF-03 | Flush interval remains 5 seconds | `queue.Queue.get(timeout=5)` causes worker to wake every 5 s and flush buffered `StateRow` items |
| WATCH-02 | Graceful shutdown: sentinel in queue, then `async_add_executor_job(thread.join)` | `_STOP = object()` sentinel; `async_unload_entry` drains, puts sentinel, awaits join |
</phase_requirements>

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| DB connection lifecycle | Worker thread | — | Connection not thread-safe to share; owned by worker for its full lifetime |
| State event capture | HA event loop (`@callback`) | — | `@callback` is sync, non-blocking; only safe place to receive events |
| State buffering (queue) | Shared boundary | — | `queue.Queue` is the concurrency-safe hand-off point |
| Schema DDL | Worker thread | — | D-02 locked; worker's first act before flush loop |
| Flush loop / batch write | Worker thread | — | All DB writes owned by worker thread |
| SCD2 change detection read | Worker thread | — | Needs psycopg3 cursor; cursor stays in worker thread |
| SCD2 close/insert write | Worker thread | — | Needs psycopg3 cursor; cursor stays in worker thread |
| Registry param extraction | HA event loop (`@callback`) | — | D-08 locked; registry access is only safe in event loop |
| Shutdown coordination | HA event loop (async) | Worker thread (sentinel) | `async_unload_entry` puts sentinel, awaits join via executor job |

## Standard Stack

### Core

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `psycopg[binary]` | `3.3.3` | Sync PostgreSQL driver | Only pg driver designed for threaded use; binary wheel for HA OS / aarch64; Python 3.14 compatible [VERIFIED: PyPI] |
| `threading.Thread` | stdlib | Worker OS thread isolation | Exact pattern HA recorder uses; daemon=True for clean exit [VERIFIED: HA recorder core.py] |
| `queue.Queue` | stdlib | Task queue with blocking `get(timeout=)` | `timeout=5` delivers the 5 s flush interval without a separate timer; `queue.SimpleQueue` has no timeout support [VERIFIED: Python stdlib docs] |
| `dataclasses` with `frozen=True, slots=True` | stdlib | `StateRow`, `MetaCommand` queue payloads | Immutable after enqueue; slots reduce memory per item [ASSUMED] |

### Supporting

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `psycopg.types.json.Jsonb` | ships with psycopg 3.3.3 | Wrap Python dicts for JSONB columns | Required — psycopg3 does NOT auto-adapt dicts to JSONB [VERIFIED: psycopg3 docs] |
| `asyncio.run_coroutine_threadsafe` | stdlib | Call HA async API from worker with blocking wait | Use when you need a result; always pass `timeout=N` [VERIFIED: HA PITFALLS.md, PR #45807] |
| `concurrent.futures.TimeoutError` | stdlib | Catch timeout from `run_coroutine_threadsafe(...).result(timeout=N)` | Prevents deadlock on shutdown if loop stops [VERIFIED: stdlib docs] |
| `attrs` | existing dep | `attrs.asdict()` in `_build_extra` | Already in use in `syncer.py`; no new dep |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `queue.Queue` | `queue.SimpleQueue` | SimpleQueue is faster but lacks `get(timeout=)` — timer would need a separate `threading.Event`, adding complexity. Queue is correct choice here. |
| `dataclasses(frozen=True, slots=True)` | `typing.NamedTuple` | NamedTuple is immutable too but slightly heavier for high-volume; frozen dataclass with slots is idiomatic Python 3.10+ |
| `worker.py` for dataclasses | `models.py` | Separate module only worthwhile if other modules import the types; `worker.py` is simpler since only `ingester.py` and `syncer.py` enqueue |

**Installation:**
```bash
# manifest.json requirements (HA installs on component load)
"requirements": ["psycopg[binary]==3.3.3"]

# Dev venv (via uv)
uv add "psycopg[binary]==3.3.3"
```

**Version verification:** psycopg 3.3.3 confirmed on PyPI [VERIFIED: PyPI April 2026].

## Architecture Patterns

### System Architecture Diagram

```
HA Event Loop Thread
  │
  ├── @callback _handle_state_changed()
  │     └── entity_filter check → StateRow → queue.put_nowait()
  │
  ├── @callback _handle_*_registry_updated()
  │     └── _extract_*_params() → MetaCommand → queue.put_nowait()
  │
  ├── async_setup_entry()
  │     ├── builds queue, starts DbWorker thread, returns True immediately
  │     └── MetadataSyncer.async_start(): iterates registries → enqueues MetaCommand snapshots
  │
  └── async_unload_entry()
        ├── puts _STOP sentinel into queue
        └── await hass.async_add_executor_job(thread.join)
                                │
                                ▼
        Worker OS Thread (DbWorker.run())
          │
          ├── [startup] psycopg.connect(dsn, autocommit=True)
          ├── [startup] schema DDL (idempotent; log + continue if DB unreachable)
          │
          └── [main loop] queue.get(timeout=5)
                ├── StateRow  → accumulate in _buffer list
                │              every 5 s (timeout) → executemany INSERT states
                ├── MetaCommand → _process_meta_command(cmd)
                │                  ├── "create"/"update" → change detection read + SCD2 insert
                │                  └── "remove" → SCD2 close
                └── _STOP → break (thread exits cleanly)
                                │
                                ▼
        TimescaleDB
          └── states hypertable + dim_* SCD2 tables
```

### Recommended Project Structure

```
custom_components/timescaledb_recorder/
├── __init__.py      # async_setup_entry, async_unload_entry (no DB ops)
├── worker.py        # DbWorker, StateRow, MetaCommand, _STOP
├── ingester.py      # StateIngester: thin @callback relay → queue
├── syncer.py        # MetadataSyncer: thin @callback relay → queue; sync change-detection methods
├── schema.py        # sync_setup_schema() — all DDL (replaces async_setup_schema)
├── const.py         # SQL with %s placeholders (migrated from $N)
├── config_flow.py   # unchanged
└── manifest.json    # requirements: psycopg[binary]==3.3.3
```

### Pattern 1: Worker Thread Flush Loop with Timeout

**What:** `queue.Queue.get(timeout=5)` wakes the worker at most every 5 seconds even when the queue is idle. This delivers BUF-03 (5 s flush interval) without a separate timer thread.

**When to use:** Any worker that needs both event-driven processing AND a periodic flush.

**Example:**
```python
# Source: Python stdlib queue.Queue docs + HA recorder core.py pattern
import queue

_STOP = object()

class DbWorker:
    def run(self) -> None:
        self._conn = psycopg.connect(self._dsn, autocommit=True)
        self._setup_schema()
        buffer: list[tuple] = []
        while True:
            try:
                item = self._queue.get(timeout=5)
            except queue.Empty:
                # Timeout — flush whatever is buffered
                if buffer:
                    self._flush(buffer)
                    buffer = []
                continue

            if item is _STOP:
                if buffer:
                    self._flush(buffer)
                break

            if isinstance(item, StateRow):
                buffer.append(item)
            elif isinstance(item, MetaCommand):
                # Flush pending states before meta ops to preserve ordering
                if buffer:
                    self._flush(buffer)
                    buffer = []
                self._process_meta_command(item)
```

### Pattern 2: psycopg3 Synchronous Connection and Cursor Lifecycle

**What:** Single `psycopg.connect()` with `autocommit=True`. Each DB operation creates its own cursor via `with conn.cursor() as cur`. Cursors are not shared between operations.

**When to use:** Single-writer worker thread owning one connection for its lifetime.

**Example:**
```python
# Source: https://www.psycopg.org/psycopg3/docs/basic/usage.html
import psycopg
from psycopg.types.json import Jsonb

conn = psycopg.connect(dsn, autocommit=True)

# Batch insert
with conn.cursor() as cur:
    cur.executemany(INSERT_SQL, rows)

# Single-row read (change detection)
with conn.cursor() as cur:
    cur.execute(SELECT_SQL, (entity_id,))
    row = cur.fetchone()   # NOT fetchrow() — psycopg3 uses fetchone()
    if row is None:
        return True  # No existing row → must insert
```

**Critical note:** asyncpg uses `conn.fetchrow()`. psycopg3 uses `cur.fetchone()`. All change-detection helpers in `syncer.py` must rename this call. [VERIFIED: psycopg3 API docs]

### Pattern 3: SCD2 Close + Insert in Worker Thread

**What:** MetadataSyncer change-detection and write methods become sync, receiving a psycopg3 cursor from the worker. The method signature changes from `async def _entity_row_changed(self, conn, ...)` to `def _entity_row_changed(self, cur, ...)`.

**When to use:** Any SCD2 operation called from the worker thread.

**Example:**
```python
# Before (asyncpg, async):
async def _entity_row_changed(self, conn, entity_id, new_params):
    row = await conn.fetchrow("SELECT ... WHERE entity_id = $1", entity_id)

# After (psycopg3, sync):
def _entity_row_changed(self, cur: psycopg.Cursor, entity_id: str, new_params: tuple) -> bool:
    cur.execute("SELECT ... WHERE entity_id = %s", (entity_id,))
    row = cur.fetchone()
```

### Pattern 4: Graceful Shutdown (WATCH-02)

**What:** `async_unload_entry` drains the queue, puts `_STOP`, awaits `async_add_executor_job(thread.join)`. Never calls `run_coroutine_threadsafe(...).result()` during teardown.

**When to use:** Every HA component that owns an OS thread. [VERIFIED: HA recorder core.py lines 432-433, 439-441]

**Example:**
```python
# Source: HA recorder core.py:432-433
async def async_unload_entry(hass, entry):
    worker = entry.runtime_data.worker
    worker.stop()  # puts _STOP into queue
    await hass.async_add_executor_job(worker.thread.join)
    return True

# In DbWorker:
def stop(self) -> None:
    """Thread-safe: called from event loop to initiate shutdown."""
    self._queue.put_nowait(_STOP)
```

### Pattern 5: Thread-Safe HA API Calls from Worker

**What:** `hass.add_job(coro)` is the fire-and-forget thread-safe path. It is NOT deprecated (only `hass.async_add_job` is deprecated as of 2025.4). For blocking waits with results, use `asyncio.run_coroutine_threadsafe` with a timeout.

**When to use:** Any HA API call from inside the worker thread.

**Example:**
```python
# Source: HA core.py:542-562 — hass.add_job uses loop.call_soon_threadsafe internally
# Fire-and-forget (Phase 3 observability, Phase 1 uses none)
hass.add_job(some_async_function, arg1)

# Blocking with timeout (Phase 1: not needed; Phase 3: async_create_issue)
import asyncio
import concurrent.futures
try:
    future = asyncio.run_coroutine_threadsafe(some_coro(), hass.loop)
    result = future.result(timeout=5.0)
except concurrent.futures.TimeoutError:
    _LOGGER.warning("Timed out calling HA async API from worker thread")
```

### Pattern 6: Schema Setup as Sync Function in Worker

**What:** `schema.py`'s `async_setup_schema(pool, ...)` must become `sync_setup_schema(conn, ...)` — a plain sync function accepting a psycopg3 connection. Called by the worker thread at startup.

**Example:**
```python
# Before (asyncpg, called from event loop):
async def async_setup_schema(pool, chunk_interval_days, compress_after_hours):
    async with pool.acquire() as conn:
        await conn.execute(CREATE_TABLE_SQL)

# After (psycopg3, called from worker thread):
def sync_setup_schema(conn: psycopg.Connection, chunk_interval_days: int, compress_after_hours: int) -> None:
    with conn.cursor() as cur:
        cur.execute(CREATE_TABLE_SQL)
        cur.execute(CREATE_HYPERTABLE_SQL.format(chunk_days=chunk_interval_days))
        # ... rest of DDL
```

### Anti-Patterns to Avoid

- **Calling `hass.async_add_job` from worker thread:** Deprecated in 2025.4; logs a warning in 2026.3.4. Use `hass.add_job` (sync) instead. [VERIFIED: HA core.py line 612-618]
- **Sharing a cursor between flush and change-detection:** Cursors are NOT thread-safe; creating a new cursor per operation is the correct pattern. [VERIFIED: psycopg3 concurrent ops docs]
- **`run_coroutine_threadsafe(...).result()` without timeout:** Deadlocks if the event loop has stopped; always pass `timeout=N`. [VERIFIED: HA PITFALLS.md, PR #45807]
- **`queue.SimpleQueue` for the task queue:** Has no `get(timeout=)` support; would require a separate `threading.Event` for the 5-second flush interval. Use `queue.Queue`. [VERIFIED: Python stdlib docs]
- **Using `asyncpg` style `conn.fetchrow()` in ported syncer methods:** psycopg3 cursors use `cur.fetchone()`. Using `fetchrow()` raises `AttributeError` at runtime. [VERIFIED: psycopg3 API docs]

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| JSONB serialization | Custom `json.dumps()` in worker | `psycopg.types.json.Jsonb(d)` | psycopg3 will raise a TypeError if a raw dict is passed to a JSONB column; `Jsonb()` wrapper handles encoding correctly [VERIFIED: psycopg3 type adaptation docs] |
| Batch insert performance | Manual `for row in rows: cur.execute(...)` loop | `cur.executemany(sql, rows)` | psycopg 3.1+ uses pipeline mode internally in executemany; sending SQL individually wastes round-trips [VERIFIED: psycopg3 pipeline docs] |
| Thread-safe event loop dispatch | `threading.Lock` + shared state | `queue.Queue` (producer) / `queue.get(timeout=5)` (consumer) | Standard, well-tested producer/consumer pattern; no locking needed on our side |
| Periodic flush timer | `threading.Timer` or `asyncio` interval | `queue.Queue.get(timeout=5)` — timeout IS the flush interval | Eliminates a separate timer thread and a threading.Event; simpler lifecycle |

**Key insight:** The `queue.Queue.get(timeout=5)` pattern means the worker thread is also its own flush scheduler — no `async_track_time_interval` needed after the refactor.

## Common Pitfalls

### Pitfall 1: asyncpg `fetchrow()` → psycopg3 `fetchone()`

**What goes wrong:** All four `_*_row_changed()` methods in `syncer.py` call `await conn.fetchrow(...)`. After porting to psycopg3, `fetchrow` does not exist on `psycopg.Cursor` — `AttributeError` at runtime.

**Why it happens:** The asyncpg and psycopg3 cursor APIs use different names for the same operation.

**How to avoid:** Rename every `conn.fetchrow(sql, *args)` call to `cur.execute(sql, (args,)); cur.fetchone()`. There are exactly 4 calls to rename (one per registry type).

**Warning signs:** `AttributeError: 'psycopg.Cursor' object has no attribute 'fetchrow'` in logs.

### Pitfall 2: `hass.async_add_job` is deprecated

**What goes wrong:** `async_add_job` still exists in HA 2026.3.4 but logs a deprecation warning (`breaks_in_ha_version="2025.4"`). Integration tests will show unexpected log warnings.

**Why it happens:** The CONTEXT.md and prior SUMMARY.md mention `hass.add_job` (correct) alongside `run_coroutine_threadsafe` — but the distinction between `add_job` (sync, fine) and `async_add_job` (deprecated) is easy to mix up.

**How to avoid:** Use `hass.add_job` (sync method, line 542 of core.py — no `report_usage` call, no deprecation). Never use `hass.async_add_job` from any code path. [VERIFIED: HA core.py 2026.3.4]

**Warning signs:** `calls 'async_add_job', which should be reviewed against ...` in HA logs.

### Pitfall 3: SQL placeholder mismatch ($N → %s)

**What goes wrong:** asyncpg uses `$1, $2, ...` positional placeholders. psycopg3 uses `%s` (positional) or `%(name)s` (named). Any unreplaced `$N` in `const.py` raises a `psycopg.errors.SyntaxError` at runtime.

**Why it happens:** There are 14 SQL constants in `const.py` that use `$N` syntax. Missing even one causes a runtime SQL error.

**How to avoid:** Count and migrate all `$N` occurrences. Full inventory (from codebase read):

| Constant | Placeholder count | Notes |
|----------|------------------|-------|
| `INSERT_SQL` | 5 (`$1`–`$5`) | `entity_id, state, attributes, last_updated, last_changed` |
| `SCD2_CLOSE_ENTITY_SQL` | 2 (`$1`–`$2`) | `valid_to, entity_id` |
| `SCD2_CLOSE_DEVICE_SQL` | 2 | |
| `SCD2_CLOSE_AREA_SQL` | 2 | |
| `SCD2_CLOSE_LABEL_SQL` | 2 | |
| `SCD2_SNAPSHOT_ENTITY_SQL` | 13 (`$1`–`$13`) | `$1` repeated in WHERE NOT EXISTS subquery |
| `SCD2_SNAPSHOT_DEVICE_SQL` | 8 | `$1` repeated in subquery |
| `SCD2_SNAPSHOT_AREA_SQL` | 4 | `$1` repeated in subquery |
| `SCD2_SNAPSHOT_LABEL_SQL` | 5 | `$1` repeated in subquery |
| `SCD2_INSERT_ENTITY_SQL` | 13 | |
| `SCD2_INSERT_DEVICE_SQL` | 8 | |
| `SCD2_INSERT_AREA_SQL` | 4 | |
| `SCD2_INSERT_LABEL_SQL` | 5 | |
| Change-detection inline SQL (in syncer.py) | 1 each × 4 | `WHERE entity_id = $1 AND valid_to IS NULL` |

Also: the two `.format()`-parameterized constants (`CREATE_HYPERTABLE_SQL` with `{chunk_days}` and `ADD_COMPRESSION_POLICY_SQL` with `{compress_hours}/{schedule_hours}`) use Python `.format()` — those `{}` placeholders are NOT psycopg parameters and must remain as-is.

**Warning signs:** `psycopg.errors.SyntaxError: syntax error at or near "$"` in HA logs.

### Pitfall 4: executemany pipeline mode error handling

**What goes wrong:** `cur.executemany(sql, rows)` uses pipeline mode internally (psycopg 3.1+). If one row in the batch causes a server error, the server aborts the implicit transaction and raises `PipelineAborted` for subsequent rows. With `autocommit=True`, the server still wraps the pipeline in an implicit transaction.

**Why it happens:** This is a known psycopg3 behavior documented in the pipeline mode docs.

**How to avoid:** For Phase 1, catch `psycopg.Error` broadly and log + continue. Phase 2 adds per-row isolation (SQLERR-03). For `INSERT_SQL` (no `ON CONFLICT`), a duplicate `(last_updated, entity_id)` would cause a `UniqueViolation` — this is fine because Phase 1 does not yet add the unique constraint (that's Phase 2/3).

**Warning signs:** `psycopg.errors.PipelineAborted` appearing after an initial `psycopg.errors.UniqueViolation` in a batch.

### Pitfall 5: `conn.execute()` vs `cur.execute()` — JSONB adaptation context

**What goes wrong:** `conn.execute()` (the convenience method) creates a cursor internally but does not inherit any custom type adapters registered on the connection. If `Jsonb` adaptation is registered on the connection's type map, using `conn.execute()` shortcut may bypass it.

**Why it happens:** psycopg3 docs distinguish between `conn.execute()` (convenience) and `conn.cursor().execute()`.

**How to avoid:** Use `with conn.cursor() as cur:` explicitly for all operations. Do not use `conn.execute()` shortcut. This is also consistent with the PITFALLS.md guidance to create a new cursor per operation. [VERIFIED: psycopg3 usage docs]

### Pitfall 6: daemon=True thread exit vs clean flush

**What goes wrong:** `threading.Thread(daemon=True)` means if HA exits without calling `async_unload_entry` (e.g., killed with SIGKILL), the daemon thread dies immediately with no final flush.

**Why it happens:** daemon=True is correct and intentional per D-19 (clean exit if unload skipped). The in-memory buffer is lost in this case.

**How to avoid:** This is the accepted trade-off. Document it. The proper shutdown path (`async_unload_entry` → sentinel → join) handles the clean case. [VERIFIED: PITFALLS.md checklist, HA recorder pattern]

## Code Examples

Verified patterns from official sources:

### psycopg3 connect + executemany

```python
# Source: https://www.psycopg.org/psycopg3/docs/basic/usage.html
import psycopg
from psycopg.types.json import Jsonb

conn = psycopg.connect(
    "postgresql://user:pass@host/dbname",
    autocommit=True,
)

# Batch insert with Jsonb wrapping
rows = [
    (entity_id, state, Jsonb(attributes), last_updated, last_changed)
    for entity_id, state, attributes, last_updated, last_changed in state_rows
]
with conn.cursor() as cur:
    cur.executemany(INSERT_SQL, rows)
```

### psycopg3 single-row read (change detection)

```python
# Source: https://www.psycopg.org/psycopg3/docs/api/connections.html
with conn.cursor() as cur:
    cur.execute(
        "SELECT name, platform, device_id, area_id, labels, "
        "device_class, unit_of_measurement, disabled_by, extra "
        "FROM entities WHERE entity_id = %s AND valid_to IS NULL",
        (entity_id,),
    )
    row = cur.fetchone()  # None if no open row exists
    if row is None:
        return True  # Must insert
```

### HA recorder shutdown pattern (reference implementation)

```python
# Source: HA recorder core.py:432-433, 439-441
# From async_close / _async_shutdown (both patterns used):
self.queue_task(StopTask())
await self.hass.async_add_executor_job(self.join)
```

### `hass.add_job` thread-safe dispatch (confirmed non-deprecated)

```python
# Source: HA core.py:542-562 (no report_usage call — confirmed not deprecated)
# From worker thread — safe:
hass.add_job(some_coroutine_function)   # schedules coro on event loop

# DO NOT USE (deprecated, logs warning):
# hass.async_add_job(some_coroutine_function)
```

### `queue.Queue.get(timeout=)` flush loop skeleton

```python
# Source: Python stdlib docs + HA recorder core.py pattern (adapted)
import queue

def run(self) -> None:
    buffer = []
    while True:
        try:
            item = self._queue.get(timeout=5.0)
        except queue.Empty:
            if buffer:
                self._do_flush(buffer)
                buffer.clear()
            continue

        if item is _STOP:
            if buffer:
                self._do_flush(buffer)
            break
        elif isinstance(item, StateRow):
            buffer.append(item)
        elif isinstance(item, MetaCommand):
            if buffer:
                self._do_flush(buffer)
                buffer.clear()
            self._process_meta_command(item)
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| asyncpg + async write path | psycopg3 sync connection in worker thread | Phase 1 of v1.1 | Eliminates async-in-thread complexity entirely |
| `async_track_time_interval` for flush timer | `queue.Queue.get(timeout=5)` inside worker | Phase 1 | Removes event-loop-side timer; simpler lifecycle |
| `pool: asyncpg.Pool` in `TimescaledbRecorderData` | `worker: DbWorker` in `TimescaledbRecorderData` | Phase 1 | Pool replaced by single connection owned by worker |
| `hass.async_create_task(self._async_flush())` | `queue.put_nowait(StateRow(...))` | Phase 1 | No async tasks for DB writes; no event loop dependency |
| `async def _entity_row_changed(conn, ...)` | `def _entity_row_changed(cur: Cursor, ...)` | Phase 1 | Sync method; cursor passed from worker thread |

**Deprecated/outdated:**
- `asyncpg==0.31.0`: removed from `manifest.json`; replaced by `psycopg[binary]==3.3.3`
- `hass.async_add_job`: deprecated in HA 2025.4; use `hass.add_job` from worker threads
- `async_setup_schema(pool, ...)`: becomes `sync_setup_schema(conn, ...)` called from worker

## SQL Placeholder Migration Reference

All `$N` placeholders in `const.py` and inline in `syncer.py` must become `%s`. Complete inventory:

```
const.py SQL constants requiring migration:
  INSERT_SQL:                  $1 $2 $3 $4 $5           → 5 replacements
  SCD2_CLOSE_ENTITY_SQL:       $1 $2                    → 2 replacements
  SCD2_CLOSE_DEVICE_SQL:       $1 $2                    → 2 replacements
  SCD2_CLOSE_AREA_SQL:         $1 $2                    → 2 replacements
  SCD2_CLOSE_LABEL_SQL:        $1 $2                    → 2 replacements
  SCD2_SNAPSHOT_ENTITY_SQL:    $1..$13 + $1 subquery    → 14 replacements
  SCD2_SNAPSHOT_DEVICE_SQL:    $1..$8 + $1 subquery     → 9 replacements
  SCD2_SNAPSHOT_AREA_SQL:      $1..$4 + $1 subquery     → 5 replacements
  SCD2_SNAPSHOT_LABEL_SQL:     $1..$5 + $1 subquery     → 6 replacements
  SCD2_INSERT_ENTITY_SQL:      $1..$13                  → 13 replacements
  SCD2_INSERT_DEVICE_SQL:      $1..$8                   → 8 replacements
  SCD2_INSERT_AREA_SQL:        $1..$4                   → 4 replacements
  SCD2_INSERT_LABEL_SQL:       $1..$5                   → 5 replacements

syncer.py inline SELECT statements (4 change-detection methods):
  _entity_row_changed: WHERE entity_id = $1             → 1 replacement
  _device_row_changed: WHERE device_id = $1             → 1 replacement
  _area_row_changed:   WHERE area_id = $1               → 1 replacement
  _label_row_changed:  WHERE label_id = $1              → 1 replacement

Total: ~82 placeholder replacements across 17 SQL strings.
NOTE: CREATE_HYPERTABLE_SQL and ADD_COMPRESSION_POLICY_SQL use Python .format() {}
      syntax — these are NOT psycopg parameters; leave {} curly braces unchanged.
```

## asyncpg → psycopg3 API Migration Reference

| asyncpg call | psycopg3 equivalent | Notes |
|-------------|---------------------|-------|
| `await conn.execute(sql, *args)` | `with conn.cursor() as cur: cur.execute(sql, (args,))` | Arguments must be a tuple/list, not unpacked |
| `await conn.executemany(sql, rows)` | `with conn.cursor() as cur: cur.executemany(sql, rows)` | Pipeline mode automatic in psycopg 3.1+ |
| `await conn.fetchrow(sql, *args)` | `cur.execute(sql, (args,)); cur.fetchone()` | RENAME: `fetchrow` → `fetchone` |
| `asyncpg.Pool.acquire()` | N/A — no pool | Single connection owned by worker |
| `pool.expire_connections()` | N/A | Connection error handling deferred to Phase 2 |
| `asyncpg.PostgresConnectionError` | `psycopg.OperationalError` | Superclass for connection/operational errors |
| `asyncpg.PostgresError` | `psycopg.DatabaseError` | Superclass for all DB errors |
| `dict` as JSONB | `Jsonb(dict)` | REQUIRED wrapper |
| `list[str]` as TEXT[] | `list[str]` unchanged | Auto-adapted |

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `frozen=True, slots=True` on `StateRow` / `MetaCommand` dataclasses | Standard Stack | Minimal — could use `frozen=True` without slots; slots reduce per-object memory but are not required for correctness |
| A2 | Flushing pending `StateRow` buffer before processing `MetaCommand` items preserves sufficient ordering | Worker flush loop pattern | If a MetaCommand arrives right after a state for the same entity, the state would flush first — which is correct behavior. Risk is low. |

**If this table is empty:** All claims in this research were verified or cited — no user confirmation needed.

## Open Questions

1. **Where should `StateRow`, `MetaCommand`, and `_STOP` be defined?**
   - What we know: CONTEXT.md says `worker.py` or a new `models.py` — both are mentioned as options.
   - What's unclear: Only `ingester.py` and `syncer.py` import these types; `worker.py` also defines and processes them.
   - Recommendation: Define in `worker.py`. A `models.py` is only worthwhile if types are imported by 3+ modules. With only 2 importers, `worker.py` is simpler. The planner should pick one and document it.

2. **Should `async_unload_entry` drain the queue before inserting the sentinel?**
   - What we know: HA recorder (core.py lines 427-432) drains the queue before inserting `StopTask()` when the database is broken. For a healthy shutdown this is not strictly necessary.
   - What's unclear: Whether we should drain to ensure the final flush occurs before the sentinel is processed.
   - Recommendation: Do NOT drain. The worker's flush loop handles any buffered `StateRow` items when it processes `_STOP` (before breaking). Draining from the event loop side would require a lock or atomicity guarantee we don't have. Keep it simple: put sentinel, join.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| `psycopg[binary]` | WORK-03 | Not yet — must be added to manifest.json | 3.3.3 (target) | — |
| Python 3.14 | Project | ✓ (venv is python3.14) | 3.14.x | — |
| TimescaleDB | All DB writes | ✓ (user has running instance) | ≥2.18.1 required | — |
| `uv` | Dev tooling | ✓ (per CLAUDE.md) | — | — |

**Missing dependencies with no fallback:**
- `psycopg[binary]==3.3.3` must be added to `manifest.json` requirements and `uv` dev venv.

## Project Constraints (from CLAUDE.md)

The following directives from `CLAUDE.md` apply to Phase 1 implementation:

| Directive | Applies To |
|-----------|-----------|
| NEVER use system Python; always use `uv` and `uvx` | Dev tooling — `uv add "psycopg[binary]==3.3.3"` |
| LSP for code navigation (`goToDefinition`, `findReferences`) | Executor must use LSP tools |
| All SQL in `const.py` — no inline SQL in business logic | SQL migration: move the 4 inline SELECT statements from `syncer.py` change-detection methods into `const.py` constants |
| Async public methods: `async_*` prefix | `DbWorker.async_stop()` should be the async teardown method name |
| Sync private HA callbacks: `_handle_*` with `@callback` decorator | Preserve `_handle_state_changed`, `_handle_*_registry_updated` patterns |
| Test files mirror production modules: `ingester.py` → `test_ingester.py` | New `worker.py` → `test_worker.py` needed |
| New runtime deps declared in `manifest.json` requirements | `psycopg[binary]==3.3.3` must be added |
| NEVER use `---` as section separators in markdown | Research docs compliance |
| NEVER use bold text as implicit headings | Research docs compliance |
| In code comments: document non-obvious decisions | Worker thread lifecycle decisions (daemon=True rationale, queue.Queue vs SimpleQueue rationale) must be commented |

**Extra constraint from CLAUDE.md project section:** "All SQL in `const.py` — no inline SQL in business logic." This means the 4 inline SELECT strings in `syncer.py`'s change-detection helpers (`_entity_row_changed`, etc.) must be extracted to named constants in `const.py` during the refactor. They currently embed SQL directly as string literals in the method body.

## Sources

### Primary (HIGH confidence)

- HA recorder `core.py` (local .venv, 2026.3.4) — `StopTask` sentinel pattern, `queue_task(StopTask())` + `async_add_executor_job(join)` shutdown, `hass.add_job` usage, `queue.SimpleQueue` as reference
- HA `core.py` (local .venv) — `hass.add_job` (line 542, not deprecated) vs `hass.async_add_job` (line 592, `report_usage` deprecation confirmed)
- HA `const.py` (local .venv) — `EVENT_HOMEASSISTANT_STOP` confirmed constant name
- [psycopg3 basic usage docs](https://www.psycopg.org/psycopg3/docs/basic/usage.html) — cursor lifecycle, `fetchone()` (not `fetchrow()`), connection context manager
- [psycopg3 API connection docs](https://www.psycopg.org/psycopg3/docs/api/connections.html) — `Connection.connect(autocommit=False)` signature, `cursor()` method
- [psycopg3 errors docs](https://www.psycopg.org/psycopg3/docs/api/errors.html) — `OperationalError` for connection failures, `DatabaseError` hierarchy
- [psycopg3 pipeline docs](https://www.psycopg.org/psycopg3/docs/advanced/pipeline.html) — `executemany` auto-uses pipeline mode in 3.1+; error cascade behavior
- `const.py` (codebase read) — complete inventory of all `$N` placeholder SQL constants
- `syncer.py` (codebase read) — all 4 inline `fetchrow()` calls identified; full method signatures documented
- `schema.py` (codebase read) — `async_setup_schema` identified for sync conversion
- `__init__.py` (codebase read) — current lifecycle to replace; `TimescaledbRecorderData` fields confirmed

### Secondary (MEDIUM confidence)

- [HA deprecation blog 2024-03-13](https://developers.home-assistant.io/blog/2024/03/13/deprecate_add_run_job) — confirms `async_add_job` deprecated; `add_job` (sync) is the replacement for thread-safe dispatch

### Tertiary (LOW confidence)

- None

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — psycopg3 3.3.3 verified on PyPI; HA recorder patterns confirmed from installed source
- Architecture: HIGH — all patterns verified from installed HA source and official psycopg3 docs
- SQL migration: HIGH — complete inventory read from `const.py` and `syncer.py` source
- API migration table: HIGH — verified from psycopg3 docs (fetchone vs fetchrow) and HA source (add_job deprecation)
- Pitfalls: HIGH — all sourced from installed HA source code and official docs

**Research date:** 2026-04-19
**Valid until:** 2026-05-19 (30 days — HA 2026.x stable; psycopg3 3.3.x stable)
