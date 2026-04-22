"""Unit tests for retry_until_success (D-07)."""
import threading
from unittest.mock import MagicMock

import pytest

from custom_components.ha_timescaledb_recorder.retry import (
    _BACKOFF_SCHEDULE,
    _STALL_NOTIFY_THRESHOLD,
    retry_until_success,
)


def test_success_path_returns_value_no_hooks():
    ev = threading.Event()
    on_transient = MagicMock()
    notify_stall = MagicMock()

    @retry_until_success(stop_event=ev,
                         on_transient=on_transient,
                         notify_stall=notify_stall)
    def fn(x):
        return x * 2

    assert fn(21) == 42
    on_transient.assert_not_called()
    notify_stall.assert_not_called()


def test_retry_then_success_with_fast_backoff():
    ev = threading.Event()
    calls = [0]

    # backoff=(0,) so wait returns immediately without actually sleeping
    @retry_until_success(stop_event=ev,
                         backoff_schedule=(0,))
    def fn():
        calls[0] += 1
        if calls[0] < 3:
            raise RuntimeError("transient")
        return "ok"

    assert fn() == "ok"
    assert calls[0] == 3


def test_stop_event_interrupts_backoff_returns_none():
    ev = threading.Event()
    ev.set()  # stop_event.wait returns True immediately
    calls = [0]

    @retry_until_success(stop_event=ev)
    def fn():
        calls[0] += 1
        raise RuntimeError("always")

    assert fn() is None
    assert calls[0] == 1  # one attempt, then stop_event → exit


def test_on_transient_called_on_each_failure():
    ev = threading.Event()
    on_transient = MagicMock()
    calls = [0]

    @retry_until_success(stop_event=ev,
                         on_transient=on_transient,
                         backoff_schedule=(0,))
    def fn():
        calls[0] += 1
        if calls[0] < 3:
            raise RuntimeError("x")
        return "done"

    fn()
    assert on_transient.call_count == 2  # one per failure


def test_notify_stall_fires_once_at_threshold():
    ev = threading.Event()
    notify_stall = MagicMock()
    calls = [0]

    @retry_until_success(stop_event=ev,
                         notify_stall=notify_stall,
                         backoff_schedule=(0,),
                         stall_threshold=3)
    def fn():
        calls[0] += 1
        if calls[0] < 5:
            raise RuntimeError("x")
        return "done"

    fn()
    # Fires on attempt 3; does NOT re-fire on 4 (still stalled)
    assert notify_stall.call_count == 1
    assert notify_stall.call_args[0][0] == 3


def test_notify_stall_rearms_after_success():
    ev = threading.Event()
    notify_stall = MagicMock()
    side_effect = [RuntimeError("x")] * 3 + ["ok"] + [RuntimeError("y")] * 3 + ["ok"]
    side_iter = iter(side_effect)

    @retry_until_success(stop_event=ev,
                         notify_stall=notify_stall,
                         backoff_schedule=(0,),
                         stall_threshold=2)
    def fn():
        v = next(side_iter)
        if isinstance(v, Exception):
            raise v
        return v

    fn()
    assert notify_stall.call_count == 1
    fn()
    # Re-armed after first success; second stall → second notify
    assert notify_stall.call_count == 2


def test_broad_exception_catch_d07c():
    """D-07-c: catches BaseException-derived data-class errors too."""
    ev = threading.Event()
    calls = [0]

    @retry_until_success(stop_event=ev, backoff_schedule=(0,))
    def fn():
        calls[0] += 1
        if calls[0] < 2:
            raise ValueError("data-class-error looks like transient to us")
        return "ok"

    assert fn() == "ok"


def test_default_backoff_constants():
    assert _BACKOFF_SCHEDULE == (1, 5, 10, 30, 60)
    assert _STALL_NOTIFY_THRESHOLD == 5
