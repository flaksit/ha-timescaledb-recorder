---
phase: 01-thread-worker-foundation
plan: "06"
subsystem: integration-lifecycle
tags:
  - lifecycle
  - wiring
  - psycopg3
  - rollback
dependency_graph:
  requires:
    - 01-02  # schema.py psycopg3 migration
    - 01-04  # StateIngester thin queue relay
    - 01-05  # MetadataSyncer thin queue relay
  provides:
    - integration entry point with full lifecycle wiring
    - rollback guard on partial setup failure
  affects:
    - custom_components/ha_timescaledb_recorder/__init__.py
    - custom_components/ha_timescaledb_recorder/manifest.json
tech_stack:
  added:
    - psycopg[binary]==3.3.3 declared in manifest.json (replaces asyncpg==0.31.0)
  patterns:
    - Two-phase construction: syncer created before worker, bind_queue() wires queue after
    - Nested try/except rollback: reverse-order stop on any setup step failure
    - D-01: async_setup_entry makes no DB calls, returns True immediately
    - WATCH-02: ingester.stop() (sync) then syncer/worker async_stop() in unload
key_files:
  modified:
    - custom_components/ha_timescaledb_recorder/__init__.py
    - custom_components/ha_timescaledb_recorder/manifest.json
decisions:
  - "Two-phase construction order: syncer(hass) → worker(syncer) → bind_queue() → ingester(queue) — worker needs syncer for SCD2 change-detection; syncer needs worker.queue for enqueuing"
  - "Rollback uses nested try/except; outer except handles ingester.async_start() failure, inner handles syncer.async_start() failure; worker.async_stop() may be called twice in syncer-failure path but that is safe (second join() returns immediately)"
  - "ingester.stop() called (sync, not awaited) in async_unload_entry per StateIngester API from plan 04"
metrics:
  duration: "~5 minutes"
  completed: "2026-04-19T13:30:36Z"
  tasks_completed: 1
  tasks_total: 1
  files_modified: 2
---

# Phase 01 Plan 06: Integration Lifecycle Wiring Summary

**One-liner:** Replaced asyncpg pool wiring with DbWorker/StateIngester/MetadataSyncer two-phase construction, rollback guard, and psycopg[binary]==3.3.3 manifest declaration.

## What Was Built

`__init__.py` was rewritten to wire the three components built in plans 03, 04, 05:

- `HaTimescaleDBData` dataclass now holds `worker: DbWorker`, `ingester: StateIngester`, `syncer: MetadataSyncer` — the `pool: asyncpg.Pool` field is gone
- `async_setup_entry` follows D-01: no DB calls, returns True immediately; worker thread starts in the background
- Two-phase construction: syncer created first (no queue), worker created with syncer reference, `syncer.bind_queue(worker.queue)` wires the shared queue via public API
- Rollback guard: nested try/except stops all started components in reverse order if any step raises after `worker.start()`
- `async_unload_entry` follows WATCH-02: `ingester.stop()` (sync) → `await syncer.async_stop()` → `await worker.async_stop()`

`manifest.json` updated: `asyncpg==0.31.0` → `psycopg[binary]==3.3.3`; version bumped `0.3.6` → `1.1.0`.

## Tasks

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Rewrite __init__.py lifecycle with rollback and update manifest.json | 5a66a96 | __init__.py, manifest.json |

## Deviations from Plan

None — plan executed exactly as written.

The plan noted a potential double-call to `worker.async_stop()` in the syncer-failure path (inner except calls it, then re-raise causes outer except to call it again). This was acknowledged in the plan spec as an acceptable pattern — second `join()` call returns immediately after the thread has exited.

## Known Stubs

None. All components are fully wired with real implementations from prior plans.

## Threat Flags

No new security surface introduced beyond what the plan's threat model covers. All DB connections remain confined to `DbWorker`. The DSN flows from `entry.data[CONF_DSN]` to `DbWorker.__init__()` — not logged anywhere in `__init__.py` (T-06-01 mitigation applied).

## Self-Check: PASSED

- [x] `custom_components/ha_timescaledb_recorder/__init__.py` exists and has no `asyncpg`, no `async_setup_schema`
- [x] `custom_components/ha_timescaledb_recorder/manifest.json` requires `psycopg[binary]==3.3.3`, version `1.1.0`
- [x] Commit `5a66a96` exists: `feat(01-06): wire DbWorker/StateIngester/MetadataSyncer lifecycle in __init__.py`
- [x] Verification script: PASS
- [x] Overall verification: OK
