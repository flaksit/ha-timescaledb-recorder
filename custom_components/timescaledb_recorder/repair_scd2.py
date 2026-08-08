#!/usr/bin/env python3
"""Repair SCD2 dimension history and enforce the invariant (issue #17).

From the HA host terminal (SSH addon):

    docker exec homeassistant python3 \
        /config/custom_components/timescaledb_recorder/repair_scd2.py --dry-run

Run order matters. Update the integration and restart HA FIRST, so the corrected
write path is live, then repair. Repairing while the old code still runs lets the
damage reappear immediately, and adding the constraints under the old code would
make every metadata write fail.

    1. update the integration, restart HA
    2. --dry-run      inspect what would change; mutates nothing
    3. --apply        back up, repair, verify, add constraints
    4. --verify-only  confirm, now and after a day of normal operation

What it does
------------
Reconstructs each dimension's version intervals from `valid_from` ordering alone,
and writes `valid_to` only. It never writes `valid_from`, and by default it never
deletes a row.

Per table, in one all-or-nothing transaction under SHARE ROW EXCLUSIVE:

    backup -> [collapse duplicates] -> archive rows the clamp will rewrite
           -> rebuild valid_to -> clamp inverted intervals

then verify, then add the exclusion constraint if and only if verification is
clean. Atomicity is per table, not per run: an interrupted run leaves earlier
tables committed and the rest untouched, and re-running converges.

What it does NOT do
-------------------
`valid_from` is the best available anchor, not a proven one. It is the field the
known defects leave alone, but earlier versions of this integration processed
registry events concurrently and read both the clock and the registry entry at
processing time rather than event time. So a `valid_from` can be later than the
change it records, and a version that was overwritten before it was ever written
is simply absent. This script normalises intervals; it cannot recover history
that was never persisted, and it does not consult the live HA registries.

The `Fidelity` section of its output reports what it cannot decide: gaps the
`states` table contradicts, and ids left with no current version. Read it.

Safety
------
- A full copy of each table is written to `<table>_prerepair_<utc timestamp>`
  before anything is modified. That is the undo path, and the only complete
  record of the pre-repair `valid_to` values; drop them once satisfied.
- `scd2_repair_quarantine` holds the rows that were deleted or clamped, with
  their full payload. Rebuilt `valid_to` values are not archived there — the
  backup tables carry those.
- Re-running is safe and converges: a second --apply changes zero rows.
- The DSN password is visible in the process list. Prefer PGPASSWORD or .pgpass.
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone

import psycopg  # pyright: ignore[reportMissingImports]
from psycopg.types.json import Jsonb  # pyright: ignore[reportMissingImports]

try:
    from .const import (
        SCD2_DIMENSIONS,
        SCD2_BTREE_GIST_SQL,
        SCD2_EXCLUDE_CONSTRAINT_SQL,
        SCD2_OPEN_UNIQUE_IDX_SQL,
        SCD2_QUARANTINE_DDL_SQL,
        SCD2_QUARANTINE_IDX_SQL,
        SCD2_QUARANTINE_INSERT_SQL,
        SCD2_QUARANTINE_SUMMARY_SQL,
        SCD2_REGCLASS_EXISTS_SQL,
        SCD2_REPAIR_AMBIGUOUS_SQL,
        SCD2_REPAIR_BACKUP_SQL,
        SCD2_REPAIR_CLAMP_PREVIEW_SQL,
        SCD2_REPAIR_CLAMP_ROWS_SQL,
        SCD2_REPAIR_CLAMP_SQL,
        SCD2_REPAIR_COLLAPSE_SQL,
        SCD2_REPAIR_IDENTICAL_DUPES_SQL,
        SCD2_REPAIR_LOCK_SQL,
        SCD2_REPAIR_LOCK_TIMEOUT_SQL,
        SCD2_REPAIR_REBUILD_PREVIEW_SQL,
        SCD2_REPAIR_REBUILD_SQL,
        SCD2_STATES_WITHOUT_DIM_SQL,
        SCD2_SUSPICIOUS_GAPS_SQL,
        SCD2_NO_CURRENT_VERSION_SQL,
        SCD2_VERIFY_CHECKS,
    )
except ImportError:
    # Executed as a plain script (docker exec python3 .../repair_scd2.py), so the
    # package context does not exist and the relative import fails.
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
    from const import (  # pyright: ignore[reportMissingImports]
        SCD2_DIMENSIONS,
        SCD2_BTREE_GIST_SQL,
        SCD2_EXCLUDE_CONSTRAINT_SQL,
        SCD2_OPEN_UNIQUE_IDX_SQL,
        SCD2_QUARANTINE_DDL_SQL,
        SCD2_QUARANTINE_IDX_SQL,
        SCD2_QUARANTINE_INSERT_SQL,
        SCD2_QUARANTINE_SUMMARY_SQL,
        SCD2_REGCLASS_EXISTS_SQL,
        SCD2_REPAIR_AMBIGUOUS_SQL,
        SCD2_REPAIR_BACKUP_SQL,
        SCD2_REPAIR_CLAMP_PREVIEW_SQL,
        SCD2_REPAIR_CLAMP_ROWS_SQL,
        SCD2_REPAIR_CLAMP_SQL,
        SCD2_REPAIR_COLLAPSE_SQL,
        SCD2_REPAIR_IDENTICAL_DUPES_SQL,
        SCD2_REPAIR_LOCK_SQL,
        SCD2_REPAIR_LOCK_TIMEOUT_SQL,
        SCD2_REPAIR_REBUILD_PREVIEW_SQL,
        SCD2_REPAIR_REBUILD_SQL,
        SCD2_STATES_WITHOUT_DIM_SQL,
        SCD2_SUSPICIOUS_GAPS_SQL,
        SCD2_NO_CURRENT_VERSION_SQL,
        SCD2_VERIFY_CHECKS,
    )

_HA_CONFIG_ENTRIES = "/config/.storage/core.config_entries"
_HA_DOMAIN = "timescaledb_recorder"

# A hit here means the trust-valid_from assumption is wrong for this database and
# the reconstruction would be built on bad input. Stop rather than repair.
_FATAL_CHECK = "valid_from_sanity"


def _detect_pg_dsn() -> str:
    """Read the DSN from HA config entries storage."""
    try:
        with open(_HA_CONFIG_ENTRIES, encoding="utf-8") as f:
            data = json.load(f)
        for entry in data.get("data", {}).get("entries", []):
            if entry.get("domain") == _HA_DOMAIN:
                dsn = entry.get("data", {}).get("dsn")
                if dsn:
                    return dsn
    except (OSError, json.JSONDecodeError):
        pass
    raise ValueError(
        f"Could not auto-detect DSN from {_HA_CONFIG_ENTRIES}. Pass --dsn explicitly."
    )


def _redact(dsn: str) -> str:
    return re.sub(r"(://[^:@]+:)[^@]+(@)", r"\1***\2", dsn) if "@" in dsn else dsn


def _scalar(cur, sql: str, params: tuple = ()) -> object:
    cur.execute(sql, params)
    row = cur.fetchone()
    return row[0] if row else None


def print_identity(conn: psycopg.Connection) -> None:
    """Show which server and database this is about to touch.

    Printed before anything else and before any mutation: the whole point of the
    dry-run/apply split is that a human confirms the target, and they cannot do
    that without seeing it.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_database(), inet_server_addr(), inet_server_port(),"
            " current_user, version()"
        )
        db, addr, port, user, version = cur.fetchone()
    print(f"  database : {db}")
    print(f"  server   : {addr}:{port}")
    print(f"  user     : {user}")
    print(f"  version  : {version.split(',')[0]}")


def run_verification(conn: psycopg.Connection) -> dict[str, dict[str, int]]:
    """Return {table: {check_name: offending_row_count}} for all dimensions."""
    results: dict[str, dict[str, int]] = {}
    for table, _key in SCD2_DIMENSIONS:
        per_table: dict[str, int] = {}
        for name, statements in SCD2_VERIFY_CHECKS:
            with conn.cursor() as cur:
                cur.execute(statements[table])
                per_table[name] = len(cur.fetchall())
        results[table] = per_table
    return results


def print_verification(results: dict[str, dict[str, int]]) -> bool:
    """Print the verification matrix. Returns True if every check is clean."""
    names = [name for name, _ in SCD2_VERIFY_CHECKS]
    width = max(len(t) for t, _ in SCD2_DIMENSIONS)
    header = "  " + "table".ljust(width) + "".join(f"  {n:>17}" for n in names)
    print(header)
    print("  " + "-" * (len(header) - 2))
    clean = True
    for table, _key in SCD2_DIMENSIONS:
        row = results[table]
        cells = "".join(f"  {row[n]:>17}" for n in names)
        print("  " + table.ljust(width) + cells)
        clean = clean and not any(row.values())
    return clean


def report_ambiguity(conn: psycopg.Connection) -> dict[str, int]:
    """Report same-(id, valid_from) collisions. Read-only.

    Groups where the payloads differ are genuinely ambiguous — nothing in the data
    says which version owned the era. The repair keeps every row and lets all but
    one collapse to an empty interval, so no payload is lost, but the collapsed
    versions label no state rows and a human may want to adjudicate.
    """
    counts: dict[str, int] = {}
    for table, _key in SCD2_DIMENSIONS:
        with conn.cursor() as cur:
            cur.execute(SCD2_REPAIR_AMBIGUOUS_SQL[table])
            groups = cur.fetchall()
            cur.execute(SCD2_REPAIR_IDENTICAL_DUPES_SQL[table])
            identical = cur.fetchall()
        differing = len(groups) - len(identical)
        counts[table] = differing
        if groups:
            print(
                f"  {table}: {len(groups)} duplicate (id, valid_from) group(s) — "
                f"{len(identical)} byte-identical, {differing} with differing payloads"
            )
    return counts


def report_fidelity(conn: psycopg.Connection) -> int:
    """Report what the repair cannot decide. Read-only; returns the doubt count.

    Passing the invariant checks means the intervals are consistent. It does not
    mean they are true. Two kinds of doubt are measurable, and staying quiet about
    them would repeat the failure that made issue #17 invisible for months.
    """
    doubts = 0

    with conn.cursor() as cur:
        cur.execute(SCD2_SUSPICIOUS_GAPS_SQL)
        gaps = cur.fetchall()
    if gaps:
        doubts += len(gaps)
        total = sum(row[3] for row in gaps)
        print(f"  {len(gaps)} gap(s) where the entity kept recording states "
              f"({total} state rows fall inside a period the dimension calls absent). "
              "These are lost inserts, not removals. The repair preserves the gap "
              "rather than inventing continuity — states_flat is unaffected, since "
              "it derives eras from valid_from and ignores valid_to.")
        for entity_id, start, end, n in gaps[:5]:
            print(f"    {entity_id}: {start} -> {end} ({n} states)")
        if len(gaps) > 5:
            print(f"    ... and {len(gaps) - 5} more")

    for table, _key in SCD2_DIMENSIONS:
        with conn.cursor() as cur:
            cur.execute(SCD2_NO_CURRENT_VERSION_SQL[table])
            rows = cur.fetchall()
        if rows:
            doubts += len(rows)
            print(f"  {table}: {len(rows)} id(s) whose newest version is already "
                  "closed while an older one is open. After repair they have no "
                  "current version. Restart HA afterwards — the startup snapshot "
                  "re-creates an open row for anything that still exists.")
            for id_value, newest_from, newest_to in rows[:5]:
                print(f"    {id_value}: newest [{newest_from} -> {newest_to})")
            if len(rows) > 5:
                print(f"    ... and {len(rows) - 5} more")

    if not doubts:
        print("  none")
    return doubts


def preview(conn: psycopg.Connection) -> None:
    """Print how many rows --apply would change, changing nothing."""
    for table, _key in SCD2_DIMENSIONS:
        with conn.cursor() as cur:
            rebuild = _scalar(cur, SCD2_REPAIR_REBUILD_PREVIEW_SQL[table])
            clamp = _scalar(cur, SCD2_REPAIR_CLAMP_PREVIEW_SQL[table])
        print(f"  {table}: {rebuild} interval(s) would be rebuilt, {clamp} clamped")


def _backup_name(table: str, stamp: str) -> str:
    return f"{table}_prerepair_{stamp}"


def repair_table(
    conn: psycopg.Connection,
    table: str,
    run_id: str,
    stamp: str,
    collapse_duplicates: bool,
) -> None:
    """Back up and repair one dimension in a single all-or-nothing transaction.

    The table lock is required, not defensive: meta_worker writes on its own
    connection, and a concurrent UPDATE would move a row's ctid out from under the
    rebuild plan, silently skipping that row. lock_timeout means a busy worker
    fails this fast instead of hanging.
    """
    backup = _backup_name(table, stamp)
    with conn.cursor() as cur:
        if _scalar(cur, SCD2_REGCLASS_EXISTS_SQL, (backup,)):
            raise RuntimeError(f"Backup table {backup} already exists; refusing to overwrite")

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(SCD2_REPAIR_LOCK_TIMEOUT_SQL)
            cur.execute(SCD2_REPAIR_LOCK_SQL.format(table=table))
            cur.execute(SCD2_REPAIR_BACKUP_SQL.format(backup=backup, table=table))
            print(f"  {table}: backed up to {backup}")

            if collapse_duplicates:
                cur.execute(SCD2_REPAIR_COLLAPSE_SQL[table], (run_id,))
                if cur.rowcount:
                    print(f"  {table}: collapsed {cur.rowcount} byte-identical duplicate(s)")

            # Archive before clamping — the clamp discards the recorded close
            # time, so capture it while it still exists.
            cur.execute(SCD2_REPAIR_CLAMP_ROWS_SQL[table])
            for (row_data,) in cur.fetchall():
                cur.execute(
                    SCD2_QUARANTINE_INSERT_SQL,
                    (run_id, table, str(row_data.get(_id_column(table))),
                     "clamped_inverted_interval", Jsonb(row_data)),
                )

            cur.execute(SCD2_REPAIR_REBUILD_SQL[table])
            rebuilt = cur.rowcount
            cur.execute(SCD2_REPAIR_CLAMP_SQL[table])
            clamped = cur.rowcount
    print(f"  {table}: rebuilt {rebuilt} interval(s), clamped {clamped}")


def _id_column(table: str) -> str:
    return dict(SCD2_DIMENSIONS)[table]


def add_constraints(conn: psycopg.Connection, tables: list[str]) -> list[str]:
    """Add the exclusion constraint to each verified-clean table.

    Returns the tables left unprotected. Falls back to a unique index on open rows
    when btree_gist cannot be installed — that catches duplicate open versions but
    not overlaps between closed ones, so it is a degradation, and it says so.
    """
    have_gist = True
    try:
        with conn.cursor() as cur:
            cur.execute(SCD2_BTREE_GIST_SQL)
    except psycopg.Error as exc:
        have_gist = False
        print(f"  btree_gist unavailable ({exc}); falling back to a unique index on open rows")

    statements = SCD2_EXCLUDE_CONSTRAINT_SQL if have_gist else SCD2_OPEN_UNIQUE_IDX_SQL
    failed: list[str] = []
    for table in tables:
        try:
            with conn.cursor() as cur:
                cur.execute(statements[table])
            print(f"  {table}: invariant enforced")
        except psycopg.Error as exc:
            failed.append(table)
            print(f"  {table}: could not enforce invariant — {exc}")
    return failed


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--dsn", default=None, metavar="DSN",
                   help="PostgreSQL DSN. Auto-detected from HA config if omitted.")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="Diagnose and report what would change. Default.")
    mode.add_argument("--apply", action="store_true",
                      help="Back up, repair, verify, and add constraints.")
    mode.add_argument("--verify-only", action="store_true",
                      help="Check the invariant and exit. Never mutates.")
    p.add_argument("--collapse-duplicates", action="store_true",
                   help="Also delete rows byte-identical to a row that stays "
                        "(archived to scd2_repair_quarantine first). Off by default: "
                        "the repair reaches the invariant without deleting anything.")
    p.add_argument("--no-constraints", action="store_true",
                   help="Repair but do not add the exclusion constraints.")
    p.add_argument("--yes", action="store_true",
                   help="Skip the --apply confirmation prompt (for scripted runs).")
    return p.parse_args()


def confirm_apply(target: str, args) -> bool:
    """Make the operator confirm the target before the first mutation.

    This repairs irreplaceable history in place, and the usual failure is not a
    bug in the SQL — it is running it against the wrong database, or before
    restarting HA onto the corrected write path, which lets the damage
    reappear immediately and wedges the metadata queue against the new
    constraints.
    """
    if args.yes:
        return True
    if not sys.stdin.isatty():
        print("\nRefusing to --apply without a terminal to confirm on. "
              "Re-run with --yes if this is intentional.")
        return False
    print(f"\nAbout to repair {target} in place.")
    print("Confirm you have already updated the integration and restarted HA, so the "
          "corrected write path is live. Repairing under the old code lets the damage "
          "return immediately.")
    print("A full copy of each table is written to <table>_prerepair_<timestamp> first.")
    return input("Type 'yes' to proceed: ").strip().lower() == "yes"


def main() -> int:
    args = parse_args()
    dsn = args.dsn or _detect_pg_dsn()
    apply_changes = args.apply
    verify_only = args.verify_only

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    stamp = run_id

    print(f"DSN: {_redact(dsn)}")
    with psycopg.connect(dsn, autocommit=True) as conn:
        print("\nTarget")
        print_identity(conn)

        print("\nInvariant check")
        results = run_verification(conn)
        clean = print_verification(results)

        fatal = [t for t, r in results.items() if r[_FATAL_CHECK]]
        if fatal:
            print(
                f"\nSTOP: {_FATAL_CHECK} failed for {', '.join(fatal)}. valid_from is the "
                "anchor this repair reconstructs from; if it is unsound the result would "
                "be too. Investigate before repairing."
            )
            return 2

        print("\nAmbiguity")
        ambiguous = report_ambiguity(conn)
        if not any(ambiguous.values()):
            print("  none")

        print("\nFidelity — what the repair cannot decide")
        report_fidelity(conn)

        with conn.cursor() as cur:
            orphans = _scalar(cur, SCD2_STATES_WITHOUT_DIM_SQL)
        print(f"\nCoverage (informational): {orphans} entity_id(s) in states have no "
              "row in entities. Not repairable from here — the next HA start re-creates "
              "an open row for any that still exist.")

        if verify_only:
            print("\nClean." if clean else "\nInvariant VIOLATED.")
            return 0 if clean else 1

        if clean:
            print("\nNothing to repair.")
        elif not apply_changes:
            print("\nWould change")
            preview(conn)
            print("\nDry run — nothing was modified. Re-run with --apply to repair.")
            return 1

        if not apply_changes:
            return 0

        # Confirm before announcing any work, so the operator is never told
        # "Repairing" for a run they then decline.
        if not clean and not confirm_apply(_redact(dsn), args):
            print("Aborted. Nothing was modified.")
            return 1

        with conn.cursor() as cur:
            cur.execute(SCD2_QUARANTINE_DDL_SQL)
            cur.execute(SCD2_QUARANTINE_IDX_SQL)

        if not clean:
            for table, _key in SCD2_DIMENSIONS:
                repair_table(conn, table, run_id, stamp, args.collapse_duplicates)

            print("\nRe-checking")
            results = run_verification(conn)
            clean_now = print_verification(results)
        else:
            clean_now = True

        verified = [t for t, _k in SCD2_DIMENSIONS if not any(results[t].values())]
        unresolved = [t for t, _k in SCD2_DIMENSIONS if any(results[t].values())]

        if not args.no_constraints:
            print("\nEnforcing")
            failed = add_constraints(conn, verified)
            unresolved.extend(failed)

        with conn.cursor() as cur:
            cur.execute(SCD2_QUARANTINE_SUMMARY_SQL, (run_id,))
            rows = cur.fetchall()
        if rows:
            print("\nArchived to scd2_repair_quarantine (run %s)" % run_id)
            for table, reason, count in rows:
                print(f"  {table}: {count} x {reason}")

        if unresolved:
            print(f"\nUnresolved: {', '.join(sorted(set(unresolved)))}. "
                  "Inspect scd2_repair_quarantine.")
            if not clean:
                print(f"Backups retained: <table>_prerepair_{stamp}")
            return 1

        print("\nDone. Invariant holds on all dimensions.")
        if not clean:
            # Only mention backups when some were actually taken — this line is
            # the undo path, and naming tables that do not exist is worse than
            # saying nothing.
            print(f"Backups: <table>_prerepair_{stamp} — drop them once satisfied.")
        return 0 if clean_now else 1


if __name__ == "__main__":
    sys.exit(main())
