"""Unit tests for backfill_orchestrator + _fetch_slice_raw (D-08)."""
import asyncio
import queue
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.ha_timescaledb_recorder.backfill import (
    BACKFILL_DONE,
    _LATE_ARRIVAL_GRACE,
    _fetch_slice_raw,
    backfill_orchestrator,
)


def _threading_stop_event():
    """Return a non-set threading.Event for use as threading_stop_event."""
    return threading.Event()


def test_fetch_slice_raw_iterates_per_entity_never_passes_none():
    hass = MagicMock()
    entities = {"sensor.a", "sensor.b"}
    t_start = datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc)
    t_end = t_start + timedelta(minutes=5)
    with patch(
        "custom_components.ha_timescaledb_recorder.backfill.recorder_history.state_changes_during_period"
    ) as mock_sc:
        mock_sc.side_effect = lambda hass, s, e, *, entity_id, include_start_time_state: (
            {entity_id: [MagicMock(entity_id=entity_id)]}
        )
        out = _fetch_slice_raw(hass, entities, t_start, t_end)
    # Called once per entity; entity_id=None NEVER passed
    for call in mock_sc.call_args_list:
        assert call.kwargs["entity_id"] is not None
        assert call.kwargs["include_start_time_state"] is False
    assert set(out.keys()) == entities
    assert len(mock_sc.call_args_list) == 2


def test_fetch_slice_raw_empty_entities_returns_empty():
    hass = MagicMock()
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

    with patch("custom_components.ha_timescaledb_recorder.backfill.recorder.get_instance"):
        await asyncio.wait_for(backfill_orchestrator(
            hass,
            live_queue=MagicMock(),
            backfill_queue=MagicMock(),
            backfill_request=backfill_request,
            read_watermark=MagicMock(return_value=None),
            open_entities_reader=MagicMock(return_value=set()),
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

    with patch("custom_components.ha_timescaledb_recorder.backfill.recorder.get_instance"), \
         patch("custom_components.ha_timescaledb_recorder.backfill.clear_buffer_dropping_issue"):
        orch = asyncio.create_task(backfill_orchestrator(
            hass,
            live_queue=live_queue,
            backfill_queue=backfill_queue,
            backfill_request=backfill_request,
            read_watermark=MagicMock(return_value=None),
            open_entities_reader=MagicMock(return_value=set()),
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

    with patch("custom_components.ha_timescaledb_recorder.backfill.recorder.get_instance"), \
         patch(
             "custom_components.ha_timescaledb_recorder.backfill.clear_buffer_dropping_issue"
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
                open_entities_reader=MagicMock(return_value=set()),
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
        "custom_components.ha_timescaledb_recorder.backfill.recorder.get_instance",
        return_value=recorder_inst,
    ), patch(
        "custom_components.ha_timescaledb_recorder.backfill.notify_backfill_gap"
    ) as mock_notify, patch(
        "custom_components.ha_timescaledb_recorder.backfill.clear_buffer_dropping_issue"
    ), patch(
        "custom_components.ha_timescaledb_recorder.backfill.er.async_get"
    ) as mock_er:
        mock_er.return_value.entities.values.return_value = []

        async def stopper():
            # Let one full cycle complete then stop.
            await asyncio.sleep(0.05)
            stop_event.set()

        asyncio.create_task(stopper())
        with caplog.at_level(logging.DEBUG,
                             logger="custom_components.ha_timescaledb_recorder.backfill"):
            try:
                await asyncio.wait_for(
                    backfill_orchestrator(
                        hass,
                        live_queue=live_queue,
                        backfill_queue=backfill_queue,
                        backfill_request=backfill_request,
                        read_watermark=MagicMock(return_value=wm),
                        open_entities_reader=MagicMock(return_value=set()),
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
    stop_event = asyncio.Event()
    backfill_request = asyncio.Event()
    backfill_request.set()

    recorder_inst = _make_recorder_instance(oldest_ts=oldest_ts_float)

    with patch(
        "custom_components.ha_timescaledb_recorder.backfill.recorder.get_instance",
        return_value=recorder_inst,
    ), patch(
        "custom_components.ha_timescaledb_recorder.backfill.notify_backfill_gap"
    ) as mock_notify, patch(
        "custom_components.ha_timescaledb_recorder.backfill.clear_buffer_dropping_issue"
    ), patch(
        "custom_components.ha_timescaledb_recorder.backfill.er.async_get"
    ) as mock_er:
        mock_er.return_value.entities.values.return_value = []

        stop_event.set()  # single cycle only
        await backfill_orchestrator(
            hass,
            live_queue=live_queue,
            backfill_queue=backfill_queue,
            backfill_request=backfill_request,
            read_watermark=MagicMock(return_value=wm),
            open_entities_reader=MagicMock(return_value=set()),
            entity_filter=lambda _eid: True,
            stop_event=stop_event,
            threading_stop_event=_threading_stop_event(),
        )

    mock_notify.assert_called_once()
    call_kwargs = mock_notify.call_args
    assert call_kwargs[0][1] == "recorder_retention" or call_kwargs[1].get("reason") == "recorder_retention"


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
        "custom_components.ha_timescaledb_recorder.backfill.recorder.get_instance",
        return_value=recorder_inst,
    ), patch(
        "custom_components.ha_timescaledb_recorder.backfill.notify_backfill_gap"
    ) as mock_notify, patch(
        "custom_components.ha_timescaledb_recorder.backfill.clear_buffer_dropping_issue"
    ), patch(
        "custom_components.ha_timescaledb_recorder.backfill.er.async_get"
    ) as mock_er:
        mock_er.return_value.entities.values.return_value = []

        stop_event.set()  # single cycle only
        await backfill_orchestrator(
            hass,
            live_queue=live_queue,
            backfill_queue=backfill_queue,
            backfill_request=backfill_request,
            read_watermark=MagicMock(return_value=wm),
            open_entities_reader=MagicMock(return_value=set()),
            entity_filter=lambda _eid: True,
            stop_event=stop_event,
            threading_stop_event=_threading_stop_event(),
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
        "custom_components.ha_timescaledb_recorder.backfill.recorder.get_instance",
        return_value=recorder_inst,
    ), patch(
        "custom_components.ha_timescaledb_recorder.backfill.notify_backfill_gap"
    ), patch(
        "custom_components.ha_timescaledb_recorder.backfill.clear_buffer_dropping_issue"
    ), patch(
        "custom_components.ha_timescaledb_recorder.backfill.er.async_get"
    ) as mock_er:
        # Provide one entity so the slice loop executes at least one iteration.
        entry = MagicMock()
        entry.entity_id = "sensor.test"
        mock_er.return_value.entities.values.return_value = [entry]

        stop_event.set()  # single cycle only — stop_event checked in slice loop
        with patch(
            "custom_components.ha_timescaledb_recorder.backfill._SLICE_WINDOW",
            timedelta(hours=2),
        ):
            await backfill_orchestrator(
                hass,
                live_queue=live_queue,
                backfill_queue=backfill_queue,
                backfill_request=backfill_request,
                read_watermark=MagicMock(return_value=wm),
                open_entities_reader=MagicMock(return_value=set()),
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
        "custom_components.ha_timescaledb_recorder.backfill.retry_until_success",
        fake_retry,
    ), patch(
        "custom_components.ha_timescaledb_recorder.backfill.recorder.get_instance"
    ), patch(
        "custom_components.ha_timescaledb_recorder.backfill.clear_buffer_dropping_issue"
    ):
        await backfill_orchestrator(
            hass,
            live_queue=live_queue,
            backfill_queue=queue.Queue(maxsize=4),
            backfill_request=backfill_request,
            read_watermark=MagicMock(return_value=wm),
            open_entities_reader=MagicMock(return_value=set()),
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
    the orchestrator must not raise to caller on first failure."""
    import psycopg

    hass = MagicMock()
    wm = datetime(2026, 4, 23, 10, 0, 0, tzinfo=timezone.utc)
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

    with patch(
        "custom_components.ha_timescaledb_recorder.backfill.recorder.get_instance"
    ) as mock_get_inst, patch(
        "custom_components.ha_timescaledb_recorder.backfill._fetch_slice_raw",
        flaky_fetch_raw,
    ), patch(
        "custom_components.ha_timescaledb_recorder.backfill.clear_buffer_dropping_issue"
    ), patch(
        "custom_components.ha_timescaledb_recorder.backfill.er.async_get"
    ) as mock_er, patch(
        "custom_components.ha_timescaledb_recorder.backfill.notify_backfill_gap"
    ):
        entry = MagicMock()
        entry.entity_id = "sensor.test"
        mock_er.return_value.entities.values.return_value = [entry]

        recorder_inst = MagicMock()
        recorder_inst.states_manager.oldest_ts = None
        recorder_inst.async_add_executor_job = AsyncMock(
            side_effect=lambda fn, *args: fn(*args)
        )
        mock_get_inst.return_value = recorder_inst

        async def stopper():
            # Stop after _fetch_slice_raw has been called at least twice.
            for _ in range(200):
                if call_count["n"] >= 2:
                    stop_event.set()
                    return
                await asyncio.sleep(0.005)
            stop_event.set()

        asyncio.create_task(stopper())

        with patch(
            "custom_components.ha_timescaledb_recorder.backfill._SLICE_WINDOW",
            timedelta(hours=2),
        ), patch(
            "custom_components.ha_timescaledb_recorder.backfill._RECORDER_COMMIT_LAG",
            timedelta(seconds=0),
        ):
            await asyncio.wait_for(
                backfill_orchestrator(
                    hass,
                    live_queue=live_queue,
                    backfill_queue=backfill_queue,
                    backfill_request=backfill_request,
                    read_watermark=MagicMock(return_value=wm),
                    open_entities_reader=MagicMock(return_value=set()),
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
        "custom_components.ha_timescaledb_recorder.backfill.recorder.get_instance"
    ), patch(
        "custom_components.ha_timescaledb_recorder.backfill.clear_buffer_dropping_issue"
    ):
        with pytest.raises(RuntimeError, match="unhandled-boom"):
            await backfill_orchestrator(
                hass,
                live_queue=live_queue,
                backfill_queue=queue.Queue(maxsize=4),
                backfill_request=backfill_request,
                read_watermark=MagicMock(return_value=None),
                open_entities_reader=MagicMock(return_value=set()),
                entity_filter=lambda _eid: True,
                stop_event=stop_event,
                threading_stop_event=_threading_stop_event(),
            )
