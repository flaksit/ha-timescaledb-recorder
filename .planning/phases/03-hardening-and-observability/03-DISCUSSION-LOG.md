# Phase 3: Hardening and Observability - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-04-22
**Phase:** 03-hardening-and-observability
**Areas discussed:** SQLERR-03/04 reinstatement, Backfill one-shot notifications

Areas NOT selected (left to Claude's Discretion for researcher/planner):
- Watchdog mechanics
- db_unreachable / recorder_disabled detection

## SQLERR-03/04 reinstatement

### Q1 — High-level strategy for SQLERR-03/04?

| Option | Description | Selected |
|--------|-------------|----------|
| A3 Partial — code-bug path only (Recommended) | Keep unified retry for data errors. Add ProgrammingError/SyntaxError → critical log + `schema_drift` repair issue + stop retrying. | |
| A1 Drop both | Keep Phase 2 unified retry as-is. Close SQLERR-03 AND SQLERR-04. Rewrite ROADMAP goals. | ✓ |
| A2 Reinstate both fully | Per-row fallback loop + code-bug halt+repair-issue. Full ROADMAP compliance. | |
| A4 SQLERR-03 only | Per-row fallback, leave code bugs under generic retry. | |

**User's choice:** A1 Drop both.
**Notes:** Consistent with Phase 2 D-07-c principle. Drives subsequent decision to rewrite ROADMAP Phase 3 goal #3 (remove) + #4 (reword around stall notification + worker_stalled repair issue).

### Q2 — If code-bug path enabled: behaviour when ProgrammingError fires?

**User's choice:** n/a (Q1 dropped both).

## worker_stalled repair issue (follow-up after A1)

### Q3 — Add `worker_stalled` repair issue on top of existing stall notification?

| Option | Description | Selected |
|--------|-------------|----------|
| Yes — repair issue + notification (Recommended) | Both paths: one-shot notification (Phase 2) + ongoing repair issue. Auto-clear on first success after stall. | ✓ |
| No — notification only | Keep Phase 2 behaviour as-is. | |
| Repair issue only, drop notification | Replace notification with repair issue. | |

**User's choice:** Yes — repair issue + notification.

### Q4 — How to document the SQLERR-03/04 drop?

| Option | Description | Selected |
|--------|-------------|----------|
| Rewrite ROADMAP goals #3 + #4 (Recommended) | Edit ROADMAP + REQUIREMENTS.md during Phase 3 execution. | ✓ |
| Leave ROADMAP, add CONTEXT note | Silent override. | |

**User's choice:** Rewrite ROADMAP goals.

### Q5 — One repair issue shared across both workers, or one per worker?

| Option | Description | Selected |
|--------|-------------|----------|
| Per-worker: `states_worker_stalled` + `meta_worker_stalled` (Recommended) | Two distinct translation_keys, concurrent-stall visibility. | ✓ |
| Shared `worker_stalled` with dynamic placeholders | Single id, HA dedupes, last stall wins. | |

**User's choice:** Per-worker.

### Q6 — Severity for worker_stalled?

| Option | Description | Selected |
|--------|-------------|----------|
| ERROR (Recommended) | Matches "data stops flowing" urgency. | ✓ |
| WARNING | Matches buffer_dropping; implies auto-recovery (misleading). | |
| CRITICAL | Overkill. | |

**User's choice:** ERROR.

### Q7 — When does worker_stalled clear?

| Option | Description | Selected |
|--------|-------------|----------|
| First successful op after stall (Recommended) | Symmetric with buffer_dropping auto-clear; retry decorator triggers. | ✓ |
| Only on HA restart | No auto-clear. | |

**User's choice:** First successful op after stall.

### Q8 — Where does retry decorator expose stall state for clearing?

| Option | Description | Selected |
|--------|-------------|----------|
| Add on_recovery hook param + stalled flag on decorator (Recommended) | Symmetric with existing on_transient + on_stall. | ✓ |
| Worker tracks stall state externally | More coupling. | |

**User's choice:** on_recovery hook.

## Backfill one-shot notifications

### Q9 — What counts as a 'gap' for backfill-completed-with-gap notification?

| Option | Description | Selected |
|--------|-------------|----------|
| Recorder-retention overrun (Recommended) | Outage duration > HA recorder's purge_keep_days; compute at orchestrator start. | ✓ |
| Any slice fetch exception | Aggregate errored slices. | |
| Per-entity ValueError aggregation | Granular but noisy. | |
| Both retention-overrun + fetch exceptions | Union. | |

**User's choice:** Recorder-retention overrun.

### Q10 — What counts as 'backfill failed entirely'?

| Option | Description | Selected |
|--------|-------------|----------|
| Orchestrator task crashes unhandled (Recommended) | try/except in orchestrator body, notification + re-raise. | |
| Watermark unreadable after N retries | Narrower. | |
| BACKFILL_DONE never sent | Worker-side detection. | |

**User's choice:** (free-text) "Need a notif if backfill fails for other reason than that we have under our control (timescaledb). Actually, this is a general need, for any operation. Not backfill specific. As soon as watchdog needs to step in, user needs to be notified that there was an unrecoverable error. And dev needs to be able to investigate (need backtrace and maybe context somewhere)."

**Notes:** Reframing accepted. Drove D-07 unified `notify_watchdog_recovery` design and D-03 generalization of retry decorator to cover watermark + slice reads + D-04 orchestrator add_done_callback. Drops "backfill failed entirely" as a distinct notification (D-09-b).

### Q11 — Notification dedup / id strategy?

| Option | Description | Selected |
|--------|-------------|----------|
| Per-event static ids (Recommended) | Static notification_id per event. HA replaces on re-fire. | ✓ |
| Timestamped ids | Every event = separate notification. | |
| Single mutating id | One notification, updated body. | |

**User's choice:** Per-event static ids.

### Q12 — Where does the notification helper live?

| Option | Description | Selected |
|--------|-------------|----------|
| New `notifications.py` module (Recommended) | Parallel to `issues.py`. Single module for all helpers. | ✓ |
| Inline in backfill.py / watchdog.py | Copy-paste. | |

**User's choice:** New `notifications.py` module.

## Unified watchdog-recovery notification (follow-up after Q10 reframing)

### Q13 — Unified watchdog-recovery notification shape?

| Option | Description | Selected |
|--------|-------------|----------|
| Single helper `notify_watchdog_recovery` (Recommended) | One function, all components. | ✓ |
| Per-component helpers | Copy-paste between them. | |

**User's choice:** Single helper.

### Q14 — Backtrace: where does it go?

| Option | Description | Selected |
|--------|-------------|----------|
| Both: full traceback → ERROR log; abridged + exc repr → notification body (Recommended) | Signal in UI, full context in HA log. | ✓ |
| Full traceback in notification body | Long tracebacks unwieldy. | |
| Log only, minimal notification | Dev-unfriendly for end users. | |

**User's choice:** Both.

### Q15 — What context to snapshot in context dict?

| Option | Description | Selected |
|--------|-------------|----------|
| Standard set: timestamp, mode, retry_attempt, last_op (Recommended) | Structured schema callers populate. | ✓ |
| Minimal: timestamp + component name only | Simpler but loses debugging hints. | |
| Let each caller decide | Inconsistent UX. | |

**User's choice:** Standard set.

### Q16 — Retry-wrap watermark read + slice reads?

| Option | Description | Selected |
|--------|-------------|----------|
| Yes, both — generalize retry decorator (Recommended) | Make on_transient optional; wrap watermark (with reset hook) + slice reads (without). | ✓ |
| Yes watermark only | Slice errors bubble to orchestrator. | |
| Neither | Rely on orchestrator restart. | |

**User's choice:** Yes, both — generalize retry decorator.

### Q17 — Orchestrator-level crash recovery (async task, not thread)?

| Option | Description | Selected |
|--------|-------------|----------|
| add_done_callback auto-relaunch (Recommended) | Callback inspects Future, relaunches if crash + not cancelled + not stopping. | ✓ |
| Thread-watchdog handles it | Cross-paradigm, awkward. | |
| No orchestrator watchdog | Terminal on crash. | |

**User's choice:** add_done_callback auto-relaunch.

### Q18 — Drop 'backfill failed entirely' notification given auto-retry?

| Option | Description | Selected |
|--------|-------------|----------|
| Drop it (Recommended) | All failure paths now auto-recover via retry or watchdog; unified notify_watchdog_recovery covers unrecoverable cases. | (chosen implicitly via Q10 free-text) |
| Keep as 'backfill_retrying' | Fire on first crash before recovery. | |
| Keep after N orchestrator restart cycles | Watchdog-scoped counter. | |

**User's choice:** Drop (derived from Q10 reframing).

## Claude's Discretion

- Watchdog polling cadence, max-restart cap, thread-recreation factory details, module placement.
- `db_unreachable >5 min` timer implementation.
- `recorder_disabled` detection (startup-only check suggested in D-11).
- Exact translation-key copy for all D-10 entries.
- Whether `_fetch_slice_raw` retry stalls deserve their own repair issue.
- Abridged-traceback line count (10 suggested).
- Context-key population conventions on worker thread attributes.
- Whether STALL_THRESHOLD=5 is promoted to a config option.

## Deferred Ideas

See CONTEXT.md `<deferred>` section. Notable: v2 `is_fixable=True` repair flows, `ha_version_untested` (Phase 2 drop), setup-entry watchdog layer, external metrics export.
