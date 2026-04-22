"""Unit tests for TimescaledbMetaRecorderThread (D-05, D-07, D-15-c)."""
import threading
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from custom_components.ha_timescaledb_recorder.const import (
    SCD2_CLOSE_AREA_SQL,
    SCD2_CLOSE_DEVICE_SQL,
    SCD2_CLOSE_ENTITY_SQL,
    SCD2_CLOSE_LABEL_SQL,
    SCD2_INSERT_ENTITY_SQL,
    SCD2_SNAPSHOT_AREA_SQL,
    SCD2_SNAPSHOT_DEVICE_SQL,
    SCD2_SNAPSHOT_ENTITY_SQL,
    SCD2_SNAPSHOT_LABEL_SQL,
)
from custom_components.ha_timescaledb_recorder.meta_worker import (
    _VALID_FROM_INDEX,
    TimescaledbMetaRecorderThread,
)


def _make_worker(meta_queue=None, syncer=None):
    hass = MagicMock()
    meta_queue = meta_queue or MagicMock()
    syncer = syncer or MagicMock()
    stop_event = threading.Event()
    return TimescaledbMetaRecorderThread(
        hass=hass, dsn="postgresql://",
        meta_queue=meta_queue, syncer=syncer, stop_event=stop_event,
    )


def test_valid_from_index_constants():
    assert _VALID_FROM_INDEX == {
        "entity": 11, "device": 6, "area": 2, "label": 3,
    }


def test_rehydrate_params_converts_iso_string_at_entity_slot():
    w = _make_worker()
    # entity params: 13 slots; slot 11 (0-indexed) is valid_from
    params = ["x.y", "uuid", "name", "sensor", "zha", None, None,
              [], None, None, None, "2026-04-21T12:00:00+00:00", "{}"]
    out = w._rehydrate_params("entity", params)
    assert isinstance(out, tuple)
    assert isinstance(out[11], datetime)
    assert out[11] == datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc)


def test_rehydrate_params_none_is_none():
    w = _make_worker()
    assert w._rehydrate_params("entity", None) is None


def test_write_item_entity_create_uses_snapshot_sql(mock_psycopg_conn):
    conn, cur = mock_psycopg_conn
    w = _make_worker()
    w._conn = conn
    item = {
        "registry": "entity", "action": "create",
        "registry_id": "sensor.x", "old_id": None,
        "params": ["sensor.x", "uuid", "n", "sensor", "zha", None, None,
                   [], None, None, None, "2026-04-21T12:00:00+00:00", "{}"],
        "enqueued_at": "2026-04-21T12:00:00+00:00",
    }
    w._write_item_raw(item)
    args = cur.execute.call_args_list
    # First execute is the snapshot SQL
    sql, params = args[0].args
    assert sql == SCD2_SNAPSHOT_ENTITY_SQL
    assert params[0] == "sensor.x"
    # entity_id appears twice: once in SELECT, once in WHERE NOT EXISTS
    assert params[-1] == "sensor.x"


def test_write_item_entity_remove_uses_close_sql(mock_psycopg_conn):
    conn, cur = mock_psycopg_conn
    w = _make_worker()
    w._conn = conn
    item = {
        "registry": "entity", "action": "remove",
        "registry_id": "sensor.x", "old_id": None,
        "params": None,
        "enqueued_at": "2026-04-21T12:00:00+00:00",
    }
    w._write_item_raw(item)
    call = cur.execute.call_args
    sql, params = call.args
    assert sql == SCD2_CLOSE_ENTITY_SQL
    assert params[1] == "sensor.x"


def test_write_item_entity_rename_closes_old_inserts_new(mock_psycopg_conn):
    conn, cur = mock_psycopg_conn
    w = _make_worker()
    w._conn = conn
    # conn.transaction() must work as a context manager
    conn.transaction = MagicMock(return_value=MagicMock(
        __enter__=MagicMock(),
        __exit__=MagicMock(return_value=False),
    ))
    item = {
        "registry": "entity", "action": "update",
        "registry_id": "sensor.new", "old_id": "sensor.old",
        "params": ["sensor.new", "uuid", "n", "sensor", "zha", None, None,
                   [], None, None, None, "2026-04-21T12:00:00+00:00", "{}"],
        "enqueued_at": "2026-04-21T12:00:00+00:00",
    }
    w._write_item_raw(item)
    calls = cur.execute.call_args_list
    sqls = [c.args[0] for c in calls]
    assert SCD2_CLOSE_ENTITY_SQL in sqls
    assert SCD2_INSERT_ENTITY_SQL in sqls


def test_write_item_device_create(mock_psycopg_conn):
    conn, cur = mock_psycopg_conn
    w = _make_worker()
    w._conn = conn
    item = {
        "registry": "device", "action": "create",
        "registry_id": "dev-1", "old_id": None,
        "params": ["dev-1", "name", "mfr", "model", None, [],
                   "2026-04-21T12:00:00+00:00", "{}"],
        "enqueued_at": "2026-04-21T12:00:00+00:00",
    }
    w._write_item_raw(item)
    sql = cur.execute.call_args.args[0]
    assert sql == SCD2_SNAPSHOT_DEVICE_SQL


def test_write_item_area_create(mock_psycopg_conn):
    conn, cur = mock_psycopg_conn
    w = _make_worker()
    w._conn = conn
    item = {
        "registry": "area", "action": "create",
        "registry_id": "area-1", "old_id": None,
        "params": ["area-1", "name", "2026-04-21T12:00:00+00:00", "{}"],
        "enqueued_at": "2026-04-21T12:00:00+00:00",
    }
    w._write_item_raw(item)
    sql = cur.execute.call_args.args[0]
    assert sql == SCD2_SNAPSHOT_AREA_SQL


def test_write_item_label_create(mock_psycopg_conn):
    conn, cur = mock_psycopg_conn
    w = _make_worker()
    w._conn = conn
    item = {
        "registry": "label", "action": "create",
        "registry_id": "label-1", "old_id": None,
        "params": ["label-1", "name", "#fff", "2026-04-21T12:00:00+00:00", "{}"],
        "enqueued_at": "2026-04-21T12:00:00+00:00",
    }
    w._write_item_raw(item)
    sql = cur.execute.call_args.args[0]
    assert sql == SCD2_SNAPSHOT_LABEL_SQL


def test_run_loop_skips_task_done_on_shutdown_interrupt(mock_psycopg_conn):
    """D-03-h: when retry returns None due to shutdown, do not call task_done."""
    conn, cur = mock_psycopg_conn
    stop_event = threading.Event()
    meta_queue = MagicMock()
    meta_queue.get = MagicMock(side_effect=[{
        "registry": "entity", "action": "create",
        "registry_id": "x.y", "old_id": None,
        "params": ["x.y", "u", "n", "sensor", "zha", None, None,
                   [], None, None, None, "2026-04-21T12:00:00+00:00", "{}"],
        "enqueued_at": "2026-04-21T12:00:00+00:00",
    }])
    w = _make_worker(meta_queue=meta_queue)
    w._conn = conn

    def fake_write_item(item):
        # Simulate: retry-decorator interrupted by shutdown → sets stop_event
        # and returns None (D-07-e behaviour).
        stop_event.set()
        return None

    w._write_item = fake_write_item
    w._stop_event = stop_event

    w.run()
    meta_queue.task_done.assert_not_called()
