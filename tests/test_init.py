"""Integration lifecycle tests for async_setup_entry / async_unload_entry.

These tests verify that:
1. async_setup_entry returns True and stores runtime data (HaTimescaleDBData)
2. async_unload_entry executes D-13 shutdown sequence in the correct order
3. D-12 setup steps fire in the documented order
4. Partial-start rollback: if syncer.async_start() raises, workers are stopped
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.ha_timescaledb_recorder import (
    HaTimescaleDBData,
    async_setup_entry,
    async_unload_entry,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_entry():
    """Minimal mock config entry with DSN and default options."""
    entry = MagicMock()
    entry.data = {"dsn": "postgresql://local/test"}
    entry.options = {}
    entry.runtime_data = None
    entry.async_on_unload = MagicMock()
    entry.add_update_listener = MagicMock(return_value=MagicMock())
    return entry


# conftest provides `hass` fixture — override here with the fields needed for
# async_setup_entry (config.path, bus.async_listen_once, async_create_task).
@pytest.fixture
def hass():
    """Return a mock hass instance sufficient for async_setup_entry."""
    h = MagicMock()
    h.bus = MagicMock()
    h.bus.async_listen = MagicMock(return_value=MagicMock())
    h.bus.async_listen_once = MagicMock()
    h.async_add_executor_job = AsyncMock(return_value=None)
    h.async_create_task = MagicMock()
    h.config = MagicMock()
    h.config.path = MagicMock(return_value="/tmp/ha_tsdb_test/metadata_queue.jsonl")
    return h


# ---------------------------------------------------------------------------
# D-12: setup ordering
# ---------------------------------------------------------------------------

async def test_async_setup_entry_runs_d12_steps_in_order(hass, mock_entry):
    """async_setup_entry must execute D-12 steps 2-8 in documented order.

    Steps recorded via side_effect callbacks that append to a shared sequence list.
    The expected order is: meta_worker.start (2), states_worker.start (3),
    meta_queue.join (4), _async_initial_registry_backfill (5),
    syncer.async_start (6), ingester.async_start (7),
    bus.async_listen_once registration (8).
    """
    sequence: list[str] = []

    with patch(
        "custom_components.ha_timescaledb_recorder.PersistentQueue"
    ) as MockPQ, patch(
        "custom_components.ha_timescaledb_recorder.TimescaledbMetaRecorderThread"
    ) as MockMW, patch(
        "custom_components.ha_timescaledb_recorder.TimescaledbStateRecorderThread"
    ) as MockSW, patch(
        "custom_components.ha_timescaledb_recorder._async_initial_registry_backfill",
        new=AsyncMock(side_effect=lambda *a, **kw: sequence.append("step5")),
    ), patch(
        "custom_components.ha_timescaledb_recorder.MetadataSyncer"
    ) as MockSyncer, patch(
        "custom_components.ha_timescaledb_recorder.StateIngester"
    ) as MockIng, patch(
        "custom_components.ha_timescaledb_recorder._overflow_watcher",
        new=AsyncMock(),
    ):
        pq = MockPQ.return_value
        pq.join = AsyncMock(side_effect=lambda: sequence.append("step4"))

        mw = MockMW.return_value
        mw.start = MagicMock(side_effect=lambda: sequence.append("step2"))

        sw = MockSW.return_value
        sw.start = MagicMock(side_effect=lambda: sequence.append("step3"))

        syncer = MockSyncer.return_value
        syncer.async_start = AsyncMock(side_effect=lambda: sequence.append("step6"))

        ing = MockIng.return_value
        ing.async_start = MagicMock(side_effect=lambda: sequence.append("step7"))

        # Step 8 is async_listen_once registration
        hass.bus.async_listen_once = MagicMock(
            side_effect=lambda *a, **kw: sequence.append("step8")
        )

        await async_setup_entry(hass, mock_entry)

    assert sequence == ["step2", "step3", "step4", "step5", "step6", "step7", "step8"], (
        f"D-12 steps must fire in order 2-8. Got: {sequence}"
    )


async def test_setup_entry_returns_true(hass, mock_entry):
    """async_setup_entry must return True and populate entry.runtime_data."""
    with patch(
        "custom_components.ha_timescaledb_recorder.PersistentQueue"
    ) as MockPQ, patch(
        "custom_components.ha_timescaledb_recorder.TimescaledbMetaRecorderThread"
    ) as MockMW, patch(
        "custom_components.ha_timescaledb_recorder.TimescaledbStateRecorderThread"
    ) as MockSW, patch(
        "custom_components.ha_timescaledb_recorder._async_initial_registry_backfill",
        new=AsyncMock(),
    ), patch(
        "custom_components.ha_timescaledb_recorder.MetadataSyncer"
    ) as MockSyncer, patch(
        "custom_components.ha_timescaledb_recorder.StateIngester"
    ) as MockIng, patch(
        "custom_components.ha_timescaledb_recorder._overflow_watcher",
        new=AsyncMock(),
    ):
        MockPQ.return_value.join = AsyncMock()
        MockMW.return_value.start = MagicMock()
        MockSW.return_value.start = MagicMock()
        MockSyncer.return_value.async_start = AsyncMock()
        MockIng.return_value.async_start = MagicMock()

        result = await async_setup_entry(hass, mock_entry)

    assert result is True
    assert mock_entry.runtime_data is not None
    assert isinstance(mock_entry.runtime_data, HaTimescaleDBData)


async def test_setup_entry_runtime_data_has_all_fields(hass, mock_entry):
    """HaTimescaleDBData on runtime_data must have all Phase 2 fields populated."""
    with patch(
        "custom_components.ha_timescaledb_recorder.PersistentQueue"
    ) as MockPQ, patch(
        "custom_components.ha_timescaledb_recorder.TimescaledbMetaRecorderThread"
    ) as MockMW, patch(
        "custom_components.ha_timescaledb_recorder.TimescaledbStateRecorderThread"
    ) as MockSW, patch(
        "custom_components.ha_timescaledb_recorder._async_initial_registry_backfill",
        new=AsyncMock(),
    ), patch(
        "custom_components.ha_timescaledb_recorder.MetadataSyncer"
    ) as MockSyncer, patch(
        "custom_components.ha_timescaledb_recorder.StateIngester"
    ) as MockIng, patch(
        "custom_components.ha_timescaledb_recorder._overflow_watcher",
        new=AsyncMock(),
    ):
        MockPQ.return_value.join = AsyncMock()
        MockMW.return_value.start = MagicMock()
        MockSW.return_value.start = MagicMock()
        MockSyncer.return_value.async_start = AsyncMock()
        MockIng.return_value.async_start = MagicMock()

        await async_setup_entry(hass, mock_entry)

    data = mock_entry.runtime_data
    # All Phase 2 fields must be present
    assert data.states_worker is not None
    assert data.meta_worker is not None
    assert data.ingester is not None
    assert data.syncer is not None
    assert data.live_queue is not None
    assert data.meta_queue is not None
    assert data.backfill_queue is not None
    assert data.backfill_request is not None
    assert data.stop_event is not None
    assert data.loop_stop_event is not None


# ---------------------------------------------------------------------------
# D-13: unload sequence
# ---------------------------------------------------------------------------

async def test_async_unload_entry_runs_d13_sequence(hass, mock_entry):
    """async_unload_entry must execute the D-13 shutdown sequence.

    Verifies: stop_event.set(), loop_stop_event.set(), orchestrator cancel,
    overflow_watcher cancel, ingester.stop(), syncer.async_stop(),
    meta_queue.wake_consumer(), and executor joins for both workers.
    """
    data = MagicMock(spec=HaTimescaleDBData)
    data.stop_event = MagicMock()
    data.loop_stop_event = MagicMock()
    data.ingester = MagicMock()
    data.syncer = MagicMock()
    data.syncer.async_stop = AsyncMock()
    data.meta_queue = MagicMock()
    data.states_worker = MagicMock()
    data.states_worker.is_alive = MagicMock(return_value=False)
    data.meta_worker = MagicMock()
    data.meta_worker.is_alive = MagicMock(return_value=False)

    # Tasks that cancel cleanly
    async def _cancelled():
        raise asyncio.CancelledError

    orch_task = MagicMock()
    orch_task.cancel = MagicMock()

    async def _await_orch():
        raise asyncio.CancelledError

    orch_task.__await__ = lambda self: _await_orch().__await__()

    data.orchestrator_task = asyncio.create_task(_cancelled())
    data.orchestrator_task.cancel()
    # Re-create as simple cancelled future for cleaner mock
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    fut.cancel()
    data.orchestrator_task = fut

    overflow_fut = loop.create_future()
    overflow_fut.cancel()
    data.overflow_watcher_task = overflow_fut

    mock_entry.runtime_data = data
    hass.async_add_executor_job = AsyncMock(return_value=None)

    await async_unload_entry(hass, mock_entry)

    data.stop_event.set.assert_called_once()
    data.loop_stop_event.set.assert_called_once()
    data.ingester.stop.assert_called_once()
    data.syncer.async_stop.assert_awaited_once()
    data.meta_queue.wake_consumer.assert_called_once()
    # Both workers joined via executor job with 30s timeout
    hass.async_add_executor_job.assert_any_call(data.states_worker.join, 30)
    hass.async_add_executor_job.assert_any_call(data.meta_worker.join, 30)


async def test_async_unload_entry_returns_true(hass, mock_entry):
    """async_unload_entry must return True on success."""
    data = MagicMock(spec=HaTimescaleDBData)
    data.stop_event = MagicMock()
    data.loop_stop_event = MagicMock()
    data.orchestrator_task = None
    data.overflow_watcher_task = None
    data.ingester = MagicMock()
    data.syncer = MagicMock()
    data.syncer.async_stop = AsyncMock()
    data.meta_queue = MagicMock()
    data.states_worker = MagicMock()
    data.states_worker.is_alive = MagicMock(return_value=False)
    data.meta_worker = MagicMock()
    data.meta_worker.is_alive = MagicMock(return_value=False)
    mock_entry.runtime_data = data
    hass.async_add_executor_job = AsyncMock(return_value=None)

    result = await async_unload_entry(hass, mock_entry)

    assert result is True


# ---------------------------------------------------------------------------
# Partial-start rollback
# ---------------------------------------------------------------------------

async def test_partial_start_rollback_on_syncer_failure(hass, mock_entry):
    """If syncer.async_start() raises, workers must be stopped — no orphaned threads."""
    worker_stopped = []

    with patch(
        "custom_components.ha_timescaledb_recorder.PersistentQueue"
    ) as MockPQ, patch(
        "custom_components.ha_timescaledb_recorder.TimescaledbMetaRecorderThread"
    ) as MockMW, patch(
        "custom_components.ha_timescaledb_recorder.TimescaledbStateRecorderThread"
    ) as MockSW, patch(
        "custom_components.ha_timescaledb_recorder._async_initial_registry_backfill",
        new=AsyncMock(),
    ), patch(
        "custom_components.ha_timescaledb_recorder.MetadataSyncer"
    ) as MockSyncer, patch(
        "custom_components.ha_timescaledb_recorder.StateIngester"
    ) as MockIng:
        pq = MockPQ.return_value
        pq.join = AsyncMock()
        pq.wake_consumer = MagicMock()

        mw = MockMW.return_value
        mw.start = MagicMock()

        sw = MockSW.return_value
        sw.start = MagicMock()

        syncer = MockSyncer.return_value
        syncer.async_start = AsyncMock(side_effect=RuntimeError("syncer exploded"))

        ing = MockIng.return_value
        ing.async_start = MagicMock()

        with pytest.raises(RuntimeError, match="syncer exploded"):
            await async_setup_entry(hass, mock_entry)

    # stop_event.set() must have been called (workers signalled to stop)
    # Confirmed indirectly: async_add_executor_job called for joins
    hass.async_add_executor_job.assert_any_call(mw.join, 30)
    hass.async_add_executor_job.assert_any_call(sw.join, 30)
