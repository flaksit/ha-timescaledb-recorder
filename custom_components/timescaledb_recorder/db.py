"""Worker connection policy and in-flight DB call tracking (issue #14).

Both worker threads open their connections through connect(), so every
connection, including each reconnect after reset_db_connection, carries the same
timeouts. DbCallTracker lets the watchdog see a call that never returns.
"""
from __future__ import annotations

import functools
import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.conninfo import conninfo_to_dict

from .const import (
    DB_CONNECT_DEFAULTS,
    DB_STATEMENT_TIMEOUT_MS,
    STATEMENT_TIMEOUT_DEFAULT_SQL,
    STATEMENT_TIMEOUT_DISABLE_SQL,
    STATEMENT_TIMEOUT_RESTORE_SQL,
    STATEMENT_TIMEOUT_SHOW_SQL,
)

_LOGGER = logging.getLogger(__name__)


def connect_kwargs(dsn: str) -> dict[str, Any]:
    """Return the DB_CONNECT_DEFAULTS entries the DSN does not set itself."""
    user = conninfo_to_dict(dsn)
    return {k: v for k, v in DB_CONNECT_DEFAULTS.items() if k not in user}


def connect(dsn: str) -> psycopg.Connection:
    """Open an autocommit worker connection with bounded network and statement waits.

    statement_timeout is set per session after connecting rather than through the
    `options` startup parameter, which PgBouncer rejects unless configured to
    ignore it.
    """
    conn = psycopg.connect(dsn, autocommit=True, **connect_kwargs(dsn))
    try:
        with conn.cursor() as cur:
            cur.execute(STATEMENT_TIMEOUT_DEFAULT_SQL, (str(DB_STATEMENT_TIMEOUT_MS),))
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def statement_timeout_lifted(conn: psycopg.Connection) -> Iterator[None]:
    """Run the block with no statement_timeout, then restore the session's value.

    Restoring is best-effort: if the connection broke, the caller's retry resets
    it and the replacement starts from connect()'s setting anyway.
    """
    with conn.cursor() as cur:
        cur.execute(STATEMENT_TIMEOUT_SHOW_SQL)
        row = cur.fetchone()
        saved = row[0] if row else "0"
        cur.execute(STATEMENT_TIMEOUT_DISABLE_SQL)
    try:
        yield
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute(STATEMENT_TIMEOUT_RESTORE_SQL, (saved,))
        except psycopg.Error:
            _LOGGER.debug("Could not restore statement_timeout", exc_info=True)


class DbCallTracker:
    """The DB calls a worker currently has in flight, readable from any thread.

    Keyed by thread: the states worker's reads also run from executor threads
    on behalf of the backfill orchestrator.
    """

    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        self._calls: dict[int, tuple[str, float]] = {}

    @contextmanager
    def call(self, op: str) -> Iterator[None]:
        """Mark `op` in flight on this thread for the duration of the block.

        A nested call keeps the outer start time, so a long outer call stays
        visible.
        """
        ident = threading.get_ident()
        with self._lock:
            if ident in self._calls:
                nested = True
            else:
                nested = False
                self._calls[ident] = (op, time.monotonic())
        try:
            yield
        finally:
            if not nested:
                with self._lock:
                    self._calls.pop(ident, None)

    def track[**P, R](self, fn: Callable[P, R]) -> Callable[P, R]:
        """Wrap `fn` so each invocation is tracked under its name."""
        op = fn.__name__.strip("_").removesuffix("_raw")

        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            with self.call(op):
                return fn(*args, **kwargs)

        return wrapper

    def oldest(self) -> tuple[str, float] | None:
        """(op, monotonic start) of the longest-running call, or None if idle."""
        with self._lock:
            if not self._calls:
                return None
            return min(self._calls.values(), key=lambda c: c[1])
