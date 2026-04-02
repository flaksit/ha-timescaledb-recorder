"""Tests for MetadataSyncer — replaces Wave 0 stubs from Plan 01.

Each test exercises one behavioural contract of MetadataSyncer as documented
in RESEARCH.md (Patterns 1-5, Pitfalls 1-6) and CONTEXT.md (D-01 through D-11).
"""
from unittest.mock import AsyncMock, MagicMock, patch, call
from datetime import datetime, timezone

import pytest

from custom_components.ha_timescaledb_recorder.syncer import MetadataSyncer
from custom_components.ha_timescaledb_recorder.const import (
    SCD2_SNAPSHOT_ENTITY_SQL,
    SCD2_CLOSE_ENTITY_SQL,
    SCD2_INSERT_ENTITY_SQL,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_syncer(hass, mock_pool):
    """Return a MetadataSyncer with hass and pool; registries not yet set."""
    return MetadataSyncer(hass=hass, pool=mock_pool)


def _patch_registries(syncer, mock_entity_registry, device_reg=None, area_reg=None, label_reg=None):
    """Directly inject mock registries so tests skip async_start() wiring."""
    syncer._entity_reg = mock_entity_registry
    syncer._device_reg = device_reg or _empty_device_reg()
    syncer._area_reg = area_reg or _empty_area_reg()
    syncer._label_reg = label_reg or _empty_label_reg()


def _empty_device_reg():
    reg = MagicMock()
    reg.devices = MagicMock()
    reg.devices.values = MagicMock(return_value=iter([]))
    return reg


def _empty_area_reg():
    reg = MagicMock()
    reg.async_list_areas = MagicMock(return_value=[])
    return reg


def _empty_label_reg():
    reg = MagicMock()
    reg.async_list_labels = MagicMock(return_value=[])
    return reg


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_initial_snapshot_inserts_all_entities(
    hass, mock_pool, mock_conn, mock_entity_registry
):
    """Snapshot must call SCD2_SNAPSHOT_ENTITY_SQL once per entity.

    - Two entities in mock_entity_registry
    - After _async_snapshot(), mock_conn.execute called exactly twice with
      SCD2_SNAPSHOT_ENTITY_SQL as the first argument
    """
    syncer = _make_syncer(hass, mock_pool)
    # Inject registries directly — two entities, zero devices/areas/labels
    _patch_registries(syncer, mock_entity_registry)

    await syncer._async_snapshot()

    # Collect all first-argument SQL strings from execute calls
    executed_sql = [c.args[0] for c in mock_conn.execute.call_args_list]
    snapshot_calls = [s for s in executed_sql if s == SCD2_SNAPSHOT_ENTITY_SQL]
    assert len(snapshot_calls) == 2, (
        f"Expected 2 snapshot entity calls, got {len(snapshot_calls)}. "
        f"All calls: {executed_sql}"
    )


async def test_initial_snapshot_idempotent_on_restart(
    hass, mock_pool, mock_conn, mock_entity_registry
):
    """Running the snapshot twice must use the WHERE NOT EXISTS SQL, not a plain INSERT.

    The WHERE NOT EXISTS guard in SCD2_SNAPSHOT_ENTITY_SQL ensures that re-running
    the snapshot on HA restart is a no-op at the DB level for existing open rows.
    This test verifies the correct SQL constant is used, not a plain INSERT.
    """
    syncer = _make_syncer(hass, mock_pool)

    # Reset mock registry iterator between calls (iter is consumed after one pass)
    from unittest.mock import MagicMock as MM

    def make_entries():
        from tests.conftest import _make_registry_entry
        return [
            _make_registry_entry(
                entity_id="sensor.living_room_temp",
                id="uuid-entity-001",
                name="Living Room Temperature",
                original_name="Temperature",
                domain="sensor",
                platform="zha",
                device_id="device-001",
                area_id="area-living-room",
                labels={"indoor"},
                device_class="temperature",
                unit_of_measurement="°C",
                disabled_by=None,
            ),
        ]

    reg = MagicMock()
    reg.entities = MagicMock()

    call_count = 0

    def fresh_values():
        nonlocal call_count
        call_count += 1
        return iter(make_entries())

    reg.entities.values = MagicMock(side_effect=fresh_values)
    reg.async_get = MagicMock(side_effect=lambda eid: make_entries()[0])
    _patch_registries(syncer, reg)

    await syncer._async_snapshot()
    await syncer._async_snapshot()

    # Verify the idempotent snapshot SQL is used (not a plain INSERT)
    executed_sql = [c.args[0] for c in mock_conn.execute.call_args_list]
    assert all(
        s == SCD2_SNAPSHOT_ENTITY_SQL for s in executed_sql if "dim_entities" in s
    ), "Snapshot must use SCD2_SNAPSHOT_ENTITY_SQL (WHERE NOT EXISTS), not plain INSERT"
    assert "WHERE NOT EXISTS" in SCD2_SNAPSHOT_ENTITY_SQL


async def test_scd2_close_and_insert(hass, mock_pool, mock_conn, mock_entity_registry):
    """An update event must: (a) close the old row, then (b) insert the new row.

    Both calls must use the same timestamp (SCD2 consistency per D-08).
    """
    syncer = _make_syncer(hass, mock_pool)
    _patch_registries(syncer, mock_entity_registry)

    entity_id = "sensor.living_room_temp"
    await syncer._async_process_entity_event("update", entity_id, None)

    calls = mock_conn.execute.call_args_list
    assert len(calls) == 2, f"Expected 2 execute calls (close + insert), got {len(calls)}"

    close_sql, close_ts, close_eid = calls[0].args
    insert_sql = calls[1].args[0]
    insert_ts = calls[1].args[12]  # valid_from is 12th positional arg ($12)

    assert close_sql == SCD2_CLOSE_ENTITY_SQL
    assert insert_sql == SCD2_INSERT_ENTITY_SQL
    assert close_eid == entity_id
    # Both operations must share the same timestamp (D-08)
    assert close_ts == insert_ts, "Close and insert must use the same timestamp"


async def test_registry_event_create(hass, mock_pool, mock_conn, mock_entity_registry):
    """A create event must trigger only an INSERT — no preceding close."""
    syncer = _make_syncer(hass, mock_pool)
    _patch_registries(syncer, mock_entity_registry)

    await syncer._async_process_entity_event("create", "sensor.living_room_temp", None)

    calls = mock_conn.execute.call_args_list
    assert len(calls) == 1, f"Expected 1 execute call (insert only), got {len(calls)}"
    assert calls[0].args[0] == SCD2_INSERT_ENTITY_SQL
    assert SCD2_CLOSE_ENTITY_SQL not in [c.args[0] for c in calls]


async def test_registry_event_remove(hass, mock_pool, mock_conn, mock_entity_registry):
    """A remove event must close the open row without fetching from registry.

    Registry.async_get must NOT be called — the entry is already gone (Pitfall 4).
    """
    syncer = _make_syncer(hass, mock_pool)
    _patch_registries(syncer, mock_entity_registry)

    entity_id = "sensor.living_room_temp"
    await syncer._async_process_entity_event("remove", entity_id, None)

    calls = mock_conn.execute.call_args_list
    assert len(calls) == 1, f"Expected 1 execute call (close only), got {len(calls)}"
    assert calls[0].args[0] == SCD2_CLOSE_ENTITY_SQL
    assert SCD2_INSERT_ENTITY_SQL not in [c.args[0] for c in calls]

    # Registry.async_get must NOT be called on remove (Pitfall 4)
    mock_entity_registry.async_get.assert_not_called()


async def test_entity_rename_scd2(hass, mock_pool, mock_conn, mock_entity_registry):
    """A rename event must close the old entity_id row AND insert a new row.

    Rename arrives as action=update with old_entity_id in event data (D-09, Pitfall 2).
    """
    syncer = _make_syncer(hass, mock_pool)
    _patch_registries(syncer, mock_entity_registry)

    old_entity_id = "sensor.old_name"
    new_entity_id = "sensor.living_room_temp"

    # Add old_entity_id to the registry mock for the fetch after rename
    old_entry = MagicMock()
    old_entry.entity_id = new_entity_id  # after rename, registry has the new entity_id
    old_entry.id = "uuid-entity-001"
    old_entry.name = "Living Room Temperature"
    old_entry.original_name = "Temperature"
    old_entry.platform = "zha"
    old_entry.device_id = "device-001"
    old_entry.area_id = "area-living-room"
    old_entry.labels = {"indoor"}
    old_entry.device_class = "temperature"
    old_entry.unit_of_measurement = "°C"
    old_entry.disabled_by = None

    mock_entity_registry.async_get = MagicMock(return_value=old_entry)

    await syncer._async_process_entity_event("update", new_entity_id, old_entity_id)

    calls = mock_conn.execute.call_args_list
    assert len(calls) == 2, f"Expected 2 execute calls (close old + insert new), got {len(calls)}"

    close_sql, _ts, closed_eid = calls[0].args
    insert_sql = calls[1].args[0]
    inserted_eid = calls[1].args[1]  # $1 = entity_id

    assert close_sql == SCD2_CLOSE_ENTITY_SQL
    assert closed_eid == old_entity_id, (
        f"Close must target old_entity_id '{old_entity_id}', got '{closed_eid}'"
    )
    assert insert_sql == SCD2_INSERT_ENTITY_SQL
    assert inserted_eid == new_entity_id


async def test_area_reorder_ignored(hass, mock_pool, mock_conn, mock_area_registry):
    """An area event with action=reorder must be a no-op (Pitfall 6)."""
    syncer = _make_syncer(hass, mock_pool)
    _patch_registries(syncer, _empty_entity_reg(), area_reg=mock_area_registry)

    await syncer._async_process_area_event("reorder", None)

    mock_conn.execute.assert_not_called()


def _empty_entity_reg():
    reg = MagicMock()
    reg.entities = MagicMock()
    reg.entities.values = MagicMock(return_value=iter([]))
    reg.async_get = MagicMock(return_value=None)
    return reg


async def test_labels_serialized_as_list(
    hass, mock_pool, mock_conn, mock_entity_registry
):
    """Entity labels (set[str]) must be converted to list before passing to asyncpg.

    asyncpg raises TypeError when a Python set is passed as TEXT[] (Pitfall 5).
    Verify the value reaching conn.execute for the labels parameter is a list.
    """
    syncer = _make_syncer(hass, mock_pool)
    _patch_registries(syncer, mock_entity_registry)

    # Use the first entity which has labels={"indoor", "climate"}
    await syncer._async_process_entity_event("create", "sensor.living_room_temp", None)

    calls = mock_conn.execute.call_args_list
    assert len(calls) == 1

    # $8 = labels (8th positional arg, index 8 in args tuple — SQL + 7 typed fields before it)
    # args: (sql, entity_id, ha_entity_uuid, name, domain, platform, device_id, area_id, labels, ...)
    # index:   0        1           2          3      4        5         6          7       8
    labels_arg = calls[0].args[8]
    assert isinstance(labels_arg, list), (
        f"labels must be passed as list to asyncpg, got {type(labels_arg).__name__}: {labels_arg!r}"
    )
