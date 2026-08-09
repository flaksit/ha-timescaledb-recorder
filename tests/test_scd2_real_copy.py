"""Repair checks against a copy of a real, damaged database (issue #17).

`test_scd2_invariant_db.py` builds synthetic damage. This module makes no
assumptions about the contents at all: point it at a restored copy of a real
instance and every assertion still applies. That is what makes it a meaningful
rehearsal for the one-shot production run.

Load a copy first — dimension tables in full, plus enough of `states` to measure
join fan-out — then:

    SCD2_REAL_COPY_DSN=postgresql://postgres:pw@127.0.0.1:5601/homeassistant \
        uv run pytest tests/test_scd2_real_copy.py

NEVER point this at production: it repairs, and it drops nothing but does write.
The module refuses to run against a host that is not loopback.
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
        with c.cursor() as cur:
            cur.execute("SELECT coalesce(host(inet_server_addr()), 'local')")
            host = cur.fetchone()[0]
        # Loopback / container-local only. The repair mutates; a stray DSN
        # pointing at the live instance must fail here, not halfway through.
        assert host.startswith(("127.", "172.", "::1")) or host == "local", (
            f"refusing to run against non-local host {host!r}")
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
