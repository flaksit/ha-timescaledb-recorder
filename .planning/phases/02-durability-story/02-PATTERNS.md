# Phase 2: Durability Story - Pattern Map

**Mapped:** 2026-04-21
**Files analyzed:** 13 (7 new, 6 modified)
**Analogs found:** 13 / 13

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|---|---|---|---|---|
| `custom_components/ha_timescaledb_recorder/overflow_queue.py` | utility | transform (queue wrapper) | `worker.py` (queue usage site) | role-match — subclass of `queue.Queue`, pure stdlib |
| `custom_components/ha_timescaledb_recorder/persistent_queue.py` | utility | file-I/O + event-driven | none in repo — synthesis of `queue.Queue` + jsonl append | no analog (see "No Analog Found") |
| `custom_components/ha_timescaledb_recorder/retry.py` | utility | transform (decorator) | none in repo — synthesis | no analog (see "No Analog Found") |
| `custom_components/ha_timescaledb_recorder/issues.py` | utility | request-response | `worker.py` `hass.add_job` bridge site | role-match — thin wrapper around HA helper |
| `custom_components/ha_timescaledb_recorder/backfill.py` | service | event-driven + batch | `worker.py` `DbWorker` (orchestrator lifecycle) | role-match — event-loop coroutine orchestrator |
| `custom_components/ha_timescaledb_recorder/states_worker.py` (or `worker.py` refactor — planner decides name) | service | event-driven + batch | `worker.py` `DbWorker` (self) | exact — split of existing class |
| `custom_components/ha_timescaledb_recorder/meta_worker.py` (or `worker.py` refactor) | service | event-driven + CRUD | `worker.py` `DbWorker._process_meta_command` | exact — existing SCD2 dispatch moves here |
| `custom_components/ha_timescaledb_recorder/const.py` | config | transform | `const.py` (self) | exact — append constants + modify INSERT_SQL |
| `custom_components/ha_timescaledb_recorder/schema.py` | utility | CRUD (DDL) | `schema.py` (self, `sync_setup_schema`) | exact — one-line addition |
| `custom_components/ha_timescaledb_recorder/ingester.py` | event-relay | event-driven | `ingester.py` (self) | exact — target queue type swap only |
| `custom_components/ha_timescaledb_recorder/syncer.py` | event-relay | event-driven | `syncer.py` (self) | exact — target queue type + JSON serialization |
| `custom_components/ha_timescaledb_recorder/__init__.py` | config | request-response | `__init__.py` (self) | exact — startup ordering D-12 + shutdown D-13 |
| `custom_components/ha_timescaledb_recorder/strings.json` | config | transform | `strings.json` (self) | exact — add `"issues"` section |

## Pattern Assignments

### `overflow_queue.py` (new utility, queue wrapper)

**Analog:** `worker.py` — queue instantiation site (lines 101-103) and producer site convention across `ingester.py` / `syncer.py`.

**Imports pattern** — minimal stdlib, no HA deps (this is a pure utility):

```python
# Adapted from worker.py lines 1-4 (import skeleton, stdlib-only portion)
import logging
import queue
import threading

_LOGGER = logging.getLogger(__name__)
```

**Class skeleton** — subclass `queue.Queue`, add overflow flag + mutex-protected operations:

```python
# Pattern synthesis — queue.Queue subclassing is stdlib documented.
# Purpose: non-blocking put_nowait that never raises queue.Full from an @callback context.
# D-02-a / D-02-b: drop-newest on overflow; overflowed flag observed by worker thread.
class OverflowQueue(queue.Queue):
    def __init__(self, maxsize: int) -> None:
        super().__init__(maxsize=maxsize)
        self.overflowed: bool = False
        # Mutex guards the (put attempt → set overflowed flag → drop) sequence
        # and the clear_and_reset_overflow() drain. queue.Queue has its own internal
        # mutex for .put/.get/.qsize — we add a separate one for our flag transitions
        # to avoid holding the internal mutex across our bookkeeping.
        self._overflow_lock = threading.Lock()
        # Monotonic counter for D-11-c recovery log ("dropped N events during outage")
        self._dropped: int = 0
```

**Overflow enqueue pattern** (D-02-b, D-11-a):

```python
def put_nowait(self, item) -> None:
    """Non-blocking enqueue. On full: set overflowed flag, drop silently.

    Never raises queue.Full — @callback producers (ingester._handle_state_changed)
    cannot tolerate exceptions.
    """
    try:
        # block=False here duplicates put_nowait semantics — we override the base class.
        super().put(item, block=False)
    except queue.Full:
        with self._overflow_lock:
            if not self.overflowed:
                # D-11-a: first drop per outage — single warning, then counter-only
                _LOGGER.warning(
                    "live_queue full (%d) — dropping events until DB recovery",
                    self.maxsize,
                )
            self.overflowed = True
            self._dropped += 1
```

**Drain-and-reset pattern** (D-02-d, D-11-c) — called by orchestrator at backfill start:

```python
def clear_and_reset_overflow(self) -> int:
    """Drain internal deque under mutex, reset flag, return drop counter snapshot.

    Caller (backfill orchestrator) logs the returned count per D-11-c.
    """
    with self._overflow_lock:
        # Drain: pop until empty. queue.Queue.queue is the internal collections.deque.
        # Direct .clear() is atomic under GIL but we hold _overflow_lock to pair
        # with put_nowait's flag update — no enqueue can race between clear and reset.
        with self.mutex:  # queue.Queue's internal mutex
            self.queue.clear()
            self.unfinished_tasks = 0
            self.not_full.notify_all()
        dropped = self._dropped
        self._dropped = 0
        self.overflowed = False
        return dropped
```

**Usage site** (consumed from worker thread — `states_worker.run`):

```python
# Read flag without lock is safe — Python attribute read is atomic under GIL
# and we only need eventually-consistent observation for mode transition decision (D-04-c).
if self._live_queue.overflowed:
    # trigger backfill via call_soon_threadsafe(backfill_request.set)
    ...
```

---

### `persistent_queue.py` (new utility, file-I/O + event-driven)

**Analog:** No direct analog; synthesis of `queue.Queue` blocking semantics from `worker.py` lines 142-143 + jsonl append pattern. Closest conceptual analog: HA recorder's own queue persistence (not in this repo).

**Imports pattern:**

```python
import json
import logging
import os
import tempfile
import threading
from pathlib import Path

_LOGGER = logging.getLogger(__name__)
```

**Class skeleton** (D-03-a):

```python
class PersistentQueue:
    """File-backed FIFO, JSON-lines format, single lock.

    D-03-h: crash safety — on worker crash between DB write and task_done,
    item replays on next startup. SCD2 write handler is idempotent (change-detection
    no-ops on unchanged data), so replay is safe.

    Thread model:
    - Producers (syncer @callbacks on event loop) call put_async() → put() via executor
    - Consumer (meta_worker thread) calls get() → task_done() after DB success
    - Shutdown (event loop) calls wake_consumer() to unblock get()
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # Condition variable for get() to block on when file is empty.
        # Reuses _lock so notify_all pairs with the append path.
        self._cond = threading.Condition(self._lock)
        self._in_flight: object | None = None  # item returned by get() but not yet task_done'd
        # _SENTINEL distinguishes "no in-flight" from "in-flight is None/falsy"
        self._SENTINEL = object()
        self._in_flight = self._SENTINEL
```

**Producer patterns** (D-03-b, D-03-c):

```python
def put(self, item: dict) -> None:
    """Synchronous producer: append line + fsync. Blocks on _lock (microseconds)."""
    line = json.dumps(item, default=str) + "\n"
    with self._cond:
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())  # D-03-b: fsync for crash durability
        self._cond.notify_all()   # wake consumer blocked in get()

async def put_async(self, item: dict) -> None:
    """Async producer for event-loop callers (syncer).

    D-03-c: offload blocking file I/O to default executor so event loop does not stall.
    NOTE: this is the event-loop-side entry; the underlying put() acquires the mutex
    in an executor thread, not the event loop thread.
    """
    import asyncio
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, self.put, item)
```

**Consumer patterns** (D-03-d, D-03-e, D-03-f, D-03-g):

```python
def get(self) -> dict | None:
    """Block until item available; return front line without removing from disk.

    Returns None only if woken by wake_consumer() during shutdown — caller
    (meta_worker) must check stop_event.is_set() and exit the loop.
    """
    with self._cond:
        while True:
            if self._in_flight is not self._SENTINEL:
                return self._in_flight
            # Read first line without consuming
            first = self._read_first_line()
            if first is not None:
                self._in_flight = json.loads(first)
                return self._in_flight
            # File empty — block on condition. wake_consumer() notifies during shutdown.
            self._cond.wait()
            # Loop: check stop_event happens in caller; we just return on spurious wake too
            if self._in_flight is self._SENTINEL:
                # Spurious wake with no item — let caller re-check shutdown flag
                return None

def task_done(self) -> None:
    """Atomically rewrite file without the front line. Call only after DB success.

    D-03-e: tempfile + os.replace is atomic on POSIX; partial write never visible.
    """
    with self._cond:
        # Read all lines except first, write to tempfile, atomic rename
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with open(self._path, "r", encoding="utf-8") as src, \
             open(tmp, "w", encoding="utf-8") as dst:
            src.readline()  # skip first
            for remaining in src:
                dst.write(remaining)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(tmp, self._path)  # atomic
        self._in_flight = self._SENTINEL

async def join(self) -> None:
    """Block until queue empty and no in-flight item (startup drain — D-03-f).

    Runs the blocking _join body in the default executor so event loop is not stalled.
    """
    import asyncio
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, self._join_blocking)

def _join_blocking(self) -> None:
    with self._cond:
        while self._in_flight is not self._SENTINEL or self._read_first_line() is not None:
            self._cond.wait(timeout=0.5)  # re-check periodically (consumer is making progress)

def wake_consumer(self) -> None:
    """Notify all waiters on _cond. Used during shutdown to unblock blocked get().

    D-03-g: paired with stop_event.is_set() check in consumer loop — after wake_consumer,
    consumer loops back to top, sees stop_event set, exits cleanly.
    """
    with self._cond:
        self._cond.notify_all()
```

**Helper** — read first line without consuming (internal):

```python
def _read_first_line(self) -> str | None:
    if not self._path.exists():
        return None
    with open(self._path, "r", encoding="utf-8") as f:
        line = f.readline()
    return line if line else None
```

**Usage site** — construction in `__init__.py` async_setup_entry per D-12 step 1:

```python
# In async_setup_entry (event loop) — D-12 step 1
from .persistent_queue import PersistentQueue
meta_queue = PersistentQueue(hass.config.path(DOMAIN, "metadata_queue.jsonl"))
```

---

### `retry.py` (new utility, decorator)

**Analog:** No direct analog; synthesis. The `stop_event.wait(backoff)` pattern comes from `worker.py` shutdown model (lines 141 / 386).

**Imports pattern:**

```python
import functools
import logging
import threading
from typing import Callable

_LOGGER = logging.getLogger(__name__)
```

**Decorator pattern** (D-07, all sub-decisions):

```python
# D-07-b: backoff schedule — capped at 60s, repeats forever until success or shutdown
_BACKOFF_SCHEDULE: tuple[int, ...] = (1, 5, 10, 30, 60)
# D-07-f: threshold for stall notification (Claude's Discretion — suggested 5)
_STALL_NOTIFY_THRESHOLD: int = 5


def retry_until_success(
    *,
    on_transient: Callable[[], None] | None = None,
    stop_event: threading.Event,
    notify_stall: Callable[[int], None] | None = None,
) -> Callable:
    """Decorator: retry wrapped function forever with capped exponential backoff.

    D-07-a: wraps _insert_chunk (states) and write_item (metadata).
    D-07-c: catches bare Exception — no error-class branching (YAGNI).
    D-07-d: persistent failure halts worker forever — recovery is HA restart.
    D-07-e: stop_event.wait(backoff) returns True on shutdown — decorator exits early.
    D-07-f: after _STALL_NOTIFY_THRESHOLD consecutive failures, notify_stall fires
            a persistent_notification via hass.add_job.
    D-07-g: on_transient hook (reset_db_connection) called after each failure so
            get_db_connection lazily reconnects on next call.

    Returns the wrapped function's return value on success. On shutdown, returns None.
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            attempts = 0
            notified = False
            while True:
                try:
                    result = fn(*args, **kwargs)
                    # D-07-f tail: reset notification state on first success
                    if notified and notify_stall is not None:
                        # Signal "recovered" by passing 0 — caller (issues.py-like)
                        # decides to dismiss notification on 0.
                        # (Planner: may prefer separate notify_recovered callback.)
                        pass
                    return result
                except Exception as exc:  # noqa: BLE001 — D-07-c: broad catch by design
                    attempts += 1
                    if on_transient is not None:
                        try:
                            on_transient()
                        except Exception:  # noqa: BLE001
                            _LOGGER.exception("on_transient hook failed; continuing")
                    if not notified and attempts >= _STALL_NOTIFY_THRESHOLD:
                        if notify_stall is not None:
                            try:
                                notify_stall(attempts)
                            except Exception:  # noqa: BLE001
                                _LOGGER.exception("notify_stall failed; continuing")
                        notified = True
                    backoff = _BACKOFF_SCHEDULE[min(attempts - 1, len(_BACKOFF_SCHEDULE) - 1)]
                    _LOGGER.warning(
                        "%s failed (attempt %d); retrying in %ds: %s",
                        fn.__qualname__, attempts, backoff, exc,
                    )
                    # D-07-e: interruptible backoff — stop_event.wait returns True on set
                    if stop_event.wait(backoff):
                        _LOGGER.info("%s retry interrupted by shutdown", fn.__qualname__)
                        return None
        return wrapper
    return decorator
```

**Usage pattern** — applied by worker classes:

```python
# In states worker (D-06-b):
@retry_until_success(
    on_transient=self.reset_db_connection,
    stop_event=self._stop_event,
    notify_stall=self._notify_stall,  # hass.add_job(create_persistent_notification)
)
def _insert_chunk(self, chunk: list[StateRow]) -> None:
    ...
```

**NOTE for planner:** decorator applied to methods; `on_transient` / `notify_stall` are bound methods of the worker. Because decoration happens at class-body time but the hooks are instance methods, the planner must either decorate at `__init__` time (`self._insert_chunk = retry_until_success(...)(self._insert_chunk.__func__.__get__(self))`) or accept passing `self` explicitly. Suggested approach: module-level decorator factory invoked inside `__init__` on bound methods.

---

### `issues.py` (new utility, HA bridge)

**Analog:** `worker.py` lines 82-85 (comment documenting `hass.add_job` bridge) + `__init__.py` import of HA helpers (lines 7-10).

**Imports pattern:**

```python
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
```

**Create/clear wrappers** (D-10-a):

```python
# Issue key is stable (never changes across HA versions); strings.json carries UI text.
_BUFFER_DROPPING_ISSUE_ID = "buffer_dropping"


def create_buffer_dropping_issue(hass: HomeAssistant) -> None:
    """Register the buffer_dropping repair issue. Idempotent — HA dedupes by issue_id.

    Safe to call from the event loop. For calls originating on the worker thread,
    wrap in hass.add_job(create_buffer_dropping_issue, hass) per D-10-b.
    """
    ir.async_create_issue(
        hass,
        domain=DOMAIN,
        issue_id=_BUFFER_DROPPING_ISSUE_ID,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=_BUFFER_DROPPING_ISSUE_ID,
    )


def clear_buffer_dropping_issue(hass: HomeAssistant) -> None:
    """Delete the buffer_dropping repair issue. Idempotent — no-op if absent."""
    ir.async_delete_issue(hass, DOMAIN, _BUFFER_DROPPING_ISSUE_ID)
```

**Thread-safety boundary** (D-10-b vs D-10-c):

```python
# D-10-b: ingester's @callback is on event loop — call directly or via hass.add_job.
#   Simpler: call directly (we're already on the loop).
#   But ingester doesn't know if it's being called from @callback or worker;
#   D-10-b says "first overflowed flip" — this flip is observed by the WORKER
#   thread in the states_worker loop. So worker uses hass.add_job:
#       self._hass.add_job(create_buffer_dropping_issue, self._hass)
#
# D-10-c: orchestrator is on event loop — call directly:
#       clear_buffer_dropping_issue(self._hass)
```

---

### `backfill.py` (new service, event-loop orchestrator)

**Analog:** `worker.py` `DbWorker.run()` lifecycle (lines 120-190) for the "loop until shutdown" shape; `syncer.py` `async_start` (lines 122-189) for event-loop coroutine patterns.

**Imports pattern:**

```python
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from homeassistant.components import recorder
from homeassistant.components.recorder import history as recorder_history
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import SELECT_OPEN_ENTITIES_SQL, SELECT_WATERMARK_SQL  # new consts

_LOGGER = logging.getLogger(__name__)

# Sentinel pushed onto backfill_queue after the last slice of a backfill cycle (D-04-d).
BACKFILL_DONE = object()
```

**Orchestrator coroutine skeleton** (D-08-a .. D-08-g):

```python
async def backfill_orchestrator(
    hass: HomeAssistant,
    *,
    live_queue,                  # OverflowQueue
    backfill_queue,              # queue.Queue(maxsize=2)
    backfill_request: asyncio.Event,
    read_watermark,              # sync callable: () -> datetime | None (runs in worker)
    open_entities_reader,        # sync callable: () -> set[str] (runs in worker)
    entity_filter,
    stop_event: asyncio.Event,   # event-loop-side mirror of worker stop_event
) -> None:
    """Long-running event-loop task driving HA sqlite backfill on demand.

    D-08-a: spawned via hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, ...)
            from async_setup_entry step 8.
    D-08-c: awakens on backfill_request.set(); processes one cycle; loops.
    D-08-g: cascade prevention via backfill_queue(maxsize=2) blocking put.
    """
    recorder_instance = recorder.get_instance(hass)
    while not stop_event.is_set():
        await backfill_request.wait()
        backfill_request.clear()
        if stop_event.is_set():
            return

        t_clear = datetime.now(timezone.utc)
        dropped = live_queue.clear_and_reset_overflow()
        if dropped > 0:
            _LOGGER.warning(
                "overflow cleared, dropped %d events during outage", dropped,
            )
        # D-10-c: clear repair issue now that live_queue is accepting events again
        from .issues import clear_buffer_dropping_issue
        clear_buffer_dropping_issue(hass)

        # D-08-d step 4: read watermark via states worker's connection in its pool/thread.
        # Dispatched to HA default executor because the states worker itself does not
        # expose a runloop — worker's connection is OK for a one-shot read while worker
        # is idle between get() calls (planner must confirm safe coordination).
        wm = await hass.async_add_executor_job(read_watermark)
        if wm is None:
            _LOGGER.info(
                "ha_states is empty — first-install backfill is out of scope; "
                "use paradise-ha-tsdb/scripts/backfill/backfill.py for bulk import",
            )
            await hass.async_add_executor_job(backfill_queue.put, BACKFILL_DONE)
            continue

        from_ = wm - timedelta(minutes=10)   # D-08-d step 6: late-arrival grace
        cutoff = t_clear

        # Entity set = live registry (filtered) ∪ open rows in dim_entities (D-08-f)
        entity_reg = er.async_get(hass)
        live_entities = {
            e.entity_id for e in entity_reg.entities.values()
            if entity_filter(e.entity_id)
        }
        open_entities = await hass.async_add_executor_job(open_entities_reader)
        entities = live_entities | open_entities

        slice_start = from_
        while slice_start < cutoff and not stop_event.is_set():
            slice_end = min(slice_start + timedelta(minutes=5), cutoff)
            # D-08-d step 7: wait until slice_end + 5s wall-clock before reading sqlite
            # (HA recorder commit lag buffer)
            delay = (slice_end + timedelta(seconds=5) - datetime.now(timezone.utc)).total_seconds()
            if delay > 0:
                await asyncio.sleep(delay)

            raw = await recorder_instance.async_add_executor_job(
                _fetch_slice_raw, hass, entities, slice_start, slice_end,
            )
            # D-08-g: blocking put = natural backpressure at maxsize=2
            await hass.async_add_executor_job(backfill_queue.put, raw)
            slice_start = slice_end

        # D-08-d step 8: signal end of cycle
        await hass.async_add_executor_job(backfill_queue.put, BACKFILL_DONE)
```

**Per-entity slice fetch** (D-08-e) — runs in HA recorder pool:

```python
def _fetch_slice_raw(
    hass: HomeAssistant,
    entities: set[str],
    t_start: datetime,
    t_end: datetime,
) -> dict:
    """Iterate per entity_id; MUST NOT pass entity_id=None (research: raises ValueError).

    Returns dict[entity_id -> list[HA State]]. Includes empty lists so the worker
    can distinguish "entity exists, no changes" from "entity never queried".
    """
    out: dict[str, list] = {}
    for eid in entities:
        # include_start_time_state=False: avoid re-ingesting boundary rows
        # (research: otherwise duplicates the row at t_start on every slice).
        states = recorder_history.state_changes_during_period(
            hass, t_start, t_end,
            entity_id=eid,
            include_start_time_state=False,
        )
        out[eid] = states.get(eid, [])
    return out
```

**Spawn pattern** (wired in `__init__.py` D-12 step 8):

```python
# In async_setup_entry:
backfill_request = asyncio.Event()
stop_event_loop = asyncio.Event()

async def _start_orchestrator(_event):
    hass.async_create_task(backfill_orchestrator(
        hass,
        live_queue=live_queue,
        backfill_queue=backfill_queue,
        backfill_request=backfill_request,
        read_watermark=states_worker.read_watermark,
        open_entities_reader=states_worker.read_open_entities,
        entity_filter=entity_filter,
        stop_event=stop_event_loop,
    ))

hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _start_orchestrator)
```

---

### `states_worker.py` (new, or `worker.py` split) — service, event-driven + batch

**Analog:** `worker.py` `DbWorker` class (self, lines 74-391). Entire class is rewritten with three-mode state machine (D-04) but the shape is preserved.

**Imports pattern** — extends existing `worker.py` imports (lines 1-26) with:

```python
import time  # for MODE_LIVE adaptive timeout math
# asyncio.Event is accessed via hass.loop.call_soon_threadsafe for cross-thread set
# D-08-c: hass.loop.call_soon_threadsafe(backfill_request.set)
from .backfill import BACKFILL_DONE
from .retry import retry_until_success
```

**State machine constants** (D-04-b):

```python
MODE_INIT = "init"
MODE_BACKFILL = "backfill"
MODE_LIVE = "live"

# D-04-e / D-06: batch and flush tunables — move from const.py (see const.py section)
```

**Class skeleton** — adapted from `worker.py` `DbWorker.__init__` (lines 88-109):

```python
class TimescaledbStateRecorderThread(threading.Thread):
    """Dedicated OS thread owning the states psycopg3 connection.

    D-04-a: daemon thread; inherits the shape of Phase 1 DbWorker.
    D-15-a: one thread per data type — this thread handles only StateRow.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        dsn: str,
        live_queue,              # OverflowQueue
        backfill_queue,          # queue.Queue(maxsize=2)
        backfill_request,        # asyncio.Event owned by event loop
        stop_event: threading.Event,
    ) -> None:
        super().__init__(daemon=True, name="ha_timescaledb_states_worker")
        self._hass = hass
        self._dsn = dsn
        self._live_queue = live_queue
        self._backfill_queue = backfill_queue
        self._backfill_request = backfill_request
        self._stop_event = stop_event
        self._conn: psycopg.Connection | None = None
        # D-07: retry decorator applied to _insert_chunk; applied at __init__ on
        # bound method so on_transient/notify_stall can reference self.
        self._insert_chunk = retry_until_success(
            on_transient=self.reset_db_connection,
            stop_event=stop_event,
            notify_stall=self._notify_stall,
        )(self._insert_chunk_raw)
```

**Main loop** — three-mode state machine replacing `worker.py` lines 140-190:

```python
def run(self) -> None:
    mode = MODE_INIT
    buffer: list[StateRow] = []
    last_flush = time.monotonic()

    while not self._stop_event.is_set():
        # D-04-c: transition to MODE_BACKFILL on init or overflow detected
        if mode == MODE_INIT or (mode == MODE_LIVE and self._live_queue.overflowed):
            if buffer:
                self._flush(buffer)
                buffer.clear()
            # D-04-c: cross-thread wake — call_soon_threadsafe is the correct bridge.
            # self._backfill_request.set() from this thread is NOT safe
            # (asyncio.Event is not thread-safe per HA docs).
            self._hass.loop.call_soon_threadsafe(self._backfill_request.set)
            mode = MODE_BACKFILL
            last_flush = time.monotonic()
            continue

        if mode == MODE_BACKFILL:
            try:
                item = self._backfill_queue.get(timeout=5.0)
            except queue.Empty:
                continue  # D-04-f: timeout lets stop_event check happen
            if item is BACKFILL_DONE:
                if buffer:
                    self._flush(buffer)
                    buffer.clear()
                mode = MODE_LIVE
                last_flush = time.monotonic()
                continue
            # item is dict[entity_id, list[HA State]] — D-04-d
            rows = self._slice_to_rows(item)
            buffer.extend(rows)
            self._flush(buffer)
            buffer.clear()
            continue

        # MODE_LIVE — adaptive timeout flushing
        now = time.monotonic()
        remaining = max(0.1, FLUSH_INTERVAL - (now - last_flush))
        try:
            item = self._live_queue.get(timeout=remaining)
        except queue.Empty:
            if buffer:
                self._flush(buffer)
                buffer.clear()
            last_flush = time.monotonic()
            continue
        if isinstance(item, StateRow):
            buffer.append(item)
        if (len(buffer) >= BATCH_FLUSH_SIZE
                or time.monotonic() - last_flush >= FLUSH_INTERVAL):
            self._flush(buffer)
            buffer.clear()
            last_flush = time.monotonic()

    # Shutdown: final flush
    if buffer and self._conn is not None:
        self._flush(buffer)
    if self._conn is not None:
        self._conn.close()
```

**Flush + chunk pattern** (D-06):

```python
def _flush(self, rows: list[StateRow]) -> None:
    """Chunk and insert. D-06-a: per-chunk retry granularity."""
    for i in range(0, len(rows), INSERT_CHUNK_SIZE):
        self._insert_chunk(rows[i:i + INSERT_CHUNK_SIZE])

def _insert_chunk_raw(self, chunk: list[StateRow]) -> None:
    """Raw insert — wrapped by retry_until_success in __init__.

    Preserves worker.py Jsonb() wrapping pattern (lines 212-221).
    INSERT_SQL now includes ON CONFLICT (last_updated, entity_id) DO NOTHING
    per D-06-d + D-09-a.
    """
    params = [
        (r.entity_id, r.state, Jsonb(r.attributes), r.last_updated, r.last_changed)
        for r in chunk
    ]
    conn = self.get_db_connection()
    with conn.cursor() as cur:
        cur.executemany(INSERT_SQL, params)

def reset_db_connection(self) -> None:
    """D-07-g: drop connection handle so get_db_connection reconnects lazily."""
    if self._conn is not None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass
    self._conn = None

def get_db_connection(self) -> psycopg.Connection:
    if self._conn is None:
        self._conn = psycopg.connect(self._dsn, autocommit=True)
    return self._conn
```

**Slice-to-rows transform** (D-04-d) — CPU work done in worker thread:

```python
def _slice_to_rows(self, slice_dict: dict) -> list[StateRow]:
    """Merge + sort across entities by last_updated; convert HA State → StateRow."""
    all_states = []
    for eid, states in slice_dict.items():
        all_states.extend(states)
    all_states.sort(key=lambda s: s.last_updated)
    return [StateRow.from_ha_state(s) for s in all_states]
```

**Watermark + open-entities readers** — exposed for orchestrator (D-08-d, D-08-f):

```python
def read_watermark(self) -> datetime | None:
    """Return MAX(last_updated) from ha_states, or None if empty hypertable."""
    conn = self.get_db_connection()
    with conn.cursor() as cur:
        cur.execute(SELECT_WATERMARK_SQL)
        row = cur.fetchone()
    return row[0] if row and row[0] is not None else None

def read_open_entities(self) -> set[str]:
    """Return entity_ids with open (valid_to IS NULL) rows in dim_entities."""
    conn = self.get_db_connection()
    with conn.cursor() as cur:
        cur.execute(SELECT_OPEN_ENTITIES_SQL)
        return {row[0] for row in cur.fetchall()}
```

---

### `meta_worker.py` (new, or `worker.py` split) — service, event-driven + CRUD

**Analog:** `worker.py` `DbWorker._process_meta_command` + four `_process_*_command` methods (lines 236-369). These move wholesale into the new meta worker; only the queue source changes.

**Class skeleton:**

```python
class TimescaledbMetaRecorderThread(threading.Thread):
    """Dedicated OS thread for metadata (SCD2) writes.

    D-05-a: owns its own psycopg3 connection — separate from states worker.
    D-15-b: receives plain dicts from PersistentQueue (JSON-serializable, not MetaCommand).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        dsn: str,
        meta_queue,                  # PersistentQueue
        syncer: "MetadataSyncer",    # for change-detection helpers (reused from Phase 1)
        stop_event: threading.Event,
    ) -> None:
        super().__init__(daemon=True, name="ha_timescaledb_meta_worker")
        self._hass = hass
        self._dsn = dsn
        self._meta_queue = meta_queue
        self._syncer = syncer
        self._stop_event = stop_event
        self._conn: psycopg.Connection | None = None
        self._write_item = retry_until_success(
            on_transient=self.reset_db_connection,
            stop_event=stop_event,
            notify_stall=self._notify_stall,
        )(self._write_item_raw)
```

**Main loop** (D-05-b, D-05-d):

```python
def run(self) -> None:
    while not self._stop_event.is_set():
        item = self._meta_queue.get()  # blocks on Condition
        if item is None or self._stop_event.is_set():
            # D-05-d: wake_consumer() from async_unload_entry unblocks get() returning None
            break
        self._write_item(item)         # retry-wrapped
        self._meta_queue.task_done()   # atomic rewrite on disk (only after DB success)
    if self._conn is not None:
        self._conn.close()
```

**Dispatch pattern** (D-05-c) — reuses Phase 1 SCD2 dispatch, adapted from `worker.py` lines 256-369:

```python
def _write_item_raw(self, item: dict) -> None:
    """Dispatch by item['registry']; reuse Phase 1 SCD2 helpers.

    item schema (Claude's Discretion — planner to finalize):
        {
          "registry": "entity" | "device" | "area" | "label",
          "action":   "create" | "update" | "remove",
          "registry_id": str,
          "old_id": str | None,
          "params":  list | None,    # JSON-serializable; datetimes as ISO strings
          "enqueued_at": "2026-04-21T12:34:56.789+00:00",
        }
    """
    # Rehydrate datetimes from ISO strings before passing to SQL
    params = self._rehydrate_params(item)
    now = datetime.now(timezone.utc)
    registry = item["registry"]
    action = item["action"]
    registry_id = item["registry_id"]
    old_id = item.get("old_id")

    conn = self.get_db_connection()
    if registry == "entity":
        self._process_entity(conn, action, registry_id, old_id, params, now)
    elif registry == "device":
        self._process_device(conn, action, registry_id, params, now)
    # ... area, label — copy structure from worker.py lines 321-369
```

**Entity dispatch** — near-verbatim reuse of `worker.py` `_process_entity_command` (lines 256-294):

```python
def _process_entity(self, conn, action, registry_id, old_id, params, now):
    with conn.cursor() as cur:
        if action == "create":
            # params[0] = entity_id — must appear twice for WHERE NOT EXISTS subquery
            cur.execute(SCD2_SNAPSHOT_ENTITY_SQL, (*params, params[0]))
        elif action == "remove":
            cur.execute(SCD2_CLOSE_ENTITY_SQL, (now, registry_id))
        elif action == "update":
            if old_id is not None:
                with conn.transaction():
                    cur.execute(SCD2_CLOSE_ENTITY_SQL, (now, old_id))
                    cur.execute(SCD2_INSERT_ENTITY_SQL, (*params,))
            else:
                with conn.cursor(row_factory=psycopg.rows.dict_row) as dict_cur:
                    changed = self._syncer._entity_row_changed(dict_cur, registry_id, params)
                if changed:
                    with conn.transaction():
                        cur.execute(SCD2_CLOSE_ENTITY_SQL, (now, registry_id))
                        cur.execute(SCD2_INSERT_ENTITY_SQL, (*params,))
```

Device/area/label dispatches are mechanical copies from `worker.py` lines 296-369 with the registry-id and helper method swapped.

---

### `const.py` (modify — add Phase 2 constants)

**Analog:** `const.py` (self) — lines 63-66 (INSERT_SQL) and lines 58-61 (index DDL style).

**Append constants** (scattered by section):

```python
# Phase 2 ingestion tunables (D-04-e, D-06-a, D-01-a) — replace DEFAULT_BATCH_SIZE
# and DEFAULT_FLUSH_INTERVAL which are now user-configurable options. These are the
# hardcoded internals of the states worker loop.
BATCH_FLUSH_SIZE: int = 200       # D-04-e: flush when buffer reaches this size
INSERT_CHUNK_SIZE: int = 200      # D-06-a: sub-batch size per _insert_chunk call
FLUSH_INTERVAL: float = 5.0       # D-04-e: seconds — adaptive timeout target
LIVE_QUEUE_MAXSIZE: int = 10000   # D-01-a: OverflowQueue cap
BACKFILL_QUEUE_MAXSIZE: int = 2   # D-01-b: backpressure cap for backfill_queue

# D-09-a: unique index — enables ON CONFLICT DO NOTHING dedup (D-06-d).
# partitioning column (last_updated) MUST be included for hypertable unique index.
CREATE_UNIQUE_INDEX_SQL = f"""
CREATE UNIQUE INDEX IF NOT EXISTS idx_{TABLE_NAME}_uniq
    ON {TABLE_NAME} (last_updated, entity_id);
"""

# D-08-d step 4: watermark read.
SELECT_WATERMARK_SQL = f"SELECT MAX(last_updated) FROM {TABLE_NAME}"

# D-08-f: open entities reader.
SELECT_OPEN_ENTITIES_SQL = "SELECT entity_id FROM dim_entities WHERE valid_to IS NULL"
```

**Modify INSERT_SQL** (D-06-d) — append ON CONFLICT clause to the existing string at lines 63-66:

```python
# BEFORE (const.py:63-66):
# INSERT_SQL = f"""
# INSERT INTO {TABLE_NAME} (entity_id, state, attributes, last_updated, last_changed)
# VALUES (%s, %s, %s, %s, %s)
# """

# AFTER:
INSERT_SQL = f"""
INSERT INTO {TABLE_NAME} (entity_id, state, attributes, last_updated, last_changed)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (last_updated, entity_id) DO NOTHING
"""
```

---

### `schema.py` (modify — add unique index)

**Analog:** `schema.py` `sync_setup_schema` (self, lines 29-78). Surgical single-line addition.

**Pattern to extend** (after line 59, after `CREATE_INDEX_SQL`):

```python
# sync_setup_schema body, inside the existing `with conn.cursor() as cur:` block
# (after the current CREATE_INDEX_SQL execute, before the dim_* DDL):
cur.execute(CREATE_INDEX_SQL)
cur.execute(CREATE_UNIQUE_INDEX_SQL)    # D-09-a: added in Phase 2
```

**Import addition** (schema.py line 6-24 import block):

```python
from .const import (
    ...existing imports...
    CREATE_UNIQUE_INDEX_SQL,    # new in Phase 2
)
```

---

### `ingester.py` (modify — target OverflowQueue)

**Analog:** `ingester.py` (self, lines 44-66). The only change is the queue type annotation and the overflow-observation call in a follow-up hook.

**Pattern preserved as-is** (lines 44-66) — `_handle_state_changed` body is unchanged:

```python
@callback
def _handle_state_changed(self, event: Event) -> None:
    """Handle state_changed events — synchronous, never awaits."""
    new_state = event.data.get("new_state")
    if new_state is None:
        return
    entity_id = new_state.entity_id
    if not self._entity_filter(entity_id):
        return
    self._queue.put_nowait(StateRow(  # OverflowQueue.put_nowait never raises (D-02-b)
        entity_id=entity_id,
        state=new_state.state,
        attributes=dict(new_state.attributes),
        last_updated=new_state.last_updated,
        last_changed=new_state.last_changed,
    ))
```

**Constructor signature change** — queue param type tightens from `queue.Queue` to `OverflowQueue`:

```python
from .overflow_queue import OverflowQueue

def __init__(
    self,
    hass: HomeAssistant,
    queue: OverflowQueue,
    entity_filter: EntityFilter,
) -> None:
```

**D-10-b trigger** (first-flip issue creation) — NOTE: CONTEXT states ingester fires the issue on overflowed flip, but the flip is observed by the states worker thread (inside `OverflowQueue.put_nowait`'s except branch). The cleanest wiring puts the `hass.add_job(create_buffer_dropping_issue, hass)` call inside `OverflowQueue.put_nowait`'s first-flip branch — this avoids ingester holding a reference to `issues.py`.

Alternative (if planner prefers separation): inject a `on_first_overflow` callback into OverflowQueue and have `__init__.py` wire it to `issues.create_buffer_dropping_issue` at construction time.

---

### `syncer.py` (modify — target PersistentQueue, JSON-serializable dicts)

**Analog:** `syncer.py` (self, lines 122-189 for async_start snapshot; lines 348-482 for registry `@callback`s).

**Key transformation** (D-15-c): drop `MetaCommand` dataclass; enqueue plain JSON-serializable dicts into `PersistentQueue` via `put_async`.

**Constructor signature change:**

```python
from .persistent_queue import PersistentQueue

def __init__(
    self,
    hass: HomeAssistant,
    meta_queue: PersistentQueue | None = None,
) -> None:
    self._hass = hass
    self._meta_queue = meta_queue
    ...
```

**Snapshot pattern** (adapt `syncer.py` lines 141-167) — enqueue dicts instead of `MetaCommand`:

```python
# In async_start():
for entry in self._entity_reg.entities.values():
    params = self._extract_entity_params(entry, now)
    await self._meta_queue.put_async({
        "registry": "entity",
        "action": "create",
        "registry_id": entry.entity_id,
        "old_id": None,
        "params": _to_json_safe(params),   # datetime → ISO string
        "enqueued_at": now.isoformat(),
    })
```

**Registry @callback pattern** (adapt `syncer.py` lines 348-376) — call `put_async` instead of `put_nowait`. Because `put_async` is a coroutine and `@callback`s are sync, wrap via `hass.async_create_task`:

```python
@callback
def _handle_entity_registry_updated(self, event: Event) -> None:
    """Extract entity registry params and enqueue to PersistentQueue.

    PersistentQueue.put_async is a coroutine (offloads to executor). @callback must
    not await, so we schedule the put via async_create_task. Order is preserved
    because async_create_task queues on the event loop in FIFO order, and
    put_async serializes on the file lock.
    """
    action = event.data["action"]
    entity_id = event.data["entity_id"]
    old_entity_id = event.data.get("old_entity_id")
    if action == "remove":
        params = None
    else:
        entry = self._entity_reg.async_get(entity_id)
        if entry is None:
            _LOGGER.warning("Entity %s not found during %s event; skipping", entity_id, action)
            return
        params = self._extract_entity_params(entry, datetime.now(timezone.utc))

    item = {
        "registry": "entity",
        "action": action,
        "registry_id": entity_id,
        "old_id": old_entity_id,
        "params": _to_json_safe(params) if params is not None else None,
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }
    self._hass.async_create_task(self._meta_queue.put_async(item))
```

**Reused helpers (unchanged)** — `_extract_*_params`, `_entity_row_changed`, etc.: all sync helpers in `syncer.py` lines 195-342 continue to work as-is. The meta worker calls them with a psycopg3 dict_row cursor, exactly the Phase 1 pattern.

**JSON-safe helper** (new):

```python
def _to_json_safe(params: tuple | list) -> list:
    """Convert params tuple to a JSON-serializable list.

    datetime → isoformat string (meta_worker rehydrates via fromisoformat).
    All other types (str, int, float, list[str], None) pass through.
    """
    return [
        v.isoformat() if isinstance(v, datetime) else v
        for v in params
    ]
```

---

### `__init__.py` (modify — D-12 startup + D-13 shutdown)

**Analog:** `__init__.py` (self, lines 53-127). Same skeleton, expanded orchestration.

**HaTimescaleDBData** (lines 26-32) — replace single `worker` field with the new collaborators:

```python
@dataclass
class HaTimescaleDBData:
    states_worker: "TimescaledbStateRecorderThread"
    meta_worker: "TimescaledbMetaRecorderThread"
    ingester: StateIngester
    syncer: MetadataSyncer
    live_queue: "OverflowQueue"
    meta_queue: "PersistentQueue"
    backfill_queue: queue.Queue
    backfill_request: asyncio.Event
    stop_event: threading.Event
    orchestrator_task: asyncio.Task | None
```

**async_setup_entry** (adapts lines 53-108 with D-12's 8 steps):

```python
async def async_setup_entry(hass: HomeAssistant, entry: HaTimescaleDBConfigEntry) -> bool:
    dsn = entry.data[CONF_DSN]
    options = entry.options
    chunk_interval_days = options.get(CONF_CHUNK_INTERVAL, DEFAULT_CHUNK_INTERVAL_DAYS)
    compress_after_hours = options.get(CONF_COMPRESS_AFTER, DEFAULT_COMPRESS_AFTER_HOURS)
    entity_filter = _get_entity_filter(entry)

    # D-12 step 1: open PersistentQueue for metadata
    meta_queue = PersistentQueue(hass.config.path(DOMAIN, "metadata_queue.jsonl"))

    # Shared shutdown signal across worker threads and orchestrator.
    stop_event = threading.Event()

    # Queues — live_queue = OverflowQueue; backfill_queue = stdlib Queue(maxsize=2)
    live_queue = OverflowQueue(maxsize=LIVE_QUEUE_MAXSIZE)
    backfill_queue: queue.Queue = queue.Queue(maxsize=BACKFILL_QUEUE_MAXSIZE)
    backfill_request = asyncio.Event()

    # Syncer needs meta_queue for enqueue; worker needs syncer for change-detection.
    syncer = MetadataSyncer(hass=hass, meta_queue=meta_queue)

    meta_worker = TimescaledbMetaRecorderThread(
        hass=hass, dsn=dsn, meta_queue=meta_queue,
        syncer=syncer, stop_event=stop_event,
    )
    states_worker = TimescaledbStateRecorderThread(
        hass=hass, dsn=dsn,
        live_queue=live_queue, backfill_queue=backfill_queue,
        backfill_request=backfill_request, stop_event=stop_event,
    )

    # D-12 step 2: start meta worker (immediately drains file from prior outage)
    meta_worker.start()
    # D-12 step 3: start states worker (enters MODE_INIT; signals backfill_request which nobody is listening yet — no-op)
    states_worker.start()
    # D-12 step 4: drain pre-existing persisted items before new events arrive
    await meta_queue.join()
    # D-12 step 5: initial registry backfill — enumerate HA registries, call write_item path
    await _async_initial_registry_backfill(hass, meta_queue)

    ingester = StateIngester(hass=hass, queue=live_queue, entity_filter=entity_filter)

    started: list[str] = []
    try:
        # D-12 step 6: subscribe to HA registry update events (syncer.async_start registers 4)
        await syncer.async_start()
        started.append("syncer")
        # D-12 step 7: subscribe to state_changed events
        ingester.async_start()
        started.append("ingester")
    except Exception:
        _LOGGER.exception("Setup failed after starting %s; rolling back", started)
        if "ingester" in started:
            ingester.stop()
        if "syncer" in started:
            await syncer.async_stop()
        stop_event.set()
        meta_queue.wake_consumer()
        await hass.async_add_executor_job(meta_worker.join, 30)
        await hass.async_add_executor_job(states_worker.join, 30)
        raise

    # D-12 step 8: on HA started, spawn backfill orchestrator
    orch_task_holder: list[asyncio.Task] = []

    @callback
    def _on_ha_started(_event):
        task = hass.async_create_task(backfill_orchestrator(
            hass,
            live_queue=live_queue,
            backfill_queue=backfill_queue,
            backfill_request=backfill_request,
            read_watermark=states_worker.read_watermark,
            open_entities_reader=states_worker.read_open_entities,
            entity_filter=entity_filter,
            stop_event=asyncio.Event(),  # wired separately per planner
        ))
        orch_task_holder.append(task)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_ha_started)

    entry.runtime_data = HaTimescaleDBData(
        states_worker=states_worker,
        meta_worker=meta_worker,
        ingester=ingester,
        syncer=syncer,
        live_queue=live_queue,
        meta_queue=meta_queue,
        backfill_queue=backfill_queue,
        backfill_request=backfill_request,
        stop_event=stop_event,
        orchestrator_task=None,   # set from closure once step 8 fires
    )
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True
```

**async_unload_entry** (adapts lines 116-127, D-13):

```python
async def async_unload_entry(hass: HomeAssistant, entry: HaTimescaleDBConfigEntry) -> bool:
    data: HaTimescaleDBData = entry.runtime_data
    # D-13-b step 1: stop_event.set() — orchestrator backoff, worker loops observe
    data.stop_event.set()
    # D-13-b step 2: cancel orchestrator
    if data.orchestrator_task is not None:
        data.orchestrator_task.cancel()
        try:
            await data.orchestrator_task
        except asyncio.CancelledError:
            pass
    # Stop HA event listeners first so no new items enqueue
    data.ingester.stop()
    await data.syncer.async_stop()
    # D-13-b step 3: unblock meta worker's Condition wait
    data.meta_queue.wake_consumer()
    # D-13-b step 4 + 5: join workers with 30s timeout (WATCH-02: no run_coroutine_threadsafe)
    await hass.async_add_executor_job(data.states_worker.join, 30)
    await hass.async_add_executor_job(data.meta_worker.join, 30)
    if data.states_worker.is_alive():
        _LOGGER.critical("states worker did not exit within 30s; Phase 3 watchdog will address")
    if data.meta_worker.is_alive():
        _LOGGER.critical("meta worker did not exit within 30s")
    # D-13-b step 6: connections are closed inside run() after loop exit
    return True
```

**Initial registry backfill** (D-12 step 5) — new helper on event loop:

```python
async def _async_initial_registry_backfill(hass: HomeAssistant, meta_queue: PersistentQueue) -> None:
    """Enumerate all four HA registries; enqueue create items.

    Idempotent by construction: meta_worker dispatches through SCD2_SNAPSHOT_*_SQL
    which carries a WHERE NOT EXISTS guard. If the dim_* row already exists,
    the INSERT is a no-op.

    Iteration order per CONTEXT Claude's Discretion: area → label → entity → device.
    Rationale: loose FK-like dependency order; ensures referenced area_id/label_id
    rows exist before the referencing entity/device row lands.
    """
    entity_reg = er.async_get(hass)
    device_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)
    label_reg = lr.async_get(hass)
    now = datetime.now(timezone.utc)
    # Shape-identical to syncer.async_start snapshot block, but targeting meta_queue.
    # ... (planner fills in)
```

---

### `strings.json` (modify — add issues section)

**Analog:** `strings.json` (self, existing sections `config` lines 2-17 and `options` lines 18-36).

**Pattern to add** (append top-level key after `options`):

```json
{
  "config": { ... },
  "options": { ... },
  "issues": {
    "buffer_dropping": {
      "title": "TimescaleDB recorder buffer overflow",
      "description": "The TimescaleDB recorder in-memory buffer filled up and is dropping state events. This usually means the TimescaleDB database has been unavailable for an extended period. Dropped events will be recovered automatically from Home Assistant's sqlite recorder once the database is reachable again. No user action is required; this issue will clear itself."
    }
  }
}
```

**Extension point** (D-10-e): Phase 3 appends additional keys (`db_unreachable`, `recorder_disabled`) under `"issues"` — no re-implementation needed.

---

## Shared Patterns

### `@callback` decorator (event loop boundary)

**Source:** `ingester.py:44-52`, `syncer.py:348, 383, 415, 457`

**Apply to:** `ingester.py` `_handle_state_changed` (unchanged); all `syncer.py` `_handle_*_registry_updated` (unchanged).

```python
from homeassistant.core import callback

@callback
def _handle_state_changed(self, event: Event) -> None:
    """Synchronous, never awaits. Enqueue-only."""
```

All `@callback` handlers remain synchronous. For Phase 2, the syncer handlers schedule a coroutine (`hass.async_create_task(self._meta_queue.put_async(item))`) — scheduling is sync even though the target is a coroutine.

### Cross-thread wakes (asyncio.Event from worker thread)

**Source:** No Phase 1 use; new in Phase 2. Pattern from RESEARCH.md / HA async docs.

**Apply to:** `states_worker.py` when transitioning to MODE_BACKFILL (D-04-c), D-10-b overflow flip surface.

```python
# CORRECT — thread-safe cross-thread wake
self._hass.loop.call_soon_threadsafe(self._backfill_request.set)

# WRONG — asyncio.Event methods are NOT thread-safe
# self._backfill_request.set()    # do NOT call directly from worker thread
```

### `hass.add_job(coro)` fire-and-forget from worker thread

**Source:** `worker.py:82-85` (comment documenting the pattern).

**Apply to:** `meta_worker.py` and `states_worker.py` for issue/notification calls.

```python
# In worker thread (states_worker or meta_worker):
self._hass.add_job(create_buffer_dropping_issue, self._hass)
self._hass.add_job(
    hass.components.persistent_notification.async_create,
    "TimescaleDB write stalled", "TimescaleDB Recorder",
)
```

### `hass.async_add_executor_job(fn, ...)` for crossing into worker/recorder pool

**Source:** `worker.py:386` (thread.join), `__init__.py:103,126` (shutdown join).

**Apply to:** `backfill.py` (watermark read, blocking queue.put), `__init__.py` shutdown.

```python
# Orchestrator — event loop dispatches blocking call
await hass.async_add_executor_job(backfill_queue.put, raw)      # backpressure blocking put
wm = await hass.async_add_executor_job(read_watermark)          # D-08-d step 4

# Recorder pool (separate executor, enforced API)
raw = await recorder_instance.async_add_executor_job(_fetch_slice_raw, ...)
```

### Graceful shutdown (WATCH-02)

**Source:** `worker.py:375-391`, `__init__.py:116-127`.

**Apply to:** `__init__.py` async_unload_entry (D-13), all worker `run()` loops.

```python
# Event-loop side — never block, never run_coroutine_threadsafe().result()
stop_event.set()
meta_queue.wake_consumer()                                     # unblock Condition
await hass.async_add_executor_job(states_worker.join, 30)
await hass.async_add_executor_job(meta_worker.join, 30)

# Worker thread loop top — always check stop_event early
while not self._stop_event.is_set():
    ...
```

### psycopg3 cursor-per-operation + Jsonb wrapping

**Source:** `worker.py:212-224`.

**Apply to:** `states_worker._insert_chunk_raw`, `meta_worker._process_entity`/etc.

```python
from psycopg.types.json import Jsonb

with conn.cursor() as cur:
    cur.executemany(INSERT_SQL, [
        (r.entity_id, r.state, Jsonb(r.attributes), r.last_updated, r.last_changed)
        for r in chunk
    ])

# For dict-keyed change-detection reads, use dict_row factory:
with conn.cursor(row_factory=psycopg.rows.dict_row) as dict_cur:
    changed = self._syncer._entity_row_changed(dict_cur, ...)
```

### SCD2 dispatch (atomic close+insert via conn.transaction)

**Source:** `worker.py:280-294` (entity), `worker.py:310-344` (device/area/label).

**Apply to:** `meta_worker.py` `_process_*` methods — verbatim reuse with parameter rehydration.

```python
if action == "update":
    # Rename path — atomic close+insert under transaction()
    with conn.transaction():
        cur.execute(SCD2_CLOSE_ENTITY_SQL, (now, old_id))
        cur.execute(SCD2_INSERT_ENTITY_SQL, (*params,))
```

### Registration / cancellation of HA listeners

**Source:** `syncer.py:170-189, 488-496`; `ingester.py:36-42, 68-80`.

**Apply to:** `syncer.py` (unchanged), `ingester.py` (unchanged), `__init__.py` `_on_ha_started` one-shot.

```python
# One-shot listener for orchestrator spawn (D-12 step 8)
hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_ha_started)

# Regular listeners — returned cancel callable stored for teardown
self._cancel_listeners.append(
    self._hass.bus.async_listen(EVENT_ENTITY_REGISTRY_UPDATED, self._handle_entity_registry_updated)
)
```

### Logging pattern

**Source:** Every module (e.g., `worker.py:31`, `syncer.py:29`).

**Apply to:** every new file.

```python
import logging
_LOGGER = logging.getLogger(__name__)
```

### Per-item exception boundary (bad item must not kill thread)

**Source:** `worker.py:177-187`.

**Apply to:** `meta_worker.run()` around the retry-wrapped `_write_item` — though retry-until-success changes this: with retry, a persistent failure loops forever instead of being skipped. D-07-d is explicit that this is the accepted tradeoff (recovery = HA restart). So the per-item try/except from Phase 1 is deliberately removed.

```python
# Phase 1 pattern (deliberately NOT ported to Phase 2):
# try:
#     self._process_meta_command(item)
# except Exception as exc:
#     _LOGGER.error("...skipped: %s", exc)
#
# Phase 2: retry_until_success catches everything and retries forever.
# Consequence: poisoned items stall the worker. Accepted per D-07-d.
```

### Test pattern (unit tests with mock psycopg3)

**Source:** `tests/conftest.py:24-43` (`mock_psycopg_conn` fixture), `tests/test_worker.py` structure.

**Apply to:** new `tests/test_overflow_queue.py`, `tests/test_persistent_queue.py`, `tests/test_retry.py`, `tests/test_backfill.py`, `tests/test_states_worker.py`, `tests/test_meta_worker.py`, `tests/test_issues.py`.

```python
# Conftest already provides mock_psycopg_conn for sync worker tests.
# For PersistentQueue tests, use tmp_path for file isolation:
def test_put_get_task_done(tmp_path):
    q = PersistentQueue(tmp_path / "q.jsonl")
    q.put({"k": "v"})
    assert q.get() == {"k": "v"}
    q.task_done()
    assert q.get() is None or q._read_first_line() is None
```

## No Analog Found

Files with no close match in the codebase (planner should synthesize from RESEARCH.md + CONTEXT D-02..D-10):

| File | Role | Data Flow | Reason |
|------|------|-----------|--------|
| `persistent_queue.py` | utility | file-I/O + event-driven | No file-persistent data structure exists in repo. Closest conceptual reference: HA recorder's own commit queue (external to this repo). Pattern synthesized from `queue.Queue` blocking semantics + stdlib `tempfile` + `os.replace` atomic swap. |
| `retry.py` | utility | transform decorator | No decorator-based retry pattern in Phase 1 (error handling was one-shot log-and-drop at `worker.py:228-234`). Pattern synthesized from `stop_event.wait(backoff)` shutdown-aware sleep idiom + standard exponential backoff. |

## Metadata

**Analog search scope:** `custom_components/ha_timescaledb_recorder/` (7 source files), `scripts/backfill_gaps.py` (external reference for `ON CONFLICT DO NOTHING` shape), `tests/conftest.py` (fixture reuse).
**Files scanned:** 13
**Pattern extraction date:** 2026-04-21
