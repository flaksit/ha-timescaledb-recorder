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

Robustness: the entire poll body runs inside try/except Exception so a bug
in one monitoring cycle cannot kill the watchdog task. (Review MEDIUM-9.)

Assumptions:
- runtime.loop_stop_event is an asyncio.Event used as interruptible sleep.
- runtime.stop_event is the threading.Event shared with workers.
- runtime has fields dsn, live_queue, backfill_queue, backfill_request,
  meta_queue, registry_listener, chunk_interval_days, compress_after_hours — added
  to TimescaledbRecorderData by Plan 07. See PATTERNS.md __init__.py section.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback

from .const import WATCHDOG_INTERVAL_S
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
    # Throttle: wait before respawn to prevent tight crash/notify/restart loops.
    # Does not significantly impact recovery time (WATCHDOG_INTERVAL_S=10s).
    # (Cross-AI review 2026-04-23, concern MEDIUM-5.)
    await asyncio.sleep(5)
    new_thread = spawn_fn(hass, runtime)
    setattr(runtime, component, new_thread)
    new_thread.start()


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
    @callback
    def _on_ha_stop(_event) -> None:
        runtime.loop_stop_event.set()

    cancel_stop_listener = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_ha_stop)

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
                if not runtime.states_worker.is_alive() and not runtime.stop_event.is_set():
                    await _watchdog_respawn(
                        hass, runtime, component="states_worker",
                        dead_thread=runtime.states_worker,
                        spawn_fn=spawn_states_worker,
                    )
                if not runtime.meta_worker.is_alive() and not runtime.stop_event.is_set():
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
    finally:
        cancel_stop_listener()
