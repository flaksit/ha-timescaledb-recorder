---
phase: 03-hardening-and-observability
plan: 08
subsystem: planning-docs
tags: [docs, scope-decision, requirements, roadmap]
dependency_graph:
  requires: [03-01, 03-02, 03-03, 03-04, 03-05, 03-06, 03-07]
  provides: [updated-requirements-sqlerr-dropped, updated-roadmap-success-criteria]
  affects: [REQUIREMENTS.md, ROADMAP.md]
tech_stack:
  added: []
  patterns: []
key_files:
  created: []
  modified:
    - .planning/REQUIREMENTS.md
    - .planning/ROADMAP.md
decisions:
  - "SQLERR-03 and SQLERR-04 marked DROPPED with strikethrough format and pointer to 03-CONTEXT.md#D-01"
  - "Phase 3 success criteria rewritten: per-row fallback (old #3) removed; code-bug halt (old #4) replaced by unified retry + worker_stalled path; read-path retry coverage added as new #4"
  - "Traceability table updated: 6 Phase 3 requirements marked Shipped (WATCH-01, WATCH-03, OBS-01..04), 2 marked DROPPED (SQLERR-03, SQLERR-04)"
metrics:
  duration: "2 minutes"
  completed: "2026-04-23"
  tasks_completed: 2
  files_modified: 2
---

# Phase 3 Plan 8: Scope Decisions — Requirements and Roadmap Update Summary

**One-liner:** SQLERR-03/04 marked DROPPED in REQUIREMENTS.md; ROADMAP.md Phase 3 success criteria rewritten to reflect unified-retry + worker_stalled architecture per D-01-c and D-09-b.

## What Was Done

This plan recorded Phase 3 scope decisions in the project's source-of-truth documents. No code was written — this is a documentation-only plan.

### REQUIREMENTS.md Changes

#### DROPPED Marker Format (D-01-b)

The two SQLERR entries now use markdown strikethrough on both the requirement ID and original text, followed by a DROPPED label and pointer to the context decision:

```markdown
- ~~**SQLERR-03**~~: ~~DataError / IntegrityError → isolate offending row via single-row fallback loop; log + skip bad row; continue batch~~ **DROPPED** — see [Phase 3 CONTEXT D-01](phases/03-hardening-and-observability/03-CONTEXT.md#D-01). Persistent bad-row failures stall the worker and surface via the `worker_stalled` repair issue + Phase 2 stall notification.
- ~~**SQLERR-04**~~: ~~ProgrammingError / SyntaxError → critical log + repair issue; do not retry (code bug or schema drift)~~ **DROPPED** — see [Phase 3 CONTEXT D-01](phases/03-hardening-and-observability/03-CONTEXT.md#D-01). Persistent code-bug / schema-drift failures follow the same unified path as other errors: retry → stall notification after N=5 → `worker_stalled` repair issue.
```

#### Traceability Table (D-12)

| Requirement | Old Status | New Status |
|-------------|-----------|-----------|
| WATCH-01 | Pending | Shipped |
| WATCH-03 | Pending | Shipped |
| OBS-01 | Pending | Shipped |
| OBS-02 | Pending | Shipped |
| OBS-03 | Pending | Shipped |
| OBS-04 | Pending | Shipped |
| SQLERR-03 | Pending | DROPPED (see 03-CONTEXT.md#D-01) |
| SQLERR-04 | Pending | DROPPED (see 03-CONTEXT.md#D-01) |

All other rows (WORK-*, BUF-*, BACK-*, META-*, SQLERR-01/02/05, WATCH-02) left unchanged.

#### Coverage Footer

Added fourth bullet:
```
- Dropped (scope reduction during phase planning): 2 (SQLERR-03, SQLERR-04 — see [Phase 3 CONTEXT D-01](phases/03-hardening-and-observability/03-CONTEXT.md#D-01))
```

Total count (29) unchanged — DROPPED requirements still count toward total.

### ROADMAP.md Changes

#### Phase 3 Success Criteria (D-01-c + D-09-b)

Final Phase 3 Success Criteria text (verbatim — serves as the "what shipped" record):

```
**Success Criteria** (what must be TRUE):
  1. The HA Repairs UI shows active repair issues for: DB unreachable >5 min (`db_unreachable`), buffer dropping records (`buffer_dropping`, Phase 2), HA recorder disabled (`recorder_disabled`), states worker stalled (`states_worker_stalled`), meta worker stalled (`meta_worker_stalled`); issues clear automatically when conditions resolve
  2. A `persistent_notification` appears when the states or meta worker thread dies and restarts, when the orchestrator async task crashes and is relaunched, and when backfill encounters a recorder-retention gap
  3. Persistent DB write failures surface via `persistent_notification` after N=5 consecutive retries and as a `worker_stalled` repair issue; the issue clears automatically on the first successful operation after recovery. No error-class branching — `DataError`, `IntegrityError`, `ProgrammingError`, and `SyntaxError` all follow this unified path (see [Phase 3 CONTEXT D-01](phases/03-hardening-and-observability/03-CONTEXT.md#D-01))
  4. The retry decorator also wraps read paths (`read_watermark` on the worker connection, `_fetch_slice_raw` on the HA recorder pool); transient read errors retry silently with the same backoff schedule as writes
  5. The watchdog async task detects worker thread death, restarts the thread via spawn factories, and fires a structured `persistent_notification` with the captured `_last_exception` and `_last_context`; unhandled exceptions in worker `run()` are caught by an outer try/except and routed through the watchdog restart path
  6. `strings.json` ships an `"issues"` section with `title` and `description` for every repair issue `translation_key`; no missing-key warnings appear in HA logs
```

Key changes from original:
- Old criterion 3 (per-row fallback loop) — **removed** per D-01
- Old criterion 4 (code-bug critical-log + no-retry) — **replaced** by unified retry + worker_stalled path per D-01
- New criterion 3 describes the unified-retry + stall notification story
- New criterion 4 describes read-path retry coverage (D-03-c)
- "backfill failed entirely" removed from criterion 2 per D-09-b (folded into watchdog recovery)
- Criterion 1 expanded with specific issue_ids

#### Progress Table

Updated Phase 3 row: `0/TBD | Not started` → `0/8 | In progress`

## Context Decision Cross-References

- **D-01** (03-CONTEXT.md): SQLERR-03/04 dropped — Phase 2 D-07-c unified-retry principle extended; no error-class branching; persistent failures stall the worker and surface via worker_stalled + stall notification
- **D-01-b** (03-CONTEXT.md): REQUIREMENTS.md DROPPED markers with D-01 pointer — implemented here
- **D-01-c** (03-CONTEXT.md): ROADMAP.md Phase 3 success criteria rewrite — implemented here
- **D-09-b** (03-CONTEXT.md): "backfill failed entirely" notification dropped — every unrecoverable backfill path now routes through watchdog recovery or retry stall
- **D-12** (03-CONTEXT.md): Requirement remapping — WATCH-01, WATCH-03 delivered by watchdog + run() exception catching; OBS-01..04 delivered by notifications + issues + strings.json; SQLERR-03/04 DROPPED

## Deviations from Plan

**1. [Rule 1 - Bug] Added `#D-01` anchor to requirement bullets and coverage footer**
- **Found during:** Task 1 verification
- **Issue:** The plan's `<interfaces>` section specified link text without the `#D-01` anchor fragment, but the acceptance criterion required >= 3 occurrences of `03-CONTEXT.md#D-01` (with anchor). The bullet lines and coverage footer used `03-CONTEXT.md` without `#D-01`, yielding only 2 matching occurrences (the two traceability table rows).
- **Fix:** Added `#D-01` anchor to both SQLERR bullet links and the coverage footer link.
- **Files modified:** `.planning/REQUIREMENTS.md`
- **Commit:** af08cba (part of Task 1 commit)

## Known Stubs

None — this is a documentation-only plan with no UI or data sources.

## Threat Flags

None — no new network endpoints, auth paths, file access patterns, or schema changes introduced. Documents are internal planning artifacts.

## Self-Check: PASSED

Files confirmed present:
- `.planning/REQUIREMENTS.md` — modified, committed in af08cba
- `.planning/ROADMAP.md` — modified, committed in 87a4d12

Commits confirmed:
- af08cba — docs(03-08): mark SQLERR-03/04 DROPPED; update Phase 3 traceability
- 87a4d12 — docs(03-08): rewrite Phase 3 success criteria; update progress table
