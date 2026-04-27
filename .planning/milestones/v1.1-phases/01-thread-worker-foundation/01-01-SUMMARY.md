---
phase: 01-thread-worker-foundation
plan: 01
subsystem: const
tags: [sql, psycopg3, migration, constants]
dependency_graph:
  requires: []
  provides:
    - const.py with psycopg3 %s placeholder syntax in all parameterized SQL
    - SELECT_ENTITY_CURRENT_SQL, SELECT_DEVICE_CURRENT_SQL, SELECT_AREA_CURRENT_SQL, SELECT_LABEL_CURRENT_SQL
  affects:
    - worker.py (consumes INSERT_SQL via executemany)
    - syncer.py (consumes SCD2_* and SELECT_*_CURRENT_SQL constants)
    - schema.py (consumes DDL constants — unchanged)
tech_stack:
  added: []
  patterns:
    - All SQL in const.py (project convention enforced)
    - psycopg3 %s positional placeholder syntax
key_files:
  modified:
    - custom_components/timescaledb_recorder/const.py
decisions:
  - "SCD2_SNAPSHOT_*_SQL: each %s is a separate positional slot in psycopg3 (unlike asyncpg $N reuse); first ID param must be passed twice when executing snapshot SQL"
  - "SELECT_*_CURRENT_SQL placed after SCD2_INSERT_*_SQL constants in const.py for logical grouping (DDL → SCD2 close → snapshot → insert → select)"
metrics:
  duration: "~5 minutes"
  completed: "2026-04-19T10:15:28Z"
  tasks_completed: 1
  tasks_total: 1
  files_modified: 1
requirements:
  - WORK-03
  - WORK-04
---

# Phase 1 Plan 01: SQL Placeholder Migration and SELECT Constants Summary

Migrated all asyncpg `$N` positional placeholders to psycopg3 `%s` syntax across 13 parameterized SQL constants in `const.py`, and added four `SELECT_*_CURRENT_SQL` constants extracted from inline strings in `syncer.py`.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Migrate $N→%s and add SELECT constants | a3fd4b3 | const.py |

## What Was Built

All SQL constants in `const.py` now use psycopg3-compatible `%s` positional parameters. The 13 migrated constants are:

- `INSERT_SQL` (5 params)
- `SCD2_CLOSE_ENTITY_SQL`, `SCD2_CLOSE_DEVICE_SQL`, `SCD2_CLOSE_AREA_SQL`, `SCD2_CLOSE_LABEL_SQL` (2 params each)
- `SCD2_SNAPSHOT_ENTITY_SQL`, `SCD2_SNAPSHOT_DEVICE_SQL`, `SCD2_SNAPSHOT_AREA_SQL`, `SCD2_SNAPSHOT_LABEL_SQL` (id param appears twice — see Decisions)
- `SCD2_INSERT_ENTITY_SQL` (13 params), `SCD2_INSERT_DEVICE_SQL` (8 params), `SCD2_INSERT_AREA_SQL` (4 params), `SCD2_INSERT_LABEL_SQL` (5 params)

Four new constants added at the end of `const.py`:
- `SELECT_ENTITY_CURRENT_SQL` — reads current open entity row by `entity_id`
- `SELECT_DEVICE_CURRENT_SQL` — reads current open device row by `device_id`
- `SELECT_AREA_CURRENT_SQL` — reads current open area row by `area_id`
- `SELECT_LABEL_CURRENT_SQL` — reads current open label row by `label_id`

Python `.format()` tokens (`{chunk_days}`, `{compress_hours}`, `{schedule_hours}`) in DDL constants were left unchanged.

## Decisions Made

**Snapshot SQL duplicate ID parameter:** In asyncpg, `$1` could be reused in the `WHERE NOT EXISTS` subquery of `SCD2_SNAPSHOT_*_SQL` because asyncpg maps `$N` by index. psycopg3 treats each `%s` as a separate positional slot — so the id value (entity_id, device_id, etc.) must be passed **twice** in the parameter tuple when executing snapshot SQL. Each snapshot constant has a comment documenting this requirement directly above it so future callers cannot miss it.

## Deviations from Plan

None — plan executed exactly as written.

## Known Stubs

None.

## Threat Flags

None — `const.py` contains only static string constants. No new network endpoints, auth paths, file access patterns, or schema changes were introduced.

## Self-Check: PASSED

- FOUND: `custom_components/timescaledb_recorder/const.py`
- FOUND: `.planning/phases/01-thread-worker-foundation/01-01-SUMMARY.md`
- FOUND: commit `a3fd4b3`
