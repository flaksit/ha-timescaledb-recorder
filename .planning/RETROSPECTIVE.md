# Project Retrospective

*A living document updated after each milestone. Lessons feed forward into future planning.*

## Milestone: v1.1 — Robust Ingestion

**Shipped:** 2026-04-23
**Phases:** 3 | **Plans:** 29 | **Timeline:** 2 days (2026-04-22 → 2026-04-23) | **Commits:** 205

### What Was Built

- Thread-worker isolation: all DB writes moved to dedicated OS thread; HA event loop fully isolated from DB errors
- psycopg3 migration: asyncpg replaced with sync driver; JSONB/TEXT[] adapters, placeholder syntax ($N→%s), single-connection lifecycle
- Durability stack: bounded RAM buffer (10k, drop-oldest) + file-backed persistent queue (JSON-lines FIFO); zero data loss up to 10+ days DB outage
- HA sqlite backfill: `recorder.history` API, per-entity iteration, chunked writes, interleaved with live flush
- Retry decorator: `retry_until_success` with backoff, `on_recovery` + `on_sustained_fail` hooks wired to repair issues
- Watchdog: async `watchdog_loop` with spawn factories, `_last_exception`/`_last_context` capture, structured notifications
- Full observability: 5 repair issues (`db_unreachable`, `buffer_dropping`, `recorder_disabled`, `states_worker_stalled`, `meta_worker_stalled`); all auto-clear; `strings.json` "issues" section; `persistent_notification` for crash/restart events

### What Worked

- **TDD discipline throughout Phase 3**: writing tests alongside each plan prevented regressions and made the watchdog/hooks wiring verifiable without a live HA instance; 194 tests at completion
- **Decision D-01 (drop SQLERR-03/04)**: recognising mid-milestone that per-row fallback was unnecessary complexity — unified retry+stall is cleaner and simpler; the scope reduction saved ~1 plan worth of work
- **Phase sequencing**: each phase had exactly one dependency; Phase 1 correctness made Phase 2 straightforward; Phase 2 hooks made Phase 3 mechanical
- **Research upfront**: discovering `state_changes_during_period(entity_id=None)` raises `ValueError` before planning Phase 2 avoided a hard-to-debug runtime failure
- **File-backed persistent queue**: choosing JSON-lines append-only FIFO over SQLite avoided locking complexity and made crash-recovery trivial

### What Was Inefficient

- **Traceability table not updated per phase**: Phase 1 and Phase 2 requirements stayed "Pending" in REQUIREMENTS.md until milestone close; required manual sweep at archival time. Update traceability row at plan completion, not milestone close.
- **VERIFICATION.md status not updated after UAT**: `03-VERIFICATION.md` showed `status: human_needed` even after `03-HUMAN-UAT.md` was completed with 3/3 passed; required a manual fix at milestone close. The verifier agent should set `status: complete` when human UAT is confirmed done.
- **STATE.md progress counters stale**: STATE.md showed `completed_plans: 21` and `percent: 72` after all 29 plans were done; counters not updated during Phase 3 execution. GSD executor should update STATE.md progress on every plan completion.

### Patterns Established

- **Worker hook pattern**: `_stall_hook` / `_recovery_hook` / `_sustained_fail_hook` on worker classes + wiring via `retry_until_success` kwargs — replicable for any future worker type
- **Spawn factory pattern**: worker construction via factory fn passed to `__init__.py`; watchdog calls factory to respawn → no circular import, no hard-coded class names
- **`hass.add_job` for fire-and-forget from worker thread**: all repair issue create/clear calls go through `hass.add_job(fn, hass)` — never `async_create_task` or direct call from thread
- **`getattr(thread, "_last_exception", None)` defensive read**: watchdog always uses getattr with default; thread sets attribute only in except block before finally teardown

### Key Lessons

1. **`state_changes_during_period(entity_id=None)` raises `ValueError`** — always iterate per entity; never pass None
2. **`run_coroutine_threadsafe().result()` deadlocks on HA shutdown** — use sentinel + `async_add_executor_job(thread.join)` exclusively
3. **TimescaleDB ≤ 2.17.2 `ON CONFLICT DO NOTHING` bug** — aborts entire batch on first conflict; gate on version ≥ 2.18.1
4. **Watchdog cancel must precede orchestrator cancel in `async_unload_entry`** — watchdog may respawn workers; cancelling it first prevents respawn races during teardown
5. **`hasattr` guard on `async_wait_recorder`** — HA internal API; guard prevents hard failure if removed in future HA version; document explicitly so it's not mistaken for incomplete impl
6. **Update traceability + verification status at completion time, not milestone close** — retroactive sweeps add unnecessary friction

### Cost Observations

- Model mix: sonnet (executor, plans, verifier)
- Sessions: multiple across 2 days
- Notable: Phase 3 was the most test-heavy phase (8 plans, all TDD) and produced the cleanest verification report (8/8 automated, 3/3 human UAT)

---

## Cross-Milestone Trends

| Metric | v1.1 |
|--------|------|
| Phases | 3 |
| Plans | 29 |
| Days | 2 |
| Commits | 205 |
| Source LOC | 3,669 |
| Test LOC | 5,565 |
| Test count | 194 |
| Requirements shipped | 27/29 |
| Requirements dropped | 2 |
