"""Refuse to run a destructive DB-backed test against anything but a scratch server.

Both database-backed modules mutate: `test_scd2_invariant_db` drops and recreates
the dimension tables and `states`, and `test_scd2_real_copy` runs the repair. A
mistyped or stale DSN therefore destroys data, and an env var is a single
shell-history line away from pointing at the live instance.

Two independent checks, because either one alone is fooled:

- Where the CLIENT connected. `inet_server_addr()` is the wrong question: a
  containerised Postgres reports its bridge address (172.17.0.2) no matter who
  reached it, which is why an earlier version of this guard had to allow the
  whole 172.16/12 range — and that is exactly where a production container
  answers. `conn.info.host` is what the DSN asked for, so loopback means the
  developer typed loopback.

- How much history is already there. Loopback is not proof of harmlessness: an
  SSH tunnel to production is loopback too. A scratch database has no states to
  speak of, so a populated one is refused whatever its address.

SCD2_ALLOW_DESTRUCTIVE=1 overrides both. That is a deliberate act rather than a
leftover, which is the whole distinction being drawn here.
"""
import os

import psycopg

_OPT_IN = "SCD2_ALLOW_DESTRUCTIVE"
_LOOPBACK = ("127.", "::1", "localhost")

# Two statements, not one CASE: PostgreSQL plans both branches of a CASE, so a
# guarded reference to a missing `states` still fails at plan time.
_STATES_EXISTS_SQL = "SELECT to_regclass('states') IS NOT NULL;"
_EXISTING_STATES_SQL = "SELECT count(*) FROM (SELECT 1 FROM states LIMIT %s) s;"


def assert_disposable(conn: psycopg.Connection, max_states: int | None = 1000) -> None:
    """Raise unless `conn` points at a server this test may destroy.

    max_states bounds how much existing history is compatible with "scratch".
    The count is capped by LIMIT so this stays cheap against a real hypertable.
    Pass None where a large `states` is the point rather than a warning sign —
    the real-copy module loads one deliberately, and it repairs in place behind
    a backup instead of dropping anything, so the address check carries it.
    """
    if os.environ.get(_OPT_IN) == "1":
        return

    host = conn.info.host or "local"
    if not host.startswith(_LOOPBACK):
        msg = (
            f"refusing to run a destructive test against host {host!r}."
            f" Only loopback runs unattended; set {_OPT_IN}=1 if this really is a"
            f" disposable copy."
        )
        raise AssertionError(msg)

    if max_states is None:
        return

    with conn.cursor() as cur:
        cur.execute(_STATES_EXISTS_SQL)
        exists = cur.fetchone()
        if not (exists and exists[0]):
            return
        cur.execute(_EXISTING_STATES_SQL, (max_states + 1,))
        row = cur.fetchone()
    existing = int(row[0]) if row else 0
    if existing > max_states:
        msg = (
            f"refusing to run a destructive test against database"
            f" {conn.info.dbname!r}: it already holds more than {max_states} state"
            f" rows, so it is not a scratch database. Loopback proves nothing here"
            f" — a tunnel to production is loopback too. Set {_OPT_IN}=1 if this"
            f" really is a disposable copy."
        )
        raise AssertionError(msg)
