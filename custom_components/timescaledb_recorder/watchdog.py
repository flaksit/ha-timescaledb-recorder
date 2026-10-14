"""Async watchdog supervising the states_worker and meta_worker threads.

D-05: single coroutine watchdog_loop polls thread.is_alive() at
WATCHDOG_INTERVAL_S cadence. On dead-thread detection + not-shutting-down,
fires notify_watchdog_recovery with the thread's _last_exception and
_last_context (set by the worker's D-06-a outer try/except in run()),
then waits 5s (restart throttle — cross-AI review MEDIUM-5) before
respawning the thread via the appropriate spawn factory.

D-05-b: orchestrator crash recovery uses a different mechanism
(asyncio.Task.add_done_callback — see __init__.py). This watchdog covers
ONLY the worker threads.

Issue #14: a worker stuck inside a DB call is alive and raises nothing, so
neither check above sees it. Each tick also reads the workers' in-flight DB
calls (and the states worker's backfill wait) and raises the
ingestion_stalled repair issue past INGESTION_STALL_THRESHOLD_S. Detection
only: a thread cannot be killed, and cancelling its query from this thread is
not documented as safe for psycopg's cancel_safe(); the statement and TCP
timeouts in db.connect are what end such a call.

Robustness: the entire poll body runs inside try/except Exception so a bug
in one monitoring cycle cannot kill the watchdog task. (Review MEDIUM-9.)

Assumptions:
- runtime.loop_stop_event is an asyncio.Event used as interruptible sleep.
- runtime.stop_event is the threading.Event shared with workers.
- runtime has fields dsn, live_queue, backfill_queue, backfill_request,
  meta_queue, registry_listener, chunk_interval_days, compress_after_hours,
  states_stats — added
  to TimescaledbRecorderData by Plan 07. See PATTERNS.md __init__.py section.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from homeassistant.components import persistent_notification
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    INGESTION_STALL_THRESHOLD_S,
    SIGNAL_WORKER_STATE_CHANGE,
    WATCHDOG_INTERVAL_S,
)
from .issues import clear_ingestion_stalled_issue, create_ingestion_stalled_issue
from .meta_worker import TimescaledbMetaRecorderThread
from .notifications import notify_watchdog_recovery
from .states_worker import TimescaledbStateRecorderThread

if TYPE_CHECKING:
    from . import TimescaledbRecorderData

_LOGGER = logging.getLogger(__name__)


def spawn_states_worker(
    hass: HomeAssistant,
    runtime: "TimescaledbRecorderData",
) -> TimescaledbStateRecorderThread:
    """Construct a new states_worker with shared queues + stop_event.

    Sources all constructor args from runtime; the caller is responsible
    for calling .start() on the returned thread. Used by:
    - async_setup_entry (Plan 07) — initial spawn.
    - watchdog_loop — post-crash respawn (after 5s throttle delay).
    """
    return TimescaledbStateRecorderThread(
        hass=hass,
        dsn=runtime.dsn,
        live_queue=runtime.live_queue,
        backfill_queue=runtime.backfill_queue,
        backfill_request=runtime.backfill_request,
        stop_event=runtime.stop_event,
        chunk_interval_days=runtime.chunk_interval_days,
        compress_after_hours=runtime.compress_after_hours,
        stats=runtime.states_stats,
        schema_ready=runtime.schema_ready,
    )


def spawn_meta_worker(
    hass: HomeAssistant,
    runtime: "TimescaledbRecorderData",
) -> TimescaledbMetaRecorderThread:
    """Construct a new meta_worker with shared meta_queue + registry_listener + stop_event."""
    return TimescaledbMetaRecorderThread(
        hass=hass,
        dsn=runtime.dsn,
        meta_queue=runtime.meta_queue,
        registry_listener=runtime.registry_listener,
        stop_event=runtime.stop_event,
        schema_ready=runtime.schema_ready,
    )


async def _watchdog_respawn(
    hass: HomeAssistant,
    runtime: "TimescaledbRecorderData",
    *,
    component: str,
    dead_thread,
    spawn_fn,
) -> None:
    """Extract post-mortem context, fire notification, throttle, respawn thread.

    The 5s asyncio.sleep before respawn prevents crash-loop notification churn
    when a worker crashes immediately on start (cross-AI review MEDIUM-5).
    """
    exc = getattr(dead_thread, "_last_exception", None)
    ctx = getattr(dead_thread, "_last_context", {}) or {}
    _LOGGER.error(
        "Watchdog detected dead %s thread — respawning after 5s", component,
    )
    notify_watchdog_recovery(hass, component=component, exc=exc, context=ctx)
    # Signal worker-dead state before the throttle sleep so push-updated
    # binary sensors reflect the dead state immediately (not 5s later).
    async_dispatcher_send(hass, SIGNAL_WORKER_STATE_CHANGE)
    # Throttle: wait before respawn to prevent tight crash/notify/restart loops.
    # Does not significantly impact recovery time (WATCHDOG_INTERVAL_S=10s).
    # (Cross-AI review 2026-04-23, concern MEDIUM-5.)
    await asyncio.sleep(5)
    new_thread = spawn_fn(hass, runtime)
    setattr(runtime, component, new_thread)
    new_thread.start()
    # Signal worker-alive state after respawn so binary sensors show recovery.
    async_dispatcher_send(hass, SIGNAL_WORKER_STATE_CHANGE)


@dataclass
class _StallTracker:
    """Watchdog-local state for ingestion stall detection."""

    # Monotonic time HA was first seen RUNNING. The backfill orchestrator only
    # starts then, so the states worker's backfill wait counts from here.
    running_since: float | None = None
    # component -> (details, monotonic time first reported)
    stalled: dict[str, tuple[str, float]] = field(default_factory=dict)


def _stall_details(
    component: str, worker, now: float, running_since: float | None,
) -> str | None:
    """Describe why `worker` counts as stalled, or None if it does not."""
    if worker is None or not worker.is_alive():
        return None  # dead threads are the respawn path's job
    in_flight = worker.db_calls.oldest()
    if in_flight is not None:
        op, started = in_flight
        if now - started >= INGESTION_STALL_THRESHOLD_S:
            return (
                f"{component} DB call {op} in flight for {(now - started) / 60:.0f} min "
                f"(last_op {getattr(worker, '_last_op', 'unknown')})"
            )
    waiting_since = getattr(worker, "backfill_wait_since", None)
    if waiting_since is not None and running_since is not None:
        waited = now - max(waiting_since, running_since)
        if waited >= INGESTION_STALL_THRESHOLD_S:
            return (
                f"{component} waiting {waited / 60:.0f} min for backfill data "
                "while live state changes queue up"
            )
    return None


def _check_stalls(
    hass: HomeAssistant, runtime: "TimescaledbRecorderData", tracker: _StallTracker,
) -> None:
    """Raise, update or clear ingestion_stalled from the workers' current state.

    Details are captured when a worker is first found stalled and kept until it
    recovers, so the repair issue is rewritten only when the set changes.
    """
    now = time.monotonic()
    if tracker.running_since is None and hass.state is CoreState.running:
        tracker.running_since = now
    changed = False
    for component in ("states_worker", "meta_worker"):
        details = _stall_details(
            component, getattr(runtime, component), now, tracker.running_since,
        )
        if details is not None and component not in tracker.stalled:
            tracker.stalled[component] = (details, now)
            changed = True
            _LOGGER.error("Ingestion stalled: %s", details)
            persistent_notification.async_create(
                hass,
                f"{details}. Nothing is being written by this worker and it reported "
                "no error. See the ingestion_stalled repair issue.",
                "TimescaleDB recorder: ingestion stalled",
                f"timescaledb_recorder.ingestion_stalled_{component}",
            )
        elif details is None and component in tracker.stalled:
            _, since = tracker.stalled.pop(component)
            changed = True
            _LOGGER.info(
                "Ingestion resumed: %s is making progress again after being reported "
                "stalled for %.0f s", component, now - since,
            )
    if not changed:
        return
    if tracker.stalled:
        create_ingestion_stalled_issue(
            hass, "; ".join(d for d, _ in tracker.stalled.values()),
        )
    else:
        clear_ingestion_stalled_issue(hass)


async def watchdog_loop(
    hass: HomeAssistant,
    runtime: "TimescaledbRecorderData",
) -> None:
    """Async supervisor for both worker threads (D-05).

    Cadence: WATCHDOG_INTERVAL_S. Interruptible sleep via loop_stop_event.
    On detected-dead-thread + not-shutting-down: fire recovery notification,
    wait 5s (restart throttle), then respawn. Cleans up on loop_stop_event set.

    The poll body is wrapped in try/except Exception so a monitoring-cycle bug
    does not kill this task. (Cross-AI review MEDIUM-9.)

    EVENT_HOMEASSISTANT_STOP listener: belt-and-suspenders exit so the task
    ends during HA's STOPPING stage regardless of when async_unload_entry runs.
    Without this, HA logs "still running after final writes shutdown stage" when
    the restart path fires FINAL_WRITE before unload completes.
    """
    cancel_stop_listener: Callable[[], None] | None = None

    @callback
    def _on_ha_stop(_event) -> None:
        # A one-time listener removes itself when it fires; calling the remover
        # again makes HA log "Unable to remove unknown job listener".
        nonlocal cancel_stop_listener
        cancel_stop_listener = None
        runtime.loop_stop_event.set()

    cancel_stop_listener = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_ha_stop)
    stall_tracker = _StallTracker()

    try:
        while not runtime.loop_stop_event.is_set():
            try:
                # Interruptible sleep. If loop_stop_event is set during the sleep,
                # wait_for returns normally (event set); otherwise asyncio.TimeoutError.
                await asyncio.wait_for(
                    runtime.loop_stop_event.wait(),
                    timeout=WATCHDOG_INTERVAL_S,
                )
                return  # loop_stop_event set — shutdown path
            except asyncio.TimeoutError:
                pass  # normal cadence tick

            # Defensive: shutdown may have been signalled between wait and poll.
            if runtime.stop_event.is_set() or runtime.loop_stop_event.is_set():
                return

            # Entire poll body in try/except — a bug in one cycle must not kill
            # the watchdog task and leave the workers unsupervised. (MEDIUM-9)
            try:
                if (
                    runtime.states_worker is not None
                    and not runtime.states_worker.is_alive()
                    and not runtime.stop_event.is_set()
                ):
                    await _watchdog_respawn(
                        hass, runtime, component="states_worker",
                        dead_thread=runtime.states_worker,
                        spawn_fn=spawn_states_worker,
                    )
                if (
                    runtime.meta_worker is not None
                    and not runtime.meta_worker.is_alive()
                    and not runtime.stop_event.is_set()
                ):
                    await _watchdog_respawn(
                        hass, runtime, component="meta_worker",
                        dead_thread=runtime.meta_worker,
                        spawn_fn=spawn_meta_worker,
                    )
            except Exception:  # noqa: BLE001
                _LOGGER.warning(
                    "Watchdog poll cycle raised unexpected exception — loop continues",
                    exc_info=True,
                )
            # Separate guard: a bug here must not cost the respawn check, and
            # vice versa.
            try:
                _check_stalls(hass, runtime, stall_tracker)
            except Exception:  # noqa: BLE001
                _LOGGER.warning(
                    "Watchdog stall check raised unexpected exception — loop continues",
                    exc_info=True,
                )
    finally:
        if cancel_stop_listener is not None:
            cancel_stop_listener()
