# Phase 1: Thread Worker Foundation - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-04-19
**Phase:** 01-thread-worker-foundation
**Mode:** discuss
**Areas discussed:** Schema setup approach, Queue payload typing

---

## Schema Setup Approach

| Option | Description | Selected |
|--------|-------------|----------|
| Sync executor job | `hass.async_add_executor_job(setup_schema_sync, dsn)` before starting worker thread | |
| Worker does it at startup | Worker connects, runs DDL, then enters flush loop | ✓ (via user clarification) |
| Async psycopg3 in event loop | Use psycopg3 AsyncConnection in async_setup_entry | |

**User's choice:** Custom direction — user did not select a predefined option but expressed a clear priority constraint: "do as little stuff in setup, so setup is fast and we don't block HA startup." Priority is to get state events buffering in memory ASAP. DB initialization can be deferred. "Refactoring or completely recoding from scratch is ok."

**Notes:** This led to: `async_setup_entry` does only queue creation, thread start, and event listener registration. Worker thread does schema DDL as its first act. MetadataSyncer.async_start() enqueues initial snapshot commands (fast registry iteration in event loop) then registers listeners — no DB calls in async_setup_entry at all.

---

## Queue Payload Typing

| Option | Description | Selected |
|--------|-------------|----------|
| Two frozen dataclasses | StateRow + MetaCommand, worker dispatches with isinstance() | ✓ |
| Tagged tuples | ('state', ...) and ('meta', ...) | |

**User's choice:** Two frozen dataclasses with `frozen=True, slots=True`.

**Notes:** User also asked about two separate queues (state_queue + meta_queue). Reasoning provided: single queue preserves event ordering (entity rename must precede subsequent state writes for same entity); Python has no efficient multi-queue select (polling adds latency/complexity); isinstance() dispatch is negligible vs DB I/O. User accepted single queue decision.

---

## Worker Module Structure (Claude's Discretion)

User requested "cleanest architecture" and delegated this decision. Decision: new `worker.py` module with `DbWorker` class. Ingester and Syncer become thin event relays. Rationale: separation of concerns — DB ownership is explicit, one place to find all thread/queue/connection logic.

## MetadataSyncer Fate (Claude's Discretion)

User requested "cleanest architecture" and delegated this decision. Decision: MetadataSyncer keeps event relay responsibilities + field extraction helpers; SCD2 DB operations (change detection reads + close/insert writes) move to DbWorker. Param extraction happens in @callbacks (event loop, thread-safe) before enqueue.

---

## Claude's Discretion

- Worker module naming: DbWorker in worker.py
- Queue item dataclasses: frozen=True, slots=True; defined in worker.py or models.py (researcher/planner decides)
- MetadataSyncer change-detection methods: signature changes from async(conn) to sync(cur)
- Schema setup resilience: if DB unreachable at worker startup, log warning and continue; state events buffer in queue

## Deferred Ideas

None — discussion stayed within Phase 1 scope.
