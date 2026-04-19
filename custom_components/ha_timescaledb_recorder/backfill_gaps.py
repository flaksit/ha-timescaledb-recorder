#!/usr/bin/env python3
"""Backfill gaps in ha_states (TimescaleDB) from Home Assistant's SQLite recorder.

From the HA host terminal (SSH addon):

    docker exec homeassistant python3 \
        /config/custom_components/ha_timescaledb_recorder/backfill_gaps.py

No arguments needed — auto-detects SQLite path (/config/home-assistant_v2.db)
and reads the DSN from the ha_timescaledb_recorder integration config.

Requires psycopg[binary] — already present in the HA container once the
ha_timescaledb_recorder integration is installed.

Safe to run while HA is running — SQLite is opened read-only. Re-running is safe:
(entity_id, last_updated) fingerprint comparison skips rows already in TimescaleDB.

Memory is bounded to one time bucket at a time (default 60 min). At 40k rows/hour
a bucket occupies ~8 MB.

Pre-check: each bucket compares COUNT(*) between SQLite and TimescaleDB before
fetching individual rows. Matching counts → skip with zero row fetches.

The PG password in --pg-dsn is visible in the process list. Use PGPASSWORD or
a .pgpass file if that is a concern.

Algorithm (per bucket)
----------------------
1. Fast COUNT(*) in SQLite (no join).
2. Fast COUNT(*) in TimescaleDB.
3. If counts match → skip (no gaps possible without count divergence).
4. If --entities set and counts differ → filtered re-count before row fetches.
5. Fetch candidate rows from SQLite for the bucket.
6. Fetch (entity_id, last_updated) fingerprints from TimescaleDB.
7. Set-subtract; insert missing rows in batches inside transactions.
"""

import argparse
import asyncio
import json
import math
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg  # pyright: ignore[reportMissingImports]
from psycopg.types.json import Jsonb  # pyright: ignore[reportMissingImports]

# ha_states has no unique constraint; deduplication handled by fingerprint
# comparison before this INSERT runs. Placeholders are %s (psycopg3 style).
INSERT_SQL = """
INSERT INTO ha_states (entity_id, state, attributes, last_updated, last_changed)
VALUES (%s, %s, %s, %s, %s)
"""

# Bucket-level count pre-check — cheap aggregate, no row fetching.
PG_COUNT_SQL = """
SELECT COUNT(*) FROM ha_states
WHERE last_updated >= %s AND last_updated < %s
"""

# Filtered count — only used when --entities is set and full counts differ.
PG_COUNT_FILTERED_SQL = """
SELECT COUNT(*) FROM ha_states
WHERE last_updated >= %s AND last_updated < %s
  AND entity_id = ANY(%s::text[])
"""

PG_FINGERPRINT_SQL = """
SELECT entity_id, last_updated
FROM ha_states
WHERE last_updated >= %s AND last_updated < %s
  AND entity_id = ANY(%s::text[])
"""

# Fast count — no join. Every states row has a metadata_id so count is identical
# to the join-based count.
SQLITE_COUNT_SQL = """
SELECT COUNT(*) FROM states
WHERE last_updated_ts >= :start_ts AND last_updated_ts < :end_ts
"""

# Filtered count — join required to access entity_id. Only used when --entities
# is set AND the fast counts differ (avoids the join in the common case).
SQLITE_COUNT_FILTERED_SQL = """
SELECT COUNT(*) FROM states s
JOIN states_meta sm ON sm.metadata_id = s.metadata_id
WHERE s.last_updated_ts >= :start_ts AND s.last_updated_ts < :end_ts
  AND sm.entity_id IN ({placeholders})
"""

# In modern HA (2023.3+) entity_id lives in states_meta, not states directly.
# COALESCE: last_changed_ts is NULL when it equals last_updated_ts (HA normalises
# it away to save space). Bucket end is exclusive to avoid double-counting edges.
SQLITE_BUCKET_SQL = """
SELECT
    sm.entity_id,
    s.state,
    sa.shared_attrs,
    s.last_updated_ts,
    COALESCE(s.last_changed_ts, s.last_updated_ts) AS last_changed_ts
FROM states s
JOIN states_meta sm ON sm.metadata_id = s.metadata_id
LEFT JOIN state_attributes sa ON sa.attributes_id = s.attributes_id
WHERE s.last_updated_ts >= :start_ts AND s.last_updated_ts < :end_ts
ORDER BY s.last_updated_ts
"""

SQLITE_RANGE_SQL = """
SELECT MIN(last_updated_ts), MAX(last_updated_ts) FROM states
"""


def _ts_to_dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _dt_to_ts(dt: datetime) -> float:
    return dt.timestamp()


def _parse_iso_utc(s: str) -> datetime:
    """Parse ISO 8601 → UTC. Naive strings assumed UTC; aware strings converted."""
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _parse_end_exclusive(s: str) -> datetime:
    """Parse --end as an inclusive boundary; return the equivalent exclusive end.

    Precision is inferred from the input string structure so the whole unit is
    included: a date includes the full day, a HH:MM includes the full minute, etc.

      2026-04-10          → 2026-04-11T00:00:00  (whole day)
      2026-04-10T14       → 2026-04-10T15:00:00  (whole hour)
      2026-04-10T14:30    → 2026-04-10T14:31:00  (whole minute)
      2026-04-10T14:30:45 → 2026-04-10T14:30:46  (whole second)
      sub-second / full   → used as-is (already an exact point)
    """
    from datetime import timedelta
    dt = _parse_iso_utc(s)
    # Strip timezone suffix to inspect structural precision of the input string.
    b = s.split("+")[0].rstrip("Z").strip()
    if "T" not in b:
        return dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    time_part = b.split("T")[1]
    colons = time_part.count(":")
    if colons == 0:
        return dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    if colons == 1:
        return dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
    if colons == 2 and "." not in time_part:
        return dt.replace(microsecond=0) + timedelta(seconds=1)
    return dt  # sub-second precision → treat as exact exclusive boundary


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Backfill gaps in ha_states (TimescaleDB) from Home Assistant's "
            "SQLite recorder database."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--sqlite", default=None, metavar="PATH",
                   help="Path to home-assistant_v2.db. Default: /config/home-assistant_v2.db")
    p.add_argument("--pg-dsn", default=None, metavar="DSN",
                   help="PostgreSQL/TimescaleDB DSN. Default: read from HA config entries.")
    p.add_argument("--start", metavar="ISO8601", default=None,
                   help="Earliest timestamp (UTC, inclusive). Default: earliest SQLite row.")
    p.add_argument("--end", metavar="ISO8601", default=None,
                   help="Latest timestamp (UTC, exclusive). Default: latest SQLite row + 1 µs.")
    p.add_argument("--bucket-minutes", type=float, default=60.0, metavar="M",
                   help="Bucket width in minutes (default: 60). Larger = fewer PG queries, more memory.")
    p.add_argument("--batch-size", type=int, default=500, metavar="N",
                   help="Rows per INSERT batch (default: 500).")
    p.add_argument("--entities", metavar="ENTITY_IDS", default=None,
                   help="Comma-separated entity_ids to backfill. Default: all.")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would be inserted without writing to TimescaleDB.")
    return p.parse_args()


def fetch_sqlite_range(db_path: str) -> tuple[float, float]:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        row = conn.execute(SQLITE_RANGE_SQL).fetchone()
    if row is None or row[0] is None:
        raise ValueError("SQLite states table is empty — nothing to backfill.")
    return float(row[0]), float(row[1])


def sqlite_bucket_count(
    db_path: str,
    start_ts: float,
    end_ts: float,
    entities_filter: set[str] | None = None,
) -> int:
    """COUNT(*) for the bucket. No join without filter; join + IN with filter."""
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        if entities_filter:
            placeholders = ",".join("?" * len(entities_filter))
            sql = SQLITE_COUNT_FILTERED_SQL.format(placeholders=placeholders)
            params: tuple[Any, ...] = (start_ts, end_ts, *entities_filter)
            row = conn.execute(sql, params).fetchone()
        else:
            row = conn.execute(
                SQLITE_COUNT_SQL, {"start_ts": start_ts, "end_ts": end_ts}
            ).fetchone()
    return int(row[0]) if row else 0


def fetch_sqlite_bucket(
    db_path: str,
    start_ts: float,
    end_ts: float,
    entities_filter: set[str] | None,
) -> list[dict[str, Any]]:
    """Fetch one bucket's rows from SQLite. Memory bounded to bucket size."""
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(SQLITE_BUCKET_SQL, {"start_ts": start_ts, "end_ts": end_ts})
        rows: list[dict[str, Any]] = [
            dict(r) for r in cursor
            if entities_filter is None or r["entity_id"] in entities_filter
        ]
    return rows


async def pg_bucket_count(
    conn: psycopg.AsyncConnection,
    start: datetime,
    end: datetime,
    entities_filter: set[str] | None = None,
) -> int:
    if entities_filter:
        cur = await conn.execute(PG_COUNT_FILTERED_SQL, (start, end, list(entities_filter)))
    else:
        cur = await conn.execute(PG_COUNT_SQL, (start, end))
    row = await cur.fetchone()
    return int(row[0]) if row else 0


async def fetch_pg_fingerprints(
    conn: psycopg.AsyncConnection,
    start: datetime,
    end: datetime,
    entity_ids: list[str],
) -> set[tuple[str, datetime]]:
    cur = await conn.execute(PG_FINGERPRINT_SQL, (start, end, entity_ids))
    rows = await cur.fetchall()
    return {(r[0], r[1]) for r in rows}


def build_missing_rows(
    sqlite_rows: list[dict[str, Any]],
    pg_fingerprints: set[tuple[str, datetime]],
) -> list[tuple[Any, ...]]:
    """Return rows present in SQLite but absent from TimescaleDB.

    NULL shared_attrs → insert NULL attributes (ha_states.attributes is nullable).
    Non-null shared_attrs → wrap in Jsonb() as required by psycopg3 for JSONB columns.
    """
    missing: list[tuple[Any, ...]] = []
    for row in sqlite_rows:
        last_updated_dt = _ts_to_dt(row["last_updated_ts"])
        if (row["entity_id"], last_updated_dt) in pg_fingerprints:
            continue
        attrs = Jsonb(json.loads(row["shared_attrs"])) if row["shared_attrs"] is not None else None
        missing.append((
            row["entity_id"],
            row["state"],
            attrs,
            last_updated_dt,
            _ts_to_dt(row["last_changed_ts"]),
        ))
    return missing


async def insert_batches(
    conn: psycopg.AsyncConnection,
    rows: list[tuple[Any, ...]],
    batch_size: int,
    dry_run: bool,
) -> int:
    """Insert in batches, each in its own transaction. Returns count inserted."""
    inserted = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        if not dry_run:
            async with conn.transaction():
                await conn.executemany(INSERT_SQL, batch)
        inserted += len(batch)
    return inserted


_DEFAULT_SQLITE = "/config/home-assistant_v2.db"
_HA_CONFIG_ENTRIES = "/config/.storage/core.config_entries"
_HA_DOMAIN = "ha_timescaledb_recorder"


def _detect_pg_dsn() -> str:
    """Read DSN from HA config entries storage."""
    try:
        with open(_HA_CONFIG_ENTRIES) as f:
            data = json.load(f)
        for entry in data.get("data", {}).get("entries", []):
            if entry.get("domain") == _HA_DOMAIN:
                dsn = entry.get("data", {}).get("dsn")
                if dsn:
                    return dsn
    except (OSError, json.JSONDecodeError):
        pass
    raise ValueError(
        f"Could not auto-detect DSN from {_HA_CONFIG_ENTRIES}. "
        "Pass --pg-dsn explicitly."
    )


async def main() -> None:
    args = parse_args()
    sqlite_path = str(Path(args.sqlite or _DEFAULT_SQLITE).expanduser().resolve())
    pg_dsn = args.pg_dsn or _detect_pg_dsn()
    print(f"SQLite: {sqlite_path}")
    print(f"PG DSN: {pg_dsn[:pg_dsn.index('@') + 1]}… (credentials hidden)" if "@" in pg_dsn else f"PG DSN: {pg_dsn}")

    entities_filter: set[str] | None = (
        {e.strip() for e in args.entities.split(",") if e.strip()}
        if args.entities else None
    )

    # ── Time window ───────────────────────────────────────────────────────────
    print("Detecting time range from SQLite …")
    min_ts, max_ts = fetch_sqlite_range(sqlite_path)

    start_ts = _dt_to_ts(_parse_iso_utc(args.start)) if args.start else min_ts
    end_ts = _dt_to_ts(_parse_end_exclusive(args.end)) if args.end else max_ts + 1e-6

    bucket_secs = args.bucket_minutes * 60
    n_buckets = math.ceil((end_ts - start_ts) / bucket_secs)

    print(
        f"Time window: {_ts_to_dt(start_ts).isoformat()} → {_ts_to_dt(end_ts).isoformat()}\n"
        f"Buckets: {n_buckets} × {args.bucket_minutes:.0f}min"
    )

    # ── Connect ───────────────────────────────────────────────────────────────
    pg_conn = await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True)

    total_scanned = total_gaps = total_inserted = 0
    buckets_skipped = bucket_idx = 0
    last_print = time.monotonic()

    try:
        ts = start_ts
        while ts < end_ts:
            bucket_end = min(ts + bucket_secs, end_ts)
            start_dt, end_dt = _ts_to_dt(ts), _ts_to_dt(bucket_end)
            bucket_idx += 1

            # ── Count pre-check ───────────────────────────────────────────────
            sq_count = sqlite_bucket_count(sqlite_path, ts, bucket_end)
            if sq_count == 0:
                buckets_skipped += 1
                ts = bucket_end
                continue

            pg_count = await pg_bucket_count(pg_conn, start_dt, end_dt)
            if pg_count != sq_count and entities_filter:
                # Other entities may explain the mismatch — recount with filter.
                sq_count = sqlite_bucket_count(sqlite_path, ts, bucket_end, entities_filter)
                pg_count = await pg_bucket_count(pg_conn, start_dt, end_dt, entities_filter)

            if pg_count == sq_count:
                # Same count → assume in sync (count collision for timestamped
                # state events is astronomically unlikely).
                buckets_skipped += 1
                ts = bucket_end
                continue

            # ── Counts differ → full fingerprint comparison ───────────────────
            sqlite_rows = fetch_sqlite_bucket(sqlite_path, ts, bucket_end, entities_filter)
            if sqlite_rows:
                entity_ids = list({r["entity_id"] for r in sqlite_rows})
                fingerprints = await fetch_pg_fingerprints(pg_conn, start_dt, end_dt, entity_ids)
                missing = build_missing_rows(sqlite_rows, fingerprints)

                if missing:
                    if args.dry_run and total_gaps == 0:
                        print("\n[DRY RUN] First 5 rows that would be inserted:")
                        for row in missing[:5]:
                            print(f"  {row[0]} @ {row[3].isoformat()} state={row[1]!r}")

                    inserted = await insert_batches(
                        pg_conn, missing, args.batch_size, args.dry_run
                    )
                    total_inserted += inserted
                    total_gaps += len(missing)

                total_scanned += len(sqlite_rows)

            # ── Progress (every 10 s) ─────────────────────────────────────────
            now = time.monotonic()
            if now - last_print >= 10.0:
                print(
                    f"  [{bucket_idx}/{n_buckets}] "
                    f"scanned={total_scanned:,} gaps={total_gaps:,} inserted={total_inserted:,}",
                    flush=True,
                )
                last_print = now

            ts = bucket_end

    finally:
        await pg_conn.close()

    dry_suffix = " (dry-run — no rows written)" if args.dry_run else ""
    print(
        f"Buckets skipped (in sync): {buckets_skipped:,} / {n_buckets:,}\n"
        f"Scanned: {total_scanned:,} | Gaps: {total_gaps:,} | Inserted: {total_inserted:,}{dry_suffix}"
    )


try:
    asyncio.run(main())
except Exception as exc:
    print(f"Error: {exc}", file=sys.stderr)
    sys.exit(1)
