---
phase: 02-durability-story
plan: "09"
subsystem: ingester-syncer-queue-retarget
tags: [ingester, syncer, overflow-queue, persistent-queue, json-safe, metacommand-removal]
dependency_graph:
  requires: [02-04, 02-05, 02-06]
  provides: [producer-side-queue-topology]
  affects: [custom_components/timescaledb_recorder/ingester.py, custom_components/timescaledb_recorder/syncer.py]
tech_stack:
  added: []
  patterns: [OverflowQueue-binding, PersistentQueue-put_async-via-async_create_task, JSON-safe-dict-enqueue]
key_files:
  modified:
    - custom_components/timescaledb_recorder/ingester.py
    - custom_components/timescaledb_recorder/syncer.py
decisions:
  - "Snapshot loops use async_create_task (not await put_async) to avoid serializing event-loop on fsync (D-01-c)"
  - "_to_json_safe accepts None and returns None (remove actions carry null params)"
  - "All 8 enqueue sites include enqueued_at ISO timestamp for meta_worker traceability"
  - "Stale MetaCommand docstring references in area/label handlers updated alongside code"
metrics:
  duration: "3 minutes"
  completed: "2026-04-22"
  tasks_completed: 2
  tasks_total: 2
  files_modified: 2
---

# Phase 02 Plan 09: Producer Queue Retarget Summary

One-liner: Ingester now types its queue as `OverflowQueue` (drop-newest, never raises); syncer now emits JSON-safe dicts into `PersistentQueue` via `hass.async_create_task(put_async(...))`, dropping `MetaCommand` entirely.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Update ingester.py — type queue as OverflowQueue | c08fa35 | ingester.py |
| 2 | Update syncer.py — target PersistentQueue, emit JSON-safe dicts, drop MetaCommand | c4b4406 | syncer.py |

## What Was Built

### Task 1 — ingester.py

- Replaced `import queue` with `from .overflow_queue import OverflowQueue`
- Tightened `__init__` queue parameter annotation from `queue.Queue` to `OverflowQueue`
- Added one-line comment at `put_nowait(StateRow(...))` call site noting the never-raises property (D-02-b)
- `StateRow` import path unchanged (`from .worker import StateRow`) per plan note — plan 06 retained `StateRow` in `worker.py`

### Task 2 — syncer.py

- Replaced `import queue` and `from .worker import MetaCommand` with `from .persistent_queue import PersistentQueue`
- Added module-level `_to_json_safe(params)` helper after `_extra_changed`: converts `datetime` values to ISO strings, passes all other types through; returns `None` for `None` input (remove-action path)
- Changed `__init__` signature from `queue: queue.Queue | None` to `meta_queue: PersistentQueue | None` (D-01-c)
- Replaced `bind_queue(q: queue.Queue)` with `bind_meta_queue(q: PersistentQueue)` — same post-hoc wiring semantics, new type
- Converted all 8 enqueue sites from `self._queue.put_nowait(MetaCommand(...))` to `self._hass.async_create_task(self._meta_queue.put_async(item))` with JSON-safe dict including `enqueued_at` ISO timestamp:
  - 4 snapshot loops in `async_start()` (entity, device, area, label)
  - 4 `@callback` handlers (`_handle_entity/device/area/label_registry_updated`)
- Updated stale docstring references to `MetaCommand` in area and label handlers and `async_start`

## Deviations from Plan

None — plan executed exactly as written. The import failure seen during verification (`DbWorker` missing from `worker.py`) is a pre-existing condition introduced by plan 06 in `__init__.py`; it is not caused by or related to this plan's changes. Both modified files parse cleanly and pass all structural assertions.

## Known Stubs

None. Both files wire real queue types; no placeholder data flows to any UI or downstream consumer.

## Threat Flags

None. No new network endpoints, auth paths, or trust boundaries introduced. Queue retarget is internal to the integration's data flow.

## Self-Check: PASSED

- ingester.py: FOUND
- syncer.py: FOUND
- 02-09-SUMMARY.md: FOUND
- Commit c08fa35 (Task 1): FOUND
- Commit c4b4406 (Task 2): FOUND
- No unexpected file deletions
