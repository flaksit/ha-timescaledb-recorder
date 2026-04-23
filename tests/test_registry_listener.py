"""Unit tests for RegistryListener (registry relay + SCD2 change-detection helpers)."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from custom_components.ha_timescaledb_recorder.registry_listener import RegistryListener, _to_json_safe
from custom_components.ha_timescaledb_recorder.persistent_queue import PersistentQueue


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_meta_queue():
    """Return a PersistentQueue mock with put_async as AsyncMock."""
    q = MagicMock(spec=PersistentQueue)
    q.put_async = AsyncMock()
    return q


@pytest.fixture
def listener(hass, mock_meta_queue):
    """Return a RegistryListener wired to mock_meta_queue."""
    return RegistryListener(hass=hass, meta_queue=mock_meta_queue)


@pytest.fixture
def enabled_listener(hass, mock_meta_queue):
    """Return a RegistryListener with DISCARD mode disabled (live mode)."""
    rl = RegistryListener(hass=hass, meta_queue=mock_meta_queue)
    rl.enable()
    return rl


# ---------------------------------------------------------------------------
# _to_json_safe helper
# ---------------------------------------------------------------------------

def test_to_json_safe_converts_datetime_to_iso():
    """datetime values in params must be converted to ISO-8601 strings."""
    t = datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc)
    out = _to_json_safe(("x.y", t, None, [1, 2]))
    assert isinstance(out[1], str)
    assert out[1] == "2026-04-21T12:00:00+00:00"
    assert out[0] == "x.y"
    assert out[2] is None
    assert out[3] == [1, 2]


def test_to_json_safe_none_passthrough():
    """_to_json_safe(None) must return None (remove-action path)."""
    assert _to_json_safe(None) is None


def test_to_json_safe_non_datetime_values_unchanged():
    """Non-datetime types (str, int, list, None elements) must pass through unchanged."""
    out = _to_json_safe(("hello", 42, None, ["a", "b"]))
    assert out == ["hello", 42, None, ["a", "b"]]


# ---------------------------------------------------------------------------
# DISCARD mode
# ---------------------------------------------------------------------------

def test_starts_in_discard_mode(listener):
    """RegistryListener must start with _enabled=False (DISCARD mode)."""
    assert listener._enabled is False


def test_enable_flips_to_live_mode(listener):
    """enable() must set _enabled=True."""
    listener.enable()
    assert listener._enabled is True


def test_entity_callback_discarded_before_enable(listener, mock_meta_queue):
    """Events must be silently dropped when _enabled=False."""
    listener._entity_reg = MagicMock()
    event = MagicMock()
    event.data = {"action": "update", "entity_id": "sensor.temp", "old_entity_id": None}
    listener._handle_entity_registry_updated(event)
    mock_meta_queue.put_async.assert_not_called()


def test_device_callback_discarded_before_enable(listener, mock_meta_queue):
    """Device events must be silently dropped when _enabled=False."""
    listener._device_reg = MagicMock()
    event = MagicMock()
    event.data = {"action": "update", "device_id": "dev-001"}
    listener._handle_device_registry_updated(event)
    mock_meta_queue.put_async.assert_not_called()


def test_area_callback_discarded_before_enable(listener, mock_meta_queue):
    """Area events must be silently dropped when _enabled=False."""
    listener._area_reg = MagicMock()
    event = MagicMock()
    event.data = {"action": "update", "area_id": "area-001"}
    listener._handle_area_registry_updated(event)
    mock_meta_queue.put_async.assert_not_called()


def test_label_callback_discarded_before_enable(listener, mock_meta_queue):
    """Label events must be silently dropped when _enabled=False."""
    listener._label_reg = MagicMock()
    event = MagicMock()
    event.data = {"action": "update", "label_id": "label-001"}
    listener._handle_label_registry_updated(event)
    mock_meta_queue.put_async.assert_not_called()


def test_entity_callback_enqueues_after_enable(enabled_listener, mock_meta_queue, mock_entity_registry):
    """Events must be enqueued normally after enable() is called."""
    enabled_listener._entity_reg = mock_entity_registry

    event = MagicMock()
    event.data = {
        "action": "update",
        "entity_id": "sensor.living_room_temp",
        "old_entity_id": None,
    }
    enabled_listener._handle_entity_registry_updated(event)

    mock_meta_queue.put_async.assert_called_once()
    item = mock_meta_queue.put_async.call_args.args[0]
    assert item["registry"] == "entity"
    assert item["action"] == "update"


# ---------------------------------------------------------------------------
# Entity registry callback → dict enqueue (live mode)
# ---------------------------------------------------------------------------

def test_entity_callback_enqueues_dict(enabled_listener, mock_meta_queue, mock_entity_registry):
    """_handle_entity_registry_updated must schedule put_async with a correctly-shaped dict."""
    enabled_listener._entity_reg = mock_entity_registry

    event = MagicMock()
    event.data = {
        "action": "update",
        "entity_id": "sensor.living_room_temp",
        "old_entity_id": None,
    }
    enabled_listener._handle_entity_registry_updated(event)

    enabled_listener._hass.async_create_task.assert_called_once()
    mock_meta_queue.put_async.assert_called_once()

    item = mock_meta_queue.put_async.call_args.args[0]
    assert item["registry"] == "entity"
    assert item["action"] == "update"
    assert item["registry_id"] == "sensor.living_room_temp"
    assert item["old_id"] is None
    assert item["params"] is not None, "Non-remove action must extract params"
    assert "enqueued_at" in item


def test_entity_remove_enqueues_params_none(enabled_listener, mock_meta_queue):
    """Remove action must enqueue params=None — registry entry is already gone."""
    enabled_listener._entity_reg = MagicMock()

    event = MagicMock()
    event.data = {
        "action": "remove",
        "entity_id": "sensor.temp",
        "old_entity_id": None,
    }
    enabled_listener._handle_entity_registry_updated(event)

    item = mock_meta_queue.put_async.call_args.args[0]
    assert item["params"] is None, "Remove action must not fetch params"
    assert item["action"] == "remove"
    enabled_listener._entity_reg.async_get.assert_not_called()


def test_entity_rename_sets_old_id(enabled_listener, mock_meta_queue, mock_entity_registry):
    """Rename (update with old_entity_id) must populate old_id in the dict."""
    enabled_listener._entity_reg = mock_entity_registry

    event = MagicMock()
    event.data = {
        "action": "update",
        "entity_id": "sensor.living_room_temp",
        "old_entity_id": "sensor.old_name",
    }
    enabled_listener._handle_entity_registry_updated(event)

    item = mock_meta_queue.put_async.call_args.args[0]
    assert item["old_id"] == "sensor.old_name"
    assert item["registry_id"] == "sensor.living_room_temp"


# ---------------------------------------------------------------------------
# Area registry callback — reorder skip
# ---------------------------------------------------------------------------

def test_area_reorder_skipped(enabled_listener, mock_meta_queue):
    """Reorder events carry no data change and must be silently dropped (Pitfall 6)."""
    event = MagicMock()
    event.data = {"action": "reorder"}
    enabled_listener._handle_area_registry_updated(event)
    mock_meta_queue.put_async.assert_not_called()


# ---------------------------------------------------------------------------
# Change-detection helpers (sync, called from meta worker thread)
# ---------------------------------------------------------------------------

def test_entity_row_changed_returns_true_when_no_row(listener, mock_psycopg_conn):
    """No existing row → always changed (first write for this entity_id)."""
    _conn, cur = mock_psycopg_conn
    cur.fetchone.return_value = None

    params = (
        "sensor.temp",   # [0]  entity_id
        "uuid-001",      # [1]  ha_entity_uuid
        "Temperature",   # [2]  name
        "sensor",        # [3]  domain
        "zha",           # [4]  platform
        None,            # [5]  device_id
        None,            # [6]  area_id
        [],              # [7]  labels
        "temperature",   # [8]  device_class
        "°C",            # [9]  unit_of_measurement
        None,            # [10] disabled_by
        datetime.now(timezone.utc),  # [11] valid_from
        "{}",            # [12] extra
    )
    assert listener._entity_row_changed(cur, "sensor.temp", params) is True


def test_entity_row_changed_returns_false_when_unchanged(listener, mock_psycopg_conn):
    """Identical row data → no change; helper must return False to suppress spurious SCD2 writes."""
    _conn, cur = mock_psycopg_conn
    cur.fetchone.return_value = {
        "name": "Temperature",
        "platform": "zha",
        "device_id": None,
        "area_id": None,
        "labels": [],
        "device_class": "temperature",
        "unit_of_measurement": "°C",
        "disabled_by": None,
        "extra": "{}",
    }

    params = (
        "sensor.temp",   # [0]  entity_id
        "uuid-001",      # [1]  ha_entity_uuid
        "Temperature",   # [2]  name (matches row)
        "sensor",        # [3]  domain
        "zha",           # [4]  platform (matches row)
        None,            # [5]  device_id (matches row)
        None,            # [6]  area_id (matches row)
        [],              # [7]  labels (matches row)
        "temperature",   # [8]  device_class (matches row)
        "°C",            # [9]  unit_of_measurement (matches row)
        None,            # [10] disabled_by (matches row)
        datetime.now(timezone.utc),  # [11] valid_from
        "{}",            # [12] extra (matches row)
    )
    assert listener._entity_row_changed(cur, "sensor.temp", params) is False


def test_entity_row_changed_returns_true_when_name_differs(listener, mock_psycopg_conn):
    """Changed name must trigger a new SCD2 row."""
    _conn, cur = mock_psycopg_conn
    cur.fetchone.return_value = {
        "name": "Old Name",
        "platform": "zha",
        "device_id": None,
        "area_id": None,
        "labels": [],
        "device_class": "temperature",
        "unit_of_measurement": "°C",
        "disabled_by": None,
        "extra": "{}",
    }

    params = (
        "sensor.temp",   # [0]
        "uuid-001",      # [1]
        "New Name",      # [2]  name differs from row
        "sensor",        # [3]
        "zha",           # [4]
        None,            # [5]
        None,            # [6]
        [],              # [7]
        "temperature",   # [8]
        "°C",            # [9]
        None,            # [10]
        datetime.now(timezone.utc),  # [11]
        "{}",            # [12]
    )
    assert listener._entity_row_changed(cur, "sensor.temp", params) is True


def test_device_row_changed_returns_true_when_no_row(listener, mock_psycopg_conn):
    """No existing device row → always changed."""
    _conn, cur = mock_psycopg_conn
    cur.fetchone.return_value = None
    params = (
        "device-001",   # [0] device_id
        "My Device",    # [1] name
        "IKEA",         # [2] manufacturer
        "Model X",      # [3] model
        None,           # [4] area_id
        [],             # [5] labels
        datetime.now(timezone.utc),  # [6] valid_from
        "{}",           # [7] extra
    )
    assert listener._device_row_changed(cur, "device-001", params) is True
