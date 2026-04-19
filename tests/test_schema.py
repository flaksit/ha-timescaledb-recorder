"""Tests for the schema module (psycopg3 sync API)."""
import pytest

from custom_components.ha_timescaledb_recorder.schema import sync_setup_schema


def test_create_schema_executes_all_statements(mock_psycopg_conn):
    """sync_setup_schema must execute exactly 15 SQL statements.

    6 hypertable setup statements (CREATE TABLE, create_hypertable, SET compression,
    remove_compression_policy, add_compression_policy, CREATE INDEX)
    + 4 dimension table DDL (entities, devices, areas, labels)
    + 5 dimension table indexes (entities compound, entities current-row,
      devices, areas, labels)
    = 15 total

    The cursor is obtained via conn.cursor() context manager in sync_setup_schema.
    """
    conn, cur = mock_psycopg_conn
    sync_setup_schema(conn)
    assert cur.execute.call_count == 15


def test_create_schema_order(mock_psycopg_conn):
    """SQL statements must be executed in the defined order."""
    conn, cur = mock_psycopg_conn
    sync_setup_schema(conn)

    calls = [call.args[0] for call in cur.execute.call_args_list]

    # 6 hypertable setup statements
    assert "CREATE TABLE" in calls[0]
    assert "create_hypertable" in calls[1]
    assert "timescaledb.compress" in calls[2]
    assert "remove_compression_policy" in calls[3]
    assert "add_compression_policy" in calls[4]
    assert "CREATE INDEX" in calls[5]
    # Dimension table DDL follows
    assert "dim_entities" in calls[6]
    assert "dim_devices" in calls[7]
    assert "dim_areas" in calls[8]
    assert "dim_labels" in calls[9]


def test_custom_chunk_interval(mock_psycopg_conn):
    """Custom chunk_interval_days must appear in the hypertable SQL."""
    conn, cur = mock_psycopg_conn
    sync_setup_schema(conn, chunk_interval_days=14)

    hypertable_sql = cur.execute.call_args_list[1].args[0]
    assert "14 days" in hypertable_sql


def test_custom_compress_after(mock_psycopg_conn):
    """Custom compress_after_hours must appear in the compression policy SQL."""
    conn, cur = mock_psycopg_conn
    sync_setup_schema(conn, compress_after_hours=48)

    policy_sql = cur.execute.call_args_list[4].args[0]
    assert "48 hours" in policy_sql


def test_schedule_interval_is_half_compress_after(mock_psycopg_conn):
    """schedule_interval must equal max(1, min(12, compress_after_hours // 2))."""
    conn, cur = mock_psycopg_conn

    # compress_after=2h → schedule=1h
    sync_setup_schema(conn, compress_after_hours=2)
    policy_sql = cur.execute.call_args_list[4].args[0]
    assert "1 hours" in policy_sql

    cur.execute.reset_mock()

    # compress_after=48h → schedule=12h (capped)
    sync_setup_schema(conn, compress_after_hours=48)
    policy_sql = cur.execute.call_args_list[4].args[0]
    assert "12 hours" in policy_sql


def test_default_values(mock_psycopg_conn):
    """Default chunk interval (7 days) and compress after (2 hours) are applied."""
    conn, cur = mock_psycopg_conn
    sync_setup_schema(conn)

    calls = [call.args[0] for call in cur.execute.call_args_list]

    assert "7 days" in calls[1]
    assert "2 hours" in calls[4]
    assert "1 hours" in calls[4]  # schedule_interval = compress_after // 2 = 1h


def test_dim_tables_ddl_executed(mock_psycopg_conn):
    """Dimension table DDL must be executed for all four registries (META-01)."""
    conn, cur = mock_psycopg_conn
    sync_setup_schema(conn)

    all_sql = " ".join(call.args[0] for call in cur.execute.call_args_list)

    assert "CREATE TABLE IF NOT EXISTS dim_entities" in all_sql
    assert "CREATE TABLE IF NOT EXISTS dim_devices" in all_sql
    assert "CREATE TABLE IF NOT EXISTS dim_areas" in all_sql
    assert "CREATE TABLE IF NOT EXISTS dim_labels" in all_sql


def test_dim_indexes_created(mock_psycopg_conn):
    """Index DDL must be executed for all four dimension tables."""
    conn, cur = mock_psycopg_conn
    sync_setup_schema(conn)

    all_sql = " ".join(call.args[0] for call in cur.execute.call_args_list)

    # Five indexes: compound + partial current-row for entities; one each for the rest
    assert "idx_dim_entities_entity_time" in all_sql
    assert "idx_dim_entities_current" in all_sql
    assert "idx_dim_devices_device_time" in all_sql
    assert "idx_dim_areas_area_time" in all_sql
    assert "idx_dim_labels_label_time" in all_sql
