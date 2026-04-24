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

import voluptuous as vol

from homeassistant.components import recorder as ha_recorder
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from homeassistant.helpers.entityfilter import (
    convert_filter,
    convert_include_exclude_filter,
)
from homeassistant.helpers.reload import async_integration_yaml_config

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
from .state_listener import StateListener
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
from .registry_listener import RegistryListener, _to_json_safe
from .watchdog import spawn_meta_worker, spawn_states_worker, watchdog_loop

_LOGGER = logging.getLogger(__name__)

# Voluptuous schema for one include/exclude block inside the YAML config.
# Each key is optional — missing keys default to an empty list so
# convert_include_exclude_filter always receives well-formed dicts.
_FILTER_SCHEMA_INNER = vol.Schema({
    vol.Optional("domains", default=[]): vol.All(list, [cv.string]),
    vol.Optional("entities", default=[]): vol.All(list, [cv.string]),
    vol.Optional("entity_globs", default=[]): vol.All(list, [cv.string]),
})

# CONFIG_SCHEMA is required for HA to call async_setup before async_setup_entry.
# ALLOW_EXTRA lets other integrations' top-level keys pass through validation —
# HA validates the full configuration.yaml against every component's schema.
CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema({
            vol.Optional("include"): _FILTER_SCHEMA_INNER,
            vol.Optional("exclude"): _FILTER_SCHEMA_INNER,
        }),
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Store YAML filter config in hass.data before config entries are set up.

    HA guarantees async_setup runs before any async_setup_entry call for this
    domain, so the filter dict is always present in hass.data by the time
    _get_entity_filter reads it during entry setup.
    """
    domain_config = config.get(DOMAIN, {})
    hass.data.setdefault(DOMAIN, {})["filter"] = domain_config

    async def _async_reload_filter(call) -> None:
        # Re-parse configuration.yaml and reload all config entries so the new
        # filter takes effect without a full HA restart.
        fresh = await async_integration_yaml_config(hass, DOMAIN)
        hass.data.setdefault(DOMAIN, {})["filter"] = (fresh or {}).get(DOMAIN, {})
        for entry in hass.config_entries.async_entries(DOMAIN):
            await hass.config_entries.async_reload(entry.entry_id)

    hass.services.async_register(DOMAIN, "reload", _async_reload_filter)
    return True


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
    state_listener: StateListener | None
    registry_listener: RegistryListener
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


def _get_entity_filter(hass: HomeAssistant, entry):
    """Build entity filter from YAML config stored in hass.data.

    entry.data never carries a filter key (config-flow entries only store the
    DSN), so the old entry.data path was permanently pass-all. The YAML filter
    is now the authoritative source, stored by async_setup before this runs.
    Falls back to pass-all when no YAML filter is configured.
    """
    raw_filter = hass.data.get(DOMAIN, {}).get("filter", {})
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

    # Construct a throwaway RegistryListener bound to meta_queue so we can reuse
    # its _extract_*_params helpers without duplicating the extraction logic.
    # async_start() has NOT been called on this instance, so _entity_reg etc. are
    # None — but the extract helpers receive the registry entry directly as an
    # argument and do not read self._entity_reg.
    now = datetime.now(timezone.utc)
    listener_helper = RegistryListener(hass=hass, meta_queue=meta_queue)

    for entry in area_reg.async_list_areas():
        params = listener_helper._extract_area_params(entry, now)
        await meta_queue.put_async({
            "registry": "area",
            "action": "create",
            "registry_id": entry.id,
            "old_id": None,
            "params": _to_json_safe(params),
            "enqueued_at": now_iso,
        })

    for entry in label_reg.async_list_labels():
        params = listener_helper._extract_label_params(entry, now)
        await meta_queue.put_async({
            "registry": "label",
            "action": "create",
            "registry_id": entry.label_id,
            "old_id": None,
            "params": _to_json_safe(params),
            "enqueued_at": now_iso,
        })

    for entry in entity_reg.entities.values():
        params = listener_helper._extract_entity_params(entry, now)
        await meta_queue.put_async({
            "registry": "entity",
            "action": "create",
            "registry_id": entry.entity_id,
            "old_id": None,
            "params": _to_json_safe(params),
            "enqueued_at": now_iso,
        })

    for entry in device_reg.devices.values():
        params = listener_helper._extract_device_params(entry, now)
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
            new_task = hass.async_create_background_task(
                backfill_orchestrator(**orchestrator_kwargs),
                name="ha_timescaledb_recorder_backfill_orchestrator",
            )
            new_task.add_done_callback(_on_orchestrator_done)
            runtime.orchestrator_task = new_task

        hass.async_create_background_task(_relaunch(), name="ha_timescaledb_recorder_orchestrator_relaunch")

    return _on_orchestrator_done


async def _async_meta_init(
    hass: HomeAssistant,
    meta_queue: PersistentQueue,
    registry_listener: RegistryListener,
) -> None:
    """Drain persisted queue, run registry backfill, then enable registry_listener.

    Runs as a background task so async_setup_entry returns fast and does not
    delay homeassistant_started. Three steps in strict order:
    1. Drain the file-backed queue from the previous session (meta_worker processes
       old items first — FIFO ordering preserved).
    2. Snapshot current HA registries into meta_queue via SCD2_SNAPSHOT (idempotent,
       WHERE NOT EXISTS guard). No recorder dependency — reads er/ar/dr/lr directly.
    3. Enable registry_listener: flip DISCARD → LIVE so incoming registry events
       now flow to meta_queue normally.

    HA cancels background tasks on shutdown automatically, so no stop_event check
    is needed — CancelledError interrupts any awaited step cleanly.
    """
    await meta_queue.join()
    await _async_initial_registry_backfill(hass, meta_queue)
    registry_listener.enable()


async def async_setup_entry(
    hass: HomeAssistant, entry: HaTimescaleDBConfigEntry,
) -> bool:
    """Set up the TimescaleDB integration from a config entry."""
    dsn = entry.data[CONF_DSN]
    options = entry.options
    chunk_interval_days = options.get(CONF_CHUNK_INTERVAL, DEFAULT_CHUNK_INTERVAL_DAYS)
    compress_after_hours = options.get(CONF_COMPRESS_AFTER, DEFAULT_COMPRESS_AFTER_HOURS)
    entity_filter = _get_entity_filter(hass, entry)

    # D-12 step 1: open PersistentQueue
    meta_queue = PersistentQueue(hass.config.path(DOMAIN, "metadata_queue.jsonl"))

    # Shared signals
    stop_event = threading.Event()
    loop_stop_event = asyncio.Event()
    backfill_request = asyncio.Event()
    live_queue = OverflowQueue(maxsize=LIVE_QUEUE_MAXSIZE)
    backfill_queue: queue.Queue = queue.Queue(maxsize=BACKFILL_QUEUE_MAXSIZE)

    # RegistryListener constructed before workers — meta_worker holds a reference
    # for SCD2 change-detection helpers. StateListener needs live_queue.
    registry_listener = RegistryListener(hass=hass, meta_queue=meta_queue)
    state_listener = StateListener(hass=hass, queue=live_queue, entity_filter=entity_filter)

    # Build data first so spawn factories can read dsn, queue fields, etc.
    # Enables a single code path for initial spawn and watchdog respawn (D-05-c).
    data = HaTimescaleDBData(
        states_worker=None,         # filled by spawn_states_worker below
        meta_worker=None,           # filled by spawn_meta_worker below
        state_listener=state_listener,
        registry_listener=registry_listener,
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

    started: list[str] = []
    try:
        # D-12 step 4: subscribe registry_listener in DISCARD mode — events dropped
        # until _async_meta_init calls enable() after drain + backfill complete.
        await registry_listener.async_start()
        started.append("registry_listener")
        # D-12 step 5: subscribe state_listener — live_queue buffers all events
        # from this point; no states are lost regardless of init timing.
        state_listener.async_start()
        started.append("state_listener")
    except Exception:
        _LOGGER.exception("Setup failed after starting %s; rolling back", started)
        if "state_listener" in started:
            state_listener.stop()
        if "registry_listener" in started:
            await registry_listener.async_stop()
        stop_event.set()
        meta_queue.wake_consumer()
        await hass.async_add_executor_job(data.meta_worker.join, 30)
        await hass.async_add_executor_job(data.states_worker.join, 30)
        raise

    # D-12 steps 6+7: drain persisted queue + registry backfill + enable listeners.
    # Background task does not block homeassistant_started (HA: "Will not block startup").
    hass.async_create_background_task(
        _async_meta_init(hass, meta_queue, registry_listener),
        name="ha_timescaledb_recorder_meta_init",
    )

    # D-11: recorder_disabled detection — one-shot check at setup. Backfill
    # requires HA's sqlite recorder; if it's not loaded or disabled, raise a
    # repair issue. The auto-clear background task waits for the recorder to
    # become available and clears the issue automatically (OBS-03 / HIGH-1).
    try:
        _recorder_check = ha_recorder.get_instance(hass)
        if not _recorder_check.enabled:
            create_recorder_disabled_issue(hass)
            hass.async_create_background_task(_wait_for_recorder_and_clear(hass), name="ha_timescaledb_recorder_wait_recorder")
    except KeyError:
        create_recorder_disabled_issue(hass)
        hass.async_create_background_task(_wait_for_recorder_and_clear(hass), name="ha_timescaledb_recorder_wait_recorder")

    # Spawn the overflow watcher now that workers are running; it fires the
    # D-10-b repair issue on first overflow flip until orchestrator clears it.
    # async_create_background_task: tasks not tracked by HA bootstrap/shutdown
    # stages, so long-running coroutines don't cause "Setup timed out for bootstrap"
    # warnings or delay HA shutdown beyond the unload cancellation we do explicitly.
    data.overflow_watcher_task = hass.async_create_background_task(
        _overflow_watcher(hass, live_queue, loop_stop_event),
        name="ha_timescaledb_recorder_overflow_watcher",
    )

    # D-05-a: watchdog task supervising both worker threads. Must be spawned
    # AFTER workers exist on data (watchdog_loop reads runtime.states_worker etc.).
    # Cancelled first in async_unload_entry before orchestrator teardown.
    data.watchdog_task = hass.async_create_background_task(
        watchdog_loop(hass, data),
        name="ha_timescaledb_recorder_watchdog",
    )

    # D-12 step 8: defer orchestrator spawn until HA has fully started.
    @callback
    def _on_ha_started(_event):
        # Snapshot all current HA states into backfill_queue before the orchestrator
        # runs. This catches startup states that fired before state_listener was
        # active (zone.home, sun.sun, person.*, conversation.* etc. are initialized
        # by core components that load before custom config entries). Reading from
        # hass.states avoids the SQLite commit-lag race that affects get_significant_states
        # at startup. ON CONFLICT DO NOTHING on INSERT makes this idempotent — any
        # states already in ha_states are silently skipped.
        # The worker is guaranteed to be in MODE_BACKFILL here (it entered that mode
        # immediately on startup before HOMEASSISTANT_STARTED), so backfill_queue
        # items will be processed before the transition to MODE_LIVE.
        snapshot: dict[str, list] = {
            state.entity_id: [state]
            for state in hass.states.async_all()
            if entity_filter(state.entity_id)
        }
        if snapshot:
            try:
                backfill_queue.put_nowait(snapshot)
            except queue.Full:
                _LOGGER.warning(
                    "backfill_queue full at startup — startup snapshot dropped; "
                    "run backfill_gaps.py manually to fill any resulting gaps"
                )

        orchestrator_kwargs = dict(
            hass=hass,
            live_queue=live_queue,
            backfill_queue=backfill_queue,
            backfill_request=backfill_request,
            read_watermark=data.states_worker.read_watermark,
            all_entities_reader=data.states_worker.read_all_known_entities,
            entity_filter=entity_filter,
            stop_event=loop_stop_event,
            threading_stop_event=stop_event,  # Plan 05 kwarg
        )
        task = hass.async_create_background_task(
            backfill_orchestrator(**orchestrator_kwargs),
            name="ha_timescaledb_recorder_backfill_orchestrator",
        )
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
    if data.state_listener is not None:
        data.state_listener.stop()
    await data.registry_listener.async_stop()

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
