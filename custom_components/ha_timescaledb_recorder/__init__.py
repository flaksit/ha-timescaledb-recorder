"""TimescaleDB Recorder custom component for Home Assistant."""
from dataclasses import dataclass
import logging

import asyncpg

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entityfilter import (
    convert_filter,
    convert_include_exclude_filter,
)

from .const import (
    CONF_BATCH_SIZE,
    CONF_CHUNK_INTERVAL,
    CONF_COMPRESS_AFTER,
    CONF_DSN,
    CONF_FLUSH_INTERVAL,
    DEFAULT_BATCH_SIZE,
    DEFAULT_COMPRESS_AFTER_HOURS,
    DEFAULT_CHUNK_INTERVAL_DAYS,
    DEFAULT_FLUSH_INTERVAL,
)
from .ingester import StateIngester
from .schema import async_setup_schema
from .syncer import MetadataSyncer

_LOGGER = logging.getLogger(__name__)


@dataclass
class HaTimescaleDBData:
    """Runtime data stored on a config entry."""

    ingester: StateIngester
    syncer: MetadataSyncer
    pool: asyncpg.Pool


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
    """Set up the TimescaleDB integration from a config entry."""
    dsn = entry.data[CONF_DSN]
    options = entry.options

    pool = await asyncpg.create_pool(
        dsn,
        min_size=1,
        max_size=3,
        max_inactive_connection_lifetime=300.0,
        max_queries=50_000,
        command_timeout=30.0,
        ssl=False,
    )

    chunk_interval_days = options.get(CONF_CHUNK_INTERVAL, DEFAULT_CHUNK_INTERVAL_DAYS)
    compress_after_hours = options.get(CONF_COMPRESS_AFTER, DEFAULT_COMPRESS_AFTER_HOURS)
    await async_setup_schema(
        pool,
        chunk_interval_days=chunk_interval_days,
        compress_after_hours=compress_after_hours,
    )

    entity_filter = _get_entity_filter(entry)
    batch_size = options.get(CONF_BATCH_SIZE, DEFAULT_BATCH_SIZE)
    flush_interval = options.get(CONF_FLUSH_INTERVAL, DEFAULT_FLUSH_INTERVAL)

    ingester = StateIngester(
        hass=hass,
        pool=pool,
        entity_filter=entity_filter,
        batch_size=batch_size,
        flush_interval=flush_interval,
    )
    ingester.async_start()

    syncer = MetadataSyncer(hass=hass, pool=pool)
    await syncer.async_start()

    entry.runtime_data = HaTimescaleDBData(ingester=ingester, syncer=syncer, pool=pool)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: HaTimescaleDBConfigEntry) -> None:
    """Restart the ingester when options change (applies new values immediately)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: HaTimescaleDBConfigEntry) -> bool:
    """Unload a config entry — stop ingester, close pool."""
    data: HaTimescaleDBData = entry.runtime_data
    await data.ingester.async_stop()
    await data.syncer.async_stop()
    await data.pool.close()
    return True
