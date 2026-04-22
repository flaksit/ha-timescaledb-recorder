# Roadmap: ha-timescaledb-recorder v1.1 — Robust Ingestion

## Overview

The v1.1 milestone refactors the write path from an async, event-loop-resident asyncpg design to a dedicated OS worker thread with a synchronous psycopg3 connection. Three phases deliver the full durability story: first the correct thread/async boundary (everything else depends on this), then the durability mechanics (bounded buffer, backfill, metadata recovery, transient error handling), then hardening with per-row error isolation and full observability.

## Milestones

- 🚧 **v1.1 Robust Ingestion** - Phases 1-3 (in progress)

## Phases

- [ ] **Phase 1: Thread Worker Foundation** - Correct async/thread boundary, psycopg3 wiring, flush loop, graceful shutdown
- [ ] **Phase 2: Durability Story** - Bounded buffer, HA sqlite backfill, metadata recovery, transient SQL error handling
- [ ] **Phase 3: Hardening and Observability** - Watchdog restart, per-row error isolation, repair issues, persistent notifications, strings.json

## Phase Details

### Phase 1: Thread Worker Foundation
**Goal**: The integration runs all DB writes in a dedicated OS thread; the HA event loop can never be blocked or crashed by DB errors; the thread starts, runs, and stops correctly across the full HA lifecycle
**Depends on**: Nothing (first phase)
**Requirements**: WORK-01, WORK-02, WORK-03, WORK-04, WORK-05, BUF-03, WATCH-02
**Success Criteria** (what must be TRUE):
  1. HA starts and stops normally with the integration loaded; no event loop blocking occurs even when TimescaleDB is unreachable
  2. State change events are enqueued from the HA `@callback` handler without any await or lock; the worker thread dequeues and owns all psycopg3 writes
  3. The worker thread is shut down via a sentinel value in the queue; HA's stop handler awaits `async_add_executor_job(thread.join)` and never deadlocks
  4. psycopg3 `Jsonb()` wrappers and `TEXT[]` list adaptation produce correct inserts for the `ha_states` hypertable columns
  5. All `hass.async_*` calls from the worker use `hass.add_job` or `run_coroutine_threadsafe`; no unsafe cross-thread calls
**Plans**: 8 plans

Plans:
- [x] 01-01-PLAN.md — Migrate const.py SQL placeholders $N→%s; add SELECT_*_CURRENT_SQL constants
- [x] 01-02-PLAN.md — Convert schema.py to sync sync_setup_schema(conn, ...)
- [x] 01-03-PLAN.md — Create worker.py: DbWorker, StateRow, MetaCommand, _STOP
- [x] 01-04-PLAN.md — Refactor ingester.py to thin queue relay (StateRow enqueue)
- [x] 01-05-PLAN.md — Refactor syncer.py to thin relay + sync change-detection helpers
- [x] 01-06-PLAN.md — Wire __init__.py lifecycle; update manifest.json to psycopg[binary]==3.3.3
- [x] 01-07-PLAN.md — Create test_worker.py; add mock_psycopg_conn to conftest.py
- [x] 01-08-PLAN.md — Rewrite test_ingester.py, test_syncer.py, test_schema.py

### Phase 2: Durability Story
**Goal**: The integration survives DB outages up to ~10 days without data loss; RAM buffer is bounded; gaps are filled from HA sqlite on startup and recovery; transient SQL errors are retried silently
**Depends on**: Phase 1
**Requirements**: BUF-01, BUF-02, BACK-01, BACK-02, BACK-03, BACK-04, BACK-05, BACK-06, META-01, META-02, META-03, SQLERR-01, SQLERR-02, SQLERR-05
**Success Criteria** (what must be TRUE):
  1. Buffer cap holds at 10,000 records; oldest records are dropped (not newest) when the cap is reached; a warning log is emitted with the drop count
  2. On startup and on DB-healthy transition, backfill fetches state changes from HA sqlite since `MAX(last_updated)` in `ha_states`, applies the entity filter, and writes in chunks of ≤1000 rows; backfill does not block HA startup
  3. Backfill uses per-entity `state_changes_during_period` calls (never `entity_id=None`) dispatched via `recorder_instance.async_add_executor_job`
  4. On DB-healthy transition, all four registry tables (entities, devices, areas, labels) are re-snapshotted idempotently via `WHERE NOT EXISTS`; current state is always correct after recovery
  5. `PostgresConnectionError`, `OSError`, `DeadlockDetected`, and serialization errors keep rows in the buffer and retry on next flush cycle without logging errors
  6. `ON CONFLICT (last_updated, entity_id) DO NOTHING` dedup is only used when TimescaleDB ≥ 2.18.1 is confirmed; a startup check or warning gate enforces this
**Plans**: 13 plans

Plans:
- [x] 02-01-PLAN.md — Add Phase 2 tunables + CREATE_UNIQUE_INDEX_SQL + SELECT_WATERMARK/OPEN_ENTITIES SQL to const.py; execute unique index in schema.py
- [x] 02-02-PLAN.md — Create issues.py (create/clear buffer_dropping) + strings.json "issues" section
- [x] 02-03-PLAN.md — Create retry.py with retry_until_success decorator (D-07)
- [x] 02-04-PLAN.md — Create overflow_queue.py with OverflowQueue drop-newest-on-full (D-02 + D-11)
- [x] 02-05-PLAN.md — Create persistent_queue.py with file-backed FIFO (D-03)
- [x] 02-06-PLAN.md — Retire DbWorker/MetaCommand from worker.py; create states_worker.py with TimescaledbStateRecorderThread (D-04 + D-06 + D-15)
- [x] 02-07-PLAN.md — Create meta_worker.py with TimescaledbMetaRecorderThread dispatching via Phase 1 SCD2 helpers (D-05)
- [x] 02-08-PLAN.md — Create backfill.py with backfill_orchestrator + _fetch_slice_raw (D-08)
- [x] 02-09-PLAN.md — Retarget ingester.py to OverflowQueue; retarget syncer.py to PersistentQueue with JSON-safe dicts (D-15-b/c)
- [x] 02-10-PLAN.md — Rewrite __init__.py for D-12 8-step startup + D-13 6-step shutdown + initial registry backfill + overflow watcher
- [x] 02-11-PLAN.md — Unit tests for Wave 1 primitives (overflow_queue, persistent_queue, retry, issues)
- [x] 02-12-PLAN.md — Unit tests for Wave 2/3 services (states_worker, meta_worker, backfill)
- [x] 02-13-PLAN.md — Retarget Phase 1 tests (test_worker, test_ingester, test_syncer, test_schema, test_init); full-suite green check

### Phase 3: Hardening and Observability
**Goal**: The integration surfaces ongoing problems in the HA Repairs UI and fires one-shot notifications for critical events; data errors are isolated per-row and never drop an entire batch; code bugs produce critical logs and repair issues
**Depends on**: Phase 2
**Requirements**: WATCH-01, WATCH-03, OBS-01, OBS-02, OBS-03, OBS-04, SQLERR-03, SQLERR-04
**Success Criteria** (what must be TRUE):
  1. The HA Repairs UI shows active repair issues for: DB unreachable >5 min, buffer dropping records, HA recorder disabled; issues clear automatically when conditions resolve
  2. A `persistent_notification` appears when the worker thread dies and restarts, when backfill completes with a gap, and when backfill fails entirely
  3. `DataError` and `IntegrityError` trigger a per-row fallback loop; bad rows are logged and skipped; valid rows in the same batch still succeed
  4. `ProgrammingError` and `SyntaxError` (code bugs or schema drift) produce a critical-level log entry and a repair issue; no retry occurs
  5. The watchdog async task detects worker thread death, restarts the thread, and fires a `persistent_notification`; unhandled exceptions in the worker are caught and routed through the same restart path
  6. `strings.json` ships an `"issues"` section with title and description for every repair issue `translation_key`; no missing-key warnings appear in HA logs
**Plans**: 8 plans

Plans:
- [ ] 03-01-PLAN.md — Phase 3 constants + strings.json translation keys + issues.py helpers
- [ ] 03-02-PLAN.md — notifications.py: notify_watchdog_recovery + notify_backfill_gap
- [ ] 03-03-PLAN.md — Extend retry_until_success with on_recovery + on_sustained_fail hooks (TDD)
- [ ] 03-04-PLAN.md — Wire Phase 3 hooks + _last_* context in states_worker and meta_worker
- [ ] 03-05-PLAN.md — Backfill gap detection + retry-wrap read paths
- [ ] 03-06-PLAN.md — watchdog.py: watchdog_loop + spawn factories
- [ ] 03-07-PLAN.md — Wire watchdog + orchestrator done_callback + recorder_disabled in __init__.py
- [ ] 03-08-PLAN.md — Update REQUIREMENTS.md and ROADMAP.md per D-01 / D-12

## Progress

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Thread Worker Foundation | 0/8 | In progress | - |
| 2. Durability Story | 13/13 | Complete | 2026-04-22 |
| 3. Hardening and Observability | 0/TBD | Not started | - |
