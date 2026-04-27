---
phase: 02-durability-story
plan: "01"
subsystem: constants-and-schema
tags: [sql-constants, schema-ddl, phase2-tunables, dedup, unique-index]
dependency_graph:
  requires: []
  provides:
    - CREATE_UNIQUE_INDEX_SQL (consumed by schema.py sync_setup_schema)
    - INSERT_SQL with ON CONFLICT (consumed by states_worker plans 05/06)
    - BATCH_FLUSH_SIZE, INSERT_CHUNK_SIZE, FLUSH_INTERVAL (consumed by states_worker plan 05)
    - LIVE_QUEUE_MAXSIZE (consumed by overflow_queue plan 02)
    - BACKFILL_QUEUE_MAXSIZE (consumed by __init__.py plan 09)
    - SELECT_WATERMARK_SQL, SELECT_OPEN_ENTITIES_SQL (consumed by states_worker plan 05, backfill plan 07)
  affects:
    - custom_components/timescaledb_recorder/const.py
    - custom_components/timescaledb_recorder/schema.py
tech_stack:
  added: []
  patterns:
    - All SQL in const.py (project convention maintained)
    - CREATE UNIQUE INDEX IF NOT EXISTS (idempotent DDL — safe on every startup)
    - ON CONFLICT (last_updated, entity_id) DO NOTHING (TimescaleDB ≥ 2.18.1 required)
key_files:
  modified:
    - custom_components/timescaledb_recorder/const.py
    - custom_components/timescaledb_recorder/schema.py
decisions:
  - "Phase 2 tunables are module-level int/float constants (not classes/enums) — simple and grep-able"
  - "CREATE_UNIQUE_INDEX_SQL placed adjacent to CREATE_INDEX_SQL in const.py for collocation of index DDL"
  - "SELECT_WATERMARK_SQL and SELECT_OPEN_ENTITIES_SQL placed in existing Change-detection SELECT constants grouping"
metrics:
  duration_minutes: 2
  completed_date: "2026-04-22"
  tasks_completed: 2
  tasks_total: 2
  files_modified: 2
---

# Phase 2 Plan 1: SQL and Schema Constants Summary

One-liner: Added Phase 2 tuning constants, dedup unique index DDL, ON CONFLICT INSERT, and backfill SELECT constants to const.py; schema.py now executes the unique index on startup.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Add Phase 2 tunables + SQL constants to const.py | bf1724b | const.py |
| 2 | Execute CREATE_UNIQUE_INDEX_SQL in sync_setup_schema | b0c4830 | schema.py |

## What Was Built

### Task 1 — const.py additions

Five Phase 2 internal tunables added (not user-configurable):

- `BATCH_FLUSH_SIZE = 200` — states worker flushes when buffer hits this (D-04-e)
- `INSERT_CHUNK_SIZE = 200` — sub-batch size per `_insert_chunk` call (D-06-a)
- `FLUSH_INTERVAL = 5.0` — adaptive `get()` timeout in seconds (D-04-e)
- `LIVE_QUEUE_MAXSIZE = 10000` — OverflowQueue cap (~15–20 min of events) (D-01-a)
- `BACKFILL_QUEUE_MAXSIZE = 2` — backpressure cap on backfill_queue (D-01-b)

New DDL constant:

- `CREATE_UNIQUE_INDEX_SQL` — `CREATE UNIQUE INDEX IF NOT EXISTS idx_states_uniq ON states (last_updated, entity_id)` (D-09-a)

Modified INSERT:

- `INSERT_SQL` — appended `ON CONFLICT (last_updated, entity_id) DO NOTHING` (D-06-d)

New SELECT constants for backfill support:

- `SELECT_WATERMARK_SQL` — `SELECT MAX(last_updated) FROM states` (D-08-d)
- `SELECT_OPEN_ENTITIES_SQL` — `SELECT entity_id FROM entities WHERE valid_to IS NULL` (D-08-f)

### Task 2 — schema.py update

- Added `CREATE_UNIQUE_INDEX_SQL` to the import block from `.const`
- Added `cur.execute(CREATE_UNIQUE_INDEX_SQL)` immediately after `cur.execute(CREATE_INDEX_SQL)` inside `sync_setup_schema`
- `IF NOT EXISTS` guard makes re-execution on every HA startup safe (idempotent)

## Verification

All automated checks passed:

- `grep` checks for all new constants: PASS
- `uv run python` import smoke test for all constants: PASS
- `inspect.getsource(schema.sync_setup_schema)` contains `CREATE_UNIQUE_INDEX_SQL`: PASS
- Full wildcard import smoke test (`from const import *; from schema import sync_setup_schema`): PASS

## Deviations from Plan

None — plan executed exactly as written.

## Known Stubs

None — this plan delivers only constants and DDL; no UI-visible data paths.

## Threat Flags

| Flag | File | Description |
|------|------|-------------|
| threat_flag: schema-change | custom_components/timescaledb_recorder/const.py | New unique index on (last_updated, entity_id) in states hypertable. Requires TimescaleDB ≥ 2.18.1 for safe ON CONFLICT DO NOTHING behavior (≤ 2.17.2 bug aborts entire batch on first conflict). This constraint is documented in CLAUDE.md and STATE.md. |

## Self-Check: PASSED

Files exist:
- custom_components/timescaledb_recorder/const.py: FOUND
- custom_components/timescaledb_recorder/schema.py: FOUND

Commits exist:
- bf1724b: FOUND (feat(02-01): add Phase 2 SQL + tuning constants to const.py)
- b0c4830: FOUND (feat(02-01): execute CREATE_UNIQUE_INDEX_SQL in sync_setup_schema)
