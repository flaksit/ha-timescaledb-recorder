"""Repair checks against a copy of a real, damaged database (issue #17).

`test_scd2_invariant_db.py` builds synthetic damage. This module assumes nothing
about WHICH entities or metadata a copy contains — every assertion is written
against whatever is there. It does assume the copy is damaged, since a clean one
would make the before/after comparisons vacuous, and it says so in its first two
tests rather than passing quietly.

Load a copy first — dimension tables in full, plus enough of `states` to measure
join fan-out — then:

    SCD2_REAL_COPY_DSN=postgresql://postgres:pw@127.0.0.1:5601/homeassistant \
        uv run pytest tests/test_scd2_real_copy.py

The tests run in order and share one connection: the module repairs the copy in
place, so each test builds on the previous one's result. Selecting a single test
out of the middle proves less than running the module.

NEVER point this at production: it repairs, and it drops nothing but does write.
The module refuses to run against anything but a loopback server unless
SCD2_ALLOW_DESTRUCTIVE=1 says the target really is disposable.
"""
import os
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

pytest.importorskip("psycopg")
import psycopg  # noqa: E402  pyright: ignore[reportMissingImports]

from custom_components.timescaledb_recorder import const, repair_scd2  # noqa: E402
from custom_components.timescaledb_recorder.meta_worker import (  # noqa: E402
    TimescaledbMetaRecorderThread,
)
from custom_components.timescaledb_recorder.registry_listener import (  # noqa: E402
    RegistryListener,
)
from tests.db_guard import assert_disposable  # noqa: E402

DSN = os.environ.get("SCD2_REAL_COPY_DSN")

pytestmark = [
    pytest.mark.skipif(not DSN, reason="SCD2_REAL_COPY_DSN not set"),
    pytest.mark.enable_socket,
]

# Unique per run: the repair refuses to overwrite an existing backup table (by
# design — it is the undo path), so a fixed stamp would make this module
# runnable exactly once against a given copy.
_RUN = datetime.now(timezone.utc).strftime("pytest%Y%m%d%H%M%S")


def _clean() -> dict:
    return {t: 0 for t, _key in const.SCD2_DIMENSIONS}


@pytest.fixture(scope="module")
def conn():
    with psycopg.connect(DSN, autocommit=True) as c:
        # The repair mutates; a stray DSN pointing at the live instance must
        # fail here, not halfway through. The old check asked the SERVER for its
        # address and accepted 172.16/12, which is the Docker bridge — precisely
        # where a production container answers. This asks where the client was
        # pointed instead. No size check: a large `states` is what this module
        # wants, and the repair works behind a backup rather than dropping.
        assert_disposable(c, max_states=None)
        yield c


@pytest.fixture(scope="module")
def baseline(conn):
    """Damage census taken before anything is modified."""
    return {
        "violations": _violations(conn),
        "rows": _row_counts(conn),
        "valid_from": _valid_from_checksum(conn),
        "fanout": _fanout(conn),
        "states_flat": _states_flat_count(conn),
        "gaps": _gap_set(conn),
    }


def _violations(conn) -> dict:
    return {t: sum(v.values()) for t, v in repair_scd2.run_verification(conn).items()}


def _row_counts(conn) -> dict:
    out = {}
    with conn.cursor() as cur:
        for table, _key in const.SCD2_DIMENSIONS:
            cur.execute(f"SELECT count(*) FROM {table}")
            out[table] = cur.fetchone()[0]
    return out


def _gap_set(conn) -> set:
    """Every period an entity's history leaves uncovered, as (id, start, end).

    "The repair preserves gaps" was asserted only on synthetic data, where the
    fixture has exactly one. On real history the rebuild touches thousands of
    intervals, and shrink-only is what stops it erasing a legitimate
    remove-then-recreate gap — so the claim needs checking against data that
    actually has them.

    greatest(...) mirrors the clamp and the window tiebreak mirrors the rebuild
    plan, so a gap is measured the same way before and after.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT entity_id, gap_start, gap_end FROM (
                SELECT entity_id,
                       greatest(valid_to, valid_from) AS gap_start,
                       lead(valid_from) OVER (
                           PARTITION BY entity_id
                           ORDER BY valid_from, (valid_to IS NULL), valid_to, ctid
                       ) AS gap_end
                  FROM entities
                 WHERE valid_to IS NOT NULL) g
             WHERE gap_end IS NOT NULL AND gap_start < gap_end""")
        return set(cur.fetchall())


def _eras(conn) -> dict:
    """Every recorded interval per entity_id, open rows as (from, None)."""
    out: dict = {}
    with conn.cursor() as cur:
        cur.execute("SELECT entity_id, valid_from, valid_to FROM entities")
        for entity_id, valid_from, valid_to in cur.fetchall():
            out.setdefault(entity_id, []).append((valid_from, valid_to))
    return out


def _covers(eras: list, start, end) -> bool:
    """Does any era overlap [start, end)?"""
    return any(era_from < end and (era_to is None or era_to > start)
               for era_from, era_to in eras)


def _valid_from_checksum(conn) -> dict:
    """Fingerprint of every (id, valid_from) pair — the field repair must not write."""
    out = {}
    with conn.cursor() as cur:
        for table, key in const.SCD2_DIMENSIONS:
            cur.execute(
                f"SELECT md5(string_agg({key}||'|'||valid_from::text, E'\\n'"
                f" ORDER BY {key}, valid_from)) FROM {table}")
            out[table] = cur.fetchone()[0]
    return out


def _fanout(conn):
    """Entities whose `valid_to IS NULL` join returns more rows than they have.

    Counting duplicates only. An entity removed from the registry legitimately
    drops out of this join, which is a different (documented) limitation.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT s.entity_id, count(*) AS joined,
                   (SELECT count(*) FROM states x WHERE x.entity_id = s.entity_id) AS actual
            FROM states s JOIN entities e
              ON e.entity_id = s.entity_id AND e.valid_to IS NULL
            GROUP BY s.entity_id
            HAVING count(*) > (SELECT count(*) FROM states x WHERE x.entity_id = s.entity_id)
            ORDER BY 2 DESC""")
        return cur.fetchall()


def _states_flat_count(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('states_flat') IS NOT NULL")
        if not cur.fetchone()[0]:
            return None
        cur.execute("SELECT count(*) FROM states_flat")
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Ordered: the module repairs the copy in place, so these run as a sequence.
# ---------------------------------------------------------------------------

def test_copy_is_actually_damaged(baseline):
    """A clean copy would make every later assertion vacuous."""
    assert sum(baseline["violations"].values()) > 0, (
        "the loaded copy shows no damage — reload it from the damaged source")


def test_copy_exhibits_the_reported_fanout(baseline):
    """The user-visible symptom must be present before repair."""
    assert baseline["fanout"], "no entity fans out; copy may predate the corruption"
    for _eid, joined, actual in baseline["fanout"]:
        assert joined > actual


def test_repair_converges_on_real_history(conn, baseline):
    for table, _key in const.SCD2_DIMENSIONS:
        repair_scd2.repair_table(conn, table, _RUN, _RUN, False)
    assert _violations(conn) == _clean()


def test_repair_wrote_no_valid_from(conn, baseline):
    """The anchor the whole reconstruction trusts must come through untouched."""
    assert _valid_from_checksum(conn) == baseline["valid_from"]


def test_repair_deleted_nothing(conn, baseline):
    assert _row_counts(conn) == baseline["rows"]


def test_repair_preserved_every_gap(conn, baseline):
    """Shrink-only means a gap can widen but must never be filled.

    A gap is a period the dimension says nothing existed. Closing one would
    invent metadata continuity, and on real history that would be thousands of
    silent inventions rather than the single synthetic case the other module
    covers. Checked as coverage rather than as equal tuples: the rebuild moves a
    gap's start earlier when it shrinks an overrunning interval, which widens
    the gap without filling any of it.
    """
    eras = _eras(conn)
    filled = [(eid, start, end) for eid, start, end in baseline["gaps"]
              if _covers(eras.get(eid, []), start, end)]
    assert filled == [], f"{len(filled)} gap(s) were filled in by the repair"


def test_fanout_is_gone(conn):
    assert _fanout(conn) == []


def test_states_flat_returns_one_row_per_state(conn, baseline):
    """The view joins the recorded interval, so the repair fixes it too.

    On the damaged copy the overlapping versions multiply rows here; afterwards
    each state appears exactly once. A view that ignored valid_to would have
    looked correct throughout and told you nothing.
    """
    if baseline["states_flat"] is None:
        pytest.skip("states_flat view not present in the copy")
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM states")
        states = cur.fetchone()[0]
    assert baseline["states_flat"] > states, "damaged copy should have fanned out"
    assert _states_flat_count(conn) == states


def test_repair_is_idempotent(conn):
    before = _valid_from_checksum(conn), _row_counts(conn)
    for table, _key in const.SCD2_DIMENSIONS:
        repair_scd2.repair_table(conn, table, _RUN + "b", _RUN + "b", False)
    assert (_valid_from_checksum(conn), _row_counts(conn)) == before
    assert _violations(conn) == _clean()


def test_constraints_apply_to_repaired_real_history(conn):
    assert repair_scd2.add_constraints(
        conn, [t for t, _key in const.SCD2_DIMENSIONS]) == []


def test_burst_on_real_history_keeps_the_invariant(conn):
    """Issue #17's acceptance criterion, on genuine history with constraints live.

    Includes a rename and a remove/re-create, since those are the paths that
    previously had no idempotency guard.
    """
    worker = TimescaledbMetaRecorderThread(
        hass=MagicMock(), dsn=DSN, meta_queue=MagicMock(),
        registry_listener=RegistryListener(hass=MagicMock(), meta_queue=MagicMock()),
        stop_event=threading.Event(),
    )
    worker._conn = conn

    with conn.cursor() as cur:
        cur.execute("SELECT entity_id FROM entities WHERE valid_to IS NULL"
                    " ORDER BY entity_id LIMIT 4")
        targets = [r[0] for r in cur.fetchall()]
    assert targets, "copy has no open entity versions to drive"

    now = datetime.now(timezone.utc)
    before = _row_counts(conn)["entities"]

    def item(action, entity_id, name, vf, old_id=None):
        return {
            "registry": "entity", "action": action, "registry_id": entity_id,
            "old_id": old_id,
            "params": [entity_id, "uuid-x", name, entity_id.split(".")[0], "zha",
                       None, None, [], None, None, None, vf.isoformat(), "{}"],
            "enqueued_at": vf.isoformat(),
        }

    n = 0
    for round_i in range(50):
        for target in targets:
            n += 1
            worker._write_item_raw(
                item("update", target, f"burst-{round_i}", now + timedelta(seconds=n)))

    n += 1
    worker._write_item_raw(item("update", "sensor.scd2_renamed", "R",
                                now + timedelta(seconds=n), old_id=targets[0]))
    n += 1
    worker._write_item_raw({"registry": "entity", "action": "remove",
                            "registry_id": targets[1], "old_id": None, "params": None,
                            "enqueued_at": (now + timedelta(seconds=n)).isoformat()})
    n += 1
    worker._write_item_raw(item("create", targets[1], "back", now + timedelta(seconds=n)))

    assert _violations(conn) == _clean()
    assert worker.integrity_drops == 0, "the constraint rejected a legitimate write"
    assert worker.out_of_order_skips == 0, "in-order items were skipped"
    assert _row_counts(conn)["entities"] > before, "the burst wrote nothing"
    assert _fanout(conn) == []
