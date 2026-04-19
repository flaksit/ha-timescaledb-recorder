"""TimescaleDB Recorder custom component for Home Assistant."""
from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entityfilter import (
    convert_filter,
    convert_include_exclude_filter,
)

from .const import (
    CONF_CHUNK_INTERVAL,
    CONF_COMPRESS_AFTER,
    CONF_DSN,
    DEFAULT_COMPRESS_AFTER_HOURS,
    DEFAULT_CHUNK_INTERVAL_DAYS,
)
from .ingester import StateIngester
from .syncer import MetadataSyncer
from .worker import DbWorker

_LOGGER = logging.getLogger(__name__)


@dataclass
class HaTimescaleDBData:
    """Runtime data stored on a config entry."""

    worker: DbWorker
    ingester: StateIngester
    syncer: MetadataSyncer


HaTimescaleDBConfigEntry = ConfigEntry[HaTimescaleDBData]


def _get_entity_filter(entry):
    """Build entity filter from config entry data."""
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


async def async_setup_entry(hass: HomeAssistant, entry: HaTimescaleDBConfigEntry) -> bool:
    """Set up the TimescaleDB integration from a config entry.

    D-01: no DB calls here — returns True immediately so HA startup is not blocked.
    Worker thread starts and handles all DB operations asynchronously.

    Rollback: if any component start raises after worker.start(), all started components
    are stopped in reverse order before re-raising. This prevents orphaned background
    threads if setup partially fails (e.g., registry enumeration raises in syncer.async_start).
    """
    dsn = entry.data[CONF_DSN]
    options = entry.options
    chunk_interval_days = options.get(CONF_CHUNK_INTERVAL, DEFAULT_CHUNK_INTERVAL_DAYS)
    compress_after_hours = options.get(CONF_COMPRESS_AFTER, DEFAULT_COMPRESS_AFTER_HOURS)
    entity_filter = _get_entity_filter(entry)

    # Construct syncer first with no queue — bind_queue() called after worker creates it.
    # Worker needs syncer for SCD2 change-detection; syncer needs worker.queue for enqueuing.
    syncer = MetadataSyncer(hass=hass)
    worker = DbWorker(
        hass=hass,
        dsn=dsn,
        chunk_interval_days=chunk_interval_days,
        compress_after_hours=compress_after_hours,
        syncer=syncer,
    )
    # Wire the real queue via public API (avoids direct _queue attribute mutation).
    syncer.bind_queue(worker.queue)

    ingester = StateIngester(
        hass=hass,
        queue=worker.queue,
        entity_filter=entity_filter,
    )

    # Start components in dependency order with rollback on failure.
    worker.start()  # starts daemon thread; returns immediately (D-01)
    try:
        ingester.async_start()  # registers STATE_CHANGED listener (sync)
        try:
            await syncer.async_start()  # enqueues registry snapshots + registers 4 listeners
        except Exception:
            # syncer.async_start() failed — stop ingester and worker before re-raising.
            _LOGGER.exception("syncer.async_start() failed; rolling back setup")
            ingester.stop()
            await worker.async_stop()
            raise
    except Exception:
        # ingester.async_start() failed (or re-raised from syncer block) — stop worker.
        # Note: ingester.stop() is safe to call even if async_start() never completed
        # (it guards on self._cancel_listener is not None).
        await worker.async_stop()
        raise

    entry.runtime_data = HaTimescaleDBData(worker=worker, ingester=ingester, syncer=syncer)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: HaTimescaleDBConfigEntry) -> None:
    """Restart the integration when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: HaTimescaleDBConfigEntry) -> bool:
    """Unload a config entry.

    Stop event listeners first so no new items are enqueued during shutdown,
    then stop the worker (puts _STOP sentinel, awaits thread.join(timeout=30) via executor job).
    WATCH-02: never call run_coroutine_threadsafe().result() here — deadlocks on shutdown.
    """
    data: HaTimescaleDBData = entry.runtime_data
    data.ingester.stop()            # sync: cancels STATE_CHANGED listener (not async_stop)
    await data.syncer.async_stop()  # cancels 4 registry listeners
    await data.worker.async_stop()  # puts _STOP sentinel, awaits thread.join(timeout=30)
    return True
