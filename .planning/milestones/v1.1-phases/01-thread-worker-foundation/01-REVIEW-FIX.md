---
phase: 01-thread-worker-foundation
fixed_at: 2026-04-19T00:00:00Z
review_path: .planning/phases/01-thread-worker-foundation/01-REVIEW.md
iteration: 1
findings_in_scope: 4
fixed: 4
skipped: 0
status: all_fixed
---

# Phase 01: Code Review Fix Report

**Fixed at:** 2026-04-19
**Source review:** .planning/phases/01-thread-worker-foundation/01-REVIEW.md
**Iteration:** 1

## Summary

- Findings in scope: 4
- Fixed: 4
- Skipped: 0

## Fixed Issues

### WR-01: `async_get` return value not guarded for `None` in all four registry event handlers

**Files modified:** `custom_components/timescaledb_recorder/syncer.py`
**Commit:** 0d289d2
**Applied fix:** Added `if entry is None: _LOGGER.warning(...); return` guard in each of the four registry event handlers (`_handle_entity_registry_updated`, `_handle_device_registry_updated`, `_handle_area_registry_updated`, `_handle_label_registry_updated`) immediately after the `async_get` / `async_get_area` / `async_get_label` call. Each guard logs a warning with the registry ID and action before returning, preventing `AttributeError` propagation into the extract helpers on a tight delete race.

### WR-02: `area_id` retrieved with `.get()` instead of `[]` — silently propagates `None`

**Files modified:** `custom_components/timescaledb_recorder/syncer.py`
**Commit:** 39ba4c3
**Applied fix:** Changed `area_id = event.data.get("area_id")` to `area_id = event.data["area_id"]` in `_handle_area_registry_updated`. Added an explanatory comment noting that the `reorder` action (which carries no `area_id`) is already filtered by the early-return guard above, so bracket access is safe and intentionally raises `KeyError` on unexpected event shapes rather than silently flowing `None` into the SQL predicate.

### WR-03: Flush-tick debug log captures buffer length *after* `buffer.clear()`

**Files modified:** `custom_components/timescaledb_recorder/worker.py`
**Commit:** d8ae317
**Applied fix:** Introduced `flushed_count` variable in the `queue.Empty` branch. `flushed_count = len(buffer)` is captured before `self._flush(buffer)` and `buffer.clear()`. An `else` branch sets `flushed_count = 0` when no flush occurs. The `_LOGGER.debug` call now uses `flushed_count` and renames the format key from `buffered=` to `flushed=` to accurately reflect what was written.

### WR-04: `worker.async_stop()` called twice when `syncer.async_start()` raises

**Files modified:** `custom_components/timescaledb_recorder/__init__.py`
**Commit:** 92c1479
**Applied fix:** Replaced the nested try/except structure with a flat single try/except that tracks started components in a `started: list[str]` list. `ingester.stop()` is only called when `"ingester"` is in `started`; `worker.async_stop()` is called exactly once at the bottom of the except block regardless of which component failed. Added a comment explaining why the flat structure is preferred over nested to prevent future regression.

---

_Fixed: 2026-04-19_
_Fixer: Claude (gsd-code-fixer)_
_Iteration: 1_
