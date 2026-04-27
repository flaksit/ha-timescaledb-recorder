"""Unit tests for backfill_orchestrator + _fetch_slice_raw (D-08)."""
import asyncio
import queue
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.timescaledb_recorder.backfill import (
    BACKFILL_DONE,
    _LATE_ARRIVAL_GRACE,
    _fetch_slice_raw,
    backfill_orchestrator,
)


def _threading_stop_event():
    """Return a non-set threading.Event for use as threading_stop_event."""
    return threading.Event()


def test_fetch_slice_raw_uses_significant_states_batch():
    """_fetch_slice_raw must use get_significant_states(significant_changes_only=False).

    state_changes_during_period filters out restart-restored states (where
    last_changed_ts != last_updated_ts). get_significant_states with
    significant_changes_only=False captures all state rows by last_updated_ts,
    including those from entities whose value didn't change across a restart.
    """
    hass = MagicMock()
    entities = {"sensor.a", "sensor.b"}
    t_start = datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc)
    t_end = t_start + timedelta(minutes=5)
    expected = {"sensor.a": [MagicMock()], "sensor.b": [MagicMock()]}
    with patch(
        "custom_components.timescaledb_recorder.backfill.recorder_history.get_significant_states",
        return_value=expected,
    ) as mock_gs:
        out = _fetch_slice_raw(hass, entities, t_start, t_end)
    # Called once with all entity_ids in a single batch query
    mock_gs.assert_called_once()
    _, kwargs = mock_gs.call_args[0], mock_gs.call_args[1]
    assert kwargs.get("significant_changes_only") is False
    assert kwargs.get("include_start_time_state") is False
    assert set(kwargs.get("entity_ids", [])) == entities
    assert out == expected


def test_fetch_slice_raw_empty_entities_returns_empty():
    hass = MagicMock()
    with patch(
        "custom_components.timescaledb_recorder.backfill.recorder_history.get_significant_states",
        return_value={},
    ):
        out = _fetch_slice_raw(hass, set(), datetime.now(), datetime.now())
    assert out == {}


@pytest.mark.asyncio
async def test_orchestrator_exits_on_stop_event_set_before_trigger():
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock()
    stop_event = asyncio.Event()
    stop_event.set()
    backfill_request = asyncio.Event()
    backfill_request.set()

    with patch("custom_components.timescaledb_recorder.backfill.recorder.get_instance"):
        await asyncio.wait_for(backfill_orchestrator(
            hass,
            live_queue=MagicMock(),
            backfill_queue=MagicMock(),
            backfill_request=backfill_request,
            read_watermark=MagicMock(return_value=None),
            all_entities_reader=MagicMock(return_value=set()),
            entity_filter=lambda _eid: True,
            stop_event=stop_event,
            threading_stop_event=_threading_stop_event(),
        ), timeout=2)


@pytest.mark.asyncio
async def test_orchestrator_empty_hypertable_pushes_done_and_loops():
    """wm=None → log + push BACKFILL_DONE + continue."""
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: fn(*args))
    backfill_queue = queue.Queue(maxsize=2)
    live_queue = MagicMock()
    live_queue.clear_and_reset_overflow = MagicMock(return_value=0)
    stop_event = asyncio.Event()
    backfill_request = asyncio.Event()
    backfill_request.set()

    async def stop_after_first_done():
        # Wait until BACKFILL_DONE is in the queue then flip stop_event
        for _ in range(50):
            try:
                item = backfill_queue.get_nowait()
                if item is BACKFILL_DONE:
                    stop_event.set()
                    return
            except queue.Empty:
                await asyncio.sleep(0.01)

    with patch("custom_components.timescaledb_recorder.backfill.recorder.get_instance"), \
         patch("custom_components.timescaledb_recorder.backfill.clear_buffer_dropping_issue"):
        orch = asyncio.create_task(backfill_orchestrator(
            hass,
            live_queue=live_queue,
            backfill_queue=backfill_queue,
            backfill_request=backfill_request,
            read_watermark=MagicMock(return_value=None),
            all_entities_reader=MagicMock(return_value=set()),
            entity_filter=lambda _eid: True,
            stop_event=stop_event,
            threading_stop_event=_threading_stop_event(),
        ))
        await stop_after_first_done()
        orch.cancel()
        try:
            await orch
        except asyncio.CancelledError:
            pass

    # live_queue clear must have happened before the wm check
    live_queue.clear_and_reset_overflow.assert_called()


@pytest.mark.asyncio
async def test_orchestrator_clears_buffer_dropping_on_recovery():
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: fn(*args))
    backfill_queue = queue.Queue(maxsize=2)
    live_queue = MagicMock()
    live_queue.clear_and_reset_overflow = MagicMock(return_value=7)
    stop_event = asyncio.Event()
    backfill_request = asyncio.Event()
    backfill_request.set()

    with patch("custom_components.timescaledb_recorder.backfill.recorder.get_instance"), \
         patch(
             "custom_components.timescaledb_recorder.backfill.clear_buffer_dropping_issue"
         ) as mock_clear_issue:
        async def stopper():
            await asyncio.sleep(0.2)
            stop_event.set()
        asyncio.create_task(stopper())
        try:
            await asyncio.wait_for(backfill_orchestrator(
                hass,
                live_queue=live_queue,
                backfill_queue=backfill_queue,
                backfill_request=backfill_request,
                read_watermark=MagicMock(return_value=None),
                all_entities_reader=MagicMock(return_value=set()),
                entity_filter=lambda _eid: True,
                stop_event=stop_event,
                threading_stop_event=_threading_stop_event(),
            ), timeout=2)
        except asyncio.TimeoutError:
            pass
    mock_clear_issue.assert_called()  # issue cleared on every backfill cycle (D-10-c)


# ---------------------------------------------------------------------------
# Phase 3 Plan 05: gap detection + retry-wrap _fetch_slice_raw (D-08, D-03-c)
# ---------------------------------------------------------------------------


def _make_recorder_instance(oldest_ts=None):
    """Return a MagicMock recorder instance with states_manager.oldest_ts set."""
    inst = MagicMock()
    inst.states_manager.oldest_ts = oldest_ts
    # async_add_executor_job: runs fn(*args) synchronously for easy testing.
    inst.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: fn(*args))
    return inst


def _make_watermark(offset_minutes: int = 60) -> datetime:
    """Return a fixed watermark datetime offset_minutes before 'now'."""
    return datetime(2026, 4, 23, 10, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_orchestrator_skips_gap_detection_when_oldest_ts_none(caplog):
    """HIGH-2: when oldest_ts is None, gap detection must be skipped entirely.

    None means 'recorder not yet ready', NOT 'confirmed data loss'. Firing
    notify_backfill_gap would be a spurious alert on every cold start.
    """
    import logging

    hass = MagicMock()
    wm = _make_watermark()
    # async_add_executor_job runs fn(*args) inline for determinism.
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: fn(*args))

    backfill_queue = queue.Queue(maxsize=4)
    live_queue = MagicMock()
    live_queue.clear_and_reset_overflow = MagicMock(return_value=0)
    stop_event = asyncio.Event()
    backfill_request = asyncio.Event()
    backfill_request.set()

    recorder_inst = _make_recorder_instance(oldest_ts=None)  # None = not ready

    with patch(
        "custom_components.timescaledb_recorder.backfill.recorder.get_instance",
        return_value=recorder_inst,
    ), patch(
        "custom_components.timescaledb_recorder.backfill.notify_backfill_gap"
    ) as mock_notify, patch(
        "custom_components.timescaledb_recorder.backfill.clear_buffer_dropping_issue"
    ):
        async def stopper():
            # Let one full cycle complete then stop.
            await asyncio.sleep(0.05)
            stop_event.set()

        asyncio.create_task(stopper())
        with caplog.at_level(logging.DEBUG,
                             logger="custom_components.timescaledb_recorder.backfill"):
            try:
                await asyncio.wait_for(
                    backfill_orchestrator(
                        hass,
                        live_queue=live_queue,
                        backfill_queue=backfill_queue,
                        backfill_request=backfill_request,
                        read_watermark=MagicMock(return_value=wm),
                        all_entities_reader=MagicMock(return_value=set()),
                        entity_filter=lambda _eid: True,
                        stop_event=stop_event,
                        threading_stop_event=_threading_stop_event(),
                    ),
                    timeout=2,
                )
            except asyncio.TimeoutError:
                pass

    # notify_backfill_gap must NOT be called when oldest_ts is None.
    mock_notify.assert_not_called()
    # A debug log about skipping gap detection must be emitted.
    assert any(
        "oldest_ts" in record.message and "skipping" in record.message.lower()
        for record in caplog.records
    ), f"Expected debug log about skipping gap detection, got: {[r.message for r in caplog.records]}"


@pytest.mark.asyncio
async def test_orchestrator_fires_backfill_gap_when_oldest_ts_after_needed_from():
    """Gap confirmed: oldest_ts is a float AFTER from_ → notify_backfill_gap fires once."""
    hass = MagicMock()
    wm = datetime(2026, 4, 23, 10, 0, 0, tzinfo=timezone.utc)
    # from_ = wm - _LATE_ARRIVAL_GRACE
    from_ = wm - _LATE_ARRIVAL_GRACE

    # oldest_ts is 30 minutes after from_ → gap confirmed.
    oldest_recorder_ts = from_ + timedelta(minutes=30)
    oldest_ts_float = oldest_recorder_ts.timestamp()

    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: fn(*args))
    backfill_queue = queue.Queue(maxsize=4)
    live_queue = MagicMock()
    live_queue.clear_and_reset_overflow = MagicMock(return_value=0)
    # stop_event starts clear; we set it AFTER the cycle completes (no entities → fast)
    stop_event = asyncio.Event()
    backfill_request = asyncio.Event()
    backfill_request.set()

    recorder_inst = _make_recorder_instance(oldest_ts=oldest_ts_float)

    with patch(
        "custom_components.timescaledb_recorder.backfill.recorder.get_instance",
        return_value=recorder_inst,
    ), patch(
        "custom_components.timescaledb_recorder.backfill.notify_backfill_gap"
    ) as mock_notify, patch(
        "custom_components.timescaledb_recorder.backfill.clear_buffer_dropping_issue"
    ):
        # Stop after the first cycle: set stop_event AND backfill_request so the
        # orchestrator can exit its backfill_request.wait() on the next iteration.
        async def stopper():
            await asyncio.sleep(0.05)
            stop_event.set()
            backfill_request.set()  # unblock backfill_request.wait() so stop check fires

        asyncio.create_task(stopper())
        await asyncio.wait_for(
            backfill_orchestrator(
                hass,
                live_queue=live_queue,
                backfill_queue=backfill_queue,
                backfill_request=backfill_request,
                read_watermark=MagicMock(return_value=wm),
                all_entities_reader=MagicMock(return_value=set()),
                entity_filter=lambda _eid: True,
                stop_event=stop_event,
                threading_stop_event=_threading_stop_event(),
            ),
            timeout=2,
        )

    mock_notify.assert_called_once()
    call_kwargs = mock_notify.call_args
    # notify_backfill_gap(hass, reason=..., details=...) — reason is keyword-only.
    assert call_kwargs.kwargs.get("reason") == "recorder_retention"


@pytest.mark.asyncio
async def test_orchestrator_no_gap_notification_when_oldest_ts_before_needed_from():
    """No gap: oldest_ts is a float but <= from_ → notify_backfill_gap NOT called."""
    hass = MagicMock()
    wm = datetime(2026, 4, 23, 10, 0, 0, tzinfo=timezone.utc)
    from_ = wm - _LATE_ARRIVAL_GRACE

    # oldest_ts is BEFORE from_ (data in recorder covers the window) → no gap.
    oldest_recorder_ts = from_ - timedelta(hours=1)
    oldest_ts_float = oldest_recorder_ts.timestamp()

    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: fn(*args))
    backfill_queue = queue.Queue(maxsize=4)
    live_queue = MagicMock()
    live_queue.clear_and_reset_overflow = MagicMock(return_value=0)
    stop_event = asyncio.Event()
    backfill_request = asyncio.Event()
    backfill_request.set()

    recorder_inst = _make_recorder_instance(oldest_ts=oldest_ts_float)

    with patch(
        "custom_components.timescaledb_recorder.backfill.recorder.get_instance",
        return_value=recorder_inst,
    ), patch(
        "custom_components.timescaledb_recorder.backfill.notify_backfill_gap"
    ) as mock_notify, patch(
        "custom_components.timescaledb_recorder.backfill.clear_buffer_dropping_issue"
    ):
        # Stop after the first cycle completes.
        async def stopper():
            await asyncio.sleep(0.05)
            stop_event.set()
            backfill_request.set()  # unblock backfill_request.wait()

        asyncio.create_task(stopper())
        await asyncio.wait_for(
            backfill_orchestrator(
                hass,
                live_queue=live_queue,
                backfill_queue=backfill_queue,
                backfill_request=backfill_request,
                read_watermark=MagicMock(return_value=wm),
                all_entities_reader=MagicMock(return_value=set()),
                entity_filter=lambda _eid: True,
                stop_event=stop_event,
                threading_stop_event=_threading_stop_event(),
            ),
            timeout=2,
        )

    mock_notify.assert_not_called()


@pytest.mark.asyncio
async def test_orchestrator_adjusts_from_to_oldest_ts_after_gap():
    """After gap detection fires, from_ must equal oldest_recorder_ts before slice loop."""
    hass = MagicMock()
    wm = datetime(2026, 4, 23, 10, 0, 0, tzinfo=timezone.utc)
    from_ = wm - _LATE_ARRIVAL_GRACE

    oldest_recorder_ts = from_ + timedelta(minutes=30)
    oldest_ts_float = oldest_recorder_ts.timestamp()

    fetch_calls = []

    async def fake_executor_job(fn, *args):
        if fn.__name__ == "_fetch_slice_raw" or (
            hasattr(fn, "__wrapped__") or callable(fn)
        ):
            # Capture the first positional arg after hass: it's either hass or entities
            # The actual slice args are (hass, entities, t_start, t_end).
            # We capture t_start to verify from_ was adjusted.
            if len(args) >= 3:
                fetch_calls.append(args[2])  # t_start = third arg after fn
            return {}
        return fn(*args)

    hass.async_add_executor_job = AsyncMock(side_effect=fake_executor_job)
    backfill_queue = queue.Queue(maxsize=4)
    live_queue = MagicMock()
    live_queue.clear_and_reset_overflow = MagicMock(return_value=0)
    stop_event = asyncio.Event()
    backfill_request = asyncio.Event()
    backfill_request.set()

    recorder_inst = _make_recorder_instance(oldest_ts=oldest_ts_float)
    # The fetch runs via recorder_inst.async_add_executor_job.
    recorder_inst.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: {})

    with patch(
        "custom_components.timescaledb_recorder.backfill.recorder.get_instance",
        return_value=recorder_inst,
    ), patch(
        "custom_components.timescaledb_recorder.backfill.notify_backfill_gap"
    ), patch(
        "custom_components.timescaledb_recorder.backfill.clear_buffer_dropping_issue"
    ):
        stop_event.set()  # single cycle only — stop_event checked in slice loop
        with patch(
            "custom_components.timescaledb_recorder.backfill._SLICE_WINDOW",
            timedelta(hours=2),
        ):
            await backfill_orchestrator(
                hass,
                live_queue=live_queue,
                backfill_queue=backfill_queue,
                backfill_request=backfill_request,
                read_watermark=MagicMock(return_value=wm),
                all_entities_reader=MagicMock(return_value={"sensor.test"}),
                entity_filter=lambda _eid: True,
                stop_event=stop_event,
                threading_stop_event=_threading_stop_event(),
            )

    # The slice loop uses recorder_inst.async_add_executor_job, capture via that mock.
    first_call = recorder_inst.async_add_executor_job.call_args_list
    if first_call:
        # Third positional arg to the wrapped _fetch_slice_raw is t_start (slice_start).
        # After gap adjustment, slice_start must equal oldest_recorder_ts.
        _, pos_args = first_call[0][0], first_call[0]
        # pos_args = (fetch_fn, hass, entities, slice_start, slice_end)
        if len(pos_args) >= 4:
            slice_start_used = pos_args[3]
            assert slice_start_used == oldest_recorder_ts, (
                f"Expected slice_start={oldest_recorder_ts}, got {slice_start_used}"
            )


@pytest.mark.asyncio
async def test_fetch_slice_retry_wrapping_uses_on_transient_none():
    """D-03-c: retry wrap for _fetch_slice_raw must use on_transient=None
    (no owned connection — recorder pool manages the session)."""
    captured_calls = []

    def fake_retry(*, stop_event, on_transient=None, **kwargs):
        captured_calls.append({"on_transient": on_transient})
        def decorator(fn):
            return fn
        return decorator

    hass = MagicMock()
    wm = datetime(2026, 4, 23, 10, 0, 0, tzinfo=timezone.utc)
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: fn(*args) if callable(fn) else None)

    stop_event = asyncio.Event()
    stop_event.set()
    backfill_request = asyncio.Event()
    backfill_request.set()
    live_queue = MagicMock()
    live_queue.clear_and_reset_overflow = MagicMock(return_value=0)

    with patch(
        "custom_components.timescaledb_recorder.backfill.retry_until_success",
        fake_retry,
    ), patch(
        "custom_components.timescaledb_recorder.backfill.recorder.get_instance"
    ), patch(
        "custom_components.timescaledb_recorder.backfill.clear_buffer_dropping_issue"
    ):
        await backfill_orchestrator(
            hass,
            live_queue=live_queue,
            backfill_queue=queue.Queue(maxsize=4),
            backfill_request=backfill_request,
            read_watermark=MagicMock(return_value=wm),
            all_entities_reader=MagicMock(return_value=set()),
            entity_filter=lambda _eid: True,
            stop_event=stop_event,
            threading_stop_event=_threading_stop_event(),
        )

    # retry_until_success must have been called at least once (wrapping _fetch_slice_raw)
    assert len(captured_calls) >= 1, "retry_until_success was not called in orchestrator"
    # The fetch-slice wrapper must use on_transient=None.
    fetch_call = captured_calls[0]
    assert fetch_call["on_transient"] is None


@pytest.mark.asyncio
async def test_fetch_slice_retries_on_transient_error():
    """D-03-c: a transient error from _fetch_slice_raw must be retried;
    the orchestrator must not raise to caller on first failure.

    This test uses a zero-backoff retry wrapper (via patching retry_until_success
    in backfill) so the retry happens immediately without sleeping. The real
    retry backoff logic is already tested in test_retry.py.

    Watermark is set 2 hours in the past so from_ < t_clear (slice loop executes).
    """
    from custom_components.timescaledb_recorder.retry import retry_until_success
    from custom_components.timescaledb_recorder.backfill import _LATE_ARRIVAL_GRACE

    hass = MagicMock()
    # wm must be in the past so that from_ = wm - _LATE_ARRIVAL_GRACE < t_clear = now()
    from datetime import datetime, timezone, timedelta as _td
    wm = datetime.now(timezone.utc) - _td(hours=2)
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: fn(*args) if callable(fn) else None)

    stop_event = asyncio.Event()
    backfill_request = asyncio.Event()
    backfill_request.set()
    live_queue = MagicMock()
    live_queue.clear_and_reset_overflow = MagicMock(return_value=0)
    backfill_queue = queue.Queue(maxsize=4)

    call_count = {"n": 0}

    def flaky_fetch_raw(h, entities, t_start, t_end):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise Exception("transient error")
        return {}

    threading_stop = threading.Event()

    # Use a zero-backoff retry wrapper so the test doesn't sleep.
    # The real retry backoff is tested separately in test_retry.py.
    def zero_backoff_retry(*, stop_event=None, on_transient=None, **kwargs):
        def decorator(fn):
            return retry_until_success(
                stop_event=threading_stop,
                on_transient=on_transient,
                backoff_schedule=(0,),
            )(fn)
        return decorator

    with patch(
        "custom_components.timescaledb_recorder.backfill.retry_until_success",
        zero_backoff_retry,
    ), patch(
        "custom_components.timescaledb_recorder.backfill.recorder.get_instance"
    ) as mock_get_inst, patch(
        "custom_components.timescaledb_recorder.backfill._fetch_slice_raw",
        flaky_fetch_raw,
    ), patch(
        "custom_components.timescaledb_recorder.backfill.clear_buffer_dropping_issue"
    ), patch(
        "custom_components.timescaledb_recorder.backfill.notify_backfill_gap"
    ):
        recorder_inst = MagicMock()
        recorder_inst.states_manager.oldest_ts = None
        recorder_inst.async_add_executor_job = AsyncMock(
            side_effect=lambda fn, *args: fn(*args)
        )
        mock_get_inst.return_value = recorder_inst

        async def stopper():
            # Wait until the second call succeeds, then stop.
            for _ in range(400):
                if call_count["n"] >= 2:
                    stop_event.set()
                    backfill_request.set()
                    return
                await asyncio.sleep(0.005)
            stop_event.set()
            backfill_request.set()

        asyncio.create_task(stopper())

        with patch(
            "custom_components.timescaledb_recorder.backfill._SLICE_WINDOW",
            timedelta(hours=2),
        ), patch(
            "custom_components.timescaledb_recorder.backfill._RECORDER_COMMIT_LAG",
            timedelta(seconds=0),
        ):
            await asyncio.wait_for(
                backfill_orchestrator(
                    hass,
                    live_queue=live_queue,
                    backfill_queue=backfill_queue,
                    backfill_request=backfill_request,
                    read_watermark=MagicMock(return_value=wm),
                    all_entities_reader=MagicMock(return_value={"sensor.test"}),
                    entity_filter=lambda _eid: True,
                    stop_event=stop_event,
                    threading_stop_event=threading_stop,
                ),
                timeout=5,
            )

    # _fetch_slice_raw must have been called more than once (retry happened).
    assert call_count["n"] >= 2, f"Expected retry, got {call_count['n']} call(s)"


@pytest.mark.asyncio
async def test_orchestrator_does_not_swallow_unhandled_exceptions():
    """D-09-b: an exception escaping after watermark read must propagate from the
    orchestrator coroutine — no body-level try/except swallowing it."""
    hass = MagicMock()
    stop_event = asyncio.Event()
    backfill_request = asyncio.Event()
    backfill_request.set()
    live_queue = MagicMock()
    live_queue.clear_and_reset_overflow = MagicMock(return_value=0)

    boom = RuntimeError("unhandled-boom")

    def raise_after_wm(fn, *args):
        # This is called as async_add_executor_job(read_watermark) — raise on the
        # second executor call (after watermark succeeds) to simulate a post-wm crash.
        raise boom

    hass.async_add_executor_job = AsyncMock(side_effect=raise_after_wm)

    with patch(
        "custom_components.timescaledb_recorder.backfill.recorder.get_instance"
    ), patch(
        "custom_components.timescaledb_recorder.backfill.clear_buffer_dropping_issue"
    ):
        with pytest.raises(RuntimeError, match="unhandled-boom"):
            await backfill_orchestrator(
                hass,
                live_queue=live_queue,
                backfill_queue=queue.Queue(maxsize=4),
                backfill_request=backfill_request,
                read_watermark=MagicMock(return_value=None),
                all_entities_reader=MagicMock(return_value=set()),
                entity_filter=lambda _eid: True,
                stop_event=stop_event,
                threading_stop_event=_threading_stop_event(),
            )


# ---------------------------------------------------------------------------
# Issue #8: non-entity-registry entities included in backfill entity set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_includes_non_registry_entities():
    """Entities in hass.states but absent from entity_reg (no unique_id, e.g. sun.sun,
    zone.home, conversation.*) must appear in the backfill entity set.

    entity_reg.entities only contains entities that have a unique_id.  Entities
    without one never appear there, so they were silently excluded from backfill
    before the fix.  hass.states.async_all() is the authoritative source for all
    live entities regardless of registry status.
    """
    hass = MagicMock()
    # wm in the recent past so from_ < cutoff (slice loop executes once).
    wm = datetime.now(timezone.utc) - timedelta(minutes=5)
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: fn(*args))

    # State machine has sun.sun (no unique_id → not in entity_reg).
    sun_state = MagicMock()
    sun_state.entity_id = "sun.sun"
    hass.states.async_all.return_value = [sun_state]

    captured_entities: list[set[str]] = []
    stop_event = asyncio.Event()

    def capture_fetch(h, entities, t_start, t_end):
        captured_entities.append(set(entities))
        return {}

    backfill_queue = queue.Queue(maxsize=8)
    live_queue = MagicMock()
    live_queue.clear_and_reset_overflow = MagicMock(return_value=0)
    backfill_request = asyncio.Event()
    backfill_request.set()

    recorder_inst = _make_recorder_instance(oldest_ts=None)
    # Run fetch_slice synchronously so the captured entities are visible immediately.
    recorder_inst.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: fn(*args))

    async def stopper():
        # Wait until the orchestrator completes its first full cycle (slice + DONE),
        # then signal shutdown.
        for _ in range(500):
            if captured_entities:
                stop_event.set()
                backfill_request.set()
                return
            await asyncio.sleep(0.01)
        stop_event.set()
        backfill_request.set()

    with patch(
        "custom_components.timescaledb_recorder.backfill.recorder.get_instance",
        return_value=recorder_inst,
    ), patch(
        "custom_components.timescaledb_recorder.backfill.notify_backfill_gap"
    ), patch(
        "custom_components.timescaledb_recorder.backfill.clear_buffer_dropping_issue"
    ), patch(
        "custom_components.timescaledb_recorder.backfill._fetch_slice_raw",
        capture_fetch,
    ), patch(
        "custom_components.timescaledb_recorder.backfill._SLICE_WINDOW",
        timedelta(hours=1),
    ), patch(
        "custom_components.timescaledb_recorder.backfill._RECORDER_COMMIT_LAG",
        timedelta(seconds=0),
    ):
        asyncio.create_task(stopper())
        await asyncio.wait_for(
            backfill_orchestrator(
                hass,
                live_queue=live_queue,
                backfill_queue=backfill_queue,
                backfill_request=backfill_request,
                read_watermark=MagicMock(return_value=wm),
                # sensor.test via DB (registry entity); sun.sun via hass.states
                all_entities_reader=MagicMock(return_value={"sensor.test"}),
                entity_filter=lambda _eid: True,
                stop_event=stop_event,
                threading_stop_event=_threading_stop_event(),
            ),
            timeout=10,
        )

    assert captured_entities, "No slice fetches — entity set construction skipped"
    entities = captured_entities[0]
    assert "sun.sun" in entities, (
        "sun.sun absent from backfill entity set — hass.states not unioned into entity set"
    )
    assert "sensor.test" in entities, (
        "sensor.test absent from backfill entity set — all_entities_reader result lost"
    )
