"""Tests for the schema module."""
import pytest

from custom_components.ha_timescaledb.schema import async_setup_schema


async def test_create_schema_executes_all_statements(mock_pool, mock_conn):
    """async_setup_schema must execute exactly 5 SQL statements."""
    await async_setup_schema(mock_pool)
    assert mock_conn.execute.call_count == 5


async def test_create_schema_order(mock_pool, mock_conn):
    """SQL statements must be executed in the defined order."""
    await async_setup_schema(mock_pool)

    calls = [call.args[0] for call in mock_conn.execute.call_args_list]

    assert "CREATE TABLE" in calls[0]
    assert "create_hypertable" in calls[1]
    assert "timescaledb.compress" in calls[2]
    assert "add_compression_policy" in calls[3]
    assert "CREATE INDEX" in calls[4]


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
