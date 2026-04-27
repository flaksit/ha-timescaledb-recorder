# Phase 3: Hardening and Observability - Pattern Map

**Mapped:** 2026-04-22
**Files analyzed:** 10
**Analogs found:** 10 / 10

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `retry.py` | utility | event-driven (hook callbacks) | `retry.py` (self) | exact — additive extension |
| `states_worker.py` | service | CRUD + event-driven | `states_worker.py` (self) | exact — additive extension |
| `meta_worker.py` | service | CRUD + event-driven | `meta_worker.py` (self) | exact — additive extension |
| `backfill.py` | service | request-response + batch | `backfill.py` (self) | exact — additive extension |
| `issues.py` | utility | request-response | `issues.py` (self) | exact — additive extension |
| `notifications.py` | utility | request-response | `states_worker._notify_stall` + `issues.py` | role-match (same HA bridge pattern) |
| `watchdog.py` | service | event-driven (polling) | `__init__._overflow_watcher` | role-match (same asyncio polling loop shape) |
| `strings.json` | config | — | `strings.json` (self) | exact — additive extension |
| `const.py` | config | — | `const.py` (self) | exact — additive extension |
| `__init__.py` | provider | request-response | `__init__.py` (self) | exact — additive extension |

## Pattern Assignments

### `retry.py` (utility, event-driven)

**Analog:** `custom_components/timescaledb_recorder/retry.py` (self — additive extension)

**Existing signature** (lines 25-32):
```python
def retry_until_success(
    *,
    stop_event: threading.Event,
    on_transient: Callable[[], None] | None = None,
    notify_stall: Callable[[int], None] | None = None,
    backoff_schedule: tuple[int, ...] = _BACKOFF_SCHEDULE,
    stall_threshold: int = _STALL_NOTIFY_THRESHOLD,
) -> Callable:
```

**New parameters to add** (D-03-a, D-11):
- `on_recovery: Callable[[], None] | None = None` — fired once after a successful call that follows a stall.
- `on_sustained_fail: Callable[[], None] | None = None` — fired once when cumulative fail duration exceeds `sustained_fail_seconds`.
- `sustained_fail_seconds: float = 300.0` — threshold; default matches `DB_UNREACHABLE_THRESHOLD_SECONDS` from `const.py`.

**New closure state to add** (lines inserted in `wrapper`):
```python
stalled = False              # arms on_recovery; cleared on success
first_fail_ts: float | None = None   # arms on_sustained_fail
sustained_notified = False
```

**Existing success path** (lines 98-102) — add recovery hook here:
```python
# Success path: reset notify-armed state so a future stall notifies again.
if notified:
    notified = False
    attempts = 0
return result
```
Becomes:
```python
# Success path: reset all notify-armed state.
if stalled and on_recovery is not None:
    try:
        on_recovery()
    except Exception:  # noqa: BLE001
        _LOGGER.exception("on_recovery hook raised; continuing")
stalled = False
notified = False
sustained_notified = False
first_fail_ts = None
attempts = 0
return result
```

**Existing on_transient call** (lines 66-73) — `on_transient` is already optional (`| None`); the existing `if on_transient is not None:` guard requires no change. Just ensure `stalled = True` is set when `notified` flips (alongside `notified = True` at line 83):
```python
notified = True
stalled = True   # arm on_recovery tracking
```

**Sustained-fail hook** — insert after the existing `if not notified and attempts >= stall_threshold:` block:
```python
import time  # add to module imports
now_mono = time.monotonic()
if first_fail_ts is None:
    first_fail_ts = now_mono
if (
    not sustained_notified
    and on_sustained_fail is not None
    and now_mono - first_fail_ts >= sustained_fail_seconds
):
    try:
        on_sustained_fail()
    except Exception:  # noqa: BLE001
        _LOGGER.exception("on_sustained_fail hook raised; continuing")
    sustained_notified = True
```

**Module-level constant to remove** (line 22) — `_STALL_NOTIFY_THRESHOLD = 5` moves to `const.py` as `STALL_THRESHOLD = 5` per D-03-d. Import it here:
```python
from .const import STALL_THRESHOLD as _STALL_NOTIFY_THRESHOLD
```

---

### `states_worker.py` (service, CRUD + event-driven)

**Analog:** `custom_components/timescaledb_recorder/states_worker.py` (self)

**Existing retry wiring in `__init__`** (lines 88-93):
```python
self._insert_chunk = retry_until_success(
    stop_event=stop_event,
    on_transient=self.reset_db_connection,
    notify_stall=self._notify_stall,
)(self._insert_chunk_raw)
```
Extend with `on_recovery` and `on_sustained_fail` hooks (D-02-c, D-11):
```python
self._insert_chunk = retry_until_success(
    stop_event=stop_event,
    on_transient=self.reset_db_connection,
    notify_stall=self._stall_hook,
    on_recovery=self._recovery_hook,
    on_sustained_fail=self._sustained_fail_hook,
)(self._insert_chunk_raw)
```

**Existing `_notify_stall` method** (lines 114-133) — rename to `_stall_hook` for symmetry with `_recovery_hook`; behaviour stays the same. Add two new methods below it:

```python
def _stall_hook(self, attempts: int) -> None:
    """Fire stall notification + worker_stalled repair issue (D-02, D-03-a)."""
    # ... existing _notify_stall body ...
    from .issues import create_states_worker_stalled_issue
    self._hass.add_job(create_states_worker_stalled_issue, self._hass)

def _recovery_hook(self) -> None:
    """Clear worker_stalled repair issue on first success after stall (D-02-c, D-03-a)."""
    _LOGGER.info("states worker recovered after stall — clearing repair issue")
    from .issues import clear_states_worker_stalled_issue
    self._hass.add_job(clear_states_worker_stalled_issue, self._hass)

def _sustained_fail_hook(self) -> None:
    """Fire db_unreachable repair issue after sustained failure (D-11)."""
    _LOGGER.warning("states worker: DB unreachable for > threshold — firing repair issue")
    from .issues import create_db_unreachable_issue
    self._hass.add_job(create_db_unreachable_issue, self._hass)
```

**New instance attributes for watchdog** — add to `__init__` after `self._conn = None` (D-06-b):
```python
self._last_exception: Exception | None = None
self._last_context: dict = {}
```

**Existing `run()` method** (lines 139-284) — wrap the main loop body in outer try/except (D-06-a). The current `run()` starts with `try: ... except Exception:` only around schema setup. Add an **outer** try/except around the entire `while not self._stop_event.is_set():` block plus the shutdown final-flush section:

```python
def run(self) -> None:
    """Thread entry point. Outer try/except catches unhandled bugs (D-06-a)."""
    try:
        # ... existing schema setup try/except block (unchanged) ...
        mode = MODE_INIT
        # ... existing while loop + shutdown flush + conn.close (all unchanged) ...
    except Exception as err:  # noqa: BLE001 — last-resort guard for bugs outside retry scope
        _LOGGER.error("%s died with unhandled exception", self.name, exc_info=True)
        self._last_exception = err
        self._last_context = {
            "at": datetime.now(timezone.utc).isoformat(),
            "mode": mode if "mode" in dir() else "unknown",  # captured before crash
            "last_op": "unknown",  # inner loop should update self._last_op before each op
            "retry_attempt": None,
        }
        # Thread exits naturally — watchdog detects is_alive() == False on next tick.
```

**Note on `_last_context` population:** Workers should update `self._last_op` before each retried operation so the watchdog captures a meaningful `last_op` on crash. Pattern for mode tracking: update a local `mode` variable and set `self._last_mode = mode` on each transition.

---

### `meta_worker.py` (service, CRUD + event-driven)

**Analog:** `custom_components/timescaledb_recorder/meta_worker.py` (self) — identical shape to `states_worker.py` additions.

**Existing retry wiring in `__init__`** (lines 89-93):
```python
self._write_item = retry_until_success(
    stop_event=stop_event,
    on_transient=self.reset_db_connection,
    notify_stall=self._notify_stall,
)(self._write_item_raw)
```
Extend with the same three hooks as states_worker (D-02, D-03-a):
```python
self._write_item = retry_until_success(
    stop_event=stop_event,
    on_transient=self.reset_db_connection,
    notify_stall=self._stall_hook,
    on_recovery=self._recovery_hook,
    on_sustained_fail=self._sustained_fail_hook,
)(self._write_item_raw)
```

**New hook methods** — same shape as states_worker but referencing `meta_worker_stalled` issue helpers:
```python
def _stall_hook(self, attempts: int) -> None:
    from .issues import create_meta_worker_stalled_issue
    self._hass.add_job(create_meta_worker_stalled_issue, self._hass)

def _recovery_hook(self) -> None:
    from .issues import clear_meta_worker_stalled_issue
    self._hass.add_job(clear_meta_worker_stalled_issue, self._hass)

def _sustained_fail_hook(self) -> None:
    from .issues import create_db_unreachable_issue
    self._hass.add_job(create_db_unreachable_issue, self._hass)
```

**New instance attributes** — identical pattern to states_worker:
```python
self._last_exception: Exception | None = None
self._last_context: dict = {}
```

**Outer try/except in `run()`** (lines 143-163) — same D-06-a pattern. Current `run()` has no outer guard; wrap the `while not self._stop_event.is_set():` block plus the final conn-close in an outer `try/except Exception`.

---

### `backfill.py` (service, request-response + batch)

**Analog:** `custom_components/timescaledb_recorder/backfill.py` (self)

**Wrap `_fetch_slice_raw` with retry** (D-03-c) — in `backfill_orchestrator`, the call at line 142-144:
```python
raw = await recorder_instance.async_add_executor_job(
    _fetch_slice_raw, hass, entities, slice_start, slice_end,
)
```
Changes to wrap `_fetch_slice_raw` at definition time using `retry_until_success` with `on_transient=None` and a `stop_event` derived from the `stop_event` asyncio.Event. **However**: the retry decorator uses `threading.Event`, not `asyncio.Event`. Since `_fetch_slice_raw` runs in the recorder executor pool (a thread), a `threading.Event` adapter is needed. The existing pattern in `states_worker.py` shows the `stop_event` passed into the decorator is `threading.Event`. The orchestrator holds an `asyncio.Event` (`loop_stop_event`). Resolution: pass the `threading.Event` from `runtime_data.stop_event` (the same one workers use) to the retry-wrapped version of `_fetch_slice_raw`. This is consistent with the worker pattern.

Alternative: apply retry at call-site using a closure that captures `stop_event`. This mirrors how workers wrap in `__init__`. Either approach is valid; planner should specify which.

**Existing `backfill_orchestrator` signature** (lines 59-68):
```python
async def backfill_orchestrator(
    hass: HomeAssistant,
    *,
    live_queue,
    backfill_queue: queue.Queue,
    backfill_request: asyncio.Event,
    read_watermark: Callable[[], "datetime | None"],
    open_entities_reader: Callable[[], set],
    entity_filter: Callable[[str], bool],
    stop_event: asyncio.Event,
) -> None:
```
Add `threading_stop_event: threading.Event` parameter to carry the threading-event-compatible stop signal into the retry-wrapped `_fetch_slice_raw`. Alternatively, the planner may thread `stop_event` through differently — this is a planner discretion item.

**Wrap `read_watermark` with retry** (D-03-c) — `read_watermark` is passed as a callable (line 65). The existing `read_watermark` method on `states_worker` is NOT yet wrapped. Wrap it in the worker's `__init__` (same as `_insert_chunk`):
```python
self.read_watermark = retry_until_success(
    stop_event=stop_event,
    on_transient=self.reset_db_connection,
    # no on_recovery or on_sustained_fail needed for watermark reads —
    # failure here manifests as stalled backfill, covered by orchestrator done_callback
)(self._read_watermark_raw)
```

**Gap detection** — insert at top of each backfill cycle, after watermark read at line 99 and before `from_` computation at line 108 (D-08-a):
```python
from_ = wm - _LATE_ARRIVAL_GRACE

# D-08: gap detection — compare needed_from against oldest recorder state.
oldest_ts_float = recorder_instance.states_manager.oldest_ts
if oldest_ts_float is None:
    oldest_recorder_ts = datetime.now(timezone.utc)
else:
    oldest_recorder_ts = datetime.fromtimestamp(oldest_ts_float, tz=timezone.utc)

if oldest_recorder_ts > from_:
    gap_end = oldest_recorder_ts
    duration_minutes = int((gap_end - from_).total_seconds() // 60)
    notify_backfill_gap(
        hass,
        reason="recorder_retention",
        details={
            "window_start": from_.isoformat(),
            "window_end": gap_end.isoformat(),
            "duration_minutes": duration_minutes,
        },
    )
    from_ = oldest_recorder_ts
```
Import `notify_backfill_gap` from `.notifications` (new module).

**Orchestrator outer try/except** — The orchestrator itself does NOT need an inner try/except. Its unhandled exceptions propagate to the asyncio Task; the `add_done_callback` in `__init__.py` catches them. Do NOT add a swallowing try/except in the orchestrator body — that would prevent the done_callback from seeing the exception. Any exception that escapes the `while` loop will naturally exit the coroutine with an exception attached to the Task.

---

### `issues.py` (utility, request-response)

**Analog:** `custom_components/timescaledb_recorder/issues.py` (self — additive extension)

**Existing create/clear pair pattern** (lines 18-40) — copy exactly for each new issue:
```python
_BUFFER_DROPPING_ISSUE_ID = "buffer_dropping"

def create_buffer_dropping_issue(hass: HomeAssistant) -> None:
    ir.async_create_issue(
        hass,
        domain=DOMAIN,
        issue_id=_BUFFER_DROPPING_ISSUE_ID,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=_BUFFER_DROPPING_ISSUE_ID,
    )

def clear_buffer_dropping_issue(hass: HomeAssistant) -> None:
    ir.async_delete_issue(hass, DOMAIN, _BUFFER_DROPPING_ISSUE_ID)
```

**New issue IDs and severity** (D-02-b, D-10, D-11):
```python
_STATES_WORKER_STALLED_ID = "states_worker_stalled"   # IssueSeverity.ERROR
_META_WORKER_STALLED_ID = "meta_worker_stalled"        # IssueSeverity.ERROR
_DB_UNREACHABLE_ID = "db_unreachable"                  # IssueSeverity.ERROR
_RECORDER_DISABLED_ID = "recorder_disabled"            # IssueSeverity.WARNING
```

Four new create/clear pairs following the identical `buffer_dropping` shape. Each function takes only `hass: HomeAssistant` as argument — this is the required shape for `hass.add_job(helper, hass)` (RESEARCH.md Pitfall 2).

---

### `notifications.py` (NEW utility, request-response)

**Analog:** `states_worker._notify_stall` (lines 114-133) + `issues.py` (same `hass.add_job` bridge shape)

**Module structure** — copy the module docstring + `_LOGGER` pattern from `issues.py` (lines 1-11):
```python
"""One-shot user-visible notifications for watchdog recovery and backfill gaps.

D-07: notify_watchdog_recovery fires a structured persistent_notification with
abridged traceback + context dict, plus a full-traceback _LOGGER.error.
D-08: notify_backfill_gap fires a human-readable notification about retention overrun.
"""
import logging
import traceback
from datetime import datetime, timezone

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)
```

**`notify_watchdog_recovery` signature** (D-07-a, D-07-b):
```python
def notify_watchdog_recovery(
    hass: HomeAssistant,
    component: str,
    exc: BaseException | None,
    context: dict | None = None,
) -> None:
```
This is a plain function (not `async def`, not `@callback`). It calls `persistent_notification.async_create` which is a `@callback` — safe to call directly from the event loop (watchdog runs on event loop). Workers bridge via `hass.add_job(notify_watchdog_recovery, hass, component, exc, context)`. Note: `hass.add_job` passes positional args only; all four args after the callable are positional, which works.

**`notify_backfill_gap` signature** (D-07-a, D-08-b):
```python
def notify_backfill_gap(
    hass: HomeAssistant,
    reason: str,
    details: dict | None = None,
) -> None:
```

**Existing stall notification pattern** from `states_worker._notify_stall` (lines 124-133):
```python
from homeassistant.components import persistent_notification
self._hass.add_job(
    persistent_notification.async_create,
    self._hass,
    f"TimescaleDB states worker has failed {attempts} times in a row. ...",
    "TimescaleDB Recorder",
    "timescaledb_recorder_states_stalled",
)
```
`notify_watchdog_recovery` is the generalized version: it does the same `persistent_notification.async_create` call but directly on the event loop (not via `add_job`) and formats a richer body with traceback.

**CRITICAL:** `persistent_notification.async_create` signature is `(hass, message, title=None, notification_id=None)` — all positional or keyword. The notification_id for watchdog recovery is `f"timescaledb_recorder.watchdog_{component}"` per D-07-b; for backfill gap it is `"timescaledb_recorder.backfill_gap"` per D-08-b.

---

### `watchdog.py` (NEW service, event-driven polling)

**Analog:** `__init__._overflow_watcher` (lines 163-186) — exact same asyncio polling loop shape.

**`_overflow_watcher` pattern** (lines 163-186):
```python
async def _overflow_watcher(
    hass: HomeAssistant,
    live_queue: OverflowQueue,
    stop_event: asyncio.Event,
) -> None:
    last_seen = False
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=_OVERFLOW_WATCH_INTERVAL_S,
            )
            return
        except asyncio.TimeoutError:
            pass
        # ... body ...
```

**`watchdog_loop` follows the same shape** with `runtime.loop_stop_event` as the asyncio stop event and `WATCHDOG_INTERVAL_S` as the timeout (D-05-a, D-05-c):
```python
async def watchdog_loop(hass: HomeAssistant, runtime: TimescaledbRecorderData) -> None:
    """Async task: detect dead worker threads and restart them. D-05."""
    while not runtime.loop_stop_event.is_set():
        try:
            await asyncio.wait_for(
                runtime.loop_stop_event.wait(), timeout=WATCHDOG_INTERVAL_S,
            )
            return  # loop_stop_event set — normal shutdown
        except asyncio.TimeoutError:
            pass  # normal cadence tick

        for attr, component in [
            ("states_worker", "states_worker"),
            ("meta_worker", "meta_worker"),
        ]:
            thread = getattr(runtime, attr)
            if not thread.is_alive() and not runtime.stop_event.is_set():
                # ... extract exc/ctx, call notify_watchdog_recovery, respawn
```

**Thread respawn** — two module-level factory helpers (D-05-c, RESEARCH.md Open Question 2):
```python
def spawn_states_worker(hass: HomeAssistant, runtime: TimescaledbRecorderData) -> TimescaledbStateRecorderThread:
    """Construct a new states worker with the same shared queues and stop_event."""

def spawn_meta_worker(hass: HomeAssistant, runtime: TimescaledbRecorderData) -> TimescaledbMetaRecorderThread:
    """Construct a new meta worker with the same shared queues and stop_event."""
```
These replicate the constructor calls from `async_setup_entry` (lines 213-225 of `__init__.py`) but source all args from `runtime`. Avoids circular import: `watchdog.py` imports from `states_worker`, `meta_worker`, and `const`; it does NOT import from `__init__`.

**Imports for `watchdog.py`**:
```python
import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant

from .const import WATCHDOG_INTERVAL_S
from .meta_worker import TimescaledbMetaRecorderThread
from .notifications import notify_watchdog_recovery
from .states_worker import TimescaledbStateRecorderThread

if TYPE_CHECKING:
    from . import TimescaledbRecorderData
```

---

### `strings.json` (config)

**Analog:** `custom_components/timescaledb_recorder/strings.json` (self — additive extension)

**Existing `"issues"` entry pattern** (lines 37-43):
```json
"issues": {
    "buffer_dropping": {
      "title": "TimescaleDB recorder buffer overflow",
      "description": "The TimescaleDB recorder in-memory buffer filled up and is dropping state events. ..."
    }
}
```

**Four new entries to add** (D-10) — each with `"title"` and `"description"`:
- `"states_worker_stalled"` — `IssueSeverity.ERROR`
- `"meta_worker_stalled"` — `IssueSeverity.ERROR`
- `"db_unreachable"` — `IssueSeverity.ERROR`
- `"recorder_disabled"` — `IssueSeverity.WARNING`

Translation key in `async_create_issue` MUST match the JSON key exactly (RESEARCH.md verified).

---

### `const.py` (config)

**Analog:** `custom_components/timescaledb_recorder/const.py` (self — additive extension)

**Existing constants pattern** (lines 17-21):
```python
BATCH_FLUSH_SIZE: int = 200
INSERT_CHUNK_SIZE: int = 200
FLUSH_INTERVAL: float = 5.0
LIVE_QUEUE_MAXSIZE: int = 10000
BACKFILL_QUEUE_MAXSIZE: int = 2
```

**New constants to add** (D-03-d, D-05-c, D-11):
```python
# D-03-d: stall threshold — after this many consecutive failures,
# notify_stall fires once and the worker_stalled repair issue is raised.
STALL_THRESHOLD: int = 5

# D-05-c: watchdog polling cadence (seconds).
WATCHDOG_INTERVAL_S: float = 10.0

# D-11: db_unreachable issue fires when cumulative fail duration exceeds this.
DB_UNREACHABLE_THRESHOLD_SECONDS: float = 300.0
```

Import `STALL_THRESHOLD` into `retry.py` to replace the local `_STALL_NOTIFY_THRESHOLD = 5` (D-03-d).

---

### `__init__.py` (provider, request-response)

**Analog:** `custom_components/timescaledb_recorder/__init__.py` (self — additive extension)

**Existing `TimescaledbRecorderData` dataclass** (lines 57-72) — add `watchdog_task` field:
```python
@dataclass
class TimescaledbRecorderData:
    # ... existing fields ...
    orchestrator_task: asyncio.Task | None
    overflow_watcher_task: asyncio.Task | None
    watchdog_task: asyncio.Task | None   # NEW D-05-a
```

**Existing task-spawn pattern** (lines 264-266) — `_overflow_watcher` spawn is the model for watchdog spawn:
```python
overflow_watcher_task = hass.async_create_task(
    _overflow_watcher(hass, live_queue, loop_stop_event),
)
```
Watchdog spawn is identical in shape:
```python
from .watchdog import watchdog_loop
watchdog_task = hass.async_create_task(
    watchdog_loop(hass, data),
)
```
Spawn AFTER `data = TimescaledbRecorderData(...)` is constructed (since `watchdog_loop` takes `runtime: TimescaledbRecorderData`).

**Orchestrator `add_done_callback`** — in `_on_ha_started` callback (lines 272-284), after `hass.async_create_task(...)`:
```python
@callback
def _on_ha_started(_event):
    task = hass.async_create_task(backfill_orchestrator(...))
    cb = _make_orchestrator_done_callback(hass, data, ...)
    task.add_done_callback(cb)
    orchestrator_holder["task"] = task
    data.orchestrator_task = task
```
`_make_orchestrator_done_callback` is a closure factory (see RESEARCH.md Pattern 3) defined at module level in `__init__.py` or as a nested function.

**`recorder_disabled` check** — add between step 7 (ingester start) and step 8 (orchestrator spawn) in `async_setup_entry` (lines 247-266), after `ingester.async_start()`:
```python
from homeassistant.components import recorder as ha_recorder
from .issues import create_recorder_disabled_issue
try:
    recorder_instance = ha_recorder.get_instance(hass)
    if not recorder_instance.enabled:
        create_recorder_disabled_issue(hass)
except KeyError:
    create_recorder_disabled_issue(hass)
```

**Existing task-cancel pattern in `async_unload_entry`** (lines 337-350) — add watchdog cancellation BEFORE orchestrator cancellation (D-05-e):
```python
# Cancel watchdog first — prevents restart attempts during shutdown.
if data.watchdog_task is not None:
    data.watchdog_task.cancel()
    try:
        await data.watchdog_task
    except asyncio.CancelledError:
        pass

# D-13-b step 2: cancel orchestrator and wait for it.
if data.orchestrator_task is not None:
    ...
```

## Shared Patterns

### Thread-to-event-loop bridge

**Source:** `states_worker._notify_stall` (lines 124-133) + `meta_worker._notify_stall` (lines 128-137)
**Apply to:** All new hook methods in workers (`_stall_hook`, `_recovery_hook`, `_sustained_fail_hook`) and any call site in `watchdog.py` that needs to reach a `@callback`-decorated HA API.

```python
# Pattern: never call async HA APIs directly from a worker thread.
# Use hass.add_job with a wrapper function that takes only `hass` as argument.
self._hass.add_job(create_states_worker_stalled_issue, self._hass)
# NOT: self._hass.add_job(ir.async_create_issue, hass, DOMAIN, ..., is_fixable=False)
# — the kwargs after issue_id are keyword-only in async_create_issue; add_job drops them.
```

### Asyncio polling loop with interruptible sleep

**Source:** `__init__._overflow_watcher` (lines 163-186)
**Apply to:** `watchdog.py` `watchdog_loop`

```python
while not stop_event.is_set():
    try:
        await asyncio.wait_for(
            stop_event.wait(), timeout=INTERVAL_S,
        )
        return  # stop_event set — normal shutdown
    except asyncio.TimeoutError:
        pass  # normal cadence tick
    # ... polling body ...
```

### Hook best-effort guard

**Source:** `retry.py` (lines 68-73)
**Apply to:** All new hooks in `retry.py` (`on_recovery`, `on_sustained_fail`), all hook methods in workers.

```python
try:
    hook()
except Exception:  # noqa: BLE001
    _LOGGER.exception("hook raised; continuing")
```

### Issue create/clear pair

**Source:** `issues.py` (lines 18-40)
**Apply to:** All four new create/clear pairs in `issues.py`.

```python
_ISSUE_ID = "some_issue_id"

def create_some_issue(hass: HomeAssistant) -> None:
    """One line docstring. Idempotent (HA dedupes by id)."""
    ir.async_create_issue(
        hass,
        domain=DOMAIN,
        issue_id=_ISSUE_ID,
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,  # or WARNING
        translation_key=_ISSUE_ID,
    )

def clear_some_issue(hass: HomeAssistant) -> None:
    """One line docstring. Idempotent — no-op if absent."""
    ir.async_delete_issue(hass, DOMAIN, _ISSUE_ID)
```

**CRITICAL:** `is_fixable` and `severity` are required keyword arguments with no defaults — omitting either raises `TypeError` at runtime (RESEARCH.md Pattern 4 verified note).

### asyncio Task cancel-and-await

**Source:** `__init__.async_unload_entry` (lines 337-350)
**Apply to:** Watchdog task cancellation in `async_unload_entry`.

```python
if data.some_task is not None:
    data.some_task.cancel()
    try:
        await data.some_task
    except asyncio.CancelledError:
        pass
```

## No Analog Found

All files have close analogs in the existing codebase. No file requires falling back to RESEARCH.md patterns exclusively.

## Metadata

**Analog search scope:** `custom_components/timescaledb_recorder/`
**Files scanned:** 10 (all production modules)
**Pattern extraction date:** 2026-04-22
