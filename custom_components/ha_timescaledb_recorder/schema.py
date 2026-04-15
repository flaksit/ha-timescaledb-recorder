"""Idempotent schema setup for the ha_timescaledb_recorder hypertable."""
import logging

import asyncpg

from .const import (
    ADD_COMPRESSION_POLICY_SQL,
    REMOVE_COMPRESSION_POLICY_SQL,
    CREATE_INDEX_SQL,
    CREATE_TABLE_SQL,
    CREATE_HYPERTABLE_SQL,
    DEFAULT_CHUNK_INTERVAL_DAYS,
    DEFAULT_COMPRESS_AFTER_HOURS,
    SET_COMPRESSION_SQL,
    CREATE_DIM_ENTITIES_SQL,
    CREATE_DIM_DEVICES_SQL,
    CREATE_DIM_AREAS_SQL,
    CREATE_DIM_LABELS_SQL,
    CREATE_DIM_ENTITIES_IDX_SQL,
    CREATE_DIM_ENTITIES_CURRENT_IDX_SQL,
    CREATE_DIM_DEVICES_IDX_SQL,
    CREATE_DIM_AREAS_IDX_SQL,
    CREATE_DIM_LABELS_IDX_SQL,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_schema(
    pool: asyncpg.Pool,
    chunk_interval_days: int = DEFAULT_CHUNK_INTERVAL_DAYS,
    compress_after_hours: int = DEFAULT_COMPRESS_AFTER_HOURS,
) -> None:
    """Create the hypertable and configure compression policy idempotently.

    The compression policy is removed and re-added on every call so that
    changes to compress_after_hours or the schedule interval take effect
    immediately without requiring manual SQL intervention.
    """
    # Policy runs at half the compression window, capped at 12 h to avoid
    # excessive polling (e.g. compress_after=2h → schedule=1h).
    schedule_hours = max(1, min(12, compress_after_hours // 2))
    async with pool.acquire() as conn:
        await conn.execute(CREATE_TABLE_SQL)
        await conn.execute(
            CREATE_HYPERTABLE_SQL.format(chunk_days=chunk_interval_days)
        )
        await conn.execute(SET_COMPRESSION_SQL)
        await conn.execute(REMOVE_COMPRESSION_POLICY_SQL)
        await conn.execute(
            ADD_COMPRESSION_POLICY_SQL.format(
                compress_hours=compress_after_hours,
                schedule_hours=schedule_hours,
            )
        )
        await conn.execute(CREATE_INDEX_SQL)

        # Dimension tables for SCD2 metadata sync (Phase 4).
        # All DDL is idempotent — safe to re-execute on every startup (D-11).
        await conn.execute(CREATE_DIM_ENTITIES_SQL)
        await conn.execute(CREATE_DIM_DEVICES_SQL)
        await conn.execute(CREATE_DIM_AREAS_SQL)
        await conn.execute(CREATE_DIM_LABELS_SQL)
        await conn.execute(CREATE_DIM_ENTITIES_IDX_SQL)
        await conn.execute(CREATE_DIM_ENTITIES_CURRENT_IDX_SQL)
        await conn.execute(CREATE_DIM_DEVICES_IDX_SQL)
        await conn.execute(CREATE_DIM_AREAS_IDX_SQL)
        await conn.execute(CREATE_DIM_LABELS_IDX_SQL)

    _LOGGER.debug(
        "Schema setup complete (chunk=%d days, compress_after=%d hours, schedule=%d hours)",
        chunk_interval_days,
        compress_after_hours,
        schedule_hours,
    )
