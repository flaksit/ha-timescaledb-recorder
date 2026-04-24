# Phase 3: Hardening and Observability - Context

**Gathered:** 2026-04-22
**Status:** Ready for planning

<domain>
## Phase Boundary

Deliver resilience (watchdog-driven self-healing for worker threads and orchestrator async task; all blocking DB/sqlite calls wrapped in unified retry) and observability (persistent_notification on every watchdog-triggered recovery with backtrace + context; repair-issue lifecycle for ongoing conditions; extended strings.json catalogue).

New capabilities in Phase 3:

- Thread watchdog that detects worker thread death and restarts, firing a structured recovery notification.
- Orchestrator crash recovery via `add_done_callback` auto-relaunch, firing the same recovery notification.
- Retry decorator generalized to cover HA sqlite reads (`_fetch_slice_raw`) and the watermark read (`read_watermark`) — not only psycopg writes.
- `worker_stalled` repair issues (per-worker) driven by a new `on_recovery` hook on the retry decorator; auto-clear on first successful operation after stall.
- Recorder-retention-overrun gap detection at orchestrator wake-up, fires `backfill_gap` one-shot notification.
- New `notifications.py` module providing the single `notify_watchdog_recovery(hass, component, exc, context)` entry point + backfill-gap helper.
- Extended `issues.py` + `strings.json` `"issues"` section with the full Phase 3 translation-key catalogue.

Out of scope / DROPPED / deferred: SQLERR-03 (per-row fallback loop) — dropped; SQLERR-04 (code-bug repair issue + no-retry) — dropped (rationale: Phase 2 D-07-c principle extended to reads; unified retry + stall notification + `worker_stalled` repair issue is the recovery story for persistent failures, including code bugs / schema drift). `ha_version_untested` sub-case of OBS-02 — dropped (Phase 2 already confirmed). Distinct "backfill failed entirely" notification — dropped (folded into `notify_watchdog_recovery`, since every unrecoverable backfill path now routes through watchdog-induced orchestrator relaunch or retry stall). Is-fixable repair flows (reset-from-HA-UI) — deferred to v2.

</domain>

<decisions>
## Implementation Decisions

### D-01 SQLERR-03 and SQLERR-04 dropped

- **D-01-a:** Phase 2 D-07-c principle (unified retry, no error-class branching) extended to Phase 3. No per-row fallback loop for `DataError` / `IntegrityError`; no special code-bug halt for `ProgrammingError` / `SyntaxError`. A persistently failing row or SQL statement stalls its worker; stall is surfaced via the `worker_stalled` repair issue (D-02) + the existing Phase 2 D-07-f persistent_notification. User recovers by fixing the condition (schema, bad data, etc.) and restarting HA.
- **D-01-b:** REQUIREMENTS.md marks `SQLERR-03` and `SQLERR-04` as `DROPPED` with a pointer to `.planning/phases/03-hardening-and-observability/03-CONTEXT.md#D-01`.
- **D-01-c:** ROADMAP.md Phase 3 goal text and Success Criteria list updated: success criterion #3 (per-row fallback) removed; success criterion #4 (code-bug repair issue, no retry) rewritten to "persistent DB write failures surface via `persistent_notification` after N=5 retries and as a `worker_stalled` repair issue; issue clears automatically on first successful operation after recovery". Edit happens as part of Phase 3 execution (doc plan).

### D-02 `worker_stalled` repair issue

- **D-02-a:** Per-worker issue_ids: `states_worker_stalled` and `meta_worker_stalled`. Each ships its own translation_key in strings.json. Two distinct entries in `issues.py`:
  - `create_states_worker_stalled_issue(hass)` / `clear_states_worker_stalled_issue(hass)`
  - `create_meta_worker_stalled_issue(hass)` / `clear_meta_worker_stalled_issue(hass)`
- **D-02-b:** Severity: `ir.IssueSeverity.ERROR`, `is_fixable=False`. Issues appear in HA Repairs UI with red treatment.
- **D-02-c:** Clearing semantics: first successful operation after the stall threshold was crossed triggers the clear. Matches the auto-recovery narrative communicated to users. No manual dismiss required.
- **D-02-d:** Fire/clear are bridged from the worker thread to the event loop via `hass.add_job(create_*_stalled_issue, hass)` / `hass.add_job(clear_*_stalled_issue, hass)` — same bridge used for persistent_notification in Phase 2.

### D-03 Retry decorator gains `on_recovery` hook + generalization for reads

- **D-03-a:** `retry_until_success` signature extended. Add parameter:
  - `on_recovery: Callable[[], None] | None = None` — invoked exactly once after a successful call that immediately follows a stall threshold crossing.
  The decorator tracks a `stalled: bool` state inside its closure (per wrapped call site). On each failure, attempt counter increments; when counter crosses `STALL_THRESHOLD` (N=5), `on_stall` fires and `stalled = True`. On first successful call where `stalled` is True, `on_recovery` fires and `stalled = False`. Both hooks are best-effort: exceptions raised inside the hooks are logged and swallowed so they never abort the retry loop.
- **D-03-b:** `on_transient` parameter made optional (default `None`). When `None`, the decorator skips the transient-reset step and proceeds straight to backoff. Enables wrapping call sites where there is no owned connection to reset (notably `_fetch_slice_raw` which reads through the HA recorder pool — HA manages that session).
- **D-03-c:** Reads wrapped by the decorator:
  - `read_watermark` (psycopg `SELECT MAX(last_updated)` on worker connection) — uses `on_transient=reset_db_connection` identical to `_insert_chunk`.
  - `_fetch_slice_raw` (HA recorder sqlite via `state_changes_during_period` in recorder pool) — uses `on_transient=None`. Runs inside `recorder_instance.async_add_executor_job`, so the retry loop consumes a recorder-pool thread during backoff; interruptible via shared `stop_event`.
- **D-03-d:** `STALL_THRESHOLD` constant = 5 (matches Phase 2 D-07-f). Lives in `const.py`. Backoff schedule unchanged: `[1, 5, 10, 30, 60]`, capped at 60s, forever.
- **D-03-e:** Decorator state is per wrapped function, not global. Each retry-wrapped call site (states `_insert_chunk`, meta `write_item`, `read_watermark`, `_fetch_slice_raw`) has independent `stalled` state. Concurrent stalls on multiple sites are possible and expected.

### D-04 Orchestrator crash recovery

- **D-04-a:** `backfill_orchestrator` continues to be launched via `hass.async_create_task(backfill_orchestrator(...))`. The returned `Task` is stored on `entry.runtime_data` and given an `add_done_callback(_on_orchestrator_done)`.
- **D-04-b:** `_on_orchestrator_done(task: asyncio.Task)` inspects the finished task:
  1. `if task.cancelled(): return` — normal shutdown path.
  2. `if stop_event.is_set(): return` — graceful shutdown already in progress.
  3. `exc = task.exception()`. If `exc is None`: orchestrator exited cleanly (current code exits only on cancellation, so this path is defensive).
  4. If `exc is not None`:
     - Call `notify_watchdog_recovery(hass, component='orchestrator', exc=exc, context={'at': ISO_now})`.
     - Schedule a relaunch: `new_task = hass.async_create_task(backfill_orchestrator(...))` and `new_task.add_done_callback(_on_orchestrator_done)` recursively.
     - Replace the reference on `entry.runtime_data` so `async_unload_entry` cancels the live task.
- **D-04-c:** Relaunching re-enters MODE_BACKFILL convergence (worker flush state unchanged; orchestrator wakes, recomputes watermark, pushes slices). No data loss from crash — unique index + `ON CONFLICT DO NOTHING` absorbs duplicates if the crash happened mid-slice.
- **D-04-d:** No relaunch cap: indefinite auto-recovery is acceptable because every relaunch fires a `notify_watchdog_recovery` notification (visible to user) and a traceback to the HA log. Restart storms surface as notification storms.

### D-05 Thread watchdog

- **D-05-a:** New `watchdog.py` module. Exposes a single async coroutine `watchdog_loop(hass, runtime)` spawned as an asyncio task during `async_setup_entry`, parallel to `backfill_orchestrator`. Stored on `entry.runtime_data.watchdog_task`.
- **D-05-b:** Watchdog covers both worker threads (`states_worker`, `meta_worker`). Orchestrator crash recovery does NOT go through the watchdog — it uses the `add_done_callback` path in D-04 (cleaner for async Tasks than polling from another async task).
- **D-05-c:** Claude's Discretion (researcher/planner finalizes): polling cadence (suggestion: 10s); max-restart cap (suggestion: none — see D-04-d); thread-recreation details (new `Thread()` object with the SAME shared queues and the SAME `stop_event`, since `stop_event` is owned by `async_unload_entry` lifetime; the old `Thread` object is discarded, the new one takes its place on `entry.runtime_data`); whether the watchdog owns the `Thread` objects directly or delegates to a `spawn_states_worker` / `spawn_meta_worker` factory.
- **D-05-d:** On detecting `thread.is_alive() is False` AND `not stop_event.is_set()`:
  1. Extract the stored exception from the dead thread (D-06).
  2. Call `notify_watchdog_recovery(hass, component='states_worker'|'meta_worker', exc=exc, context={'at': ISO_now, 'mode': thread.mode if hasattr else None, 'retry_attempt': decorator counter if exposed, 'last_op': last recorded op})`.
  3. Replace the dead `Thread` with a new one using the same factory; start it.
- **D-05-e:** `async_unload_entry` cancels `watchdog_task` before joining worker threads so watchdog does not attempt a restart mid-shutdown.

### D-06 WATCH-03 — worker `run()` catches unhandled exceptions

- **D-06-a:** Both `TimescaledbStateRecorderThread.run` and `TimescaledbMetaRecorderThread.run` wrap their main loop in an outer `try/except Exception as err:` block.
- **D-06-b:** On exception: `_LOGGER.error('%s died with unhandled exception', self.name, exc_info=True)`; record the exception on `self._last_exception = err`; record the last known state (mode, retry counter, last op) on `self._last_context: dict`. Then fall through `run()` naturally — thread exits, `is_alive()` becomes False, watchdog picks up on the next tick.
- **D-06-c:** Watchdog reads `self._last_exception` and `self._last_context` off the dead thread object when composing the recovery notification.
- **D-06-d:** Expected exceptions (base `Exception` caught inside retry decorator) do NOT trigger this path — only unhandled exceptions outside the retry scope (e.g., bug in worker state-machine logic, uncaught `AttributeError` in `StateRow.from_ha_state`, failure in schema setup without retry wrapping). The retry loop itself handles persistent DB errors via stall notification (D-02, D-03).

### D-07 Unified watchdog-recovery notification

- **D-07-a:** New `notifications.py` module. Exposes:
  - `notify_watchdog_recovery(hass, component: str, exc: BaseException, context: dict | None = None) -> None`
  - `notify_backfill_gap(hass, reason: str, details: dict | None = None) -> None`
  Module-level `_LOGGER`. Helpers only; all HA-specific imports localized here.
- **D-07-b:** `notify_watchdog_recovery` behaviour:
  1. `_LOGGER.error('%s restarted after unhandled exception: %s', component, exc, exc_info=True)` — ensures full traceback lands in `home-assistant.log` for grep-based investigation.
  2. Build markdown message body:
     ```
     **Component:** {component}
     **Exception:** `{exc.__class__.__name__}: {exc}`
     **At:** {context.at}
     **Context:**
     - mode: {context.mode}
     - retry_attempt: {context.retry_attempt}
     - last_op: {context.last_op}
     {additional component-specific context keys as `- key: value`}

     **Traceback (last 10 lines):**
     ```
     {abridged traceback via traceback.format_exception(exc)[-10:]}
     ```

     See `home-assistant.log` for the full traceback.
     ```
  3. `persistent_notification.async_create(hass, message=body, title=f'TimescaleDB recorder: {component} restarted', notification_id=f'timescaledb_recorder.watchdog_{component}')`. HA dedupes by id — repeated restarts of the same component replace the previous notification; distinct components accumulate distinct notifications.
- **D-07-c:** Context dict schema (standard keys; callers populate what they have):
  - `at: str` — ISO-8601 UTC timestamp (required).
  - `mode: str | None` — `'INIT' | 'BACKFILL' | 'LIVE'` for states worker; unused for meta worker (None); unused for orchestrator (None).
  - `retry_attempt: int | None` — current retry counter, if the exception fired while inside the retry decorator.
  - `last_op: str | None` — `'insert_chunk' | 'write_item' | 'fetch_slice' | 'read_watermark' | 'schema_setup' | 'unknown'`.
  Orchestrator extras (merged in by `_on_orchestrator_done`): `slice_start`, `slice_end`, `entity_count` when available.
- **D-07-d:** Both helpers are safe to call from the event loop directly. Workers bridge via `hass.add_job(notify_watchdog_recovery, hass, component, exc, context)` when invoking from a thread — but in practice the watchdog itself runs on the event loop, so direct calls dominate.

### D-08 Backfill-gap detection

- **D-08-a:** Gap semantics = recorder-retention overrun. At the top of each orchestrator backfill cycle, after reading the watermark `wm`:
  1. Let `needed_from = wm - timedelta(minutes=10)` (the late-arrival grace boundary established by Phase 2 D-08-d).
  2. Determine the oldest available timestamp in HA's recorder sqlite: `oldest_recorder_ts = await recorder_instance.async_add_executor_job(_fetch_oldest_recorder_ts)`. Implementation reads via HA recorder API (exact call Claude's Discretion — likely a direct `States` table query using the recorder session). If the recorder has no data / integration disabled, treat as `oldest_recorder_ts = utcnow()` which makes the entire window a gap.
  3. If `oldest_recorder_ts > needed_from`: compute `gap = (needed_from, oldest_recorder_ts)`. Fire `notify_backfill_gap(hass, reason='recorder_retention', details={'window_start': needed_from.isoformat(), 'window_end': oldest_recorder_ts.isoformat(), 'duration_minutes': int((oldest_recorder_ts - needed_from).total_seconds() // 60)})`. Adjust the effective `from_ = oldest_recorder_ts` to skip the unreachable prefix and proceed with the slice loop for what IS available.
  4. Fires once per backfill run. `notification_id='timescaledb_recorder.backfill_gap'`.
- **D-08-b:** `notify_backfill_gap` composes markdown body:
  ```
  Backfill could not recover events in the range **{window_start} — {window_end}** ({duration_minutes} minutes) because they were purged from Home Assistant's recorder before the TimescaleDB database became reachable.

  This typically happens when `recorder.purge_keep_days` is shorter than the TimescaleDB outage duration. Consider increasing `recorder.purge_keep_days` to at least match your expected worst-case outage duration.
  ```
  Title: `'TimescaleDB recorder: backfill gap detected'`. `notification_id='timescaledb_recorder.backfill_gap'`.
- **D-08-c:** Gap detection runs BEFORE the slice loop, not after. Slice-level fetch errors are handled by D-03-c retry wrapping — they never produce a gap notification (they retry until success, or stall and raise `worker_stalled` on the orchestrator/recorder-pool call site if we wire the same `on_stall`/`on_recovery` hooks — Claude's Discretion for researcher/planner on whether `_fetch_slice_raw` deserves its own stall repair issue).

### D-09 OBS-01 scope redefined

- **D-09-a:** OBS-01 one-shot notifications now consist of:
  1. `notify_watchdog_recovery(component='states_worker', ...)` — WATCH-01 worker restart path.
  2. `notify_watchdog_recovery(component='meta_worker', ...)` — WATCH-03 + watchdog restart.
  3. `notify_watchdog_recovery(component='orchestrator', ...)` — D-04 async task crash path.
  4. `notify_backfill_gap(reason='recorder_retention', ...)` — D-08 retention overrun.
- **D-09-b:** The original "backfill failed entirely" one-shot is DROPPED as a distinct notification. Rationale: every previously-"failed-entirely" path now either auto-retries via the generalized retry decorator (read errors) or auto-relaunches via `add_done_callback` (orchestrator crash). Both cases fire `notify_watchdog_recovery`, which is the unified "something we did not anticipate happened, we recovered, here is the backtrace" signal.
- **D-09-c:** Gap-and-failure notifications are OBS-01's ONLY surface for backfill events. Runtime progress (slice N of M processed) continues to use `_LOGGER.info` only — no notifications, no issues.

### D-10 Translation-key catalogue

New entries added to `strings.json` `"issues"` section (Claude's Discretion on exact copy):

- `states_worker_stalled` (ERROR) — "TimescaleDB recorder states worker stalled"
- `meta_worker_stalled` (ERROR) — "TimescaleDB recorder metadata worker stalled"
- `db_unreachable` (ERROR, Claude's Discretion) — "TimescaleDB unreachable for more than 5 minutes"
- `recorder_disabled` (WARNING, Claude's Discretion) — "Home Assistant recorder integration is disabled"

`buffer_dropping` (Phase 2) stays unchanged.

Persistent notifications do NOT use the `"issues"` translation section — their copy lives in `notifications.py` directly (per D-07-b and D-08-b).

### D-11 OBS-02 / OBS-03 — remaining repair-issue lifecycle

Delivered partially by D-02 (worker_stalled). Remaining items marked Claude's Discretion for researcher/planner:

- `db_unreachable >5 min` detection source — suggested: extend retry decorator to also track `first_fail_ts`; expose via a second hook `on_sustained_fail: Callable[[float], None]` fired when `now - first_fail_ts > 300s` on each subsequent failure. Worker translates to `create_db_unreachable_issue`. Auto-clears on `on_recovery` hook (same mechanism as worker_stalled, different issue_id). May reuse D-03's `stalled` tracking with a separate timestamp field.
- `recorder_disabled` detection source — suggested: one-shot check in `async_setup_entry` step 7 (before subscribing to state_changed) via `hass.data.get('recorder_instance') is None` / `homeassistant.components.recorder.get_instance(hass).enabled`. Fire once on discovery; clear via HA integration reload. No periodic check.
- Exact copy and severity choices above.

### D-12 Requirement remapping (Phase 3)

- **WATCH-01:** delivered by D-05 + D-06 + D-07 (watchdog + worker run() exception catching + unified recovery notification).
- **WATCH-03:** delivered by D-06 (run()-wrapped try/except) + D-05 (watchdog restart).
- **OBS-01:** redefined per D-09. Covers worker_restarted (states + meta) + orchestrator_restarted + backfill_gap. DOES NOT include a distinct "backfill_failed" one-shot.
- **OBS-02:** `buffer_dropping` (Phase 2 done) + `states_worker_stalled` + `meta_worker_stalled` (D-02) + `db_unreachable` (D-11, Claude's Discretion) + `recorder_disabled` (D-11, Claude's Discretion). `ha_version_untested` DROPPED (Phase 2 carry-forward).
- **OBS-03:** auto-clear wired through D-02-c (worker_stalled) + D-11 (db_unreachable) + D-11 (recorder_disabled via integration reload). `buffer_dropping` auto-clear already in Phase 2.
- **OBS-04:** strings.json extended per D-10 catalogue.
- **SQLERR-03:** DROPPED per D-01.
- **SQLERR-04:** DROPPED per D-01.

### Claude's Discretion

- Watchdog polling cadence (10s suggested); whether the cadence is configurable via `const.py` or a config option.
- Watchdog restart factory details: whether `watchdog.py` owns a `respawn_states_worker(hass, runtime) -> Thread` factory or the existing `__init__.py` startup sequence is refactored into reusable spawn helpers.
- `db_unreachable >5 min` timer implementation — in retry decorator closure (preferred, see D-11) vs separate async polling task.
- `recorder_disabled` detection — exact HA API call (`recorder.get_instance(hass)` availability and attributes).
- Translation-key copy for all D-10 entries.
- Whether `_fetch_slice_raw` retry failures also deserve their own `*_stalled` repair issue (symmetry with writes) or remain notification-only via D-07.
- Abridged-traceback line-count (10 suggested) and formatting for D-07-b body.
- Context-key population conventions — how workers stash `last_op` / `retry_attempt` / `mode` on `self` so the watchdog can read them off the dead thread.
- Whether STALL_THRESHOLD N=5 is promoted to a config option or remains a const.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project constraints and requirements

- `.planning/PROJECT.md` — architecture decisions table, v0.3.6 baseline, constraints.
- `.planning/REQUIREMENTS.md` — Phase 3 requirements (WATCH-01, WATCH-03, OBS-01..04). SQLERR-03, SQLERR-04 MUST be marked DROPPED with pointer to D-01 during Phase 3 execution.
- `.planning/ROADMAP.md` — Phase 3 success criteria (#3 and #4 MUST be rewritten per D-01-c during Phase 3 execution).

### Research findings (critical)

- `.planning/research/SUMMARY.md` — `hass.async_*` not thread-safe from worker thread; `hass.add_job` is the fire-and-forget bridge; `run_coroutine_threadsafe().result()` deadlocks on HA shutdown; strings.json `"issues"` section required; psycopg3 error classes; `state_changes_during_period` requires single `entity_id`.
- `.planning/research/PITFALLS.md` — relevant pitfalls for thread restart, connection ownership.

### Phase 1 + Phase 2 artifacts

- `.planning/phases/01-thread-worker-foundation/01-CONTEXT.md` — WATCH-02 graceful shutdown pattern (sentinel + `async_add_executor_job(thread.join)`) carries forward; `hass.add_job` bridge carries forward.
- `.planning/phases/02-durability-story/02-CONTEXT.md` — D-07 (unified retry decorator + stall notification + stop_event interruption) carries forward and is EXTENDED in Phase 3 D-03; D-10 (issues.py + strings.json scaffolding + `buffer_dropping` keys) carries forward and is EXTENDED in Phase 3 D-10; D-13 (shutdown sequence with 30s join timeout, "Phase 3 watchdog addresses" note) is FULFILLED by Phase 3 D-05.

### Existing implementation

- `custom_components/ha_timescaledb_recorder/retry.py` — retry decorator. Phase 3 D-03 extends signature (`on_recovery`, optional `on_transient`).
- `custom_components/ha_timescaledb_recorder/states_worker.py` — `run()` gains D-06 outer try/except + `_last_exception` / `_last_context` fields; `_stall_hook` stays, `_recovery_hook` added.
- `custom_components/ha_timescaledb_recorder/meta_worker.py` — same D-06 + D-02 additions as states_worker.
- `custom_components/ha_timescaledb_recorder/backfill.py` — orchestrator body wrapped in outer try/except logging + re-raise to fire `add_done_callback` path (D-04); `read_watermark` decorated with retry (D-03-c); `_fetch_slice_raw` decorated with retry `on_transient=None` (D-03-c); D-08 gap detection inserted at top of each cycle.
- `custom_components/ha_timescaledb_recorder/issues.py` — extended with D-02 worker_stalled helpers + D-11 db_unreachable + recorder_disabled helpers (Claude's Discretion).
- `custom_components/ha_timescaledb_recorder/notifications.py` — NEW module per D-07.
- `custom_components/ha_timescaledb_recorder/watchdog.py` — NEW module per D-05.
- `custom_components/ha_timescaledb_recorder/strings.json` — `"issues"` section extended per D-10.
- `custom_components/ha_timescaledb_recorder/const.py` — add `STALL_THRESHOLD`, watchdog cadence constant, `DB_UNREACHABLE_THRESHOLD_SECONDS=300`.
- `custom_components/ha_timescaledb_recorder/__init__.py` — `async_setup_entry` spawns watchdog task and wires orchestrator `add_done_callback`; `async_unload_entry` cancels watchdog before joining threads; `runtime_data` gains `watchdog_task` field.
- `custom_components/ha_timescaledb_recorder/manifest.json` — unchanged.

### HA platform API references (to confirm during research)

- `homeassistant.helpers.issue_registry.async_create_issue` / `async_delete_issue` / `IssueSeverity`.
- `homeassistant.components.persistent_notification.async_create`.
- `homeassistant.components.recorder.get_instance` (for `recorder_disabled` detection in D-11).
- `asyncio.Task.add_done_callback` / `Task.exception()` / `Task.cancelled()`.
- `homeassistant.core.HomeAssistant.async_create_task` lifecycle semantics on integration unload.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable assets

- `retry.py` — extended (not rewritten) per D-03. Existing `on_transient`, `stop_event`, backoff schedule preserved; `on_recovery` and optional `on_transient` added.
- `issues.py` — extended with new translation-key helpers using the same `ir.async_create_issue` / `ir.async_delete_issue` pattern already in place.
- `hass.add_job` bridge from Phase 1 — reused for worker-thread → event-loop calls to `create_*_issue` / `clear_*_issue` / `notify_watchdog_recovery` (watchdog itself runs on event loop and calls directly, but worker-origin paths bridge via `hass.add_job`).
- `_stall_hook` pattern in both `states_worker.py` and `meta_worker.py` — new `_recovery_hook` follows the same shape, plumbed symmetrically through the decorator.
- `async_setup_entry` / `async_unload_entry` startup-sequence scaffolding from Phase 2 D-12 / D-13 — extended (not rewritten) to add the watchdog task and orchestrator done-callback.

### Established patterns

- `hass.async_add_executor_job(sync_fn, ...)` for crossing into recorder pool from event loop — reused for `_fetch_slice_raw` retry-wrapped calls and for the `_fetch_oldest_recorder_ts` gap-detection call (D-08-a step 2).
- `entry.runtime_data` with `HaTimescaleDBData` dataclass — extended with `watchdog_task: asyncio.Task | None` field.
- `entry.async_on_unload` for cleanup registration — reused for watchdog task cancellation.
- `@callback` decorator / `asyncio.Event` / `stop_event: threading.Event` sharing across components — reused unchanged.
- `_LOGGER.error(..., exc_info=True)` convention for exceptions — reused in D-06 and D-07-b.

### Integration points

- `async_setup_entry` sequence (Phase 2 D-12): a new step spawns `watchdog_task = hass.async_create_task(watchdog_loop(hass, entry.runtime_data))` after orchestrator registration (step 8 of D-12). Orchestrator `hass.async_create_task(...)` call is modified in-place to attach `add_done_callback(_on_orchestrator_done)`.
- `async_unload_entry` sequence (Phase 2 D-13): new step before step 2 (orchestrator cancel) cancels `watchdog_task` first; existing cancel+await pattern reused.
- HA event bus subscriptions, registry listeners, state_changed listener — untouched.

### Non-obvious callouts

- Watchdog is async, not a thread. Polling `thread.is_alive()` from an asyncio task works because `is_alive()` is just a boolean check on the underlying OS thread state — no blocking. Cadence (e.g., `await asyncio.sleep(10)`) keeps event loop free.
- Orchestrator crash recovery via `add_done_callback` runs the callback on the event loop synchronously — must NOT do blocking work. `hass.async_create_task(backfill_orchestrator(...))` inside the callback is safe (scheduling is non-blocking).
- Workers store `_last_exception` and `_last_context` as plain attributes on the Thread subclass; the watchdog reads them from outside the thread AFTER the thread has fully exited, so no cross-thread mutation race (Python memory model via join + is_alive).

</code_context>

<specifics>
## Specific Ideas

- User's framing for "backfill failed entirely": if the failure path is already covered by retry (data operation) or watchdog relaunch (unhandled crash), then the failure is not permanent — it is "failed and auto-retried". The correct observability is therefore one unified `notify_watchdog_recovery` that fires whenever the watchdog (or its async-task analogue) has to step in, carrying backtrace + context for dev investigation, rather than a distinct "backfill terminal" notification.
- Watchdog-recovery notification carries BOTH a user-friendly summary (component restarted + context) AND developer-useful traceback context (class, abridged traceback inline, hint to home-assistant.log for the full trace). Single helper so copy stays consistent across all restart paths.
- All backfill and watermark reads must get the same retry treatment as writes — that asymmetry was a gap in Phase 2 that Phase 3 closes. The retry decorator is the right abstraction for this; generalization is additive (optional `on_transient`), no rewrite.
- Dropping SQLERR-03/04 entirely (rather than partially) keeps the worker architecture simple and consistent with Phase 2 D-07-c. Persistent unrecoverable failures are a user problem (fix data / fix schema / restart HA), not a framework problem; the framework's job is to make them visible (stall notification + worker_stalled repair issue + HA log traceback).
- Per-worker `*_stalled` issue_ids chosen over a shared `worker_stalled` with translation_placeholders because concurrent stalls on states + meta workers should both be visible simultaneously in the Repairs UI.
- No max-restart cap on watchdog (and on orchestrator relaunch) because every recovery fires a loud notification. A broken worker will flood notifications, which IS the alarm signal.

</specifics>

<deferred>
## Deferred Ideas

### v2 — `is_fixable=True` repair-flow handlers

Current Phase 3 repair issues are `is_fixable=False` (informational, auto-clearing). v2 would make `worker_stalled`, `db_unreachable`, and `buffer_dropping` `is_fixable=True` with a custom fix flow that resets the worker queue + reconnects without an HA restart. Out of scope for v1.1.

### `ha_version_untested` repair issue

Explicitly dropped per Phase 2 carry-forward (user: "we're on the last TimescaleDB version"). Not reintroduced.

### Watchdog coverage for `async_setup_entry` itself

Phase 3 watchdog covers workers + orchestrator, NOT the setup-entry coroutine. If setup-entry fails, HA retries the integration setup per its own retry policy. Adding a watchdog layer on top is redundant.

### Telemetry / metrics export (Prometheus, etc.)

ROADMAP v1.1 scope is observability *inside HA* (notifications + Repairs UI + logs). External metrics export (retry rates, stall counts, watermark-lag gauges) belongs to a later milestone.

### `backfill_slice_stalled` repair issue

Whether `_fetch_slice_raw` deserves its own `*_stalled` repair issue, symmetric with write-side stalls, left as Claude's Discretion in D-11. If rejected during planning, a stalled slice read surfaces only via `notify_watchdog_recovery` on orchestrator relaunch (which will eventually happen since the stall blocks the whole backfill).

### REQUIREMENTS.md Phase 1 unticked boxes

Same carry-forward note as Phase 2: some Phase 1 requirement checkboxes are still `- [ ]`. Not Phase 3's concern; separate housekeeping.

### v2 — FILTER-01 options-flow UI

Confirmed deferred (from Phase 2).

</deferred>

---

*Phase: 03-hardening-and-observability*
*Context gathered: 2026-04-22*
