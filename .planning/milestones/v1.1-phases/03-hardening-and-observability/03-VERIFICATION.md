---
phase: 03-hardening-and-observability
verified: 2026-04-23T10:30:00Z
status: complete
human_uat_completed: 2026-04-23T20:30:00Z
human_uat_result: 3/3 passed, 0 issues (see 03-HUMAN-UAT.md)
score: 8/8 must-haves verified
overrides_applied: 0
human_verification:
  - test: "Trigger worker thread crash (e.g. inject a TypeError in states_worker._run_main_loop via monkey-patching or a bad DB response) and confirm a persistent_notification appears in the HA UI within WATCHDOG_INTERVAL_S (10s) with the expected title 'TimescaleDB recorder: states_worker restarted' and a traceback excerpt."
    expected: "HA Repairs UI and/or Notification bell shows the recovery notification; a new states_worker thread is alive shortly after; HA log shows full traceback."
    why_human: "End-to-end thread death → watchdog poll → notification → respawn requires a live HA instance; cannot be verified by grep or unit tests alone."
  - test: "Simulate >5 consecutive DB write failures (e.g. bring down TimescaleDB or patch the connection to raise psycopg.OperationalError on every call) and wait >5 minutes; confirm the 'states_worker_stalled' AND 'db_unreachable' repair issues appear in HA Repairs UI. Then restore the DB and confirm both issues clear automatically."
    expected: "After STALL_THRESHOLD=5 failures: 'states_worker_stalled' issue appears in Repairs. After 300s of failure: 'db_unreachable' issue also appears. On first successful flush after restoration: both issues disappear automatically with no user action."
    why_human: "Requires a real HA instance with a controllable TimescaleDB; the 300s timer cannot be fast-forwarded in production."
  - test: "Load the integration without the HA recorder component (set 'recorder: disable: true' in configuration.yaml or unload the recorder integration). Confirm the 'recorder_disabled' repair issue appears in Repairs. Then re-enable the recorder and confirm the issue clears automatically (without restarting the timescaledb_recorder integration)."
    expected: "recorder_disabled repair issue visible in HA Repairs UI immediately. After recorder is re-enabled, the issue clears within a few seconds via async_wait_recorder resolution."
    why_human: "Requires toggling HA recorder integration state in a live HA environment; not reproducible in unit tests."
---

# Phase 3: Hardening and Observability — Verification Report

**Phase Goal:** All thread-worker crashes become observable, self-healing events — HA users see a persistent notification; the integration auto-restarts within WATCHDOG_INTERVAL_S seconds; no silent degradation.
**Verified:** 2026-04-23T10:30:00Z
**Status:** human_needed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | Worker thread death fires a persistent_notification and auto-restarts within WATCHDOG_INTERVAL_S | VERIFIED | `watchdog.py`: `watchdog_loop` polls `is_alive()` at `WATCHDOG_INTERVAL_S=10.0s`, calls `notify_watchdog_recovery` + `asyncio.sleep(5)` + spawns new thread via factory. 14 unit tests pass. |
| 2 | Unhandled exceptions in worker `run()` are captured and routed to watchdog restart | VERIFIED | Both `states_worker.py` and `meta_worker.py`: outer `try/except Exception as err` in `run()` sets `self._last_exception = err` and `self._last_context` before `finally` teardown. Watchdog reads these with `getattr(dead_thread, "_last_exception", None)`. |
| 3 | Stall after N=5 failures creates `worker_stalled` repair issue; clears on recovery | VERIFIED | `retry.py` extended with `on_recovery` hook. `states_worker._stall_hook` calls `hass.add_job(create_states_worker_stalled_issue, self._hass)`. `_recovery_hook` calls `hass.add_job(clear_states_worker_stalled_issue, self._hass)`. Same for `meta_worker`. |
| 4 | DB unreachable >300s creates `db_unreachable` repair issue; clears on recovery | VERIFIED | `retry.py` `on_sustained_fail` hook wired in both workers via `_sustained_fail_hook` → `hass.add_job(create_db_unreachable_issue)`. `_recovery_hook` also calls `clear_db_unreachable_issue`. `time.monotonic()` used for threshold (MEDIUM-7 fix). |
| 5 | `recorder_disabled` issue fires when HA recorder absent; auto-clears when recorder loads | VERIFIED | `__init__.py`: `ha_recorder.get_instance(hass)` wrapped in try/except KeyError; enabled=False branch both call `create_recorder_disabled_issue(hass)` and spawn `_wait_for_recorder_and_clear` which awaits `ha_recorder.async_wait_recorder(hass)` and calls `clear_recorder_disabled_issue`. |
| 6 | Backfill retention gap fires `notify_backfill_gap` notification (None-safe) | VERIFIED | `backfill.py`: `oldest_ts_float = recorder_instance.states_manager.oldest_ts`; `if oldest_ts_float is None: skip`; else if `oldest_recorder_ts > from_`: call `notify_backfill_gap(hass, reason="recorder_retention", ...)`. HIGH-2 None-guard confirmed. |
| 7 | `strings.json` ships `"issues"` section with title+description for all 5 repair issues | VERIFIED | All 5 keys (`buffer_dropping`, `states_worker_stalled`, `meta_worker_stalled`, `db_unreachable`, `recorder_disabled`) present with non-empty title and description. Translation-key round-trip test passes. |
| 8 | SQLERR-03 and SQLERR-04 formally dropped; ROADMAP + REQUIREMENTS updated | VERIFIED | REQUIREMENTS.md: both entries show strikethrough + DROPPED + pointer to 03-CONTEXT.md#D-01. Traceability table: WATCH-01/03 and OBS-01..04 marked Shipped; SQLERR-03/04 marked DROPPED. ROADMAP.md: success criteria rewritten per D-01-c. |

**Score:** 8/8 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `custom_components/ha_timescaledb_recorder/const.py` | STALL_THRESHOLD=5, WATCHDOG_INTERVAL_S=10.0, DB_UNREACHABLE_THRESHOLD_SECONDS=300.0 | VERIFIED | All three constants present with exact values |
| `custom_components/ha_timescaledb_recorder/strings.json` | 5 issues keys with title+description | VERIFIED | All 5 keys present; JSON valid |
| `custom_components/ha_timescaledb_recorder/issues.py` | 8 new create/clear helpers (4 pairs) | VERIFIED | All 8 functions confirmed; issue_id constants used (no string literals); hass-only positional arg |
| `custom_components/ha_timescaledb_recorder/notifications.py` | `notify_watchdog_recovery`, `notify_backfill_gap`, `_format_traceback` | VERIFIED | All 3 functions present; `TracebackException.from_exception` used (not `exc_info=exc`); both are plain `def` not async |
| `custom_components/ha_timescaledb_recorder/retry.py` | Extended with `on_recovery`, `on_sustained_fail`, `sustained_fail_seconds`; `STALL_THRESHOLD` from const | VERIFIED | New kwargs present; `time.monotonic()` used; `time.time()` absent; event-loop constraint documented |
| `custom_components/ha_timescaledb_recorder/states_worker.py` | `_stall_hook`, `_recovery_hook`, `_sustained_fail_hook`, `_last_exception`, `_last_context`, `_run_main_loop`, outer try/except/finally, retry-wrapped `read_watermark` | VERIFIED | All present; safe defaults in `__init__`; `finally` block handles teardown |
| `custom_components/ha_timescaledb_recorder/meta_worker.py` | Same Phase 3 additions as states_worker (with meta issue IDs) | VERIFIED | All hooks, attributes, and structure mirrored; `mode=None` in `_last_context` (no mode machine) |
| `custom_components/ha_timescaledb_recorder/backfill.py` | Gap detection block with None guard, retry-wrapped `_fetch_slice_raw`, `threading_stop_event` kwarg | VERIFIED | All three present; `on_transient=None` for recorder-pool reads |
| `custom_components/ha_timescaledb_recorder/watchdog.py` | `watchdog_loop` (async), `spawn_states_worker`, `spawn_meta_worker`, `_watchdog_respawn`, restart throttle, poll-body guard | VERIFIED | New module; all 4 functions present; `asyncio.sleep(5)` before respawn; `except Exception` poll guard |
| `custom_components/ha_timescaledb_recorder/__init__.py` | HaTimescaleDBData Phase 3 fields, factory-based spawn, watchdog task, orchestrator done_callback, recorder_disabled check, correct unload order | VERIFIED | All fields present; spawn factories used; watchdog cancel+await precedes orchestrator cancel (line 476 vs 484) |
| `.planning/REQUIREMENTS.md` | SQLERR-03/04 DROPPED; WATCH/OBS rows Shipped | VERIFIED | All 8 Phase 3 rows updated correctly |
| `.planning/ROADMAP.md` | Success criteria rewritten; plans list populated | VERIFIED | New criteria present; "per-row fallback" and "critical-level log entry" removed; 8 plans listed |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `issues.py` helpers | `strings.json` issues keys | `translation_key` constant == strings.json key | VERIFIED | `_STATES_WORKER_STALLED_ID = "states_worker_stalled"` used as both `issue_id=` and `translation_key=`; round-trip test confirms no drift |
| `states_worker._stall_hook` | `issues.create_states_worker_stalled_issue` | `hass.add_job` | VERIFIED | `self._hass.add_job(create_states_worker_stalled_issue, self._hass)` in `_stall_hook` |
| `states_worker._recovery_hook` | `issues.clear_states_worker_stalled_issue` + `clear_db_unreachable_issue` | `hass.add_job` | VERIFIED | Both calls present in `_recovery_hook` |
| `meta_worker._stall_hook` | `issues.create_meta_worker_stalled_issue` | `hass.add_job` | VERIFIED | Confirmed in `meta_worker.py` |
| `worker.run()` outer except | `self._last_exception`, `self._last_context` | attribute assignment in except block before finally | VERIFIED | `self._last_exception = err` in `except Exception as err` block; `finally` runs teardown only |
| `watchdog_loop` | `notify_watchdog_recovery` | direct call on event loop | VERIFIED | `notify_watchdog_recovery(hass, component=component, exc=exc, context=ctx)` in `_watchdog_respawn` |
| `watchdog_loop` | dead thread `_last_exception`/`_last_context` | `getattr` defensive read | VERIFIED | `getattr(dead_thread, "_last_exception", None)` and `getattr(dead_thread, "_last_context", {})` |
| `async_setup_entry` | `watchdog_loop` | `hass.async_create_task` | VERIFIED | `data.watchdog_task = hass.async_create_task(watchdog_loop(hass, data))` |
| `orchestrator task` | `_on_orchestrator_done` | `task.add_done_callback` | VERIFIED | `task.add_done_callback(_make_orchestrator_done_callback(hass, data, orchestrator_kwargs))` |
| `_on_orchestrator_done` | `notify_watchdog_recovery(component="orchestrator")` | direct call on event loop | VERIFIED | `notify_watchdog_recovery(hass, component="orchestrator", exc=exc, ...)` in `_on_orchestrator_done` |
| `async_setup_entry` recorder check | `create_recorder_disabled_issue` + `_wait_for_recorder_and_clear` | direct call + `async_create_task` | VERIFIED | Both KeyError and `.enabled=False` branches call the issue creator and spawn the background task |
| `backfill_orchestrator` | `notify_backfill_gap` | direct call on event loop | VERIFIED | `notify_backfill_gap(hass, reason="recorder_retention", ...)` in gap-detection block |
| `retry.py` | `const.STALL_THRESHOLD` | import | VERIFIED | `from .const import STALL_THRESHOLD` at module level; local `_STALL_NOTIFY_THRESHOLD` removed |
| `async_unload_entry` watchdog cancel | precedes orchestrator cancel | line ordering | VERIFIED | `data.watchdog_task.cancel()` at line 476; `data.orchestrator_task.cancel()` at line 484 |
| `backfill_orchestrator` | `threading_stop_event` kwarg | call site in `__init__.py` | VERIFIED | `threading_stop_event=stop_event` passed at orchestrator spawn in `_on_ha_started` |

### Requirements Coverage

| Requirement | Phase | Description | Status | Evidence |
|-------------|-------|-------------|--------|----------|
| WATCH-01 | 3 | Async watchdog task checks `thread.is_alive()`, restarts dead worker, fires `persistent_notification` | SATISFIED | `watchdog_loop` in `watchdog.py`; polls both workers; `_watchdog_respawn` fires `notify_watchdog_recovery` + spawns new thread |
| WATCH-03 | 3 | Worker `run()` catches unhandled exceptions and routes through watchdog restart path | SATISFIED | Outer `try/except Exception as err` in both workers captures exception and sets `_last_exception`; watchdog reads on next tick |
| OBS-01 | 3 | `persistent_notification` for: worker restart, orchestrator crash+relaunch, backfill gap | SATISFIED | `notify_watchdog_recovery` called for states/meta worker restart (via watchdog) and orchestrator crash (via `_on_orchestrator_done`); `notify_backfill_gap` for retention gap |
| OBS-02 | 3 | Repair issues for: DB unreachable >5min, buffer dropping, recorder disabled, worker stalled | SATISFIED | `db_unreachable` via `on_sustained_fail` hook; `buffer_dropping` Phase 2; `recorder_disabled` in `async_setup_entry`; `states_worker_stalled`/`meta_worker_stalled` via `_stall_hook` |
| OBS-03 | 3 | Repair issues clear automatically when condition resolves | SATISFIED | `_recovery_hook` clears `worker_stalled`+`db_unreachable`; `_wait_for_recorder_and_clear` clears `recorder_disabled`; `buffer_dropping` Phase 2 |
| OBS-04 | 3 | `strings.json` `"issues"` section with title+description for every translation_key | SATISFIED | 5 entries confirmed; translation-key round-trip test passes |
| SQLERR-03 | 3 | DROPPED — per-row fallback loop | DROPPED | Formally dropped per D-01; REQUIREMENTS.md updated with strikethrough+DROPPED; unified retry+stall is the recovery story |
| SQLERR-04 | 3 | DROPPED — code-bug critical-log + no-retry | DROPPED | Formally dropped per D-01; same unified path |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `__init__.py` `_wait_for_recorder_and_clear` | ~237 | `hasattr(ha_recorder, "async_wait_recorder")` fallback returns without clearing | Info | Intentional forward-compat guard; documented in code. If HA removes `async_wait_recorder` in a future version, the `recorder_disabled` issue will not auto-clear. This is acceptable — the user can dismiss it manually. NOT a stub. |

No blockers or warnings found. The `hasattr` fallback is a documented design decision (03-07 SUMMARY), not an incomplete implementation.

### Human Verification Required

The following items require a live Home Assistant instance to verify the end-to-end user-visible behaviour. All automated checks (194 tests) pass; these items verify the runtime integration contract.

#### 1. Worker Thread Crash Self-Healing

**Test:** In a running HA instance with this integration loaded, cause `states_worker._run_main_loop` to raise an unhandled exception (e.g., by temporarily breaking the DB schema or patching the module). Wait up to 10 seconds.
**Expected:** A persistent notification with title "TimescaleDB recorder: states_worker restarted" appears in the HA notification bell. The HA log shows a full traceback. Within 15 seconds, a new states_worker thread is running (visible via HA diagnostics or custom log).
**Why human:** Thread death → `is_alive()=False` → `WATCHDOG_INTERVAL_S=10s` poll → `asyncio.sleep(5)` throttle → respawn requires a live HA event loop; cannot be simulated in unit tests without running HA itself.

#### 2. Worker Stall + db_unreachable Repair Issues and Auto-Clear

**Test:** Bring down TimescaleDB (or block the DB port) while the integration is running. Wait until 5 consecutive write failures occur (within ~30s given 5s flush interval), then wait 5 more minutes.
**Expected:** After 5 failures: `states_worker_stalled` repair issue appears in HA Repairs UI. After 300 seconds of sustained failure: `db_unreachable` repair issue also appears. Restore DB connectivity. On first successful flush: both repair issues disappear automatically.
**Why human:** The 300s sustained-fail timer uses `time.monotonic()` and cannot be fast-forwarded; requires a real DB outage scenario.

#### 3. recorder_disabled Issue Auto-Clear

**Test:** Start HA with `recorder: disable: true` in `configuration.yaml`. Load the timescaledb_recorder integration. Confirm `recorder_disabled` appears in Repairs UI. Then re-enable the recorder (remove the disable setting and reload HA config). Do NOT restart the timescaledb_recorder integration.
**Expected:** `recorder_disabled` repair issue visible immediately after setup. After the HA recorder loads (which triggers `async_wait_recorder` resolution in the background task), the issue clears automatically — no user action required.
**Why human:** Requires controlling the HA recorder lifecycle; `async_wait_recorder` behaviour depends on the HA supervisor, not unit-testable.

---

_Verified: 2026-04-23T10:30:00Z_
_Verifier: Claude (gsd-verifier)_
