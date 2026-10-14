"""Tests for watchdog.py — async supervisor for states_worker + meta_worker threads.

Tests cover:
- watchdog_loop is a coroutine function
- Early return when loop_stop_event is preset
- No-op when both workers are alive
- Dead states_worker triggers notify + 5s sleep + respawn
- Dead meta_worker triggers notify + 5s sleep + respawn
- Notification fired with correct _last_exception / _last_context
- asyncio.sleep(5) called BEFORE new_thread.start() (MEDIUM-5 throttle)
- No respawn when stop_event is set (shutdown in progress)
- Defensive handling of None exc and missing _last_exception attribute
- Poll-body exception does not kill the watchdog loop (MEDIUM-9)
- Polling cadence is interruptible by loop_stop_event
- Stop listener removed on exit only if it has not fired
- spawn_states_worker constructs thread from runtime fields
- spawn_meta_worker constructs thread from runtime fields
- Factories return unstarted threads
- Ingestion stall detection: in-flight DB call / backfill wait past the
  threshold raises ingestion_stalled once, clears on progress (issue #14)
"""
from __future__ import annotations

import asyncio
import inspect
import threading
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from homeassistant.core import CoreState

from custom_components.timescaledb_recorder.const import INGESTION_STALL_THRESHOLD_S
from custom_components.timescaledb_recorder.watchdog import (
    _check_stalls,
    _StallTracker,
    spawn_meta_worker,
    spawn_states_worker,
    watchdog_loop,
)

_WD = "custom_components.timescaledb_recorder.watchdog"


# ---------------------------------------------------------------------------
# Test-local runtime stand-in
# ---------------------------------------------------------------------------


@dataclass
class _MockRuntime:
    """Minimal TimescaledbRecorderData stand-in for watchdog tests.

    Real TimescaledbRecorderData will be extended in Plan 07 with dsn, chunk_interval_days,
    compress_after_hours, etc. Tests here use this dataclass to avoid depending
    on Plan 07 work landing first.
    """

    states_worker: object
    meta_worker: object
    loop_stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    stop_event: object = None
    # Fields required by spawn factories (only accessed in factory tests).
    dsn: str = "postgresql://test"
    live_queue: object = None
    backfill_queue: object = None
    backfill_request: object = None
    meta_queue: object = None
    registry_listener: object = None
    chunk_interval_days: int = 7
    compress_after_hours: int = 2
    states_stats: object = None


def _alive_worker():
    """Return a MagicMock that simulates a running thread."""
    w = MagicMock()
    w.is_alive.return_value = True
    w._last_exception = None
    w._last_context = {}
    w.db_calls.oldest.return_value = None
    w.backfill_wait_since = None
    return w


def _dead_worker():
    """Return a MagicMock that simulates a dead thread with post-mortem context."""
    w = MagicMock()
    w.is_alive.return_value = False
    w._last_exception = RuntimeError("test failure")
    w._last_context = {
        "at": "2026-04-22T12:00:00+00:00",
        "mode": "live",
        "retry_attempt": 7,
        "last_op": "insert_chunk",
    }
    return w


# ---------------------------------------------------------------------------
# Basic contract tests
# ---------------------------------------------------------------------------


def test_watchdog_loop_is_coroutine():
    """watchdog_loop must be an async coroutine function (inspect.iscoroutinefunction)."""
    assert inspect.iscoroutinefunction(watchdog_loop)


@pytest.mark.asyncio
async def test_watchdog_returns_immediately_when_loop_stop_event_preset():
    """When loop_stop_event is already set, watchdog_loop returns without polling."""
    hass = MagicMock()
    states_w = _alive_worker()
    meta_w = _alive_worker()
    stop_event = threading.Event()

    runtime = _MockRuntime(
        states_worker=states_w,
        meta_worker=meta_w,
        stop_event=stop_event,
    )
    runtime.loop_stop_event.set()

    await watchdog_loop(hass, runtime)

    # No polling happened
    states_w.is_alive.assert_not_called()
    meta_w.is_alive.assert_not_called()


@pytest.mark.asyncio
async def test_watchdog_no_action_when_both_workers_alive():
    """When both workers are alive, watchdog does nothing and exits when stop is set."""
    hass = MagicMock()
    states_w = _alive_worker()
    meta_w = _alive_worker()
    stop_event = threading.Event()

    runtime = _MockRuntime(
        states_worker=states_w,
        meta_worker=meta_w,
        stop_event=stop_event,
    )

    with patch(
        "custom_components.timescaledb_recorder.watchdog.WATCHDOG_INTERVAL_S",
        0.01,
    ), patch(
        "custom_components.timescaledb_recorder.watchdog.notify_watchdog_recovery"
    ) as mock_notify, patch(
        "custom_components.timescaledb_recorder.watchdog.asyncio.sleep",
        new_callable=AsyncMock,
    ) as mock_sleep:
        # Allow one poll cycle then signal stop
        async def _set_stop_after_poll(*args, **kwargs):
            raise asyncio.TimeoutError

        orig_wait_for = asyncio.wait_for
        call_count = 0

        async def _patched_wait_for(coro, timeout):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                # Second call: simulate loop_stop_event being set
                runtime.loop_stop_event.set()
                return  # simulates normal return (event set)
            raise asyncio.TimeoutError

        with patch("asyncio.wait_for", side_effect=_patched_wait_for):
            await watchdog_loop(hass, runtime)

    # notify must NOT have been called since both workers were alive
    mock_notify.assert_not_called()
    # asyncio.sleep(5) must NOT have been called
    mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# Dead worker respawn tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchdog_respawns_dead_states_worker():
    """Dead states_worker triggers respawn; runtime.states_worker is replaced."""
    hass = MagicMock()
    dead_states = _dead_worker()
    meta_w = _alive_worker()
    stop_event = threading.Event()

    runtime = _MockRuntime(
        states_worker=dead_states,
        meta_worker=meta_w,
        stop_event=stop_event,
    )

    new_thread = MagicMock()
    mock_spawn = MagicMock(return_value=new_thread)

    with patch(
        "custom_components.timescaledb_recorder.watchdog.WATCHDOG_INTERVAL_S",
        0.01,
    ), patch(
        "custom_components.timescaledb_recorder.watchdog.spawn_states_worker",
        mock_spawn,
    ), patch(
        "custom_components.timescaledb_recorder.watchdog.notify_watchdog_recovery"
    ), patch(
        "custom_components.timescaledb_recorder.watchdog.asyncio.sleep",
        new_callable=AsyncMock,
    ):
        call_count = 0

        async def _patched_wait_for(coro, timeout):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                runtime.loop_stop_event.set()
                return
            raise asyncio.TimeoutError

        with patch("asyncio.wait_for", side_effect=_patched_wait_for):
            await watchdog_loop(hass, runtime)

    # Runtime reference must be replaced with the new thread
    assert runtime.states_worker is new_thread
    # New thread must have been started
    new_thread.start.assert_called_once()


@pytest.mark.asyncio
async def test_watchdog_respawns_dead_meta_worker():
    """Dead meta_worker triggers respawn; runtime.meta_worker is replaced."""
    hass = MagicMock()
    states_w = _alive_worker()
    dead_meta = _dead_worker()
    stop_event = threading.Event()

    runtime = _MockRuntime(
        states_worker=states_w,
        meta_worker=dead_meta,
        stop_event=stop_event,
    )

    new_thread = MagicMock()
    mock_spawn = MagicMock(return_value=new_thread)

    with patch(
        "custom_components.timescaledb_recorder.watchdog.WATCHDOG_INTERVAL_S",
        0.01,
    ), patch(
        "custom_components.timescaledb_recorder.watchdog.spawn_meta_worker",
        mock_spawn,
    ), patch(
        "custom_components.timescaledb_recorder.watchdog.notify_watchdog_recovery"
    ), patch(
        "custom_components.timescaledb_recorder.watchdog.asyncio.sleep",
        new_callable=AsyncMock,
    ):
        call_count = 0

        async def _patched_wait_for(coro, timeout):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                runtime.loop_stop_event.set()
                return
            raise asyncio.TimeoutError

        with patch("asyncio.wait_for", side_effect=_patched_wait_for):
            await watchdog_loop(hass, runtime)

    assert runtime.meta_worker is new_thread
    new_thread.start.assert_called_once()


@pytest.mark.asyncio
async def test_watchdog_fires_notify_with_last_exception_and_context():
    """notify_watchdog_recovery is called with the dead thread's _last_exception/_last_context."""
    hass = MagicMock()
    dead_states = _dead_worker()
    meta_w = _alive_worker()
    stop_event = threading.Event()

    runtime = _MockRuntime(
        states_worker=dead_states,
        meta_worker=meta_w,
        stop_event=stop_event,
    )

    new_thread = MagicMock()
    mock_spawn = MagicMock(return_value=new_thread)

    with patch(
        "custom_components.timescaledb_recorder.watchdog.WATCHDOG_INTERVAL_S",
        0.01,
    ), patch(
        "custom_components.timescaledb_recorder.watchdog.spawn_states_worker",
        mock_spawn,
    ), patch(
        "custom_components.timescaledb_recorder.watchdog.notify_watchdog_recovery"
    ) as mock_notify, patch(
        "custom_components.timescaledb_recorder.watchdog.asyncio.sleep",
        new_callable=AsyncMock,
    ):
        call_count = 0

        async def _patched_wait_for(coro, timeout):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                runtime.loop_stop_event.set()
                return
            raise asyncio.TimeoutError

        with patch("asyncio.wait_for", side_effect=_patched_wait_for):
            await watchdog_loop(hass, runtime)

    mock_notify.assert_called_once_with(
        hass,
        component="states_worker",
        exc=dead_states._last_exception,
        context=dead_states._last_context,
    )


@pytest.mark.asyncio
async def test_watchdog_sleeps_5s_before_respawn():
    """asyncio.sleep(5) is called BEFORE new_thread.start() (MEDIUM-5 throttle).

    Call order must be: notify_watchdog_recovery → asyncio.sleep(5) → start().
    """
    hass = MagicMock()
    dead_states = _dead_worker()
    meta_w = _alive_worker()
    stop_event = threading.Event()

    runtime = _MockRuntime(
        states_worker=dead_states,
        meta_worker=meta_w,
        stop_event=stop_event,
    )

    new_thread = MagicMock()
    mock_spawn = MagicMock(return_value=new_thread)
    call_order = []

    async def _mock_sleep(seconds):
        call_order.append(("sleep", seconds))

    def _mock_notify(*args, **kwargs):
        call_order.append(("notify", kwargs.get("component")))

    def _mock_start():
        call_order.append(("start",))

    new_thread.start = _mock_start

    with patch(
        "custom_components.timescaledb_recorder.watchdog.WATCHDOG_INTERVAL_S",
        0.01,
    ), patch(
        "custom_components.timescaledb_recorder.watchdog.spawn_states_worker",
        mock_spawn,
    ), patch(
        "custom_components.timescaledb_recorder.watchdog.notify_watchdog_recovery",
        side_effect=_mock_notify,
    ), patch(
        "custom_components.timescaledb_recorder.watchdog.asyncio.sleep",
        side_effect=_mock_sleep,
    ):
        call_count = 0

        async def _patched_wait_for(coro, timeout):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                runtime.loop_stop_event.set()
                return
            raise asyncio.TimeoutError

        with patch("asyncio.wait_for", side_effect=_patched_wait_for):
            await watchdog_loop(hass, runtime)

    # Verify: notify → sleep(5) → start
    assert ("notify", "states_worker") in call_order
    assert ("sleep", 5) in call_order
    assert ("start",) in call_order

    notify_idx = call_order.index(("notify", "states_worker"))
    sleep_idx = call_order.index(("sleep", 5))
    start_idx = call_order.index(("start",))
    assert notify_idx < sleep_idx < start_idx, (
        f"Expected notify < sleep < start but got: {call_order}"
    )


@pytest.mark.asyncio
async def test_watchdog_skips_respawn_when_stop_event_set():
    """When runtime.stop_event is set, watchdog does NOT respawn a dead worker."""
    hass = MagicMock()
    dead_states = _dead_worker()
    meta_w = _alive_worker()
    stop_event = threading.Event()
    stop_event.set()  # Shutdown in progress

    runtime = _MockRuntime(
        states_worker=dead_states,
        meta_worker=meta_w,
        stop_event=stop_event,
    )

    with patch(
        "custom_components.timescaledb_recorder.watchdog.WATCHDOG_INTERVAL_S",
        0.01,
    ), patch(
        "custom_components.timescaledb_recorder.watchdog.notify_watchdog_recovery"
    ) as mock_notify, patch(
        "custom_components.timescaledb_recorder.watchdog.asyncio.sleep",
        new_callable=AsyncMock,
    ) as mock_sleep:
        call_count = 0

        async def _patched_wait_for(coro, timeout):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                runtime.loop_stop_event.set()
                return
            raise asyncio.TimeoutError

        with patch("asyncio.wait_for", side_effect=_patched_wait_for):
            await watchdog_loop(hass, runtime)

    mock_notify.assert_not_called()
    mock_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_watchdog_defensive_none_exc_and_missing_context():
    """Dead thread with _last_exception=None + no _last_context attribute still fires notify.

    Verifies getattr(..., None) defensive reads don't crash.
    """
    hass = MagicMock()
    # A thread missing both _last_exception and _last_context attributes entirely
    dead_states = MagicMock()
    dead_states.is_alive.return_value = False
    # Deliberately do NOT set _last_exception or _last_context — getattr with default handles it

    # Delete the attributes to simulate a thread without them
    del dead_states._last_exception
    del dead_states._last_context

    meta_w = _alive_worker()
    stop_event = threading.Event()

    runtime = _MockRuntime(
        states_worker=dead_states,
        meta_worker=meta_w,
        stop_event=stop_event,
    )

    new_thread = MagicMock()
    mock_spawn = MagicMock(return_value=new_thread)

    with patch(
        "custom_components.timescaledb_recorder.watchdog.WATCHDOG_INTERVAL_S",
        0.01,
    ), patch(
        "custom_components.timescaledb_recorder.watchdog.spawn_states_worker",
        mock_spawn,
    ), patch(
        "custom_components.timescaledb_recorder.watchdog.notify_watchdog_recovery"
    ) as mock_notify, patch(
        "custom_components.timescaledb_recorder.watchdog.asyncio.sleep",
        new_callable=AsyncMock,
    ):
        call_count = 0

        async def _patched_wait_for(coro, timeout):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                runtime.loop_stop_event.set()
                return
            raise asyncio.TimeoutError

        with patch("asyncio.wait_for", side_effect=_patched_wait_for):
            # Must NOT raise
            await watchdog_loop(hass, runtime)

    # notify must have been called with exc=None (default from getattr)
    mock_notify.assert_called_once_with(
        hass,
        component="states_worker",
        exc=None,
        context={},
    )


# ---------------------------------------------------------------------------
# MEDIUM-9: poll-body exception resilience
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchdog_poll_body_exception_does_not_kill_loop():
    """A bug in one poll cycle (is_alive raises AttributeError) must not kill the watchdog.

    The loop should log a WARNING and continue. Assert the loop survives at least
    two cycles despite the exception on the first cycle.
    """
    hass = MagicMock()
    states_w = MagicMock()
    states_w.is_alive.side_effect = AttributeError("boom — simulated poll-body bug")
    meta_w = _alive_worker()
    stop_event = threading.Event()

    runtime = _MockRuntime(
        states_worker=states_w,
        meta_worker=meta_w,
        stop_event=stop_event,
    )

    cycles_after_error = 0

    with patch(
        "custom_components.timescaledb_recorder.watchdog.WATCHDOG_INTERVAL_S",
        0.01,
    ):
        call_count = 0

        async def _patched_wait_for(coro, timeout):
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                # After 2 poll cycles, stop the loop
                runtime.loop_stop_event.set()
                return
            raise asyncio.TimeoutError

        with patch("asyncio.wait_for", side_effect=_patched_wait_for):
            # Must NOT raise despite AttributeError inside poll body
            await watchdog_loop(hass, runtime)

    # Loop ran at least 2 cycles (call_count >= 2 before stopping)
    assert call_count >= 2, f"Expected at least 2 cycles, got {call_count}"


# ---------------------------------------------------------------------------
# Cadence + interruptibility test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchdog_polls_cadence_interruptible_by_loop_stop_event():
    """watchdog_loop polls workers multiple times before loop_stop_event stops it."""
    hass = MagicMock()
    states_w = _alive_worker()
    meta_w = _alive_worker()
    stop_event = threading.Event()

    runtime = _MockRuntime(
        states_worker=states_w,
        meta_worker=meta_w,
        stop_event=stop_event,
    )

    poll_count = 0

    with patch(
        "custom_components.timescaledb_recorder.watchdog.WATCHDOG_INTERVAL_S",
        0.01,
    ):
        call_count = 0

        async def _patched_wait_for(coro, timeout):
            nonlocal call_count, poll_count
            call_count += 1
            if call_count >= 4:
                runtime.loop_stop_event.set()
                return
            raise asyncio.TimeoutError

        with patch("asyncio.wait_for", side_effect=_patched_wait_for):
            await watchdog_loop(hass, runtime)

    # states_worker.is_alive should have been called at least twice (3 poll cycles)
    assert states_w.is_alive.call_count >= 2


# ---------------------------------------------------------------------------
# EVENT_HOMEASSISTANT_STOP listener cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchdog_does_not_remove_fired_stop_listener():
    """A fired one-time stop listener is not removed again on exit (#22)."""
    hass = MagicMock()
    remover = MagicMock()
    hass.bus.async_listen_once.return_value = remover
    runtime = _MockRuntime(
        states_worker=_alive_worker(),
        meta_worker=_alive_worker(),
        stop_event=threading.Event(),
    )

    task = asyncio.create_task(watchdog_loop(hass, runtime))
    await asyncio.sleep(0)
    on_ha_stop = hass.bus.async_listen_once.call_args.args[1]
    on_ha_stop(None)
    await task

    assert runtime.loop_stop_event.is_set()
    remover.assert_not_called()


@pytest.mark.asyncio
async def test_watchdog_removes_unfired_stop_listener():
    """Exit via unload (listener never fired) removes the stop listener."""
    hass = MagicMock()
    remover = MagicMock()
    hass.bus.async_listen_once.return_value = remover
    runtime = _MockRuntime(
        states_worker=_alive_worker(),
        meta_worker=_alive_worker(),
        stop_event=threading.Event(),
    )
    runtime.loop_stop_event.set()

    await watchdog_loop(hass, runtime)

    remover.assert_called_once_with()


# ---------------------------------------------------------------------------
# Factory tests
# ---------------------------------------------------------------------------


def test_spawn_states_worker_uses_runtime_fields():
    """spawn_states_worker constructs TimescaledbStateRecorderThread from runtime fields."""
    hass = MagicMock()

    live_q = MagicMock()
    backfill_q = MagicMock()
    backfill_req = MagicMock()
    stop_ev = threading.Event()

    runtime = _MockRuntime(
        states_worker=MagicMock(),
        meta_worker=MagicMock(),
        stop_event=stop_ev,
        dsn="postgresql://testhost/testdb",
        live_queue=live_q,
        backfill_queue=backfill_q,
        backfill_request=backfill_req,
        chunk_interval_days=14,
        compress_after_hours=4,
    )

    with patch(
        "custom_components.timescaledb_recorder.watchdog.TimescaledbStateRecorderThread"
    ) as MockThread:
        mock_instance = MagicMock()
        MockThread.return_value = mock_instance

        result = spawn_states_worker(hass, runtime)

    MockThread.assert_called_once_with(
        hass=hass,
        dsn="postgresql://testhost/testdb",
        live_queue=live_q,
        backfill_queue=backfill_q,
        backfill_request=backfill_req,
        stop_event=stop_ev,
        chunk_interval_days=14,
        compress_after_hours=4,
        stats=runtime.states_stats,
    )
    assert result is mock_instance


def test_spawn_meta_worker_uses_runtime_fields():
    """spawn_meta_worker constructs TimescaledbMetaRecorderThread from runtime fields."""
    hass = MagicMock()

    meta_q = MagicMock()
    registry_listener = MagicMock()
    stop_ev = threading.Event()

    runtime = _MockRuntime(
        states_worker=MagicMock(),
        meta_worker=MagicMock(),
        stop_event=stop_ev,
        dsn="postgresql://testhost/testdb",
        meta_queue=meta_q,
        registry_listener=registry_listener,
    )

    with patch(
        "custom_components.timescaledb_recorder.watchdog.TimescaledbMetaRecorderThread"
    ) as MockThread:
        mock_instance = MagicMock()
        MockThread.return_value = mock_instance

        result = spawn_meta_worker(hass, runtime)

    MockThread.assert_called_once_with(
        hass=hass,
        dsn="postgresql://testhost/testdb",
        meta_queue=meta_q,
        registry_listener=registry_listener,
        stop_event=stop_ev,
    )
    assert result is mock_instance


def test_spawn_factories_do_not_start_thread():
    """Both spawn factories return an unstarted thread — caller calls .start()."""
    hass = MagicMock()
    stop_ev = threading.Event()

    runtime = _MockRuntime(
        states_worker=MagicMock(),
        meta_worker=MagicMock(),
        stop_event=stop_ev,
    )

    with patch(
        "custom_components.timescaledb_recorder.watchdog.TimescaledbStateRecorderThread"
    ) as MockStates, patch(
        "custom_components.timescaledb_recorder.watchdog.TimescaledbMetaRecorderThread"
    ) as MockMeta:
        mock_states_instance = MagicMock()
        mock_meta_instance = MagicMock()
        MockStates.return_value = mock_states_instance
        MockMeta.return_value = mock_meta_instance

        states_result = spawn_states_worker(hass, runtime)
        meta_result = spawn_meta_worker(hass, runtime)

    # .start() must NOT have been called by the factory
    mock_states_instance.start.assert_not_called()
    mock_meta_instance.start.assert_not_called()


# ---------------------------------------------------------------------------
# Ingestion stall detection (issue #14)
# ---------------------------------------------------------------------------


def _run_check(hass, runtime, tracker, now):
    with patch(f"{_WD}.time.monotonic", return_value=now), patch(
        f"{_WD}.persistent_notification"
    ) as pn, patch(f"{_WD}.create_ingestion_stalled_issue") as create, patch(
        f"{_WD}.clear_ingestion_stalled_issue"
    ) as clear:
        _check_stalls(hass, runtime, tracker)
    return pn, create, clear


def test_hung_db_call_raises_once_then_clears(caplog):
    hass = MagicMock()
    states_w = _alive_worker()
    states_w._last_op = "flush_live_timeout"
    runtime = _MockRuntime(states_worker=states_w, meta_worker=_alive_worker())
    tracker = _StallTracker()
    states_w.db_calls.oldest.return_value = ("insert_chunk", 1000.0)

    pn, create, _ = _run_check(hass, runtime, tracker, 1000.0 + INGESTION_STALL_THRESHOLD_S - 1)
    create.assert_not_called()
    pn.async_create.assert_not_called()

    with caplog.at_level("ERROR"):
        pn, create, _ = _run_check(hass, runtime, tracker, 1000.0 + INGESTION_STALL_THRESHOLD_S + 1)
    pn.async_create.assert_called_once()
    create.assert_called_once()
    details = create.call_args.args[1]
    assert "states_worker" in details
    assert "insert_chunk" in details
    assert "flush_live_timeout" in details
    assert "Ingestion stalled" in caplog.text

    # Still hung on later ticks: reported once, not every tick.
    pn, create, clear = _run_check(hass, runtime, tracker, 1000.0 + 2 * INGESTION_STALL_THRESHOLD_S)
    create.assert_not_called()
    pn.async_create.assert_not_called()
    clear.assert_not_called()

    states_w.db_calls.oldest.return_value = None
    with caplog.at_level("INFO"):
        _, _, clear = _run_check(hass, runtime, tracker, 1000.0 + 2 * INGESTION_STALL_THRESHOLD_S + 10)
    clear.assert_called_once_with(hass)
    assert "Ingestion resumed" in caplog.text


def test_issue_keeps_other_stalled_worker_when_one_recovers():
    hass = MagicMock()
    states_w, meta_w = _alive_worker(), _alive_worker()
    runtime = _MockRuntime(states_worker=states_w, meta_worker=meta_w)
    tracker = _StallTracker()
    states_w.db_calls.oldest.return_value = ("insert_chunk", 0.0)
    meta_w.db_calls.oldest.return_value = ("write_item", 0.0)
    now = INGESTION_STALL_THRESHOLD_S + 1

    _, create, _ = _run_check(hass, runtime, tracker, now)
    assert "states_worker" in create.call_args.args[1]
    assert "meta_worker" in create.call_args.args[1]

    states_w.db_calls.oldest.return_value = None
    _, create, clear = _run_check(hass, runtime, tracker, now + 10)
    clear.assert_not_called()
    assert "meta_worker" in create.call_args.args[1]
    assert "states_worker" not in create.call_args.args[1]


def test_dead_worker_is_not_reported_as_stalled():
    hass = MagicMock()
    dead = _dead_worker()
    dead.db_calls.oldest.return_value = ("insert_chunk", 0.0)
    runtime = _MockRuntime(states_worker=dead, meta_worker=_alive_worker())
    _, create, _ = _run_check(hass, runtime, _StallTracker(), INGESTION_STALL_THRESHOLD_S * 10)
    create.assert_not_called()


def test_backfill_wait_counts_only_from_ha_running():
    """The orchestrator starts at homeassistant_started: a long HA startup must
    not count as the states worker waiting for backfill data."""
    hass = MagicMock()
    states_w = _alive_worker()
    states_w.backfill_wait_since = 0.0
    runtime = _MockRuntime(states_worker=states_w, meta_worker=_alive_worker())
    tracker = _StallTracker()

    hass.state = CoreState.starting
    _, create, _ = _run_check(hass, runtime, tracker, 2 * INGESTION_STALL_THRESHOLD_S)
    create.assert_not_called()

    hass.state = CoreState.running
    t_running = 2 * INGESTION_STALL_THRESHOLD_S + 1
    _, create, _ = _run_check(hass, runtime, tracker, t_running)
    create.assert_not_called()
    _, create, _ = _run_check(hass, runtime, tracker, t_running + INGESTION_STALL_THRESHOLD_S - 1)
    create.assert_not_called()
    _, create, _ = _run_check(hass, runtime, tracker, t_running + INGESTION_STALL_THRESHOLD_S + 1)
    create.assert_called_once()
    assert "backfill" in create.call_args.args[1]


@pytest.mark.asyncio
async def test_stall_check_exception_does_not_kill_loop():
    hass = MagicMock()
    runtime = _MockRuntime(
        states_worker=_alive_worker(), meta_worker=_alive_worker(), stop_event=threading.Event(),
    )
    calls = 0

    async def _patched_wait_for(coro, timeout):
        nonlocal calls
        coro.close()
        calls += 1
        if calls >= 3:
            runtime.loop_stop_event.set()
            return
        raise asyncio.TimeoutError

    with patch(f"{_WD}._check_stalls", side_effect=RuntimeError("bug")) as check, patch(
        "asyncio.wait_for", side_effect=_patched_wait_for
    ):
        await watchdog_loop(hass, runtime)
    assert check.call_count == 2
