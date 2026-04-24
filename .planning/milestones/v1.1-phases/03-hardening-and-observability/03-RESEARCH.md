# Phase 3: Hardening and Observability - Research

**Researched:** 2026-04-22
**Domain:** HA custom component — watchdog, retry extensions, repair issues, persistent notifications
**Confidence:** HIGH

## Summary

Phase 3 adds a resilience and observability layer over the Phase 2 thread-worker foundation. Three independent capabilities interlock: (1) the retry decorator is extended with an `on_recovery` hook and generalized for read paths; (2) a new async watchdog task polls both worker threads and auto-restarts dead ones; (3) repair issues and persistent notifications surface ongoing and one-shot problems in the HA Repairs UI and notification panel.

All key HA APIs were verified directly against the installed HA 2026.3.4 codebase (`homeassistant==2026.3.4` in the project venv). There are no surprises from training data: `async_create_issue` is a `@callback` (synchronous), not an `async def`; `persistent_notification.async_create` follows the same pattern; and `asyncio.Task.add_done_callback` receives a sync callback with the Task as its sole argument. The only non-obvious finding is the `hass.add_job` positional-only constraint — all issue/notification helpers must be factored as wrapper functions (taking `hass` as their only argument) so they can be scheduled from worker threads via `hass.add_job(helper, hass)`.

For recorder availability detection (D-11), `recorder.get_instance(hass)` raises `KeyError` when the recorder component was not loaded; the instance has an `.enabled` attribute (default `True`) that can be set to `False` at runtime. Both conditions represent "recorder unavailable for backfill". Gap detection (D-08) can use `recorder_instance.states_manager.oldest_ts` — a cached `float | None` epoch timestamp maintained by the recorder's `StatesManager`.

**Primary recommendation:** All three new modules (`watchdog.py`, `notifications.py`, extended `issues.py`) are additive. They introduce no changes to the existing data paths — they only observe existing state (thread.is_alive(), `_last_exception`, `_last_context`) and schedule event-loop-side notifications via the already-established `hass.add_job` bridge.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

- **D-01:** SQLERR-03 and SQLERR-04 DROPPED. Phase 2 D-07-c principle extended to Phase 3. No per-row fallback loop; no code-bug halt. Persistent failure stalls worker → `worker_stalled` repair issue + stall notification. User recovery = fix condition + restart HA.
- **D-02:** Per-worker repair issue ids: `states_worker_stalled` and `meta_worker_stalled`. Severity: `ir.IssueSeverity.ERROR`, `is_fixable=False`. Clear on first successful operation after stall. Worker-thread bridge: `hass.add_job(create_*_stalled_issue, hass)` / `hass.add_job(clear_*_stalled_issue, hass)`.
- **D-03:** `retry_until_success` signature extended with `on_recovery: Callable[[], None] | None = None`. `on_transient` made optional (default `None`). `read_watermark` wrapped with `on_transient=reset_db_connection`. `_fetch_slice_raw` wrapped with `on_transient=None`. `STALL_THRESHOLD` constant = 5 in `const.py`.
- **D-04:** Orchestrator crash recovery via `asyncio.Task.add_done_callback(_on_orchestrator_done)`. On exception: fire `notify_watchdog_recovery`, relaunch orchestrator, attach new callback recursively. On cancellation or clean shutdown: return without relaunching. Replace reference on `entry.runtime_data`.
- **D-05:** New `watchdog.py` module. Coroutine `watchdog_loop(hass, runtime)` spawned as asyncio task during `async_setup_entry`. Covers both worker threads (not orchestrator). Stored on `entry.runtime_data.watchdog_task`. On dead thread + not shutdown: extract `_last_exception`/`_last_context`, call `notify_watchdog_recovery`, respawn thread.
- **D-06:** Both worker `run()` methods wrap main loop in outer `try/except Exception as err`. On exception: `_LOGGER.error(..., exc_info=True)`, store `self._last_exception = err`, store `self._last_context: dict`. Thread exits naturally; watchdog picks up on next tick.
- **D-07:** New `notifications.py` module with `notify_watchdog_recovery(hass, component, exc, context)` and `notify_backfill_gap(hass, reason, details)`. `notify_watchdog_recovery`: logs full traceback, fires `persistent_notification` with markdown body including abridged traceback. Context dict standard keys: `at`, `mode`, `retry_attempt`, `last_op`.
- **D-08:** Gap detection at top of each orchestrator backfill cycle. Compare `needed_from = wm - timedelta(minutes=10)` against oldest available recorder timestamp. If `oldest > needed_from`: fire `notify_backfill_gap`, adjust `from_`. Uses `recorder_instance.states_manager.oldest_ts`.
- **D-09:** OBS-01 scope: `notify_watchdog_recovery` (states_worker, meta_worker, orchestrator) + `notify_backfill_gap`. "Backfill failed entirely" dropped (folded into watchdog recovery path).
- **D-10:** New `strings.json` translation keys: `states_worker_stalled`, `meta_worker_stalled`, `db_unreachable`, `recorder_disabled`.
- **D-11:** `db_unreachable` and `recorder_disabled` issue helpers (Claude's Discretion items). `db_unreachable`: extend retry decorator with `first_fail_ts` tracking + `on_sustained_fail` hook. `recorder_disabled`: one-shot check in `async_setup_entry` via `recorder.get_instance`.
- **D-12:** Requirement remapping confirmed in CONTEXT.md. SQLERR-03 and SQLERR-04 marked DROPPED in REQUIREMENTS.md during Phase 3 execution.

### Claude's Discretion

- Watchdog polling cadence (10s suggested); whether configurable via `const.py`.
- Watchdog thread-recreation: does `watchdog.py` own a `spawn_states_worker` / `spawn_meta_worker` factory or delegate to `__init__.py` helpers.
- `db_unreachable >5 min` timer: extend retry decorator closure with `first_fail_ts` + `on_sustained_fail` hook (preferred per D-11) vs separate polling task.
- `recorder_disabled` detection: exact check pattern (`try/except KeyError` on `get_instance` + `.enabled` attribute).
- Translation-key copy for all D-10 entries.
- Whether `_fetch_slice_raw` stall deserves its own `*_stalled` repair issue or remains notification-only via orchestrator relaunch.
- Abridged-traceback line count (10 suggested) and formatting in notification body.
- Context-key population conventions on worker `self` for watchdog to read off dead thread.
- Whether `STALL_THRESHOLD` N=5 is promoted to a config option.

### Deferred Ideas (OUT OF SCOPE)

- `is_fixable=True` repair-flow handlers (v2).
- `ha_version_untested` repair issue (explicitly dropped Phase 2).
- Watchdog coverage for `async_setup_entry` itself.
- Telemetry / metrics export (Prometheus, etc.).
- `backfill_slice_stalled` repair issue — whether `_fetch_slice_raw` deserves its own `*_stalled` issue is Claude's Discretion; if rejected, stalled slice surfaces via `notify_watchdog_recovery` on orchestrator relaunch only.
- REQUIREMENTS.md Phase 1 unticked checkboxes.
- FILTER-01 options-flow UI.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| WATCH-01 | Async watchdog task periodically checks worker thread.is_alive(); restarts dead worker and fires a persistent_notification | D-05 watchdog.py + D-06 exception catching + D-07 notify_watchdog_recovery |
| WATCH-03 | Worker thread unhandled exceptions caught in run() and trigger watchdog restart path | D-06 outer try/except + D-05 watchdog poll detects dead thread |
| OBS-01 | persistent_notification for one-shot events: worker died+restarted, backfill gap | D-07 notify_watchdog_recovery + D-08 notify_backfill_gap — via notifications.py |
| OBS-02 | Repair issues for ongoing conditions: DB unreachable >5 min, buffer dropping, HA recorder disabled | D-02 worker_stalled + D-11 db_unreachable + D-11 recorder_disabled + existing buffer_dropping |
| OBS-03 | Repair issues cleared when condition resolves | D-02-c auto-clear on_recovery hook + D-11 db_unreachable on_recovery + D-11 recorder_disabled on reload |
| OBS-04 | strings.json ships "issues" section with all translation keys | D-10 catalogue: states_worker_stalled, meta_worker_stalled, db_unreachable, recorder_disabled |
| SQLERR-03 | DROPPED — per D-01-a/b | Mark DROPPED in REQUIREMENTS.md; no implementation |
| SQLERR-04 | DROPPED — per D-01-a/b | Mark DROPPED in REQUIREMENTS.md; no implementation |
</phase_requirements>

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Thread liveness polling | Async task (event loop) | — | `is_alive()` is a non-blocking boolean check; an asyncio task with `sleep(10)` cadence keeps event loop free |
| Worker restart / spawn | Async task (event loop) | — | Thread construction and `.start()` are non-blocking; new Thread references replace dead ones on `runtime_data` |
| Orchestrator crash recovery | Event loop (done_callback) | — | `add_done_callback` runs on event loop; new task scheduling is a non-blocking `call_soon` |
| Persistent notifications | Event loop (@callback) | Worker thread (bridge) | `async_create` is a `@callback`; called directly from event loop, or via `hass.add_job` from worker thread |
| Repair issue lifecycle | Event loop (@callback) | Worker thread (bridge) | `async_create_issue` / `async_delete_issue` are `@callback`; same bridge pattern as notifications |
| Gap detection | Event loop (orchestrator) | Recorder thread (read) | `states_manager.oldest_ts` is an in-memory float read; can be accessed on event loop without executor |
| `db_unreachable` timer | Worker thread (retry closure) | — | `first_fail_ts` tracked inside retry decorator closure; `on_sustained_fail` fires `hass.add_job(create_db_unreachable_issue, hass)` |
| `recorder_disabled` detection | Event loop (async_setup_entry) | — | One-shot check in step 7; `recorder.get_instance` raises `KeyError` if not loaded; `instance.enabled` attribute |

## Standard Stack

### Core (all verified against installed HA 2026.3.4)

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `homeassistant.helpers.issue_registry` | HA 2026.3.4 | Repair issues lifecycle | Official HA API; `async_create_issue` and `async_delete_issue` are `@callback` functions |
| `homeassistant.components.persistent_notification` | HA 2026.3.4 | One-shot user notifications | Official HA API; `async_create` is a `@callback`; same `notification_id` replaces existing notification |
| `asyncio.Task.add_done_callback` | stdlib | Orchestrator crash recovery | Standard asyncio — callback receives Task, runs on event loop synchronously |
| `traceback.format_exception` | stdlib | Abridged exception display in notifications | Returns list of strings; `''.join(parts[-10:])` gives last 10 parts |
| `threading.Thread.is_alive` | stdlib | Watchdog liveness check | Non-blocking boolean; safe from asyncio event loop |
| `homeassistant.components.recorder` | HA 2026.3.4 | Recorder availability detection, oldest-ts access | `get_instance(hass)` raises `KeyError` if recorder not loaded; `instance.states_manager.oldest_ts` is `float | None` |

### No New Third-Party Dependencies

Phase 3 is purely additive using existing Phase 1/2 dependencies plus HA built-ins. `manifest.json` unchanged.

## Architecture Patterns

### System Architecture Diagram

```
              HA Event Loop
              ─────────────────────────────────────────
              async_setup_entry
                  │
                  ├── hass.async_create_task(watchdog_loop)
                  │       │
                  │       ▼
                  │   watchdog_loop (polls every 10s)
                  │       │
                  │       ├── thread.is_alive()? No + not stop_event
                  │       │       │
                  │       │       ├── read dead_thread._last_exception
                  │       │       ├── read dead_thread._last_context
                  │       │       ├── notify_watchdog_recovery(hass, component, exc, ctx)
                  │       │       │       └── persistent_notification.async_create(...)
                  │       │       └── spawn new Thread; start; update runtime_data
                  │       │
                  │       └── loop back (await asyncio.sleep(10))
                  │
                  ├── task = hass.async_create_task(backfill_orchestrator(...))
                  │   task.add_done_callback(_on_orchestrator_done)
                  │       │
                  │       ▼ (on task completion with exception)
                  │   _on_orchestrator_done(task)
                  │       ├── task.cancelled()? → return
                  │       ├── stop_event.is_set()? → return
                  │       ├── task.exception() → exc
                  │       ├── notify_watchdog_recovery(hass, 'orchestrator', exc, ctx)
                  │       ├── new_task = hass.async_create_task(backfill_orchestrator(...))
                  │       ├── new_task.add_done_callback(_on_orchestrator_done)
                  │       └── runtime_data.orchestrator_task = new_task
                  │
              backfill_orchestrator (event loop)
                  │
                  ├── [top of each cycle] D-08 gap detection
                  │       ├── oldest_ts = recorder_instance.states_manager.oldest_ts
                  │       ├── If oldest_ts > needed_from:
                  │       │       └── notify_backfill_gap(hass, reason, details)
                  │       └── adjust from_ = datetime.fromtimestamp(oldest_ts, utc)
                  │
                  └── [slice loop] _fetch_slice_raw (wrapped with retry on_transient=None)

Worker Thread (states/meta)
              ─────────────────────────────────────────
              run()
                  ├── try: [main loop]
                  │       ├── _insert_chunk / _write_item (retry-wrapped)
                  │       │       ├── on failure N≥5: on_stall → hass.add_job(create_*_stalled_issue, hass)
                  │       │       ├── on recovery:    on_recovery → hass.add_job(clear_*_stalled_issue, hass)
                  │       │       └── on fail > 300s: on_sustained_fail → hass.add_job(create_db_unreachable_issue, hass)
                  │       └── [stores mode, last_op on self for watchdog]
                  └── except Exception as err:
                          ├── _LOGGER.error(..., exc_info=True)
                          ├── self._last_exception = err
                          ├── self._last_context = {mode, retry_attempt, last_op, at}
                          └── return  ← thread exits; watchdog detects is_alive()==False
```

### Recommended Project Structure

```
custom_components/ha_timescaledb_recorder/
├── watchdog.py          # NEW: watchdog_loop coroutine
├── notifications.py     # NEW: notify_watchdog_recovery + notify_backfill_gap
├── issues.py            # EXTEND: add worker_stalled + db_unreachable + recorder_disabled helpers
├── retry.py             # EXTEND: on_recovery hook + on_sustained_fail + first_fail_ts
├── states_worker.py     # EXTEND: D-06 outer try/except + _last_exception/_last_context
├── meta_worker.py       # EXTEND: same D-06 additions
├── backfill.py          # EXTEND: orchestrator done_callback + gap detection + retry for reads
├── __init__.py          # EXTEND: spawn watchdog + wire done_callback + recorder_disabled check
├── const.py             # EXTEND: STALL_THRESHOLD, WATCHDOG_INTERVAL_S, DB_UNREACHABLE_THRESHOLD_SECONDS
└── strings.json         # EXTEND: 4 new "issues" keys
```

### Pattern 1: Extending the Retry Decorator with `on_recovery` and `on_sustained_fail`

**What:** Add two new hooks to `retry_until_success` with minimal state tracking in the closure.
**When to use:** All existing and new wrapped call sites (states `_insert_chunk`, meta `_write_item`, `read_watermark`, `_fetch_slice_raw`).

```python
# Source: verified against existing retry.py + Python stdlib

def retry_until_success(
    *,
    stop_event: threading.Event,
    on_transient: Callable[[], None] | None = None,       # EXISTING (now optional with default None)
    notify_stall: Callable[[int], None] | None = None,    # EXISTING
    on_recovery: Callable[[], None] | None = None,        # NEW D-03-a
    on_sustained_fail: Callable[[], None] | None = None,  # NEW D-11
    sustained_fail_seconds: float = 300.0,                # NEW D-11
    backoff_schedule: tuple[int, ...] = _BACKOFF_SCHEDULE,
    stall_threshold: int = _STALL_NOTIFY_THRESHOLD,
) -> Callable:
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            attempts = 0
            notified = False             # stall notified flag
            stalled = False              # recovery tracking flag (D-03-a)
            first_fail_ts: float | None = None   # sustained-fail timer (D-11)
            sustained_notified = False
            while True:
                try:
                    result = fn(*args, **kwargs)
                except Exception as exc:
                    attempts += 1
                    now = time.monotonic()
                    if first_fail_ts is None:
                        first_fail_ts = now
                    # on_transient: reset connection (optional — None for reads via recorder pool)
                    if on_transient is not None:
                        try:
                            on_transient()
                        except Exception:
                            _LOGGER.exception("on_transient hook raised; continuing")
                    # stall notification (D-07-f, existing)
                    if not notified and attempts >= stall_threshold:
                        if notify_stall is not None:
                            try:
                                notify_stall(attempts)
                            except Exception:
                                _LOGGER.exception("notify_stall hook raised; continuing")
                        notified = True
                        stalled = True   # arm recovery tracking
                    # sustained-fail notification (D-11 db_unreachable)
                    if (not sustained_notified
                            and on_sustained_fail is not None
                            and first_fail_ts is not None
                            and now - first_fail_ts >= sustained_fail_seconds):
                        try:
                            on_sustained_fail()
                        except Exception:
                            _LOGGER.exception("on_sustained_fail hook raised; continuing")
                        sustained_notified = True
                    # ... backoff + stop_event ...
                    continue
                # Success path
                if stalled and on_recovery is not None:
                    try:
                        on_recovery()
                    except Exception:
                        _LOGGER.exception("on_recovery hook raised; continuing")
                stalled = False
                notified = False
                sustained_notified = False
                first_fail_ts = None
                attempts = 0
                return result
        return wrapper
    return decorator
```

[VERIFIED: existing retry.py structure + Python stdlib time.monotonic]

### Pattern 2: Watchdog Loop

**What:** Async coroutine polling two worker threads at a fixed cadence.
**When to use:** Spawned once in `async_setup_entry`; cancelled in `async_unload_entry` before joining workers.

```python
# Source: verified semantics via Python stdlib + asyncio Task docs

async def watchdog_loop(hass: HomeAssistant, runtime: HaTimescaleDBData) -> None:
    """Async task: detect dead workers, restart, notify. D-05."""
    while not runtime.stop_event.is_set():
        try:
            await asyncio.wait_for(
                runtime.loop_stop_event.wait(), timeout=WATCHDOG_INTERVAL_S
            )
            return  # stop_event side-channel via loop_stop_event
        except asyncio.TimeoutError:
            pass   # normal cadence tick

        for attr, component in [
            ("states_worker", "states_worker"),
            ("meta_worker", "meta_worker"),
        ]:
            thread = getattr(runtime, attr)
            if not thread.is_alive() and not runtime.stop_event.is_set():
                exc = getattr(thread, "_last_exception", None)
                ctx = getattr(thread, "_last_context", {})
                notify_watchdog_recovery(hass, component=component, exc=exc, context=ctx)
                new_thread = _spawn_worker(hass, runtime, component)
                setattr(runtime, attr, new_thread)
                new_thread.start()
```

Key design note: `watchdog_loop` uses `runtime.loop_stop_event.wait()` (the same `asyncio.Event` used by the orchestrator) as its interruptible sleep. This mirrors the existing pattern in `_overflow_watcher` in `__init__.py` and avoids adding a third event.
[VERIFIED: existing `__init__.py` `_overflow_watcher` pattern]

### Pattern 3: Orchestrator Done Callback

**What:** Sync callback attached to orchestrator task; relaunches on unhandled exception.
**When to use:** Attached in `async_setup_entry` after `hass.async_create_task(backfill_orchestrator(...))`.

```python
# Source: verified asyncio.Task.add_done_callback semantics via Python stdlib + local test

def _make_orchestrator_done_callback(hass, runtime, **orchestrator_kwargs):
    """Return a closure that relaunches the orchestrator on crash. D-04-b."""
    def _on_orchestrator_done(task: asyncio.Task) -> None:
        # Order matters: check cancelled BEFORE exception() — cancelled task
        # raises CancelledError from exception().
        if task.cancelled():
            return
        if runtime.stop_event.is_set():
            return
        exc = task.exception()
        if exc is None:
            return  # clean exit (defensive)
        notify_watchdog_recovery(
            hass, component="orchestrator", exc=exc,
            context={"at": datetime.now(timezone.utc).isoformat()},
        )
        new_task = hass.async_create_task(backfill_orchestrator(**orchestrator_kwargs))
        new_task.add_done_callback(_on_orchestrator_done)  # recursive attachment
        runtime.orchestrator_task = new_task
    return _on_orchestrator_done
```

[VERIFIED: asyncio.Task.add_done_callback callback is synchronous; task.exception() raises CancelledError on cancelled task; creating new tasks from within callback is safe — local test confirmed]

### Pattern 4: Issue Helpers (Extended issues.py)

**What:** Thin wrapper functions following the Phase 2 `create_buffer_dropping_issue` shape.
**When to use:** Called directly on event loop, or via `hass.add_job(helper, hass)` from worker thread.

```python
# Source: verified ir.async_create_issue / async_delete_issue signatures from
# installed homeassistant/helpers/issue_registry.py

_STATES_WORKER_STALLED_ID = "states_worker_stalled"
_META_WORKER_STALLED_ID = "meta_worker_stalled"
_DB_UNREACHABLE_ID = "db_unreachable"
_RECORDER_DISABLED_ID = "recorder_disabled"

def create_states_worker_stalled_issue(hass: HomeAssistant) -> None:
    ir.async_create_issue(
        hass, domain=DOMAIN, issue_id=_STATES_WORKER_STALLED_ID,
        is_fixable=False, severity=ir.IssueSeverity.ERROR,
        translation_key=_STATES_WORKER_STALLED_ID,
    )

def clear_states_worker_stalled_issue(hass: HomeAssistant) -> None:
    ir.async_delete_issue(hass, DOMAIN, _STATES_WORKER_STALLED_ID)

# meta_worker_stalled, db_unreachable, recorder_disabled follow identical shape
# db_unreachable uses IssueSeverity.ERROR; recorder_disabled uses IssueSeverity.WARNING
```

CRITICAL: `is_fixable` and `severity` are **required keyword arguments** (no defaults). Omitting either raises `TypeError` at runtime.
[VERIFIED: direct inspection of `homeassistant/helpers/issue_registry.py` line 331-365 in installed HA 2026.3.4]

### Pattern 5: notify_watchdog_recovery (notifications.py)

**What:** Fires a `_LOGGER.error` with full traceback + a persistent_notification with abridged traceback.
**When to use:** Called on event loop directly (from watchdog and done_callback) or bridged from workers.

```python
# Source: verified persistent_notification.async_create signature from
# installed homeassistant/components/persistent_notification/__init__.py

import traceback
from homeassistant.components import persistent_notification

def notify_watchdog_recovery(
    hass: HomeAssistant,
    component: str,
    exc: BaseException | None,
    context: dict | None = None,
) -> None:
    ctx = context or {}
    _LOGGER.error(
        "%s restarted after unhandled exception: %s",
        component, exc, exc_info=exc,
    )
    # Abridged traceback: join last 10 parts from format_exception
    # format_exception(exc) returns a list of strings (each ending with \n).
    # Slicing [-10:] on the list gives the last 10 parts (usually covers
    # the 6-8 most recent frames + exception line for typical tracebacks).
    tb_parts = traceback.format_exception(exc) if exc is not None else []
    tb_text = "".join(tb_parts[-10:]) if tb_parts else "(no traceback)"

    at = ctx.get("at", "unknown")
    mode = ctx.get("mode")
    retry_attempt = ctx.get("retry_attempt")
    last_op = ctx.get("last_op")

    lines = [
        f"**Component:** {component}",
        f"**Exception:** `{exc.__class__.__name__}: {exc}`" if exc else "**Exception:** (none)",
        f"**At:** {at}",
        "**Context:**",
        f"- mode: {mode}",
        f"- retry_attempt: {retry_attempt}",
        f"- last_op: {last_op}",
    ]
    # Append any extra context keys not in the standard set
    for k, v in ctx.items():
        if k not in {"at", "mode", "retry_attempt", "last_op"}:
            lines.append(f"- {k}: {v}")
    lines += [
        "",
        "**Traceback (last 10 parts):**",
        f"```\n{tb_text}```",
        "",
        "See `home-assistant.log` for the full traceback.",
    ]
    body = "\n".join(lines)

    persistent_notification.async_create(
        hass,
        message=body,
        title=f"TimescaleDB recorder: {component} restarted",
        notification_id=f"timescaledb_recorder.watchdog_{component}",
    )
```

[VERIFIED: `async_create` is `@callback`; same notification_id replaces existing notification; positional args match function signature exactly]

### Pattern 6: recorder_disabled Detection (async_setup_entry)

**What:** One-shot check before subscribing to state_changed events.
**When to use:** Step 7 of `async_setup_entry` sequence, between meta-drain and state subscription.

```python
# Source: verified recorder.get_instance + DATA_INSTANCE + Recorder.enabled
# from installed HA 2026.3.4 homeassistant/helpers/recorder.py + components/recorder/core.py

from homeassistant.components import recorder as ha_recorder

try:
    recorder_instance = ha_recorder.get_instance(hass)
    if not recorder_instance.enabled:
        # Recorder loaded but disabled at runtime via recorder.disable service
        create_recorder_disabled_issue(hass)
except KeyError:
    # Recorder component was not loaded at all (excluded from configuration.yaml
    # or failed to initialize)
    recorder_instance = None
    create_recorder_disabled_issue(hass)
```

Note: the same `recorder.get_instance(hass)` call at the top of `backfill_orchestrator` will crash with `KeyError` if recorder is not loaded — the existing code in `backfill.py` line 81 needs a corresponding guard (or the orchestrator should catch the exception naturally, which the done_callback will then handle).
[VERIFIED: `get_instance` definition is `return hass.data[DATA_INSTANCE]` — raises `KeyError` when key absent. `Recorder.enabled` defaults to `True`, can be set `False` via `set_enable()`. Confirmed in installed HA 2026.3.4.]

### Pattern 7: Gap Detection (backfill_orchestrator)

**What:** Compare watermark boundary against oldest recorder state before slice loop.
**When to use:** Top of each orchestrator backfill cycle, after watermark read.

```python
# Source: verified recorder_instance.states_manager.oldest_ts type (float | None)
# from installed HA 2026.3.4 table_managers/states.py

from datetime import datetime, timezone

# oldest_ts is a float | None epoch timestamp cached by StatesManager
# Reading it on the event loop is safe (GIL-protected float read; not thread-unsafe
# in the write-protection sense — only write operations must be on recorder thread)
oldest_ts_float = recorder_instance.states_manager.oldest_ts

if oldest_ts_float is not None:
    oldest_recorder_ts = datetime.fromtimestamp(oldest_ts_float, tz=timezone.utc)
else:
    # Recorder has no data at all — treat entire window as gap
    oldest_recorder_ts = datetime.now(timezone.utc)

needed_from = wm - _LATE_ARRIVAL_GRACE  # wm - 10min, same as from_

if oldest_recorder_ts > needed_from:
    gap_start = needed_from
    gap_end = oldest_recorder_ts
    duration_minutes = int((gap_end - gap_start).total_seconds() // 60)
    notify_backfill_gap(
        hass,
        reason="recorder_retention",
        details={
            "window_start": gap_start.isoformat(),
            "window_end": gap_end.isoformat(),
            "duration_minutes": duration_minutes,
        },
    )
    from_ = oldest_recorder_ts  # skip unreachable prefix
```

[VERIFIED: `StatesManager.oldest_ts` returns `float | None`; confirmed via `StatesManager()` instantiation in HA 2026.3.4 venv]

### Anti-Patterns to Avoid

- **Checking `task.exception()` on a cancelled task:** Raises `CancelledError`. Always check `task.cancelled()` first in `_on_orchestrator_done`.
- **Using `async def` for done_callback:** `add_done_callback` requires a synchronous callable. An async def will not be awaited.
- **Passing kwargs directly to `hass.add_job`:** `hass.add_job` only accepts positional args after the callable. Wrap kwargs-heavy calls like `ir.async_create_issue` inside helper functions (`create_states_worker_stalled_issue(hass)`) that take only `hass` as argument.
- **Reading `states_manager.oldest_ts` via executor job:** Unnecessary — it's a pure in-memory float read. Direct access on the event loop is safe.
- **Calling `notify_watchdog_recovery` directly from a worker thread:** It calls `persistent_notification.async_create` which is a `@callback` — safe to schedule via `hass.add_job`, but the watchdog itself runs on the event loop so direct calls dominate in practice.
- **Starting a new worker thread from within `_on_orchestrator_done`:** The done_callback is for the orchestrator (asyncio Task), not the worker threads. Worker thread respawning belongs in `watchdog_loop`.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| HA repair issue UI | Custom UI components | `ir.async_create_issue` / `ir.async_delete_issue` | HA handles deduplication, UI rendering, lifecycle |
| In-app notifications | Custom notification component | `persistent_notification.async_create` | HA handles UI, persistence, notification_id dedup (same id replaces) |
| Exception formatting | Custom formatter | `traceback.format_exception(exc)` | stdlib handles chained exceptions, cause chains, etc. |
| Thread liveness | OS-level process monitoring | `threading.Thread.is_alive()` | Non-blocking, accurate, no extra dependencies |
| asyncio task crash recovery | Manual task state machine | `asyncio.Task.add_done_callback` | stdlib mechanism; callback guaranteed to run exactly once on task completion |

**Key insight:** Phase 3 is pure observation + bridging. Every building block is either HA-provided or stdlib — zero new third-party dependencies.

## Common Pitfalls

### Pitfall 1: `task.exception()` on a cancelled task raises `CancelledError`

**What goes wrong:** `_on_orchestrator_done` calls `task.exception()` without first checking `task.cancelled()`. The callback raises `CancelledError` and the relaunch logic never runs. Worse: in `async_unload_entry`, cancelling the orchestrator task will cause the callback to raise inside the event loop, logging a confusing traceback.

**Why it happens:** Developers read "exception() returns the exception if one was raised" and assume it returns None for cancellation. It does not — it raises `CancelledError`.

**How to avoid:** Always: `if task.cancelled(): return` — THEN `exc = task.exception()`.

**Warning signs:** `asyncio.CancelledError` in HA logs with a traceback pointing to `_on_orchestrator_done`.

[VERIFIED: Python stdlib confirmed via local test — `task.exception()` raises `CancelledError` when `task.cancelled()` is `True`]

### Pitfall 2: `hass.add_job` with keyword arguments

**What goes wrong:** A worker thread calls `hass.add_job(ir.async_create_issue, hass, DOMAIN, issue_id, is_fixable=False, ...)`. `hass.add_job` signature is `add_job(target, *args)` — it passes `*args` positionally. Keyword arguments are silently dropped or cause a `TypeError`.

**Why it happens:** `add_job` is documented as "positional args only". The `ir.async_create_issue` function has `*` forcing keyword-only args after `issue_id`.

**How to avoid:** Wrap the full call in a helper function: `create_states_worker_stalled_issue(hass)` which takes only `hass` and internally calls `ir.async_create_issue` with all kwargs baked in. Then: `hass.add_job(create_states_worker_stalled_issue, hass)`.

**Warning signs:** `TypeError: async_create_issue() got unexpected keyword argument` during testing.

[VERIFIED: `hass.add_job` uses `*args` positional passing; `ir.async_create_issue` has keyword-only params after `issue_id`]

### Pitfall 3: `get_instance` raises `KeyError` when recorder not loaded

**What goes wrong:** `backfill_orchestrator` calls `recorder.get_instance(hass)` at startup (line 81). If the user has excluded recorder from their `configuration.yaml`, this raises `KeyError` immediately on orchestrator start. The done_callback then relaunches the orchestrator indefinitely, flooding logs with `KeyError` tracebacks.

**Why it happens:** `get_instance` is defined as `return hass.data[DATA_INSTANCE]` — no None-check, no fallback.

**How to avoid:** In `async_setup_entry` step 7 (before orchestrator spawn), catch `KeyError` from `recorder.get_instance(hass)` and fire the `recorder_disabled` repair issue. Pass `recorder_instance=None` to the orchestrator and add a guard at the top of the orchestrator that returns early (or awaits indefinitely on `backfill_request`) when `recorder_instance is None`.

**Warning signs:** Rapid notification storm for `orchestrator restarted` with `KeyError` tracebacks, no data in ha_states.

[VERIFIED: `get_instance` source code inspection; `hass.data[DATA_INSTANCE]` — no defensive check present]

### Pitfall 4: Worker `run()` outer try/except swallows all exceptions including expected shutdown

**What goes wrong:** The outer `try/except Exception` in `run()` catches `queue.Empty` or other control-flow exceptions that were intended to be handled by inner logic. The thread records a "crash" with a benign exception as `_last_exception`.

**Why it happens:** The outer handler is broad by design (D-06-a). But `queue.Empty` raised by `backfill_queue.get(timeout=5)` is caught by the inner `except queue.Empty` block — it never reaches the outer handler. Only truly unhandled exceptions escape the inner loop.

**How to avoid:** Structure the `run()` method so the inner per-mode loop handles expected exceptions (queue.Empty, stop_event check). The outer `except` is a last-resort guard for unexpected exceptions outside the inner loop (e.g., attribute errors in mode-switching logic, bugs in schema setup). Document this layering in code comments.

**Warning signs:** Watchdog detects a "dead" thread with `_last_exception` = `queue.Empty` — this indicates an inner handler was accidentally removed.

### Pitfall 5: `states_manager.oldest_ts` returns `None` when recorder has no data

**What goes wrong:** Gap detection assumes `oldest_ts_float` is a valid timestamp. If recorder has no states yet (brand-new HA install), `oldest_ts` is `None`. Treating `None` as `utcnow()` correctly makes the full backfill window a gap, but calling `datetime.fromtimestamp(None)` raises `TypeError`.

**How to avoid:** Explicit `None` guard: `if oldest_ts_float is None: oldest_recorder_ts = datetime.now(timezone.utc)`.

**Warning signs:** `TypeError: an integer is required` in orchestrator traceback.

[VERIFIED: `StatesManager.__init__` sets `self._oldest_ts: float | None = None`]

### Pitfall 6: Done callback attached to wrong task reference

**What goes wrong:** `async_setup_entry` wraps `orchestrator_task` in a closure variable, but when the orchestrator crashes and relaunches, the new task reference replaces `runtime_data.orchestrator_task`. `async_unload_entry` cancels `entry.runtime_data.orchestrator_task` — if the callback forgot to update `runtime_data.orchestrator_task = new_task`, it cancels a stale (already-finished) task.

**How to avoid:** `_on_orchestrator_done` MUST update `runtime.orchestrator_task = new_task` before returning, so the unload handler always holds a reference to the live task.

**Warning signs:** `async_unload_entry` returns successfully but the orchestrator keeps running after unload (no cancellation signal reached it).

## Runtime State Inventory

Not applicable — Phase 3 is greenfield (two new modules) plus additive changes to existing modules. No rename, refactor, or migration of stored state.

## Code Examples

### Verified: `async_create_issue` Full Signature

```python
# Source: installed homeassistant/helpers/issue_registry.py (HA 2026.3.4)
@callback
def async_create_issue(
    hass: HomeAssistant,
    domain: str,
    issue_id: str,
    *,
    breaks_in_ha_version: str | None = None,
    data: dict[str, str | int | float | None] | None = None,
    is_fixable: bool,                   # REQUIRED — no default
    is_persistent: bool = False,
    issue_domain: str | None = None,
    learn_more_url: str | None = None,
    severity: IssueSeverity,            # REQUIRED — no default
    translation_key: str,               # REQUIRED — no default
    translation_placeholders: dict[str, str] | None = None,
) -> None:
    """Create an issue, or replace an existing one."""
```

[VERIFIED: direct source inspection, HA 2026.3.4]

### Verified: `persistent_notification.async_create` Full Signature

```python
# Source: installed homeassistant/components/persistent_notification/__init__.py (HA 2026.3.4)
@callback
def async_create(
    hass: HomeAssistant,
    message: str,
    title: str | None = None,
    notification_id: str | None = None,
) -> None:
    """Generate a notification."""
    # notifications[notification_id] = {...} — REPLACES existing if same id
```

[VERIFIED: direct source inspection, HA 2026.3.4]

### Verified: `IssueSeverity` Enum Values

```python
# Source: HA 2026.3.4 venv (uv run confirmed)
class IssueSeverity(StrEnum):
    CRITICAL = "critical"
    ERROR = "error"
    WARNING = "warning"
```

[VERIFIED: `uv run python3 -c "from homeassistant.helpers.issue_registry import IssueSeverity; print(list(IssueSeverity))"` → confirmed all three values]

### Verified: `traceback.format_exception` Behavior

```python
# Source: Python 3.14 stdlib (confirmed via local execution)

# format_exception(exc) — single-arg form available Python 3.10+
# Returns list of strings, each ending with \n
# To get "last 10 parts" for notification body:
tb_text = "".join(traceback.format_exception(exc)[-10:])

# Note: "parts" ≠ "lines". One part may span multiple lines (e.g., exception cause chains).
# For a typical 4-frame traceback: ~7 parts total.
# For a deep traceback with "[Previous line repeated N times]" — CPython compresses, so
# still only ~8 parts. "[-10:]" is generous and works well in practice.
```

[VERIFIED: local Python 3.14 execution of `traceback.format_exception`]

### Verified: `strings.json` "issues" Section Pattern

```json
{
  "issues": {
    "states_worker_stalled": {
      "title": "TimescaleDB recorder states worker stalled",
      "description": "..."
    },
    "meta_worker_stalled": {
      "title": "TimescaleDB recorder metadata worker stalled",
      "description": "..."
    },
    "db_unreachable": {
      "title": "TimescaleDB database unreachable",
      "description": "..."
    },
    "recorder_disabled": {
      "title": "Home Assistant recorder integration is disabled",
      "description": "..."
    }
  }
}
```

Translation key in `async_create_issue` MUST match the JSON key exactly. HA logs a warning for missing keys; the issue is still created but without user-friendly text.
[VERIFIED: existing `buffer_dropping` pattern in `issues.py` + `strings.json`; HA issue_registry confirmed]

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `run_coroutine_threadsafe` for issue creation from worker | `hass.add_job(helper, hass)` | Phase 1 (WATCH-02 deadlock risk discovered) | No deadlock risk on shutdown |
| Direct `ir.async_create_issue(...)` from worker thread | `hass.add_job(create_*_issue, hass)` bridge | Phase 2 (D-10) | Thread-safe; non-blocking |
| Broad except + stall notification only | Broad except + stall + recovery + sustained-fail | Phase 3 (D-03) | Auto-clearing issues; db_unreachable surface |

**Current (Phase 3) stack:** `@callback` for all issue/notification APIs; `asyncio.Task.add_done_callback` for orchestrator recovery; `threading.Thread.is_alive()` for watchdog; `recorder_instance.states_manager.oldest_ts` for gap detection.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `states_manager.oldest_ts` can be read safely from the asyncio event loop (GIL-protected float read) | Pattern 7 / Gap Detection | If HA moves to free-threaded Python (no GIL), a concurrent recorder thread write could race; mitigation: use `async_add_executor_job` to read it |
| A2 | `watchdog_loop` using `runtime.loop_stop_event.wait()` as interruptible sleep is equivalent to the `stop_event` interrupt used by the retry decorator | Pattern 2 / Watchdog | If `loop_stop_event` is not set during shutdown (e.g., bug in `async_unload_entry`), watchdog won't wake; fallback is the `WATCHDOG_INTERVAL_S` timeout |
| A3 | `Recorder.enabled` attribute (`instance.enabled`) is the correct flag for runtime-disabled detection | Pattern 6 / recorder_disabled | Confirmed `.enabled` exists and defaults `True`; `set_enable(False)` sets it. If HA changes this in a future version, detection breaks silently |

**If this table is empty:** All claims in this research were verified or cited — no user confirmation needed.

Claims A1-A3 are LOW risk and based on verified code, but involve behavioral assumptions about the HA threading model and future compatibility.

## Open Questions

1. **`_fetch_slice_raw` stall: own repair issue or notification-only?**
   - What we know: D-03-c wraps `_fetch_slice_raw` with `on_transient=None`. The `on_stall` hook could fire `create_backfill_fetch_stalled_issue`, symmetrically with write-side stalls.
   - What's unclear: Is a stalled `_fetch_slice_raw` (recorder pool thread stuck reading sqlite) surfaced adequately by the orchestrator's eventual relaunch (which fires `notify_watchdog_recovery`)? Or does the user need a Repairs-UI issue specifically for "backfill read stall"?
   - Recommendation: Keep it notification-only via orchestrator relaunch (simpler). If user requests finer granularity in v2, add a `backfill_fetch_stalled` issue then. Document this as a known gap in Phase 3 scope.

2. **Watchdog factory: `watchdog.py`-owned spawn helpers vs `__init__.py` refactor**
   - What we know: Thread construction requires the same `hass`, `dsn`, `live_queue`, `stop_event`, etc. that are already on `runtime_data`. The watchdog needs to construct a new `Thread` identical to what `async_setup_entry` originally built.
   - Recommendation: Define `spawn_states_worker(hass, runtime) -> Thread` and `spawn_meta_worker(hass, runtime) -> Thread` as module-level helpers in `watchdog.py`. They accept `runtime: HaTimescaleDBData` which carries all required constructor args. This avoids importing `__init__.py` helpers into `watchdog.py` (circular import risk).

3. **`db_unreachable` `sustained_fail_seconds` constant placement**
   - The `DB_UNREACHABLE_THRESHOLD_SECONDS = 300` constant should live in `const.py` per project convention (D-03-d places `STALL_THRESHOLD` there). The `sustained_fail_seconds` parameter to `retry_until_success` should default to this constant.

## Environment Availability

Step 2.6: SKIPPED (no new external dependencies — Phase 3 uses only existing HA built-ins + stdlib).

## Sources

### Primary (HIGH confidence)

- Installed `homeassistant==2026.3.4` — `homeassistant/helpers/issue_registry.py` lines 330-426 — `async_create_issue` / `async_delete_issue` exact signatures, `IssueSeverity` enum values
- Installed `homeassistant==2026.3.4` — `homeassistant/components/persistent_notification/__init__.py` lines 79-118 — `async_create` signature, notification_id replacement behavior
- Installed `homeassistant==2026.3.4` — `homeassistant/helpers/recorder.py` lines 73-76 — `get_instance` definition (`return hass.data[DATA_INSTANCE]`, raises `KeyError`)
- Installed `homeassistant==2026.3.4` — `homeassistant/components/recorder/core.py` lines 229, 267-269 — `Recorder.enabled` attribute and `set_enable()` method
- Installed `homeassistant==2026.3.4` — `homeassistant/components/recorder/table_managers/states.py` — `StatesManager.oldest_ts` property returns `float | None`
- Installed `homeassistant==2026.3.4` — `homeassistant/core.py` lines 542-560 — `hass.add_job` positional-only `*args` constraint, `@callback` detection
- [Python docs: asyncio.Future.add_done_callback](https://docs.python.org/3/library/asyncio-future.html#asyncio.Future.add_done_callback) — callback is sync, receives Future as sole arg
- Local Python 3.14 execution — `asyncio.Task.add_done_callback` / `task.exception()` / `task.cancelled()` semantics confirmed via test script
- Local Python 3.14 execution — `traceback.format_exception(exc)` single-arg form, returns list of strings, `[-10:]` slice behavior confirmed
- Local Python 3.14 execution — `threading.Thread.is_alive()` confirmed non-blocking

### Secondary (MEDIUM confidence)

- [Python asyncio docs](https://docs.python.org/3/library/asyncio-task.html) — Task lifecycle, add_done_callback runs on event loop via call_soon
- Existing `custom_components/ha_timescaledb_recorder/issues.py` — Phase 2 pattern confirmed for `@callback` issue helpers + `hass.add_job` bridge
- Existing `custom_components/ha_timescaledb_recorder/__init__.py` — `_overflow_watcher` pattern confirms `asyncio.wait_for(event.wait(), timeout=N)` as interruptible sleep

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — all APIs verified against installed HA 2026.3.4 source
- Architecture: HIGH — patterns derived from existing Phase 2 code + stdlib docs
- Pitfalls: HIGH — all verified via direct source inspection or local execution
- API signatures: HIGH — read from installed source files, not training data

**Research date:** 2026-04-22
**Valid until:** 2026-05-22 (HA releases monthly; API stability HIGH within minor version)
