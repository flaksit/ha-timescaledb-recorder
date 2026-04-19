# Project Research Summary

**Project:** ha-timescaledb-recorder v1.1 — Robust Ingestion Milestone
**Domain:** HA custom component — threaded worker + PostgreSQL/TimescaleDB write path
**Researched:** 2026-04-19
**Confidence:** HIGH

## Executive Summary

ha-timescaledb-recorder v1.1 is a production-hardening refactor of an existing Home Assistant custom component. The write path moves from an async, event-loop-resident asyncpg design to a dedicated OS worker thread with a synchronous psycopg3 connection. This mirrors the architecture HA uses for its own recorder integration and is the canonical pattern for HA components that do significant I/O. The driver change from asyncpg to `psycopg[binary]==3.3.3` eliminates async-in-thread complexity entirely: the worker thread owns a single persistent psycopg3 connection, buffers state changes in a bounded `collections.deque`, and flushes in batches every 5 seconds. Startup and recovery backfill fills gaps by reading HA's own SQLite recorder via the `recorder.history` public API.

The recommended architecture funnels all writes through a single worker thread via a `queue.Queue` containing flush and backfill tasks. This eliminates the need for a `threading.Lock` entirely: since flush and backfill execute sequentially in the same thread, there is no concurrent DB access to serialise. The key non-obvious constraint governing the backfill path is that `state_changes_during_period` requires a single `entity_id` per call — multi-entity batch queries via `entity_id=None` raise `ValueError` — and the function must be dispatched via `recorder_instance.async_add_executor_job` from an async context, never called directly from the worker thread. HA async callbacks and issue/notification APIs are not thread-safe; the worker thread must use `hass.add_job` or `asyncio.run_coroutine_threadsafe` for all event-loop-side calls.

The highest-risk areas are the shutdown lifecycle and the thread/async boundary. `run_coroutine_threadsafe(...).result()` will deadlock if called during HA shutdown after the event loop stops accepting work. The correct shutdown pattern — sentinel value in the queue plus `hass.async_add_executor_job(thread.join)` from an event-loop-side stop handler — is documented in HA's own recorder source and must be followed exactly. TimescaleDB ≤ 2.17.2 has a confirmed bug where the first `ON CONFLICT DO NOTHING` conflict aborts the entire batch; the HA TimescaleDB add-on must be ≥ 2.18.1 for reliable dedup against compressed chunks.

## Key Findings

### Recommended Stack

Replace `asyncpg==0.31.0` with `psycopg[binary]==3.3.3` in `manifest.json`. The binary extra provides pre-built wheels for all HA-supported architectures including aarch64 (Raspberry Pi 4/5) and Python 3.14. HA core has no bundled PostgreSQL driver as of 2026.3, so there is no version conflict risk. psycopg3 requires explicit `Jsonb()` wrappers for dict-to-JSONB adaptation; `list[str]` adapts to `TEXT[]` automatically. `executemany` uses pipeline mode automatically since psycopg 3.1.

A single bare `psycopg.connect()` connection owned by the worker thread is simpler and sufficient. A `ConnectionPool(min_size=1, max_size=1)` adds reconnect machinery but introduces pool lifecycle that must be managed off the event loop thread. Cursors are not thread-safe and must never be shared; create a new cursor per operation.

**Core technologies:**
- `psycopg[binary]==3.3.3`: sync PostgreSQL driver — only pg driver designed for threaded use; binary wheel avoids build dependency; pipeline mode automatic in executemany
- `threading.Thread(daemon=True)`: worker isolation from HA event loop — matches HA recorder pattern; daemon=True ensures clean exit if unload is skipped
- `collections.deque(maxlen=10000)`: bounded ring buffer — O(1) append/popleft; drop-oldest is free; no lock needed for single-producer/single-consumer
- `queue.Queue`: task queue for worker — blocking `get()` simplifies worker sleep loop; delivers flush signals and backfill chunks from event loop to worker

### Expected Features

v1.1 adds no user-visible capabilities. The milestone is a write-path robustness upgrade. Feature scope is defined by what fails in v0.3.6 under DB unavailability.

**Must have (table stakes for v1.1):**
- Thread-worker isolation — HA event loop never touches DB; DB errors cannot crash HA
- Bounded buffer with drop-oldest eviction — OOM risk eliminated; ~15–20 min RAM coverage before backfill needed
- HA sqlite backfill on startup and DB-healthy transition — closes gaps after outages up to ~10 days
- Metadata re-snapshot on recovery — idempotent `WHERE NOT EXISTS` restores current registry state
- Watchdog — detects thread death, fires `persistent_notification`, restarts thread
- `issue_registry` repair issues — DB unreachable >5 min, buffer dropping surfaced in HA Repairs UI
- SQL error granularity — transient (retry), data (per-row skip), code bug (critical log + repair issue)

**Correctness requirements (not optional):**
- `include_start_time_state=False` on all backfill calls — avoids re-ingesting boundary rows
- Entity filter applied after `state_changes_during_period` results — function ignores HA recorder exclude filters
- `ON CONFLICT (last_updated, entity_id) DO NOTHING` using column-list syntax, not constraint name — TimescaleDB hypertables do not support `ON CONFLICT ON CONSTRAINT` syntax
- `strings.json` with `"issues"` section — required for `issue_registry` repair issues; missing keys produce log warnings

**Defer (out of scope for v1.1):**
- Per-event WAL / fsync'd append log — HA sqlite is the safety net
- SSL/TLS for TimescaleDB connection
- Direct SQLite reads bypassing recorder API

### Architecture Approach

The architecture has two halves separated by a `queue.Queue`. The HA event loop side is entirely non-blocking: a sync `@callback` filters state events and appends to a bounded deque; async callbacks post flush signals and backfill chunks to the worker queue. The worker thread owns the psycopg3 connection for its full lifetime and drains the queue sequentially. The backfill path is the most complex: it must dispatch `state_changes_during_period` through `recorder_instance.async_add_executor_job` from an async coroutine, per entity, per time chunk, delivering results to the worker queue.

**Major components:**
1. `ingester.py` — `@callback _handle_state_changed`: filter, append to deque, never blocks
2. `buffer.py` — `StateBuffer`: `deque(maxlen=10000)`, drop-oldest with warning and repair issue on overflow
3. `worker.py` — `WorkerThread`: owns psycopg3 connection; drains queue; flush loop; shutdown sentinel
4. `backfill.py` — async orchestrator on event loop; dispatches per-entity per-chunk history API calls; delivers chunks to worker queue
5. `syncer.py` — `MetadataSyncer`: full registry re-snapshot on recovery; SCD2 close+insert for changes; runs in worker thread
6. `schema.py` — DDL setup: unchanged from v0.3.6
7. `errors.py` — error classification: transient / data / code bug
8. `__init__.py` — wiring: setup/unload, watchdog async task, shutdown handler

### Critical Pitfalls

1. **`state_changes_during_period` requires a single `entity_id`; `entity_id=None` raises `ValueError`** — backfill must loop per entity; use `get_significant_states` with `significant_changes_only=False` only if per-call overhead proves measurable
2. **All `hass.async_*` calls are unsafe from the worker thread** — use `hass.add_job` for fire-and-forget positional-arg calls; use thin async wrapper + `asyncio.run_coroutine_threadsafe` for keyword-heavy APIs like `async_create_issue`
3. **`run_coroutine_threadsafe(...).result()` deadlocks on HA shutdown** — stop worker via sentinel in queue; join via `hass.async_add_executor_job(thread.join)` from event-loop-side `EVENT_HOMEASSISTANT_STOP` handler; never call `.result()` without a timeout
4. **Single `queue.Queue` (sequential processing) eliminates `threading.Lock` entirely** — flush and backfill in the same worker thread are sequential by design; the `threading.Lock` described in PROJECT.md is only needed if they run on separate threads; single-queue design avoids all mutex complexity
5. **TimescaleDB ≤ 2.17.2 bug: first `ON CONFLICT` conflict aborts entire batch** — fixed in 2.18.1 (2025-02-10); check user's TimescaleDB add-on version before relying on `ON CONFLICT DO NOTHING` against compressed chunks
6. **`strings.json` with `"issues"` section required for `issue_registry` repair issues** — ship all issue keys (`db_unreachable`, `buffer_dropping`, etc.); missing keys cause log noise

## Implications for Roadmap

The build has a clear dependency chain: buffer.py before worker.py; worker.py before backfill.py, syncer.py, and watchdog; backfill requires thread-safe HA API dispatch patterns established first.

### Phase 1: Thread Worker Scaffolding

**Rationale:** All subsequent phases depend on the worker thread existing with a correct lifecycle and the async/thread boundary correctly established. The most dangerous pitfalls (async_* from thread, shutdown deadlock) live here. Ship a minimal worker that starts, runs, and stops cleanly before adding any real work.

**Delivers:** `WorkerThread` with queue, flush loop stub, sentinel-based shutdown, `EVENT_HOMEASSISTANT_STOP` handler using `async_add_executor_job(thread.join)`, watchdog async task with thread liveness check.

**Addresses:** Thread-worker isolation; watchdog requirement.

**Avoids:** async_* from worker thread; shutdown deadlock; blocking pool ops in event loop.

**Research flag:** No additional research needed — patterns verified against HA recorder source.

### Phase 2: psycopg3 Connection + Flush Path

**Rationale:** Replace asyncpg pool with a single persistent psycopg3 connection owned by the worker thread. Migrate the existing `ha_states` batch insert. This is the hot path and must work before backfill or observability are layered on.

**Delivers:** Working state ingestion via worker thread; `psycopg[binary]==3.3.3` in manifest; `deque(maxlen=10000)` buffer; drop-oldest with warning log; flush SQL migrated to psycopg3 syntax with `Jsonb()` wrappers.

**Uses:** `psycopg[binary]==3.3.3`; `collections.deque`; `queue.Queue` flush signal.

**Implements:** `buffer.py`, `ingester.py` refactor, `worker.py` flush path, `errors.py`.

**Avoids:** Cursor sharing across calls; blocking connection open/close on event loop thread.

**Research flag:** No additional research needed.

### Phase 3: HA SQLite Backfill

**Rationale:** Backfill depends on Phase 1 (worker queue) and Phase 2 (DB write path). It introduces the recorder history API dispatch pattern, which has the entity-per-call constraint and async executor routing requirement that must be implemented correctly.

**Delivers:** `backfill.py` with per-entity per-chunk time-window loop; `include_start_time_state=False`; entity filter applied post-fetch; `INSERT ... ON CONFLICT (last_updated, entity_id) DO NOTHING`; backfill triggered on startup and DB-healthy transition.

**Uses:** `asyncio.run_coroutine_threadsafe` from worker to dispatch fetch coroutine; `recorder_instance.async_add_executor_job(state_changes_during_period, ...)` from async coroutine; results chunked and delivered to worker via queue.

**Implements:** `backfill.py`.

**Avoids:** `entity_id=None` ValueError; calling history API directly from worker thread; holding write lock during HA sqlite read.

**Research flag:** Verify TimescaleDB add-on is ≥ 2.18.1 before finalising dedup SQL. If user is on ≤ 2.17.2, add a startup warning or add-on upgrade requirement to docs.

### Phase 4: Metadata Recovery + SCD2

**Rationale:** Refactor `MetadataSyncer` to run in the worker thread. Re-snapshot pattern on DB-healthy transition is idempotent. Depends on Phase 2 (connection owned by worker).

**Delivers:** `syncer.py` refactored to worker-thread execution; full registry re-snapshot on startup and recovery via `WHERE NOT EXISTS`; SCD2 close+insert for changes during uptime.

**Implements:** `syncer.py`.

**Research flag:** No additional research needed — SCD2 pattern unchanged from v0.3.6.

### Phase 5: Observability + Error Handling

**Rationale:** Add `persistent_notification` and `issue_registry` instrumentation last, once the conditions being reported actually exist in the running system.

**Delivers:** `issue_registry` repair issues for DB unreachable >5 min and buffer dropping; `persistent_notification` for worker death and backfill completion; `strings.json` with all issue keys; SQL error granularity (transient vs data vs code bug).

**Uses:** `hass.add_job` for fire-and-forget; thin async wrapper + `run_coroutine_threadsafe` for keyword-heavy `async_create_issue`; `ir.IssueSeverity.ERROR/WARNING`; `is_fixable=False`.

**Avoids:** Missing `strings.json` issues section; omitting required `is_fixable`/`severity` kwargs (TypeError at runtime); calling `async_create_issue` directly from worker.

**Research flag:** No additional research needed — all API signatures verified against HA core dev branch.

### Phase Ordering Rationale

- Worker lifecycle must be correct before any real work is added — shutdown bugs are the hardest to diagnose after the fact.
- psycopg3 connection and flush establishes the DB write contract that backfill and syncer both depend on.
- Backfill is isolated in its own phase because the async executor dispatch pattern must be validated independently of the flush path.
- Metadata recovery is sequenced after backfill because both share the `ON CONFLICT DO NOTHING` dedup pattern and the DB-healthy transition trigger.
- Observability is last because the conditions it reports only exist once the full write path is operational.

### Research Flags

Phases with standard patterns, no further research needed:
- **Phase 1:** HA recorder source is the reference implementation; sentinel + `async_add_executor_job(thread.join)` confirmed.
- **Phase 2:** psycopg3 type adaptation and executemany pipeline confirmed; manifest.json conventions confirmed.
- **Phase 4:** SCD2 pattern unchanged from v0.3.6; idempotent re-snapshot is well-understood.
- **Phase 5:** All notification/issue API signatures verified against HA core dev branch.

Phases needing a pre-implementation check:
- **Phase 3:** Verify TimescaleDB add-on version ≥ 2.18.1. If user is on ≤ 2.17.2, `ON CONFLICT DO NOTHING` aborts entire batches against compressed chunks — need fallback strategy or add-on upgrade gate.

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | psycopg3 3.3.3 verified on PyPI; HA core requirements.txt checked (no PG driver); binary wheels confirmed for aarch64 + cp314 |
| Features | HIGH | All API signatures from HA core dev branch source; `state_changes_during_period` semantics verified by reading implementation |
| Architecture | HIGH | Threading patterns from stdlib docs; HA executor routing from core.py source; TimescaleDB unique index from official docs |
| Pitfalls | HIGH | All critical pitfalls sourced from HA developer docs, HA PRs, and CPython issue tracker |

**Overall confidence:** HIGH

### Gaps to Address

- **TimescaleDB add-on version:** Research confirms the ≤ 2.17.2 `ON CONFLICT` bug and 2.18.1 fix but cannot determine what version the user's add-on currently ships. Add a startup check or document the minimum version requirement prominently.
- **Bare connection vs pool reconnect:** If implementation uses a bare connection with manual reconnect logic instead of a pool, the `reconnect_failed` callback pattern does not apply. Equivalent error surfacing must come from the watchdog health check loop instead.
- **`queue.Queue` vs `queue.SimpleQueue`:** ARCHITECTURE.md recommends `Queue` for blocking `get()` semantics; PITFALLS.md notes `SimpleQueue` has a GIL assumption. Implementation should pick one and document the rationale — `Queue` is the safer long-term choice.

## Sources

### Primary (HIGH confidence)

- HA core dev branch — `homeassistant/components/recorder/history/__init__.py` — `state_changes_during_period` signature, `entity_id` requirement, filter semantics
- HA core dev branch — `homeassistant/components/recorder/core.py:348-352` — `async_add_executor_job` executor routing
- HA core dev branch — `homeassistant/helpers/issue_registry.py` — `async_create_issue` / `async_delete_issue` signatures
- HA core dev branch — `homeassistant/components/persistent_notification/__init__.py` — `async_create` / `async_dismiss` signatures
- HA core dev branch — `homeassistant/core.py` — shutdown stage timeouts; `hass.add_job` positional-only constraint
- HA core dev branch — `homeassistant/components/history_stats/data.py` — canonical `async_add_executor_job(state_changes_during_period, ...)` call pattern
- [psycopg PyPI](https://pypi.org/project/psycopg/) — 3.3.3 confirmed, Python >=3.10 support
- [psycopg-binary PyPI](https://pypi.org/project/psycopg-binary/) — cp314 + linux_aarch64 wheels confirmed
- [psycopg3 Type Adaptation docs](https://www.psycopg.org/psycopg3/docs/basic/adapt.html) — Jsonb wrapper required; list to TEXT[] automatic
- [TimescaleDB unique index docs](https://www.tigerdata.com/docs/use-timescale/latest/hypertables/hypertables-and-unique-indexes) — must include partitioning column
- [TimescaleDB 2.18.1 release](https://github.com/timescale/timescaledb/releases/tag/2.18.1) — ON CONFLICT DO NOTHING compressed-chunk bug fix confirmed
- [HA Developer Docs: Thread safety with asyncio](https://developers.home-assistant.io/docs/asyncio_thread_safety/) — async_* methods unsafe from worker thread
- [HA PR #45807](https://github.com/home-assistant/core/pull/45807) — run_coroutine_threadsafe shutdown deadlock

### Secondary (MEDIUM confidence)

- TimescaleDB insert cost with secondary unique index — community benchmarks; 20–40% throughput reduction at high write rates (not HA-scale tested)
- [HA SQLite external access risk](https://github.com/home-assistant/home-assistant.io/issues/23276) — direct sqlite3 reads can trigger HA corruption detection
- [CPython issue #113884](https://github.com/python/cpython/issues/113884) — SimpleQueue not thread-safe without GIL

---
*Research completed: 2026-04-19*
*Ready for roadmap: yes*
