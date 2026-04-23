# Requirements: ha-timescaledb-recorder v1.1

**Defined:** 2026-04-19
**Core Value:** State changes and registry metadata land in TimescaleDB reliably even when the database is temporarily unavailable — HA continues to function normally regardless.

## v1 Requirements

### Worker (Thread isolation + psycopg3)

- [ ] **WORK-01**: Integration uses a dedicated OS thread (threading.Thread) for all DB writes; HA event loop never blocked or crashed by DB errors
- [ ] **WORK-02**: HA event bus callback enqueues to queue.Queue (sync, safe from asyncio @callback); worker thread dequeues and owns all DB writes
- [ ] **WORK-03**: asyncpg replaced by psycopg[binary]==3.3.3; single psycopg.Connection owned by worker thread for its lifetime
- [ ] **WORK-04**: psycopg3 JSONB writes wrap Python dicts in Jsonb(); list[str] adapts to TEXT[] automatically
- [ ] **WORK-05**: All hass.async_* calls from worker use thread-safe bridges (hass.add_job for fire-and-forget, run_coroutine_threadsafe for keyword-heavy APIs like async_create_issue)

### Buffer (Bounded RAM + drop-oldest)

- [ ] **BUF-01**: RAM buffer bounded at 10,000 records; oldest records dropped when limit reached
- [ ] **BUF-02**: Buffer overflow logs a warning with drop count and raises a persistent repair issue
- [ ] **BUF-03**: Flush interval remains 5 seconds (matches HA recorder durability class)

### Backfill (HA sqlite recovery)

- [ ] **BACK-01**: On integration startup, backfill runs in background: fetches state changes from HA recorder since MAX(last_updated) in ha_states
- [ ] **BACK-02**: On DB-healthy transition (after flush failure → success), backfill runs to fill the gap
- [ ] **BACK-03**: Backfill uses recorder.history.state_changes_during_period dispatched via recorder_instance.async_add_executor_job; iterates per entity_id (entity_id=None is not supported by the API)
- [ ] **BACK-04**: Backfill writes in chunks (≤1000 rows/chunk); flush tasks interleave between chunks via shared queue.Queue (no threading.Lock needed)
- [ ] **BACK-05**: Backfill applies the same entity filter as live ingestion
- [ ] **BACK-06**: Integration startup does not block on backfill completion; backfill runs as background task

### Metadata (Recovery on DB restore)

- [ ] **META-01**: On DB-healthy transition, worker triggers full registry re-snapshot (idempotent WHERE NOT EXISTS)
- [ ] **META-02**: Re-snapshot covers all four registries: entities, devices, areas, labels
- [ ] **META-03**: Loss of change-time precision during outage is accepted; final state is always correct

### Watchdog (Worker liveness)

- [ ] **WATCH-01**: Async watchdog task periodically checks worker thread.is_alive(); restarts dead worker and fires a persistent_notification
- [ ] **WATCH-02**: Graceful shutdown: EVENT_HOMEASSISTANT_STOP handler puts sentinel in queue, then awaits async_add_executor_job(thread.join); never calls run_coroutine_threadsafe().result() (deadlock risk on shutdown)
- [ ] **WATCH-03**: Worker thread unhandled exceptions are caught in the thread's run() method and trigger watchdog restart path

### Observability (Notifications + repair issues)

- [ ] **OBS-01**: persistent_notification fires for one-shot events: worker died + restarted, backfill completed with gap, backfill failed
- [ ] **OBS-02**: Repair issues (issue_registry) for ongoing conditions: DB unreachable >5 min, buffer dropping records, HA recorder disabled, HA version untested
- [ ] **OBS-03**: Repair issues cleared (async_delete_issue) when condition resolves
- [ ] **OBS-04**: strings.json ships with "issues" section containing title + description for each repair issue translation_key

### SQL Errors (Granular error handling)

- [ ] **SQLERR-01**: PostgresConnectionError / OSError → transient; keep rows, retry on next flush
- [ ] **SQLERR-02**: DeadlockDetected / LockNotAvailable / SerializationError → transient; retry
- ~~**SQLERR-03**~~: ~~DataError / IntegrityError → isolate offending row via single-row fallback loop; log + skip bad row; continue batch~~ **DROPPED** — see [Phase 3 CONTEXT D-01](phases/03-hardening-and-observability/03-CONTEXT.md#D-01). Persistent bad-row failures stall the worker and surface via the `worker_stalled` repair issue + Phase 2 stall notification.
- ~~**SQLERR-04**~~: ~~ProgrammingError / SyntaxError → critical log + repair issue; do not retry (code bug or schema drift)~~ **DROPPED** — see [Phase 3 CONTEXT D-01](phases/03-hardening-and-observability/03-CONTEXT.md#D-01). Persistent code-bug / schema-drift failures follow the same unified path as other errors: retry → stall notification after N=5 → `worker_stalled` repair issue.
- [ ] **SQLERR-05**: TimescaleDB add-on version verified ≥ 2.18.1 before enabling ON CONFLICT DO NOTHING dedup (≤ 2.17.2 bug aborts entire batch on first conflict)

## v2 Requirements

### Schema migration infrastructure

- **SCHEMA-01**: schema_version table + migration registry for future ALTER TABLE operations (deferred — v1.1 schema is identical to v0.3.6)

### SSL/TLS configuration

- **SSL-01**: SSL option for TimescaleDB connection (deferred — localhost add-on topology low risk)

### Sensor entity for integration health

- **SENSOR-01**: sensor.timescaledb_recorder_status exposing last_flush_time, buffer_depth, error_count (deferred — repair issues provide sufficient observability for v1.1)

### Entity filter in options flow

- **FILTER-01**: Entity filter configurable via HA options UI without re-adding integration (deferred)

## Out of Scope

| Feature | Reason |
|---------|--------|
| Per-event WAL (own fsync'd append log) | Overkill — HA sqlite is the safety net |
| Multiprocessing isolation | Overkill for I/O-bound low-rate workload |
| Direct sqlite3 reads (bypassing recorder.history API) | Risks HA corruption detection; only if API proves too slow |
| CPU parallelism | I/O bound — unnecessary |
| GIL-free thread safety | Python 3.14 retains GIL for HA deployments; queue.SimpleQueue/Queue safety relies on this |

## Traceability

Updated by roadmap creation (2026-04-19).

| Requirement | Phase | Status |
|-------------|-------|--------|
| WORK-01 | Phase 1 | Pending |
| WORK-02 | Phase 1 | Pending |
| WORK-03 | Phase 1 | Pending |
| WORK-04 | Phase 1 | Pending |
| WORK-05 | Phase 1 | Pending |
| BUF-01 | Phase 2 | Pending |
| BUF-02 | Phase 2 | Pending |
| BUF-03 | Phase 1 | Pending |
| BACK-01 | Phase 2 | Pending |
| BACK-02 | Phase 2 | Pending |
| BACK-03 | Phase 2 | Pending |
| BACK-04 | Phase 2 | Pending |
| BACK-05 | Phase 2 | Pending |
| BACK-06 | Phase 2 | Pending |
| META-01 | Phase 2 | Pending |
| META-02 | Phase 2 | Pending |
| META-03 | Phase 2 | Pending |
| WATCH-01 | Phase 3 | Shipped |
| WATCH-02 | Phase 1 | Pending |
| WATCH-03 | Phase 3 | Shipped |
| OBS-01 | Phase 3 | Shipped |
| OBS-02 | Phase 3 | Shipped |
| OBS-03 | Phase 3 | Shipped |
| OBS-04 | Phase 3 | Shipped |
| SQLERR-01 | Phase 2 | Pending |
| SQLERR-02 | Phase 2 | Pending |
| SQLERR-03 | Phase 3 | DROPPED (see 03-CONTEXT.md#D-01) |
| SQLERR-04 | Phase 3 | DROPPED (see 03-CONTEXT.md#D-01) |
| SQLERR-05 | Phase 2 | Pending |

**Coverage:**
- v1 requirements: 29 total (WORK×5, BUF×3, BACK×6, META×3, WATCH×3, OBS×4, SQLERR×5)
- Mapped to phases: 29
- Unmapped: 0 ✓
- Dropped (scope reduction during phase planning): 2 (SQLERR-03, SQLERR-04 — see [Phase 3 CONTEXT D-01](phases/03-hardening-and-observability/03-CONTEXT.md#D-01))

Note: The pre-roadmap header said "25 total" — actual count is 29 after full enumeration.

---
*Requirements defined: 2026-04-19*
*Last updated: 2026-04-23 — Phase 3 execution: SQLERR-03/04 marked DROPPED per D-01; WATCH/OBS entries marked Shipped*
