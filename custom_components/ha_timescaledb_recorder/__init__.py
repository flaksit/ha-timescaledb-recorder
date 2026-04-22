"""TimescaleDB Recorder custom component for Home Assistant.

Phase 2 topology: one thread for states, one thread for metadata; a bounded
in-memory queue for live events; a file-persisted queue for metadata; an
event-loop orchestrator driving backfill from HA sqlite on demand.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from homeassistant.helpers.entityfilter import (
    convert_filter,
    convert_include_exclude_filter,
)

from .backfill import backfill_orchestrator
from .const import (
    BACKFILL_QUEUE_MAXSIZE,
    CONF_CHUNK_INTERVAL,
    CONF_COMPRESS_AFTER,
    CONF_DSN,
    DEFAULT_CHUNK_INTERVAL_DAYS,
    DEFAULT_COMPRESS_AFTER_HOURS,
    DOMAIN,
    LIVE_QUEUE_MAXSIZE,
)
from .ingester import StateIngester
from .issues import create_buffer_dropping_issue
from .meta_worker import TimescaledbMetaRecorderThread
from .overflow_queue import OverflowQueue
from .persistent_queue import PersistentQueue
from .states_worker import TimescaledbStateRecorderThread
from .syncer import MetadataSyncer, _to_json_safe

_LOGGER = logging.getLogger(__name__)

# Sleep interval for the overflow watcher task. Small enough to notice the
# flag flip quickly, large enough to be effectively free. The watcher only
# exists to surface the D-10-b repair issue; orchestrator does the actual
# reactive work via backfill_request.
_OVERFLOW_WATCH_INTERVAL_S = 0.5


@dataclass
class HaTimescaleDBData:
    """Runtime data stored on a config entry."""

    states_worker: TimescaledbStateRecorderThread
    meta_worker: TimescaledbMetaRecorderThread
    ingester: StateIngester
    syncer: MetadataSyncer
    live_queue: OverflowQueue
    meta_queue: PersistentQueue
    backfill_queue: queue.Queue
    backfill_request: asyncio.Event
    stop_event: threading.Event
    loop_stop_event: asyncio.Event
    orchestrator_task: asyncio.Task | None
    overflow_watcher_task: asyncio.Task | None


HaTimescaleDBConfigEntry = ConfigEntry[HaTimescaleDBData]


def _get_entity_filter(entry):
    """Build entity filter from config entry data (unchanged from Phase 1)."""
    raw_filter = entry.data.get("filter", {})
    if raw_filter:
        return convert_include_exclude_filter(raw_filter)
    return convert_filter({
        "include_domains": [],
        "include_entity_globs": [],
        "include_entities": [],
        "exclude_domains": [],
        "exclude_entity_globs": [],
        "exclude_entities": [],
    })


async def _async_initial_registry_backfill(
    hass: HomeAssistant, meta_queue: PersistentQueue,
) -> None:
    """Enumerate all four HA registries and enqueue create items (D-12 step 5).

    Idempotent by construction: meta_worker dispatches through SCD2_SNAPSHOT_*
    which carries a WHERE NOT EXISTS guard.

    Iteration order per CONTEXT Claude's Discretion: area → label → entity → device.
    Rationale: loose FK-like dependency ordering — referenced area_id / label_id
    rows land before the referencing entity/device rows.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    area_reg = ar.async_get(hass)
    label_reg = lr.async_get(hass)
    entity_reg = er.async_get(hass)
    device_reg = dr.async_get(hass)

    # Construct a throwaway MetadataSyncer bound to meta_queue so we can reuse
    # its _extract_*_params helpers without duplicating the extraction logic.
    # At this point syncer.async_start() has NOT been called, so _entity_reg etc.
    # are None — but the extract helpers receive the registry entry directly as
    # an argument, so they do not read self._entity_reg.
    now = datetime.now(timezone.utc)
    syncer_helper = MetadataSyncer(hass=hass, meta_queue=meta_queue)

    for entry in area_reg.async_list_areas():
        params = syncer_helper._extract_area_params(entry, now)
        await meta_queue.put_async({
            "registry": "area",
            "action": "create",
            "registry_id": entry.id,
            "old_id": None,
            "params": _to_json_safe(params),
            "enqueued_at": now_iso,
        })

    for entry in label_reg.async_list_labels():
        params = syncer_helper._extract_label_params(entry, now)
        await meta_queue.put_async({
            "registry": "label",
            "action": "create",
            "registry_id": entry.label_id,
            "old_id": None,
            "params": _to_json_safe(params),
            "enqueued_at": now_iso,
        })

    for entry in entity_reg.entities.values():
        params = syncer_helper._extract_entity_params(entry, now)
        await meta_queue.put_async({
            "registry": "entity",
            "action": "create",
            "registry_id": entry.entity_id,
            "old_id": None,
            "params": _to_json_safe(params),
            "enqueued_at": now_iso,
        })

    for entry in device_reg.devices.values():
        params = syncer_helper._extract_device_params(entry, now)
        await meta_queue.put_async({
            "registry": "device",
            "action": "create",
            "registry_id": entry.id,
            "old_id": None,
            "params": _to_json_safe(params),
            "enqueued_at": now_iso,
        })


async def _overflow_watcher(
    hass: HomeAssistant,
    live_queue: OverflowQueue,
    stop_event: asyncio.Event,
) -> None:
    """Watch live_queue.overflowed; fire buffer_dropping repair issue on flip (D-10-b).

    The repair issue is cleared by the orchestrator (plan 08) when it runs
    clear_and_reset_overflow. Between flip and clear the issue remains
    visible in the HA Repairs UI.
    """
    last_seen = False
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=_OVERFLOW_WATCH_INTERVAL_S,
            )
            return
        except asyncio.TimeoutError:
            pass
        now = live_queue.overflowed
        if now and not last_seen:
            create_buffer_dropping_issue(hass)
        last_seen = now


async def async_setup_entry(
    hass: HomeAssistant, entry: HaTimescaleDBConfigEntry,
) -> bool:
    """Set up the TimescaleDB integration from a config entry (D-12 8 steps)."""
    dsn = entry.data[CONF_DSN]
    options = entry.options
    chunk_interval_days = options.get(CONF_CHUNK_INTERVAL, DEFAULT_CHUNK_INTERVAL_DAYS)
    compress_after_hours = options.get(CONF_COMPRESS_AFTER, DEFAULT_COMPRESS_AFTER_HOURS)
    entity_filter = _get_entity_filter(entry)

    # D-12 step 1: open PersistentQueue
    meta_queue = PersistentQueue(hass.config.path(DOMAIN, "metadata_queue.jsonl"))

    # Shared signals
    stop_event = threading.Event()
    loop_stop_event = asyncio.Event()
    backfill_request = asyncio.Event()
    live_queue = OverflowQueue(maxsize=LIVE_QUEUE_MAXSIZE)
    backfill_queue: queue.Queue = queue.Queue(maxsize=BACKFILL_QUEUE_MAXSIZE)

    # Syncer is constructed before workers because meta_worker needs a reference
    # to reuse SCD2 change-detection helpers.
    syncer = MetadataSyncer(hass=hass, meta_queue=meta_queue)

    meta_worker = TimescaledbMetaRecorderThread(
        hass=hass, dsn=dsn, meta_queue=meta_queue,
        syncer=syncer, stop_event=stop_event,
    )
    states_worker = TimescaledbStateRecorderThread(
        hass=hass, dsn=dsn,
        live_queue=live_queue,
        backfill_queue=backfill_queue,
        backfill_request=backfill_request,
        stop_event=stop_event,
        chunk_interval_days=chunk_interval_days,
        compress_after_hours=compress_after_hours,
    )

    # D-12 step 2: start meta worker — immediately drains any items persisted
    # from a prior outage. This must happen before step 4 (join) to guarantee
    # consumer progress.
    meta_worker.start()
    # D-12 step 3: start states worker — enters MODE_INIT, fires backfill_request
    # immediately, but orchestrator not yet spawned so the flag is a no-op.
    states_worker.start()
    # D-12 step 4: drain file before subscribing to new events.
    await meta_queue.join()
    # D-12 step 5: initial registry backfill — covers pre-install and
    # missed-subscription windows.
    await _async_initial_registry_backfill(hass, meta_queue)

    ingester = StateIngester(hass=hass, queue=live_queue, entity_filter=entity_filter)

    started: list[str] = []
    try:
        # D-12 step 6: subscribe to registry events (4 listeners).
        await syncer.async_start()
        started.append("syncer")
        # D-12 step 7: subscribe to state_changed events.
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

    # Spawn the overflow watcher now that workers are running; it fires the
    # D-10-b repair issue on first overflow flip until orchestrator clears it.
    overflow_watcher_task = hass.async_create_task(
        _overflow_watcher(hass, live_queue, loop_stop_event),
    )

    # D-12 step 8: defer orchestrator spawn until HA has fully started.
    orchestrator_holder: dict[str, asyncio.Task] = {}

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
            stop_event=loop_stop_event,
        ))
        orchestrator_holder["task"] = task
        data.orchestrator_task = task

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_ha_started)

    data = HaTimescaleDBData(
        states_worker=states_worker,
        meta_worker=meta_worker,
        ingester=ingester,
        syncer=syncer,
        live_queue=live_queue,
        meta_queue=meta_queue,
        backfill_queue=backfill_queue,
        backfill_request=backfill_request,
        stop_event=stop_event,
        loop_stop_event=loop_stop_event,
        orchestrator_task=None,
        overflow_watcher_task=overflow_watcher_task,
    )
    entry.runtime_data = data
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: HaTimescaleDBConfigEntry,
) -> None:
    """Restart the integration when options change (unchanged Phase 1 behaviour)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: HaTimescaleDBConfigEntry,
) -> bool:
    """Unload a config entry (D-13 six steps).

    Step 1: stop_event.set() + loop_stop_event.set()
    Step 2: cancel orchestrator task; await its cancellation
    Step 2b: cancel overflow watcher task
    Step 3: stop HA listeners (ingester + syncer) so no new events enqueue
    Step 4: wake meta worker's Condition wait (D-13-b step 3)
    Step 5: join states_worker (30s), then meta_worker (30s) — WATCH-02 safe
    Step 6: connections close inside worker run() when loop exits

    No run_coroutine_threadsafe().result() anywhere — deadlocks on HA shutdown.
    """
    data: HaTimescaleDBData = entry.runtime_data

    # D-13-b step 1: signal shutdown to worker threads + retry decorators +
    # orchestrator sleep points.
    data.stop_event.set()
    data.loop_stop_event.set()

    # D-13-b step 2: cancel orchestrator and wait for it.
    if data.orchestrator_task is not None:
        data.orchestrator_task.cancel()
        try:
            await data.orchestrator_task
        except asyncio.CancelledError:
            pass

    # Step 2b: cancel overflow watcher.
    if data.overflow_watcher_task is not None:
        data.overflow_watcher_task.cancel()
        try:
            await data.overflow_watcher_task
        except asyncio.CancelledError:
            pass

    # Step 3: stop event-loop-side producers so no new events enqueue during
    # the thread join.
    data.ingester.stop()
    await data.syncer.async_stop()

    # D-13-b step 3: wake meta worker from its Condition wait.
    data.meta_queue.wake_consumer()

    # D-13-b step 4+5: join workers with 30s timeout. If stuck, log critical
    # (Phase 3 watchdog addresses persistent stalls).
    await hass.async_add_executor_job(data.states_worker.join, 30)
    await hass.async_add_executor_job(data.meta_worker.join, 30)

    if data.states_worker.is_alive():
        _LOGGER.critical(
            "states worker did not exit within 30s after shutdown signal — "
            "thread may be stuck in a DB call. Phase 3 watchdog will address.",
        )
    if data.meta_worker.is_alive():
        _LOGGER.critical(
            "meta worker did not exit within 30s after shutdown signal.",
        )

    # D-13-b step 6: connections are closed inside each worker's run() loop
    # after its shutdown branch. No further action needed here.
    return True
