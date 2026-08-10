"""Idempotent schema setup for the timescaledb_recorder hypertable."""
import logging

import psycopg

from .const import (
    ADD_COMPRESSION_POLICY_SQL,
    REMOVE_COMPRESSION_POLICY_SQL,
    CREATE_INDEX_SQL,
    CREATE_UNIQUE_INDEX_SQL,
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
    CREATE_VIEW_STATES_NUMERIC_SQL,
    CREATE_VIEW_STATES_FLAT_SQL,
    METADATA_DEADLETTER_DDL_SQL,
    METADATA_DEADLETTER_IDX_SQL,
    SCD2_BTREE_GIST_SQL,
    SCD2_DDL_LOCK_TIMEOUT_RESET_SQL,
    SCD2_DDL_LOCK_TIMEOUT_SQL,
    SCD2_DIMENSIONS,
    SCD2_EXCLUDE_CONSTRAINT_SQL,
    SCD2_OPEN_UNIQUE_IDX_SQL,
)

_LOGGER = logging.getLogger(__name__)

_REPAIR_HINT = (
    "Run the repair script to reconstruct the affected history: "
    "docker exec homeassistant python3 -m "
    "custom_components.timescaledb_recorder.repair_scd2 --dsn <dsn> --dry-run"
)


def setup_scd2_constraints(conn: psycopg.Connection) -> None:
    """Enforce the SCD2 invariant at the database level, best-effort.

    The invariant — no overlapping version intervals per id, at most one open
    version — had nothing enforcing it, which is how issue #17 stayed invisible:
    a duplicated open row silently doubled every fact row joined through it while
    row counts still looked plausible.

    Every statement is attempted independently and no failure propagates. On a
    fresh install the tables are empty and all of this succeeds, so new
    deployments get the guarantee immediately. On an install that is already
    damaged the ALTER fails on the existing overlaps; that is expected, and it
    must not take down schema setup or block states ingestion, so it is logged
    with a pointer to the repair script and skipped. Once the repair has run, the
    next startup adds the constraint and later startups find it already present.

    Requires autocommit (the caller's connection is), so a rejected statement
    does not abort the ones after it.

    Bounded by lock_timeout. ADD CONSTRAINT needs ACCESS EXCLUSIVE, so it queues
    behind any open reader — a Grafana query on states_flat holds ACCESS SHARE on
    entities — and while it waits, every later access to that table queues behind
    it. Without a timeout, one slow dashboard query would stall schema setup,
    states ingestion and metadata writes for as long as it ran. Giving up is the
    right answer: this is best-effort DDL and the next startup retries it.
    """
    have_gist = True
    try:
        with conn.cursor() as cur:
            cur.execute(SCD2_BTREE_GIST_SQL)
    except psycopg.Error as exc:
        # Needs CREATE on the database. Without it only the weaker guarantee is
        # available: a unique index catches duplicate open rows but cannot see
        # overlaps between closed versions.
        have_gist = False
        _LOGGER.warning(
            "Could not install btree_gist (%s); falling back to a unique index on open "
            "rows. Overlapping closed versions will no longer be detected automatically.",
            exc,
        )

    statements = SCD2_EXCLUDE_CONSTRAINT_SQL if have_gist else SCD2_OPEN_UNIQUE_IDX_SQL
    try:
        with conn.cursor() as cur:
            cur.execute(SCD2_DDL_LOCK_TIMEOUT_SQL)
        for table, _key in SCD2_DIMENSIONS:
            try:
                with conn.cursor() as cur:
                    cur.execute(statements[table])
            except psycopg.errors.LockNotAvailable:
                # Someone else is holding the table. Not a data problem, so the
                # repair hint would be misleading — say what actually happened.
                _LOGGER.warning(
                    "Could not enforce the SCD2 invariant on %s: another session held the "
                    "table longer than the lock timeout. Schema setup continues; the next "
                    "startup will retry.",
                    table,
                )
            except psycopg.Error as exc:
                _LOGGER.warning(
                    "Could not enforce the SCD2 invariant on %s (%s). This normally means the "
                    "table still holds overlapping or duplicate-open versions. %s",
                    table, exc, _REPAIR_HINT,
                )
    finally:
        # The connection is long-lived and shared with the states worker, so the
        # timeout must not outlive this function.
        try:
            with conn.cursor() as cur:
                cur.execute(SCD2_DDL_LOCK_TIMEOUT_RESET_SQL)
        except psycopg.Error:
            _LOGGER.debug("Could not reset lock_timeout after SCD2 DDL", exc_info=True)


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
        cur.execute(CREATE_UNIQUE_INDEX_SQL)    # D-09-a: Phase 2 — enables ON CONFLICT DO NOTHING dedup

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

        # Where the meta worker records an item it gives up on. Created here,
        # not by the worker, so the drop path can assume it exists — a worker
        # that has to CREATE TABLE while handling a failure has two ways to
        # lose the item instead of one.
        cur.execute(METADATA_DEADLETTER_DDL_SQL)
        cur.execute(METADATA_DEADLETTER_IDX_SQL)

        # Convenience views — must follow the dimension tables, since
        # states_flat joins them. CREATE OR REPLACE (not IF NOT EXISTS)
        # so a definition change ships with an integration update rather
        # than needing manual DDL on every deployment.
        #
        # Caveat: CREATE OR REPLACE VIEW can only *append* columns. Renaming,
        # reordering, or retyping an existing column raises "cannot change name
        # of view column" — such a change needs an explicit DROP VIEW here.
        cur.execute(CREATE_VIEW_STATES_NUMERIC_SQL)
        cur.execute(CREATE_VIEW_STATES_FLAT_SQL)

    # Outside the cursor block above: each constraint is attempted on its own
    # cursor so one rejection cannot disturb the others (issue #17).
    setup_scd2_constraints(conn)

    _LOGGER.debug(
        "Schema setup complete (chunk=%d days, compress_after=%d hours, schedule=%d hours)",
        chunk_interval_days,
        compress_after_hours,
        schedule_hours,
    )
