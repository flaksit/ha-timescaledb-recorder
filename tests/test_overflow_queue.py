"""Unit tests for OverflowQueue (D-02 + D-11)."""
import logging
import queue
from unittest.mock import patch

import pytest

from custom_components.ha_timescaledb_recorder.overflow_queue import OverflowQueue


def test_init_not_overflowed():
    q = OverflowQueue(maxsize=4)
    assert q.overflowed is False
    assert q.maxsize == 4
    assert q.qsize() == 0


def test_put_nowait_basic_fifo():
    q = OverflowQueue(maxsize=3)
    q.put_nowait("a")
    q.put_nowait("b")
    q.put_nowait("c")
    assert q.get_nowait() == "a"
    assert q.get_nowait() == "b"
    assert q.get_nowait() == "c"
    assert q.overflowed is False


def test_put_nowait_drops_newest_on_full_without_raising(caplog):
    q = OverflowQueue(maxsize=2)
    q.put_nowait("a")
    q.put_nowait("b")
    with caplog.at_level(logging.WARNING,
                         logger="custom_components.ha_timescaledb_recorder.overflow_queue"):
        q.put_nowait("c")  # must not raise
    assert q.overflowed is True
    # Existing items untouched — we dropped the newest
    assert q.get_nowait() == "a"
    assert q.get_nowait() == "b"
    assert q.qsize() == 0
    # Single warning per D-11-a
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "live_queue full" in warnings[0].getMessage()


def test_put_nowait_subsequent_overflow_does_not_log(caplog):
    q = OverflowQueue(maxsize=1)
    q.put_nowait("a")
    with caplog.at_level(logging.WARNING,
                         logger="custom_components.ha_timescaledb_recorder.overflow_queue"):
        q.put_nowait("b")  # first drop → logs
        q.put_nowait("c")  # second drop → no log (D-11-b)
        q.put_nowait("d")  # third drop → no log
    assert q._dropped == 3
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1  # one log, not three


def test_clear_and_reset_overflow_returns_count_and_resets():
    q = OverflowQueue(maxsize=2)
    q.put_nowait("a")
    q.put_nowait("b")
    q.put_nowait("c")  # dropped
    q.put_nowait("d")  # dropped
    assert q.overflowed is True
    dropped = q.clear_and_reset_overflow()
    assert dropped == 2
    assert q.overflowed is False
    assert q.qsize() == 0
    # Counter resets; a post-reset drop logs again
    q.put_nowait("e")
    q.put_nowait("f")
    q.put_nowait("g")  # dropped
    assert q._dropped == 1
