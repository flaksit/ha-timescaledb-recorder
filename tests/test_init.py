"""Integration lifecycle tests for async_setup_entry / async_unload_entry.

These tests verify that:
1. async_setup_entry returns True and stores runtime data
2. async_unload_entry stops all components in correct order
3. Partial-start rollback: if syncer.async_start() raises, worker is stopped cleanly
4. bind_queue() is called rather than direct _queue attribute mutation
"""
import queue
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.ha_timescaledb_recorder import (
    HaTimescaleDBData,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.ha_timescaledb_recorder.ingester import StateIngester
from custom_components.ha_timescaledb_recorder.syncer import MetadataSyncer
from custom_components.ha_timescaledb_recorder.worker import DbWorker


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_config_entry():
    """Minimal mock config entry with DSN and default options."""
    entry = MagicMock()
    entry.data = {"dsn": "postgresql://test/db"}
    entry.options = {}
    entry.runtime_data = None
    entry.async_on_unload = MagicMock()
    entry.add_update_listener = MagicMock(return_value=MagicMock())
    return entry


@pytest.fixture
def hass():
    """Return a mock hass instance with async_add_executor_job wired as AsyncMock."""
    h = MagicMock()
    h.bus = MagicMock()
    h.bus.async_listen = MagicMock(return_value=MagicMock())
    h.async_add_executor_job = AsyncMock()
    return h


# ---------------------------------------------------------------------------
# Happy-path: successful setup and teardown
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_setup_entry_returns_true(hass, mock_config_entry):
    """async_setup_entry must return True and populate entry.runtime_data."""
    with patch.object(DbWorker, "start"), \
         patch.object(StateIngester, "async_start"), \
         patch.object(MetadataSyncer, "async_start", new_callable=AsyncMock), \
         patch.object(MetadataSyncer, "bind_queue"):
        result = await async_setup_entry(hass, mock_config_entry)

    assert result is True
    assert mock_config_entry.runtime_data is not None


@pytest.mark.asyncio
async def test_unload_entry_stops_in_order(hass, mock_config_entry):
    """async_unload_entry must stop components in the correct order: ingester → syncer → worker.

    Correct order prevents new items being enqueued (ingester stopped first) while the
    worker still drains the queue (stopped last).
    """
    call_order = []

    mock_worker = MagicMock()
    mock_worker.async_stop = AsyncMock(
        side_effect=lambda: call_order.append("worker.async_stop")
    )

    mock_ingester = MagicMock()
    mock_ingester.stop = MagicMock(
        side_effect=lambda: call_order.append("ingester.stop")
    )

    mock_syncer = MagicMock()
    mock_syncer.async_stop = AsyncMock(
        side_effect=lambda: call_order.append("syncer.async_stop")
    )

    mock_config_entry.runtime_data = HaTimescaleDBData(
        worker=mock_worker,
        ingester=mock_ingester,
        syncer=mock_syncer,
    )

    result = await async_unload_entry(hass, mock_config_entry)

    assert result is True
    assert call_order == ["ingester.stop", "syncer.async_stop", "worker.async_stop"], (
        f"Components must stop in order: ingester → syncer → worker. Got: {call_order}"
    )


# ---------------------------------------------------------------------------
# Partial-start rollback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_partial_start_rollback_on_syncer_failure(hass, mock_config_entry):
    """If syncer.async_start() raises, worker and ingester must be stopped (no orphaned thread)."""
    worker_stopped = []
    ingester_stopped = []

    with patch.object(DbWorker, "start"), \
         patch.object(DbWorker, "async_stop", new_callable=AsyncMock,
                      side_effect=lambda: worker_stopped.append(True)), \
         patch.object(StateIngester, "async_start"), \
         patch.object(StateIngester, "stop",
                      side_effect=lambda: ingester_stopped.append(True)), \
         patch.object(MetadataSyncer, "bind_queue"), \
         patch.object(MetadataSyncer, "async_start",
                      new_callable=AsyncMock,
                      side_effect=RuntimeError("syncer exploded")):
        with pytest.raises(RuntimeError, match="syncer exploded"):
            await async_setup_entry(hass, mock_config_entry)

    assert worker_stopped, "worker.async_stop() must be called on rollback — no orphaned thread"
    assert ingester_stopped, "ingester.stop() must be called before worker on rollback"


# ---------------------------------------------------------------------------
# API contract: bind_queue() must be used, not direct _queue mutation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_setup_uses_bind_queue_not_attribute_mutation(hass, mock_config_entry):
    """async_setup_entry must call syncer.bind_queue(worker.queue), not set syncer._queue directly.

    bind_queue() provides a stable API and makes the two-phase construction intent explicit.
    Direct _queue mutation would bypass any future validation logic in bind_queue().
    """
    bind_queue_calls = []

    def tracking_bind(self, q):
        """Intercept bind_queue() calls to verify the argument type."""
        bind_queue_calls.append(q)

    with patch.object(DbWorker, "start"), \
         patch.object(StateIngester, "async_start"), \
         patch.object(MetadataSyncer, "async_start", new_callable=AsyncMock), \
         patch.object(MetadataSyncer, "bind_queue", tracking_bind):
        await async_setup_entry(hass, mock_config_entry)

    assert len(bind_queue_calls) == 1, "bind_queue() must be called exactly once during setup"
    assert isinstance(bind_queue_calls[0], queue.Queue), (
        f"bind_queue() must receive a queue.Queue instance, got {type(bind_queue_calls[0])}"
    )
