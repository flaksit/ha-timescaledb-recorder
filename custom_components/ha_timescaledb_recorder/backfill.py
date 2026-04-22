"""Event-loop orchestrator: fills gaps from HA sqlite into the states worker via a bounded queue.

D-08: the only place in this integration that reads from HA's sqlite recorder.
Orchestrator dispatches only; sqlite reads run in recorder pool; transforms
run in the states worker thread. Three-way separation keeps event-loop CPU
work at microseconds per slice (dispatch + await + queue.put).
"""
from __future__ import annotations

import asyncio
import logging
import queue
from datetime import datetime, timedelta, timezone
from typing import Callable

from homeassistant.components import recorder
from homeassistant.components.recorder import history as recorder_history
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .issues import clear_buffer_dropping_issue

_LOGGER = logging.getLogger(__name__)

# Sentinel pushed onto backfill_queue after the last slice of a cycle (D-04-d).
BACKFILL_DONE = object()

# D-08-d step 7 tuning. Keep as module-level constants so tests can monkeypatch.
_SLICE_WINDOW = timedelta(minutes=5)
_LATE_ARRIVAL_GRACE = timedelta(minutes=10)   # watermark - 10min (D-08-d step 6)
_RECORDER_COMMIT_LAG = timedelta(seconds=5)   # wait past slice_end before reading


def _fetch_slice_raw(
    hass: HomeAssistant,
    entities: set[str],
    t_start: datetime,
    t_end: datetime,
) -> dict:
    """Iterate per entity_id and return dict[entity_id -> list[HA State]].

    D-08-e: runs in the HA recorder executor pool. include_start_time_state=False
    avoids re-ingesting boundary rows on every slice (research SUMMARY).
    Passing a None entity_id raises ValueError — must iterate per entity_id.
    """
    out: dict[str, list] = {}
    for eid in entities:
        states = recorder_history.state_changes_during_period(
            hass,
            t_start,
            t_end,
            entity_id=eid,
            include_start_time_state=False,
        )
        out[eid] = states.get(eid, [])
    return out


async def backfill_orchestrator(
    hass: HomeAssistant,
    *,
    live_queue,                                      # OverflowQueue
    backfill_queue: queue.Queue,
    backfill_request: asyncio.Event,
    read_watermark: Callable[[], "datetime | None"],  # sync; runs in executor
    open_entities_reader: Callable[[], set],          # sync; runs in executor
    entity_filter: Callable[[str], bool],
    stop_event: asyncio.Event,
) -> None:
    """Long-running event-loop task driving HA sqlite backfill on demand.

    D-08-a: spawned from async_setup_entry via
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, ...)
    D-08-c: awakens on backfill_request.set(); processes one cycle; loops.
    D-08-g: cascade prevention — backfill_queue(maxsize=2) blocking put keeps
            orchestrator fetch-rate <= worker write-rate.
    Shutdown: stop_event.is_set() check at loop top and after backfill_request.wait()
            returns → orchestrator exits cleanly.
    """
    recorder_instance = recorder.get_instance(hass)
    while not stop_event.is_set():
        await backfill_request.wait()
        backfill_request.clear()
        if stop_event.is_set():
            return

        # D-08-d step 2-3: capture "now" before clearing so live producers
        # resume enqueue at t_clear while we fetch [wm-10min, t_clear).
        t_clear = datetime.now(timezone.utc)
        dropped = live_queue.clear_and_reset_overflow()
        if dropped > 0:
            _LOGGER.warning(
                "overflow cleared, dropped %d events during outage", dropped,
            )
        # D-10-c: clear repair issue now that live_queue is accepting again.
        clear_buffer_dropping_issue(hass)

        # D-08-d step 4: watermark read via states worker's connection.
        wm = await hass.async_add_executor_job(read_watermark)
        if wm is None:
            _LOGGER.info(
                "ha_states is empty — first-install bulk import is out of scope; "
                "use paradise-ha-tsdb/scripts/backfill/backfill.py for bulk import",
            )
            await hass.async_add_executor_job(backfill_queue.put, BACKFILL_DONE)
            continue

        from_ = wm - _LATE_ARRIVAL_GRACE          # D-08-d step 6
        cutoff = t_clear

        # D-08-f: entity set = live registry (filtered) ∪ open rows in dim_entities.
        entity_reg = er.async_get(hass)
        live_entities: set[str] = {
            e.entity_id
            for e in entity_reg.entities.values()
            if entity_filter(e.entity_id)
        }
        open_entities = await hass.async_add_executor_job(open_entities_reader)
        entities = live_entities | open_entities
        if not entities:
            _LOGGER.info("backfill: no entities to query, skipping")
            await hass.async_add_executor_job(backfill_queue.put, BACKFILL_DONE)
            continue

        slice_start = from_
        while slice_start < cutoff and not stop_event.is_set():
            slice_end = min(slice_start + _SLICE_WINDOW, cutoff)
            # D-08-d step 7: wait until commit-lag has elapsed past slice_end.
            # Uses stop_event.wait() with timeout instead of bare asyncio.sleep() so
            # a shutdown signal unblocks the orchestrator immediately rather than
            # waiting the full commit-lag window (WATCH-02 extension to event-loop side).
            delay = (
                slice_end + _RECORDER_COMMIT_LAG - datetime.now(timezone.utc)
            ).total_seconds()
            if delay > 0:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=delay)
                    # stop_event set during sleep — exit cleanly
                    return
                except asyncio.TimeoutError:
                    pass  # normal: delay elapsed, proceed to fetch
            raw = await recorder_instance.async_add_executor_job(
                _fetch_slice_raw, hass, entities, slice_start, slice_end,
            )
            # Blocking put = natural backpressure at maxsize=2 (D-08-g).
            await hass.async_add_executor_job(backfill_queue.put, raw)
            slice_start = slice_end

        # D-08-d step 8: end of cycle.
        await hass.async_add_executor_job(backfill_queue.put, BACKFILL_DONE)
