"""Integration lifecycle tests for async_setup_entry / async_unload_entry.

These tests verify that:
1. async_setup_entry returns True and stores runtime data (HaTimescaleDBData)
2. async_unload_entry executes D-13 shutdown sequence in the correct order
3. D-12 setup steps fire in the documented order
4. Partial-start rollback: if syncer.async_start() raises, workers are stopped
5. Phase 3: HaTimescaleDBData extended with watchdog + spawn-factory fields
6. Phase 3: watchdog task spawned, orchestrator done_callback wired
7. Phase 3: recorder_disabled check + auto-clear background task
8. Phase 3: unload cancels and awaits watchdog before orchestrator (MEDIUM-12)
"""
import asyncio
import dataclasses
from unittest.mock import AsyncMock, MagicMock, call, patch

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
    h.async_create_background_task = MagicMock()
    h.config = MagicMock()
    h.config.path = MagicMock(return_value="/tmp/ha_tsdb_test/metadata_queue.jsonl")
    return h


# ---------------------------------------------------------------------------
# D-12: setup ordering
# ---------------------------------------------------------------------------

async def test_async_setup_entry_runs_d12_steps_in_order(hass, mock_entry):
    """async_setup_entry must execute D-12 steps in documented order.

    Steps recorded via side_effect callbacks that append to a shared sequence list.
    Expected order in async_setup_entry: meta_worker.start (step2), states_worker.start
    (step3), registry_listener.async_start (step6), state_listener.async_start (step7),
    bus.async_listen_once registration (step8).

    Steps 4+5 (persistent queue drain + registry backfill) run inside _async_meta_init
    background task — not awaited in async_setup_entry, so not in this sequence.
    """
    sequence: list[str] = []

    with patch(
        "custom_components.ha_timescaledb_recorder.PersistentQueue"
    ) as MockPQ, patch(
        "custom_components.ha_timescaledb_recorder._async_meta_init",
        new=AsyncMock(),
    ), patch(
        "custom_components.ha_timescaledb_recorder.RegistryListener"
    ) as MockRL, patch(
        "custom_components.ha_timescaledb_recorder.StateListener"
    ) as MockSL, patch(
        "custom_components.ha_timescaledb_recorder._overflow_watcher",
        new=AsyncMock(),
    ), patch(
        "custom_components.ha_timescaledb_recorder.spawn_meta_worker"
    ) as mock_spawn_mw, patch(
        "custom_components.ha_timescaledb_recorder.spawn_states_worker"
    ) as mock_spawn_sw, patch(
        "custom_components.ha_timescaledb_recorder.watchdog_loop", return_value=None,
    ), patch(
        "custom_components.ha_timescaledb_recorder.ha_recorder",
    ) as mock_recorder:
        MockPQ.return_value

        mw = MagicMock()
        mw.start = MagicMock(side_effect=lambda: sequence.append("step2"))
        mw.read_watermark = MagicMock()
        mw.read_open_entities = MagicMock()
        mock_spawn_mw.return_value = mw

        sw = MagicMock()
        sw.start = MagicMock(side_effect=lambda: sequence.append("step3"))
        sw.read_watermark = MagicMock()
        sw.read_open_entities = MagicMock()
        mock_spawn_sw.return_value = sw

        registry_listener = MockRL.return_value
        registry_listener.async_start = AsyncMock(side_effect=lambda: sequence.append("step6"))

        state_listener = MockSL.return_value
        state_listener.async_start = MagicMock(side_effect=lambda: sequence.append("step7"))

        # Step 8 is async_listen_once registration
        hass.bus.async_listen_once = MagicMock(
            side_effect=lambda *a, **kw: sequence.append("step8")
        )

        recorder_instance = MagicMock()
        recorder_instance.enabled = True
        mock_recorder.get_instance.return_value = recorder_instance

        await async_setup_entry(hass, mock_entry)

    assert sequence == ["step2", "step3", "step6", "step7", "step8"], (
        f"D-12 steps must fire in order. Got: {sequence}"
    )


async def test_setup_entry_returns_true(hass, mock_entry):
    """async_setup_entry must return True and populate entry.runtime_data."""
    with patch(
        "custom_components.ha_timescaledb_recorder.PersistentQueue"
    ) as MockPQ, patch(
        "custom_components.ha_timescaledb_recorder._async_initial_registry_backfill",
        new=AsyncMock(),
    ), patch(
        "custom_components.ha_timescaledb_recorder.RegistryListener"
    ) as MockRL, patch(
        "custom_components.ha_timescaledb_recorder.StateListener"
    ) as MockSL, patch(
        "custom_components.ha_timescaledb_recorder._overflow_watcher",
        new=AsyncMock(),
    ), patch(
        "custom_components.ha_timescaledb_recorder.spawn_meta_worker"
    ) as mock_spawn_mw, patch(
        "custom_components.ha_timescaledb_recorder.spawn_states_worker"
    ) as mock_spawn_sw, patch(
        "custom_components.ha_timescaledb_recorder.watchdog_loop", return_value=None,
    ), patch(
        "custom_components.ha_timescaledb_recorder.ha_recorder",
    ) as mock_recorder:
        MockPQ.return_value.join = AsyncMock()
        mock_mw = MagicMock()
        mock_mw.start = MagicMock()
        mock_mw.read_watermark = MagicMock()
        mock_mw.read_open_entities = MagicMock()
        mock_spawn_mw.return_value = mock_mw
        mock_sw = MagicMock()
        mock_sw.start = MagicMock()
        mock_sw.read_watermark = MagicMock()
        mock_sw.read_open_entities = MagicMock()
        mock_spawn_sw.return_value = mock_sw
        MockRL.return_value.async_start = AsyncMock()  # registry_listener
        MockSL.return_value.async_start = MagicMock()
        recorder_instance = MagicMock()
        recorder_instance.enabled = True
        mock_recorder.get_instance.return_value = recorder_instance

        result = await async_setup_entry(hass, mock_entry)

    assert result is True
    assert mock_entry.runtime_data is not None
    assert isinstance(mock_entry.runtime_data, HaTimescaleDBData)


async def test_setup_entry_runtime_data_has_all_fields(hass, mock_entry):
    """HaTimescaleDBData on runtime_data must have all Phase 2 fields populated."""
    with patch(
        "custom_components.ha_timescaledb_recorder.PersistentQueue"
    ) as MockPQ, patch(
        "custom_components.ha_timescaledb_recorder._async_initial_registry_backfill",
        new=AsyncMock(),
    ), patch(
        "custom_components.ha_timescaledb_recorder.RegistryListener"
    ) as MockRL, patch(
        "custom_components.ha_timescaledb_recorder.StateListener"
    ) as MockSL, patch(
        "custom_components.ha_timescaledb_recorder._overflow_watcher",
        new=AsyncMock(),
    ), patch(
        "custom_components.ha_timescaledb_recorder.spawn_meta_worker"
    ) as mock_spawn_mw, patch(
        "custom_components.ha_timescaledb_recorder.spawn_states_worker"
    ) as mock_spawn_sw, patch(
        "custom_components.ha_timescaledb_recorder.watchdog_loop", return_value=None,
    ), patch(
        "custom_components.ha_timescaledb_recorder.ha_recorder",
    ) as mock_recorder:
        MockPQ.return_value.join = AsyncMock()
        mock_mw = MagicMock()
        mock_mw.start = MagicMock()
        mock_mw.read_watermark = MagicMock()
        mock_mw.read_open_entities = MagicMock()
        mock_spawn_mw.return_value = mock_mw
        mock_sw = MagicMock()
        mock_sw.start = MagicMock()
        mock_sw.read_watermark = MagicMock()
        mock_sw.read_open_entities = MagicMock()
        mock_spawn_sw.return_value = mock_sw
        MockRL.return_value.async_start = AsyncMock()  # registry_listener
        MockSL.return_value.async_start = MagicMock()
        recorder_instance = MagicMock()
        recorder_instance.enabled = True
        mock_recorder.get_instance.return_value = recorder_instance

        await async_setup_entry(hass, mock_entry)

    data = mock_entry.runtime_data
    # All Phase 2 fields must be present
    assert data.states_worker is not None
    assert data.meta_worker is not None
    assert data.state_listener is not None
    assert data.registry_listener is not None
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

    Verifies: stop_event.set(), loop_stop_event.set(), watchdog cancel (Phase 3),
    orchestrator cancel, overflow_watcher cancel, ingester.stop(),
    syncer.async_stop(), meta_queue.wake_consumer(), and executor joins for
    both workers.
    """
    data = MagicMock(spec=HaTimescaleDBData)
    data.stop_event = MagicMock()
    data.loop_stop_event = MagicMock()
    data.state_listener = MagicMock()
    data.registry_listener = MagicMock()
    data.registry_listener.async_stop = AsyncMock()
    data.meta_queue = MagicMock()
    data.states_worker = MagicMock()
    data.states_worker.is_alive = MagicMock(return_value=False)
    data.meta_worker = MagicMock()
    data.meta_worker.is_alive = MagicMock(return_value=False)

    loop = asyncio.get_event_loop()

    # Phase 3: watchdog_task must be a proper awaitable cancelled future
    watchdog_fut = loop.create_future()
    watchdog_fut.cancel()
    data.watchdog_task = watchdog_fut

    # Orchestrator and overflow watcher as cancelled futures
    orch_fut = loop.create_future()
    orch_fut.cancel()
    data.orchestrator_task = orch_fut

    overflow_fut = loop.create_future()
    overflow_fut.cancel()
    data.overflow_watcher_task = overflow_fut

    mock_entry.runtime_data = data
    hass.async_add_executor_job = AsyncMock(return_value=None)

    await async_unload_entry(hass, mock_entry)

    data.stop_event.set.assert_called_once()
    data.loop_stop_event.set.assert_called_once()
    data.state_listener.stop.assert_called_once()
    data.registry_listener.async_stop.assert_awaited_once()
    data.meta_queue.wake_consumer.assert_called_once()
    # Both workers joined via executor job with 30s timeout
    hass.async_add_executor_job.assert_any_call(data.states_worker.join, 30)
    hass.async_add_executor_job.assert_any_call(data.meta_worker.join, 30)


async def test_async_unload_entry_returns_true(hass, mock_entry):
    """async_unload_entry must return True on success."""
    data = MagicMock(spec=HaTimescaleDBData)
    data.stop_event = MagicMock()
    data.loop_stop_event = MagicMock()
    data.watchdog_task = None      # Phase 3 field — None means not spawned
    data.orchestrator_task = None
    data.overflow_watcher_task = None
    data.state_listener = MagicMock()
    data.registry_listener = MagicMock()
    data.registry_listener.async_stop = AsyncMock()
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

async def test_partial_start_rollback_on_registry_listener_failure(hass, mock_entry):
    """If registry_listener.async_start() raises, workers must be stopped — no orphaned threads.

    Phase 3 note: workers are now constructed via spawn factories so this test
    patches spawn_states_worker / spawn_meta_worker to return controllable mocks.
    The rollback path calls join on the factory-returned worker mocks.
    """
    with patch(
        "custom_components.ha_timescaledb_recorder.PersistentQueue"
    ) as MockPQ, patch(
        "custom_components.ha_timescaledb_recorder.RegistryListener"
    ) as MockRL, patch(
        "custom_components.ha_timescaledb_recorder.StateListener"
    ) as MockSL, patch(
        "custom_components.ha_timescaledb_recorder.spawn_meta_worker"
    ) as mock_spawn_mw, patch(
        "custom_components.ha_timescaledb_recorder.spawn_states_worker"
    ) as mock_spawn_sw:
        pq = MockPQ.return_value
        pq.wake_consumer = MagicMock()

        mw = MagicMock()
        mw.start = MagicMock()
        mock_spawn_mw.return_value = mw

        sw = MagicMock()
        sw.start = MagicMock()
        mock_spawn_sw.return_value = sw

        registry_listener = MockRL.return_value
        registry_listener.async_start = AsyncMock(side_effect=RuntimeError("registry_listener exploded"))

        state_listener = MockSL.return_value
        state_listener.async_start = MagicMock()

        with pytest.raises(RuntimeError, match="registry_listener exploded"):
            await async_setup_entry(hass, mock_entry)

    # stop_event.set() must have been called (workers signalled to stop)
    # Confirmed indirectly: async_add_executor_job called for joins
    hass.async_add_executor_job.assert_any_call(mw.join, 30)
    hass.async_add_executor_job.assert_any_call(sw.join, 30)


# ---------------------------------------------------------------------------
# Phase 3: HaTimescaleDBData extended fields
# ---------------------------------------------------------------------------

def test_data_dataclass_has_phase3_fields():
    """HaTimescaleDBData must carry the five Phase 3 fields added by Plan 07."""
    field_names = {f.name for f in dataclasses.fields(HaTimescaleDBData)}
    expected = {"watchdog_task", "dsn", "chunk_interval_days", "compress_after_hours", "entity_filter"}
    missing = expected - field_names
    assert not missing, f"HaTimescaleDBData missing Phase 3 fields: {missing}"


def test_data_dataclass_preserves_phase2_fields():
    """All Phase 2 HaTimescaleDBData fields must still be present after Phase 3 extension."""
    field_names = {f.name for f in dataclasses.fields(HaTimescaleDBData)}
    phase2_fields = {
        "states_worker", "meta_worker", "state_listener", "registry_listener",
        "live_queue", "meta_queue", "backfill_queue", "backfill_request",
        "stop_event", "loop_stop_event", "orchestrator_task", "overflow_watcher_task",
    }
    missing = phase2_fields - field_names
    assert not missing, f"Phase 2 fields missing after Phase 3 extension: {missing}"


# ---------------------------------------------------------------------------
# Phase 3: spawn-factory usage in async_setup_entry
# ---------------------------------------------------------------------------

async def test_setup_spawns_workers_via_factories(hass, mock_entry):
    """async_setup_entry must call spawn_states_worker and spawn_meta_worker (not direct constructors)."""
    with patch(
        "custom_components.ha_timescaledb_recorder.PersistentQueue"
    ) as MockPQ, patch(
        "custom_components.ha_timescaledb_recorder.TimescaledbMetaRecorderThread"
    ), patch(
        "custom_components.ha_timescaledb_recorder.TimescaledbStateRecorderThread"
    ), patch(
        "custom_components.ha_timescaledb_recorder._async_initial_registry_backfill",
        new=AsyncMock(),
    ), patch(
        "custom_components.ha_timescaledb_recorder.RegistryListener"
    ) as MockRL, patch(
        "custom_components.ha_timescaledb_recorder.StateListener"
    ) as MockSL, patch(
        "custom_components.ha_timescaledb_recorder._overflow_watcher", new=AsyncMock(),
    ), patch(
        "custom_components.ha_timescaledb_recorder.spawn_states_worker"
    ) as mock_spawn_sw, patch(
        "custom_components.ha_timescaledb_recorder.spawn_meta_worker"
    ) as mock_spawn_mw, patch(
        "custom_components.ha_timescaledb_recorder.watchdog_loop", new=AsyncMock(),
    ), patch(
        "custom_components.ha_timescaledb_recorder.ha_recorder",
    ) as mock_recorder, patch(
        "custom_components.ha_timescaledb_recorder._wait_for_recorder_and_clear",
        new=AsyncMock(),
    ):
        MockPQ.return_value.join = AsyncMock()
        MockRL.return_value.async_start = AsyncMock()  # registry_listener
        MockSL.return_value.async_start = MagicMock()

        # Factories return mock workers with .start() methods
        mock_spawn_sw.return_value = MagicMock()
        mock_spawn_sw.return_value.start = MagicMock()
        mock_spawn_sw.return_value.read_watermark = MagicMock()
        mock_spawn_sw.return_value.read_open_entities = MagicMock()
        mock_spawn_mw.return_value = MagicMock()
        mock_spawn_mw.return_value.start = MagicMock()

        # Recorder available and enabled — no issue raised
        recorder_instance = MagicMock()
        recorder_instance.enabled = True
        mock_recorder.get_instance.return_value = recorder_instance

        await async_setup_entry(hass, mock_entry)

    mock_spawn_sw.assert_called_once()
    mock_spawn_mw.assert_called_once()


# ---------------------------------------------------------------------------
# Phase 3: watchdog task creation
# ---------------------------------------------------------------------------

async def test_setup_creates_watchdog_task(hass, mock_entry):
    """async_setup_entry must assign data.watchdog_task via hass.async_create_task(watchdog_loop(...))."""
    # Use a sentinel object as the coroutine returned by watchdog_loop.
    # watchdog_loop must be patched as a regular MagicMock (not AsyncMock) so
    # that calling it returns the sentinel directly — AsyncMock wraps the return
    # value in a coroutine, which would prevent identity comparison.
    watchdog_coro_sentinel = object()

    with patch(
        "custom_components.ha_timescaledb_recorder.PersistentQueue"
    ) as MockPQ, patch(
        "custom_components.ha_timescaledb_recorder.TimescaledbMetaRecorderThread"
    ), patch(
        "custom_components.ha_timescaledb_recorder.TimescaledbStateRecorderThread"
    ), patch(
        "custom_components.ha_timescaledb_recorder._async_initial_registry_backfill",
        new=AsyncMock(),
    ), patch(
        "custom_components.ha_timescaledb_recorder.RegistryListener"
    ) as MockRL, patch(
        "custom_components.ha_timescaledb_recorder.StateListener"
    ) as MockSL, patch(
        "custom_components.ha_timescaledb_recorder._overflow_watcher", new=AsyncMock(),
    ), patch(
        "custom_components.ha_timescaledb_recorder.spawn_states_worker"
    ) as mock_spawn_sw, patch(
        "custom_components.ha_timescaledb_recorder.spawn_meta_worker"
    ) as mock_spawn_mw, patch(
        "custom_components.ha_timescaledb_recorder.watchdog_loop",
        new=MagicMock(return_value=watchdog_coro_sentinel),
    ) as mock_wl, patch(
        "custom_components.ha_timescaledb_recorder.ha_recorder",
    ) as mock_recorder, patch(
        "custom_components.ha_timescaledb_recorder._wait_for_recorder_and_clear",
        new=AsyncMock(),
    ):
        MockPQ.return_value.join = AsyncMock()
        MockRL.return_value.async_start = AsyncMock()  # registry_listener
        MockSL.return_value.async_start = MagicMock()

        mock_spawn_sw.return_value = MagicMock()
        mock_spawn_sw.return_value.start = MagicMock()
        mock_spawn_sw.return_value.read_watermark = MagicMock()
        mock_spawn_sw.return_value.read_open_entities = MagicMock()
        mock_spawn_mw.return_value = MagicMock()
        mock_spawn_mw.return_value.start = MagicMock()

        recorder_instance = MagicMock()
        recorder_instance.enabled = True
        mock_recorder.get_instance.return_value = recorder_instance

        # Track what async_create_background_task was called with
        created_bg_tasks = []

        def _capture_bg_task(coro, *, name=None):
            created_bg_tasks.append(coro)
            return MagicMock()

        hass.async_create_background_task = MagicMock(side_effect=_capture_bg_task)

        await async_setup_entry(hass, mock_entry)

    # watchdog_loop must have been called
    mock_wl.assert_called_once()
    # watchdog_loop's return value (the sentinel) must have been passed to async_create_background_task
    assert watchdog_coro_sentinel in created_bg_tasks, (
        "watchdog_loop coroutine not passed to hass.async_create_background_task"
    )


# ---------------------------------------------------------------------------
# Phase 3: threading_stop_event passed to backfill_orchestrator
# ---------------------------------------------------------------------------

async def test_setup_passes_threading_stop_event_to_orchestrator(hass, mock_entry):
    """backfill_orchestrator must be called with threading_stop_event=data.stop_event."""
    captured_kwargs = {}

    with patch(
        "custom_components.ha_timescaledb_recorder.PersistentQueue"
    ) as MockPQ, patch(
        "custom_components.ha_timescaledb_recorder.TimescaledbMetaRecorderThread"
    ), patch(
        "custom_components.ha_timescaledb_recorder.TimescaledbStateRecorderThread"
    ), patch(
        "custom_components.ha_timescaledb_recorder._async_initial_registry_backfill",
        new=AsyncMock(),
    ), patch(
        "custom_components.ha_timescaledb_recorder.RegistryListener"
    ) as MockRL, patch(
        "custom_components.ha_timescaledb_recorder.StateListener"
    ) as MockSL, patch(
        "custom_components.ha_timescaledb_recorder._overflow_watcher", new=AsyncMock(),
    ), patch(
        "custom_components.ha_timescaledb_recorder.spawn_states_worker"
    ) as mock_spawn_sw, patch(
        "custom_components.ha_timescaledb_recorder.spawn_meta_worker"
    ) as mock_spawn_mw, patch(
        "custom_components.ha_timescaledb_recorder.watchdog_loop", return_value=None,
    ), patch(
        "custom_components.ha_timescaledb_recorder.backfill_orchestrator",
    ) as mock_orch, patch(
        "custom_components.ha_timescaledb_recorder.ha_recorder",
    ) as mock_recorder, patch(
        "custom_components.ha_timescaledb_recorder._wait_for_recorder_and_clear",
        new=AsyncMock(),
    ):
        MockPQ.return_value.join = AsyncMock()
        MockRL.return_value.async_start = AsyncMock()  # registry_listener
        MockSL.return_value.async_start = MagicMock()

        mock_spawn_sw.return_value = MagicMock()
        mock_spawn_sw.return_value.start = MagicMock()
        mock_spawn_sw.return_value.read_watermark = MagicMock()
        mock_spawn_sw.return_value.read_open_entities = MagicMock()
        mock_spawn_mw.return_value = MagicMock()
        mock_spawn_mw.return_value.start = MagicMock()

        recorder_instance = MagicMock()
        recorder_instance.enabled = True
        mock_recorder.get_instance.return_value = recorder_instance

        # Capture the coroutine sentinel returned by backfill_orchestrator
        mock_orch.return_value = object()

        await async_setup_entry(hass, mock_entry)

        # Trigger _on_ha_started by calling the registered callback
        assert hass.bus.async_listen_once.called
        _event_type, _callback = hass.bus.async_listen_once.call_args[0]
        _callback(MagicMock())  # simulate HA started event

    assert mock_orch.called, "backfill_orchestrator not called"
    _, call_kwargs = mock_orch.call_args
    assert "threading_stop_event" in call_kwargs, (
        "backfill_orchestrator not called with threading_stop_event"
    )


# ---------------------------------------------------------------------------
# Phase 3: orchestrator done_callback behaviour
# ---------------------------------------------------------------------------

def test_orchestrator_done_callback_returns_on_cancelled_task():
    """_on_orchestrator_done must return without side-effects if task was cancelled."""
    from custom_components.ha_timescaledb_recorder import _make_orchestrator_done_callback

    hass = MagicMock()
    runtime = MagicMock()
    runtime.stop_event = MagicMock()
    runtime.stop_event.is_set.return_value = False
    runtime.loop_stop_event = MagicMock()
    runtime.loop_stop_event.is_set.return_value = False

    cb = _make_orchestrator_done_callback(hass, runtime, {})

    task = MagicMock()
    task.cancelled.return_value = True

    with patch(
        "custom_components.ha_timescaledb_recorder.notify_watchdog_recovery"
    ) as mock_notify:
        cb(task)

    mock_notify.assert_not_called()
    hass.async_create_background_task.assert_not_called()


def test_orchestrator_done_callback_returns_on_stop_event_set():
    """_on_orchestrator_done must return without side-effects if stop_event is set."""
    from custom_components.ha_timescaledb_recorder import _make_orchestrator_done_callback

    hass = MagicMock()
    runtime = MagicMock()
    runtime.stop_event = MagicMock()
    runtime.stop_event.is_set.return_value = True
    runtime.loop_stop_event = MagicMock()
    runtime.loop_stop_event.is_set.return_value = False

    cb = _make_orchestrator_done_callback(hass, runtime, {})

    task = MagicMock()
    task.cancelled.return_value = False

    with patch(
        "custom_components.ha_timescaledb_recorder.notify_watchdog_recovery"
    ) as mock_notify:
        cb(task)

    mock_notify.assert_not_called()
    hass.async_create_background_task.assert_not_called()


def test_orchestrator_done_callback_returns_on_loop_stop_event_set():
    """_on_orchestrator_done must return without side-effects if loop_stop_event is set."""
    from custom_components.ha_timescaledb_recorder import _make_orchestrator_done_callback

    hass = MagicMock()
    runtime = MagicMock()
    runtime.stop_event = MagicMock()
    runtime.stop_event.is_set.return_value = False
    runtime.loop_stop_event = MagicMock()
    runtime.loop_stop_event.is_set.return_value = True

    cb = _make_orchestrator_done_callback(hass, runtime, {})

    task = MagicMock()
    task.cancelled.return_value = False

    with patch(
        "custom_components.ha_timescaledb_recorder.notify_watchdog_recovery"
    ) as mock_notify:
        cb(task)

    mock_notify.assert_not_called()
    hass.async_create_background_task.assert_not_called()


def test_orchestrator_done_callback_returns_on_clean_exit():
    """_on_orchestrator_done must return without side-effects if task.exception() is None (clean exit)."""
    from custom_components.ha_timescaledb_recorder import _make_orchestrator_done_callback

    hass = MagicMock()
    runtime = MagicMock()
    runtime.stop_event = MagicMock()
    runtime.stop_event.is_set.return_value = False
    runtime.loop_stop_event = MagicMock()
    runtime.loop_stop_event.is_set.return_value = False

    cb = _make_orchestrator_done_callback(hass, runtime, {})

    task = MagicMock()
    task.cancelled.return_value = False
    task.exception.return_value = None

    with patch(
        "custom_components.ha_timescaledb_recorder.notify_watchdog_recovery"
    ) as mock_notify:
        cb(task)

    mock_notify.assert_not_called()
    hass.async_create_background_task.assert_not_called()


def test_orchestrator_done_callback_fires_notify_and_schedules_relaunch():
    """_on_orchestrator_done must call notify_watchdog_recovery and schedule _relaunch on unhandled exc."""
    from custom_components.ha_timescaledb_recorder import _make_orchestrator_done_callback

    hass = MagicMock()
    runtime = MagicMock()
    runtime.stop_event = MagicMock()
    runtime.stop_event.is_set.return_value = False
    runtime.loop_stop_event = MagicMock()
    runtime.loop_stop_event.is_set.return_value = False

    cb = _make_orchestrator_done_callback(hass, runtime, {})

    exc = RuntimeError("orchestrator died")
    task = MagicMock()
    task.cancelled.return_value = False
    task.exception.return_value = exc

    with patch(
        "custom_components.ha_timescaledb_recorder.notify_watchdog_recovery"
    ) as mock_notify:
        cb(task)

    mock_notify.assert_called_once()
    call_kwargs = mock_notify.call_args
    # Called as notify_watchdog_recovery(hass, component="orchestrator", exc=exc, ...)
    assert call_kwargs[1]["component"] == "orchestrator" or (
        len(call_kwargs[0]) >= 2 and call_kwargs[0][1] == "orchestrator"
    )
    # _relaunch coroutine must be scheduled via background task
    hass.async_create_background_task.assert_called_once()


# ---------------------------------------------------------------------------
# Phase 3: _relaunch throttle (MEDIUM-6)
# ---------------------------------------------------------------------------

async def test_orchestrator_relaunch_sleeps_5s_before_new_task():
    """_relaunch() must await asyncio.sleep(5) before creating the new orchestrator task."""
    from custom_components.ha_timescaledb_recorder import _make_orchestrator_done_callback

    hass = MagicMock()
    runtime = MagicMock()
    runtime.stop_event = MagicMock()
    runtime.stop_event.is_set.return_value = False
    runtime.loop_stop_event = MagicMock()
    runtime.loop_stop_event.is_set.return_value = False

    orchestrator_kwargs = {"hass": hass}

    call_order = []

    async def mock_sleep(n):
        call_order.append(("sleep", n))

    new_task = MagicMock()
    new_task.add_done_callback = MagicMock()

    def _capture_create(coro, *, name=None):
        # First call from done_callback schedules _relaunch; subsequent calls create the task
        call_order.append(("create_task",))
        return new_task

    hass.async_create_background_task = MagicMock(side_effect=_capture_create)

    cb = _make_orchestrator_done_callback(hass, runtime, orchestrator_kwargs)

    exc = RuntimeError("crash")
    task = MagicMock()
    task.cancelled.return_value = False
    task.exception.return_value = exc

    with patch(
        "custom_components.ha_timescaledb_recorder.notify_watchdog_recovery",
    ), patch(
        "custom_components.ha_timescaledb_recorder.asyncio.sleep", new=mock_sleep,
    ), patch(
        "custom_components.ha_timescaledb_recorder.backfill_orchestrator",
    ) as mock_borch:
        mock_borch.return_value = object()
        cb(task)

        # Retrieve the _relaunch coroutine passed to async_create_background_task
        assert hass.async_create_background_task.called
        relaunch_coro = hass.async_create_background_task.call_args[0][0]
        # Run it
        await relaunch_coro

    # sleep(5) must appear BEFORE the second async_create_task call (new orchestrator)
    sleep_indices = [i for i, c in enumerate(call_order) if c == ("sleep", 5)]
    task_indices = [i for i, c in enumerate(call_order) if c == ("create_task",)]
    assert sleep_indices, "asyncio.sleep(5) was not awaited in _relaunch"
    # After the initial create_task (which scheduled _relaunch), the sleep should come
    # before the next create_task (the new orchestrator task)
    assert len(task_indices) >= 2, f"Expected 2 create_task calls, got: {call_order}"
    assert sleep_indices[0] < task_indices[1], (
        f"sleep(5) must precede new orchestrator task creation. order: {call_order}"
    )


async def test_orchestrator_relaunch_aborts_if_stop_event_set_during_sleep():
    """_relaunch() must not create a new orchestrator task if stop_event is set after the sleep."""
    from custom_components.ha_timescaledb_recorder import _make_orchestrator_done_callback

    hass = MagicMock()
    runtime = MagicMock()

    # stop_event starts False, becomes True after sleep (simulating shutdown during sleep)
    stop_called = [False]

    class _StopEvent:
        def is_set(self):
            return stop_called[0]

    class _LoopStopEvent:
        def is_set(self):
            return False

    runtime.stop_event = _StopEvent()
    runtime.loop_stop_event = _LoopStopEvent()

    create_task_calls = []

    async def mock_sleep(n):
        # Simulate shutdown happening during the sleep
        stop_called[0] = True

    def _capture(coro, *, name=None):
        create_task_calls.append(coro)
        mock_task = MagicMock()
        mock_task.add_done_callback = MagicMock()
        return mock_task

    hass.async_create_background_task = MagicMock(side_effect=_capture)

    cb = _make_orchestrator_done_callback(hass, runtime, {})

    exc = RuntimeError("crash")
    task = MagicMock()
    task.cancelled.return_value = False
    task.exception.return_value = exc

    with patch(
        "custom_components.ha_timescaledb_recorder.notify_watchdog_recovery",
    ), patch(
        "custom_components.ha_timescaledb_recorder.asyncio.sleep", new=mock_sleep,
    ), patch(
        "custom_components.ha_timescaledb_recorder.backfill_orchestrator",
    ) as mock_borch:
        mock_borch.return_value = object()
        cb(task)

        # One create_background_task call so far (for _relaunch itself)
        relaunch_coro = hass.async_create_background_task.call_args[0][0]
        await relaunch_coro

    # After the sleep sets stop_called=True, _relaunch must NOT call async_create_task
    # for the new orchestrator. Total calls should be just 1 (the _relaunch coroutine itself).
    assert len(create_task_calls) == 1, (
        f"Expected only the _relaunch task, but {len(create_task_calls)} tasks were created"
    )


# ---------------------------------------------------------------------------
# Phase 3: recorder_disabled check
# ---------------------------------------------------------------------------

async def test_recorder_disabled_check_fires_issue_on_keyerror(hass, mock_entry):
    """create_recorder_disabled_issue must be called when recorder.get_instance raises KeyError."""
    with patch(
        "custom_components.ha_timescaledb_recorder.PersistentQueue"
    ) as MockPQ, patch(
        "custom_components.ha_timescaledb_recorder.TimescaledbMetaRecorderThread"
    ), patch(
        "custom_components.ha_timescaledb_recorder.TimescaledbStateRecorderThread"
    ), patch(
        "custom_components.ha_timescaledb_recorder._async_initial_registry_backfill",
        new=AsyncMock(),
    ), patch(
        "custom_components.ha_timescaledb_recorder.RegistryListener"
    ) as MockRL, patch(
        "custom_components.ha_timescaledb_recorder.StateListener"
    ) as MockSL, patch(
        "custom_components.ha_timescaledb_recorder._overflow_watcher", new=AsyncMock(),
    ), patch(
        "custom_components.ha_timescaledb_recorder.spawn_states_worker"
    ) as mock_spawn_sw, patch(
        "custom_components.ha_timescaledb_recorder.spawn_meta_worker"
    ) as mock_spawn_mw, patch(
        "custom_components.ha_timescaledb_recorder.watchdog_loop", return_value=None,
    ), patch(
        "custom_components.ha_timescaledb_recorder.ha_recorder",
    ) as mock_recorder, patch(
        "custom_components.ha_timescaledb_recorder.create_recorder_disabled_issue",
    ) as mock_create_issue, patch(
        "custom_components.ha_timescaledb_recorder._wait_for_recorder_and_clear",
        new=AsyncMock(),
    ):
        MockPQ.return_value.join = AsyncMock()
        MockRL.return_value.async_start = AsyncMock()  # registry_listener
        MockSL.return_value.async_start = MagicMock()

        mock_spawn_sw.return_value = MagicMock()
        mock_spawn_sw.return_value.start = MagicMock()
        mock_spawn_sw.return_value.read_watermark = MagicMock()
        mock_spawn_sw.return_value.read_open_entities = MagicMock()
        mock_spawn_mw.return_value = MagicMock()
        mock_spawn_mw.return_value.start = MagicMock()

        # Recorder not loaded — raises KeyError
        mock_recorder.get_instance.side_effect = KeyError("recorder")

        await async_setup_entry(hass, mock_entry)

    mock_create_issue.assert_called_once_with(hass)


async def test_recorder_disabled_check_fires_issue_when_enabled_false(hass, mock_entry):
    """create_recorder_disabled_issue must be called when recorder instance has enabled=False."""
    with patch(
        "custom_components.ha_timescaledb_recorder.PersistentQueue"
    ) as MockPQ, patch(
        "custom_components.ha_timescaledb_recorder.TimescaledbMetaRecorderThread"
    ), patch(
        "custom_components.ha_timescaledb_recorder.TimescaledbStateRecorderThread"
    ), patch(
        "custom_components.ha_timescaledb_recorder._async_initial_registry_backfill",
        new=AsyncMock(),
    ), patch(
        "custom_components.ha_timescaledb_recorder.RegistryListener"
    ) as MockRL, patch(
        "custom_components.ha_timescaledb_recorder.StateListener"
    ) as MockSL, patch(
        "custom_components.ha_timescaledb_recorder._overflow_watcher", new=AsyncMock(),
    ), patch(
        "custom_components.ha_timescaledb_recorder.spawn_states_worker"
    ) as mock_spawn_sw, patch(
        "custom_components.ha_timescaledb_recorder.spawn_meta_worker"
    ) as mock_spawn_mw, patch(
        "custom_components.ha_timescaledb_recorder.watchdog_loop", return_value=None,
    ), patch(
        "custom_components.ha_timescaledb_recorder.ha_recorder",
    ) as mock_recorder, patch(
        "custom_components.ha_timescaledb_recorder.create_recorder_disabled_issue",
    ) as mock_create_issue, patch(
        "custom_components.ha_timescaledb_recorder._wait_for_recorder_and_clear",
        new=AsyncMock(),
    ):
        MockPQ.return_value.join = AsyncMock()
        MockRL.return_value.async_start = AsyncMock()  # registry_listener
        MockSL.return_value.async_start = MagicMock()

        mock_spawn_sw.return_value = MagicMock()
        mock_spawn_sw.return_value.start = MagicMock()
        mock_spawn_sw.return_value.read_watermark = MagicMock()
        mock_spawn_sw.return_value.read_open_entities = MagicMock()
        mock_spawn_mw.return_value = MagicMock()
        mock_spawn_mw.return_value.start = MagicMock()

        recorder_instance = MagicMock()
        recorder_instance.enabled = False
        mock_recorder.get_instance.return_value = recorder_instance

        await async_setup_entry(hass, mock_entry)

    mock_create_issue.assert_called_once_with(hass)


async def test_recorder_disabled_check_silent_when_enabled_true(hass, mock_entry):
    """create_recorder_disabled_issue must NOT be called when recorder is enabled."""
    with patch(
        "custom_components.ha_timescaledb_recorder.PersistentQueue"
    ) as MockPQ, patch(
        "custom_components.ha_timescaledb_recorder.TimescaledbMetaRecorderThread"
    ), patch(
        "custom_components.ha_timescaledb_recorder.TimescaledbStateRecorderThread"
    ), patch(
        "custom_components.ha_timescaledb_recorder._async_initial_registry_backfill",
        new=AsyncMock(),
    ), patch(
        "custom_components.ha_timescaledb_recorder.RegistryListener"
    ) as MockRL, patch(
        "custom_components.ha_timescaledb_recorder.StateListener"
    ) as MockSL, patch(
        "custom_components.ha_timescaledb_recorder._overflow_watcher", new=AsyncMock(),
    ), patch(
        "custom_components.ha_timescaledb_recorder.spawn_states_worker"
    ) as mock_spawn_sw, patch(
        "custom_components.ha_timescaledb_recorder.spawn_meta_worker"
    ) as mock_spawn_mw, patch(
        "custom_components.ha_timescaledb_recorder.watchdog_loop", return_value=None,
    ), patch(
        "custom_components.ha_timescaledb_recorder.ha_recorder",
    ) as mock_recorder, patch(
        "custom_components.ha_timescaledb_recorder.create_recorder_disabled_issue",
    ) as mock_create_issue:
        MockPQ.return_value.join = AsyncMock()
        MockRL.return_value.async_start = AsyncMock()  # registry_listener
        MockSL.return_value.async_start = MagicMock()

        mock_spawn_sw.return_value = MagicMock()
        mock_spawn_sw.return_value.start = MagicMock()
        mock_spawn_sw.return_value.read_watermark = MagicMock()
        mock_spawn_sw.return_value.read_open_entities = MagicMock()
        mock_spawn_mw.return_value = MagicMock()
        mock_spawn_mw.return_value.start = MagicMock()

        recorder_instance = MagicMock()
        recorder_instance.enabled = True
        mock_recorder.get_instance.return_value = recorder_instance

        await async_setup_entry(hass, mock_entry)

    mock_create_issue.assert_not_called()


# ---------------------------------------------------------------------------
# Phase 3: HIGH-1 auto-clear background task
# ---------------------------------------------------------------------------

async def test_recorder_disabled_spawns_wait_task_on_issue_raised(hass, mock_entry):
    """When recorder_disabled issue is raised (KeyError), a background wait task must be spawned.

    _wait_for_recorder_and_clear is patched as a regular MagicMock (not AsyncMock) so
    that calling it returns the sentinel directly — AsyncMock wraps return_value in a
    new coroutine, preventing identity comparison in created_coros.
    """
    # Sentinel representing the coroutine that _wait_for_recorder_and_clear returns.
    wait_sentinel = object()

    with patch(
        "custom_components.ha_timescaledb_recorder.PersistentQueue"
    ) as MockPQ, patch(
        "custom_components.ha_timescaledb_recorder.TimescaledbMetaRecorderThread"
    ), patch(
        "custom_components.ha_timescaledb_recorder.TimescaledbStateRecorderThread"
    ), patch(
        "custom_components.ha_timescaledb_recorder._async_initial_registry_backfill",
        new=AsyncMock(),
    ), patch(
        "custom_components.ha_timescaledb_recorder.RegistryListener"
    ) as MockRL, patch(
        "custom_components.ha_timescaledb_recorder.StateListener"
    ) as MockSL, patch(
        "custom_components.ha_timescaledb_recorder._overflow_watcher", new=AsyncMock(),
    ), patch(
        "custom_components.ha_timescaledb_recorder.spawn_states_worker"
    ) as mock_spawn_sw, patch(
        "custom_components.ha_timescaledb_recorder.spawn_meta_worker"
    ) as mock_spawn_mw, patch(
        "custom_components.ha_timescaledb_recorder.watchdog_loop", return_value=None,
    ), patch(
        "custom_components.ha_timescaledb_recorder.ha_recorder",
    ) as mock_recorder, patch(
        "custom_components.ha_timescaledb_recorder.create_recorder_disabled_issue",
    ), patch(
        "custom_components.ha_timescaledb_recorder._wait_for_recorder_and_clear",
        new=MagicMock(return_value=wait_sentinel),
    ) as mock_wait_fn:
        MockPQ.return_value.join = AsyncMock()
        MockRL.return_value.async_start = AsyncMock()  # registry_listener
        MockSL.return_value.async_start = MagicMock()

        mock_spawn_sw.return_value = MagicMock()
        mock_spawn_sw.return_value.start = MagicMock()
        mock_spawn_sw.return_value.read_watermark = MagicMock()
        mock_spawn_sw.return_value.read_open_entities = MagicMock()
        mock_spawn_mw.return_value = MagicMock()
        mock_spawn_mw.return_value.start = MagicMock()

        mock_recorder.get_instance.side_effect = KeyError("recorder")

        # Track what async_create_background_task was called with
        created_coros = []
        def _capture(coro, *, name=None):
            created_coros.append(coro)
            return MagicMock()

        hass.async_create_background_task = MagicMock(side_effect=_capture)

        await async_setup_entry(hass, mock_entry)

    assert wait_sentinel in created_coros, (
        "_wait_for_recorder_and_clear coroutine not passed to hass.async_create_background_task"
    )


async def test_wait_for_recorder_and_clear_calls_clear_when_recorder_enabled():
    """_wait_for_recorder_and_clear must call clear_recorder_disabled_issue when recorder is enabled."""
    from custom_components.ha_timescaledb_recorder import _wait_for_recorder_and_clear

    hass = MagicMock()

    recorder_instance = MagicMock()
    recorder_instance.enabled = True

    with patch(
        "custom_components.ha_timescaledb_recorder.ha_recorder",
    ) as mock_recorder, patch(
        "custom_components.ha_timescaledb_recorder.clear_recorder_disabled_issue",
    ) as mock_clear:
        mock_recorder.async_wait_recorder = AsyncMock(return_value=recorder_instance)
        # hasattr check must succeed
        mock_recorder.__dict__["async_wait_recorder"] = mock_recorder.async_wait_recorder

        await _wait_for_recorder_and_clear(hass)

    mock_clear.assert_called_once_with(hass)


async def test_wait_for_recorder_and_clear_does_not_clear_when_recorder_none():
    """_wait_for_recorder_and_clear must NOT call clear when async_wait_recorder returns None."""
    from custom_components.ha_timescaledb_recorder import _wait_for_recorder_and_clear

    hass = MagicMock()

    with patch(
        "custom_components.ha_timescaledb_recorder.ha_recorder",
    ) as mock_recorder, patch(
        "custom_components.ha_timescaledb_recorder.clear_recorder_disabled_issue",
    ) as mock_clear:
        mock_recorder.async_wait_recorder = AsyncMock(return_value=None)
        mock_recorder.__dict__["async_wait_recorder"] = mock_recorder.async_wait_recorder

        await _wait_for_recorder_and_clear(hass)

    mock_clear.assert_not_called()


# ---------------------------------------------------------------------------
# Phase 3: MEDIUM-12 — unload cancels and awaits watchdog before orchestrator
# ---------------------------------------------------------------------------

async def test_unload_cancels_and_awaits_watchdog_before_orchestrator():
    """async_unload_entry must cancel+await watchdog_task before cancelling orchestrator_task.

    Uses real asyncio.Future objects (not MagicMock) for watchdog_task and
    orchestrator_task so that `await task` works correctly. Order-tracking is done
    by wrapping the futures in a small class that overrides .cancel() to record the
    call sequence before delegating to the underlying future.
    """
    call_order = []
    loop = asyncio.get_event_loop()

    class _TrackedFuture:
        """Thin awaitable wrapper that tracks .cancel() call order."""

        def __init__(self, label: str):
            self._fut = loop.create_future()
            self._fut.cancel()
            self._label = label

        def cancel(self):
            call_order.append(self._label)
            # Future is already cancelled — idempotent
            return self._fut.cancel()

        def __await__(self):
            return self._fut.__await__()

    watchdog_tracked = _TrackedFuture("watchdog_cancel")
    orch_tracked = _TrackedFuture("orch_cancel")

    data = MagicMock(spec=HaTimescaleDBData)
    data.stop_event = MagicMock()
    data.loop_stop_event = MagicMock()
    data.state_listener = MagicMock()
    data.registry_listener = MagicMock()
    data.registry_listener.async_stop = AsyncMock()
    data.meta_queue = MagicMock()
    data.states_worker = MagicMock()
    data.states_worker.is_alive = MagicMock(return_value=False)
    data.meta_worker = MagicMock()
    data.meta_worker.is_alive = MagicMock(return_value=False)
    data.watchdog_task = watchdog_tracked
    data.orchestrator_task = orch_tracked
    data.overflow_watcher_task = None

    mock_entry = MagicMock()
    mock_entry.runtime_data = data
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(return_value=None)

    await async_unload_entry(hass, mock_entry)

    assert "watchdog_cancel" in call_order, "watchdog_task.cancel() never called"
    assert "orch_cancel" in call_order, "orchestrator_task.cancel() never called"
    wd_idx = call_order.index("watchdog_cancel")
    orch_idx = call_order.index("orch_cancel")
    assert wd_idx < orch_idx, (
        f"watchdog must be cancelled before orchestrator. order: {call_order}"
    )


async def test_unload_handles_none_watchdog_task_gracefully():
    """async_unload_entry must handle watchdog_task=None without error (setup may have failed early)."""
    data = MagicMock(spec=HaTimescaleDBData)
    data.stop_event = MagicMock()
    data.loop_stop_event = MagicMock()
    data.watchdog_task = None
    data.orchestrator_task = None
    data.overflow_watcher_task = None
    data.state_listener = MagicMock()
    data.registry_listener = MagicMock()
    data.registry_listener.async_stop = AsyncMock()
    data.meta_queue = MagicMock()
    data.states_worker = MagicMock()
    data.states_worker.is_alive = MagicMock(return_value=False)
    data.meta_worker = MagicMock()
    data.meta_worker.is_alive = MagicMock(return_value=False)

    mock_entry = MagicMock()
    mock_entry.runtime_data = data
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(return_value=None)

    # Must not raise
    result = await async_unload_entry(hass, mock_entry)
    assert result is True


async def test_unload_preserves_phase2_behaviour():
    """Unload must still execute all Phase 2 shutdown steps after Phase 3 changes.

    Uses cancelled asyncio.Future objects for task fields so that `await task`
    works correctly (MagicMock.__await__ assignment is unreliable for dunder protocols).
    """
    loop = asyncio.get_event_loop()

    def _cancelled_future():
        fut = loop.create_future()
        fut.cancel()
        return fut

    data = MagicMock(spec=HaTimescaleDBData)
    data.stop_event = MagicMock()
    data.loop_stop_event = MagicMock()
    data.state_listener = MagicMock()
    data.registry_listener = MagicMock()
    data.registry_listener.async_stop = AsyncMock()
    data.meta_queue = MagicMock()
    data.states_worker = MagicMock()
    data.states_worker.is_alive = MagicMock(return_value=False)
    data.meta_worker = MagicMock()
    data.meta_worker.is_alive = MagicMock(return_value=False)

    # Use real cancelled futures so `await task` works in async_unload_entry.
    data.watchdog_task = _cancelled_future()
    data.orchestrator_task = _cancelled_future()
    data.overflow_watcher_task = _cancelled_future()

    mock_entry = MagicMock()
    mock_entry.runtime_data = data
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(return_value=None)

    await async_unload_entry(hass, mock_entry)

    # Phase 2 steps must still fire
    data.stop_event.set.assert_called_once()
    data.loop_stop_event.set.assert_called_once()
    data.state_listener.stop.assert_called_once()
    data.registry_listener.async_stop.assert_awaited_once()
    data.meta_queue.wake_consumer.assert_called_once()
    hass.async_add_executor_job.assert_any_call(data.states_worker.join, 30)
    hass.async_add_executor_job.assert_any_call(data.meta_worker.join, 30)
