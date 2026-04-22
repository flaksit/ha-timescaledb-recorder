---
phase: 02-durability-story
plan: "02"
subsystem: infra
tags: [home-assistant, issue-registry, repair-issues, strings-json, i18n]

requires:
  - phase: 01-thread-worker-foundation
    provides: DOMAIN constant in const.py, HA helper import patterns

provides:
  - issues.py module with create_buffer_dropping_issue and clear_buffer_dropping_issue helpers
  - strings.json issues.buffer_dropping translation pair (title + description)

affects:
  - 02-04 (OverflowQueue overflow path — calls create_buffer_dropping_issue via hass.add_job)
  - 02-08 (orchestrator recovery path — calls clear_buffer_dropping_issue inline)
  - 02-10 (__init__.py wiring)

tech-stack:
  added: []
  patterns:
    - "HA repair issue thin-wrapper: delegate to ir.async_create_issue / ir.async_delete_issue; callers handle thread-safety themselves (D-10-b vs D-10-c)"
    - "Issue translation key matches strings.json key exactly (_BUFFER_DROPPING_ISSUE_ID = translation_key)"

key-files:
  created:
    - custom_components/ha_timescaledb_recorder/issues.py
  modified:
    - custom_components/ha_timescaledb_recorder/strings.json

key-decisions:
  - "Module named issues.py (plural) per D-10-a — matches HA convention"
  - "No hass.add_job wrapper in issues.py — callers (worker thread) wrap themselves with hass.add_job(create_buffer_dropping_issue, hass) per D-10-b; orchestrator (event loop) calls directly per D-10-c"
  - "ir.IssueSeverity.WARNING accessed via ir alias, not direct import, matching HA internal convention"
  - "strings.json 'issues' section appended after 'options' with two-space indentation; designed for Phase 3 extension (D-10-e)"

patterns-established:
  - "Issue helper pattern: thin module wrapping ir.async_create_issue / ir.async_delete_issue with stable DOMAIN + issue_id constants"

requirements-completed: [BUF-02]

duration: 8min
completed: 2026-04-22
---

# Phase 02 Plan 02: issues.py + strings.json Summary

**HA repair-issue module with buffer_dropping create/clear helpers and matching strings.json translation pair, wiring the BUF-02 overflow surface for Wave 2+ consumers**

## Performance

- **Duration:** ~8 min
- **Started:** 2026-04-22T17:18:00Z
- **Completed:** 2026-04-22T17:26:35Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments

- Created `issues.py` with `create_buffer_dropping_issue` and `clear_buffer_dropping_issue` delegating to `homeassistant.helpers.issue_registry` with `DOMAIN`, `is_fixable=False`, `severity=WARNING`, and `translation_key="buffer_dropping"`
- Added `"issues"."buffer_dropping"` section to `strings.json` with title and description, leaving the extension point clean for Phase 3 additions per D-10-e
- Both helpers are idempotent: HA dedupes creates by issue_id; `async_delete_issue` is a no-op when the issue is absent

## Task Commits

Each task was committed atomically:

1. **Task 1: Create issues.py with buffer_dropping helpers** - `9ed1e00` (feat)
2. **Task 2: Add issues.buffer_dropping section to strings.json** - `f16fa3b` (feat)

**Plan metadata:** (see final docs commit)

## Files Created/Modified

- `custom_components/ha_timescaledb_recorder/issues.py` - New module; two public helpers delegating to `ir.async_create_issue` / `ir.async_delete_issue`; `_BUFFER_DROPPING_ISSUE_ID = "buffer_dropping"` constant ties the module to the strings.json key
- `custom_components/ha_timescaledb_recorder/strings.json` - New top-level `"issues"` key appended after `"options"` with `buffer_dropping` title + description; existing `config` and `options` sections unchanged

## Decisions Made

- No thread-safety wrapper inside `issues.py`: the module only exposes event-loop-safe functions; worker-thread callers wrap with `hass.add_job(create_buffer_dropping_issue, hass)` per D-10-b. This keeps the module simple and the caller's threading intent explicit.
- `ir.IssueSeverity.WARNING` accessed via the `ir` alias, not a direct `from homeassistant.helpers.issue_registry import IssueSeverity` import — matches HA's own internal convention and avoids a second import path for the same module.

## Deviations from Plan

None — plan executed exactly as written.

## Issues Encountered

None.

## Known Stubs

None — the module is a thin wrapper with no placeholder data. The functions are not yet wired to callers (that happens in plans 04, 08, 10) but the module itself contains no stubs.

## Threat Flags

None — `issues.py` introduces no new network surface, auth paths, file access, or schema changes. It delegates entirely to HA's `issue_registry` helper which is already audited by the HA core team.

## Next Phase Readiness

- `issues.py` is importable and fully tested by acceptance criteria; Wave 2 plans (04, 08, 10) can call `create_buffer_dropping_issue` / `clear_buffer_dropping_issue` without re-implementing the HA helper call
- `strings.json` is valid JSON and ready for HA to render the repair issue UI as soon as an issue is raised
- No blockers for subsequent plans

## Self-Check: PASSED

- `custom_components/ha_timescaledb_recorder/issues.py` exists: FOUND
- `custom_components/ha_timescaledb_recorder/strings.json` has `"issues"` key: FOUND
- Commit `9ed1e00` exists: FOUND
- Commit `f16fa3b` exists: FOUND

---
*Phase: 02-durability-story*
*Completed: 2026-04-22*
