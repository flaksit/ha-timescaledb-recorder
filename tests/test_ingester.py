"""Unit tests for StateIngester (thin queue relay)."""
import inspect
import queue
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from custom_components.ha_timescaledb_recorder.ingester import StateIngester
from custom_components.ha_timescaledb_recorder.worker import StateRow
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
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    new_state = MagicMock()
    new_state.entity_id = entity_id
    new_state.state = state_val
    new_state.attributes = attributes or {}
    new_state.last_updated = now
    new_state.last_changed = now
    event = MagicMock()
    event.data = {"new_state": new_state}
    return event


@pytest.fixture
def hass():
    """Return a mock hass instance."""
    h = MagicMock()
    h.bus = MagicMock()
    h.bus.async_listen = MagicMock(return_value=MagicMock())
    return h


@pytest.fixture
def shared_queue():
    """Return a fresh Queue for each test."""
    return queue.Queue()


@pytest.fixture
def entity_filter():
    """Return a permissive entity filter (all entities pass)."""
    return _make_filter()


@pytest.fixture
def ingester(hass, shared_queue, entity_filter):
    """Return a StateIngester wired to the shared queue."""
    return StateIngester(hass=hass, queue=shared_queue, entity_filter=entity_filter)


def test_enqueues_state_row(ingester, shared_queue):
    """Handler must enqueue a StateRow with all fields correctly populated."""
    ingester._handle_state_changed(
        _make_state_event("sensor.temp", "21.5", {"unit": "°C"})
    )
    assert shared_queue.qsize() == 1
    row = shared_queue.get_nowait()
    assert isinstance(row, StateRow)
    assert row.entity_id == "sensor.temp"
    assert row.state == "21.5"
    assert row.attributes == {"unit": "°C"}


def test_skip_none_new_state(ingester, shared_queue):
    """Events with new_state=None must be silently dropped."""
    event = MagicMock()
    event.data = {"new_state": None}
    ingester._handle_state_changed(event)
    assert shared_queue.qsize() == 0


def test_records_unavailable(ingester, shared_queue):
    """Unavailable states are recorded — unlike HA recorder, no data gaps."""
    ingester._handle_state_changed(_make_state_event("sensor.temp", "unavailable"))
    assert shared_queue.qsize() == 1
    row = shared_queue.get_nowait()
    assert row.state == "unavailable"


def test_records_unknown(ingester, shared_queue):
    """Unknown states are recorded — unlike HA recorder, no data gaps."""
    ingester._handle_state_changed(_make_state_event("sensor.temp", "unknown"))
    assert shared_queue.qsize() == 1
    row = shared_queue.get_nowait()
    assert row.state == "unknown"


def test_skip_excluded_entity(hass, shared_queue):
    """Entities matching an exclude filter must not be enqueued."""
    ef = _make_filter(exclude_domains=["light"])
    ing = StateIngester(hass=hass, queue=shared_queue, entity_filter=ef)
    ing._handle_state_changed(_make_state_event("light.kitchen", "on"))
    assert shared_queue.qsize() == 0


def test_include_filter(hass, shared_queue):
    """Only entities matching an include filter must be enqueued."""
    ef = _make_filter(include_domains=["sensor"])
    ing = StateIngester(hass=hass, queue=shared_queue, entity_filter=ef)
    ing._handle_state_changed(_make_state_event("sensor.temp", "22"))
    ing._handle_state_changed(_make_state_event("light.kitchen", "on"))
    assert shared_queue.qsize() == 1
    assert shared_queue.get_nowait().entity_id == "sensor.temp"


def test_attributes_copied_not_referenced(ingester, shared_queue):
    """Attributes dict must be copied at enqueue time to prevent later mutation aliasing."""
    attrs = {"key": "value"}
    ingester._handle_state_changed(_make_state_event("sensor.temp", "21", attrs))
    # Mutate the original dict after enqueue — the queued row must be unaffected.
    attrs["key"] = "mutated"
    row = shared_queue.get_nowait()
    assert row.attributes["key"] == "value", (
        "StateRow.attributes must be an independent copy, not a reference to the original dict"
    )


def test_stop_cancels_listener(ingester):
    """stop() must call the cancel function and clear _cancel_listener."""
    cancel_fn = MagicMock()
    ingester._cancel_listener = cancel_fn
    ingester.stop()
    cancel_fn.assert_called_once()
    assert ingester._cancel_listener is None, (
        "stop() must clear _cancel_listener after cancelling to allow safe re-call"
    )


def test_stop_is_sync(ingester):
    """stop() must be a plain callable — not a coroutine — to prevent accidental await."""
    assert not inspect.iscoroutinefunction(ingester.stop), (
        "stop() must not be a coroutine function; callers must not await it"
    )
