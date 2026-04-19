#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "asyncpg>=0.29",
#   "tqdm>=4.66",
# ]
# ///
"""Backfill gaps in ha_states (TimescaleDB) from Home Assistant's SQLite recorder.

Usage
-----
    uv run scripts/backfill_gaps.py \\
        --sqlite /path/to/home-assistant_v2.db \\
        --pg-dsn "postgresql://user:pass@host/db"

The script is safe to run while HA is running — SQLite is opened read-only
(file URI mode) and TimescaleDB inserts use ON CONFLICT DO NOTHING so they are
idempotent as long as a unique constraint exists on (entity_id, last_updated).

Algorithm
---------
1. Read every (entity_id, last_updated_ts, last_changed_ts) from SQLite.
2. Query TimescaleDB for existing (entity_id, last_updated) fingerprints, filtered
   to only the entity_ids present in the SQLite export.
3. Set-subtract to find rows absent from TimescaleDB.
4. For each missing row, fetch state + attributes from SQLite and insert into PG.
"""

import argparse
import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
from tqdm import tqdm

# Matches ha_states table column order in INSERT_SQL from const.py:
#   (entity_id, state, attributes, last_updated, last_changed)
INSERT_SQL = """
INSERT INTO ha_states (entity_id, state, attributes, last_updated, last_changed)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT DO NOTHING
"""

# Fetch existing (entity_id, last_updated) fingerprints from TimescaleDB for
# entities that appear in the SQLite export.  Filtering by entity_id up front
# avoids scanning the entire TimescaleDB table on large installations.
#
# $1 = start TIMESTAMPTZ (inclusive), $2 = end TIMESTAMPTZ (inclusive),
# $3 = text[] of entity_ids to restrict the scan.
PG_FINGERPRINT_SQL = """
SELECT entity_id, last_updated
FROM ha_states
WHERE last_updated BETWEEN $1 AND $2
  AND entity_id = ANY($3::text[])
"""

# Fetch all candidate rows from SQLite within the time window.
# COALESCE: last_changed_ts is NULL when it equals last_updated_ts (HA
# normalises it away to save space).
SQLITE_CANDIDATES_SQL = """
SELECT
    s.entity_id,
    s.state,
    sa.shared_attrs,
    s.last_updated_ts,
    COALESCE(s.last_changed_ts, s.last_updated_ts) AS last_changed_ts
FROM states s
LEFT JOIN state_attributes sa ON sa.attributes_id = s.attributes_id
WHERE s.last_updated_ts BETWEEN :start_ts AND :end_ts
  AND s.entity_id IS NOT NULL
ORDER BY s.last_updated_ts
"""


def _ts_to_dt(ts_float: float) -> datetime:
    """Convert a UNIX timestamp (float seconds) to an aware UTC datetime."""
    return datetime.fromtimestamp(ts_float, tz=timezone.utc)


def _dt_to_ts(dt: datetime) -> float:
    """Convert an aware UTC datetime to a UNIX timestamp (float seconds)."""
    return dt.timestamp()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Backfill gaps in ha_states (TimescaleDB) from Home Assistant's "
            "SQLite recorder database."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--sqlite",
        required=True,
        metavar="PATH",
        help="Path to home-assistant_v2.db (opened read-only).",
    )
    p.add_argument(
        "--pg-dsn",
        required=True,
        metavar="DSN",
        help="PostgreSQL/TimescaleDB connection string, e.g. postgresql://user:pass@host/db",
    )
    p.add_argument(
        "--start",
        metavar="ISO8601",
        default=None,
        help=(
            "Earliest timestamp to consider (UTC, inclusive). "
            "Default: earliest row in SQLite."
        ),
    )
    p.add_argument(
        "--end",
        metavar="ISO8601",
        default=None,
        help=(
            "Latest timestamp to consider (UTC, inclusive). "
            "Default: latest row in SQLite."
        ),
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=500,
        metavar="N",
        help="Number of rows per INSERT batch (default: 500).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be inserted without writing to TimescaleDB.",
    )
    return p.parse_args()


def read_sqlite_candidates(
    db_path: str,
    start_ts: float,
    end_ts: float,
) -> tuple[list[dict], list[str]]:
    """Read candidate rows from SQLite.

    Returns (rows, entity_ids) where rows is a list of dicts with keys
    entity_id, state, shared_attrs, last_updated_ts, last_changed_ts, and
    entity_ids is a deduplicated list of all entity_ids found.

    The connection uses file URI mode (mode=ro) so HA can stay running.
    shared_attrs may be None if state_attributes row is missing — the script
    skips those rows (they cannot be backfilled without attributes data).
    """
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(
            SQLITE_CANDIDATES_SQL,
            {"start_ts": start_ts, "end_ts": end_ts},
        )
        rows = [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()

    entity_ids = list({r["entity_id"] for r in rows})
    return rows, entity_ids


def detect_sqlite_time_range(db_path: str) -> tuple[float, float]:
    """Return (min_ts, max_ts) of last_updated_ts from the SQLite states table."""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        row = conn.execute(
            "SELECT MIN(last_updated_ts), MAX(last_updated_ts) FROM states"
        ).fetchone()
    finally:
        conn.close()

    if row is None or row[0] is None:
        raise ValueError("SQLite states table is empty — nothing to backfill.")
    return float(row[0]), float(row[1])


async def fetch_pg_fingerprints(
    conn: asyncpg.Connection,
    start: datetime,
    end: datetime,
    entity_ids: list[str],
) -> set[tuple[str, datetime]]:
    """Fetch (entity_id, last_updated) pairs already in TimescaleDB.

    Filtering by entity_id = ANY($3) avoids a full table scan.  TimescaleDB
    uses the time partition + the idx_ha_states_entity_time index for
    entity_id lookups, so this is efficient even on large tables.
    """
    rows = await conn.fetch(PG_FINGERPRINT_SQL, start, end, entity_ids)
    # Normalise to (entity_id, last_updated rounded to microsecond) so that
    # float precision differences between SQLite and PG don't cause false misses.
    return {(r["entity_id"], r["last_updated"]) for r in rows}


def build_missing_rows(
    sqlite_rows: list[dict],
    pg_fingerprints: set[tuple[str, datetime]],
) -> list[tuple]:
    """Return rows present in SQLite but absent from TimescaleDB.

    Each output tuple matches INSERT_SQL parameter order:
      (entity_id, state, attributes_json, last_updated, last_changed)

    Rows with NULL shared_attrs are skipped — they cannot be stored without
    attributes data and are typically internal HA housekeeping rows.

    Timestamp comparison uses microsecond rounding to absorb float→datetime
    conversion noise (SQLite stores floats; PG stores TIMESTAMPTZ at µs).
    """
    missing = []
    for row in sqlite_rows:
        if row["shared_attrs"] is None:
            # No attributes row — skip; these are typically synthetic or
            # corrupted states that HA itself does not surface to users.
            continue

        last_updated_dt = _ts_to_dt(row["last_updated_ts"])
        # Round to microsecond to match PostgreSQL TIMESTAMPTZ resolution.
        last_updated_dt = last_updated_dt.replace(
            microsecond=round(last_updated_dt.microsecond, -0)
        )

        if (row["entity_id"], last_updated_dt) in pg_fingerprints:
            continue  # Already in TimescaleDB

        last_changed_dt = _ts_to_dt(row["last_changed_ts"])

        missing.append((
            row["entity_id"],
            row["state"],
            # Pass the raw JSON string — asyncpg accepts a JSON string for
            # JSONB columns in text protocol mode (no round-trip through
            # json.loads + json.dumps needed).
            row["shared_attrs"],
            last_updated_dt,
            last_changed_dt,
        ))
    return missing


async def insert_batches(
    conn: asyncpg.Connection,
    rows: list[tuple],
    batch_size: int,
    dry_run: bool,
) -> int:
    """Insert rows in batches; returns count of rows inserted (or would-insert)."""
    if not rows:
        return 0

    inserted = 0
    with tqdm(total=len(rows), unit="rows", desc="Inserting") as pbar:
        for i in range(0, len(rows), batch_size):
            batch = rows[i : i + batch_size]
            if dry_run:
                # Dry-run: just count and advance progress bar.
                inserted += len(batch)
                pbar.update(len(batch))
                continue

            # executemany is safe here: single connection, no pool, no
            # concurrent writers — TimescaleDB deadlock risk is eliminated.
            await conn.executemany(INSERT_SQL, batch)
            inserted += len(batch)
            pbar.update(len(batch))

    return inserted


async def main() -> None:
    args = parse_args()

    sqlite_path = str(Path(args.sqlite).expanduser().resolve())

    # ── Determine time window ──────────────────────────────────────────────
    print("Detecting time range from SQLite …")
    min_ts, max_ts = detect_sqlite_time_range(sqlite_path)

    if args.start is not None:
        start_dt = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
        start_ts = _dt_to_ts(start_dt)
    else:
        start_ts = min_ts
        start_dt = _ts_to_dt(start_ts)

    if args.end is not None:
        end_dt = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
        end_ts = _dt_to_ts(end_dt)
    else:
        end_ts = max_ts
        end_dt = _ts_to_dt(end_ts)

    print(f"Time window: {start_dt.isoformat()} → {end_dt.isoformat()}")

    # ── Read SQLite candidates ─────────────────────────────────────────────
    print("Reading SQLite candidates …")
    sqlite_rows, entity_ids = read_sqlite_candidates(sqlite_path, start_ts, end_ts)
    print(f"  Found {len(sqlite_rows):,} rows across {len(entity_ids):,} entities.")

    if not sqlite_rows:
        print("Nothing to backfill.")
        return

    # ── Connect to TimescaleDB ─────────────────────────────────────────────
    print("Connecting to TimescaleDB …")
    # Single connection (not pool) — this is a one-shot script.
    pg_conn = await asyncpg.connect(args.pg_dsn)

    try:
        # ── Fetch existing fingerprints ────────────────────────────────────
        print("Fetching existing fingerprints from TimescaleDB …")
        pg_fingerprints = await fetch_pg_fingerprints(
            pg_conn, start_dt, end_dt, entity_ids
        )
        print(f"  TimescaleDB already has {len(pg_fingerprints):,} matching rows.")

        # ── Compute gap ────────────────────────────────────────────────────
        missing = build_missing_rows(sqlite_rows, pg_fingerprints)
        print(f"  Gap: {len(missing):,} rows to backfill.")

        if not missing:
            print("No gaps found — TimescaleDB is up to date.")
            return

        # ── Insert ────────────────────────────────────────────────────────
        if args.dry_run:
            print("[DRY RUN] Would insert the following (first 5):")
            for row in missing[:5]:
                print(f"  {row[0]} @ {row[3].isoformat()} — state={row[1]!r}")

        inserted = await insert_batches(pg_conn, missing, args.batch_size, args.dry_run)

        if args.dry_run:
            print(f"[DRY RUN] Would have inserted {inserted:,} rows.")
        else:
            print(f"Done. Inserted {inserted:,} rows into ha_states.")

    finally:
        await pg_conn.close()


asyncio.run(main())
