"""Unit tests for MetadataSyncer (queue relay + sync change-detection helpers)."""
import queue
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from custom_components.ha_timescaledb_recorder.syncer import MetadataSyncer
from custom_components.ha_timescaledb_recorder.worker import MetaCommand


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def shared_queue():
    """Return a fresh Queue for each test."""
    return queue.Queue()


@pytest.fixture
def syncer(hass, shared_queue):
    """Return a MetadataSyncer already wired to shared_queue via bind_queue()."""
    s = MetadataSyncer(hass=hass)
    s.bind_queue(shared_queue)
    return s


# ---------------------------------------------------------------------------
# bind_queue API
# ---------------------------------------------------------------------------

def test_bind_queue_wires_queue(hass):
    """bind_queue() must store the queue reference on _queue."""
    s = MetadataSyncer(hass=hass)
    assert s._queue is None, "MetadataSyncer constructed without queue arg must have _queue=None"
    q = queue.Queue()
    s.bind_queue(q)
    assert s._queue is q, "bind_queue() must store the exact Queue instance passed"


# ---------------------------------------------------------------------------
# Entity registry callback → MetaCommand enqueue
# ---------------------------------------------------------------------------

def test_entity_callback_enqueues_metacommand(syncer, shared_queue, mock_entity_registry):
    """_handle_entity_registry_updated must enqueue a MetaCommand with correct fields."""
    syncer._entity_reg = mock_entity_registry

    event = MagicMock()
    event.data = {
        "action": "update",
        "entity_id": "sensor.living_room_temp",
        "old_entity_id": None,
    }
    syncer._handle_entity_registry_updated(event)

    assert shared_queue.qsize() == 1
    cmd = shared_queue.get_nowait()
    assert isinstance(cmd, MetaCommand)
    assert cmd.registry == "entity"
    assert cmd.action == "update"
    # Field name is registry_id, NOT entity_id (MetaCommand is registry-agnostic)
    assert cmd.registry_id == "sensor.living_room_temp"
    assert cmd.old_id is None  # Field name is old_id, NOT old_entity_id
    assert cmd.params is not None, "Non-remove action must extract params from registry entry"


def test_entity_remove_enqueues_params_none(syncer, shared_queue):
    """Remove action must enqueue params=None — registry entry is already gone at remove time."""
    syncer._entity_reg = MagicMock()

    event = MagicMock()
    event.data = {
        "action": "remove",
        "entity_id": "sensor.temp",
        "old_entity_id": None,
    }
    syncer._handle_entity_registry_updated(event)

    assert shared_queue.qsize() == 1
    cmd = shared_queue.get_nowait()
    assert cmd.params is None, "Remove action must not fetch params (registry entry already gone)"
    assert cmd.action == "remove"
    # async_get must NOT be called on remove — registry entry is gone (D-08)
    syncer._entity_reg.async_get.assert_not_called()


def test_entity_rename_sets_old_id(syncer, shared_queue, mock_entity_registry):
    """Rename (update with old_entity_id) must set cmd.old_id to the previous entity_id."""
    syncer._entity_reg = mock_entity_registry

    event = MagicMock()
    event.data = {
        "action": "update",
        "entity_id": "sensor.living_room_temp",
        "old_entity_id": "sensor.old_name",
    }
    syncer._handle_entity_registry_updated(event)

    cmd = shared_queue.get_nowait()
    # old_id carries the pre-rename entity_id for the SCD2 close operation
    assert cmd.old_id == "sensor.old_name"
    assert cmd.registry_id == "sensor.living_room_temp"


# ---------------------------------------------------------------------------
# Area registry callback — reorder skip
# ---------------------------------------------------------------------------

def test_area_reorder_skipped(syncer, shared_queue):
    """Reorder events carry no data change and must be silently dropped (Pitfall 6)."""
    event = MagicMock()
    event.data = {"action": "reorder"}
    syncer._handle_area_registry_updated(event)
    assert shared_queue.qsize() == 0, "Reorder action must not enqueue any MetaCommand"


# ---------------------------------------------------------------------------
# Change-detection helpers (sync, called from worker thread via DbWorker)
# ---------------------------------------------------------------------------

def test_entity_row_changed_returns_true_when_no_row(syncer, mock_psycopg_conn):
    """No existing row → always changed (first write for this entity_id)."""
    _conn, cur = mock_psycopg_conn
    cur.fetchone.return_value = None

    # 13-element params tuple: entity_id at [0], name at [2], platform at [4], etc.
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
    assert syncer._entity_row_changed(cur, "sensor.temp", params) is True


def test_entity_row_changed_returns_false_when_unchanged(syncer, mock_psycopg_conn):
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
    assert syncer._entity_row_changed(cur, "sensor.temp", params) is False


def test_entity_row_changed_returns_true_when_name_differs(syncer, mock_psycopg_conn):
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
    assert syncer._entity_row_changed(cur, "sensor.temp", params) is True


def test_device_row_changed_returns_true_when_no_row(syncer, mock_psycopg_conn):
    """No existing device row → always changed."""
    _conn, cur = mock_psycopg_conn
    cur.fetchone.return_value = None
    # 8-element params: device_id at [0], name at [1], etc.
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
    assert syncer._device_row_changed(cur, "device-001", params) is True
