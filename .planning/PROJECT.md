# ha-timescaledb-recorder

## What This Is

A custom Home Assistant integration that captures HA state changes and registry metadata (entities, devices, areas, labels) into a TimescaleDB instance. State data lands in the `ha_states` hypertable; metadata is tracked via four SCD2 dimension tables with full temporal history. Deployed as a HACS custom component against a companion TimescaleDB add-on.

Currently at v1.1.0 (phase 01 complete). Phase 01 delivered the thread-worker foundation: psycopg3 replaces asyncpg; all DB writes run in a dedicated OS thread via `DbWorker`; the HA event loop can no longer be blocked or crashed by DB errors. Phases 02-03 deliver durability and hardening.

## Core Value

State changes and registry metadata land in TimescaleDB reliably even when the database is temporarily unavailable — HA continues to function normally regardless.

## Requirements

### Validated

These capabilities exist in v0.3.6 and are the baseline.

- ✓ State changes written to `ha_states` hypertable (buffered/batched, configurable batch size + flush interval) — existing
- ✓ Entity filter (include/exclude by entity_id) applied before buffering — existing
- ✓ Registry metadata (entities, devices, areas, labels) tracked via SCD2 dimension tables — existing
- ✓ Full SCD2 temporal history: close-and-insert on update, `valid_to IS NULL` marks current row — existing
- ✓ SCD2 change detection: no-op on unchanged fields (compares typed columns, ignores `modified_at` in `extra`) — existing
- ✓ Idempotent schema setup on every startup (hypertable, compression policy, indexes, dimension tables) — existing
- ✓ Configurable: batch size, flush interval, compress_after_hours, chunk_interval_days — existing
- ✓ TimescaleDB connection via DSN (stored in HA config entry) — existing

### Active

v1.1 — robust ingestion milestone.

- [x] Thread-worker isolation: StateIngester and MetadataSyncer move to a dedicated OS thread; HA event loop never blocked or crashed by DB errors — Validated in Phase 01
- [x] psycopg3 (`psycopg[binary]`) replaces asyncpg — sync driver, cleaner in threaded context — Validated in Phase 01
- [ ] Bounded RAM buffer: 10,000-record cap, drop-oldest with warning log + repair issue on overflow
- [ ] HA sqlite backfill: triggered on startup and on DB-healthy transition; fills gaps via `recorder.history.state_changes_during_period`
- [ ] Write mutex: single `threading.Lock` serializing flush and backfill; backfill releases per-chunk to allow flush interleaving
- [ ] Metadata recovery: full registry re-snapshot on DB recovery (idempotent `WHERE NOT EXISTS`); accepts loss of change-time precision during outage
- [ ] Watchdog: async task checks worker thread liveness, restarts thread + fires persistent notification on death
- [ ] Observability: `persistent_notification` for one-shot events; `issue_registry` repair issues for ongoing conditions (DB unreachable >5 min, buffer dropping, HA recorder disabled)
- [ ] SQL error granularity: distinguish transient errors (retry), data errors (per-row fallback + skip), and code bugs (critical log + repair issue)

### Out of Scope

- Per-event WAL (own fsync'd append log) — overkill; HA sqlite provides the safety net
- Multiprocessing isolation — overkill for I/O-bound low-rate workload
- SSL/TLS option for TimescaleDB connection — security debt acknowledged, deferred past v1.1
- Reading HA sqlite via direct SQL — use public `recorder.history` API; direct SQL only if API proves too slow (defer)
- CPU parallelism — I/O bound, unnecessary

## Context

**Existing architecture (v0.3.6):** Fully async, runs inside HA's event loop. `StateIngester` holds an in-memory list buffer, flushes via `asyncpg.Pool.executemany`. `MetadataSyncer` runs SCD2 writes per registry event via `hass.async_create_task`. Both share a single asyncpg pool (min=1, max=3).

**Identified robustness gaps (from exploration, 2026-04-19):**
1. Blast radius: uncaught exception or slow DB path affects HA core
2. Unbounded buffer: grows without limit during DB outage — OOM risk
3. Metadata event loss: MetadataSyncer has no buffer; registry events lost on DB unavailability
4. Concurrent flushes: `_async_flush()` can be scheduled multiple times with unclear serialization
5. Re-queue brittleness: snapshot-then-readd pattern on flush failure is error-prone
6. SQL error granularity: any `PostgresError` drops entire batch; transient errors not distinguished from data errors

**Durability targets:**
- State changes: level B (zero loss during DB outage up to ~20 min via RAM buffer; up to 10 days via HA sqlite backfill)
- Metadata: level A for final state, level C for change-time precision during outage

**HA sqlite as safety net:** HA recorder writes to `home-assistant_v2.db` (WAL, ~10-day retention). Backfill watermark: `SELECT MAX(last_updated) FROM ha_states`. Gap coverage: `recorder.history.state_changes_during_period(hass, checkpoint, cutoff, include_start_time_state=False)`.

**Schema:** Unchanged from v0.3.6 — no migration needed. Same user-facing config options. Major version bump (v1.1) for GSD milestone alignment only.

**Deployment:** HACS custom component. `manifest.json` declares `requirements: ["psycopg[binary]==..."]` (replacing `asyncpg==0.31.0`). HA installs via pip on load.

**Open research questions before planning:** psycopg3 HA compatibility, `state_changes_during_period` semantics and thread safety, HA 2026 notification API shapes, TimescaleDB unique constraint on hypertables, sqlite WAL concurrent read safety. See `.planning/research/questions.md`.

## Constraints

- **Tech stack**: Python 3.14+, uv, HA 2026.x custom component model — no HTTP, no inter-process; all data via event bus
- **HA model**: Event handlers must be sync `@callback`; no blocking the event loop; DB writes confined to worker thread
- **Schema**: `ha_states` hypertable and four `dim_*` tables must remain schema-identical to v0.3.6
- **Config surface**: Same user-facing options (DSN, batch_size, flush_interval, compress_after_hours, chunk_interval_days)
- **HACS distribution**: All runtime dependencies declared in `manifest.json`; no system packages
- **HA recorder dependency**: Backfill requires HA recorder enabled with sqlite backend and retention ≥ expected outage window

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Threaded worker (not async-in-event-loop) | Matches HA recorder pattern; contains crashes; enables sync psycopg3; no worker event loop needed | — Pending |
| psycopg3 (`psycopg[binary]`) over asyncpg | Cleaner in threaded context; no async-in-thread complexity; handles JSONB/arrays; HA doesn't bundle a PG driver | — Pending |
| Drop-oldest buffer eviction at 10k records | ~6 MB RAM, ~15–20 min coverage; dropped events recoverable from HA sqlite; avoids OOM | — Pending |
| HA sqlite via `recorder.history` API (not direct SQL) | Stable-ish public interface; avoids sqlite locking issues; direct SQL only if API too slow | — Pending |
| Write mutex (single `threading.Lock`) | Flush and backfill don't overlap in time ranges; mutex is safety net, not coordination mechanism | — Pending |
| Re-snapshot metadata on recovery (not event replay) | Registry events are infrequent; re-snapshot is idempotent; acceptable to lose change-time precision | — Pending |
| Flush interval = 5 seconds (match HA recorder) | Accept 5s crash window (negligible vs 30s+ HA restart downtime); consistent with HA's own durability class | — Pending |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd-transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd-complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-04-19 after initialization*
