# ha-timescaledb-recorder

## What This Is

A custom Home Assistant integration that captures HA state changes and registry metadata (entities, devices, areas, labels) into a TimescaleDB instance. State data lands in the `ha_states` hypertable; metadata is tracked via four SCD2 dimension tables with full temporal history. Deployed as a HACS custom component against a companion TimescaleDB add-on.

v1.1 shipped 2026-04-23. All DB writes run in a dedicated OS thread (psycopg3); a bounded RAM buffer + file-backed persistent queue provides zero data loss up to 10+ days DB outage; HA sqlite backfill fills gaps on startup and recovery; a watchdog task auto-restarts dead workers and fires structured notifications; 5 repair issues surface ongoing problems in the HA Repairs UI and auto-clear when conditions resolve.

## Core Value

State changes and registry metadata land in TimescaleDB reliably even when the database is temporarily unavailable — HA continues to function normally regardless.

## Requirements

### Validated

These capabilities were confirmed shipped in v1.1:

- ✓ State changes written to `ha_states` hypertable (buffered/batched, configurable batch size + flush interval) — existing pre-v1.1
- ✓ Entity filter (include/exclude by entity_id) applied before buffering — existing pre-v1.1
- ✓ Registry metadata (entities, devices, areas, labels) tracked via SCD2 dimension tables — existing pre-v1.1
- ✓ Full SCD2 temporal history: close-and-insert on update, `valid_to IS NULL` marks current row — existing pre-v1.1
- ✓ SCD2 change detection: no-op on unchanged fields — existing pre-v1.1
- ✓ Idempotent schema setup on every startup — existing pre-v1.1
- ✓ TimescaleDB connection via DSN (stored in HA config entry) — existing pre-v1.1
- ✓ Thread-worker isolation: all DB writes in dedicated OS thread; HA event loop never blocked or crashed — v1.1
- ✓ psycopg3 (`psycopg[binary]==3.3.3`) replaces asyncpg — v1.1
- ✓ Bounded RAM buffer: 10,000-record cap, drop-oldest, warning log + repair issue on overflow — v1.1
- ✓ File-backed persistent queue: crash-safe offline durability, zero data loss up to 10+ days DB outage — v1.1
- ✓ HA sqlite backfill: on startup and DB-healthy transition via `recorder.history` API, per-entity, chunked — v1.1
- ✓ Metadata recovery: full registry re-snapshot on DB recovery (idempotent `WHERE NOT EXISTS`) — v1.1
- ✓ Retry decorator: transient DB errors retried silently with backoff; on_recovery + on_sustained_fail hooks — v1.1
- ✓ Watchdog: async task checks thread liveness, auto-restarts dead workers, fires structured `persistent_notification` — v1.1
- ✓ Observability: 5 repair issues (db_unreachable, buffer_dropping, recorder_disabled, states_worker_stalled, meta_worker_stalled); all auto-clear — v1.1
- ✓ `persistent_notification` for one-shot events: worker crash+restart, orchestrator crash, backfill gap — v1.1
- ✓ `strings.json` "issues" section with title+description for all 5 repair issue translation_keys — v1.1

### Active

Next milestone requirements (to be defined in `/gsd-new-milestone`):

- [ ] Schema migration infrastructure (SCHEMA-01): schema_version table + migration registry for future ALTER TABLE
- [ ] SSL/TLS option for TimescaleDB connection (SSL-01)
- [ ] Sensor entity for integration health: sensor.timescaledb_recorder_status (SENSOR-01)
- [ ] Entity filter configurable via HA options UI without re-adding integration (FILTER-01)

### Out of Scope

- Per-event WAL (own fsync'd append log) — overkill; HA sqlite is the safety net
- Multiprocessing isolation — overkill for I/O-bound low-rate workload
- Direct sqlite3 reads (bypassing recorder.history API) — risks HA corruption detection; only if API proves too slow
- CPU parallelism — I/O bound, unnecessary
- SQLERR-03/04 per-row fallback + critical-log branching — dropped per D-01; unified retry+stall is the recovery story

## Context

**Current state (v1.1, 2026-04-23):**
- Source: 3,669 LOC Python (custom_components/ha_timescaledb_recorder/)
- Tests: 5,565 LOC Python, 194 passing tests
- Tech stack: Python 3.14+, psycopg[binary]==3.3.3, HA 2026.3.4, TimescaleDB ≥ 2.18.1

**Architecture (post-v1.1):**
- `states_worker.py`: `TimescaledbStateRecorderThread` — dequeues from `OverflowQueue`, flushes to TimescaleDB via psycopg3, retries on transient errors, triggers backfill on recovery
- `meta_worker.py`: `TimescaledbMetaRecorderThread` — dequeues from `PersistentQueue`, runs SCD2 upserts
- `backfill.py`: `backfill_orchestrator` — async coroutine, fetches from HA sqlite via `recorder.history` API per entity, writes chunks via shared queue
- `overflow_queue.py`: bounded RAM buffer, drop-oldest eviction
- `persistent_queue.py`: file-backed FIFO, JSON-lines, crash-safe
- `retry.py`: `retry_until_success` decorator with backoff, on_recovery, on_sustained_fail hooks
- `watchdog.py`: `watchdog_loop` async task, spawn factories, `WATCHDOG_INTERVAL_S=10s`
- `issues.py`: create/clear helpers for all 5 repair issues
- `notifications.py`: `notify_watchdog_recovery`, `notify_backfill_gap`
- `ingester.py` / `syncer.py`: thin event-bus relays (no business logic)

**Key v1.1 discoveries:**
- `state_changes_during_period(entity_id=None)` raises `ValueError` — must iterate per entity
- `run_coroutine_threadsafe().result()` deadlocks on HA shutdown — use sentinel + `async_add_executor_job(thread.join)`
- TimescaleDB ≤ 2.17.2 `ON CONFLICT DO NOTHING` bug aborts full batch — version gate required (user has ≥ 2.18.1)
- `hass.async_*` not thread-safe from worker — use `hass.add_job` (fire-and-forget) or `run_coroutine_threadsafe`

## Constraints

- **Tech stack**: Python 3.14+, uv, HA 2026.x custom component model — no HTTP, no inter-process; all data via event bus
- **HA model**: Event handlers must be sync `@callback`; no blocking the event loop; DB writes confined to worker thread
- **Schema**: `ha_states` hypertable and four `dim_*` tables remain schema-identical to v0.3.6 (no migration in v1.1)
- **Config surface**: Same user-facing options (DSN, batch_size, flush_interval, compress_after_hours, chunk_interval_days)
- **HACS distribution**: All runtime dependencies declared in `manifest.json`; no system packages
- **HA recorder dependency**: Backfill requires HA recorder enabled with sqlite backend and retention ≥ expected outage window
- **TimescaleDB version**: ≥ 2.18.1 required for safe `ON CONFLICT DO NOTHING` dedup

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Threaded worker (not async-in-event-loop) | Matches HA recorder pattern; contains crashes; enables sync psycopg3; no worker event loop needed | ✓ Good — crash isolation proven in Phase 3 watchdog tests |
| psycopg3 (`psycopg[binary]`) over asyncpg | Cleaner in threaded context; no async-in-thread complexity; handles JSONB/arrays natively | ✓ Good — migration smooth; placeholder syntax ($N→%s) was only friction |
| Drop-oldest buffer eviction at 10k records | ~6 MB RAM, ~15–20 min coverage; dropped events recoverable from HA sqlite; avoids OOM | ✓ Good |
| HA sqlite via `recorder.history` API (not direct SQL) | Stable-ish public interface; avoids sqlite locking issues | ✓ Good — per-entity iteration constraint discovered and handled |
| Single `queue.Queue` (no `threading.Lock`) | Flush and backfill sequential in same worker thread; mutex unnecessary | ✓ Good — simplified Phase 2 significantly |
| Bare `psycopg.connect()` (not pool) | Simpler lifecycle; reconnect via watchdog restart path | ✓ Good |
| Flush interval = 5 seconds (match HA recorder) | Accept 5s crash window (negligible vs 30s+ HA restart downtime) | ✓ Good |
| Unified retry path (D-01): drop SQLERR-03/04 | Per-row fallback + critical-log branching = unnecessary complexity; stall notification after N=5 is cleaner recovery story | ✓ Good — 2 requirements dropped, implementation simpler |
| TimescaleDB ≥ 2.18.1 version gate | ≤ 2.17.2 bug: `ON CONFLICT DO NOTHING` aborts full batch on first conflict | ✓ Good — user confirmed ≥ 2.18.1 |
| `hasattr` guard for `async_wait_recorder` | Forward-compat: if HA removes the API, recorder_disabled issue won't auto-clear but user can dismiss manually | ⚠ Revisit — monitor HA API stability |

## Evolution

**After each milestone** (via `/gsd-complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-04-23 after v1.1 milestone*
