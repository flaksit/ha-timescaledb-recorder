---
phase: 02-durability-story
plan: "07"
subsystem: meta-worker
tags: [threading, scd2, retry, persistent-queue, metadata]
dependency_graph:
  requires: [02-01, 02-03, 02-05]
  provides: [TimescaledbMetaRecorderThread, meta_worker.py]
  affects: [__init__.py (plan-09 wires construction), syncer.py (plan-09 updates queue)]
tech_stack:
  added: []
  patterns:
    - retry_until_success applied at __init__ time on bound method (D-07)
    - PersistentQueue.get/task_done draining loop with crash-replay semantics (D-03-h)
    - hass.add_job fire-and-forget for notify_stall from worker thread (WATCH-02)
    - Cross-class syncer helper reuse (_entity_row_changed etc.) — established Phase 1 pattern
key_files:
  created:
    - custom_components/ha_timescaledb_recorder/meta_worker.py
  modified: []
decisions:
  - "D-15-a: TimescaledbMetaRecorderThread is a separate daemon thread from states worker — each owns its own psycopg3 connection"
  - "D-05-c: reuse Phase 1 SCD2 dispatch helpers from syncer.py unchanged"
  - "D-03-h: task_done() only called after DB write succeeds; shutdown skips task_done so item stays on disk for replay"
  - "_VALID_FROM_INDEX maps registry name to 0-based valid_from slot index matching syncer._extract_*_params tuple layout"
metrics:
  duration: 138s
  completed: "2026-04-22"
  tasks_total: 2
  tasks_completed: 2
  files_created: 1
  files_modified: 0
---

# Phase 02 Plan 07: meta_worker.py Summary

One-liner: `TimescaledbMetaRecorderThread` draining `PersistentQueue` with retry-wrapped SCD2 dispatch reusing Phase 1 syncer helpers for all four registry types.

## What Was Built

Created `custom_components/ha_timescaledb_recorder/meta_worker.py` with a single class `TimescaledbMetaRecorderThread(threading.Thread)` that:

- Owns a single psycopg3 connection (D-15-a separation from states worker)
- Loops on `PersistentQueue.get()` (blocking, Condition-based); breaks on `None` or `stop_event`
- Wraps `_write_item_raw` with `retry_until_success` at `__init__` time (D-07 integration)
- Calls `task_done()` only after the DB write succeeds; skips it on shutdown to preserve crash-replay semantics (D-03-h)
- Dispatches each item dict via `_rehydrate_params` (ISO-string → datetime in the `valid_from` slot) then routes to `_process_entity/device/area/label`
- All four dispatch methods are verbatim ports of Phase 1 `worker.py` `_process_*_command` methods with the only change being the parameter signature (plain values instead of `MetaCommand` attrs)
- Entity has a rename path (`old_id is not None`); device/area/label do not (matching HA semantics)

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Create meta_worker.py with skeleton + entity dispatch | e9f0601 | meta_worker.py (created) |
| 2 | Implement _process_device, _process_area, _process_label | 3985395 | meta_worker.py |

## Deviations from Plan

None — plan executed exactly as written. The stub signatures in Task 1 were given explicit type annotations (`action: str`, `registry_id: str`, `params: tuple | None`, `now: datetime`) for consistency with the implemented methods, which is a minor cosmetic improvement not mentioned in the plan but aligned with CLAUDE.md conventions.

## Known Stubs

None. All four registry dispatches are fully implemented. The Task 1 `NotImplementedError` stubs were replaced in Task 2 as planned.

## Self-Check

### Created files exist

```
[ -f custom_components/ha_timescaledb_recorder/meta_worker.py ] → FOUND
```

### Commits exist

```
git log: e9f0601 → FOUND
git log: 3985395 → FOUND
```

## Self-Check: PASSED
