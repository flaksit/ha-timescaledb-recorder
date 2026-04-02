"""Shared test fixtures for ha_timescaledb_recorder tests."""
from unittest.mock import AsyncMock, MagicMock
import pytest


@pytest.fixture
def mock_conn():
    """Return a mock asyncpg connection."""
    conn = AsyncMock()
    return conn


@pytest.fixture
def mock_pool(mock_conn):
    """Return a mock asyncpg pool whose acquire() context manager yields mock_conn."""
    pool = MagicMock()
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=mock_conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=cm)
    return pool


@pytest.fixture
def hass():
    """Return a shared mock hass instance.

    Placed in conftest so it can be used by both test_ingester.py (which also
    defines a local hass fixture) and test_syncer.py.  The local fixture in
    test_ingester.py takes precedence inside that module via pytest's usual
    fixture-scope rules.
    """
    h = MagicMock()
    h.async_create_task = MagicMock()
    h.bus = MagicMock()
    # Return a callable cancel function that tests can inspect
    h.bus.async_listen = MagicMock(return_value=MagicMock())
    return h


# ---------------------------------------------------------------------------
# Registry mock fixtures (used by test_syncer.py, Plan 02)
# ---------------------------------------------------------------------------

def _make_registry_entry(**attrs):
    """Return a MagicMock pre-populated with the given attributes."""
    entry = MagicMock()
    for key, value in attrs.items():
        setattr(entry, key, value)
    return entry


@pytest.fixture
def mock_entity_registry():
    """Return a mock HA entity registry with two example RegistryEntry objects.

    Attributes match the fields extracted by MetadataSyncer (Pattern 4 in RESEARCH.md):
    entity_id, id (ha_entity_uuid), name, original_name, domain, platform,
    device_id, area_id, labels (set), device_class, unit_of_measurement, disabled_by.
    """
    entries = [
        _make_registry_entry(
            entity_id="sensor.living_room_temp",
            id="uuid-entity-001",
            name="Living Room Temperature",
            original_name="Temperature",
            domain="sensor",
            platform="zha",
            device_id="device-001",
            area_id="area-living-room",
            labels={"indoor", "climate"},
            device_class="temperature",
            unit_of_measurement="°C",
            disabled_by=None,
        ),
        _make_registry_entry(
            entity_id="switch.plug_kettle_power",
            id="uuid-entity-002",
            name=None,
            original_name="Kettle Power",
            domain="switch",
            platform="zha",
            device_id="device-002",
            area_id="area-kitchen",
            labels=set(),
            device_class=None,
            unit_of_measurement=None,
            disabled_by=None,
        ),
    ]

    reg = MagicMock()
    # .entities.values() is the iteration API used for the initial snapshot
    reg.entities = MagicMock()
    reg.entities.values = MagicMock(return_value=iter(entries))
    # async_get returns the matching entry by entity_id (used after update events)
    entry_by_id = {e.entity_id: e for e in entries}
    reg.async_get = MagicMock(side_effect=lambda eid: entry_by_id.get(eid))
    return reg


@pytest.fixture
def mock_device_registry():
    """Return a mock HA device registry with two example DeviceEntry objects."""
    entries = [
        _make_registry_entry(
            id="device-001",
            name="Living Room Sensor",
            manufacturer="IKEA",
            model="TRADFRI motion sensor",
            area_id="area-living-room",
            labels={"zigbee"},
        ),
        _make_registry_entry(
            id="device-002",
            name="Smart Plug Kettle",
            manufacturer="IKEA",
            model="TRADFRI control outlet",
            area_id="area-kitchen",
            labels=set(),
        ),
    ]

    reg = MagicMock()
    reg.devices = MagicMock()
    reg.devices.values = MagicMock(return_value=iter(entries))
    return reg


@pytest.fixture
def mock_area_registry():
    """Return a mock HA area registry with two example AreaEntry objects."""
    entries = [
        _make_registry_entry(id="area-living-room", name="Living Room"),
        _make_registry_entry(id="area-kitchen", name="Kitchen"),
    ]

    reg = MagicMock()
    # Areas use async_list_areas() (not .values())
    reg.async_list_areas = MagicMock(return_value=entries)
    return reg


@pytest.fixture
def mock_label_registry():
    """Return a mock HA label registry with two example LabelEntry objects."""
    entries = [
        _make_registry_entry(label_id="indoor", name="Indoor", color="#00BFFF"),
        _make_registry_entry(label_id="climate", name="Climate", color=None),
    ]

    reg = MagicMock()
    # Labels use async_list_labels() (not .values())
    reg.async_list_labels = MagicMock(return_value=entries)
    return reg
