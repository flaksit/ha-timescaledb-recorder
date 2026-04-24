---
phase: 03-hardening-and-observability
plan: "05"
subsystem: backfill-retry-and-gap-detection
tags: [backfill, retry, gap-detection, states_worker, tdd, HIGH-2-fix]
dependency_graph:
  requires:
    - notify_backfill_gap (notifications.py — Plan 02)
    - retry_until_success with on_transient optional (retry.py — Plan 03)
    - states_worker._insert_chunk retry-wrap pattern (states_worker.py — Phase 2 + Plan 04)
  provides:
    - backfill_orchestrator new kwarg threading_stop_event: threading.Event
    - gap-detection block (lines 126-156 in backfill.py) with None guard (HIGH-2 fix)
    - fetch_slice retry wrapper in backfill_orchestrator (line 92-95)
    - states_worker._read_watermark_raw (renamed from read_watermark)
    - states_worker.read_watermark as retry-wrapped instance attribute (line 126-129)
  affects:
    - 03-07 (__init__.py must pass threading_stop_event when calling backfill_orchestrator)
    - 03-06 (watchdog reads states_worker._last_exception — no change to that interface)
tech_stack:
  added: []
  patterns:
    - TDD (RED/GREEN per task)
    - retry_until_success at call site for recorder-pool reads (on_transient=None)
    - retry_until_success in __init__ for owned-connection reads (on_transient=reset_db_connection)
    - None-guard before float comparison for external HA state (HIGH-2 pattern)
key_files:
  created: []
  modified:
    - custom_components/ha_timescaledb_recorder/backfill.py
    - custom_components/ha_timescaledb_recorder/states_worker.py
    - tests/test_backfill.py
    - tests/test_states_worker.py
decisions:
  - "Gap detection skips entirely when oldest_ts is None — None means recorder not yet ready, not confirmed data loss (HIGH-2 fix: spurious startup gap alerts prevented)"
  - "fetch_slice retry uses on_transient=None — recorder pool owns the connection, orchestrator has no handle to reset"
  - "read_watermark retry uses on_transient=reset_db_connection — worker owns the connection, same pattern as _insert_chunk"
  - "No stall/recovery hooks on read_watermark wrapper — persistent failure manifests as stalled backfill, surfaced via orchestrator done_callback (Plan 07)"
  - "backfill_orchestrator body has no try/except — unhandled exceptions propagate to asyncio Task and fire add_done_callback (D-09-b)"
  - "test_fetch_slice_retries_on_transient_error patches retry_until_success with zero-backoff wrapper to avoid blocking event loop in test (HIGH-3 constraint: synchronous retry sleep must not run on event loop)"
metrics:
  duration: "587 seconds"
  completed_date: "2026-04-23T08:38:46Z"
  tasks_completed: 2
  files_modified: 4
  files_created: 0
  tests_added: 10
---

# Phase 03 Plan 05: Backfill Gap Detection + Read-Path Retry Summary

Extends the backfill orchestrator with recorder-retention gap detection (None-safe) and retry-wraps both read paths (`_fetch_slice_raw` in the recorder pool, `read_watermark` on the worker connection) so transient errors no longer crash the orchestrator or block the worker loop.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 (RED) | states_worker read_watermark retry — failing tests | 5a0cbab | tests/test_states_worker.py |
| 1 (GREEN) | retry-wrap read_watermark on states_worker | e16c4bd | custom_components/ha_timescaledb_recorder/states_worker.py, tests/test_states_worker.py |
| 2 (RED) | backfill gap detection + retry — failing tests | 422d0ff | tests/test_backfill.py |
| 2 (GREEN) | backfill gap detection + retry-wrap _fetch_slice_raw | 6cee1a3 | custom_components/ha_timescaledb_recorder/backfill.py, tests/test_backfill.py |

## backfill_orchestrator Signature Changes

New keyword-only parameter added after `stop_event: asyncio.Event`:

```python
async def backfill_orchestrator(
    hass: HomeAssistant,
    *,
    ...
    stop_event: asyncio.Event,
    threading_stop_event: threading.Event,   # NEW — Plan 05
) -> None:
```

Plan 07 (`__init__.py` wiring) must pass `threading_stop_event` when spawning the orchestrator task.

## Gap-Detection Block (backfill.py lines 126-156)

Inserted after `from_ = wm - _LATE_ARRIVAL_GRACE` (line 124), before `cutoff = t_clear` (line 158).

```
line 126: # D-08: recorder-retention gap detection
line 133: oldest_ts_float = recorder_instance.states_manager.oldest_ts
line 134: if oldest_ts_float is None:
line 135:     _LOGGER.debug("oldest_ts not yet available ... skipping gap detection this cycle")
line 138: else:
line 139:     oldest_recorder_ts = datetime.fromtimestamp(oldest_ts_float, tz=timezone.utc)
line 140:     if oldest_recorder_ts > from_:
line 143:         notify_backfill_gap(hass, reason="recorder_retention", details={...})
line 154:         from_ = oldest_recorder_ts
```

## None-Guard Behaviour (for Plan 07)

When `oldest_ts is None`, gap detection is **skipped entirely** — no call to `notify_backfill_gap`, no adjustment to `from_`. A DEBUG log is emitted.

This is safe on cold start: the HA StatesManager may return `None` while the recorder is warming up. Plan 07 (orchestrator restart) can rely on gap detection being a no-op in that state without any additional guard.

## Retry Wrap Summary (for Plan 07)

### `_fetch_slice_raw` (backfill.py lines 92-95)

Constructed once per orchestrator invocation at entry, BEFORE the `while` loop:

```python
fetch_slice = retry_until_success(
    stop_event=threading_stop_event,
    on_transient=None,           # no owned connection
)(_fetch_slice_raw)
```

- `on_transient=None` — recorder pool manages the session; orchestrator has no handle to reset.
- `stop_event=threading_stop_event` — shared threading.Event; set on `async_unload_entry` → retry loop exits on shutdown.
- Runs inside `recorder_instance.async_add_executor_job` (thread pool) — never on the event loop directly (HIGH-3 satisfied).

### `read_watermark` (states_worker.py lines 126-129)

Constructed in `__init__` after `self._insert_chunk` wrapper:

```python
self.read_watermark = retry_until_success(
    stop_event=stop_event,
    on_transient=self.reset_db_connection,
)(self._read_watermark_raw)
```

- `on_transient=reset_db_connection` — worker owns the connection; drops and reopens on transient error.
- No stall/recovery/sustained-fail hooks — a persistently failing watermark read stalls backfill, which surfaces via orchestrator done_callback (Plan 07).
- Raw variant preserved as `_read_watermark_raw`; public API (`states_worker.read_watermark`) unchanged from caller perspective.

## Test Count

### tests/test_backfill.py

| Category | Count |
|----------|-------|
| Phase 2 preserved (updated with threading_stop_event kwarg) | 3 |
| Phase 3 new (Plan 05) | 7 |
| Total | 10 |

New tests: `test_orchestrator_skips_gap_detection_when_oldest_ts_none`, `test_orchestrator_fires_backfill_gap_when_oldest_ts_after_needed_from`, `test_orchestrator_no_gap_notification_when_oldest_ts_before_needed_from`, `test_orchestrator_adjusts_from_to_oldest_ts_after_gap`, `test_fetch_slice_retry_wrapping_uses_on_transient_none`, `test_fetch_slice_retries_on_transient_error`, `test_orchestrator_does_not_swallow_unhandled_exceptions`.

### tests/test_states_worker.py

| Category | Count |
|----------|-------|
| Phase 2 + Plan 04 preserved | 20 |
| Phase 3 new (Plan 05) | 3 |
| Total | 23 |

New tests: `test_read_watermark_is_retry_wrapped`, `test_read_watermark_retries_on_transient_error`, `test_read_watermark_retry_wiring_captures_on_transient`.

Full suite: 159 passed (up from 149 at end of Plan 04).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] test_retry_decorator_wired_with_all_phase3_hooks failed after adding second retry_until_success call**
- **Found during:** Task 1 (GREEN)
- **Issue:** The existing test used a `fake_retry` that stored a single `captured` dict. After Plan 05 adds a second `retry_until_success` call in `__init__` (for `read_watermark`), the second call with `on_recovery=None` overwrote the first call's captured values. The assertion `captured.get("on_recovery") is not None` failed.
- **Fix:** Changed the test to accumulate all calls in `all_calls: list`, then assert specifically on `all_calls[0]` (the `_insert_chunk` wrapper) for Phase 3 hooks.
- **Files modified:** `tests/test_states_worker.py`
- **Commit:** e16c4bd

**2. [Rule 1 - Bug] Bound method identity: on_transient assertion used `is` comparison**
- **Found during:** Task 1 (GREEN)
- **Issue:** Python creates a new bound method wrapper on each attribute access, so `captured_on_transient is t.reset_db_connection` always fails. The plan spec said `on_transient is self.reset_db_connection`.
- **Fix:** Changed assertion to compare `__func__` and `__self__` attributes instead.
- **Files modified:** `tests/test_states_worker.py`
- **Commit:** e16c4bd

**3. [Rule 1 - Bug] Gap detection tests timed out because stop_event.set() doesn't unblock backfill_request.wait()**
- **Found during:** Task 2 (GREEN)
- **Issue:** Tests called `stop_event.set()` to terminate the orchestrator, but the orchestrator was blocked at `await backfill_request.wait()`. Setting stop_event has no effect until backfill_request is also set.
- **Fix:** Changed stopper tasks to set both `stop_event` and `backfill_request` together.
- **Files modified:** `tests/test_backfill.py`
- **Commit:** 6cee1a3

**4. [Rule 1 - Bug] test_fetch_slice_retries_on_transient_error used future watermark causing empty slice loop**
- **Found during:** Task 2 (GREEN)
- **Issue:** `wm = datetime(2026, 4, 23, 10, 0, 0, UTC)` was in the future relative to the test's `t_clear = datetime.now()`. So `from_ = wm - 10min = 09:50` and `cutoff = now = 08:30` — `slice_start < cutoff` was False, the slice loop never ran, `_fetch_slice_raw` was never called.
- **Fix:** Changed watermark to `datetime.now(UTC) - timedelta(hours=2)` so `from_` is always in the past.
- **Files modified:** `tests/test_backfill.py`
- **Commit:** 6cee1a3

**5. [Rule 1 - Bug] test_fetch_slice_retries_on_transient_error would block event loop via synchronous retry sleep**
- **Found during:** Task 2 (GREEN)
- **Issue:** `recorder_inst.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: fn(*args))` runs `fn` synchronously on the event loop. The real retry decorator uses `threading_stop.wait(backoff)` — a blocking call — which would block the event loop (HIGH-3 violation in tests).
- **Fix:** Patched `retry_until_success` in backfill with a zero-backoff wrapper (`backoff_schedule=(0,)`) that delegates to the real decorator. Actual retry behavior is tested separately in `test_retry.py`.
- **Files modified:** `tests/test_backfill.py`
- **Commit:** 6cee1a3

## Threat Surface Scan

No new network endpoints, auth paths, file access patterns, or schema changes introduced.

T-03-05-03 (Spoofing — oldest_ts None interpreted as gap): mitigated by HIGH-2 None-guard. The guard is tested by `test_orchestrator_skips_gap_detection_when_oldest_ts_none` which asserts both that `notify_backfill_gap` is NOT called and that a DEBUG log is emitted.

## Known Stubs

None — all code paths are fully implemented. `notify_backfill_gap` is called with concrete values derived from `oldest_ts_float` and `from_`. `read_watermark` wrapper is wired to real `reset_db_connection`.

## Self-Check: PASSED

Files exist:
- custom_components/ha_timescaledb_recorder/backfill.py — FOUND (notify_backfill_gap, retry_until_success, threading_stop_event, fetch_slice, gap detection block)
- custom_components/ha_timescaledb_recorder/states_worker.py — FOUND (_read_watermark_raw, self.read_watermark = retry_until_success)
- tests/test_backfill.py — FOUND (10 test functions including 7 new)
- tests/test_states_worker.py — FOUND (23 test functions including 3 new)

Commits verified: 5a0cbab, e16c4bd, 422d0ff, 6cee1a3

Signature verification: `uv run python -c "...assert 'threading_stop_event' in sig.parameters..."` → ok

Full suite: 159 passed
