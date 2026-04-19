"""Idempotent schema setup for the ha_timescaledb_recorder hypertable."""
import logging

import psycopg

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


def sync_setup_schema(
    conn: psycopg.Connection,
    chunk_interval_days: int = DEFAULT_CHUNK_INTERVAL_DAYS,
    compress_after_hours: int = DEFAULT_COMPRESS_AFTER_HOURS,
) -> None:
    """Create the hypertable and configure compression policy idempotently.

    Called by DbWorker at thread startup using the already-open psycopg3 connection.
    The compression policy is removed and re-added on every call so that changes to
    compress_after_hours take effect without manual SQL intervention.

    Does NOT catch exceptions — callers (DbWorker._setup_schema) handle
    psycopg.OperationalError for the DB-unreachable startup case (D-03).
    """
    # Policy runs at half the compression window, capped at 12 h to avoid
    # excessive polling (e.g. compress_after=2h → schedule=1h).
    schedule_hours = max(1, min(12, compress_after_hours // 2))
    with conn.cursor() as cur:
        cur.execute(CREATE_TABLE_SQL)
        cur.execute(
            CREATE_HYPERTABLE_SQL.format(chunk_days=chunk_interval_days)
        )
        cur.execute(SET_COMPRESSION_SQL)
        cur.execute(REMOVE_COMPRESSION_POLICY_SQL)
        cur.execute(
            ADD_COMPRESSION_POLICY_SQL.format(
                compress_hours=compress_after_hours,
                schedule_hours=schedule_hours,
            )
        )
        cur.execute(CREATE_INDEX_SQL)

        # Dimension tables for SCD2 metadata sync (Phase 4).
        # All DDL is idempotent — safe to re-execute on every startup (D-11).
        cur.execute(CREATE_DIM_ENTITIES_SQL)
        cur.execute(CREATE_DIM_DEVICES_SQL)
        cur.execute(CREATE_DIM_AREAS_SQL)
        cur.execute(CREATE_DIM_LABELS_SQL)
        cur.execute(CREATE_DIM_ENTITIES_IDX_SQL)
        cur.execute(CREATE_DIM_ENTITIES_CURRENT_IDX_SQL)
        cur.execute(CREATE_DIM_DEVICES_IDX_SQL)
        cur.execute(CREATE_DIM_AREAS_IDX_SQL)
        cur.execute(CREATE_DIM_LABELS_IDX_SQL)

    _LOGGER.debug(
        "Schema setup complete (chunk=%d days, compress_after=%d hours, schedule=%d hours)",
        chunk_interval_days,
        compress_after_hours,
        schedule_hours,
    )
