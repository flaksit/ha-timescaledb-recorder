# Architecture Research

**Domain:** HA custom component — robust TimescaleDB ingestion with threaded worker
**Researched:** 2026-04-19
**Confidence:** HIGH (TimescaleDB/HA source verified), MEDIUM (threading patterns from stdlib docs + HA source)

## Standard Architecture

### System Overview

```
┌───────────────────────────────────────────────────────────────────┐
│                     HA Event Loop Thread                          │
│                                                                   │
│  @callback _handle_state_changed                                  │
│       │  (non-blocking, appends to bounded ring-buffer)           │
│       ▼                                                           │
│  StateBuffer [deque maxlen=10000]                                 │
│       │  (queue.put_nowait — thread-safe by design)               │
│       ▼                                                           │
│  queue.SimpleQueue / collections.deque  ←──────────────────────┐ │
└───────────────────────────────────────────────────────────────┐ │ │
                                                                │ │ │
┌───────────────────────────────────────────────────────────────┘ │ │
│                  Dedicated Worker OS Thread                      │ │
│                                                                  │ │
│  ┌───────────────┐    threading.Lock    ┌────────────────────┐   │ │
│  │  FlushWriter  │ ◄──── write_lock ───►│  BackfillWriter    │   │ │
│  │  (5s timer)   │                     │  (startup + recov) │   │ │
│  └──────┬────────┘                     └────────┬───────────┘   │ │
│         │                                       │               │ │
│         └─────────────────┬─────────────────────┘               │ │
│                           ▼                                     │ │
│                  psycopg3 connection                             │ │
│                  (single persistent conn, not pool)             │ │
└─────────────────────────────────────────────────────────────────┘ │
                                                                    │
┌───────────────────────────────────────────────────────────────────┘
│              TimescaleDB (PostgreSQL add-on)                       │
│                                                                    │
│  states hypertable (partitioned by last_updated, 7d chunks)    │
│  entities / devices / areas / labels (SCD2)       │
└────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│           HA Recorder Thread (separate, read-only access)        │
│                                                                  │
│  home-assistant_v2.db (SQLite, WAL mode)                        │
│  state_changes_during_period() via recorder.get_instance(hass)  │
│       .async_add_executor_job(history.state_changes_during...)  │
└─────────────────────────────────────────────────────────────────┘
```

### Component Responsibilities

| Component | Responsibility | Implementation |
|-----------|----------------|----------------|
| `@callback _handle_state_changed` | Capture event, filter, append to buffer — NEVER blocks | Sync callback on HA event loop |
| `StateBuffer` | Bounded deque (maxlen=10000), drop-oldest with warning on overflow | `collections.deque(maxlen=10000)` or `queue.Queue(maxsize=10000)` |
| `WorkerThread` | Owns psycopg3 connection; runs flush loop and backfill; isolated from HA event loop | `threading.Thread(daemon=True)` |
| `FlushWriter` | Drain buffer every 5 s; holds `write_lock` for duration of one executemany batch | psycopg3 `executemany` within `with write_lock:` |
| `BackfillWriter` | On startup and DB-healthy transition: query HA sqlite, chunk results, insert with per-chunk lock release | Calls `get_instance(hass).async_add_executor_job(history.state_changes_during_period, ...)` from async layer, then passes chunks to worker via queue |
| `MetadataSyncer` | Re-snapshot all registry tables on startup/recovery; incremental SCD2 on registry events | `WHERE NOT EXISTS` idempotent inserts in worker thread |
| `Watchdog` | Async task on HA event loop; checks worker liveness; restarts thread + fires persistent_notification on death | `async_track_time_interval` |

## Recommended Project Structure

```
custom_components/timescaledb_recorder/
├── __init__.py          # async_setup_entry, async_unload_entry, watchdog task
├── config_flow.py       # unchanged from v0.3.6
├── const.py             # SQL constants, config keys, defaults
├── buffer.py            # StateBuffer: bounded deque + thread-safe put/drain
├── worker.py            # WorkerThread: flush loop, backfill trigger, lock ownership
├── ingester.py          # @callback event handler (HA event loop side only)
├── syncer.py            # MetadataSyncer: SCD2 logic (called from worker thread)
├── backfill.py          # BackfillWriter: HA sqlite read + gap fill logic
├── schema.py            # DDL setup (unchanged from v0.3.6)
└── errors.py            # Error classification: transient vs data vs code bugs
```

### Structure Rationale

- **buffer.py:** Separates the thread-safe hand-off point from both the event handler and the writer, making the concurrency boundary explicit.
- **worker.py:** Owns the `threading.Lock` and the single psycopg3 connection; nothing else touches the DB connection.
- **backfill.py:** Isolated because HA sqlite access has its own executor contract (must go through `get_instance(hass).async_add_executor_job`).
- **errors.py:** Centralises the three error classes so `worker.py` dispatches without inline conditionals.

## Architectural Patterns

### Pattern 1: Async-to-Thread Hand-off via Bounded Queue

**What:** The HA event loop `@callback` appends to a `collections.deque(maxlen=10000)`. The worker thread drains it. No async/await crosses the thread boundary.

**When to use:** Any time HA event loop must produce data consumed by a blocking worker.

**Trade-offs:** Drop-oldest on overflow loses recent events (recoverable from HA sqlite backfill). No back-pressure to HA event loop — intentional, as blocking the event loop is worse than dropping.

### Pattern 2: Single write_lock with Chunked Backfill

**What:** A single `threading.Lock` serialises flush writes and backfill writes. Backfill processes results in chunks (e.g. 500 rows per chunk) and releases then re-acquires the lock between chunks. Flush acquires lock for each 5 s batch.

**When to use:** When two write paths must not overlap but neither can hold the lock indefinitely.

**Trade-offs:**

- No deadlock risk with a single non-reentrant lock, as long as backfill never calls flush and flush never calls backfill. Both paths acquire `write_lock` only at the DB-write site — no nested acquisition, no circular dependency.
- Backfill interleave: releasing the lock between chunks guarantees flush can run mid-backfill (within 500-row latency). This is the correct design per the PROJECT.md requirement.
- Flush call sites: flush is triggered by the timer callback (event loop posts a message to the worker queue) or a size threshold. Never called from backfill code — eliminates the nested-lock deadlock scenario entirely.

**Deadlock scenario that must be avoided:** If backfill held `write_lock` and called a function that also tried to acquire `write_lock` on the same thread, it would deadlock. Prevention: `write_lock` is acquired only in `FlushWriter._do_flush()` and `BackfillWriter._do_chunk()`. Neither calls the other. Use `RLock` as a defensive belt-and-braces if accidental re-entry ever becomes a concern.

### Pattern 3: Backfill via HA Recorder Executor (Not Direct SQLite)

**What:** The backfill path calls `get_instance(hass).async_add_executor_job(history.state_changes_during_period, hass, start, end, entity_id, ...)` from an async coroutine on the HA event loop, then delivers results to the worker thread via a `queue.Queue`.

**When to use:** Any read from HA's SQLite recorder in HA 2026+.

**Trade-offs:**

- `recorder.get_instance(hass).async_add_executor_job()` routes the call through the recorder's dedicated `DBInterruptibleThreadPoolExecutor` — verified in `core.py:348-352`. This avoids locking SQLite from our worker thread, keeps reads on the recorder's DB executor as HA expects, and prevents HA from logging "integration accesses the database without the database executor".
- `session_scope(hass=hass, read_only=True)` inside `state_changes_during_period()` uses `scoped_session` (verified in `core.py:1436`) — thread-local sessions, so calling from the recorder executor is safe.
- SQLite WAL mode: HA recorder uses WAL (`-wal`/`-shm` files confirmed by community). WAL allows concurrent readers with no writer blocking. A read-only `session_scope` is safe to run concurrently with HA recorder writes.
- Direct SQLite reads (bypassing the API) are risky: tools holding a read lock can cause HA to detect corruption and re-create the database (documented in HA GitHub issue #23276). Stay on the API.

## Data Flow

### State Ingestion Flow

```
HA state_changed event
    ↓ (@callback, event loop thread)
entity_filter check  →  drop if excluded
    ↓
StateBuffer.append()  →  drop-oldest + warning if full
    ↓ (worker thread polls buffer, or timer fires)
FlushWriter acquires write_lock
    ↓
psycopg3 executemany → states hypertable
    ↓
write_lock released
```

### Backfill Flow

```
Startup / DB-healthy transition detected (async, event loop)
    ↓
async: await get_instance(hass).async_add_executor_job(
    history.state_changes_during_period, hass,
    watermark_ts, cutoff_ts, entity_id,
    include_start_time_state=False
)  ← runs in recorder's DB executor thread
    ↓
results Dict[entity_id, List[State]] → chunk into 500-row batches
    ↓
post each chunk to worker_queue
    ↓ (worker thread)
BackfillWriter.write_chunk():
    acquire write_lock
    executemany (INSERT ... ON CONFLICT (last_updated, entity_id) DO NOTHING)
    release write_lock
    ↓
repeat until all chunks exhausted
```

### SCD2 Metadata Recovery Flow

```
DB-healthy transition (async, event loop)
    ↓
post "METADATA_SNAPSHOT" task to worker_queue
    ↓ (worker thread)
MetadataSyncer.full_snapshot():
    for each entity/device/area/label:
        WHERE NOT EXISTS guard → INSERT if no open row
    (idempotent — safe to re-run)
```

## Group A — TimescaleDB Unique Constraints: Findings

### A1: UNIQUE (last_updated, entity_id) on states hypertable

**ALLOWED.** TimescaleDB requires all partitioning columns to be present in any unique index. `states` is partitioned by `last_updated` (timestamptz). `UNIQUE (last_updated, entity_id)` includes the partitioning column — this satisfies the requirement.

Source: TimescaleDB docs (tigerdata.com/docs/use-timescale/latest/hypertables/hypertables-and-unique-indexes): "When you create a unique index, it must contain all the partitioning columns of the hypertable."

**Confidence:** HIGH (verified against current official docs).

### A2: INSERT ... ON CONFLICT DO NOTHING as dedup

**WORKS, with caveats on syntax and compressed chunks.**

Syntax: must use column list `ON CONFLICT (last_updated, entity_id) DO NOTHING` — NOT a constraint name reference (`ON CONFLICT ON CONSTRAINT ...`). TimescaleDB hypertables do not support constraint-name syntax (issue #1094).

Compressed chunk bug: TimescaleDB ≤ 2.17.2 had a bug (#7672) where `ON CONFLICT DO NOTHING` on compressed chunks aborted the entire batch at the first conflict instead of skipping and continuing. **Fixed in 2.18.1 (released 2025-02-10).** The HA TimescaleDB add-on should be 2.18.1+ for reliable batch dedup against compressed chunks.

**Confidence:** HIGH (fix version confirmed from GitHub release tag 2.18.1).

### A3: Index space + insert cost for unique constraint

Adding `UNIQUE (last_updated, entity_id)` creates a B-tree index across all chunks. Expected impact:

- **Storage:** Approximately 1 B-tree entry per row. For states with `compress_segmentby = 'entity_id'`, the unique index is a secondary index alongside the existing `idx_states_entity_time (entity_id, last_updated DESC)`. Extra storage is roughly proportional to the existing index.
- **Insert cost:** Secondary index maintenance adds 20–40% lower throughput vs no secondary index at high write rates (MEDIUM confidence — from benchmark articles, not TimescaleDB-official). For the HA use-case (100–500 states/s peak, batched 200-row inserts), this is negligible.
- **Compression interaction:** `compress_segmentby = 'entity_id'` is already set. `entity_id` is in the unique constraint, which avoids the decompression-to-verify-uniqueness worst case (issue #5892 warning: if a unique index column is not in `segmentby` or `orderby`, PostgreSQL must decompress to check uniqueness). Since `entity_id` is the segmentby column, the index aligns well with the compression scheme.

**Confidence:** MEDIUM (storage/cost estimates from community benchmarks; compression-alignment reasoning from TimescaleDB issue tracker).

### A4: TimescaleDB-specific caveats for unique indexes on hypertables

1. **Must include time column:** `last_updated` must be in the index — ✓ satisfied.
2. **Constraint-name ON CONFLICT syntax unsupported:** Use column list form — ✓ straightforward.
3. **Compressed chunk bug:** Fixed in 2.18.1 — check add-on version.
4. **CREATE INDEX runs per-chunk transactionally:** TimescaleDB creates unique indexes transaction-by-transaction per chunk (not in one transaction) — safe for existing data, just slower to create on a large hypertable.
5. **`last_updated` microsecond precision collision is genuine:** Two state changes for the same entity within 1 µs would cause a conflict even without corruption. HA state timestamps come from `datetime.now(timezone.utc)` at event fire time; real collisions are extremely rare but not impossible. `DO NOTHING` is the correct response (keep the first, discard the duplicate).

## Group B — HA SQLite Concurrent Access: Findings

### B5: Path to HA sqlite DB

**Confirmed:** `hass.config.path("home-assistant_v2.db")`

Source: `homeassistant/components/recorder/__init__.py:205`:
```python
DEFAULT_DB_FILE = "home-assistant_v2.db"
DEFAULT_URL = "sqlite:///{hass_config_path}"
return DEFAULT_URL.format(hass_config_path=hass.config.path(DEFAULT_DB_FILE))
```
This is stable across 2025–2026 HA versions. **Confidence:** HIGH (read from installed source).

### B6: Read-only concurrent SQLite connection while HA recorder writes

**Safe in WAL mode with important caveat.** SQLite WAL allows multiple concurrent readers alongside one writer — readers never block writers and writers never block readers. HA recorder uses WAL (confirmed by presence of `.db-wal` and `.db-shm` files in production and community reports).

**Caveat:** Opening an external connection (even read-only) using sqlite3 directly risks triggering HA's corruption detection if the connection holds a shared lock too long or if HA performs a WAL checkpoint at the wrong moment. HA has been documented to backup and recreate the database when it detects what it interprets as corruption (HA docs issue #23276).

**Conclusion:** Direct sqlite3 access is possible in WAL mode but risky. The safer path is the recorder API (see B7). **Confidence:** MEDIUM.

### B7: recorder.history API vs direct SQLite reads

**Use recorder.history API exclusively.** Direct SQL is explicitly Out of Scope per PROJECT.md and is the riskier option.

The correct call pattern (verified from HA source `core.py:348-352` and community issue #399):

```python
# From an async context (e.g. a coroutine on the HA event loop):
instance = get_instance(hass)
result = await instance.async_add_executor_job(
    history.state_changes_during_period,
    hass,
    start_time,
    end_time,
    entity_id,
    False,  # no_attributes — we need attributes for faithful backfill
    False,  # descending
    None,   # limit — no limit for backfill
    False,  # include_start_time_state=False to avoid synthetic boundary state
)
# result: Dict[str, List[State]]
```

`instance.async_add_executor_job` routes to `recorder._db_executor` (a `DBInterruptibleThreadPoolExecutor`), not to HA's generic thread pool. This is what HA expects for recorder DB access. Calling `state_changes_during_period` directly (without going through this executor) will trigger a log warning from HA's recorder thread-safety checker.

`state_changes_during_period` semantics (from source):
- Filters to `last_changed_ts == last_updated_ts OR last_changed_ts IS NULL` — returns only state-value changes, not attribute-only changes. If you need attribute changes, use `get_significant_states` with `significant_changes_only=False`.
- `include_start_time_state=False` skips the boundary-state UNION subquery — correct for gap-fill backfill where you only want real events in the window.
- Returns `unknown` and `unavailable` as real rows if they were recorded — no special filtering.
- Loads entire result set into memory (list of `Row` objects). For 24h windows with 50k+ rows, this can be several hundred MB. Chunk time windows (e.g. 1h at a time) for large backfills.

**Confidence:** HIGH (verified from installed HA source code).

### B8: Event loop thread requirement for recorder.history

**Must be called via `instance.async_add_executor_job()` from an async context.** The function itself is synchronous (uses `session_scope` which calls `get_session()` → `scoped_session` → thread-local SQLAlchemy session). It MUST run on the recorder's `_db_executor` thread pool, not on the HA event loop and not on our worker thread.

Our worker thread cannot call `state_changes_during_period` directly because:
1. SQLAlchemy `scoped_session` returns a thread-local session — a session created on the recorder executor thread is not accessible from our worker thread.
2. HA will log a thread-safety warning.

**Pattern for our architecture:** The backfill is orchestrated from an async coroutine (watchdog or setup code) using `await instance.async_add_executor_job(...)`. Results are placed on a `queue.Queue` for the worker thread to consume and write to TimescaleDB.

**Confidence:** HIGH (verified from HA source).

## Group C — Write Concurrency Model: Findings

### C9: Single threading.Lock to serialise flush + backfill

The standard pattern for two Python threads sharing a single resource:

```python
write_lock = threading.Lock()  # single instance, owned by WorkerThread

# In FlushWriter (called from worker thread's flush loop):
with write_lock:
    cursor.executemany(INSERT_SQL, batch)

# In BackfillWriter (called from worker thread's backfill loop):
for chunk in chunks:
    with write_lock:
        cursor.executemany(INSERT_SQL_CONFLICT, chunk)
    # lock released between chunks — flush can interleave here
```

Both paths run in the **same worker thread**, so `threading.Lock` is always acquired and released within a single thread. This makes the lock a serialisation mechanism, not a mutual exclusion between different threads — it is effectively a no-op for single-threaded access.

**Wait.** If both flush and backfill run in the same worker thread sequentially (flush loop + backfill in the same thread's event loop), there is no concurrent access at all and no lock is needed at the DB write site. The lock matters only if flush and backfill run in *different* threads.

**Recommended design decision:** Keep both flush and backfill in the same worker thread, running sequentially. The worker thread owns a queue; it processes flush tasks and backfill tasks from that queue one at a time. This eliminates the locking complexity entirely and avoids any deadlock risk. The `threading.Lock` described in PROJECT.md becomes a simple serialisation queue instead.

If the architecture evolves to have flush on a timer thread and backfill on the worker thread (two separate threads), then `write_lock` must be a module-level `threading.Lock` held for the duration of each DB write. With a single lock and no nested acquisition, deadlock is impossible.

**Confidence:** HIGH (Python threading stdlib semantics, no external verification needed).

### C10: Deadlock risk with per-chunk lock + mid-backfill flush

**No deadlock risk if single lock, two distinct code paths, no nesting.**

Deadlock requires a circular wait: Thread A holds lock X and waits for lock Y; Thread B holds lock Y and waits for lock X. With a single `write_lock`:
- If both flush and backfill are in the same thread (recommended): there is no concurrency at all — no deadlock possible.
- If flush and backfill are on separate threads: Thread A (flush) acquires `write_lock`, writes, releases. Thread B (backfill) acquires `write_lock` per chunk, writes, releases. Only one lock, no circular dependency — deadlock is **impossible**.

**The only deadlock scenario to guard against:** backfill code calling flush, or flush code calling backfill, while either holds `write_lock` — creating re-entrant acquisition on the same thread. Prevention: ensure flush and backfill are leaf functions (they write to DB and return; they do not call each other). If defensive re-entrancy is needed, use `threading.RLock` instead of `threading.Lock`.

**Confidence:** HIGH (formal deadlock analysis from first principles).

## Integration Points

### External Services

| Service | Integration Pattern | Notes |
|---------|---------------------|-------|
| TimescaleDB (psycopg3) | Single persistent sync connection in worker thread | Use `psycopg.connect(DSN)` with `autocommit=True` for simple inserts; explicit transactions only for SCD2 close+insert pairs |
| HA SQLite (recorder) | `get_instance(hass).async_add_executor_job(history.state_changes_during_period, ...)` from async context | Never call directly from worker thread; never use sqlite3 directly |
| HA event bus | `@callback` on event loop thread; appends to `StateBuffer` only | No DB access, no awaits, no blocking |

### Internal Boundaries

| Boundary | Communication | Notes |
|----------|---------------|-------|
| HA event loop → worker thread | `collections.deque` or `queue.Queue` append (lock-free for deque, lock-per-op for Queue) | Deque is faster; Queue gives blocking `get()` which simplifies worker sleep loop |
| async backfill orchestrator → worker thread | `queue.Queue.put(chunk)` | Backfill results chunked before posting to avoid large memory spikes |
| worker thread → HA event loop | `hass.loop.call_soon_threadsafe(callback)` | For watchdog health updates, persistent_notification triggers |

## Anti-Patterns

### Anti-Pattern 1: Calling state_changes_during_period from Worker Thread

**What people do:** Worker thread calls `history.state_changes_during_period(hass, ...)` directly to fetch backfill data.

**Why it's wrong:** HA logs a thread-safety warning; the SQLAlchemy `scoped_session` returns a thread-local session that was created for the recorder's DB executor thread, not our worker thread. Sessions are not safely shareable across threads.

**Do this instead:** `await instance.async_add_executor_job(history.state_changes_during_period, ...)` from an async coroutine; deliver results to the worker thread via `queue.Queue`.

### Anti-Pattern 2: Using ON CONFLICT ON CONSTRAINT Syntax

**What people do:** `INSERT INTO states ... ON CONFLICT ON CONSTRAINT uq_states DO NOTHING`

**Why it's wrong:** TimescaleDB hypertables do not support constraint-name conflict resolution (issue #1094). This raises a runtime error.

**Do this instead:** `INSERT INTO states ... ON CONFLICT (last_updated, entity_id) DO NOTHING`

### Anti-Pattern 3: Holding write_lock During HA SQLite Read

**What people do:** Acquire write_lock, then call the history API (which blocks on the recorder executor), then write results to TimescaleDB.

**Why it's wrong:** The history API call can take seconds for large windows. During this time flush cannot write. Worse, if the recorder executor is itself busy, deadlock is possible in extreme cases.

**Do this instead:** Fetch all backfill data first (without holding the lock), then acquire write_lock only for the DB write step.

### Anti-Pattern 4: asyncio Tasks for DB Writes

**What people do:** `hass.async_create_task(self._async_flush())` for every flush — the v0.3.6 pattern.

**Why it's wrong:** Multiple overlapping flushes can be scheduled; uncaught exceptions kill the task silently; slow DB paths block the event loop.

**Do this instead:** Move all DB writes to a dedicated `threading.Thread`; post flush signals to the thread via a queue from the event loop.

## Build Order Implications

The architecture creates a clear build dependency chain:

1. **buffer.py** — no dependencies except stdlib; build first
2. **errors.py** — no dependencies; build alongside buffer
3. **schema.py** — unchanged; carry forward from v0.3.6
4. **worker.py** — depends on buffer, errors, schema; build third
5. **ingester.py** — depends on buffer only (no longer depends on pool); simplest refactor
6. **syncer.py** — depends on worker (worker thread owns the connection); refactor to be called from worker
7. **backfill.py** — depends on worker queue, HA history API; build after worker
8. **__init__.py** — wires everything; build last

## Sources

- TimescaleDB unique index docs: https://www.tigerdata.com/docs/use-timescale/latest/hypertables/hypertables-and-unique-indexes
- TimescaleDB ON CONFLICT DO NOTHING compressed chunk bug fix 2.18.1: https://github.com/timescale/timescaledb/releases/tag/2.18.1
- TimescaleDB ON CONFLICT constraint-name unsupported: https://github.com/timescale/timescaledb/issues/1094
- TimescaleDB compress_segmentby + unique index alignment: https://github.com/timescale/timescaledb/issues/5892
- HA recorder `core.py` source (local `.venv`): `homeassistant/components/recorder/core.py:348-352,1436`
- HA recorder `history/__init__.py` source (local `.venv`): `state_changes_during_period` full implementation
- HA helpers `recorder.py` source (local `.venv`): `session_scope`, `get_instance`
- HA recorder executor pattern: https://github.com/KartoffelToby/better_thermostat/issues/399
- SQLite WAL concurrent read safety: https://sqlite.org/wal.html
- HA SQLite external access risk: https://github.com/home-assistant/home-assistant.io/issues/23276

---
*Architecture research for: ha-timescaledb-recorder v1.1 robust ingestion*
*Researched: 2026-04-19*
