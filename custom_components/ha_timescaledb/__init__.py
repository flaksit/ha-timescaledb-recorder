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
    CONF_COMPRESS_AFTER,
    CONF_DSN,
    CONF_FLUSH_INTERVAL,
    CONF_INGEST_UNAVAILABLE,
    DEFAULT_BATCH_SIZE,
    DEFAULT_COMPRESS_AFTER_DAYS,
    DEFAULT_CHUNK_INTERVAL_DAYS,
    DEFAULT_FLUSH_INTERVAL,
)
from .ingester import StateIngester
from .schema import async_setup_schema

_LOGGER = logging.getLogger(__name__)


@dataclass
class HaTimescaleDBData:
    """Runtime data stored on a config entry."""

    ingester: StateIngester
    pool: asyncpg.Pool


HaTimescaleDBConfigEntry = ConfigEntry[HaTimescaleDBData]


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

    compress_after_days = options.get(CONF_COMPRESS_AFTER, DEFAULT_COMPRESS_AFTER_DAYS)
    await async_setup_schema(
        pool,
        chunk_interval_days=DEFAULT_CHUNK_INTERVAL_DAYS,
        compress_after_days=compress_after_days,
    )

    raw_filter = entry.data.get("filter", {})
    entity_filter = convert_include_exclude_filter(raw_filter) if raw_filter else convert_filter({
        "include_domains": [],
        "include_entity_globs": [],
        "include_entities": [],
        "exclude_domains": [],
        "exclude_entity_globs": [],
        "exclude_entities": [],
    })

    batch_size = options.get(CONF_BATCH_SIZE, DEFAULT_BATCH_SIZE)
    flush_interval = options.get(CONF_FLUSH_INTERVAL, DEFAULT_FLUSH_INTERVAL)
    ingest_unavailable = options.get(CONF_INGEST_UNAVAILABLE, False)

    ingester = StateIngester(
        hass=hass,
        pool=pool,
        entity_filter=entity_filter,
        batch_size=batch_size,
        flush_interval=flush_interval,
        ingest_unavailable=ingest_unavailable,
    )
    ingester.async_start()

    entry.runtime_data = HaTimescaleDBData(ingester=ingester, pool=pool)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HaTimescaleDBConfigEntry) -> bool:
    """Unload a config entry — stop ingester, close pool."""
    data: HaTimescaleDBData = entry.runtime_data
    await data.ingester.async_stop()
    await data.pool.close()
    return True
