"""Unit tests for sensor.py health sensor entities.

Tests use mocked config entries and runtime_data so no HA instance is needed.
Health/db_status sensors are push-updated — tests call _update_state() directly.
Metric sensors are polled — tests call async_update() and await the result.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from custom_components.timescaledb_recorder.sensor import (
    TimescaledbDbStatusSensor,
    TimescaledbEventsDroppedSensor,
    TimescaledbHealthSensor,
    TimescaledbMetaQueueDepthSensor,
    TimescaledbQueueDepthSensor,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_entry(
    *,
    states_alive: bool = True,
    meta_alive: bool = True,
    total_dropped: int = 0,
    live_queue_peak: int = 0,
    meta_queue_len: int = 0,
) -> MagicMock:
    """Build a minimal mock config entry with runtime_data populated."""
    entry = MagicMock()
    entry.entry_id = "test_entry_id"

    states_worker = MagicMock()
    states_worker.is_alive.return_value = states_alive
    meta_worker = MagicMock()
    meta_worker.is_alive.return_value = meta_alive

    live_queue = MagicMock()
    live_queue._total_dropped = total_dropped
    live_queue.get_and_reset_peak.return_value = live_queue_peak

    meta_queue = MagicMock()
    meta_queue.__len__ = MagicMock(return_value=meta_queue_len)

    data = MagicMock()
    data.states_worker = states_worker
    data.meta_worker = meta_worker
    data.live_queue = live_queue
    data.meta_queue = meta_queue

    entry.runtime_data = data
    return entry


def _make_hass(active_issue_ids: set[str] | None = None) -> MagicMock:
    """Build a minimal mock hass with a stubbed issue_registry."""
    if active_issue_ids is None:
        active_issue_ids = set()

    hass = MagicMock()

    # Build mock issue objects — one per issue id.
    issues = {}
    for issue_id in active_issue_ids:
        issue = MagicMock()
        issue.issue_id = issue_id
        issue.domain = "timescaledb_recorder"
        issues[(issue.domain, issue_id)] = issue

    registry = MagicMock()
    registry.issues = issues

    # Patch ir.async_get so sensors see our mocked registry.
    hass._mock_issue_registry = registry
    return hass


# ---------------------------------------------------------------------------
# TimescaledbHealthSensor
# ---------------------------------------------------------------------------

def test_health_sensor_ok():
    """No active issues and both workers alive → state 'ok'."""
    hass = _make_hass()
    entry = _make_entry()

    with patch(
        "custom_components.timescaledb_recorder.sensor.ir.async_get",
        return_value=hass._mock_issue_registry,
    ):
        sensor = TimescaledbHealthSensor(hass, entry)
        sensor._update_state()

    assert sensor._attr_native_value == "ok"


def test_health_sensor_degraded():
    """buffer_dropping issue active → state 'degraded'."""
    hass = _make_hass(active_issue_ids={"buffer_dropping"})
    entry = _make_entry()

    with patch(
        "custom_components.timescaledb_recorder.sensor.ir.async_get",
        return_value=hass._mock_issue_registry,
    ):
        sensor = TimescaledbHealthSensor(hass, entry)
        sensor._update_state()

    assert sensor._attr_native_value == "degraded"


def test_health_sensor_error_issue():
    """db_unreachable issue active → state 'error'."""
    hass = _make_hass(active_issue_ids={"db_unreachable"})
    entry = _make_entry()

    with patch(
        "custom_components.timescaledb_recorder.sensor.ir.async_get",
        return_value=hass._mock_issue_registry,
    ):
        sensor = TimescaledbHealthSensor(hass, entry)
        sensor._update_state()

    assert sensor._attr_native_value == "error"


def test_health_sensor_error_worker_dead():
    """states_worker not alive (no issues) → state 'error'."""
    hass = _make_hass()
    entry = _make_entry(states_alive=False)

    with patch(
        "custom_components.timescaledb_recorder.sensor.ir.async_get",
        return_value=hass._mock_issue_registry,
    ):
        sensor = TimescaledbHealthSensor(hass, entry)
        sensor._update_state()

    assert sensor._attr_native_value == "error"


# ---------------------------------------------------------------------------
# TimescaledbDbStatusSensor
# ---------------------------------------------------------------------------

def test_db_status_connected():
    """No active issues → 'connected'."""
    hass = _make_hass()
    entry = _make_entry()

    with patch(
        "custom_components.timescaledb_recorder.sensor.ir.async_get",
        return_value=hass._mock_issue_registry,
    ):
        sensor = TimescaledbDbStatusSensor(hass, entry)
        sensor._update_state()

    assert sensor._attr_native_value == "connected"


def test_db_status_disconnected():
    """db_unreachable issue active → 'disconnected'."""
    hass = _make_hass(active_issue_ids={"db_unreachable"})
    entry = _make_entry()

    with patch(
        "custom_components.timescaledb_recorder.sensor.ir.async_get",
        return_value=hass._mock_issue_registry,
    ):
        sensor = TimescaledbDbStatusSensor(hass, entry)
        sensor._update_state()

    assert sensor._attr_native_value == "disconnected"


def test_db_status_stalled():
    """states_worker_stalled issue active → 'stalled'."""
    hass = _make_hass(active_issue_ids={"states_worker_stalled"})
    entry = _make_entry()

    with patch(
        "custom_components.timescaledb_recorder.sensor.ir.async_get",
        return_value=hass._mock_issue_registry,
    ):
        sensor = TimescaledbDbStatusSensor(hass, entry)
        sensor._update_state()

    assert sensor._attr_native_value == "stalled"


# ---------------------------------------------------------------------------
# TimescaledbQueueDepthSensor
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_queue_depth_peak():
    """get_and_reset_peak() returns 42 → native_value = 42."""
    entry = _make_entry(live_queue_peak=42)
    sensor = TimescaledbQueueDepthSensor(entry)
    await sensor.async_update()

    assert sensor._attr_native_value == 42
    entry.runtime_data.live_queue.get_and_reset_peak.assert_called_once()


# ---------------------------------------------------------------------------
# TimescaledbEventsDroppedSensor
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_events_dropped_delta():
    """_total_dropped goes from 0 to 5 between polls → delta = 5."""
    entry = _make_entry(total_dropped=0)
    sensor = TimescaledbEventsDroppedSensor(entry)

    # First poll: 0 → 0
    await sensor.async_update()
    assert sensor._attr_native_value == 0

    # Simulate 5 drops between polls
    entry.runtime_data.live_queue._total_dropped = 5
    await sensor.async_update()
    assert sensor._attr_native_value == 5


@pytest.mark.asyncio
async def test_events_dropped_wrap():
    """Second poll has same total → delta = 0 (no new drops)."""
    entry = _make_entry(total_dropped=10)
    sensor = TimescaledbEventsDroppedSensor(entry)

    # First poll
    await sensor.async_update()
    assert sensor._attr_native_value == 10

    # Second poll — no new drops
    await sensor.async_update()
    assert sensor._attr_native_value == 0


# ---------------------------------------------------------------------------
# TimescaledbMetaQueueDepthSensor
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_meta_queue_depth():
    """len(meta_queue) = 3 → native_value = 3."""
    entry = _make_entry(meta_queue_len=3)
    sensor = TimescaledbMetaQueueDepthSensor(entry)
    await sensor.async_update()

    assert sensor._attr_native_value == 3
