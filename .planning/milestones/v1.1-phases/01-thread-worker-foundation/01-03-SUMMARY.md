---
phase: 01-thread-worker-foundation
plan: 03
subsystem: worker
tags: [thread-worker, psycopg3, scd2, queue, lifecycle]
dependency_graph:
  requires:
    - 01-01 (const.py with %s placeholders and SELECT_*_CURRENT_SQL constants)
  provides:
    - worker.py with DbWorker, StateRow, MetaCommand, _STOP
  affects:
    - ingester.py (imports StateRow from worker.py)
    - syncer.py (imports MetaCommand from worker.py; change-detection helpers called by worker)
    - __init__.py (creates DbWorker, calls start/async_stop)
    - schema.py (sync_setup_schema called by DbWorker._setup_schema)
tech_stack:
  added:
    - psycopg3 (psycopg.connect, psycopg.rows.dict_row, psycopg.types.json.Jsonb)
    - threading.Thread (daemon=True, name="timescaledb_worker")
    - queue.Queue (get(timeout=5.0) as flush timer)
  patterns:
    - frozen=True, slots=True dataclasses for queue payloads
    - _STOP singleton identity sentinel (never compared by value)
    - queue.Queue.get(timeout=5.0) as flush interval timer (BUF-03)
    - D-03: enter flush loop on connect failure (do not die)
    - per-item except Exception boundary for MetaCommand processing (T-03-07)
    - with self._conn.transaction() for SCD2 close+insert atomicity (T-03-08)
    - async_add_executor_job(thread.join, 30) for WATCH-02 shutdown
    - dict_row cursor factory for syncer change-detection helpers
key_files:
  created:
    - custom_components/timescaledb_recorder/worker.py
decisions:
  - "sync_setup_schema imported at module level (not TYPE_CHECKING) — fails at import if plan 02 not merged; acceptable in parallel wave execution"
  - "MetaCommand.params tuple for entity has 13 elements; snapshot SQL requires (*cmd.params, cmd.params[0]) to supply entity_id twice"
  - "Entity rename (old_id is not None) uses close(old_id)+insert vs non-rename update uses change detection + close(registry_id)+insert — different paths to avoid redundant DB reads on renames"
  - "Per-registry _process_*_command methods instead of one giant dispatch block — improves readability and allows per-registry docstrings"
metrics:
  duration: "~6 minutes"
  completed: "2026-04-19T10:21:43Z"
  tasks_completed: 2
  tasks_total: 2
  files_modified: 1
requirements:
  - WORK-01
  - WORK-02
  - WORK-03
  - WORK-04
  - WORK-05
  - BUF-03
  - WATCH-02
---

# Phase 1 Plan 03: DbWorker Module Summary

Dedicated OS thread module (`worker.py`) that owns all psycopg3 DB writes, with frozen dataclass queue payloads (StateRow, MetaCommand), a 5-second flush timer loop, SCD2 dispatch with atomic transaction blocks, and graceful shutdown via `async_add_executor_job(thread.join, 30)`.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Define StateRow, MetaCommand, _STOP queue payload types | 63cbebf | worker.py |
| 2 | Implement DbWorker class (lifecycle, flush loop, SCD2 dispatch) | ebb81ac | worker.py |

## What Was Built

`worker.py` exports:
- `_STOP`: singleton identity sentinel enqueued by `stop()` to signal graceful shutdown
- `StateRow`: frozen dataclass with `entity_id, state, attributes, last_updated, last_changed`; attributes kept as plain dict at enqueue; `Jsonb()` wrapping deferred to `_flush()`
- `MetaCommand`: frozen dataclass with `registry, action, registry_id, old_id, params`; `params` is None on "remove" actions (D-08)
- `DbWorker`: owns the psycopg3 connection, `queue.Queue`, all DB write operations

`DbWorker` lifecycle:
- `start()`: spawns daemon thread; returns immediately (D-01)
- `run()`: connects psycopg3; on `OperationalError`, logs warning and enters flush loop anyway (D-03); flush tick every 5 seconds via `queue.get(timeout=5.0)` (BUF-03); dispatches `StateRow`, `MetaCommand`, or `_STOP`
- `_setup_schema()`: calls `sync_setup_schema(conn, ...)` once after successful connect
- `_flush()`: wraps dict attributes in `Jsonb()` (WORK-04), uses `executemany`, logs queue depth
- `_process_meta_command()`: dispatches to `_process_entity/device/area/label_command()`
- Per-registry command handlers: "create" uses snapshot SQL with idempotent WHERE NOT EXISTS and duplicate first param; "remove" closes open row; "update" uses `dict_row` cursor + syncer change-detection helper; close+insert wrapped in `with self._conn.transaction()` for atomicity (T-03-08)
- `stop()`: `queue.put_nowait(_STOP)` — thread-safe from event loop
- `async_stop()`: puts sentinel, `await async_add_executor_job(thread.join, 30)`, logs critical if thread still alive (WATCH-02)

## Decisions Made

**Snapshot SQL duplicate ID parameter:** SCD2_SNAPSHOT_*_SQL has two `%s` slots for the registry ID — one in the SELECT list and one in the WHERE NOT EXISTS subquery. `cmd.params[0]` is the ID; `(*cmd.params, cmd.params[0])` supplies it twice. This is documented in const.py (plan 01 decision).

**Rename vs. field-change update paths for entities:** Entity renames (`old_id is not None`) skip change detection and go directly to close(old_id)+insert. Non-rename updates open a dict_row cursor and call `self._syncer._entity_row_changed()` first; only close+insert if the helper returns True. Device/area/label updates always go through change detection (no rename concept).

**sync_setup_schema at module level:** Imported directly (not under TYPE_CHECKING) because it's needed at runtime in `_setup_schema()`. Plan 02 (parallel wave 2) creates this function; until merged, the import will fail. This is the expected parallel wave behavior.

## Deviations from Plan

None — plan executed exactly as written.

## Known Stubs

None.

## Threat Flags

No new trust boundaries beyond those in the plan's threat model. All T-03-01 through T-03-08 mitigations are implemented:
- T-03-05: `async_add_executor_job(thread.join, 30)` used exclusively in `async_stop()`; `run_coroutine_threadsafe().result()` is absent
- T-03-06: `join(timeout=30)` + `is_alive()` critical log
- T-03-07: `except Exception` wraps `_process_meta_command()` call in the main loop
- T-03-08: all "update" close+insert pairs wrapped in `with self._conn.transaction()`

## Self-Check: PASSED

- FOUND: `custom_components/timescaledb_recorder/worker.py`
- FOUND: commit `63cbebf` (Task 1)
- FOUND: commit `ebb81ac` (Task 2)
