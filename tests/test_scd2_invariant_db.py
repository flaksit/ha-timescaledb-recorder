"""Database-backed tests for the SCD2 invariant and its repair (issue #17).

These need a real PostgreSQL/TimescaleDB. Nothing here can be expressed against a
mock: the defects are an ordering property of the real executor and a set of SQL
semantics (window frames, tstzrange, exclusion constraints) that only a server can
answer. Skipped automatically when no database is reachable.

    docker run -d --name scd2-test -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=hatest \
        -p 127.0.0.1:5599:5432 timescale/timescaledb:latest-pg16
    SCD2_TEST_DSN=postgresql://postgres:pw@127.0.0.1:5599/hatest uv run pytest \
        tests/test_scd2_invariant_db.py

To run against a copy of real data, restore the four dimension tables into that
database first — the assertions are written to hold for any history, not just the
synthetic fixture.
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

DSN = os.environ.get("SCD2_TEST_DSN")

pytestmark = [
    pytest.mark.skipif(not DSN, reason="SCD2_TEST_DSN not set"),
    # pytest-socket blocks network access for the HA test harness by default.
    pytest.mark.enable_socket,
]

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _t(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


# Verbatim from issue #17's evidence block: two versions left open, and the
# interval [441294, 442561) overlapping the version starting at 441867.
EID = "sensor.electricity_meter_grid_power"
EVIDENCE_ROWS = [
    (EID, "Grid Power", _t("2026-03-06 18:30:40.857603+00:00"),
     _t("2026-04-15 11:58:23.441294+00:00")),
    (EID, "Power", _t("2026-04-15 11:58:23.441294+00:00"),
     _t("2026-04-15 11:58:23.442561+00:00")),
    (EID, "Grid Power", _t("2026-04-15 11:58:23.441867+00:00"), None),
    (EID, "Grid Power", _t("2026-04-15 11:58:23.442561+00:00"), None),
]

OTHER_DAMAGE = [
    # valid_to from the worker clock overruns the successor's valid_from
    ("sensor.overlap", "A", BASE, BASE + timedelta(seconds=5)),
    ("sensor.overlap", "B", BASE + timedelta(seconds=1), None),
    # inverted interval — tstzrange() raises on this one
    ("sensor.inverted", "A", BASE + timedelta(hours=1), BASE),
    # A second version of the same id, so range_overlap's `a.ctid < b.ctid`
    # self-join actually pairs the inverted row and evaluates tstzrange() on it.
    # With only one version the pair never forms and the raise stays invisible.
    ("sensor.inverted", "B", BASE + timedelta(hours=2), None),
    # legitimate remove-then-recreate gap; the repair must preserve it
    ("sensor.gap", "A", BASE, BASE + timedelta(days=1)),
    ("sensor.gap", "A2", BASE + timedelta(days=4), None),
    # byte-identical twins at one valid_from
    ("sensor.twins", "Same", BASE, None),
    ("sensor.twins", "Same", BASE, None),
    # same valid_from, different payloads — genuinely ambiguous
    ("sensor.ambig", "X", BASE, None),
    ("sensor.ambig", "Y", BASE, None),
    # already-correct history; must come through untouched
    ("sensor.clean", "A", BASE, BASE + timedelta(days=1)),
    ("sensor.clean", "B", BASE + timedelta(days=1), None),
]


@pytest.fixture
def conn():
    with psycopg.connect(DSN, autocommit=True) as c:
        yield c


@pytest.fixture
def damaged(conn):
    """A schema holding the issue's evidence plus every other damage shape."""
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS entities, devices, areas, labels, states,"
                    " scd2_repair_quarantine CASCADE")
        cur.execute("SELECT tablename FROM pg_tables WHERE tablename LIKE %s",
                    ("%_prerepair_%",))
        for (name,) in cur.fetchall():
            cur.execute(f"DROP TABLE IF EXISTS {name} CASCADE")
        for ddl in (const.CREATE_DIM_ENTITIES_SQL, const.CREATE_DIM_DEVICES_SQL,
                    const.CREATE_DIM_AREAS_SQL, const.CREATE_DIM_LABELS_SQL,
                    const.CREATE_TABLE_SQL, const.SCD2_QUARANTINE_DDL_SQL,
                    const.SCD2_QUARANTINE_IDX_SQL):
            cur.execute(ddl)
        for eid, name, vf, vt in EVIDENCE_ROWS + OTHER_DAMAGE:
            cur.execute(
                "INSERT INTO entities (entity_id, ha_entity_uuid, name, domain,"
                " valid_from, valid_to) VALUES (%s,%s,%s,%s,%s,%s)",
                (eid, f"uuid-{name}", name, eid.split(".")[0], vf, vt))
        for name, vf in (("D", BASE), ("D2", BASE + timedelta(seconds=1))):
            cur.execute("INSERT INTO devices (device_id, name, valid_from, valid_to)"
                        " VALUES (%s,%s,%s,NULL)", ("dev1", name, vf))
    return conn


def _violations(conn) -> dict:
    """Total offending rows per dimension across every verification check."""
    results = repair_scd2.run_verification(conn)
    return {t: sum(v.values()) for t, v in results.items()}


def _repair(conn, run: str = "test", collapse: bool = False) -> None:
    for table, _key in const.SCD2_DIMENSIONS:
        repair_scd2.repair_table(conn, table, run, run, collapse)


def _snapshot(conn) -> list:
    with conn.cursor() as cur:
        cur.execute("SELECT entity_id, name, valid_from, valid_to FROM entities"
                    " ORDER BY entity_id, valid_from, name")
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Diagnosis
# ---------------------------------------------------------------------------

def test_fixture_reproduces_the_reported_damage(damaged):
    """The checks must actually see the corruption, or nothing below proves anything."""
    results = repair_scd2.run_verification(damaged)
    assert results["entities"]["multi_open"] > 0
    assert results["entities"]["sequence"] > 0
    assert results["entities"]["inverted"] == 1
    assert results["devices"]["multi_open"] == 1


def test_verification_does_not_die_on_an_inverted_interval(damaged):
    """The whole script must survive the damage it exists to repair.

    range_overlap builds a tstzrange per row; PostgreSQL raises on an inverted
    one instead of returning it, and that error is not caught anywhere above.
    Running the check unconditionally therefore killed --dry-run, --verify-only
    and --apply alike, before a single row could be clamped.
    """
    # The raw check really does raise on this fixture, or the guard proves nothing.
    range_sql = dict(const.SCD2_VERIFY_CHECKS)["range_overlap"]["entities"]
    with pytest.raises(psycopg.Error):
        with damaged.cursor() as cur:
            cur.execute(range_sql)

    results = repair_scd2.run_verification(damaged)   # must not raise
    assert results["entities"]["range_overlap"] == const.SCD2_CHECK_SKIPPED
    assert not repair_scd2.print_verification(results), "a skipped check is not clean"

    # And once the clamp has run, it is executed for real and comes back zero.
    _repair(damaged)
    assert repair_scd2.run_verification(damaged)["entities"]["range_overlap"] == 0


def test_damaged_dimension_fans_out_fact_rows(damaged):
    """The user-visible symptom: a valid_to IS NULL join multiplies state rows."""
    with damaged.cursor() as cur:
        cur.execute("INSERT INTO states (last_updated, last_changed, entity_id, state)"
                    " VALUES (%s,%s,%s,%s)", (BASE, BASE, EID, "42"))
        cur.execute("SELECT count(*) FROM states s JOIN entities e"
                    " ON e.entity_id = s.entity_id AND e.valid_to IS NULL"
                    " WHERE s.entity_id = %s", (EID,))
        assert cur.fetchone()[0] == 2, "fixture should double this entity's rows"


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------

def test_dry_run_mutates_nothing(damaged):
    before = _snapshot(damaged)
    repair_scd2.preview(damaged)
    repair_scd2.run_verification(damaged)
    repair_scd2.report_ambiguity(damaged)
    assert _snapshot(damaged) == before


def test_repair_establishes_the_invariant(damaged):
    """The acceptance criterion: every check returns zero on every dimension."""
    _repair(damaged)
    assert _violations(damaged) == {t: 0 for t, _k in const.SCD2_DIMENSIONS}


def test_repair_is_idempotent(damaged):
    _repair(damaged, "run1")
    after_first = _snapshot(damaged)
    _repair(damaged, "run2")
    assert _snapshot(damaged) == after_first


def test_repair_deletes_nothing_by_default(damaged):
    """Default repair writes valid_to only — no row may disappear."""
    with damaged.cursor() as cur:
        cur.execute("SELECT count(*) FROM entities")
        before = cur.fetchone()[0]
    _repair(damaged)
    with damaged.cursor() as cur:
        cur.execute("SELECT count(*) FROM entities")
        assert cur.fetchone()[0] == before


def test_repair_never_writes_valid_from(damaged):
    """valid_from is the anchor the reconstruction trusts; it must survive intact."""
    with damaged.cursor() as cur:
        cur.execute("SELECT entity_id, valid_from FROM entities ORDER BY 1, 2")
        before = cur.fetchall()
    _repair(damaged)
    with damaged.cursor() as cur:
        cur.execute("SELECT entity_id, valid_from FROM entities ORDER BY 1, 2")
        assert cur.fetchall() == before


def test_repair_preserves_remove_then_recreate_gap(damaged):
    """A version legitimately closed long before its successor keeps its close
    time — the rebuild shrinks a valid_to but never extends one."""
    _repair(damaged)
    with damaged.cursor() as cur:
        cur.execute("SELECT valid_to FROM entities WHERE entity_id='sensor.gap'"
                    " AND name='A'")
        assert cur.fetchone()[0] == BASE + timedelta(days=1)


def test_repair_leaves_correct_history_untouched(damaged):
    _repair(damaged)
    with damaged.cursor() as cur:
        cur.execute("SELECT name, valid_from, valid_to FROM entities"
                    " WHERE entity_id='sensor.clean' ORDER BY valid_from")
        assert cur.fetchall() == [
            ("A", BASE, BASE + timedelta(days=1)),
            ("B", BASE + timedelta(days=1), None),
        ]


def test_repair_keeps_the_newest_version_open(damaged):
    """Of several simultaneously-open rows, the latest by valid_from survives."""
    _repair(damaged)
    with damaged.cursor() as cur:
        cur.execute("SELECT valid_from FROM entities WHERE entity_id=%s"
                    " AND valid_to IS NULL", (EID,))
        rows = cur.fetchall()
    assert rows == [(_t("2026-04-15 11:58:23.442561+00:00"),)]


def test_ambiguous_versions_are_kept_not_discarded(damaged):
    """Both payloads stay in the table; one collapses to an empty interval."""
    _repair(damaged)
    with damaged.cursor() as cur:
        cur.execute("SELECT name, valid_from, valid_to FROM entities"
                    " WHERE entity_id='sensor.ambig' ORDER BY name")
        rows = cur.fetchall()
    assert len(rows) == 2
    assert sum(1 for _n, vf, vt in rows if vt == vf) == 1


def test_clamped_rows_are_archived_with_full_payload(damaged):
    """The clamp discards a recorded close time, so it is captured beforehand."""
    _repair(damaged)
    with damaged.cursor() as cur:
        cur.execute("SELECT row_data FROM scd2_repair_quarantine"
                    " WHERE reason='clamped_inverted_interval'")
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0]["entity_id"] == "sensor.inverted"


def test_backup_table_is_written_before_mutation(damaged):
    _repair(damaged, "stamped")
    with damaged.cursor() as cur:
        cur.execute("SELECT count(*) FROM entities_prerepair_stamped")
        backup_rows = cur.fetchone()[0]
    assert backup_rows == len(EVIDENCE_ROWS) + len(OTHER_DAMAGE)


def test_repair_refuses_to_overwrite_an_existing_backup(damaged):
    _repair(damaged, "same")
    with pytest.raises(RuntimeError, match="already exists"):
        repair_scd2.repair_table(damaged, "entities", "same", "same", False)


def test_collapse_duplicates_removes_only_byte_identical_rows(damaged):
    _repair(damaged, "c1", collapse=True)
    with damaged.cursor() as cur:
        cur.execute("SELECT count(*) FROM entities WHERE entity_id='sensor.twins'")
        assert cur.fetchone()[0] == 1, "identical twins collapse to one"
        cur.execute("SELECT count(*) FROM entities WHERE entity_id='sensor.ambig'")
        assert cur.fetchone()[0] == 2, "differing payloads must never be deleted"
        cur.execute("SELECT count(*) FROM scd2_repair_quarantine"
                    " WHERE reason='duplicate_identical_payload'")
        assert cur.fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------

def test_constraints_apply_after_repair_and_reject_a_second_open_row(damaged):
    _repair(damaged)
    assert repair_scd2.add_constraints(
        damaged, [t for t, _k in const.SCD2_DIMENSIONS]) == []

    with damaged.cursor() as cur:
        with pytest.raises(psycopg.errors.ExclusionViolation):
            cur.execute("INSERT INTO entities (entity_id, ha_entity_uuid, name,"
                        " domain, valid_from, valid_to) VALUES (%s,%s,%s,%s,%s,NULL)",
                        (EID, "u", "Dup", "sensor", _t("2026-06-01 00:00:00+00:00")))


def test_constraint_accepts_a_legitimate_abutting_handover(damaged):
    """[old_vf, new_vf) then [new_vf, inf) touch but do not overlap. The '[)'
    bounds are what make the corrected close+insert legal."""
    _repair(damaged)
    repair_scd2.add_constraints(damaged, [t for t, _k in const.SCD2_DIMENSIONS])

    handover = _t("2026-07-01 00:00:00+00:00")
    with damaged.cursor() as cur, damaged.transaction():
        cur.execute("UPDATE entities SET valid_to=%s WHERE entity_id=%s"
                    " AND valid_to IS NULL AND valid_from < %s",
                    (handover, EID, handover))
        cur.execute("INSERT INTO entities (entity_id, ha_entity_uuid, name, domain,"
                    " valid_from, valid_to) VALUES (%s,%s,%s,%s,%s,NULL)",
                    (EID, "u", "Renamed", "sensor", handover))

    with damaged.cursor() as cur:
        cur.execute("SELECT count(*) FROM entities WHERE entity_id=%s"
                    " AND valid_to IS NULL", (EID,))
        assert cur.fetchone()[0] == 1


def test_fanout_is_gone_after_repair(damaged):
    """The reported symptom must be measurably fixed."""
    with damaged.cursor() as cur:
        for i in range(10):
            ts = _t("2026-05-01 00:00:00+00:00") + timedelta(minutes=i)
            cur.execute("INSERT INTO states (last_updated, last_changed, entity_id,"
                        " state) VALUES (%s,%s,%s,%s)", (ts, ts, EID, str(i)))
    _repair(damaged)
    with damaged.cursor() as cur:
        cur.execute("SELECT count(*) FROM states WHERE entity_id=%s", (EID,))
        raw = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM states s JOIN entities e"
                    " ON e.entity_id = s.entity_id AND e.valid_to IS NULL"
                    " WHERE s.entity_id = %s", (EID,))
        assert cur.fetchone()[0] == raw


# ---------------------------------------------------------------------------
# Acceptance: the corrected write path, against the constraint
# ---------------------------------------------------------------------------
#
# Production Home Assistant is never involved. These drive the real worker's
# dispatch against the real database with the real SQL and the real constraints
# in place — every component that can exhibit the bug except the event loop,
# whose ordering guarantee is covered in test_registry_listener.py.

@pytest.fixture
def clean_db(conn):
    """Empty, constrained dimensions — the state a fresh install starts in."""
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS entities, devices, areas, labels, states,"
                    " scd2_repair_quarantine CASCADE")
        for ddl in (const.CREATE_DIM_ENTITIES_SQL, const.CREATE_DIM_DEVICES_SQL,
                    const.CREATE_DIM_AREAS_SQL, const.CREATE_DIM_LABELS_SQL,
                    const.CREATE_TABLE_SQL):
            cur.execute(ddl)
    assert repair_scd2.add_constraints(
        conn, [t for t, _k in const.SCD2_DIMENSIONS]) == []
    return conn


@pytest.fixture
def worker(clean_db):
    w = TimescaledbMetaRecorderThread(
        hass=MagicMock(), dsn=DSN, meta_queue=MagicMock(),
        registry_listener=RegistryListener(hass=MagicMock(), meta_queue=MagicMock()),
        stop_event=threading.Event(),
    )
    w._conn = clean_db
    return w


def _entity_item(action, entity_id, name, valid_from, old_id=None):
    return {
        "registry": "entity", "action": action,
        "registry_id": entity_id, "old_id": old_id,
        "params": [entity_id, "uuid-1", name, entity_id.split(".")[0], "zha",
                   None, None, [], None, None, None, valid_from.isoformat(), "{}"],
        "enqueued_at": valid_from.isoformat(),
    }


def test_burst_of_registry_changes_keeps_the_invariant(worker, clean_db):
    """The issue's acceptance criterion: still zero after a burst of changes."""
    worker._write_item_raw(_entity_item("create", "sensor.burst", "n0", BASE))
    for i in range(1, 300):
        worker._write_item_raw(
            _entity_item("update", "sensor.burst", f"n{i}", BASE + timedelta(seconds=i)))

    assert _violations(clean_db) == {t: 0 for t, _k in const.SCD2_DIMENSIONS}
    with clean_db.cursor() as cur:
        cur.execute("SELECT count(*) FROM entities WHERE valid_to IS NULL")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM entities")
        assert cur.fetchone()[0] == 300, "every change should be a version"
    assert worker.integrity_drops == 0


def test_out_of_order_delivery_keeps_the_invariant(worker, clean_db):
    """Deterministic counterpart to the burst.

    A burst only corrupts when it happens to lose the race, which makes it a
    flaky proof. Delivering newest-first forces the exact condition the enqueue
    defect produced, so a regression fails every time rather than occasionally.
    """
    worker._write_item_raw(_entity_item("create", "sensor.ooo", "n0", BASE))
    items = [_entity_item("update", "sensor.ooo", f"n{i}", BASE + timedelta(seconds=i))
             for i in range(1, 40)]
    for item in reversed(items):
        worker._write_item_raw(item)

    assert _violations(clean_db) == {t: 0 for t, _k in const.SCD2_DIMENSIONS}
    with clean_db.cursor() as cur:
        cur.execute("SELECT count(*) FROM entities WHERE valid_to IS NULL")
        assert cur.fetchone()[0] == 1
    # Late items are skipped, not spliced in blind — and the skip is counted, so
    # the loss is diagnosable instead of silent.
    assert worker.out_of_order_skips > 0


def test_in_order_delivery_skips_nothing(worker, clean_db):
    """The counter must stay at zero on the normal path, or it is just noise."""
    worker._write_item_raw(_entity_item("create", "sensor.ordered", "n0", BASE))
    for i in range(1, 20):
        worker._write_item_raw(
            _entity_item("update", "sensor.ordered", f"n{i}", BASE + timedelta(seconds=i)))
    assert worker.out_of_order_skips == 0


def test_replayed_item_does_not_add_a_second_open_row(worker, clean_db):
    """task_done() runs after the write, so a crash in between replays the item.

    The rename path had no idempotency guard and would add a second open row.
    """
    worker._write_item_raw(_entity_item("create", "sensor.old", "n", BASE))
    rename = _entity_item("update", "sensor.new", "n", BASE + timedelta(seconds=1),
                          old_id="sensor.old")
    worker._write_item_raw(rename)
    worker._write_item_raw(rename)  # replay

    with clean_db.cursor() as cur:
        cur.execute("SELECT count(*) FROM entities WHERE entity_id='sensor.new'"
                    " AND valid_to IS NULL")
        assert cur.fetchone()[0] == 1
    assert _violations(clean_db) == {t: 0 for t, _k in const.SCD2_DIMENSIONS}
    # A replay writes nothing precisely because it was already applied. Counting
    # that as a lost registry change would make the counter non-zero after any
    # shutdown that landed mid-item, and it is documented as "should stay 0".
    assert worker.out_of_order_skips == 0


def test_close_and_insert_intervals_abut_exactly(worker, clean_db):
    """One timestamp per change: the closed version ends exactly where its
    successor starts. A second clock read here is what produced 322 overlaps."""
    worker._write_item_raw(_entity_item("create", "sensor.abut", "n0", BASE))
    second = BASE + timedelta(seconds=30)
    worker._write_item_raw(_entity_item("update", "sensor.abut", "n1", second))

    with clean_db.cursor() as cur:
        cur.execute("SELECT valid_from, valid_to FROM entities"
                    " WHERE entity_id='sensor.abut' ORDER BY valid_from")
        rows = cur.fetchall()
    assert rows == [(BASE, second), (second, None)]


def test_delayed_remove_then_recreate_does_not_lose_the_entity(worker, clean_db):
    """The queue can be hours behind — a removal must still close at event time.

    Database down 10:00-12:00; entity removed at 10:00 and re-created at 10:05.
    Both items drain at 12:00. Closing the removal at dequeue time would put
    [.., 12:00) across the re-created [10:05, inf), the constraint would reject
    the re-creation, and the entity would read as permanently removed.
    """
    base = BASE + timedelta(hours=10)
    worker._write_item_raw(_entity_item("create", "sensor.delayed", "n", base))

    removed_at = base + timedelta(minutes=30)
    recreated_at = removed_at + timedelta(minutes=5)
    # Both processed now, long after the events they describe.
    worker._write_item_raw({
        "registry": "entity", "action": "remove", "registry_id": "sensor.delayed",
        "old_id": None, "params": None, "enqueued_at": removed_at.isoformat(),
    })
    worker._write_item_raw(_entity_item("create", "sensor.delayed", "back", recreated_at))

    assert _violations(clean_db) == {t: 0 for t, _k in const.SCD2_DIMENSIONS}
    assert worker.integrity_drops == 0, "the re-creation was rejected"
    with clean_db.cursor() as cur:
        cur.execute("SELECT name, valid_from, valid_to FROM entities"
                    " WHERE entity_id='sensor.delayed' ORDER BY valid_from")
        rows = cur.fetchall()
    assert rows == [("n", base, removed_at), ("back", recreated_at, None)], rows


def test_remove_then_recreate_produces_no_overlap(worker, clean_db):
    worker._write_item_raw(_entity_item("create", "sensor.rm", "n", BASE))
    worker._write_item_raw({
        "registry": "entity", "action": "remove", "registry_id": "sensor.rm",
        "old_id": None, "params": None, "enqueued_at": BASE.isoformat(),
    })
    worker._write_item_raw(
        _entity_item("create", "sensor.rm", "n2", datetime.now(timezone.utc)))

    assert _violations(clean_db) == {t: 0 for t, _k in const.SCD2_DIMENSIONS}


def test_states_flat_leaves_a_gap_unlabelled(damaged):
    """A state inside a gap must come back with NULL metadata, not a guess.

    The view used to synthesise gap-free eras and label such a state from the
    preceding version, which made missing history indistinguishable from present
    history. Now the hole is visible.
    """
    in_gap = BASE + timedelta(days=2)          # inside sensor.gap's [+1d, +4d) hole
    with damaged.cursor() as cur:
        cur.execute("INSERT INTO states (last_updated, last_changed, entity_id, state)"
                    " VALUES (%s,%s,%s,%s)", (in_gap, in_gap, "sensor.gap", "7"))
        cur.execute(const.CREATE_VIEW_STATES_NUMERIC_SQL)
        cur.execute(const.CREATE_VIEW_STATES_FLAT_SQL)

    _repair(damaged)

    with damaged.cursor() as cur:
        cur.execute("SELECT entity_name FROM states_flat"
                    " WHERE entity_id='sensor.gap' AND last_updated=%s", (in_gap,))
        assert cur.fetchall() == [(None,)], "the gap must surface as NULL metadata"
        # The row itself survives — LEFT JOIN, so totals stay honest.
        cur.execute("SELECT count(*) FROM states_flat WHERE entity_id='sensor.gap'")
        assert cur.fetchone()[0] == 1


def test_unregistered_entity_keeps_its_domain(damaged):
    """Entities HA never registered must stay queryable.

    sun.sun, zone.home, conversation.* and YAML automations/helpers live in the
    state machine without an entity-registry entry, so the dimension can never
    hold a row for them — on the reference instance that is 46 entity_ids and
    588627 state rows. Their metadata is genuinely unknown, but the domain is
    right there in the entity_id, and taking it from the dimension instead would
    make `WHERE domain = 'sensor'` drop them without a trace.
    """
    ts = BASE + timedelta(days=1)
    with damaged.cursor() as cur:
        cur.execute("INSERT INTO states (last_updated, last_changed, entity_id, state)"
                    " VALUES (%s,%s,%s,%s)", (ts, ts, "sun.sun", "above_horizon"))
        cur.execute(const.CREATE_VIEW_STATES_NUMERIC_SQL)
        cur.execute(const.CREATE_VIEW_STATES_FLAT_SQL)
        cur.execute("SELECT domain, entity_name FROM states_flat"
                    " WHERE entity_id='sun.sun'")
        rows = cur.fetchall()

    assert rows == [("sun", None)], (
        "domain must survive, entity_name must stay honestly NULL")


def test_merge_flag_closes_a_contradicted_gap(damaged):
    """--merge-identical-gaps turns the two identical rows into one covering both."""
    in_gap = BASE + timedelta(days=2)
    with damaged.cursor() as cur:
        cur.execute("INSERT INTO states (last_updated, last_changed, entity_id, state)"
                    " VALUES (%s,%s,%s,%s)", (in_gap, in_gap, "sensor.gap", "7"))
        # Make the two sensor.gap versions identical, so they qualify.
        cur.execute("UPDATE entities SET name='A' WHERE entity_id='sensor.gap'")
        cur.execute(const.CREATE_VIEW_STATES_NUMERIC_SQL)
        cur.execute(const.CREATE_VIEW_STATES_FLAT_SQL)

    _repair(damaged)
    merged = repair_scd2.merge_identical_gaps(damaged, "mergetest")

    assert merged == 1
    with damaged.cursor() as cur:
        cur.execute("SELECT valid_from, valid_to FROM entities"
                    " WHERE entity_id='sensor.gap'")
        rows = cur.fetchall()
        assert rows == [(BASE, None)], "should be one row spanning both eras"
        cur.execute("SELECT entity_name FROM states_flat"
                    " WHERE entity_id='sensor.gap' AND last_updated=%s", (in_gap,))
        assert cur.fetchall() == [("A",)], "the state is now covered"
        cur.execute("SELECT count(*) FROM scd2_repair_quarantine"
                    " WHERE reason='merged_into_previous_version'")
        assert cur.fetchone()[0] == 1, "the removed row must be archived"
    assert _violations(damaged) == {t: 0 for t, _k in const.SCD2_DIMENSIONS}


def test_merge_flag_leaves_differing_versions_alone(damaged):
    """Only identical payloads merge — a real metadata change must survive."""
    in_gap = BASE + timedelta(days=2)
    with damaged.cursor() as cur:
        cur.execute("INSERT INTO states (last_updated, last_changed, entity_id, state)"
                    " VALUES (%s,%s,%s,%s)", (in_gap, in_gap, "sensor.gap", "7"))
    _repair(damaged)
    # sensor.gap's two versions are named 'A' and 'A2' in the fixture: different.
    assert repair_scd2.merge_identical_gaps(damaged, "mergetest2") == 0
    with damaged.cursor() as cur:
        cur.execute("SELECT count(*) FROM entities WHERE entity_id='sensor.gap'")
        assert cur.fetchone()[0] == 2


def _drop_backups(conn) -> None:
    """Backup names carry a whole-second timestamp, so two runs inside one second
    collide and the script refuses to overwrite. Not what these tests are about."""
    with conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE tablename LIKE %s",
                    ("%_prerepair_%",))
        for (name,) in cur.fetchall():
            cur.execute(f"DROP TABLE IF EXISTS {name} CASCADE")


def _run_main(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr("sys.argv", ["repair_scd2.py", "--dsn", DSN, "--yes", *argv])
    return repair_scd2.main()


def test_opt_in_steps_still_run_on_an_already_clean_database(damaged, monkeypatch):
    """The documented workflow reaches the fidelity flags only after a repair.

    --dry-run, read the Fidelity section, decide, re-run with the flag — by which
    point the first --apply has made the database clean. Gating the opt-in steps
    on the invariant being violated made that second run print "Nothing to
    repair" and exit 0 having done nothing, so the gaps it reports were
    unreachable in practice.
    """
    in_gap = BASE + timedelta(days=2)
    with damaged.cursor() as cur:
        cur.execute("INSERT INTO states (last_updated, last_changed, entity_id, state)"
                    " VALUES (%s,%s,%s,%s)", (in_gap, in_gap, "sensor.gap", "7"))
        # Make sensor.gap's two versions identical, so the pair qualifies to merge.
        cur.execute("UPDATE entities SET name='A' WHERE entity_id='sensor.gap'")

    assert _run_main(monkeypatch, "--apply") == 0
    assert _violations(damaged) == {t: 0 for t, _k in const.SCD2_DIMENSIONS}
    _drop_backups(damaged)

    assert _run_main(monkeypatch, "--apply", "--merge-identical-gaps") == 0
    with damaged.cursor() as cur:
        cur.execute("SELECT valid_from, valid_to FROM entities"
                    " WHERE entity_id='sensor.gap'")
        assert cur.fetchall() == [(BASE, None)], (
            "the follow-up run must merge the contradicted gap")
        cur.execute("SELECT count(*) FROM scd2_repair_quarantine"
                    " WHERE reason='merged_into_previous_version'")
        assert cur.fetchone()[0] == 1


def test_an_extras_only_run_still_writes_the_backup(damaged, monkeypatch):
    """A run that only carries an opt-in flag still deletes rows, so the
    documented undo path — a full copy of every table before anything is
    modified — has to exist for it too."""
    assert _run_main(monkeypatch, "--apply") == 0
    _drop_backups(damaged)

    assert _run_main(monkeypatch, "--apply", "--collapse-duplicates") == 0
    with damaged.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_tables WHERE tablename LIKE %s",
                    ("entities_prerepair_%",))
        assert cur.fetchone()[0] == 1


def test_states_flat_stops_fanning_out_after_repair(damaged):
    """The view now reflects the dimension literally, so the repair fixes it too.

    On damaged data the overlapping versions multiply rows here exactly as they
    do in any other honest join — which is the point: the damage is visible
    rather than papered over. After the repair each state appears once.
    """
    with damaged.cursor() as cur:
        for i in range(10):
            ts = _t("2026-05-01 00:00:00+00:00") + timedelta(minutes=i)
            cur.execute("INSERT INTO states (last_updated, last_changed, entity_id,"
                        " state) VALUES (%s,%s,%s,%s)", (ts, ts, EID, str(i)))
        cur.execute(const.CREATE_VIEW_STATES_NUMERIC_SQL)
        cur.execute(const.CREATE_VIEW_STATES_FLAT_SQL)
        cur.execute("SELECT count(*) FROM states_flat WHERE entity_id=%s", (EID,))
        before = cur.fetchone()[0]
    assert before > 10, "damaged data should fan out through a literal join"

    _repair(damaged)

    with damaged.cursor() as cur:
        cur.execute("SELECT count(*) FROM states_flat WHERE entity_id=%s", (EID,))
        assert cur.fetchone()[0] == 10
