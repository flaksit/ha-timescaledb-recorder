# Phase 1: Thread Worker Foundation - Context

**Gathered:** 2026-04-19
**Status:** Ready for planning

<domain>
## Phase Boundary

Replace the async, event-loop-resident write path (asyncpg + `async_create_task`) with a dedicated OS thread that owns all psycopg3 DB writes. The HA event loop can never be blocked or crashed by DB errors. The thread starts, runs, and stops correctly across the full HA lifecycle. Existing write functionality (state rows, SCD2 metadata) is preserved — only the execution model changes.

</domain>

<decisions>
## Implementation Decisions

### Schema Setup

- **D-01:** `async_setup_entry` does minimal work at startup — creates queue, starts worker thread, registers event listeners, returns True immediately. No DB calls in `async_setup_entry`. Priority: get `STATE_CHANGED` events buffering in memory as fast as possible.
- **D-02:** Schema DDL runs in the worker thread as its first act, before entering the flush loop. Worker connects psycopg3, runs idempotent DDL (all `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`), then enters the main queue-drain loop.
- **D-03:** If DB is unreachable at worker startup, schema setup fails silently (logged); worker enters the main loop anyway so state events can buffer in the in-memory queue. Schema retry happens on next flush cycle (Phase 2 adds proper transient-error handling; Phase 1 basic handling is sufficient).
- **D-04:** MetadataSyncer.async_start() iterates registries in the event loop (fast, in-memory, no DB), enqueues `MetaCommand` snapshot items, then registers the four registry event listeners. This ensures no registry events are missed and initial snapshot data is queued for the worker to process.

### Queue Payload

- **D-05:** Single `queue.Queue` carries all worker items — preserves event ordering (e.g., entity rename must precede subsequent state writes for the same entity). Two separate queues would require polling and lose ordering guarantees.
- **D-06:** Two frozen dataclasses as queue item types:
  - `StateRow(entity_id, state, attributes, last_updated, last_changed)` — state change payload
  - `MetaCommand(registry, action, registry_id, old_id, params)` — registry event payload; `params` is `None` on `remove` actions
  - `_STOP = object()` — singleton sentinel for graceful shutdown
  - Worker dispatches with `isinstance()` — O(1), negligible vs DB I/O.
- **D-07:** `MetaCommand.registry` is a string literal (`"entity"`, `"device"`, `"area"`, `"label"`). `MetaCommand.action` is `"create"`, `"update"`, or `"remove"`. `params` is the pre-extracted positional tuple (same shape as current `_extract_*_params` return values).
- **D-08:** Registry param extraction (calling `_extract_entity_params`, etc.) happens in the `@callback` handler (event loop, thread-safe), not in the worker thread. This avoids unsafe cross-thread registry access. On `remove`, no registry lookup; params=None.

### Worker Module Structure (Claude's Discretion)

- **D-09:** New `worker.py` module containing `DbWorker` class. `DbWorker` owns: `threading.Thread`, `queue.Queue`, `psycopg.Connection`, schema setup at startup, flush loop, SCD2 dispatch logic.
- **D-10:** `ingester.py` (`StateIngester`) becomes a thin event relay: registers `STATE_CHANGED` listener, applies entity filter, builds `StateRow`, enqueues to shared queue. No async flush, no asyncpg.
- **D-11:** `syncer.py` (`MetadataSyncer`) becomes a thin event relay: registers four registry event listeners, extracts params in `@callbacks`, enqueues `MetaCommand` items. SCD2 DB operations (change detection reads + close/insert writes) move to `DbWorker`.
- **D-12:** `__init__.py` runtime data (`TimescaledbRecorderData`) drops `pool: asyncpg.Pool`; holds `worker: DbWorker` instead. `async_unload_entry` calls `worker.async_stop()` (puts sentinel, awaits `async_add_executor_job(thread.join)`).

### MetadataSyncer Architecture (Claude's Discretion)

- **D-13:** MetadataSyncer retains: field extraction helpers (`_extract_*_params`), change detection helpers (`_extra_changed`, `_entity_row_changed` etc.) — these become sync methods using a psycopg3 cursor passed in from the worker. Event listener registration and `async_start()` lifecycle stay in MetadataSyncer.
- **D-14:** SCD2 write operations (close + insert) and DB reads for change detection are called by the worker thread via `DbWorker._process_meta_command(cmd)`. Worker holds the psycopg3 connection; MetadataSyncer methods receive a cursor/connection as argument.
- **D-15:** `MetadataSyncer._build_extra` / field extraction helpers stay as static or instance methods; they do not touch the DB — safe to call from `@callback` (event loop) for param extraction.

### psycopg3 SQL Adaptation

- **D-16:** All SQL constants in `const.py` migrate from asyncpg `$1, $2` positional syntax to psycopg3 `%s` positional syntax (or `%(name)s` named — stay with `%s` for conciseness, matches existing tuple order).
- **D-17:** Dict attributes column uses `psycopg.types.json.Jsonb(attrs_dict)` wrapper. `list[str]` labels columns adapt to `TEXT[]` automatically via psycopg3's default array adaptation.

### HA Thread-Safety Bridges

- **D-18:** Any HA coroutine called from the worker thread uses `hass.add_job(coro)` (fire-and-forget) or `asyncio.run_coroutine_threadsafe(coro, hass.loop).result()` with a timeout (WORK-05). No `hass.async_*` calls directly from worker thread.
- **D-19:** Graceful shutdown: `async_unload_entry` puts `_STOP` sentinel in queue, then `await hass.async_add_executor_job(thread.join)`. Never `run_coroutine_threadsafe().result()` on shutdown path (deadlock risk, per WATCH-02 constraint).

### Claude's Discretion Summary

- Worker module naming: `DbWorker` in `worker.py`
- Queue item dataclasses defined in `worker.py` (or a new `models.py` — researcher/planner to decide)
- MetadataSyncer change-detection methods receive psycopg3 cursor; signature change from `async def _entity_row_changed(conn) -> bool` to `def _entity_row_changed(cur, ...) -> bool`
- Schema setup on worker startup uses a short-lived setup phase; if DB unreachable, logs warning and continues (Phase 2 adds retry/watchdog)

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project Constraints and Requirements
- `.planning/PROJECT.md` — architecture decisions table, existing v0.3.6 context, constraints
- `.planning/REQUIREMENTS.md` — Phase 1 requirements: WORK-01–05, BUF-03, WATCH-02 with exact specs
- `.planning/ROADMAP.md` — Phase 1 success criteria (5 items to verify against)

### Research Findings (Critical)
- `.planning/research/SUMMARY.md` — Key API constraints: `state_changes_during_period` entity_id=None raises ValueError; `hass.async_*` thread-safety; `run_coroutine_threadsafe().result()` deadlock on shutdown; psycopg3 Jsonb(); strings.json issues section required; TimescaleDB ≥2.18.1 for ON CONFLICT

### Existing Implementation
- `custom_components/timescaledb_recorder/__init__.py` — current async_setup_entry lifecycle to replace
- `custom_components/timescaledb_recorder/ingester.py` — current StateIngester to refactor
- `custom_components/timescaledb_recorder/syncer.py` — current MetadataSyncer to refactor (SCD2 logic reference)
- `custom_components/timescaledb_recorder/const.py` — SQL constants to migrate from $1/$2 to %s
- `custom_components/timescaledb_recorder/schema.py` — async_setup_schema to move to worker startup

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `syncer.py _extract_*_params` helpers: already return tuples in the right shape; reusable as-is in @callbacks for param extraction before enqueue
- `syncer.py _build_extra()`: pure computation (attrs.asdict → JSON), no DB — reusable unchanged
- `syncer.py _extra_changed()`, `_entity_row_changed()` etc.: reusable as sync methods once asyncpg `await conn.fetchrow()` is replaced with psycopg3 cursor `cur.fetchone()`
- `const.py` SQL constants: all DDL stays valid; only placeholder syntax changes ($1→%s)
- `ingester.py _handle_state_changed()`: already a `@callback` (sync) — correct boundary; just needs to enqueue StateRow instead of appending to list

### Established Patterns
- `@callback` decorator for HA event handlers — must be preserved; these are sync and event-loop resident
- `async_track_time_interval` for periodic timer — no longer needed; worker thread has its own flush loop with `queue.get(timeout=5)`
- `entry.async_on_unload` for cleanup registration — pattern stays
- `async_add_executor_job` for crossing thread boundary from HA — correct pattern per WATCH-02

### Integration Points
- `async_setup_entry` → `TimescaledbRecorderData` → `async_unload_entry` lifecycle; pool replaced by worker
- HA event bus: `hass.bus.async_listen(EVENT_STATE_CHANGED, ...)` stays in StateIngester
- Four registry event listeners stay in MetadataSyncer
- `async_add_executor_job(thread.join)` in `async_unload_entry` for graceful shutdown
- `entry.runtime_data` stores the new `TimescaledbRecorderData(worker=DbWorker, ingester=StateIngester, syncer=MetadataSyncer)`

</code_context>

<specifics>
## Specific Ideas

- User explicitly OK with complete rewrite from scratch (not just incremental refactor) if it produces a cleaner result.
- Priority constraint: HA startup must not be slowed. `async_setup_entry` returns True quickly; all DB work deferred to worker thread.
- Single queue (not two separate queues) confirmed after reasoning about event ordering.
- `frozen=True, slots=True` on dataclasses for memory efficiency and immutability.

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within Phase 1 scope.

</deferred>

---

*Phase: 01-thread-worker-foundation*
*Context gathered: 2026-04-19*
