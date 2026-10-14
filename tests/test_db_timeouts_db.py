"""Database-backed checks that db.connect's bounds are in effect (issue #14).

Runs idempotent schema setup and briefly locks `states`; drops nothing. Same
guard as the other *_db modules.
"""
import os
import socket
from unittest.mock import patch

import pytest

pytest.importorskip("psycopg")
import psycopg  # noqa: E402  pyright: ignore[reportMissingImports]

from custom_components.timescaledb_recorder import db  # noqa: E402
from custom_components.timescaledb_recorder.retry import is_transient  # noqa: E402
from custom_components.timescaledb_recorder.schema import sync_setup_schema  # noqa: E402
from tests.db_guard import assert_disposable  # noqa: E402

DSN = os.environ.get("SCD2_TEST_DSN")

pytestmark = [
    pytest.mark.skipif(not DSN, reason="SCD2_TEST_DSN not set"),
    pytest.mark.enable_socket,
]


def _show(conn, name: str) -> str:
    return conn.execute(f"SHOW {name}").fetchone()[0]


@pytest.fixture
def conn():
    c = db.connect(DSN)
    try:
        assert_disposable(c)
        yield c
    finally:
        c.close()


def test_connect_applies_statement_timeout_and_tcp_bounds(conn, socket_enabled):
    assert _show(conn, "statement_timeout") == "5min"
    if conn.info.host.startswith("/"):
        pytest.skip("TCP options do not apply to Unix sockets")
    sock = socket.socket(fileno=conn.fileno())
    try:
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 1
        assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE) == 30
        assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT) == 60_000
    finally:
        sock.detach()  # the fd belongs to libpq


def test_user_statement_timeout_is_kept():
    c = db.connect(DSN + " options='-c statement_timeout=1234'")
    try:
        assert _show(c, "statement_timeout") == "1234ms"
    finally:
        c.close()


def test_schema_setup_window_has_no_statement_timeout(conn):
    with db.statement_timeout_lifted(conn):
        assert _show(conn, "statement_timeout") == "0"
    assert _show(conn, "statement_timeout") == "5min"


def test_lock_wait_ends_as_transient_error():
    setup = psycopg.connect(DSN, autocommit=True)
    try:
        assert_disposable(setup)
        sync_setup_schema(setup)
    finally:
        setup.close()
    with patch.object(db, "DB_STATEMENT_TIMEOUT_MS", 500):
        c = db.connect(DSN)
    locker = psycopg.connect(DSN)
    try:
        locker.execute("LOCK TABLE states IN ACCESS EXCLUSIVE MODE")
        with pytest.raises(psycopg.errors.QueryCanceled) as err:
            c.execute("SELECT count(*) FROM states")
        assert is_transient(err.value)
    finally:
        locker.rollback()
        locker.close()
        c.close()
