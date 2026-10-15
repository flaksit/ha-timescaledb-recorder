"""One-time data migrations of the dimension tables, gated by `schema_migrations`."""
import psycopg
from psycopg.types.json import Jsonb

from .canonical import canonical_labels, canonical_legacy
from .const import (
    CANONICAL_SETS_MIGRATION_ID,
    CANONICALIZE_SELECT_SQL,
    CANONICALIZE_UPDATE_SQL,
    SCD2_DIMENSIONS,
    SCD2_REPAIR_LOCK_SQL,
    SCD2_REPAIR_LOCK_TIMEOUT_SQL,
    SCHEMA_MIGRATION_APPLIED_SQL,
    SCHEMA_MIGRATION_MARK_SQL,
)

_FETCH_BATCH = 1000


def migration_applied(conn: psycopg.Connection, migration_id: str) -> bool:
    """True once `migration_id` has completed."""
    with conn.cursor() as cur:
        cur.execute(SCHEMA_MIGRATION_APPLIED_SQL, (migration_id,))
        return cur.fetchone() is not None


def canonicalize_dimension_rows(conn: psycopg.Connection) -> dict[str, int]:
    """Rewrite every dimension row into the form the write path now produces.

    Versions before canonical serialization stored sets in `extra` as str(set),
    whose element order depends on PYTHONHASHSEED, and could store `labels`
    unsorted. Change detection and the repair SQL compare stored values
    directly, so such rows must be rewritten once rather than tolerated forever.

    Only `extra` and `labels` change; rows already canonical are not written.
    One transaction per table under a writer-blocking lock; the marker is
    written in the last one, so an interrupted run re-runs (idempotently) on
    the next start. Returns rows rewritten per table.
    """
    rewritten: dict[str, int] = {}
    last = SCD2_DIMENSIONS[-1][0]
    for table, _key in SCD2_DIMENSIONS:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(SCD2_REPAIR_LOCK_TIMEOUT_SQL)
                cur.execute(SCD2_REPAIR_LOCK_SQL[table])
            rewritten[table] = _canonicalize_table(conn, table)
            if table == last:
                with conn.cursor() as cur:
                    cur.execute(SCHEMA_MIGRATION_MARK_SQL, (CANONICAL_SETS_MIGRATION_ID,))
    return rewritten


def _canonicalize_table(conn: psycopg.Connection, table: str) -> int:
    count = 0
    # Server-side cursor: entities can hold many versions with large `extra`
    # blobs; streaming bounds memory on small HA hosts.
    with conn.cursor(name=f"canonicalize_{table}") as read, conn.cursor() as write:
        read.execute(CANONICALIZE_SELECT_SQL[table])
        while rows := read.fetchmany(_FETCH_BATCH):
            updates = []
            for rid, labels, extra in rows:
                new_labels = canonical_labels(labels)
                new_extra = canonical_legacy(extra)
                if new_labels != labels or new_extra != extra:
                    updates.append({"rid": rid, "labels": new_labels,
                                    "extra": None if new_extra is None else Jsonb(new_extra)})
            if updates:
                write.executemany(CANONICALIZE_UPDATE_SQL[table], updates)
                count += len(updates)
    return count
