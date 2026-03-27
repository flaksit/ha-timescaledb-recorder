"""Unit tests for StateIngester."""
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

import asyncpg
import pytest

from custom_components.ha_timescaledb.const import DEFAULT_BATCH_SIZE, DEFAULT_FLUSH_INTERVAL, INSERT_SQL
from custom_components.ha_timescaledb.ingester import StateIngester
from homeassistant.helpers.entityfilter import convert_filter


def _make_filter(**kwargs):
    """Build an EntityFilter from flat keyword args (include/exclude_domains, etc.)."""
    defaults = {
        "include_domains": [],
        "include_entity_globs": [],
        "include_entities": [],
        "exclude_domains": [],
        "exclude_entity_globs": [],
        "exclude_entities": [],
    }
    defaults.update(kwargs)
    return convert_filter(defaults)


def _make_state_event(entity_id, state_val, attributes=None):
    """Create a mock state_changed event."""
    if attributes is None:
        attributes = {}
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    new_state = MagicMock()
    new_state.entity_id = entity_id
    new_state.state = state_val
    new_state.attributes = attributes
    new_state.last_updated = now
    new_state.last_changed = now
    event = MagicMock()
    event.data = {"new_state": new_state}
    return event


@pytest.fixture
def hass():
    """Return a mock hass instance."""
    h = MagicMock()
    h.async_create_task = MagicMock()
    h.bus = MagicMock()
    h.bus.async_listen = MagicMock(return_value=MagicMock())
    return h


@pytest.fixture
def entity_filter():
    """Return a permissive entity filter (all entities pass)."""
    return _make_filter()


@pytest.fixture
def ingester(hass, mock_pool, entity_filter):
    """Return a StateIngester with batch_size=3."""
    return StateIngester(
        hass=hass,
        pool=mock_pool,
        entity_filter=entity_filter,
        batch_size=3,
        flush_interval=DEFAULT_FLUSH_INTERVAL,
        ingest_unavailable=False,
    )


def test_buffer_appends_state(ingester):
    event = _make_state_event("sensor.temp", "21.5", {"unit_of_measurement": "°C"})
    ingester._handle_state_changed(event)
    assert len(ingester._buffer) == 1
    row = ingester._buffer[0]
    assert row[0] == "sensor.temp"
    assert row[1] == "21.5"
    assert row[2] == {"unit_of_measurement": "°C"}


def test_skip_none_new_state(ingester):
    event = MagicMock()
    event.data = {"new_state": None}
    ingester._handle_state_changed(event)
    assert len(ingester._buffer) == 0


def test_skip_unavailable(ingester):
    event = _make_state_event("sensor.temp", "unavailable")
    ingester._handle_state_changed(event)
    assert len(ingester._buffer) == 0


def test_skip_unknown(ingester):
    event = _make_state_event("sensor.temp", "unknown")
    ingester._handle_state_changed(event)
    assert len(ingester._buffer) == 0


def test_ingest_unavailable_when_enabled(hass, mock_pool, entity_filter):
    ing = StateIngester(
        hass=hass,
        pool=mock_pool,
        entity_filter=entity_filter,
        batch_size=3,
        flush_interval=DEFAULT_FLUSH_INTERVAL,
        ingest_unavailable=True,
    )
    event = _make_state_event("sensor.temp", "unavailable")
    ing._handle_state_changed(event)
    assert len(ing._buffer) == 1


def test_skip_excluded_entity(hass, mock_pool):
    ef = _make_filter(exclude_domains=["light"])
    ing = StateIngester(
        hass=hass,
        pool=mock_pool,
        entity_filter=ef,
        batch_size=3,
        flush_interval=DEFAULT_FLUSH_INTERVAL,
        ingest_unavailable=False,
    )
    event = _make_state_event("light.kitchen", "on")
    ing._handle_state_changed(event)
    assert len(ing._buffer) == 0


def test_include_filter(hass, mock_pool):
    ef = _make_filter(include_domains=["sensor"])
    ing = StateIngester(
        hass=hass,
        pool=mock_pool,
        entity_filter=ef,
        batch_size=5,
        flush_interval=DEFAULT_FLUSH_INTERVAL,
        ingest_unavailable=False,
    )
    ing._handle_state_changed(_make_state_event("sensor.temp", "22"))
    ing._handle_state_changed(_make_state_event("light.kitchen", "on"))
    assert len(ing._buffer) == 1
    assert ing._buffer[0][0] == "sensor.temp"


def test_batch_flush_on_size(hass, mock_pool, entity_filter):
    ing = StateIngester(
        hass=hass,
        pool=mock_pool,
        entity_filter=entity_filter,
        batch_size=3,
        flush_interval=DEFAULT_FLUSH_INTERVAL,
        ingest_unavailable=False,
    )
    for i in range(3):
        ing._handle_state_changed(_make_state_event(f"sensor.s{i}", "1"))
    hass.async_create_task.assert_called_once()


async def test_flush_calls_executemany(ingester, mock_conn):
    ingester._handle_state_changed(_make_state_event("sensor.temp", "22"))
    await ingester._async_flush()
    mock_conn.executemany.assert_called_once()
    call_args = mock_conn.executemany.call_args
    assert call_args[0][0] == INSERT_SQL
    assert len(call_args[0][1]) == 1


async def test_flush_clears_buffer(ingester, mock_conn):
    ingester._handle_state_changed(_make_state_event("sensor.temp", "22"))
    await ingester._async_flush()
    assert len(ingester._buffer) == 0


async def test_flush_empty_buffer_noop(ingester, mock_conn):
    await ingester._async_flush()
    mock_conn.executemany.assert_not_called()


async def test_timer_flush_with_data(ingester, mock_conn):
    ingester._handle_state_changed(_make_state_event("sensor.temp", "22"))
    await ingester._async_flush_timer(None)
    mock_conn.executemany.assert_called_once()


async def test_timer_flush_empty_noop(ingester, mock_conn):
    await ingester._async_flush_timer(None)
    mock_conn.executemany.assert_not_called()


async def test_jsonb_attributes_passed(ingester, mock_conn):
    attrs = {"nested": {"key": "value"}, "list": [1, 2, 3]}
    ingester._handle_state_changed(_make_state_event("sensor.temp", "22", attrs))
    await ingester._async_flush()
    call_args = mock_conn.executemany.call_args
    rows = call_args[0][1]
    assert rows[0][2] == attrs


async def test_connection_error_requeues(ingester, mock_conn, mock_pool):
    mock_conn.executemany.side_effect = OSError("connection refused")
    mock_pool.expire_connections = MagicMock()
    ingester._handle_state_changed(_make_state_event("sensor.temp", "22"))
    await ingester._async_flush()
    mock_pool.expire_connections.assert_called_once()
    assert len(ingester._buffer) == 1


async def test_postgres_error_drops_batch(ingester, mock_conn):
    mock_conn.executemany.side_effect = asyncpg.PostgresError()
    ingester._handle_state_changed(_make_state_event("sensor.temp", "22"))
    await ingester._async_flush()
    assert len(ingester._buffer) == 0


async def test_stop_flushes_remaining(ingester, mock_conn):
    cancel_listener = MagicMock()
    cancel_timer = MagicMock()
    ingester._cancel_listener = cancel_listener
    ingester._cancel_timer = cancel_timer
    ingester._handle_state_changed(_make_state_event("sensor.temp", "22"))
    await ingester.async_stop()
    cancel_listener.assert_called_once()
    cancel_timer.assert_called_once()
    mock_conn.executemany.assert_called_once()
