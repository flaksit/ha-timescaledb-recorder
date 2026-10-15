"""Database-backed checks of the one-time canonicalization of dimension rows.

DESTRUCTIVE: drops and recreates entities, devices, areas, labels, states and
schema_migrations. Same guard as the other *_db modules.
"""
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("psycopg")
import psycopg  # noqa: E402  pyright: ignore[reportMissingImports]
from psycopg.rows import dict_row  # noqa: E402  pyright: ignore[reportMissingImports]
from psycopg.types.json import Jsonb  # noqa: E402  pyright: ignore[reportMissingImports]

from custom_components.timescaledb_recorder import const, repair_scd2  # noqa: E402
from custom_components.timescaledb_recorder.meta_worker import (  # noqa: E402
    TimescaledbMetaRecorderThread,
)
from custom_components.timescaledb_recorder.migrations import (  # noqa: E402
    canonicalize_dimension_rows,
    migration_applied,
)
from custom_components.timescaledb_recorder.registry_listener import (  # noqa: E402
    RegistryListener,
    _to_json_safe,
)
from custom_components.timescaledb_recorder.schema import setup_scd2_constraints  # noqa: E402
from tests.db_guard import assert_disposable  # noqa: E402

DSN = os.environ.get("SCD2_TEST_DSN", "")

pytestmark = [
    pytest.mark.skipif(not DSN, reason="SCD2_TEST_DSN not set"),
    pytest.mark.enable_socket,
]

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
MIGRATION = const.CANONICAL_SETS_MIGRATION_ID

# Two hash-seed orders of the same sets, as versions before canonical
# serialization wrote them.
LEGACY_A = {"aliases": "{'b', 'a'}", "identifiers": "{('hue', '2'), ('hue', '1')}",
            "nested": {"x": "frozenset({'q', 'p'})"}, "icon": "{mdi:x}",
            "modified_at": "2026-01-01T00:00:00+00:00"}
LEGACY_B = {"aliases": "{'a', 'b'}", "identifiers": "{('hue', '1'), ('hue', '2')}",
            "nested": {"x": "frozenset({'p', 'q'})"}, "icon": "{mdi:x}",
            "modified_at": "2026-01-02T00:00:00+00:00"}
CANONICAL = {"aliases": ["a", "b"], "identifiers": [["hue", "1"], ["hue", "2"]],
             "nested": {"x": ["p", "q"]}, "icon": "{mdi:x}"}


def _drop(c) -> None:
    with c.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS entities, devices, areas, labels, states,"
                    " schema_migrations, scd2_repair_quarantine CASCADE")
        cur.execute("SELECT tablename FROM pg_tables WHERE tablename LIKE %s",
                    ("%_prerepair_%",))
        for (name,) in cur.fetchall():
            cur.execute(f"DROP TABLE IF EXISTS {name} CASCADE")


@pytest.fixture
def conn():
    with psycopg.connect(DSN, autocommit=True) as c:
        assert_disposable(c)
        _drop(c)
        with c.cursor() as cur:
            for ddl in (const.CREATE_ENTITIES_SQL, const.CREATE_DEVICES_SQL,
                        const.CREATE_AREAS_SQL, const.CREATE_LABELS_SQL,
                        const.CREATE_TABLE_SQL, const.SCHEMA_MIGRATIONS_DDL_SQL,
                        const.SCD2_QUARANTINE_DDL_SQL):
                cur.execute(ddl)
        setup_scd2_constraints(c)
        yield c
        # A plain `states` with rows would make a later sync_setup_schema fail
        # in create_hypertable; later modules recreate what they need.
        _drop(c)


def _entity(cur, eid, vf, vt, extra, labels=None, name="N", uuid="u1"):
    cur.execute(
        "INSERT INTO entities (entity_id, ha_entity_uuid, name, domain, labels,"
        " valid_from, valid_to, extra) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (eid, uuid, name, eid.split(".")[0], labels, vf, vt,
         None if extra is None else Jsonb(extra)))


def _rows(conn, table: str) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"SELECT xmin::text AS xmin, * FROM {table} ORDER BY 2, valid_from")
        return cur.fetchall()


def _constraints(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT conname FROM pg_constraint WHERE conname LIKE 'excl_%%_period'")
        return {r[0] for r in cur.fetchall()}


def test_rewrites_only_legacy_rows_and_only_extra_and_labels(conn):
    with conn.cursor() as cur:
        _entity(cur, "sensor.legacy", T0, T0 + timedelta(days=1), LEGACY_A, ["b", "a"])
        _entity(cur, "sensor.legacy", T0 + timedelta(days=1), None, LEGACY_B, ["a", "b"])
        _entity(cur, "sensor.clean", T0, None, CANONICAL, ["a", "b"])
        _entity(cur, "sensor.no_extra", T0, None, None, None)
        cur.execute("INSERT INTO devices (device_id, name, labels, valid_from, extra)"
                    " VALUES ('d1', 'D', %s, %s, %s)", (["z", "B", "a"], T0, Jsonb(LEGACY_A)))
        cur.execute("INSERT INTO areas (area_id, name, valid_from, extra)"
                    " VALUES ('a1', 'A', %s, %s)", (T0, Jsonb({"aliases": "set()"})))
        cur.execute("INSERT INTO labels (label_id, name, valid_from, extra)"
                    " VALUES ('l1', 'L', %s, %s)", (T0, Jsonb({"x": 1})))
    constraints = _constraints(conn)
    assert len(constraints) == 4
    before = {t: _rows(conn, t) for t, _k in const.SCD2_DIMENSIONS}

    assert canonicalize_dimension_rows(conn) == {
        "entities": 2, "devices": 1, "areas": 1, "labels": 0}

    after = {t: _rows(conn, t) for t, _k in const.SCD2_DIMENSIONS}
    for table, rows in before.items():
        assert len(after[table]) == len(rows)
        for old, new in zip(rows, after[table]):
            keep = {k for k in old if k not in ("xmin", "extra", "labels")}
            assert {k: new[k] for k in keep} == {k: old[k] for k in keep}
    clean, legacy_a, legacy_b, no_extra = after["entities"]
    for row in (legacy_a, legacy_b):
        assert {k: v for k, v in row["extra"].items() if k != "modified_at"} == CANONICAL
    assert legacy_a["labels"] == legacy_b["labels"] == ["a", "b"]
    # Canonical rows are not rewritten at all.
    assert clean["xmin"] == before["entities"][0]["xmin"]
    assert no_extra["xmin"] == before["entities"][3]["xmin"]
    assert after["labels"][0]["xmin"] == before["labels"][0]["xmin"]
    # Python's code point order, not the database collation's.
    assert after["devices"][0]["labels"] == ["B", "a", "z"]
    assert after["areas"][0]["extra"] == {"aliases": []}
    assert _constraints(conn) == constraints
    assert migration_applied(conn, MIGRATION)


def _worker(stop_event=None) -> TimescaledbMetaRecorderThread:
    return TimescaledbMetaRecorderThread(
        hass=MagicMock(), dsn=DSN, meta_queue=MagicMock(),
        registry_listener=RegistryListener(hass=MagicMock()),
        stop_event=stop_event or threading.Event(),
    )


def test_marker_prevents_a_second_run(conn):
    with conn.cursor() as cur:
        _entity(cur, "sensor.x", T0, None, LEGACY_A)
    w = _worker()
    try:
        assert w._migrate() is True
        assert _rows(conn, "entities")[0]["extra"]["aliases"] == ["a", "b"]
        with conn.cursor() as cur:
            cur.execute("UPDATE entities SET extra = %s", (Jsonb(LEGACY_A),))
        assert w._migrate() is True
    finally:
        w.reset_db_connection()
    assert _rows(conn, "entities")[0]["extra"]["aliases"] == "{'b', 'a'}"


def _device_item(entry, valid_from: datetime) -> dict[str, Any]:
    params = RegistryListener(hass=MagicMock())._extract_device_params(entry, valid_from)
    return {"registry": "device", "action": "snapshot", "registry_id": entry.id,
            "old_id": None, "params": _to_json_safe(params),
            "enqueued_at": valid_from.isoformat()}


def test_migrated_rows_match_unchanged_entries_and_real_changes_still_version(conn):
    import attrs
    from homeassistant.helpers import device_registry as dr

    entry = dr.DeviceEntry(
        config_entry_id="e1", id="d1", name="D", created_at=T0, modified_at=T0,
        identifiers={("hue", "3"), ("hue", "1"), ("hue", "2")},
        connections={("mac", "aa:bb:cc:dd:ee:ff"), ("zigbee", "00:11")},
    )
    # The row as the pre-canonical write path stored it.
    legacy = json.loads(json.dumps(
        {k: v for k, v in attrs.asdict(entry).items()
         if k not in ("id", "name", "manufacturer", "model", "area_id", "labels")
         and not k.startswith("_")}, default=str))
    with conn.cursor() as cur:
        cur.execute("INSERT INTO devices (device_id, name, labels, valid_from, extra)"
                    " VALUES ('d1', 'D', '{}', %s, %s)", (T0, Jsonb(legacy)))

    w = _worker()
    try:
        assert w._migrate() is True
        w._write_item_raw(_device_item(entry, T0 + timedelta(days=1)))
        assert len(_rows(conn, "devices")) == 1

        changed = attrs.evolve(entry, identifiers=entry.identifiers | {("hue", "4")})
        w._write_item_raw(_device_item(changed, T0 + timedelta(days=2)))
    finally:
        w.reset_db_connection()
    rows = _rows(conn, "devices")
    assert [(r["valid_from"], r["valid_to"]) for r in rows] == [
        (T0, T0 + timedelta(days=2)), (T0 + timedelta(days=2), None)]
    assert rows[1]["extra"]["identifiers"] == [["hue", str(i)] for i in range(1, 5)]


def test_repair_sees_set_order_only_differences_as_equal(conn):
    gap_start, gap_end = T0 + timedelta(days=1), T0 + timedelta(days=2)
    with conn.cursor() as cur:
        # Two versions split by a gap the states contradict, plus same-start twins;
        # each pair differs only in set order, key order and modified_at.
        _entity(cur, "sensor.gap", T0, gap_start, LEGACY_A, ["b", "a"])
        _entity(cur, "sensor.gap", gap_end, None, LEGACY_B, ["a", "b"])
        _entity(cur, "sensor.twin", T0, T0, LEGACY_A, ["b", "a"])
        _entity(cur, "sensor.twin", T0, None, dict(reversed(LEGACY_A.items())), ["a", "b"])
        cur.execute("INSERT INTO states (last_updated, last_changed, entity_id, state)"
                    " VALUES (%s, %s, 'sensor.gap', 'on')",
                    (gap_start + timedelta(hours=1),) * 2)

    def same_metadata() -> list[bool]:
        with conn.cursor() as cur:
            cur.execute(const.SCD2_SUSPICIOUS_GAPS_SQL)
            return [r[4] for r in cur.fetchall()]

    def identical_twins() -> list[str]:
        with conn.cursor() as cur:
            cur.execute(const.SCD2_REPAIR_IDENTICAL_DUPES_SQL["entities"])
            return [r[0] for r in cur.fetchall()]

    # Precondition: as stored, the set order makes them differ.
    assert same_metadata() == [False]
    assert identical_twins() == []
    assert repair_scd2.rows_canonical(conn) is False

    canonicalize_dimension_rows(conn)

    assert repair_scd2.rows_canonical(conn) is True
    assert same_metadata() == [True]
    assert identical_twins() == ["sensor.twin"]
    repair_scd2.repair_table(conn, "entities", "canon", "canon", True)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM entities WHERE entity_id = 'sensor.twin'")
        assert cur.fetchone()[0] == 1
    assert repair_scd2.merge_identical_gaps(conn, "canon") == 1


def test_repair_refuses_before_the_migration(conn, monkeypatch):
    def run(*argv: str) -> int:
        monkeypatch.setattr("sys.argv", ["repair_scd2.py", "--dsn", DSN, *argv])
        return repair_scd2.main()

    assert run("--dry-run") == 2
    assert run("--verify-only") == 0
    with conn.cursor() as cur:
        cur.execute("DROP TABLE schema_migrations")
    assert run("--dry-run") == 2
    with conn.cursor() as cur:
        cur.execute(const.SCHEMA_MIGRATIONS_DDL_SQL)
        cur.execute(const.SCHEMA_MIGRATION_MARK_SQL, (MIGRATION,))
    assert run("--dry-run") == 0
