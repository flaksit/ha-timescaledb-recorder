---
phase: 03-hardening-and-observability
reviewed: 2026-04-23T00:00:00Z
depth: standard
files_reviewed: 20
files_reviewed_list:
  - custom_components/timescaledb_recorder/__init__.py
  - custom_components/timescaledb_recorder/backfill.py
  - custom_components/timescaledb_recorder/const.py
  - custom_components/timescaledb_recorder/issues.py
  - custom_components/timescaledb_recorder/meta_worker.py
  - custom_components/timescaledb_recorder/notifications.py
  - custom_components/timescaledb_recorder/retry.py
  - custom_components/timescaledb_recorder/states_worker.py
  - custom_components/timescaledb_recorder/strings.json
  - custom_components/timescaledb_recorder/watchdog.py
  - tests/test_backfill.py
  - tests/test_const_phase3.py
  - tests/test_init.py
  - tests/test_issues.py
  - tests/test_meta_worker.py
  - tests/test_notifications.py
  - tests/test_retry.py
  - tests/test_states_worker.py
  - tests/test_strings_phase3.py
  - tests/test_watchdog.py
findings:
  critical: 0
  warning: 2
  info: 3
  total: 5
status: issues_found
---

# Phase 03: Code Review Report

**Reviewed:** 2026-04-23T00:00:00Z
**Depth:** standard
**Files Reviewed:** 20
**Status:** issues_found

## Summary

Phase 3 adds watchdog supervision, orchestrator crash recovery, stall/recovery repair issues, sustained-fail (db_unreachable) detection, and recorder_disabled auto-clear. The implementation is well-structured and the test suite covers the specified behaviours thoroughly.

Two bugs warrant attention before shipping:

1. `orchestrator_kwargs` captures bound methods of the **initial** `states_worker` instance at startup. When the watchdog respawns the states_worker after a crash, the orchestrator continues to call `read_watermark` and `read_open_entities` on the dead worker's object — not the new one. After a crash the old worker's connection is closed; subsequent calls from `read_watermark` reopen a new psycopg3 connection owned by neither the old nor the new worker thread, violating the single-connection-per-thread invariant and defeating the retry wiring on the new worker.

2. `DB_UNREACHABLE_THRESHOLD_SECONDS` in `const.py` is never actually passed as `sustained_fail_seconds=` to `retry_until_success` in either worker. Changing the constant has no effect; the decorator uses its own hard-coded default of `300.0`.

Three informational items are also noted.

## Warnings

### WR-01: Orchestrator holds stale `read_watermark`/`read_open_entities` bindings after watchdog respawn

**File:** `custom_components/timescaledb_recorder/__init__.py:415-421`

**Issue:** `orchestrator_kwargs` is built once inside `_on_ha_started` and captures:

```python
read_watermark=data.states_worker.read_watermark,
open_entities_reader=data.states_worker.read_open_entities,
```

These are bound methods of the states_worker instance that exists **at HA startup**. The watchdog (in `watchdog.py:104`) replaces `runtime.states_worker` with a new instance via `setattr(runtime, "states_worker", new_thread)`, but `orchestrator_kwargs` still holds the old worker's bound methods. After a crash:

- The dead worker's `_conn` is closed in its `finally` block (correct).
- The orchestrator's next `read_watermark()` call hits the old worker's `get_db_connection()`, which lazily opens a **new connection** — not associated with the new worker thread, not covered by the new worker's retry wiring, and potentially opened from an executor thread while the new worker's `_conn` is also used.

The orchestrator relaunch via `_make_orchestrator_done_callback` re-uses the same stale `orchestrator_kwargs` dict (`backfill_orchestrator(**orchestrator_kwargs)` at line 294), so the problem persists across every crash-respawn cycle.

**Fix:** Pass callables that dereference `runtime` at call time rather than capturing the bound method at setup time. For example:

```python
# In _on_ha_started, replace:
read_watermark=data.states_worker.read_watermark,
open_entities_reader=data.states_worker.read_open_entities,

# With:
read_watermark=lambda: data.states_worker.read_watermark(),
open_entities_reader=lambda: data.states_worker.read_open_entities(),
```

Because `data` is the `TimescaledbRecorderData` dataclass and `data.states_worker` is reassigned by the watchdog in-place, the lambda always dereferences the **current** worker. The lambda must be called from the executor context — the existing `hass.async_add_executor_job(read_watermark)` call in `backfill.py:115` already handles that.

### WR-02: `DB_UNREACHABLE_THRESHOLD_SECONDS` is never passed to `retry_until_success` — changing the constant has no effect

**File:** `custom_components/timescaledb_recorder/states_worker.py:112-118`, `custom_components/timescaledb_recorder/meta_worker.py:110-116`

**Issue:** `const.py:35` defines `DB_UNREACHABLE_THRESHOLD_SECONDS = 300.0` as the intended single source of truth for the `on_sustained_fail` timing threshold. The docstring in both workers says the hook fires "when cumulative fail duration crosses `DB_UNREACHABLE_THRESHOLD_SECONDS` (300s default)." However, neither worker imports this constant, and neither passes `sustained_fail_seconds=DB_UNREACHABLE_THRESHOLD_SECONDS` to `retry_until_success`. The decorator uses its own hard-coded default of `300.0` (see `retry.py:31`). The values happen to match today, so behaviour is correct — but the intended tunability is broken. An operator who updates `DB_UNREACHABLE_THRESHOLD_SECONDS` in `const.py` would see no change.

**Fix:** Import and pass the constant in both workers:

```python
# states_worker.py and meta_worker.py — add to const imports:
from .const import DB_UNREACHABLE_THRESHOLD_SECONDS

# In __init__, change:
self._insert_chunk = retry_until_success(
    stop_event=stop_event,
    on_transient=self.reset_db_connection,
    notify_stall=self._stall_hook,
    on_recovery=self._recovery_hook,
    on_sustained_fail=self._sustained_fail_hook,
    sustained_fail_seconds=DB_UNREACHABLE_THRESHOLD_SECONDS,  # add this
)(self._insert_chunk_raw)
```

## Info

### IN-01: Duplicate `datetime.now(timezone.utc)` calls in `_async_initial_registry_backfill`

**File:** `custom_components/timescaledb_recorder/__init__.py:133,144`

**Issue:** `now_iso` and `now` are computed separately — two calls to `datetime.now(timezone.utc)` within a few lines. The two timestamps differ by microseconds. `now_iso` is used for `enqueued_at` strings; `now` is passed to `_extract_*_params` helpers as `valid_from`. The slight skew is harmless but makes the relationship between the two values unclear.

**Fix:** Compute once and derive:

```python
now = datetime.now(timezone.utc)
now_iso = now.isoformat()
```

### IN-02: `read_open_entities` not retry-wrapped — inconsistent with `read_watermark`

**File:** `custom_components/timescaledb_recorder/states_worker.py:406-411`

**Issue:** `read_watermark` is wrapped with `retry_until_success(on_transient=reset_db_connection)` in `__init__` (lines 126-129). `read_open_entities` is not wrapped and not renamed to `_read_open_entities_raw`. If `read_open_entities` encounters a transient psycopg3 error when called from the orchestrator via `hass.async_add_executor_job`, the exception propagates to the orchestrator and causes an orchestrator crash — then the done-callback relaunches it after 5s. This is survivable, but the asymmetry relative to `read_watermark` is inconsistent and means no connection reset happens on failure. A note in the docstring explaining this intentional difference would prevent future confusion.

**Fix:** Either wrap `read_open_entities` the same way as `read_watermark` (preferred for consistency), or add a docstring comment explaining why it intentionally lacks retry wrapping.

### IN-03: Test for `hasattr` fallback branch of `_wait_for_recorder_and_clear` is absent

**File:** `tests/test_init.py` (no specific line — missing test)

**Issue:** `_wait_for_recorder_and_clear` has an explicit guard at `__init__.py:231`:

```python
if not hasattr(ha_recorder, "async_wait_recorder"):
    _LOGGER.debug(...)
    return
```

No test exercises this `not hasattr(...)` branch (early return, no clear call). The two existing tests (`test_wait_for_recorder_and_clear_calls_clear_when_recorder_enabled` and `test_wait_for_recorder_and_clear_does_not_clear_when_recorder_none`) both patch `mock_recorder.async_wait_recorder` to an `AsyncMock`, which means `MagicMock` already has the attribute via auto-creation. The guard is never tested in the negative direction.

**Fix:** Add a test that patches `ha_recorder` as a regular `MagicMock` without setting `async_wait_recorder`, verifies `clear_recorder_disabled_issue` is **not** called, and asserts the coroutine returns without error.

---

_Reviewed: 2026-04-23T00:00:00Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
