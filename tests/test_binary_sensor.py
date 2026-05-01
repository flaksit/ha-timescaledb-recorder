"""Unit tests for binary_sensor.py push-updated binary sensor entities.

Tests mock async_dispatcher_connect to capture the registered callback and
call it directly to simulate a dispatch, rather than exercising the HA
dispatcher infrastructure.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.timescaledb_recorder.binary_sensor import (
    TimescaledbBufferOverflowSensor,
    TimescaledbMetaWorkerAliveSensor,
    TimescaledbStatesWorkerAliveSensor,
)
from custom_components.timescaledb_recorder.const import (
    SIGNAL_OVERFLOW_CHANGE,
    SIGNAL_WORKER_STATE_CHANGE,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_entry(
    *,
    overflowed: bool = False,
    states_alive: bool = True,
    meta_alive: bool = True,
) -> MagicMock:
    """Build a minimal mock config entry with runtime_data."""
    entry = MagicMock()
    entry.entry_id = "test_entry_id"

    live_queue = MagicMock()
    live_queue.overflowed = overflowed

    states_worker = MagicMock()
    states_worker.is_alive.return_value = states_alive

    meta_worker = MagicMock()
    meta_worker.is_alive.return_value = meta_alive

    data = MagicMock()
    data.live_queue = live_queue
    data.states_worker = states_worker
    data.meta_worker = meta_worker

    entry.runtime_data = data
    return entry


def _make_hass() -> MagicMock:
    """Build a mock hass instance."""
    hass = MagicMock()
    return hass


async def _add_to_hass(sensor, hass: MagicMock) -> dict[str, object]:
    """Call async_added_to_hass and return a dict of {signal: callback}.

    Patches async_dispatcher_connect to intercept each subscription call,
    recording the signal name and the callback. Returns the mapping so tests
    can invoke callbacks directly to simulate dispatch.

    Also sets sensor.hass (normally done by HA entity registry) and stubs
    sensor.async_on_remove so subscription cancellation does not raise.
    """
    sensor.hass = hass
    # async_on_remove stores the unsubscribe function — stub it so the test
    # does not need to set up the full entity lifecycle.
    sensor.async_on_remove = MagicMock()

    captured: dict[str, object] = {}

    def _mock_connect(hass_arg, signal, callback_fn):
        captured[signal] = callback_fn
        # Return a callable "unsubscribe" mock.
        return MagicMock()

    with patch(
        "custom_components.timescaledb_recorder.binary_sensor.async_dispatcher_connect",
        side_effect=_mock_connect,
    ):
        await sensor.async_added_to_hass()

    return captured


# ---------------------------------------------------------------------------
# TimescaledbBufferOverflowSensor
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_buffer_overflow_initial_state():
    """overflowed=False → is_on=False after async_added_to_hass."""
    entry = _make_entry(overflowed=False)
    hass = _make_hass()
    sensor = TimescaledbBufferOverflowSensor(entry)
    await _add_to_hass(sensor, hass)

    assert sensor.is_on is False


@pytest.mark.asyncio
async def test_buffer_overflow_initial_state_true():
    """overflowed=True at setup → is_on=True."""
    entry = _make_entry(overflowed=True)
    hass = _make_hass()
    sensor = TimescaledbBufferOverflowSensor(entry)
    await _add_to_hass(sensor, hass)

    assert sensor.is_on is True


@pytest.mark.asyncio
async def test_buffer_overflow_push():
    """Dispatch SIGNAL_OVERFLOW_CHANGE with overflowed=True → is_on becomes True."""
    entry = _make_entry(overflowed=False)
    hass = _make_hass()
    sensor = TimescaledbBufferOverflowSensor(entry)

    # Stub async_write_ha_state so tests don't need a full HA entity context.
    sensor.async_write_ha_state = MagicMock()

    callbacks = await _add_to_hass(sensor, hass)
    assert sensor.is_on is False

    # Simulate overflow flip and dispatch.
    entry.runtime_data.live_queue.overflowed = True
    callbacks[SIGNAL_OVERFLOW_CHANGE]()

    assert sensor.is_on is True
    sensor.async_write_ha_state.assert_called_once()


# ---------------------------------------------------------------------------
# TimescaledbStatesWorkerAliveSensor
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_states_worker_alive_initial():
    """states worker is_alive()=True → is_on=True after setup."""
    entry = _make_entry(states_alive=True)
    hass = _make_hass()
    sensor = TimescaledbStatesWorkerAliveSensor(entry)
    await _add_to_hass(sensor, hass)

    assert sensor.is_on is True


@pytest.mark.asyncio
async def test_states_worker_alive_dead():
    """states worker is_alive()=False → is_on=False after setup."""
    entry = _make_entry(states_alive=False)
    hass = _make_hass()
    sensor = TimescaledbStatesWorkerAliveSensor(entry)
    await _add_to_hass(sensor, hass)

    assert sensor.is_on is False


@pytest.mark.asyncio
async def test_states_worker_push_update():
    """Dispatch SIGNAL_WORKER_STATE_CHANGE with worker now alive → is_on=True."""
    entry = _make_entry(states_alive=False)
    hass = _make_hass()
    sensor = TimescaledbStatesWorkerAliveSensor(entry)
    sensor.async_write_ha_state = MagicMock()

    callbacks = await _add_to_hass(sensor, hass)
    assert sensor.is_on is False

    # Simulate worker respawn.
    entry.runtime_data.states_worker.is_alive.return_value = True
    callbacks[SIGNAL_WORKER_STATE_CHANGE]()

    assert sensor.is_on is True
    sensor.async_write_ha_state.assert_called_once()


# ---------------------------------------------------------------------------
# TimescaledbMetaWorkerAliveSensor
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_meta_worker_alive_initial():
    """meta worker is_alive()=True → is_on=True after setup."""
    entry = _make_entry(meta_alive=True)
    hass = _make_hass()
    sensor = TimescaledbMetaWorkerAliveSensor(entry)
    await _add_to_hass(sensor, hass)

    assert sensor.is_on is True


@pytest.mark.asyncio
async def test_meta_worker_alive_dead():
    """meta worker is_alive()=False → is_on=False after setup."""
    entry = _make_entry(meta_alive=False)
    hass = _make_hass()
    sensor = TimescaledbMetaWorkerAliveSensor(entry)
    await _add_to_hass(sensor, hass)

    assert sensor.is_on is False
