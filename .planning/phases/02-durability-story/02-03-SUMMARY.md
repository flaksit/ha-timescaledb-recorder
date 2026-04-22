---
phase: 02-durability-story
plan: "03"
subsystem: retry
tags: [retry, decorator, backoff, threading, durability]
dependency_graph:
  requires: []
  provides: [retry_until_success]
  affects: [states_worker, meta_worker]
tech_stack:
  added: []
  patterns: [decorator-factory, stop_event-interruptible-sleep, capped-exponential-backoff]
key_files:
  created:
    - custom_components/ha_timescaledb_recorder/retry.py
  modified: []
decisions:
  - "keyword-only stop_event parameter prevents accidental positional confusion with on_transient"
  - "backoff_schedule and stall_threshold exposed as overridable params for test-speed control (0s backoff in tests)"
  - "notified flag resets and attempts counter resets to 0 on successful call so future stalls re-arm correctly"
metrics:
  duration: "~2m"
  completed: "2026-04-22T17:27:11Z"
  tasks_completed: 1
  tasks_total: 1
---

# Phase 02 Plan 03: retry.py — retry_until_success Summary

One-liner: Shutdown-aware retry decorator with capped (1,5,10,30,60)s backoff, broad Exception catch, on_transient connection-reset hook, and one-shot stall notification at 5 consecutive failures.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Create retry.py with retry_until_success decorator | 500f862 | custom_components/ha_timescaledb_recorder/retry.py |

## Decisions Made

1. **Keyword-only `stop_event`**: The `*` in the signature forces all args to be keyword-only, preventing accidental positional confusion between `stop_event` and `on_transient` at call sites.

2. **Overridable `backoff_schedule` and `stall_threshold`**: Exposed as factory parameters (defaulting to the module constants) so tests can pass `backoff_schedule=(0,)` and `stall_threshold=1` for fast, deterministic coverage without sleeping.

3. **`notified` + `attempts` reset on success**: Both the `notified` flag and the `attempts` counter reset to 0 after each successful call. This means a future stall after recovery will re-arm the `notify_stall` hook, as intended by D-07-f.

4. **`continue` after backoff, not `else` clause**: The `continue` after `stop_event.wait()` returns False makes the retry loop logic explicit — the reader can see the "stay in loop" intent without relying on implicit fall-through.

## Deviations from Plan

None — plan executed exactly as written. The implementation matches the plan's `<action>` block verbatim (preserving all comments, noqa annotations, and the precise backoff index calculation).

## Known Stubs

None.

## Threat Flags

None. `retry.py` is a pure stdlib utility with no network endpoints, auth paths, or file access.

## Self-Check: PASSED

- `custom_components/ha_timescaledb_recorder/retry.py` — found
- Commit `500f862` — found
- All 7 acceptance criteria grep checks passed
- All 4 smoke verification test cases passed
