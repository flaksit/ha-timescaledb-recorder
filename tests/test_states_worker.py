"""Unit tests for TimescaledbStateRecorderThread (D-04, D-06, D-08-d/f)."""
import queue
import threading
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from custom_components.ha_timescaledb_recorder.const import (
    INSERT_SQL,
    SELECT_OPEN_ENTITIES_SQL,
    SELECT_WATERMARK_SQL,
)
from custom_components.ha_timescaledb_recorder.overflow_queue import OverflowQueue
from custom_components.ha_timescaledb_recorder.states_worker import (
    MODE_BACKFILL,
    MODE_INIT,
    MODE_LIVE,
    TimescaledbStateRecorderThread,
)
from custom_components.ha_timescaledb_recorder.worker import StateRow


def _make_thread(hass=None, live_queue=None, backfill_queue=None,
                 backfill_request=None, stop_event=None):
    hass = hass or MagicMock()
    live_queue = live_queue or OverflowQueue(maxsize=100)
    backfill_queue = backfill_queue or queue.Queue(maxsize=2)
    backfill_request = backfill_request or MagicMock()  # asyncio.Event mock
    stop_event = stop_event or threading.Event()
    return TimescaledbStateRecorderThread(
        hass=hass, dsn="postgresql://",
        live_queue=live_queue, backfill_queue=backfill_queue,
        backfill_request=backfill_request, stop_event=stop_event,
        chunk_interval_days=7, compress_after_hours=2,
    )


def _sample_state_row(eid="x.y", t=None):
    t = t or datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)
    return StateRow(
        entity_id=eid, state="on",
        attributes={"a": 1}, last_updated=t, last_changed=t,
    )


def test_mode_constants():
    assert MODE_INIT == "init"
    assert MODE_BACKFILL == "backfill"
    assert MODE_LIVE == "live"


def test_slice_to_rows_sorts_by_last_updated():
    t = _make_thread()
    t1 = datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 4, 21, 12, 0, 1, tzinfo=timezone.utc)
    t3 = datetime(2026, 4, 21, 12, 0, 2, tzinfo=timezone.utc)

    def mkstate(eid, at):
        s = MagicMock()
        s.entity_id = eid
        s.state = "on"
        s.attributes = {}
        s.last_updated = at
        s.last_changed = at
        return s

    slice_dict = {
        "a.1": [mkstate("a.1", t3), mkstate("a.1", t1)],
        "b.2": [mkstate("b.2", t2)],
    }
    rows = t._slice_to_rows(slice_dict)
    assert [r.last_updated for r in rows] == [t1, t2, t3]


def test_insert_chunk_raw_uses_jsonb_and_insert_sql(mock_psycopg_conn):
    conn, cur = mock_psycopg_conn
    t = _make_thread()
    t._conn = conn
    rows = [_sample_state_row()]
    t._insert_chunk_raw(rows)
    # Correct SQL used
    cur.executemany.assert_called_once()
    sql_arg = cur.executemany.call_args[0][0]
    assert sql_arg == INSERT_SQL
    # Jsonb-wrapped attributes
    params = cur.executemany.call_args[0][1]
    assert len(params) == 1
    # attributes param is Jsonb-wrapped
    from psycopg.types.json import Jsonb
    assert isinstance(params[0][2], Jsonb)


def test_read_watermark_returns_max_last_updated(mock_psycopg_conn):
    conn, cur = mock_psycopg_conn
    expected_t = datetime(2026, 4, 21, 0, 0, tzinfo=timezone.utc)
    cur.fetchone = MagicMock(return_value=(expected_t,))
    t = _make_thread()
    t._conn = conn
    wm = t.read_watermark()
    assert wm == expected_t
    cur.execute.assert_called_with(SELECT_WATERMARK_SQL)


def test_read_watermark_returns_none_on_empty_hypertable(mock_psycopg_conn):
    conn, cur = mock_psycopg_conn
    cur.fetchone = MagicMock(return_value=(None,))
    t = _make_thread()
    t._conn = conn
    assert t.read_watermark() is None


def test_read_open_entities_returns_set_of_ids(mock_psycopg_conn):
    conn, cur = mock_psycopg_conn
    cur.fetchall = MagicMock(return_value=[("sensor.a",), ("sensor.b",)])
    t = _make_thread()
    t._conn = conn
    result = t.read_open_entities()
    assert result == {"sensor.a", "sensor.b"}
    cur.execute.assert_called_with(SELECT_OPEN_ENTITIES_SQL)


def test_reset_db_connection_drops_conn():
    t = _make_thread()
    t._conn = MagicMock()
    t.reset_db_connection()
    assert t._conn is None


def test_flush_chunks_by_insert_chunk_size(mock_psycopg_conn):
    conn, cur = mock_psycopg_conn
    t = _make_thread()
    t._conn = conn
    # Replace retry-wrapped _insert_chunk with a spy so we can count calls
    t._insert_chunk = MagicMock()
    # 450 rows → 3 chunks at chunk-size 200
    rows = [_sample_state_row(eid=f"s.{i}") for i in range(450)]
    t._flush(rows)
    assert t._insert_chunk.call_count == 3
    # Chunk sizes: 200, 200, 50
    sizes = [len(c.args[0]) for c in t._insert_chunk.call_args_list]
    assert sizes == [200, 200, 50]


def test_retry_wrapper_applied_at_init(mock_psycopg_conn):
    """D-07: _insert_chunk is the retry-wrapped version of _insert_chunk_raw."""
    t = _make_thread()
    # wrapped function is a different object from the raw one
    assert t._insert_chunk is not t._insert_chunk_raw
    # functools.wraps copies __qualname__ from the wrapped function
    assert "_insert_chunk_raw" in t._insert_chunk.__qualname__
