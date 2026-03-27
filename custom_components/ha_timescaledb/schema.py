"""Idempotent schema setup for the ha_timescaledb hypertable."""
import logging

import asyncpg

from .const import (
    ADD_COMPRESSION_POLICY_SQL,
    CREATE_INDEX_SQL,
    CREATE_TABLE_SQL,
    CREATE_HYPERTABLE_SQL,
    DEFAULT_CHUNK_INTERVAL_DAYS,
    DEFAULT_COMPRESS_AFTER_DAYS,
    SET_COMPRESSION_SQL,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_schema(
    pool: asyncpg.Pool,
    chunk_interval_days: int = DEFAULT_CHUNK_INTERVAL_DAYS,
    compress_after_days: int = DEFAULT_COMPRESS_AFTER_DAYS,
) -> None:
    """Create the hypertable and configure compression policy idempotently."""
    async with pool.acquire() as conn:
        await conn.execute(CREATE_TABLE_SQL)
        await conn.execute(
            CREATE_HYPERTABLE_SQL.format(chunk_days=chunk_interval_days)
        )
        await conn.execute(SET_COMPRESSION_SQL)
        await conn.execute(
            ADD_COMPRESSION_POLICY_SQL.format(compress_days=compress_after_days)
        )
        await conn.execute(CREATE_INDEX_SQL)

    _LOGGER.debug(
        "Schema setup complete (chunk=%d days, compress_after=%d days)",
        chunk_interval_days,
        compress_after_days,
    )
