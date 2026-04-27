---
phase: 03-hardening-and-observability
plan: "01"
subsystem: observability-contracts
tags: [constants, strings, issues, repair-issue, tdd]
dependency_graph:
  requires: []
  provides:
    - STALL_THRESHOLD (const.py)
    - WATCHDOG_INTERVAL_S (const.py)
    - DB_UNREACHABLE_THRESHOLD_SECONDS (const.py)
    - strings.json issues entries for states_worker_stalled, meta_worker_stalled, db_unreachable, recorder_disabled
    - create/clear_states_worker_stalled_issue (issues.py)
    - create/clear_meta_worker_stalled_issue (issues.py)
    - create/clear_db_unreachable_issue (issues.py)
    - create/clear_recorder_disabled_issue (issues.py)
  affects:
    - 03-02 (retry decorator imports STALL_THRESHOLD, calls stall helpers)
    - 03-03 (watchdog imports WATCHDOG_INTERVAL_S)
    - 03-04 (states worker imports stall helpers)
    - 03-05 (meta worker imports stall helpers)
    - 03-06 (db_unreachable + recorder_disabled helpers wired)
tech_stack:
  added: []
  patterns:
    - TDD (RED/GREEN per task)
    - Issue-ID constants as single source of truth (MEDIUM-10 mitigation)
    - Translation-key round-trip test
key_files:
  created:
    - tests/test_const_phase3.py
    - tests/test_strings_phase3.py
  modified:
    - custom_components/timescaledb_recorder/const.py
    - custom_components/timescaledb_recorder/strings.json
    - custom_components/timescaledb_recorder/issues.py
    - tests/test_issues.py
decisions:
  - "Severity ERROR for states_worker_stalled, meta_worker_stalled, db_unreachable; WARNING for buffer_dropping and recorder_disabled — matches HA repair-issue convention (actionable=ERROR, advisory=WARNING)"
  - "Issue-ID constants defined once in issues.py; issue_id= and translation_key= both reference the constant — no string literals at call sites (MEDIUM-10)"
  - "All helpers take only hass as positional arg — required for hass.add_job(helper, hass) from worker threads (RESEARCH Pitfall 2)"
metrics:
  duration: "203 seconds"
  completed_date: "2026-04-23T08:10:30Z"
  tasks_completed: 3
  files_modified: 4
  files_created: 2
  tests_added: 16
---

# Phase 03 Plan 01: Contract Layer (Constants, Strings, Issue Helpers) Summary

Phase 3 Wave 1 foundation: three new constants in const.py, four new issue translation keys in strings.json, and eight new create/clear helper functions in issues.py — all built with TDD and validated by a translation-key round-trip test.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 (RED) | Phase 3 constants — failing tests | 7b6fbbb | tests/test_const_phase3.py |
| 1 (GREEN) | Phase 3 constants to const.py | 998e6f3 | custom_components/timescaledb_recorder/const.py |
| 2 (RED) | strings.json entries — failing tests | ed79e14 | tests/test_strings_phase3.py |
| 2 (GREEN) | Four issue translation keys to strings.json | 8b59742 | custom_components/timescaledb_recorder/strings.json |
| 3 (RED) | Issue helpers — failing tests | 49117f4 | tests/test_issues.py |
| 3 (GREEN) | Four create/clear pairs to issues.py | f144685 | custom_components/timescaledb_recorder/issues.py, tests/test_issues.py |

## Constants Added to const.py

```python
# Phase 3 observability tunables (D-03-d, D-05-c, D-11).
STALL_THRESHOLD: int = 5
WATCHDOG_INTERVAL_S: float = 10.0
DB_UNREACHABLE_THRESHOLD_SECONDS: float = 300.0
```

Inserted after `BACKFILL_QUEUE_MAXSIZE` with block comment explaining each constant's role and the downstream plan that will consume it.

## Translation Keys Added to strings.json

| Key | Title | Severity (in issues.py) |
|-----|-------|------------------------|
| `states_worker_stalled` | TimescaleDB recorder states worker stalled | ERROR |
| `meta_worker_stalled` | TimescaleDB recorder metadata worker stalled | ERROR |
| `db_unreachable` | TimescaleDB database unreachable | ERROR |
| `recorder_disabled` | Home Assistant recorder integration is disabled | WARNING |

The `buffer_dropping` entry is preserved unchanged.

## Issue-ID Constants and Severity Mapping (issues.py)

| Constant | Value | Severity |
|----------|-------|----------|
| `_BUFFER_DROPPING_ISSUE_ID` | `"buffer_dropping"` | WARNING (existing) |
| `_STATES_WORKER_STALLED_ID` | `"states_worker_stalled"` | ERROR |
| `_META_WORKER_STALLED_ID` | `"meta_worker_stalled"` | ERROR |
| `_DB_UNREACHABLE_ID` | `"db_unreachable"` | ERROR |
| `_RECORDER_DISABLED_ID` | `"recorder_disabled"` | WARNING |

Each constant is referenced by both `issue_id=` and `translation_key=` at every call site — no string literals duplicated (MEDIUM-10 mitigation).

## Test Functions (test_issues.py)

13 total test functions in `tests/test_issues.py`:

1. `test_create_buffer_dropping_issue_delegates_to_ir` — regression
2. `test_clear_buffer_dropping_issue_delegates_to_ir` — regression
3. `test_create_states_worker_stalled_issue` — severity=ERROR, issue_id match
4. `test_clear_states_worker_stalled_issue` — delete args match
5. `test_create_meta_worker_stalled_issue` — severity=ERROR, issue_id match
6. `test_clear_meta_worker_stalled_issue` — delete args match
7. `test_create_db_unreachable_issue` — severity=ERROR, issue_id match
8. `test_clear_db_unreachable_issue` — delete args match
9. `test_create_recorder_disabled_issue` — severity=WARNING, issue_id match
10. `test_clear_recorder_disabled_issue` — delete args match
11. `test_all_create_helpers_take_only_hass` — positional-arg shape (hass.add_job compat)
12. `test_all_issue_id_constants_present_in_strings_json` — round-trip (MEDIUM-10)
13. `test_strings_json_has_matching_translation_key` — existing regression

Additional new test files: `tests/test_const_phase3.py` (2 tests), `tests/test_strings_phase3.py` (4 tests). Total new tests: 16.

Full suite: 106 passed (up from 90 at end of Phase 2).

## Deviations from Plan

None — plan executed exactly as written.

## Threat Surface Scan

No new network endpoints, auth paths, file access patterns, or schema changes introduced. All new code is purely additive helper registration functions and string constants. Threat T-03-01-03 (issue_id collision) is mitigated by the round-trip test `test_all_issue_id_constants_present_in_strings_json`.

## Known Stubs

None — all functions are fully wired to HA's `ir.async_create_issue` / `ir.async_delete_issue`. No placeholder values or TODO stubs present.

## Self-Check: PASSED

Files exist:
- custom_components/timescaledb_recorder/const.py — FOUND (STALL_THRESHOLD, WATCHDOG_INTERVAL_S, DB_UNREACHABLE_THRESHOLD_SECONDS)
- custom_components/timescaledb_recorder/strings.json — FOUND (5 issues keys)
- custom_components/timescaledb_recorder/issues.py — FOUND (8 new helpers)
- tests/test_issues.py — FOUND (13 test functions)
- tests/test_const_phase3.py — FOUND (2 test functions)
- tests/test_strings_phase3.py — FOUND (4 test functions)

Commits verified: 7b6fbbb, 998e6f3, ed79e14, 8b59742, 49117f4, f144685
