"""Test stubs for MetadataSyncer — implemented in Plan 02.

All tests are skipped (Wave 0) so the full suite stays green while the
implementation is pending.  Each stub documents the exact contract that
Plan 02 must satisfy, derived from RESEARCH.md and CONTEXT.md decision
records.
"""
import pytest


async def test_initial_snapshot_inserts_all_entities(
    hass, mock_pool, mock_conn, mock_entity_registry
):
    """Snapshot must write one open row (valid_to IS NULL) per entity.

    - Two entities in mock_entity_registry
    - After async_start(), two SCD2_SNAPSHOT_ENTITY_SQL calls expected
    - Each row has valid_to=NULL (open / current row)
    """
    pytest.skip("Wave 0 stub — implemented in Plan 02")


async def test_initial_snapshot_idempotent_on_restart(
    hass, mock_pool, mock_conn, mock_entity_registry
):
    """Running the snapshot twice must not create duplicate open rows.

    SCD2_SNAPSHOT_ENTITY_SQL uses WHERE NOT EXISTS, so re-executing for an
    entity that already has an open row is a no-op at the SQL level.
    The test verifies the SQL constant is used (not a plain INSERT).
    """
    pytest.skip("Wave 0 stub — implemented in Plan 02")


async def test_scd2_close_and_insert(hass, mock_pool, mock_conn, mock_entity_registry):
    """An update event must: (a) close the old row, (b) insert the new row.

    Processing sequence for action=update:
    1. conn.execute(SCD2_CLOSE_ENTITY_SQL, timestamp, entity_id)
    2. conn.execute(SCD2_INSERT_ENTITY_SQL, ..., valid_from=timestamp, valid_to=NULL)
    Both calls must use the same timestamp for SCD2 consistency (D-08).
    """
    pytest.skip("Wave 0 stub — implemented in Plan 02")


async def test_registry_event_create(hass, mock_pool, mock_conn, mock_entity_registry):
    """A create event must trigger an INSERT with no preceding close.

    action=create → SCD2_INSERT_ENTITY_SQL only (no SCD2_CLOSE_ENTITY_SQL call).
    """
    pytest.skip("Wave 0 stub — implemented in Plan 02")


async def test_registry_event_remove(hass, mock_pool, mock_conn, mock_entity_registry):
    """A remove event must close the open row without fetching from registry.

    action=remove → SCD2_CLOSE_ENTITY_SQL only (no insert, no async_get call).
    Registry.async_get must NOT be called — the entry is already gone (Pitfall 4).
    """
    pytest.skip("Wave 0 stub — implemented in Plan 02")


async def test_entity_rename_scd2(hass, mock_pool, mock_conn, mock_entity_registry):
    """A rename event must close the old entity_id row AND insert a new row.

    Rename arrives as action=update with old_entity_id in event data (D-09).
    Expected calls:
    1. SCD2_CLOSE_ENTITY_SQL with old_entity_id (not new entity_id)
    2. SCD2_INSERT_ENTITY_SQL with new entity_id
    Pitfall 2: a handler that only sees entity_id would never close the old row.
    """
    pytest.skip("Wave 0 stub — implemented in Plan 02")


async def test_area_reorder_ignored(hass, mock_pool, mock_conn, mock_area_registry):
    """An area event with action=reorder must be a no-op.

    The reorder event carries area_id=None (Pitfall 6).  The handler must
    early-return without calling any SQL.
    """
    pytest.skip("Wave 0 stub — implemented in Plan 02")


async def test_labels_serialized_as_list(
    hass, mock_pool, mock_conn, mock_entity_registry
):
    """Entity labels (set[str]) must be converted to list before passing to asyncpg.

    asyncpg raises a TypeError when a Python set is passed as a TEXT[] parameter
    (Pitfall 5).  This test verifies that the value reaching conn.execute is a
    list, not a set.
    """
    pytest.skip("Wave 0 stub — implemented in Plan 02")
