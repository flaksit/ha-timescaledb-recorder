"""Tests for the schema module (psycopg3 sync API)."""
import pytest

from custom_components.timescaledb_recorder.schema import sync_setup_schema


def test_create_schema_executes_all_statements(mock_psycopg_conn):
    """sync_setup_schema must execute exactly 23 SQL statements.

    7 hypertable setup statements (CREATE TABLE, create_hypertable, SET compression,
    remove_compression_policy, add_compression_policy, CREATE INDEX, CREATE UNIQUE INDEX)
    + 4 dimension table DDL (entities, devices, areas, labels)
    + 5 dimension table indexes (entities compound, entities current-row,
      devices, areas, labels)
    + 2 convenience views (states_numeric, states_flat)
    + 1 btree_gist extension + 4 SCD2 exclusion constraints (issue #17)
    = 23 total

    The cursor is obtained via conn.cursor() context manager in sync_setup_schema.
    """
    conn, cur = mock_psycopg_conn
    sync_setup_schema(conn)
    assert cur.execute.call_count == 23


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
    # Views come last of the main block — states_flat joins the dimension
    # tables, so they must already exist.
    assert "CREATE OR REPLACE VIEW states_numeric" in calls[16]
    assert "CREATE OR REPLACE VIEW states_flat" in calls[17]
    # Invariant enforcement follows the tables it constrains (issue #17).
    assert "btree_gist" in calls[18]
    for offset, table in enumerate(("entities", "devices", "areas", "labels")):
        assert f"excl_{table}_period" in calls[19 + offset]


def test_constraint_failure_does_not_abort_schema_setup(mock_psycopg_conn):
    """A damaged install cannot create the constraints, and that must not stop
    schema setup — states ingestion is never held hostage to dimension repair.
    """
    import psycopg

    conn, cur = mock_psycopg_conn

    def _side_effect(sql, *args, **kwargs):
        if "excl_" in sql or "btree_gist" in sql:
            raise psycopg.errors.InsufficientPrivilege("nope")
        return None

    cur.execute.side_effect = _side_effect
    sync_setup_schema(conn)  # must not raise


def test_falls_back_to_unique_index_without_btree_gist(mock_psycopg_conn):
    """Without btree_gist the weaker guarantee still gets installed."""
    import psycopg

    conn, cur = mock_psycopg_conn

    def _side_effect(sql, *args, **kwargs):
        if "btree_gist" in sql:
            raise psycopg.errors.InsufficientPrivilege("no CREATE on database")
        return None

    cur.execute.side_effect = _side_effect
    sync_setup_schema(conn)

    calls = [c.args[0] for c in cur.execute.call_args_list]
    assert any("ux_entities_open" in c for c in calls)
    assert not any("excl_entities_period" in c for c in calls)


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


def test_states_flat_joins_point_in_time_not_current_row():
    """states_flat must resolve metadata as of each state's timestamp.

    Joining on `valid_to IS NULL` regresses two ways: entities deleted from HA
    lose the metadata for their whole history, and entities that end up with more
    than one simultaneously-open version duplicate every fact row they match.
    """
    from custom_components.timescaledb_recorder.const import (
        CREATE_VIEW_STATES_FLAT_SQL,
    )

    sql = CREATE_VIEW_STATES_FLAT_SQL
    assert "valid_to IS NULL" not in sql
    # Intervals are derived from consecutive valid_from values and clamped open
    # at both ends, so exactly one version matches any timestamp.
    for dim in ("entity_id", "area_id", "device_id"):
        assert f"PARTITION BY {dim} ORDER BY valid_from" in sql
    assert sql.count("'-infinity'::timestamptz") == 3   # one per dimension
    assert sql.count("'infinity'::timestamptz") == 3    # ditto; '-infinity' does not match
    assert sql.count("lead(valid_from)") == 3
    assert sql.count("DISTINCT ON") == 3


def test_numeric_regex_accepts_negative_states():
    """The view cast guard must accept negatives — grid export and sub-zero
    temperatures are numeric states that a '^[0-9]' guard silently drops."""
    import re

    from custom_components.timescaledb_recorder.const import NUMERIC_STATE_REGEX

    pattern = re.compile(NUMERIC_STATE_REGEX)

    for accepted in ("0", "23.5", "-412", "-0.75", "1e-05", "-2.5E+3"):
        assert pattern.match(accepted), f"{accepted!r} should be numeric"

    for rejected in ("unavailable", "unknown", "None", "on", "", "12.", "1.2.3"):
        assert not pattern.match(rejected), f"{rejected!r} should not be numeric"
