"""Database-backed tests for the per-minute rollup (issue #45).

The refresh is PL/pgSQL over window functions and generate_series, so only a
real TimescaleDB can answer whether it is right. Each result is checked against
an independent Python integration of the same samples. Skipped when no database
is reachable; see test_scd2_invariant_db.py for how to start one.

DESTRUCTIVE: drops and recreates `states` and the rollup tables, so it refuses
anything but a loopback scratch server (tests/db_guard.py).
"""
import os
import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("psycopg")
import psycopg  # noqa: E402  pyright: ignore[reportMissingImports]

from custom_components.timescaledb_recorder.const import (  # noqa: E402
    NUMERIC_STATE_REGEX,
    STATES_MINUTE_MARK_DIRTY_SQL,
)
from custom_components.timescaledb_recorder.schema import sync_setup_schema  # noqa: E402
from tests.db_guard import assert_disposable  # noqa: E402

DSN = os.environ.get("SCD2_TEST_DSN", "")

pytestmark = [
    pytest.mark.skipif(not DSN, reason="SCD2_TEST_DSN not set"),
    pytest.mark.enable_socket,
]

MINUTE = timedelta(minutes=1)
# Recent enough that the job's tail and day caps apply, old enough to be closed.
BASE = (datetime.now(timezone.utc) - timedelta(days=2)).replace(second=0, microsecond=0)

PLUG = {"entity_id": "sensor.plug_power", "hold": 120, "zero_hold": 30 * 86400}
METER = {"entity_id": "sensor.meter_power", "hold": 300}

_DROP = (
    "DROP TABLE IF EXISTS states_minute_dirty, states_minute_entities, states_minute,"
    " states CASCADE;"
)


def _numeric(state: str) -> bool:
    import re
    return re.match(NUMERIC_STATE_REGEX, state) is not None


def reference(samples, hold, zero_hold, start, end):
    """Per-minute (integral, covered) from first principles, minutes in [start, end)."""
    pts = [(t, float(v)) for t, v in samples if _numeric(v)]
    out = defaultdict(lambda: [0.0, 0.0])
    for i, (t0, v) in enumerate(pts):
        nxt = pts[i + 1][0] if i + 1 < len(pts) else end
        h = zero_hold if (v == 0 and zero_hold) else hold
        t1 = min(nxt, t0 + timedelta(seconds=h), end)
        m = max(t0, start).replace(second=0, microsecond=0)
        while m < t1:
            secs = (min(t1, m + MINUTE) - max(t0, m)).total_seconds()
            if secs > 0:
                out[m][0] += v * secs
                out[m][1] += secs
            m += MINUTE
    return {m: tuple(x) for m, x in out.items() if start <= m < end and x[1] > 0}


@pytest.fixture
def conn():
    with psycopg.connect(DSN, autocommit=True) as c:
        assert_disposable(c)
        c.execute("SELECT delete_job(job_id) FROM timescaledb_information.jobs"
                  " WHERE proc_name = 'states_minute_job'")
        c.execute(_DROP)
        yield c
        c.execute("SELECT delete_job(job_id) FROM timescaledb_information.jobs"
                  " WHERE proc_name = 'states_minute_job'")
        c.execute(_DROP)


def _insert(conn, entity_id, samples):
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO states (last_updated, last_changed, entity_id, state)"
            " VALUES (%s, %s, %s, %s)",
            [(t, t, entity_id, v) for t, v in samples],
        )


def _rollup(conn, entity_id, start, end):
    rows = conn.execute(
        "SELECT minute, integral, covered_seconds FROM states_minute"
        " WHERE entity_id = %s AND minute >= %s AND minute < %s",
        (entity_id, start, end),
    ).fetchall()
    return {m: (i, c) for m, i, c in rows}


def _assert_matches(got, want):
    assert got.keys() == want.keys()
    for m, (integral, covered) in want.items():
        assert got[m][0] == pytest.approx(integral, rel=1e-9, abs=1e-6), m
        assert got[m][1] == pytest.approx(covered, rel=1e-9, abs=1e-6), m


def _plug_samples():
    """Sparse report-on-change plug: zeros held long, non-zero readings held 120 s."""
    rng = random.Random(45)
    t, out = BASE - timedelta(hours=3), []
    while t < BASE + timedelta(hours=6):
        r = rng.random()
        if r < 0.05:
            out.append((t, "unavailable"))
        elif r < 0.35:
            out.append((t, "0"))
        else:
            out.append((t, f"{rng.uniform(-5, 2500):.3f}"))
        t += timedelta(seconds=rng.choice([0.4, 3, 17, 59, 61, 130, 400, 2000]))
    return out


def _meter_samples():
    """~1 Hz meter with a gap longer than its hold, and negative readings."""
    rng = random.Random(7)
    t, out = BASE - timedelta(minutes=10), []
    while t < BASE + timedelta(hours=2):
        if BASE + timedelta(minutes=30) <= t < BASE + timedelta(minutes=50):
            t = BASE + timedelta(minutes=50, seconds=0.25)
        out.append((t, f"{rng.uniform(-4000, 4000):.1f}"))
        t += timedelta(seconds=rng.uniform(0.5, 1.5))
    return out


def test_refresh_matches_reference(conn):
    sync_setup_schema(conn, minute_rollup=[PLUG, METER])
    plug, meter = _plug_samples(), _meter_samples()
    _insert(conn, PLUG["entity_id"], plug)
    _insert(conn, METER["entity_id"], meter)

    # A range starting mid-history exercises the carry-in from before it.
    start, end = BASE + timedelta(minutes=7, seconds=31), BASE + timedelta(hours=5)
    for e in (PLUG, METER):
        conn.execute("SELECT states_minute_refresh(%s, %s, %s)", (e["entity_id"], start, end))
    s, en = start.replace(second=0), end
    _assert_matches(_rollup(conn, PLUG["entity_id"], s, en),
                    reference(plug, 120, 30 * 86400, s, en))
    _assert_matches(_rollup(conn, METER["entity_id"], s, en),
                    reference(meter, 300, None, s, en))


def test_refresh_replaces_previous_rows(conn):
    sync_setup_schema(conn, minute_rollup=[METER])
    _insert(conn, METER["entity_id"], [(BASE, "100")])
    end = BASE + timedelta(minutes=10)
    conn.execute("SELECT states_minute_refresh(%s, %s, %s)", (METER["entity_id"], BASE, end))
    assert len(_rollup(conn, METER["entity_id"], BASE, end)) == 5
    conn.execute("DELETE FROM states")
    conn.execute("SELECT states_minute_refresh(%s, %s, %s)", (METER["entity_id"], BASE, end))
    assert _rollup(conn, METER["entity_id"], BASE, end) == {}


def test_open_minute_is_never_written(conn):
    sync_setup_schema(conn, minute_rollup=[PLUG])
    now = datetime.now(timezone.utc)
    _insert(conn, PLUG["entity_id"], [(now - timedelta(minutes=3), "0")])
    conn.execute("SELECT states_minute_refresh(%s, %s, %s)",
                 (PLUG["entity_id"], now - timedelta(hours=1), now + timedelta(hours=1)))
    newest = conn.execute("SELECT max(minute) FROM states_minute").fetchone()[0]
    assert newest + MINUTE <= now


def test_new_entity_history_is_backfilled_by_the_job(conn):
    sync_setup_schema(conn)
    plug = _plug_samples()
    _insert(conn, PLUG["entity_id"], plug)
    sync_setup_schema(conn, minute_rollup=[PLUG])
    assert conn.execute("SELECT count(*) FROM states_minute_dirty").fetchone()[0] == 1

    conn.execute("CALL states_minute_job(0, '{}')")

    frontier = conn.execute("SELECT filled_until FROM states_minute_entities").fetchone()[0]
    assert frontier is not None
    assert conn.execute("SELECT count(*) FROM states_minute_dirty").fetchone()[0] == 0
    start = plug[0][0].replace(second=0, microsecond=0)
    _assert_matches(_rollup(conn, PLUG["entity_id"], start, frontier),
                    reference(plug, 120, 30 * 86400, start, frontier))


def test_dirty_work_stops_at_budget_and_resumes(conn):
    sync_setup_schema(conn)
    _insert(conn, METER["entity_id"], [(BASE - timedelta(days=d), "1") for d in range(40)])
    sync_setup_schema(conn, minute_rollup=[METER])
    conn.execute("""CALL states_minute_job(0, '{"budget": "0 seconds"}')""")
    remaining = conn.execute("SELECT from_ts, to_ts FROM states_minute_dirty").fetchone()
    # One day per unit, newest first: exactly one day was taken off the top.
    assert remaining[1] < datetime.now(timezone.utc) - timedelta(hours=23)
    conn.execute("CALL states_minute_job(0, '{}')")
    assert conn.execute("SELECT count(*) FROM states_minute_dirty").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM states_minute").fetchone()[0] == 40 * 5


def test_tail_far_behind_is_queued_not_computed_inline(conn):
    sync_setup_schema(conn, minute_rollup=[METER])
    conn.execute("UPDATE states_minute_entities SET filled_until = now() - interval '3 days'")
    conn.execute("""CALL states_minute_job(0, '{"budget": "0 seconds"}')""")
    assert conn.execute("SELECT count(*) FROM states_minute_dirty").fetchone()[0] == 1


def test_changed_hold_rebuilds_and_removed_entity_is_dropped(conn):
    sync_setup_schema(conn, minute_rollup=[PLUG, METER])
    _insert(conn, PLUG["entity_id"], [(BASE, "10")])
    _insert(conn, METER["entity_id"], [(BASE, "10")])
    sync_setup_schema(conn, minute_rollup=[PLUG, METER])  # unchanged: nothing queued
    assert conn.execute("SELECT count(*) FROM states_minute_dirty").fetchone()[0] == 0
    conn.execute("CALL states_minute_job(0, '{}')")
    for e in (PLUG, METER):
        conn.execute("SELECT states_minute_refresh(%s, %s, %s)",
                     (e["entity_id"], BASE, BASE + timedelta(hours=1)))

    sync_setup_schema(conn, minute_rollup=[{**PLUG, "hold": 600}])

    ids = {r[0] for r in conn.execute("SELECT entity_id FROM states_minute").fetchall()}
    assert ids == set()  # plug cleared for rebuild, meter removed
    assert conn.execute(
        "SELECT entity_id, hold_seconds, filled_until FROM states_minute_entities"
    ).fetchall() == [(PLUG["entity_id"], 600, None)]
    conn.execute("CALL states_minute_job(0, '{}')")
    assert len(_rollup(conn, PLUG["entity_id"], BASE, BASE + timedelta(hours=1))) == 10


def test_job_exists_only_while_entities_are_configured(conn):
    jobs = "SELECT count(*) FROM timescaledb_information.jobs WHERE proc_name = 'states_minute_job'"
    sync_setup_schema(conn)
    assert conn.execute(jobs).fetchone()[0] == 0
    sync_setup_schema(conn, minute_rollup=[METER])
    sync_setup_schema(conn, minute_rollup=[METER])
    assert conn.execute(jobs).fetchone()[0] == 1
    sync_setup_schema(conn)
    assert conn.execute(jobs).fetchone()[0] == 0


def test_mark_dirty_covers_rows_behind_the_frontier(conn):
    sync_setup_schema(conn, minute_rollup=[PLUG, METER])
    conn.execute("CALL states_minute_job(0, '{}')")
    conn.execute("DELETE FROM states_minute_dirty")
    since = datetime.now(timezone.utc) - timedelta(hours=2)
    conn.execute(STATES_MINUTE_MARK_DIRTY_SQL, {"from": since, "to": None})
    rows = conn.execute(
        "SELECT entity_id, from_ts, to_ts FROM states_minute_dirty ORDER BY entity_id"
    ).fetchall()
    assert [r[0] for r in rows] == [METER["entity_id"], PLUG["entity_id"]]
    assert all(r[1] == since for r in rows)

    conn.execute("DELETE FROM states_minute_dirty")
    newest = since + timedelta(minutes=5)
    conn.execute(STATES_MINUTE_MARK_DIRTY_SQL, {"from": since, "to": newest})
    to = dict(conn.execute("SELECT entity_id, to_ts FROM states_minute_dirty").fetchall())
    assert to[METER["entity_id"]] == newest + timedelta(seconds=300)
    assert to[PLUG["entity_id"]] > newest  # zero_hold reaches past now: capped there

    # Rows ahead of the frontier are the tail's job.
    conn.execute("DELETE FROM states_minute_dirty")
    conn.execute(STATES_MINUTE_MARK_DIRTY_SQL,
                 {"from": datetime.now(timezone.utc) + timedelta(minutes=5), "to": None})
    assert conn.execute("SELECT count(*) FROM states_minute_dirty").fetchone()[0] == 0
