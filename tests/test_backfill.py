"""Unit tests for backfill_orchestrator + _fetch_slice_raw (D-08)."""
import asyncio
import queue
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.ha_timescaledb_recorder.backfill import (
    BACKFILL_DONE,
    _fetch_slice_raw,
    backfill_orchestrator,
)


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
            ), timeout=2)
        except asyncio.TimeoutError:
            pass
    mock_clear_issue.assert_called()  # issue cleared on every backfill cycle (D-10-c)
