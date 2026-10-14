"""Tests for db.py: connection defaults, statement_timeout handling, call tracking."""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, call, patch

import psycopg
import pytest

from custom_components.timescaledb_recorder.const import (
    DB_CONNECT_DEFAULTS,
    DB_STATEMENT_TIMEOUT_MS,
    STATEMENT_TIMEOUT_DEFAULT_SQL,
    STATEMENT_TIMEOUT_DISABLE_SQL,
    STATEMENT_TIMEOUT_RESTORE_SQL,
    STATEMENT_TIMEOUT_SHOW_SQL,
)
from custom_components.timescaledb_recorder.db import (
    DbCallTracker,
    connect,
    connect_kwargs,
    statement_timeout_lifted,
)


def test_connect_kwargs_fills_all_defaults_for_bare_dsn():
    assert connect_kwargs("postgresql://u@h/db") == DB_CONNECT_DEFAULTS


@pytest.mark.parametrize(
    "dsn",
    [
        "host=h dbname=db connect_timeout=3 keepalives_idle=600",
        "postgresql://u@h/db?connect_timeout=3&keepalives_idle=600",
    ],
)
def test_connect_kwargs_keeps_user_values(dsn):
    kwargs = connect_kwargs(dsn)
    assert "connect_timeout" not in kwargs
    assert "keepalives_idle" not in kwargs
    assert kwargs["keepalives_interval"] == DB_CONNECT_DEFAULTS["keepalives_interval"]
    assert kwargs["tcp_user_timeout"] == DB_CONNECT_DEFAULTS["tcp_user_timeout"]


def test_connect_passes_defaults_and_sets_statement_timeout(mock_psycopg_conn):
    conn, cur = mock_psycopg_conn
    dsn = "host=h keepalives=0"
    with patch("custom_components.timescaledb_recorder.db.psycopg.connect", return_value=conn) as pc:
        assert connect(dsn) is conn
    expected = {k: v for k, v in DB_CONNECT_DEFAULTS.items() if k != "keepalives"}
    pc.assert_called_once_with(dsn, autocommit=True, **expected)
    cur.execute.assert_called_once_with(
        STATEMENT_TIMEOUT_DEFAULT_SQL, (str(DB_STATEMENT_TIMEOUT_MS),),
    )


def test_connect_closes_connection_if_statement_timeout_fails(mock_psycopg_conn):
    conn, cur = mock_psycopg_conn
    cur.execute.side_effect = psycopg.OperationalError("gone")
    with patch("custom_components.timescaledb_recorder.db.psycopg.connect", return_value=conn):
        with pytest.raises(psycopg.OperationalError):
            connect("host=h")
    conn.close.assert_called_once()


def test_statement_timeout_lifted_restores_session_value(mock_psycopg_conn):
    conn, cur = mock_psycopg_conn
    cur.fetchone.return_value = ("5min",)
    body = MagicMock()
    with statement_timeout_lifted(conn):
        body()
    assert cur.execute.call_args_list == [
        call(STATEMENT_TIMEOUT_SHOW_SQL),
        call(STATEMENT_TIMEOUT_DISABLE_SQL),
        call(STATEMENT_TIMEOUT_RESTORE_SQL, ("5min",)),
    ]
    body.assert_called_once()


def test_statement_timeout_lifted_restores_on_error(mock_psycopg_conn):
    conn, cur = mock_psycopg_conn
    cur.fetchone.return_value = ("5min",)
    with pytest.raises(ValueError):
        with statement_timeout_lifted(conn):
            raise ValueError("ddl failed")
    assert cur.execute.call_args_list[-1] == call(STATEMENT_TIMEOUT_RESTORE_SQL, ("5min",))


def test_tracker_records_and_clears_call():
    tracker = DbCallTracker()
    assert tracker.oldest() is None
    with patch("custom_components.timescaledb_recorder.db.time.monotonic", return_value=42.0):
        with tracker.call("insert_chunk"):
            assert tracker.oldest() == ("insert_chunk", 42.0)
    assert tracker.oldest() is None


def test_tracker_clears_call_on_exception():
    tracker = DbCallTracker()
    with pytest.raises(RuntimeError):
        with tracker.call("x"):
            raise RuntimeError
    assert tracker.oldest() is None


def test_tracker_nested_call_keeps_outer_start():
    tracker = DbCallTracker()
    with patch("custom_components.timescaledb_recorder.db.time.monotonic", side_effect=[1.0, 2.0]):
        with tracker.call("outer"):
            with tracker.call("inner"):
                assert tracker.oldest() == ("outer", 1.0)
            assert tracker.oldest() == ("outer", 1.0)
    assert tracker.oldest() is None


def test_tracker_reports_oldest_across_threads():
    tracker = DbCallTracker()
    entered = threading.Event()
    release = threading.Event()

    def other():
        with tracker.call("read_watermark"):
            entered.set()
            release.wait(5)

    with patch("custom_components.timescaledb_recorder.db.time.monotonic", side_effect=[1.0, 2.0]):
        t = threading.Thread(target=other)
        t.start()
        entered.wait(5)
        with tracker.call("insert_chunk"):
            assert tracker.oldest() == ("read_watermark", 1.0)
        release.set()
        t.join(5)
    assert tracker.oldest() is None


def test_track_names_op_after_function_and_preserves_qualname():
    tracker = DbCallTracker()
    seen = []

    def _insert_chunk_raw(x):
        seen.append(tracker.oldest()[0])
        return x * 2

    wrapped = tracker.track(_insert_chunk_raw)
    assert wrapped(3) == 6
    assert seen == ["insert_chunk"]
    assert wrapped.__qualname__ == _insert_chunk_raw.__qualname__
