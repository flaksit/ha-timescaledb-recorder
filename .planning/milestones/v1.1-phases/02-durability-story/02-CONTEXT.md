# Phase 2: Durability Story - Context

**Gathered:** 2026-04-21
**Status:** Ready for planning

<domain>
## Phase Boundary

Deliver the durability layer on top of Phase 1's thread-worker foundation. The integration survives TimescaleDB outages up to ~10 days without data loss: state changes captured in a bounded RAM queue plus recovered from HA sqlite via runtime backfill; registry metadata persisted to disk so no registry event is ever lost; all transient DB errors retried transparently.

New capabilities in Phase 2:

- Bounded, overflow-aware live state queue with drop-newest semantics.
- Event-loop backfill orchestrator that fills gaps from HA sqlite via `recorder.history.state_changes_during_period`.
- File-persisted metadata queue that survives process restarts.
- Unified retry mechanism for all DB operations (no error-class granularity).
- `UNIQUE INDEX` on `ha_states` enabling `ON CONFLICT DO NOTHING` idempotency.
- Minimal repair-issue wiring for buffer-overflow notification.

Out of scope (deferred or handled elsewhere): first-install bulk import (handled by sibling project `paradise-ha-tsdb/scripts/backfill/backfill.py`); watchdog restart; per-row DataError isolation; version gating; additional repair issues beyond buffer_dropping; integration module rename; entity filter options-flow UI.

</domain>

<decisions>
## Implementation Decisions

### D-01 Queue topology

- **D-01-a:** `live_queue = OverflowQueue(maxsize=10000)`. Ingester `@callback` (event-loop thread) enqueues via `put_nowait`. Worker (states thread) drains.
- **D-01-b:** `backfill_queue = queue.Queue(maxsize=2)`. Orchestrator (event loop) enqueues raw slice dicts via blocking `put` (through `async_add_executor_job`) for natural backpressure. Worker drains in MODE_BACKFILL.
- **D-01-c:** `metadata_queue = PersistentQueue(hass.config.path(DOMAIN, "metadata_queue.jsonl"))`. Ingester registry `@callback`s enqueue via `put_async`. Metadata worker drains.

### D-02 OverflowQueue class

- **D-02-a:** `OverflowQueue(queue.Queue)` with additional `overflowed: bool` field (default False) and `clear_and_reset_overflow()` method.
- **D-02-b:** Overridden `put_nowait`: acquires internal mutex; tries `super().put(item, block=False)`; on `queue.Full` sets `self.overflowed = True` and drops the item silently. Does NOT raise. Ingester's `@callback` thus never raises; enqueue is always O(1) non-blocking.
- **D-02-c:** Redefines BUF-01 semantics: "drop newest once full" rather than literal "drop oldest". Functionally equivalent — queue is cleared at recovery, so dropped-side choice does not affect outcome.
- **D-02-d:** `clear_and_reset_overflow()` drains internal deque under mutex and sets `overflowed = False`. Called exclusively by orchestrator at backfill start.

### D-03 PersistentQueue class

- **D-03-a:** File-backed FIFO. JSON-lines format. Single lock (`threading.Lock`) guards append and rewrite paths.
- **D-03-b:** `put(item)`: sync producer path; acquires lock; appends line + fsync; notifies Condition.
- **D-03-c:** `put_async(item)`: async producer path for event loop; runs blocking put via default executor so event loop does not stall.
- **D-03-d:** `get()`: blocks on Condition until item available; returns in-flight item without removing from disk.
- **D-03-e:** `task_done()`: atomically rewrites file without the front line (tempfile + `os.replace`). Called only after successful DB write.
- **D-03-f:** `join()`: blocks until queue empty and no in-flight item. Used during startup to drain file before registering new listeners.
- **D-03-g:** `wake_consumer()`: helper to `notify_all` under mutex. Called during shutdown to unblock consumer's Condition wait.
- **D-03-h:** Crash safety: if worker crashes between DB write and `task_done`, item replays on next startup. SCD2 write handler is idempotent (change-detection no-ops on unchanged data), so replay is safe.

### D-04 Worker thread for states

- **D-04-a:** `TimescaledbStateRecorderThread` (naming intent; final under new module name after Phase 2.5 rename). Daemon thread, inherits pattern from Phase 1 `DbWorker`.
- **D-04-b:** State machine with three modes: `MODE_INIT` (startup), `MODE_BACKFILL`, `MODE_LIVE`.
- **D-04-c:** Loop entry: `if mode == MODE_INIT or (mode == MODE_LIVE and live_queue.overflowed)`: flush current buffer; `hass.loop.call_soon_threadsafe(backfill_request.set)`; set `mode = MODE_BACKFILL`.
- **D-04-d:** MODE_BACKFILL: `backfill_queue.get(timeout=5)`. Item is either `dict[entity_id, list[HA State]]` raw slice or `BACKFILL_DONE` sentinel. On slice: merge + sort across entities by `last_updated`; convert to `StateRow` via `from_ha_state`; `buffer.extend(...)`; call `_flush(buffer)` then `buffer.clear()`. On `BACKFILL_DONE`: flush residual buffer; set `mode = MODE_LIVE`.
- **D-04-e:** MODE_LIVE: adaptive `get(timeout=remaining)` where `remaining = max(0.1, FLUSH_INTERVAL - (now - last_flush))`. Append StateRow to buffer. Flush when `len(buffer) >= BATCH_FLUSH_SIZE=200` OR `now - last_flush >= FLUSH_INTERVAL=5`. This fixes a Phase 1 latent bug in which `queue.get(timeout=5)` never raised `queue.Empty` on a busy HA (events arrive every <5s) so flush never fired except on MetaCommand arrival.
- **D-04-f:** Shutdown: loop top checks `stop_event.is_set()`. Blocked get unblocks via 5s timeout.

### D-05 Worker thread for metadata

- **D-05-a:** `TimescaledbMetaRecorderThread`. Daemon thread. Owns its own psycopg3 connection (separate from states worker). Two worker threads, two connections in total for the integration.
- **D-05-b:** Loop: `item = metadata_queue.get()` (blocks on Condition); `write_item(item)` (decorated with retry); `metadata_queue.task_done()`.
- **D-05-c:** `write_item(item)` opens a psycopg transaction, dispatches by registry type using existing Phase 1 SCD2 change-detection helpers (`_entity_row_changed`, etc.) and SQL constants (`SCD2_CLOSE_*_SQL`, `SCD2_INSERT_*_SQL`, `SCD2_SNAPSHOT_*_SQL`). No-op when change-detection reports unchanged. Full commit per item.
- **D-05-d:** Shutdown: loop top checks `stop_event.is_set()`. `PersistentQueue.wake_consumer()` called from `async_unload_entry` to unblock the Condition wait.

### D-06 Flush internals

- **D-06-a:** `_flush(rows: list[StateRow])` plain loop: for `i in range(0, len(rows), INSERT_CHUNK_SIZE=200)`: `_insert_chunk(rows[i:i+200])`.
- **D-06-b:** `_insert_chunk` decorated with `@retry_until_success`. Retry granularity is per sub-batch, not per full flush — successful sub-batches do not replay on later failures.
- **D-06-c:** Each `_insert_chunk` call runs one psycopg3 `executemany` under the connection's default autocommit. No explicit `with conn.transaction():` wrapping at `_flush` level — chunking provides finer retry + smaller individual transactions.
- **D-06-d:** INSERT SQL: `INSERT INTO ha_states (entity_id, state, attributes, last_updated, last_changed) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (last_updated, entity_id) DO NOTHING`. Requires D-09-a unique index.

### D-07 Retry mechanism

- **D-07-a:** Single `@retry_until_success(on_transient=reset_db_connection, stop_event=stop_event)` decorator wraps `_insert_chunk` (states) and `write_item` (metadata).
- **D-07-b:** Exponential backoff: `[1, 5, 10, 30, 60]` seconds, capped at 60s, forever. Index advances on each failure, resets to 0 on success.
- **D-07-c:** Catches base `Exception` — no error-class branching. YAGNI: drop SQLERR-01..05 granularity. Single generic retry uniformly handles transient (connection), lock (deadlock/serialization), data (bad row), and code-bug (schema drift) errors alike.
- **D-07-d:** Consequence: a persistently failing row halts its worker forever. Recovery path for users = restart HA. Accepted tradeoff for simplicity.
- **D-07-e:** Interruptible backoff: `stop_event.wait(backoff)` returns True on shutdown; decorator returns early without further retries.
- **D-07-f:** Stall notification: after N=5 consecutive retry attempts (≈100s wall-clock) without success, fire `persistent_notification` via `hass.add_job` to surface in HA frontend. Notification auto-dismisses on first successful operation.
- **D-07-g:** `reset_db_connection()` hook sets `self.connection = None`. `get_db_connection()` lazily reconnects via `psycopg.connect(dsn)`; connect failures bubble up to the decorator for another retry cycle.

### D-08 Backfill orchestrator

- **D-08-a:** Async task on HA event loop. Spawned in `async_setup_entry` via `hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, ...)` → `hass.async_create_task(backfill_orchestrator(...))`. Loops for integration lifetime.
- **D-08-b:** Orchestrator does ONLY dispatch: sqlite reads run in HA recorder pool via `recorder_instance.async_add_executor_job(_fetch_slice_raw, ...)`; transforms (merge/sort/StateRow conversion) run in worker thread. Event-loop CPU work per slice is microseconds (dispatch + await + queue.put).
- **D-08-c:** Trigger: `backfill_request: asyncio.Event` awaited in loop. Set by worker via `hass.loop.call_soon_threadsafe(backfill_request.set)` on MODE_INIT (startup) and on detecting `live_queue.overflowed` in MODE_LIVE.
- **D-08-d:** Sequence per trigger:
  1. `await backfill_request.wait(); backfill_request.clear()`.
  2. `t_clear = utcnow()`.
  3. `live_queue.clear_and_reset_overflow()` — mutex-protected drain + flag reset. Ingester resumes enqueue from this instant; live events with `ts >= t_clear` flow into `live_queue`.
  4. `wm = await hass.async_add_executor_job(read_watermark)`. `read_watermark` executes `SELECT MAX(last_updated) FROM ha_states` via worker's connection.
  5. If `wm is None` (empty hypertable, first install): push `BACKFILL_DONE`, log info pointing to external `paradise-ha-tsdb/scripts/backfill/backfill.py` procedure, continue loop.
  6. `from_ = wm - timedelta(minutes=10)` — late-arrival grace. `cutoff = t_clear`.
  7. Slice loop: `slice_start = from_`; while `slice_start < cutoff`: `slice_end = min(slice_start + 5min, cutoff)`; wait asyncio.sleep until `utcnow() >= slice_end + 5s` (HA recorder commit lag); `raw = await recorder_instance.async_add_executor_job(_fetch_slice_raw, ...)`; `await hass.async_add_executor_job(backfill_queue.put, raw)` (blocking put = backpressure); `slice_start = slice_end`.
  8. Push `BACKFILL_DONE` sentinel.
- **D-08-e:** `_fetch_slice_raw(hass, entities, t_start, t_end)` runs entirely in recorder pool. Iterates per entity_id calling `state_changes_during_period(hass, t_start, t_end, entity_id=eid, include_start_time_state=False)`; returns `dict[entity_id, list[HA State]]`. Never passes `entity_id=None` (API raises `ValueError` per research SUMMARY).
- **D-08-f:** Entity set = union of live `entity_registry.async_entries(er)` filtered by `entity_filter` plus open rows in `dim_entities` (`SELECT entity_id FROM dim_entities WHERE valid_to IS NULL`). Captures currently-tracked entities plus entities removed from HA during outage.
- **D-08-g:** Cascade prevention: `backfill_queue(maxsize=2)` blocking put keeps orchestrator fetch-rate ≤ worker write-rate — backfill_queue never accumulates beyond 2 slices in memory. MODE_BACKFILL exclusively drains `backfill_queue`, so live events in `live_queue` wait until BACKFILL_DONE. Watermark advances per successful sub-batch (writes increase `max(last_updated)`), so subsequent backfill triggers converge monotonically. If `live_queue` does overflow during a very long backfill, the next trigger's window is strictly smaller than the previous.

### D-09 Schema additions

- **D-09-a:** `CREATE UNIQUE INDEX IF NOT EXISTS idx_ha_states_uniq ON ha_states (last_updated, entity_id)` added to `schema.py` `sync_setup_schema`. Hypertables allow unique indexes when the partitioning column (`last_updated`) is included. Enables `ON CONFLICT (last_updated, entity_id) DO NOTHING` on every state INSERT.
- **D-09-b:** No `recorder_meta` table (earlier design iteration rejected — watermark = `SELECT MAX(last_updated)` is authoritative; persisting it separately is redundant).

### D-10 Repair issue wiring

- **D-10-a:** New `issues.py` module. Exposes `create_buffer_dropping_issue(hass)` and `clear_buffer_dropping_issue(hass)`. Thin wrappers around `homeassistant.helpers.issue_registry.async_create_issue` / `async_delete_issue` with `domain=DOMAIN`, `issue_id="buffer_dropping"`, `is_fixable=False`, `severity=IssueSeverity.WARNING`, `translation_key="buffer_dropping"`.
- **D-10-b:** Ingester fires issue on first `overflowed=True` flip via `hass.add_job(create_buffer_dropping_issue, hass)`.
- **D-10-c:** Orchestrator clears issue inside `clear_and_reset_overflow` path (or immediately after) by awaiting `clear_buffer_dropping_issue(hass)` directly (already on event loop).
- **D-10-d:** `strings.json` gains `"issues"` section with `"buffer_dropping"` translation_key. Fields: `title` ("TimescaleDB recorder buffer overflow"); `description` (explains outage, dropped events will be recovered from HA sqlite on DB recovery, no user action required, issue auto-clears).
- **D-10-e:** Phase 3 extends `strings.json` with additional `"issues"` keys (`db_unreachable`, `recorder_disabled`, etc.) and adds more create/clear helpers to `issues.py`. No re-implementation.

### D-11 Overflow logging policy

- **D-11-a:** First drop per outage: single `_LOGGER.warning("live_queue full (10000) — dropping events until DB recovery")`.
- **D-11-b:** Subsequent drops: counter increment only, no log. Avoids log spam during long outages (hours × 50 ev/s).
- **D-11-c:** On recovery (inside `clear_and_reset_overflow`): `_LOGGER.warning("overflow cleared, dropped N events during outage", counter_snapshot)`.

### D-12 Startup ordering in async_setup_entry

The sequence is ordered to guarantee that no new meta events arrive while the initial registry backfill is running (no races on `dim_*` tables):

1. Open `PersistentQueue` at `hass.config.path(DOMAIN, "metadata_queue.jsonl")`.
2. Start metadata worker thread (immediately begins draining any items left from prior outage).
3. Start states worker thread (enters MODE_INIT; will signal `backfill_request` but orchestrator not yet spawned, so no-op).
4. `await meta_queue.join()` — blocks until file is fully drained.
5. `await async_initial_registry_backfill()` — enumerates all four HA registries on event loop; for each entry, invokes the same `write_item` code path used at runtime (SCD2 smart compare via Phase 1 change-detection helpers; close+insert only on diff; no-op on unchanged). Full coverage of events never captured (pre-install) or missed (registry changes while integration disabled).
6. Subscribe to HA registry update events (existing Phase 1 listener registration).
7. Subscribe to HA state_changed event.
8. Register `EVENT_HOMEASSISTANT_STARTED` one-shot listener that spawns `backfill_orchestrator`.

### D-13 Shutdown

- **D-13-a:** `stop_event: threading.Event` shared across worker threads, retry decorator, and orchestrator.
- **D-13-b:** `async_unload_entry`:
  1. `stop_event.set()`.
  2. Cancel orchestrator task; await its completion (standard asyncio.CancelledError handling).
  3. `meta_queue.wake_consumer()` to unblock metadata worker's Condition wait.
  4. `await hass.async_add_executor_job(states_worker.join, 30)`.
  5. `await hass.async_add_executor_job(meta_worker.join, 30)`.
  6. Close both psycopg connections.
- **D-13-c:** Retry decorator interruption: `stop_event.wait(backoff)` in inner loop; returns True on shutdown; decorator exits without further retries.
- **D-13-d:** Joins with timeout=30s per worker. If thread fails to exit within 30s (stuck in a DB call that psycopg cannot interrupt): log critical + continue (matches Phase 1 pattern). Phase 3 watchdog addresses.

### D-14 Requirement remapping

- **BUF-01:** redefined to drop-newest-once-full (functionally equivalent to drop-oldest because queue cleared at recovery).
- **BUF-02:** delivered via D-10 issues.py + strings.json buffer_dropping translation_key.
- **BUF-03:** FLUSH_INTERVAL=5s preserved via adaptive get() timeout in MODE_LIVE.
- **BACK-01..06:** delivered by D-08 orchestrator + D-09 unique index.
- **META-01, META-02, META-03:** reinterpreted. PersistentQueue ensures zero meta-event loss during outage (items survive on disk until task_done). No DB-healthy-transition re-snapshot needed. Initial registry backfill on startup covers pre-install and missed-subscription cases. META-03 narrow reading: loss of change-time precision only for the brief window between HA event firing and `PersistentQueue.put_async` completing; full event loss during outage is NOT accepted.
- **SQLERR-01..05:** dropped. Replaced by D-07 single generic retry decorator + D-07-f stall notification.

### D-15 Deviations from Phase 1 DbWorker design (rewrites)

Phase 1 established `DbWorker` as the single thread owning a single queue + single connection, with `StateRow` and `MetaCommand` multiplexed through one queue. Phase 2 splits this:

- **D-15-a:** One thread per data type: `TimescaledbStateRecorderThread` (states) + `TimescaledbMetaRecorderThread` (metadata). Clean separation; each owns its own connection and retry state.
- **D-15-b:** `DbWorker` class is retired in favor of these two dedicated classes. `ingester.py` and `syncer.py` updated: ingester enqueues StateRow into `live_queue`; syncer's registry `@callback`s enqueue a JSON-serializable dict into `metadata_queue` via `put_async`.
- **D-15-c:** `MetaCommand` dataclass retired; metadata queue items are plain dicts (JSON-serializable for disk storage).
- **D-15-d:** Schema setup continues to run at startup (D-02 from Phase 1). Setup runs in states worker's thread on first `get_db_connection` call — single additive change is D-09-a unique index.

### Claude's Discretion

- Module naming for new files: `overflow_queue.py`, `persistent_queue.py`, `issues.py`, `backfill.py`. Retry decorator lives in `retry.py`.
- Exact `StateRow.from_ha_state(s)` helper signature to be decided by planner — must handle attribute dict snapshot + timezone-aware `last_updated`/`last_changed`.
- Exact `MetaCommand`-equivalent dict schema for `metadata_queue` JSON items — planner to decide between flat dict vs nested-by-registry. Must be JSON-serializable (no datetime objects — use ISO strings).
- Notification threshold N in D-07-f: suggested 5 retries; planner may tune.
- Initial registry backfill iteration order: recommend area → label → entity → device (matches FK-like dependencies from Phase 1 syncer).

</decisions>

<canonical_refs>
## Canonical References

Downstream agents MUST read these before planning or implementing.

### Project constraints and requirements

- `.planning/PROJECT.md` — architecture decisions table, v0.3.6 baseline, constraints
- `.planning/REQUIREMENTS.md` — Phase 2 requirements (see remapping in D-14 above)
- `.planning/ROADMAP.md` — Phase 2 success criteria (6 items)

### Research findings (critical)

- `.planning/research/SUMMARY.md` — API constraints: `state_changes_during_period` requires single `entity_id`; `hass.async_*` not thread-safe from worker thread; `run_coroutine_threadsafe().result()` deadlocks on shutdown; psycopg3 `Jsonb()` wrapper; strings.json `"issues"` section required; TimescaleDB ≥2.18.1 for `ON CONFLICT` on compressed chunks
- `.planning/notes/robust-ingestion-design.md` — original exploration notes driving the milestone

### Phase 1 artifacts

- `.planning/phases/01-thread-worker-foundation/01-CONTEXT.md` — Phase 1 decisions carried forward (queue.Queue serialization, bare psycopg.connect, hass.add_job bridge, graceful shutdown pattern, SCD2 dispatch)

### Existing implementation

- `custom_components/ha_timescaledb_recorder/worker.py` — DbWorker to be split/retired per D-15
- `custom_components/ha_timescaledb_recorder/ingester.py` — StateIngester to be updated (enqueue into OverflowQueue)
- `custom_components/ha_timescaledb_recorder/syncer.py` — MetadataSyncer: reuse SCD2 change-detection helpers; update registry `@callback`s to enqueue into PersistentQueue
- `custom_components/ha_timescaledb_recorder/const.py` — add `BATCH_FLUSH_SIZE`, `INSERT_CHUNK_SIZE`, `FLUSH_INTERVAL`, `LIVE_QUEUE_MAXSIZE`, unique-index DDL
- `custom_components/ha_timescaledb_recorder/schema.py` — add D-09-a unique index
- `custom_components/ha_timescaledb_recorder/__init__.py` — update `async_setup_entry` for D-12 startup ordering; update `async_unload_entry` for D-13 shutdown
- `custom_components/ha_timescaledb_recorder/strings.json` — add `"issues"` section with `buffer_dropping` translation_key
- `custom_components/ha_timescaledb_recorder/manifest.json` — unchanged (psycopg version OK from Phase 1)

### Sibling project reference (for out-of-scope context)

- `../paradise-ha-tsdb/scripts/backfill/backfill.py` — canonical first-install procedure (Docker one-shot with cutover + metadata backdate + manual compression); not invoked by Phase 2 code but log message on empty-hypertable path points users to it
- `./scripts/backfill_gaps.py` — ad-hoc gap-filler tool (separate worktree); relies on `idx_ha_states_uniq` which Phase 2 now delivers

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable assets

- `syncer.py` SCD2 change-detection helpers (`_extra_changed`, `_entity_row_changed`, `_device_row_changed`, `_area_row_changed`, `_label_row_changed`) + field extraction helpers (`_extract_*_params`) reused as-is for metadata `write_item` and initial registry backfill.
- `const.py` SCD2 SQL constants (`SCD2_CLOSE_*_SQL`, `SCD2_INSERT_*_SQL`, `SCD2_SNAPSHOT_*_SQL`, `SELECT_*_CURRENT_SQL`) unchanged, all reused.
- `schema.py sync_setup_schema` gains one line for the new unique index.
- `ingester.py _handle_state_changed` callback already has entity filter wiring; change target from queue.Queue.put_nowait to OverflowQueue.put_nowait.
- Phase 1 `StateRow` dataclass reused; add classmethod `from_ha_state(s: State) -> StateRow`.
- `INSERT_SQL` in const.py: append ` ON CONFLICT (last_updated, entity_id) DO NOTHING` suffix.

### Established patterns

- `@callback` decorator for HA event handlers: preserved.
- `hass.add_job(coro)` fire-and-forget bridge from worker thread: preserved, used for `persistent_notification.async_create` and `ir.async_create_issue` calls from worker thread.
- `hass.async_add_executor_job(sync_fn, ...)` for crossing into worker / recorder pool from event loop: preserved, used for orchestrator's watermark read and `backfill_queue.put` blocking calls.
- `hass.loop.call_soon_threadsafe(callable)` for waking event-loop-side state from a worker thread: used for `backfill_request.set()` and `db_healthy_event.set()`.
- `entry.runtime_data` with `HaTimescaleDBData` dataclass: preserved; fields updated (states_worker, meta_worker, live_queue, meta_queue, orchestrator_task).
- `entry.async_on_unload` for cleanup registration: preserved.

### Integration points

- `async_setup_entry` sequence: worker-start-order updated per D-12 (metadata worker first, then states worker, then drain+backfill, then subscribe).
- `async_unload_entry` sequence: updated per D-13 (stop_event → cancel orchestrator → wake meta consumer → join threads with 30s timeout).
- HA event bus: `hass.bus.async_listen(EVENT_STATE_CHANGED, ...)` subscription moves to step 7 of D-12 (after initial registry backfill).
- `hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, ...)` registered in step 8 of D-12 to spawn orchestrator.
- HA registry listeners: four `async_listen` calls for entity/device/area/label registry events — moved to step 6 of D-12.

</code_context>

<specifics>
## Specific Ideas

- User's original pseudo-code for `OverflowQueue`, `PersistentQueue`, and `retry_until_success` decorator (mid-discussion radical simplification pass) drives the core design. See DISCUSSION-LOG.md for the evolution.
- `retry_until_success` exhaustive list of exceptions explicitly rejected: "Don't complicate to optimize the exception scenario. Drilling down to find the exact record is complicated. Exhaustive list of exceptions is not robust. It's ok to be stalled (notify!); we have a nice fallback scenario: restart HA."
- First-event-cutoff asyncio.Event mechanism (proposed earlier in discussion) superseded by the simpler `t_clear = utcnow()` capture at orchestrator wake: no coordination with ingester, no timeout fallback; ingester simply resumes enqueueing live events at `t_clear` via `clear_and_reset_overflow` and live path handles `ts >= t_clear` via event bus direct.
- Event-loop orchestrator pattern was debated in depth. Worker-thread-driven backfill via `run_coroutine_threadsafe().result()` explicitly rejected due to WATCH-02 deadlock risk on shutdown. Event loop merely dispatches; recorder pool does sqlite reads; worker does transforms. Clean three-way separation.
- Phase 1 latent bug surfaced mid-discussion: worker's `queue.get(timeout=5)` never raises `queue.Empty` on busy HA because events arrive within the 5s window, so flush never fires. Phase 2 includes the adaptive-timeout fix as part of the worker loop rewrite.
- `ha_states` UNIQUE INDEX was dropped then re-introduced: initial agreement on "tight watermark math" plus "in-transaction watermark update" was shown to have a gap for out-of-order / late-arrival events. UNIQUE INDEX + `ON CONFLICT DO NOTHING` is the simpler robust solution. Performance cost at HA scale is negligible.
- `recorder_meta` table (earlier design iteration) was dropped. PostgreSQL transactional visibility on `SELECT MAX(last_updated) FROM ha_states` is authoritative; persisting a watermark separately is redundant under monotonic event flow.

</specifics>

<deferred>
## Deferred Ideas

### Rename `ha_timescaledb_recorder` → `timescaledb_recorder`

User wants the integration module renamed — the `ha_` prefix only belongs on the GitHub repo slug, not inside HA. Rename affects directory, `DOMAIN` constant, manifest.json, strings.json, and is a breaking change for existing config entries (HA keys by domain, no migration API for domain field). Scheduled as a separate Phase 2.5 chore after Phase 2 lands. Phase 2 new modules are created under the current `ha_timescaledb_recorder` module name; Phase 2.5 does a full find+replace + directory move + CHANGELOG entry.

### Repair issue in-HA reset button (v2)

On persistent worker stall (retry forever), current recovery is HA restart. v2 feature: convert `buffer_dropping` (and Phase 3's `db_unreachable`) into `is_fixable=True` repair issues with a custom flow handler that resets the worker queue + reconnects without HA restart. Out of scope for v1.1 durability milestone.

### REQUIREMENTS.md Phase 1 checkboxes unticked

Phase 1 checkboxes for WORK-01..05, WATCH-02, BUF-03 still show `- [ ]` despite Phase 1 being complete per STATE.md and commits. A phase-transition step missed ticking them. Not blocking Phase 2; tick as a separate housekeeping task.

### v2 — FILTER-01 options-flow UI

Entity filter itself implemented since v0.3.6 (active via `entity_filter` in `ingester.py`). FILTER-01 specifically covers the HA options-flow UI that would let users edit the filter without removing + re-adding the integration. Confirmed deferred.

### Phase 3 scope (noted, do not implement in Phase 2)

- WATCH-01, WATCH-03: watchdog task detecting worker thread death + restart.
- OBS-01: `persistent_notification` for one-shot events (worker died, backfill completed with gap, backfill failed).
- OBS-02 remainder: repair issues for `db_unreachable >5min`, `ha_recorder_disabled`. User explicitly dropped the `ha_version_untested` sub-case from OBS-02.
- OBS-03: repair issues cleared when condition resolves.
- OBS-04: extend strings.json `"issues"` section with Phase 3 translation_keys (Phase 2 already establishes the section with `buffer_dropping`).
- SQLERR-03, SQLERR-04: dropped entirely per D-07-c — single generic retry replaces class-specific handling.
- SQLERR-05: dropped entirely. User stated "we're on the last TimescaleDB version"; `ON CONFLICT DO NOTHING` works without version gate on 2.18.1+.

</deferred>

*Phase: 02-durability-story*
*Context gathered: 2026-04-21*
