# Phase 2: Durability Story - Discussion Log

> Audit trail only. Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered and the reasoning path.

**Date:** 2026-04-21
**Phase:** 02-durability-story
**Areas discussed:** Backfill orchestration + triggers, Buffer mechanics + overflow signal, Transient retry + DB-healthy detection, Metadata recovery + version gate

## Gray area selection

User selected all 4 gray areas presented: "Backfill orchestration + triggers", "Buffer mechanics + overflow signal", "Transient retry + DB-healthy detection", "Metadata recovery + version gate".

## Backfill orchestration + triggers

### Orchestrator location

Initial options: (a) new backfill.py on event loop; (b) inside worker thread; (c) split async fetch + worker insert.

User asked for clarification on "violates async/thread boundary" since HA should keep heavy work off event loop. Claude explained: `await recorder_instance.async_add_executor_job(...)` yields event loop during sqlite I/O (recorder pool thread); option 2 would force `run_coroutine_threadsafe(fut, hass.loop).result()` in worker thread creating WATCH-02 deadlock on shutdown.

Decision: option (a) with further refinement — orchestrator on event loop, ONLY dispatch (recorder pool does sqlite reads, worker does all transforms).

### Entity set for backfill

Options: (a) live entity_registry filtered; (b) entities open rows; (c) union of both.

Decision: (c) union — captures live tracked entities plus entities removed from HA during outage.

### Chunk delivery mechanism

Initial proposal: BackfillChunk(rows, slice_end_ts, done_fut) passed through queue with orchestrator awaiting done_fut for backpressure.

User challenged: `states` has no UNIQUE constraint; `ON CONFLICT` would fail. Claude verified — `const.py` shows only non-unique `idx_states_entity_time`. Two paths: add UNIQUE INDEX or rely on tight watermark math. User pushed further: "if backfill intermixed in buffer with live events and we crash mid-backfill, gap will never be discovered."

Claude walked through the crash-gap scenario confirming the silent-gap hole. Proposed solution: `recorder_meta` table with `backfill_watermark` updated in same transaction as INSERT.

User sharpened two points:
- "_fetch_and_build_slice never yields" — holds recorder pool thread for full slice duration; all chunks held in memory. Not acceptable for first-install or large gaps.
- "watermark will be max(states.last_updated) anyway" — atomic watermark update adds no value over MAX query.

Claude redesigned: streaming slices (one at a time with await between); drop recorder_meta; rely on MAX query. Late-arrival hole still open. Proposed UNIQUE INDEX + ON CONFLICT DO NOTHING as belt-and-suspenders.

User accepted UNIQUE INDEX, picked 10-minute grace period (not 1h as Claude initially proposed; "5-10min very tolerant for late arriving").

### Slice duration

Final: 5 minutes. 3k-15k rows per slice at HA scale. 7-day worst-case backfill ≈ 2000 slices.

### First-event cutoff

Initial: asyncio.Event set by ingester on first post-reconnection event.

Later simplified: cutoff = t_clear at orchestrator wake. Ingester's `put_nowait` after `clear_and_reset_overflow` naturally captures live events with ts ≥ t_clear.

### First-install catch-up scope

User noted: first-install bulk import needs manual TimescaleDB compression control; shouldn't live in runtime integration. Claude mistakenly referenced `./scripts/backfill_gaps.py` — user corrected: that's a gap-filler in another worktree. Canonical first-install procedure is `../paradise-ha-tsdb/scripts/backfill/backfill.py` (Docker one-shot with cutover + metadata backdate + manual compression).

Decision: Phase 2 runtime backfill skipped on empty hypertable (MAX returns NULL). Log info pointing to the external procedure.

## Buffer mechanics + overflow signal

### Bounded queue implementation

Options: queue.Queue(maxsize=10000) with try/except Full; custom BoundedDropOldestQueue subclass; collections.deque module.

Discussion evolved significantly. User critiqued the "drop-oldest per record" dance: "not very efficient nor desirable to drop record per record. Once we start dropping, this is because we are in trouble. Backfill will kick in anyway and clear the entire queue."

User proposed: "stop queueing on overflow" — once full, flag flips, subsequent events dropped at zero cost.

Decision: stop-queueing-on-overflow via OverflowQueue class with `overflowed: bool` field. Redefines BUF-01 literal wording (drop-oldest → drop-newest); functionally equivalent because queue cleared at recovery.

### Keep queue contents vs clear immediately on overflow

User asked: keep queue or clear immediately?

Claude: keep contents. Worker needs something to retry-flush against to detect DB-healthy transition. Clearing immediately would leave worker idle on `get(timeout=5s)` with no flush attempts, blocking recovery detection.

(This was later reversed in the radical simplification — see below.)

### Repair issue wiring

User rejected Claude's initial "Phase 3 territory" framing: "I don't see the point of doing a partial/other implementation now and then reimplementing something else in Phase 3. Bad phase boundaries! Do it now, as it should be done."

Decision: implement buffer_dropping repair issue + strings.json entry in Phase 2 via new `issues.py` module.

### Worker loop pattern

Initial options: (A) drain-burst on first item; (B) Phase 1 accumulate + tick-flush; (C) A + size trigger.

User spotted Phase 1 latent bug: "worker loop not good yet: there is never a 5s idle period. Events come in at least every second or so. So in the code you showed, buffer never flushed except on MetaCommand."

Claude confirmed: `queue.get(timeout=5)` only raises Empty on sustained idle; on busy HA, flush never fires except on MetaCommand.

Decision: adaptive `get(timeout=remaining)` with explicit last_flush tracking; flush when `now - last_flush >= FLUSH_INTERVAL` OR `len(buffer) >= BATCH_FLUSH_SIZE`.

## Transient retry + DB-healthy detection

### Initial design: was_unhealthy state machine

Multiple design iterations: buffer cap vs stop-draining; reconnect throttling; explicit DB-healthy flag with asyncio.Event signal to orchestrator; mixed state+metadata buffer in single transaction; exponential backoff on reconnect; DataError drops vs retries.

User: "buffer cap not needed. Just stop draining queue as soon as error occurs." Accepted.

User: "Option 2. META-03 interpreted too permissively. Buffer the same way as states. Just the backfill is different." Metadata events MUST be captured during outage.

### Radical simplification mid-area

After detailed multi-iteration design, user stepped back and proposed a full rewrite with YAGNI principles:

> "I want to simplify. Looking at all past questions and discussions, I think we have been over-complicating.
> - yagni
> - don't optimize for the exception scenario
> - OBS-02: remove 'HA version untested'. Multiple versions per month; most have no schema changes.
> - no specific SQL error handling. Simple. So I think all SQLERR-xx requirements can be replaced by a single, simple one"

User provided full pseudo-code with: OverflowQueue (overflow-aware), PersistentQueue (file-backed for metadata), retry_until_success decorator with simple exponential backoff, two separate worker threads.

### Claude's critical review of user's design

Raised 5 concerns:
1. Backfill in worker thread would re-enter event loop via `run_coroutine_threadsafe` — WATCH-02 deadlock.
2. `retry_until_success` catches all exceptions forever → bad row halts worker indefinitely.
3. `sleep(N)` is not interruptible during shutdown.
4. `_STOP` sentinel / shutdown signal missing in worker loops.
5. OverflowQueue replace-last semantics are quirky; delays backfill trigger.

### User responses to concerns

1. Backfill-thread-boundary: user preferred orchestrator pattern; "seems cleaner". Asked for updated pseudo-code showing worker signals event loop, orchestrator fetches + delivers.
2. Retry everything: "Don't complicate to optimize the exception scenario. Drilling down to find the exact record is complicated. Exhaustive list of exceptions is not robust. It's ok to be stalled (notify!); we have a nice fallback scenario: restart HA. Is it possible (v2) to have a HA issue resolution that doesn't restart HA but only our queue and thread?"
3. Interruptible sleep: "Of course. I just wrote pseudo-code. Need correct sleep-method for use in thread."
4. Shutdown signal: "You pick." Decision: `threading.Event` shared across worker + retry decorator + orchestrator.
5. OverflowQueue: user analyzed cascade of "clear queue / re-fill / re-overflow / re-clear" and proposed using `overflowed: bool` flag with `clear_and_reset_overflow` called by orchestrator between steps 5 and 6 (before fetching final batch) to allow ingester to resume before the gap closes. Claude agreed after walking through the race scenarios.

### Refinements on the refined orchestrator pattern

User raised additional concerns after Claude's first refined pseudo-code:
- "backfill_orchestrator() risks to get in a forever loop": live events fill queue during backfill, trigger another backfill cycle, "two steps forward one step back."
- "when queue is full, you silently drop backfill records until worker catches up" → gaps in backfill.
- "no ordering requirement because ON CONFLICT" is WRONG — ordering critical for getting correct watermark.

User proposed: (a) backfill logic in worker; (b) separate backfill_queue with tiny maxsize and blocking put for backpressure, worker drains backfill_queue exclusively until done; (c) clear live_queue ≥5s before last backfill batch so live events flow before gap closes (HA recorder 5s commit interval).

Decision: (b) + (c). backfill_queue(maxsize=2), blocking put gives backpressure; MODE_BACKFILL exclusively drains backfill_queue; `wait_until = slice_end + 5s` sleep in orchestrator before each fetch.

### Separation of concerns

User: "Let event_loop put raw result from recorder_instance.async_add_executor_job() in queue. Chunk not necessary there. Let worker convert and chunk in well-sized batches for insert_many(). Better separation of concerns."

Also corrected Claude's inaccurate comment about "full outage-gap": duration from t_clear not outage start.

Decision: queue carries raw `dict[entity_id, list[HA State]]`. Worker does merge+sort+convert+chunk+insert.

### Flush chunking

User: "self._flush() does insertmany with full buffer? Is that ok if buffer contains 15k records?"

Decision: `_flush` plain-loops in INSERT_CHUNK_SIZE=200 sub-batches; `_insert_chunk` is the retry unit (@retry_until_success wraps it, not whole _flush). Successful sub-batches don't replay.

## Metadata recovery + version gate

### File path for PersistentQueue

Decision: `hass.config.path(DOMAIN, "metadata_queue.jsonl")`.

User noted: integration module should be renamed `timescaledb_recorder` → `timescaledb_recorder` (ha_ prefix belongs only on GitHub repo slug). Breaking change for existing config entries. Bundled as separate Phase 2.5 chore.

### Initial registry backfill

Decision: orchestrator async method called after `meta_queue.join()`. Enumerates registries on event loop (thread-safe). Reuses Phase 1 SCD2 change-detection helpers via `write_item` code path. After completion: subscribe to registry update events.

### Startup sequence

Decision (D-12): open meta queue → start meta worker (drains file) → start states worker → await meta_queue.join() → initial registry backfill → subscribe registry events → subscribe state_changed → register EVENT_HOMEASSISTANT_STARTED listener for orchestrator spawn.

### META-01 re-snapshot on DB-healthy transition

User decision: NOT needed. PersistentQueue ensures no meta event loss during outage. Initial startup backfill covers pre-install / missed cases. DB-healthy transition at runtime: meta worker naturally recovers via retry_until_success on its current file item.

## Scope / requirements housekeeping

### Dropped

- SQLERR-01..05: replaced by single generic retry decorator.
- OBS-02 "HA version untested": user explicitly dropped.
- v1 lookback cap for first-install: first-install out of scope entirely.

### Deferred

- Integration module rename to Phase 2.5 chore.
- v2 in-HA reset-button repair issue (without HA restart).
- Phase 1 REQUIREMENTS.md checkbox ticking (housekeeping).
- FILTER-01 options-flow UI (confirmed v2).

## Claude's Discretion

- Module naming: `overflow_queue.py`, `persistent_queue.py`, `issues.py`, `backfill.py`, `retry.py`.
- StateRow.from_state helper signature.
- JSON dict schema for persistent metadata queue items.
- Notification retry-count threshold (N=5 suggested).
- Initial registry backfill iteration order (areas → labels → entities → devices suggested to match FK-like dependencies).

## Deferred Ideas

Captured in CONTEXT.md `<deferred>` section — see there for details.
