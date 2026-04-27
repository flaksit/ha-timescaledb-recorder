---
phase: 01-thread-worker-foundation
plan: "02"
subsystem: database
tags: [psycopg3, asyncpg, schema, DDL, hypertable, timescaledb]

requires:
  - phase: 01-01
    provides: "const.py SQL constants migrated to %s placeholders; all DDL constants exist and are importable"

provides:
  - "sync_setup_schema(conn: psycopg.Connection, chunk_interval_days, compress_after_hours) — sync DDL setup for worker thread"
  - "schema.py free of asyncpg and all async keywords"

affects:
  - 01-03-worker  # DbWorker._setup_schema() will call sync_setup_schema directly
  - any plan that imports schema.py

tech-stack:
  added: []
  patterns:
    - "Schema DDL executed synchronously in worker thread via psycopg3 cursor (not async pool)"
    - "Single conn.cursor() block for all DDL — no per-statement cursor overhead"
    - "Caller (DbWorker) owns error handling; schema function is single-responsibility"

key-files:
  created: []
  modified:
    - custom_components/timescaledb_recorder/schema.py

key-decisions:
  - "Single cursor for all 14 DDL statements — no rows returned so cursor reuse is safe and minimises diff"
  - "No try/except in sync_setup_schema — D-03 error handling belongs in DbWorker._setup_schema caller"
  - "schedule_hours cap logic (max(1, min(12, hours//2))) preserved verbatim from async original"

patterns-established:
  - "Schema setup: sync function receives already-open connection, executes DDL, does not manage connection lifecycle"

requirements-completed:
  - WORK-03

duration: 1min
completed: "2026-04-19"
---

# Phase 01 Plan 02: Schema psycopg3 Sync Conversion Summary

**Converted schema.py from asyncpg async pool pattern to psycopg3 sync cursor pattern, replacing async_setup_schema with sync_setup_schema(conn: psycopg.Connection) for direct call from worker thread**

## Performance

- **Duration:** ~1 min
- **Started:** 2026-04-19T10:18:46Z
- **Completed:** 2026-04-19T10:19:35Z
- **Tasks:** 1 of 1
- **Files modified:** 1

## Accomplishments

- Removed asyncpg dependency from schema.py entirely
- Renamed and desynchronised the schema setup function so it runs on the worker thread without async overhead
- All 14 DDL statements (hypertable, compression, indexes, 4 dimension tables, 5 dimension indexes) preserved and now execute via a single psycopg3 cursor block

## Task Commits

1. **Task 1: Convert async_setup_schema to sync_setup_schema** - `eb9d48b` (feat)

## Files Created/Modified

- `custom_components/timescaledb_recorder/schema.py` - Converted from asyncpg async to psycopg3 sync; `async_setup_schema` → `sync_setup_schema`

## Decisions Made

- Single `with conn.cursor() as cur:` block for all 14 DDL statements — DDL statements return no rows so cursor reuse is correct and keeps the diff minimal
- No exception handling added to `sync_setup_schema` — per plan spec and D-03 design, `DbWorker._setup_schema()` is responsible for catching `psycopg.OperationalError`

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None. The `grep -c 'async'` command exits with code 1 when there are 0 matches (standard grep behaviour); this is correct — 0 occurrences confirms the file is clean.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- `schema.py` exports `sync_setup_schema` with the exact signature documented in the plan interface
- Ready for Plan 03 (`worker.py` DbWorker implementation) to call `sync_setup_schema(conn)` directly from the worker thread

## Self-Check

- `custom_components/timescaledb_recorder/schema.py` — file exists and verified via ast.parse
- Commit `eb9d48b` — present in git log
- Zero `async` occurrences in schema.py confirmed by grep returning 0 matches

## Self-Check: PASSED

---
*Phase: 01-thread-worker-foundation*
*Completed: 2026-04-19*
