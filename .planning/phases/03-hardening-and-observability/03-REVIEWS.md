---
phase: 3
reviewers: [gemini, codex]
reviewed_at: 2026-04-23T00:00:00Z
plans_reviewed:
  - 03-01-PLAN.md
  - 03-02-PLAN.md
  - 03-03-PLAN.md
  - 03-04-PLAN.md
  - 03-05-PLAN.md
  - 03-06-PLAN.md
  - 03-07-PLAN.md
  - 03-08-PLAN.md
---

# Cross-AI Plan Review — Phase 3: Hardening and Observability

_Note: Claude (the current runtime) was excluded from reviewing to preserve independence. Reviewed by Gemini and Codex._

## Gemini Review

### 1. Summary
Phase 3 provides a robust error-handling and observability layer that bridges the gap between the synchronous worker threads and the Home Assistant UI. The design cleverly utilizes `hass.add_job` to bridge thread boundaries for repair issues and notifications, while introducing an asynchronous watchdog to ensure system liveness. By shifting from a "halt-on-error" approach to a "stalled-worker-notification" model, the integration prioritizes system availability and user awareness over complex local recovery loops. The plans are highly granular, demonstrate a deep understanding of Home Assistant's internal constraints (particularly regarding the `recorder` and `issue_registry`), and follow a logical dependency chain.

### 2. Strengths
- **Thread Boundary Management:** The strict adherence to `hass.add_job` for all UI/Registry interactions from the worker threads is exactly what is required for HA stability.
- **Decoupled Watchdog:** Using an async loop for health checks instead of having workers monitor each other avoids circular dependencies and complex lock-step synchronization.
- **Retry Decorator Evolution:** Extending the existing retry logic to handle "sustained failures" (300s threshold) provides a clean way to implement the `db_unreachable` repair issue without polluting the core logic of the workers.
- **Post-Mortem Capability:** Storing `_last_exception` and `_last_context` on the worker threads allows the watchdog to provide high-signal notifications (including tracebacks) even after a thread has terminated.
- **Backfill Integrity:** The gap detection logic correctly identifies data loss at the source (HA Recorder retention) and informs the user, which is critical for a "recorder" component.

### 3. Concerns

- **Notification Storm Risk (MEDIUM):** D-04 explicitly accepts a "notification storm" as an alarm for orchestrator crashes. While intentional, a fast-looping crash (e.g., a type error in a tight loop) could generate thousands of notifications/log entries very quickly, potentially impacting HA responsiveness or disk space.
- **Retry State Locality (LOW):** Plan 04 suggests wiring hooks at construction. If `retry_until_success` is used as a decorator on class methods, the closure state (`stalled`, `first_fail_ts`) is shared across all instances of that class. While there is usually only one instance of each worker, dynamic wrapping in `__init__` is safer and should be the explicit implementation path.
- **Recorder Availability in `async_setup_entry` (MEDIUM):** In Plan 07, the recorder-disabled check happens in `async_setup_entry`. Depending on the HA boot order, the `recorder` integration might not be fully initialized or its instance might not be available immediately via `get_instance`.
- **Watchdog Polling Robustness (LOW):** A 10s poll is safe, but since worker threads crash rarely, ensure that the watchdog itself is robust against exceptions so it doesn't die and leave the workers unmonitored.

### 4. Suggestions
- **Exponential Backoff for Orchestrator Restart:** In Plan 07, add a small `asyncio.sleep` (e.g., 5–10s) before relaunching the orchestrator task in `_on_orchestrator_done`. This mitigates the "notification storm" risk without significantly impacting recovery time.
- **Explicit Instance-Wrapping:** Ensure Plan 03/04 implementation wraps raw methods inside the worker's `__init__` (e.g., `self.flush = retry_until_success(...)(self._flush_raw)`) to guarantee retry state is instance-local.
- **Recorder Wait:** Consider using `await recorder.async_wait_recorder(hass)` (if available) before checking `.enabled`, to ensure the recorder component is fully ready.
- **Watchdog Robustness:** Wrap the entire body of `watchdog_loop` in a `try/except Exception` block to ensure a failure in one monitoring cycle doesn't kill the watchdog task itself.

### 5. Risk Assessment
**Overall Risk: LOW**

The plans are technically sound and show a mature approach to Home Assistant integration development. The most significant risks (thread safety and deadlocks) were addressed in Phase 1, and Phase 3 focuses on the "UI-to-Engine" communication layer. The logic for bridging sync/async boundaries is consistent with HA best practices. The "notification storm" is the only notable edge case, but since it's a known trade-off for visibility, it doesn't compromise the integrity of the data ingestion.

---

## Codex Review

### Summary
Overall, the phase is well-structured: the wave ordering is mostly sound, the HA thread/event-loop boundary is being treated seriously, and the plans align with the project's durability goal. The main gaps are cross-cutting rather than local: `recorder_disabled` is not yet planned as a self-clearing condition, backfill "failed entirely" is only partially modeled, and the retry/read-path work could accidentally block the HA loop or hide faults behind infinite retry.

### Plan 01 — Constants + strings.json + issues.py helpers

**Summary:** Good foundation work. Centralizing thresholds, translation keys, and issue helpers early is the right dependency cut for the later watchdog/retry work.

**Strengths:**
- Keeps `hass.add_job` compatibility explicit by requiring `fn(hass)` helpers.
- Puts thresholds in `const.py`, which reduces later drift.
- Adds tests around issue helpers, which is the right place to catch translation-key mismatches.

**Concerns:**
- MEDIUM: The plan does not say where issue IDs/translation keys live as shared constants; string duplication across `const.py`, `issues.py`, and `strings.json` is an avoidable drift risk.
- MEDIUM: `recorder_disabled` gets create/clear helpers, but later plans only describe creation, not the clear path needed for the success criteria.
- LOW: Existing `buffer_dropping` behavior is called "unchanged" but not explicitly regression-tested.

**Suggestions:**
- Define issue IDs/translation keys once and import them everywhere.
- Add a test that every helper's `translation_key` exists in `strings.json`.
- Add a small regression test for the existing `buffer_dropping` helpers.

**Risk Assessment: LOW-MEDIUM.** The local changes are simple, but mismatched identifiers would silently undermine the Repairs UI.

### Plan 02 — notifications.py

**Summary:** A dedicated `notifications.py` module is a clean design, but the traceback/logging details need tightening to avoid losing the most important diagnostic data.

**Strengths:**
- Good separation: one-shot notifications are distinct from ongoing repair issues.
- Plain `def` functions fit HA callback semantics and are easy to call from the event loop.
- Stable `notification_id` values help avoid notification spam.

**Concerns:**
- HIGH: `_LOGGER.error(..., exc_info=exc)` is not a reliable way to log a stored exception outside the active `except` block; it can drop the traceback.
- MEDIUM: Raw `context` dumping into markdown can leak sensitive or noisy fields later if the context grows.
- MEDIUM: The phase success criteria mention "backfill failed entirely," but this module only names watchdog recovery and backfill-gap notifications; the failure case is currently indirect.

**Suggestions:**
- Format tracebacks explicitly with `traceback.TracebackException.from_exception(exc)`.
- Whitelist context fields for notifications instead of dumping arbitrary dicts.
- Make the notification contract explicit: either add a dedicated backfill-failed notifier or document that orchestrator restart notifications are the intended fulfillment.

**Risk Assessment: MEDIUM.** The module shape is right, but losing traceback fidelity would materially reduce operability.

### Plan 03 — Extend retry_until_success (TDD)

**Summary:** Extending the retry decorator is the correct leverage point, and doing it TDD-first is strong. The main risk is ambiguity in state transitions once the decorator is used for both worker writes and read paths.

**Strengths:**
- TDD is appropriate here because the behavior is stateful and easy to regress.
- Per-wrapper closure state is the right choice.
- Swallowing hook exceptions protects the retry loop from observability-path failures.

**Concerns:**
- HIGH: If this decorator remains blocking/synchronous, reusing it on read paths from async code can block the HA event loop during backoff.
- MEDIUM: The plan says "cumulative fail >= sustained_fail_seconds" but does not specify a monotonic clock; wall-clock jumps can misfire the threshold.
- MEDIUM: `on_recovery` semantics are underspecified when only some hooks have fired; issue flapping behavior needs precise tests.

**Suggestions:**
- Use `time.monotonic()` for all threshold timing.
- Add explicit tests for: threshold crossed then recovery, transient failures below threshold, repeated fail/recover cycles, and `stop_event` interruption.
- If read paths will use this from async code, either make the decorator async-aware or ensure the wrapped call runs off the event loop.

**Risk Assessment: MEDIUM.** Correct idea, but this is now a shared primitive and mistakes here propagate widely.

### Plan 04 — Wire hooks in states_worker + meta_worker

**Summary:** Catching worker-thread top-level exceptions and routing them into the watchdog path is necessary. The plan is solid, but it needs more precision around context capture and shutdown races.

**Strengths:**
- The outer `run()` guard is the right implementation for `WATCH-03`.
- `_last_exception` and `_last_context` give the watchdog something actionable to surface.
- Clearing issues on the first successful post-stall operation matches the chosen recovery model.

**Concerns:**
- HIGH: `_last_context` is described abstractly ("mode, retry counter, last op") but not how it is safely maintained across all code paths; missing or stale context would weaken the restart notification.
- MEDIUM: `hass.add_job(...)` from a dying thread can race HA shutdown and generate noisy late callbacks.
- MEDIUM: If any write/read path bypasses the retry wrapper, `worker_stalled` or `db_unreachable` may never clear.

**Suggestions:**
- Initialize `_last_exception`/`_last_context` in `__init__` and update `_last_context` at stable points only.
- Separate failure capture from teardown with a `finally` block so cleanup errors do not hide the original fault.
- Add tests for exception-in-run, exception-in-cleanup, and hook behavior during unload.

**Risk Assessment: MEDIUM-HIGH.** The design is correct, but thread lifecycle bugs here are easy to miss and hard to diagnose later.

### Plan 05 — Backfill gap detection + retry-wrap reads

**Summary:** This plan has the highest correctness risk in the phase. Gap detection is valuable, but the current `oldest_ts is None` handling and read-path retry strategy could create false positives, silent skipping, or event-loop blocking.

**Strengths:**
- Gap detection against the recorder retention boundary directly supports the observability goal.
- Letting orchestrator crashes propagate into a restart path is cleaner than burying them in local try/except blocks.
- Retrying watermark reads is reasonable if done without coupling or loop blocking.

**Concerns:**
- HIGH: Treating `oldest_ts is None` as "entire window is gap" is unsafe; `None` may mean "unknown/not ready," not confirmed data loss.
- HIGH: Wrapping `_fetch_slice_raw` with indefinite retry can hide real bugs forever and may block the HA event loop if the retry decorator sleeps synchronously.
- HIGH: Moving watermark-read retry onto `states_worker` couples backfill correctness to worker instance lifecycle in a way that is not obviously necessary.
- MEDIUM: The plan does not clearly distinguish "gap detected" from "backfill failed entirely," yet the phase requires both signals.

**Suggestions:**
- Only declare a gap when `oldest_ts` is known and comparable; if it is `None`, defer and retry later.
- Keep read-path helpers local to `backfill.py` or a shared helper module instead of binding them to the worker object.
- Add tests for exact-boundary comparisons, `None`, partial gaps, and recorder-read failures.
- Ensure recorder-side retries happen in an executor or otherwise off the HA event loop.

**Risk Assessment: HIGH.** This is the plan most likely to affect correctness or make failure modes invisible.

### Plan 06 — watchdog.py

**Summary:** A dedicated watchdog module is the right abstraction. The missing piece is explicit protection against repeated crash/restart loops and a clear statement that queues/state are reused across worker replacement.

**Strengths:**
- Polling both workers from one async task is straightforward and easy to reason about.
- Centralized factories reduce restart duplication.
- Cancel-on-unload behavior is correct in principle.

**Concerns:**
- HIGH: The plan does not explicitly say the replacement workers reuse the existing runtime queues; if they don't, buffered data may be lost on restart.
- HIGH: No restart backoff or dedupe is described, so a crash-on-start worker can create endless thread churn and notification churn.
- MEDIUM: The factories depend on runtime fields added in Plan 07; that coupling is fine, but it should be explicit to avoid integration mistakes.

**Suggestions:**
- State clearly that worker replacement reuses the same queues, stop event, and config-derived runtime state.
- Add at least minimal restart throttling or duplicate-notification suppression for identical rapid failures.
- Test the "dies immediately on start" loop and "dies after partial work" case.

**Risk Assessment: MEDIUM-HIGH.** The module shape is good, but restart-loop behavior needs more design.

### Plan 07 — __init__.py wiring

**Summary:** This is the integration plan that makes the phase real, but it currently misses one success criterion and has the most important startup/shutdown race surfaces.

**Strengths:**
- Putting factories, watchdog task, and orchestrator done-callback wiring in `__init__.py` is the right composition point.
- Cancelling the watchdog before worker shutdown is the correct ordering.
- Using `add_done_callback` for orchestrator crash recovery matches HA/asyncio semantics.

**Concerns:**
- HIGH: `recorder_disabled` is only checked once at setup; there is no planned automatic clear when the condition resolves, which conflicts with the phase success criteria (OBS-03: issues clear automatically when conditions resolve).
- HIGH: `_on_orchestrator_done` can still race unload/startup unless restart is gated on stronger runtime state than `stop_event.is_set()` alone.
- MEDIUM: `watchdog_task` cancellation should be awaited before proceeding, otherwise it can observe half-torn runtime state.
- MEDIUM: Carrying `entity_filter` and similar callables into restart factories assumes they are immutable/thread-safe; that should be explicit.

**Suggestions:**
- Add a real clear path for `recorder_disabled`: a deferred re-check, startup polling until recorder loads, or a listener-based approach.
- Gate orchestrator restart on both `stop_event` and "entry/runtime still active."
- Await watchdog-task completion during unload.
- Add tests for unload during orchestrator failure and setup before recorder is loaded.

**Risk Assessment: HIGH.** This plan carries the critical lifecycle wiring, and it currently under-delivers on the `recorder_disabled` auto-clear requirement.

### Plan 08 — Docs update

**Summary:** Sensible doc cleanup with very low technical risk. It should follow the final implemented behavior, not lead it.

**Strengths:**
- Keeps roadmap and requirements aligned with the project decisions.
- Explicitly documents dropped requirements, which avoids future confusion.

**Concerns:**
- LOW: If merged too early, docs can drift from the actual implemented notification/issue behavior.

**Suggestions:**
- Land this after the code/tests settle.
- Reference the exact final issue IDs and notification semantics.

**Risk Assessment: LOW.** Only sequencing matters.

### Overall Risk Assessment
**MEDIUM-HIGH.** The architecture is coherent and the wave ordering is mostly good, but three items should be tightened before execution: `recorder_disabled` needs a real self-clear path, Plan 05 needs a safer gap/reader-retry design, and the watchdog/restart plans need explicit protection against crash loops and shutdown races.

---

## Consensus Summary

### Agreed Strengths

1. **Thread-safety discipline** — both reviewers praised the `hass.add_job(fn, hass)` bridge pattern for all issue/notification helpers as correct and consistent with HA internals.
2. **Decoupled watchdog architecture** — async polling loop for liveness, not cross-thread monitoring, avoids circular dependencies.
3. **Per-wrapper closure state** in retry decorator — independent `stalled`/`first_fail_ts` per call site is the right design.
4. **Post-mortem context** — `_last_exception`/`_last_context` on dead threads gives the watchdog high-signal data without cross-thread races (read after thread exits).
5. **Backfill gap detection concept** — both acknowledge detecting recorder-retention overrun is valuable and the right observability signal for a recorder component.

### Agreed Concerns

1. **`recorder_disabled` lacks a self-clear path (HIGH/MEDIUM)** — checked once at setup, never cleared automatically when condition resolves. Conflicts with OBS-03 ("issues clear automatically when conditions resolve"). Needs either a deferred re-check, listener-based approach, or polling until recorder loads.
2. **Notification/restart storm (MEDIUM/HIGH)** — no backoff or rate-limiting on orchestrator relaunch or watchdog worker respawn. A crash-on-start worker creates endless notification churn. A small `asyncio.sleep` before relaunch and minimal restart throttling in watchdog is recommended.
3. **Retry decorator blocking event loop (HIGH/MEDIUM)** — if the decorator (which does synchronous `stop_event.wait(backoff)`) is used on read paths called from async context, it will block the HA event loop. Must ensure wrapped calls run in an executor, not directly on the loop.
4. **`oldest_ts is None` gap detection (HIGH)** — `None` from `states_manager.oldest_ts` may mean "not ready" not "confirmed data loss." Treating it as "entire window is gap" can fire false notifications. Only declare gap when `oldest_ts` is a known comparable float.

### Divergent Views

- **Overall risk level:** Gemini assessed LOW; Codex assessed MEDIUM-HIGH. The difference is scope: Gemini evaluated the design patterns (solid), Codex evaluated the edge cases and lifecycle correctness (gaps exist). Both are right for their frame. Recommendation: treat as MEDIUM — execute confidently, but address the HIGH concerns before merging.
- **Plan 05 read-path retry:** Gemini did not flag the `_fetch_slice_raw` retry as particularly risky; Codex flagged it HIGH (blocking, indefinite retry hides bugs). The concern is real — verify retry calls happen only inside `recorder_instance.async_add_executor_job` (which already runs in a thread pool), not on the event loop directly.
- **`_last_context` detail:** Codex raised this as HIGH risk; Gemini didn't surface it. The risk is real but mitigable with simple `__init__` initialization to safe defaults.

---

## Usage

To incorporate this feedback:

```
/gsd-plan-phase 3 --reviews
```

Key pre-execution actions recommended by both reviewers:
1. **Plan 07**: Add `recorder_disabled` clear path (listener or deferred re-check)
2. **Plan 06**: Add restart throttling in `watchdog_loop` (e.g., `asyncio.sleep(5)` before respawn)
3. **Plan 05**: Guard `oldest_ts is None` — skip gap detection when not a float
4. **Plan 02**: Use `traceback.TracebackException.from_exception(exc)` for reliable traceback capture
