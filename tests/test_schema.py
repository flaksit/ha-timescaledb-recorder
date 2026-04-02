"""Tests for the schema module."""
import pytest

from custom_components.ha_timescaledb_recorder.schema import async_setup_schema


async def test_create_schema_executes_all_statements(mock_pool, mock_conn):
    """async_setup_schema must execute exactly 14 SQL statements.

    5 original (ha_states hypertable + compression + index)
    + 4 dimension table DDL
    + 5 dimension table indexes
    = 14 total
    """
    await async_setup_schema(mock_pool)
    assert mock_conn.execute.call_count == 14


async def test_create_schema_order(mock_pool, mock_conn):
    """SQL statements must be executed in the defined order."""
    await async_setup_schema(mock_pool)

    calls = [call.args[0] for call in mock_conn.execute.call_args_list]

    # Original 5 statements (hypertable setup)
    assert "CREATE TABLE" in calls[0]
    assert "create_hypertable" in calls[1]
    assert "timescaledb.compress" in calls[2]
    assert "add_compression_policy" in calls[3]
    assert "CREATE INDEX" in calls[4]
    # Dimension table DDL follows
    assert "dim_entities" in calls[5]
    assert "dim_devices" in calls[6]
    assert "dim_areas" in calls[7]
    assert "dim_labels" in calls[8]


async def test_custom_chunk_interval(mock_pool, mock_conn):
    """Custom chunk_interval_days must appear in the hypertable SQL."""
    await async_setup_schema(mock_pool, chunk_interval_days=14)

    hypertable_sql = mock_conn.execute.call_args_list[1].args[0]
    assert "14 days" in hypertable_sql


async def test_custom_compress_after(mock_pool, mock_conn):
    """Custom compress_after_days must appear in the compression policy SQL."""
    await async_setup_schema(mock_pool, compress_after_days=30)

    policy_sql = mock_conn.execute.call_args_list[3].args[0]
    assert "30 days" in policy_sql


async def test_default_values(mock_pool, mock_conn):
    """Default chunk interval (7 days) and compress after (7 days) are applied."""
    await async_setup_schema(mock_pool)

    calls = [call.args[0] for call in mock_conn.execute.call_args_list]

    assert "7 days" in calls[1]
    assert "7 days" in calls[3]


async def test_dim_tables_ddl_executed(mock_pool, mock_conn):
    """Dimension table DDL must be executed for all four registries (META-01)."""
    await async_setup_schema(mock_pool)

    all_sql = " ".join(call.args[0] for call in mock_conn.execute.call_args_list)

    assert "CREATE TABLE IF NOT EXISTS dim_entities" in all_sql
    assert "CREATE TABLE IF NOT EXISTS dim_devices" in all_sql
    assert "CREATE TABLE IF NOT EXISTS dim_areas" in all_sql
    assert "CREATE TABLE IF NOT EXISTS dim_labels" in all_sql


async def test_dim_indexes_created(mock_pool, mock_conn):
    """Index DDL must be executed for all four dimension tables."""
    await async_setup_schema(mock_pool)

    all_sql = " ".join(call.args[0] for call in mock_conn.execute.call_args_list)

    # Five indexes: compound + partial current-row for entities; one each for the rest
    assert "idx_dim_entities_entity_time" in all_sql
    assert "idx_dim_entities_current" in all_sql
    assert "idx_dim_devices_device_time" in all_sql
    assert "idx_dim_areas_area_time" in all_sql
    assert "idx_dim_labels_label_time" in all_sql
