---
title: Robust ingestion architecture — design decisions
date: 2026-04-19
context: Exploration of durability, isolation, and recovery for TimescaleDB recorder
---

# Robust ingestion architecture

Design decisions from exploration session on 2026-04-19. Captures rationale for upcoming refactor milestone.

## Problem statement

Current architecture has multiple robustness gaps identified in exploration:

1. **Blast radius**: recorder runs in HA event loop. Uncaught exception or slow path affects HA core.
2. **Unbounded buffer**: in-memory list grows without limit during TimescaleDB outage — OOM risk.
3. **Metadata event loss**: MetadataSyncer has no buffer. Registry events lost on DB unavailability.
4. **Concurrent flushes**: `_async_flush()` can be scheduled multiple times (batch-size trigger + timer) while a prior flush is still awaiting DB connection. Unclear serialization semantics.
5. **Re-queue brittleness**: snapshot-then-readd pattern on flush failure is error-prone.
6. **SQL error granularity**: any `PostgresError` drops entire batch. Transient errors (lock timeout, deadlock) should be distinguished from data errors. Data errors should identify the offending row and skip only that row.

User's durability targets:
- State changes: level B (zero loss during DB outage), prefer A if cost acceptable
- Metadata: level A (zero loss ever, including compound failure)

## Key research findings

HA's built-in recorder durability profile (researched via `homeassistant.components.recorder`):

- Runs in own OS thread (`Recorder(threading.Thread)` at `core.py:148`)
- Events pushed via `queue.SimpleQueue` from event bus callback
- Commit interval default = **5 seconds** (`__init__.py:59`)
- SQLite opened with `journal_mode=WAL`, `synchronous=NORMAL`
- **Durability class = C**: up to 5s of events in RAM-only before commit. HA crash = lose those 5s.
- On DB failure: retries 10× × 3s, then detaches listener at `MAX_QUEUE_BACKLOG_MIN_VALUE=65000` events → drops silently, HA keeps running
- Schema unstable (v53 current, frequent migrations v36→37→38→49→52→53)
- Default `purge_keep_days=10`, nightly purge at 04:12
- `homeassistant.components.recorder.history` module exposes raw state changes via public API (e.g. `state_changes_during_period`) — stable-ish interface

**Implication**: HA sqlite is NOT a WAL (events only land there after commit). But it IS a durable-enough 10-day history store that can serve as backfill source for our recovery path.

## Chosen architecture

### Isolation: dedicated worker thread (sync code)

- Replace async-in-event-loop with **threaded worker** (matches HA recorder pattern)
- HA event bus callback (sync `@callback`) enqueues to `queue.SimpleQueue` — cheap, never blocks event loop
- Worker thread owns: PG pool, RAM buffer, flush timer, backfill logic, SCD2 SQL execution
- Crash in worker contained to thread. HA survives.
- **Sync code inside worker** (drop asyncpg) — cleaner than async-in-thread. No need for worker event loop.
- **Driver**: `psycopg[binary]` (psycopg3) — modern, active, handles JSONB/arrays well. HA doesn't pull PG driver by default (sqlite is HA default), so no conflict risk for typical HA installs.

### Durability: bounded RAM buffer + HA sqlite as backfill

- RAM buffer bounded at ~10,000 records (≈15–20 minutes at typical 11 events/s)
- Flush interval = **5 seconds** (match HA recorder class — accept 5s crash window, which is negligible vs 30s+ restart downtime)
- On TimescaleDB unavailable: keep events in RAM, retry every 5s
- At buffer threshold: **drop oldest**, log warning, raise repair issue. Dropped events are recoverable from HA sqlite during next backfill.
- On TimescaleDB recovery or integration startup: **backfill from HA sqlite** covers gaps
- RAM alone gives us level B for short outages (<~20 min). HA sqlite extends that to level B for outages up to 10 days (HA retention default).
- True level A for state changes would require own WAL + per-event fsync → rejected (not worth complexity given HA sqlite safety net)

### Metadata path (SCD2 dim tables)

- Same threaded worker + RAM buffer pattern as state changes
- On TimescaleDB recovery / integration startup: **re-snapshot full registry** via existing `_async_snapshot` pattern
- Snapshot SQL is idempotent (`WHERE NOT EXISTS`) — safe to re-run
- Captures current registry state, loses precise time-of-change during outage (acceptable — registries change infrequently)
- **Level A target relaxed to "A for final state, C for change-time precision"** during outages

### Recovery mechanism (unified startup + runtime)

Unified procedure triggered on:
- Integration startup (`async_start`) — always (outage may have spanned restart)
- Runtime flush failure → success transition (detected via `_db_healthy` flag)

```
async def async_start():
    # 1. Start worker thread + register event listener IMMEDIATELY
    self._worker.start()
    self._cancel_listener = hass.bus.async_listen(...)
    # 2. Schedule backfill in background — do NOT await
    self._backfill_task = hass.async_create_task(self._async_trigger_backfill())
```

Backfill procedure:
1. `checkpoint = SELECT MAX(last_updated) FROM ha_states` (our DB)
2. `cutoff = now()`
3. Fetch from HA sqlite: `history.state_changes_during_period(hass, checkpoint, cutoff, entity_id=None)` with `include_start_time_state=False`
4. Chunked write (e.g. 1000 rows/chunk) to avoid huge `executemany`
5. Release write lock between chunks so live flush can interleave
6. Re-snapshot metadata registries (idempotent)

Use `last_updated` (not `last_changed`) as watermark — native hypertable time column, captures both state and attribute changes.

### Concurrency: mutex between flush and backfill

- Single `Lock` guards all TimescaleDB writes (flush + backfill)
- Backfill acquires lock per-chunk, releases between chunks → flush gets fair turn, RAM doesn't explode
- Flush holds lock during full batch write
- No overlap in time ranges: backfill covers `[checkpoint, cutoff]`, flush covers `> cutoff` — mutex is safety net

### Error handling — SQL error granularity

- `PostgresConnectionError | OSError`: transient, keep rows, retry on next flush
- `DeadlockDetected | LockNotAvailable | SerializationError`: treat as transient, retry
- `DataError | IntegrityError`: isolate offending row via single-row retry, log + skip that row, continue batch
- `ProgrammingError | SyntaxError`: log as critical (code bug or schema drift), raise repair issue, do NOT retry

Implementation: on `executemany` failure, fall back to single-row loop to identify offending row(s). Keep flush progress (partial success OK — TimescaleDB atomic per-statement).

### Observability & notifications

- **persistent_notification**: one-shot events (worker died + restarted, backfill failed)
- **issue_registry repair issues**: ongoing conditions (TimescaleDB unreachable > 5 min, buffer dropping records, HA recorder disabled, HA version untested)
- **Logs**: detailed transient errors + first-failed-row debug output
- **HA-side watchdog**: async task periodically checks `worker_thread.is_alive()`, restarts + notifies if dead

## Hard dependencies / user documentation

Must document:

1. HA recorder must be enabled with sqlite backend (or we can't backfill)
2. HA recorder retention ≥ expected worst-case outage (default 10 days; user should not lower below TimescaleDB MTTR)
3. TimescaleDB target hardware: SSD/NVMe strongly recommended (per-event fsync negligible there; SD card acceptable since we batch to 5s anyway)
4. If user disables HA recorder → integration warns + falls back to RAM-only (level C) — surfaced via repair issue

## Out of scope

- Per-event WAL (own fsync'd append log) — rejected, overkill given HA sqlite safety net
- Multiprocessing isolation — rejected, overkill for I/O-bound low-rate workload
- CPU parallelism — not needed (I/O bound)
- Reading HA sqlite via direct SQL — use public `recorder.history` API; direct SQL only if API proves too slow (defer)

## Sizing reference

From user-provided measurements (HA export):
- 1 hour raw `ha_states` CSV = 10 MB, 40,000 records
- In Python tuple form ≈ 2-3× CSV size ≈ 25 MB/h
- Buffer sizing decision: 10,000 records = ~6 MB RAM = ~15–20 min at typical rate

## Phase breakdown (tentative)

1. Thread-worker refactor + psycopg3 migration (drop asyncpg)
2. Bounded RAM buffer + drop-oldest + per-condition repair issues
3. HA sqlite backfill (startup + runtime recovery) + write mutex
4. Metadata re-snapshot on recovery
5. Watchdog + persistent_notification + issue_registry wiring
6. SQL error granularity (per-row fallback on data errors)

Phases may merge during planning.
