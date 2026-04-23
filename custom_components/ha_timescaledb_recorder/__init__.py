"""TimescaleDB Recorder custom component for Home Assistant.

Phase 2 topology: one thread for states, one thread for metadata; a bounded
in-memory queue for live events; a file-persisted queue for metadata; an
event-loop orchestrator driving backfill from HA sqlite on demand.

Phase 3 additions:
- HaTimescaleDBData gains watchdog_task, dsn, chunk_interval_days,
  compress_after_hours, entity_filter fields required by spawn factories.
- Workers constructed via spawn_states_worker / spawn_meta_worker factories
  so initial spawn and watchdog respawn share one code path (D-05-c).
- watchdog_loop task spawned after workers are running (D-05-a).
- Orchestrator task gets add_done_callback for crash recovery with 5s
  throttled relaunch to prevent notification storm (D-04, MEDIUM-6).
- recorder_disabled one-shot check at setup + auto-clear background task
  that waits for recorder availability (D-11, OBS-03, HIGH-1).
- async_unload_entry cancels and awaits watchdog_task BEFORE orchestrator
  to prevent respawn race during shutdown (MEDIUM-12).
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from homeassistant.components import recorder as ha_recorder
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
from .issues import (
    clear_recorder_disabled_issue,
    create_buffer_dropping_issue,
    create_recorder_disabled_issue,
)
from .meta_worker import TimescaledbMetaRecorderThread
from .notifications import notify_watchdog_recovery
from .overflow_queue import OverflowQueue
from .persistent_queue import PersistentQueue
from .states_worker import TimescaledbStateRecorderThread
from .syncer import MetadataSyncer, _to_json_safe
from .watchdog import spawn_meta_worker, spawn_states_worker, watchdog_loop

_LOGGER = logging.getLogger(__name__)

# Sleep interval for the overflow watcher task. Small enough to notice the
# flag flip quickly, large enough to be effectively free. The watcher only
# exists to surface the D-10-b repair issue; orchestrator does the actual
# reactive work via backfill_request.
_OVERFLOW_WATCH_INTERVAL_S = 0.5


@dataclass
class HaTimescaleDBData:
    """Runtime data stored on a config entry."""

    states_worker: TimescaledbStateRecorderThread | None
    meta_worker: TimescaledbMetaRecorderThread | None
    ingester: StateIngester | None
    syncer: MetadataSyncer
    live_queue: OverflowQueue
    meta_queue: PersistentQueue
    backfill_queue: queue.Queue
    backfill_request: asyncio.Event
    stop_event: threading.Event
    loop_stop_event: asyncio.Event
    orchestrator_task: asyncio.Task | None
    overflow_watcher_task: asyncio.Task | None
    watchdog_task: asyncio.Task | None  # D-05-a: spawned in setup, cancelled first in unload
    # Spawn-factory inputs — set during async_setup_entry; used by
    # watchdog.spawn_states_worker / spawn_meta_worker to reconstruct
    # workers with the same args after a crash (D-05-c).
    dsn: str                                # DSN string for psycopg3 connection
    chunk_interval_days: int                # TimescaleDB chunk interval
    compress_after_hours: int               # TimescaleDB compression policy
    entity_filter: Callable[[str], bool]    # entity filter for orchestrator relaunch kwargs


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


async def _wait_for_recorder_and_clear(hass: HomeAssistant) -> None:
    """Wait for the HA recorder to become available and clear recorder_disabled issue.

    Spawned as a background task when create_recorder_disabled_issue fires at setup
    time. Uses recorder.async_wait_recorder (HA 2023+, available in HA 2026.3.4) to
    block until the recorder is ready, then clears the repair issue if enabled.

    Falls back to a logged no-op if async_wait_recorder is unavailable in future HA
    versions. The hasattr check prevents AttributeError on unexpected version skew.

    (Cross-AI review 2026-04-23, concern HIGH-1 — OBS-03 auto-clear requirement.)
    """
    try:
        if not hasattr(ha_recorder, "async_wait_recorder"):
            _LOGGER.debug(
                "ha_recorder.async_wait_recorder not available — "
                "recorder_disabled issue will require manual dismissal"
            )
            return
        recorder_instance = await ha_recorder.async_wait_recorder(hass)
        if recorder_instance is not None and recorder_instance.enabled:
            clear_recorder_disabled_issue(hass)
            _LOGGER.info(
                "HA recorder became available — clearing recorder_disabled repair issue"
            )
        else:
            _LOGGER.debug(
                "async_wait_recorder returned %r — recorder_disabled issue stays raised",
                recorder_instance,
            )
    except Exception:  # noqa: BLE001
        _LOGGER.debug(
            "recorder wait task raised — recorder_disabled issue will stay raised",
            exc_info=True,
        )


def _make_orchestrator_done_callback(
    hass: HomeAssistant,
    runtime: HaTimescaleDBData,
    orchestrator_kwargs: dict,
):
    """Return the done-callback closure for the orchestrator task.

    D-04: on unhandled exception, fires notify_watchdog_recovery and schedules
    a _relaunch() coroutine that sleeps 5s before relaunching the orchestrator.
    The 5s delay prevents notification storms when the orchestrator crashes
    in a tight loop. (Cross-AI review 2026-04-23, concern MEDIUM-6.)

    Order matters: task.cancelled() must be checked BEFORE task.exception()
    because calling exception() on a cancelled task raises CancelledError
    (RESEARCH Pitfall 1).
    """
    def _on_orchestrator_done(task: asyncio.Task) -> None:
        # Cancelled task — normal shutdown or explicit cancel; nothing to do.
        if task.cancelled():
            return
        # Integration is shutting down — do not attempt restart.
        if runtime.stop_event.is_set() or runtime.loop_stop_event.is_set():
            return
        exc = task.exception()
        if exc is None:
            return  # clean exit — defensive guard

        notify_watchdog_recovery(
            hass, component="orchestrator", exc=exc,
            context={"at": datetime.now(timezone.utc).isoformat()},
        )

        async def _relaunch() -> None:
            """Throttled orchestrator relaunch — avoids notification storm (MEDIUM-6)."""
            await asyncio.sleep(5)
            # Re-check shutdown state after sleeping — may have changed.
            if runtime.stop_event.is_set() or runtime.loop_stop_event.is_set():
                return
            new_task = hass.async_create_task(
                backfill_orchestrator(**orchestrator_kwargs),
            )
            new_task.add_done_callback(_on_orchestrator_done)
            runtime.orchestrator_task = new_task

        hass.async_create_task(_relaunch())

    return _on_orchestrator_done


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

    # Build the data object first (workers=None) so spawn factories can receive
    # it as their runtime argument — factories read dsn, queue fields, etc.
    # This enables a single code path for initial spawn and watchdog respawn (D-05-c).
    data = HaTimescaleDBData(
        states_worker=None,     # filled by spawn_states_worker below
        meta_worker=None,       # filled by spawn_meta_worker below
        ingester=None,          # filled after startup steps 6-7
        syncer=syncer,
        live_queue=live_queue,
        meta_queue=meta_queue,
        backfill_queue=backfill_queue,
        backfill_request=backfill_request,
        stop_event=stop_event,
        loop_stop_event=loop_stop_event,
        orchestrator_task=None,
        overflow_watcher_task=None,
        watchdog_task=None,
        dsn=dsn,
        chunk_interval_days=chunk_interval_days,
        compress_after_hours=compress_after_hours,
        entity_filter=entity_filter,
    )

    # D-12 step 2: start meta worker via factory — same path watchdog uses for respawn.
    data.meta_worker = spawn_meta_worker(hass, data)
    data.meta_worker.start()

    # D-12 step 3: start states worker via factory.
    data.states_worker = spawn_states_worker(hass, data)
    data.states_worker.start()

    # D-12 step 4: drain file before subscribing to new events.
    await meta_queue.join()
    # D-12 step 5: initial registry backfill — covers pre-install and
    # missed-subscription windows.
    await _async_initial_registry_backfill(hass, meta_queue)

    ingester = StateIngester(hass=hass, queue=live_queue, entity_filter=entity_filter)
    data.ingester = ingester

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
        await hass.async_add_executor_job(data.meta_worker.join, 30)
        await hass.async_add_executor_job(data.states_worker.join, 30)
        raise

    # D-11: recorder_disabled detection — one-shot check at setup. Backfill
    # requires HA's sqlite recorder; if it's not loaded or disabled, raise a
    # repair issue. The auto-clear background task waits for the recorder to
    # become available and clears the issue automatically (OBS-03 / HIGH-1).
    try:
        _recorder_check = ha_recorder.get_instance(hass)
        if not _recorder_check.enabled:
            create_recorder_disabled_issue(hass)
            hass.async_create_task(_wait_for_recorder_and_clear(hass))
    except KeyError:
        create_recorder_disabled_issue(hass)
        hass.async_create_task(_wait_for_recorder_and_clear(hass))

    # Spawn the overflow watcher now that workers are running; it fires the
    # D-10-b repair issue on first overflow flip until orchestrator clears it.
    data.overflow_watcher_task = hass.async_create_task(
        _overflow_watcher(hass, live_queue, loop_stop_event),
    )

    # D-05-a: watchdog task supervising both worker threads. Must be spawned
    # AFTER workers exist on data (watchdog_loop reads runtime.states_worker etc.).
    # Cancelled first in async_unload_entry before orchestrator teardown.
    data.watchdog_task = hass.async_create_task(watchdog_loop(hass, data))

    # D-12 step 8: defer orchestrator spawn until HA has fully started.
    @callback
    def _on_ha_started(_event):
        orchestrator_kwargs = dict(
            hass=hass,
            live_queue=live_queue,
            backfill_queue=backfill_queue,
            backfill_request=backfill_request,
            read_watermark=data.states_worker.read_watermark,
            open_entities_reader=data.states_worker.read_open_entities,
            entity_filter=entity_filter,
            stop_event=loop_stop_event,
            threading_stop_event=stop_event,  # Plan 05 kwarg
        )
        task = hass.async_create_task(backfill_orchestrator(**orchestrator_kwargs))
        task.add_done_callback(
            _make_orchestrator_done_callback(hass, data, orchestrator_kwargs),
        )
        data.orchestrator_task = task

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_ha_started)

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
    """Unload a config entry (D-13 six steps, Phase 3: watchdog cancel added).

    Step 1: stop_event.set() + loop_stop_event.set()
    Step 1b (Phase 3): cancel watchdog_task + await it — prevents respawn race
      during teardown. Must complete before orchestrator cancel so the watchdog
      cannot attempt a restart after workers are torn down. (MEDIUM-12.)
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

    # Phase 3 step 1b: cancel watchdog FIRST and await it before touching any
    # other async tasks. This prevents a mid-teardown respawn race where the
    # watchdog detects a dead worker and attempts to restart it after stop_event
    # has been set but before the worker join. (Cross-AI review MEDIUM-12.)
    if data.watchdog_task is not None:
        data.watchdog_task.cancel()
        try:
            await data.watchdog_task
        except asyncio.CancelledError:
            pass

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
    # the thread join. Guard against None in case setup failed before these
    # were assigned.
    if data.ingester is not None:
        data.ingester.stop()
    await data.syncer.async_stop()

    # D-13-b step 3: wake meta worker from its Condition wait.
    data.meta_queue.wake_consumer()

    # D-13-b step 4+5: join workers with 30s timeout. If stuck, log critical
    # (Phase 3 watchdog addresses persistent stalls).
    if data.states_worker is not None:
        await hass.async_add_executor_job(data.states_worker.join, 30)
    if data.meta_worker is not None:
        await hass.async_add_executor_job(data.meta_worker.join, 30)

    if data.states_worker is not None and data.states_worker.is_alive():
        _LOGGER.critical(
            "states worker did not exit within 30s after shutdown signal — "
            "thread may be stuck in a DB call. Phase 3 watchdog will address.",
        )
    if data.meta_worker is not None and data.meta_worker.is_alive():
        _LOGGER.critical(
            "meta worker did not exit within 30s after shutdown signal.",
        )

    # D-13-b step 6: connections are closed inside each worker's run() loop
    # after its shutdown branch. No further action needed here.
    return True
