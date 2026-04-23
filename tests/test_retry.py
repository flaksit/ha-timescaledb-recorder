"""Unit tests for retry_until_success (D-07)."""
import threading
from unittest.mock import MagicMock, patch

import pytest

from custom_components.ha_timescaledb_recorder import const
from custom_components.ha_timescaledb_recorder.retry import (
    _BACKOFF_SCHEDULE,
    _STALL_NOTIFY_THRESHOLD,
    retry_until_success,
)


class _FailNTimesThenSucceed:
    """Callable that raises RuntimeError n times, then returns `value`."""

    def __init__(self, n: int, value=None):
        self.remaining = n
        self.value = value
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise RuntimeError(f"fail #{self.calls}")
        return self.value


# ---------------------------------------------------------------------------
# Existing (regression) tests — must continue to pass
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# New tests for on_recovery, on_sustained_fail, and related behaviours
# ---------------------------------------------------------------------------

def test_on_recovery_fires_once_after_stall_then_success():
    """on_recovery fires exactly once after first success following stall threshold crossing."""
    ev = threading.Event()
    on_recovery = MagicMock()
    notify_stall = MagicMock()

    # Fail STALL_THRESHOLD times (5 by default), then succeed
    fn_impl = _FailNTimesThenSucceed(n=5, value="recovered")

    @retry_until_success(
        stop_event=ev,
        notify_stall=notify_stall,
        on_recovery=on_recovery,
        backoff_schedule=(0,),
    )
    def fn():
        return fn_impl()

    result = fn()
    assert result == "recovered"
    on_recovery.assert_called_once()


def test_on_recovery_not_fired_on_success_without_stall():
    """on_recovery does NOT fire on success before any stall (attempts < stall_threshold)."""
    ev = threading.Event()
    on_recovery = MagicMock()

    # Fail only 2 times (< default STALL_THRESHOLD of 5)
    fn_impl = _FailNTimesThenSucceed(n=2, value="ok")

    @retry_until_success(
        stop_event=ev,
        on_recovery=on_recovery,
        backoff_schedule=(0,),
    )
    def fn():
        return fn_impl()

    result = fn()
    assert result == "ok"
    on_recovery.assert_not_called()


def test_on_recovery_rearms_after_recovery_and_new_stall():
    """on_recovery fires a second time if a new stall cycle happens on the same wrapped fn."""
    ev = threading.Event()
    on_recovery = MagicMock()
    notify_stall = MagicMock()

    # First call: stall (5 failures) then succeed
    # Second call: stall again (5 failures) then succeed
    fn_impl1 = _FailNTimesThenSucceed(n=5, value="first")
    fn_impl2 = _FailNTimesThenSucceed(n=5, value="second")
    calls = [0]

    @retry_until_success(
        stop_event=ev,
        notify_stall=notify_stall,
        on_recovery=on_recovery,
        backoff_schedule=(0,),
    )
    def fn():
        calls[0] += 1
        if calls[0] <= 6:
            return fn_impl1()
        return fn_impl2()

    fn()  # first pass: 5 failures, then success → on_recovery fires once
    assert on_recovery.call_count == 1

    fn()  # second pass: 5 failures, then success → on_recovery fires again
    assert on_recovery.call_count == 2


def test_on_sustained_fail_fires_once_when_duration_exceeded():
    """on_sustained_fail fires once when cumulative fail duration >= sustained_fail_seconds."""
    ev = threading.Event()
    on_sustained_fail = MagicMock()

    # Return monotonic values: first failure at t=0.0, second at t=0.06
    # With sustained_fail_seconds=0.05, on attempt 2 the threshold is crossed.
    monotonic_values = iter([0.0, 0.06, 0.12])

    fn_impl = _FailNTimesThenSucceed(n=3, value="ok")

    with patch(
        "custom_components.ha_timescaledb_recorder.retry.time.monotonic",
        side_effect=monotonic_values,
    ):
        @retry_until_success(
            stop_event=ev,
            on_sustained_fail=on_sustained_fail,
            sustained_fail_seconds=0.05,
            backoff_schedule=(0,),
        )
        def fn():
            return fn_impl()

        fn()

    on_sustained_fail.assert_called_once()


def test_on_sustained_fail_not_called_twice_in_same_streak():
    """on_sustained_fail does NOT fire a second time in the same streak even if failures continue."""
    ev = threading.Event()
    on_sustained_fail = MagicMock()

    # 5 failures, all exceeding the threshold after the first
    # monotonic: 0.0, 1.0, 2.0, 3.0, 4.0 (all > 0.05 from start)
    times = [float(i) for i in range(6)]
    monotonic_values = iter(times)

    fn_impl = _FailNTimesThenSucceed(n=5, value="ok")

    with patch(
        "custom_components.ha_timescaledb_recorder.retry.time.monotonic",
        side_effect=monotonic_values,
    ):
        @retry_until_success(
            stop_event=ev,
            on_sustained_fail=on_sustained_fail,
            sustained_fail_seconds=0.05,
            backoff_schedule=(0,),
        )
        def fn():
            return fn_impl()

        fn()

    # Despite 5 failures all exceeding the threshold, fires only once
    assert on_sustained_fail.call_count == 1


def test_on_sustained_fail_resets_after_success():
    """on_sustained_fail state resets on success — fires again if new streak exceeds threshold."""
    ev = threading.Event()
    on_sustained_fail = MagicMock()

    # Pass 1: 2 failures that exceed threshold → fires; then success resets.
    # Pass 2: 2 failures that exceed threshold again → fires again.
    pass1_times = iter([0.0, 1.0])
    pass2_times = iter([0.0, 1.0])
    call_count = [0]

    def monotonic_side_effect():
        call_count[0] += 1
        if call_count[0] <= 2:
            return next(pass1_times)
        return next(pass2_times)

    fn_call_count = [0]
    # Two separate failure-then-success sequences
    fn_states = iter(
        [RuntimeError("a"), RuntimeError("b"), "ok1",
         RuntimeError("c"), RuntimeError("d"), "ok2"]
    )

    with patch(
        "custom_components.ha_timescaledb_recorder.retry.time.monotonic",
        side_effect=monotonic_side_effect,
    ):
        @retry_until_success(
            stop_event=ev,
            on_sustained_fail=on_sustained_fail,
            sustained_fail_seconds=0.5,
            backoff_schedule=(0,),
        )
        def fn():
            v = next(fn_states)
            if isinstance(v, Exception):
                raise v
            return v

        fn()  # first streak: fires once
        assert on_sustained_fail.call_count == 1

        fn()  # second streak: fires again (state reset after first success)
        assert on_sustained_fail.call_count == 2


def test_on_transient_none_is_ok():
    """on_transient=None works without error — wrapped function succeeds or fails, no TypeError."""
    ev = threading.Event()
    fn_impl = _FailNTimesThenSucceed(n=2, value="ok")

    @retry_until_success(
        stop_event=ev,
        on_transient=None,
        backoff_schedule=(0,),
    )
    def fn():
        return fn_impl()

    # Must not raise TypeError
    result = fn()
    assert result == "ok"


def test_stall_threshold_default_imports_from_const():
    """stall_threshold defaults to STALL_THRESHOLD (5) from const.py."""
    import inspect
    from custom_components.ha_timescaledb_recorder.retry import retry_until_success

    sig = inspect.signature(retry_until_success)
    default = sig.parameters["stall_threshold"].default
    assert default == const.STALL_THRESHOLD
    assert default == 5


def test_on_recovery_hook_exception_logged_and_swallowed():
    """When on_recovery hook raises, exception is logged and swallowed; wrapper still returns result."""
    ev = threading.Event()
    on_recovery = MagicMock(side_effect=RuntimeError("hook failure"))

    fn_impl = _FailNTimesThenSucceed(n=5, value="result")

    @retry_until_success(
        stop_event=ev,
        on_recovery=on_recovery,
        backoff_schedule=(0,),
    )
    def fn():
        return fn_impl()

    # Must not propagate the hook exception
    result = fn()
    assert result == "result"
    on_recovery.assert_called_once()


def test_on_sustained_fail_hook_exception_logged_and_swallowed():
    """When on_sustained_fail hook raises, exception is logged and swallowed; retry loop continues."""
    ev = threading.Event()
    on_sustained_fail = MagicMock(side_effect=RuntimeError("hook failure"))

    monotonic_values = iter([0.0, 1.0, 2.0])
    fn_impl = _FailNTimesThenSucceed(n=2, value="ok")

    with patch(
        "custom_components.ha_timescaledb_recorder.retry.time.monotonic",
        side_effect=monotonic_values,
    ):
        @retry_until_success(
            stop_event=ev,
            on_sustained_fail=on_sustained_fail,
            sustained_fail_seconds=0.5,
            backoff_schedule=(0,),
        )
        def fn():
            return fn_impl()

        # Must not propagate hook exception; retry loop continues to success
        result = fn()

    assert result == "ok"
    on_sustained_fail.assert_called_once()


def test_per_wrapper_independent_state():
    """Two wrappers created by separate calls to retry_until_success have independent stalled state."""
    ev = threading.Event()
    on_recovery_a = MagicMock()
    on_recovery_b = MagicMock()

    fn_impl_a = _FailNTimesThenSucceed(n=5, value="a")
    fn_impl_b = _FailNTimesThenSucceed(n=0, value="b")  # succeeds immediately

    @retry_until_success(
        stop_event=ev,
        on_recovery=on_recovery_a,
        backoff_schedule=(0,),
    )
    def fn_a():
        return fn_impl_a()

    @retry_until_success(
        stop_event=ev,
        on_recovery=on_recovery_b,
        backoff_schedule=(0,),
    )
    def fn_b():
        return fn_impl_b()

    fn_a()  # stalls at attempt 5, then recovers → on_recovery_a fires
    fn_b()  # succeeds immediately, never stalls → on_recovery_b does NOT fire

    on_recovery_a.assert_called_once()
    on_recovery_b.assert_not_called()


def test_on_sustained_fail_uses_monotonic_not_wall_clock():
    """Sustained-fail timing uses time.monotonic, not time.time.

    Validates MEDIUM-7 fix: patch retry.time.monotonic (module-local reference),
    NOT time.time, and verify on_sustained_fail fires when monotonic time crosses
    the threshold but NOT when it is under.
    """
    ev = threading.Event()

    # --- Case A: monotonic time DOES exceed threshold → should fire ---
    on_sustained_fail_fires = MagicMock()
    # first failure at t=0.0, second failure checked at t=1.0 → 1.0 >= 0.5 → fires
    monotonic_exceeds = iter([0.0, 1.0])
    fn_exceeds = _FailNTimesThenSucceed(n=2, value="ok")

    with patch(
        "custom_components.ha_timescaledb_recorder.retry.time.monotonic",
        side_effect=monotonic_exceeds,
    ):
        @retry_until_success(
            stop_event=ev,
            on_sustained_fail=on_sustained_fail_fires,
            sustained_fail_seconds=0.5,
            backoff_schedule=(0,),
        )
        def fn_a():
            return fn_exceeds()

        fn_a()

    on_sustained_fail_fires.assert_called_once()

    # --- Case B: monotonic time does NOT exceed threshold → should NOT fire ---
    on_sustained_fail_no_fire = MagicMock()
    # first failure at t=0.0, second at t=0.3 → 0.3 < 0.5 → does NOT fire
    monotonic_under = iter([0.0, 0.3, 0.4])
    fn_under = _FailNTimesThenSucceed(n=2, value="ok")

    with patch(
        "custom_components.ha_timescaledb_recorder.retry.time.monotonic",
        side_effect=monotonic_under,
    ):
        @retry_until_success(
            stop_event=ev,
            on_sustained_fail=on_sustained_fail_no_fire,
            sustained_fail_seconds=0.5,
            backoff_schedule=(0,),
        )
        def fn_b():
            return fn_under()

        fn_b()

    on_sustained_fail_no_fire.assert_not_called()
