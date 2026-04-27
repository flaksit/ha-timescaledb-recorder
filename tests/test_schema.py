"""Tests for the schema module (psycopg3 sync API)."""
import pytest

from custom_components.timescaledb_recorder.schema import sync_setup_schema


def test_create_schema_executes_all_statements(mock_psycopg_conn):
    """sync_setup_schema must execute exactly 16 SQL statements.

    7 hypertable setup statements (CREATE TABLE, create_hypertable, SET compression,
    remove_compression_policy, add_compression_policy, CREATE INDEX, CREATE UNIQUE INDEX)
    + 4 dimension table DDL (entities, devices, areas, labels)
    + 5 dimension table indexes (entities compound, entities current-row,
      devices, areas, labels)
    = 16 total

    Phase 2 added CREATE_UNIQUE_INDEX_SQL (D-09-a) after CREATE_INDEX_SQL,
    raising the total from 15 to 16.

    The cursor is obtained via conn.cursor() context manager in sync_setup_schema.
    """
    conn, cur = mock_psycopg_conn
    sync_setup_schema(conn)
    assert cur.execute.call_count == 16


def test_create_schema_order(mock_psycopg_conn):
    """SQL statements must be executed in the defined order.

    Phase 2 added CREATE_UNIQUE_INDEX_SQL at position 6 (after CREATE_INDEX_SQL),
    shifting all dimension-table DDL one position forward.
    """
    conn, cur = mock_psycopg_conn
    sync_setup_schema(conn)

    calls = [call.args[0] for call in cur.execute.call_args_list]

    # 7 hypertable setup statements (Phase 2: unique index added after regular index)
    assert "CREATE TABLE" in calls[0]
    assert "create_hypertable" in calls[1]
    assert "timescaledb.compress" in calls[2]
    assert "remove_compression_policy" in calls[3]
    assert "add_compression_policy" in calls[4]
    assert "CREATE INDEX" in calls[5]
    assert "UNIQUE INDEX" in calls[6]   # D-09-a: Phase 2 addition
    # Dimension table DDL follows at offset 7
    assert "entities" in calls[7]
    assert "devices" in calls[8]
    assert "areas" in calls[9]
    assert "labels" in calls[10]


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

    assert "CREATE TABLE IF NOT EXISTS entities" in all_sql
    assert "CREATE TABLE IF NOT EXISTS devices" in all_sql
    assert "CREATE TABLE IF NOT EXISTS areas" in all_sql
    assert "CREATE TABLE IF NOT EXISTS labels" in all_sql


def test_dim_indexes_created(mock_psycopg_conn):
    """Index DDL must be executed for all four dimension tables."""
    conn, cur = mock_psycopg_conn
    sync_setup_schema(conn)

    all_sql = " ".join(call.args[0] for call in cur.execute.call_args_list)

    # Five indexes: compound + partial current-row for entities; one each for the rest
    assert "idx_dim_entities_entity_time" in all_sql
    assert "idx_dim_entities_current" in all_sql
    assert "idx_dim_devices_device_time" in all_sql
    assert "idx_areas_area_time" in all_sql
    assert "idx_dim_labels_label_time" in all_sql


def test_sync_setup_schema_executes_unique_index(mock_psycopg_conn):
    """sync_setup_schema must execute CREATE_UNIQUE_INDEX_SQL (D-09-a: enables ON CONFLICT DO NOTHING)."""
    from custom_components.timescaledb_recorder.const import CREATE_UNIQUE_INDEX_SQL

    conn, cur = mock_psycopg_conn
    sync_setup_schema(conn, chunk_interval_days=7, compress_after_hours=2)
    executed = [call.args[0] for call in cur.execute.call_args_list]
    assert CREATE_UNIQUE_INDEX_SQL in executed
