"""Diagnostic sensor entities for the TimescaleDB Recorder integration.

Five polled SensorEntity subclasses exposing internal health state:
- TimescaledbHealthSensor        — overall health rollup (ok/degraded/error), 5s
- TimescaledbDbStatusSensor      — database connectivity (connected/disconnected/stalled), 5s
- TimescaledbQueueDepthSensor    — peak live queue depth since last poll, 10s
- TimescaledbEventsDroppedSensor — delta events dropped since last poll, 10s
- TimescaledbMetaQueueDepthSensor — current metadata queue depth, 30s
"""
from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

if TYPE_CHECKING:
    from . import TimescaledbRecorderConfigEntry

# Polling intervals — issue_registry reads are cheap; queue reads are fast
# attribute accesses so tighter intervals are fine.
SCAN_INTERVAL_FAST = timedelta(seconds=5)
SCAN_INTERVAL_METRICS = timedelta(seconds=10)
SCAN_INTERVAL_META = timedelta(seconds=30)

# Issue IDs that indicate error-level health (mirrors issues.py constants).
# Duplicated here rather than imported to keep sensor logic self-contained and
# avoid coupling the sensor module to internal issue-registry constants.
_ERROR_ISSUES = frozenset({"db_unreachable", "states_worker_stalled", "meta_worker_stalled"})
_DEGRADED_ISSUES = frozenset({"buffer_dropping", "recorder_disabled"})


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    """Return a shared DeviceInfo for all integration entities.

    SERVICE entry_type creates a virtual device that groups all diagnostic
    entities under a single "TimescaleDB Recorder" device card in the HA UI.
    Keyed by entry.entry_id so multiple entries (unusual but possible) each
    get their own device.
    """
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="TimescaleDB Recorder",
        entry_type=DeviceEntryType.SERVICE,
    )


def _active_issue_ids(hass: HomeAssistant) -> frozenset[str]:
    """Return the set of issue_ids currently active for this domain."""
    registry = ir.async_get(hass)
    return frozenset(
        issue.issue_id
        for issue in registry.issues.values()
        if issue.domain == DOMAIN and not issue.is_persistent
        # is_persistent=False means the issue is currently active, not dismissed.
        # We read all issues for our domain and filter by is_persistent to match
        # the "currently active" semantics used by health/db_status rollup.
    )


def _all_domain_issue_ids(hass: HomeAssistant) -> frozenset[str]:
    """Return the set of issue_ids currently raised (not dismissed) for this domain."""
    registry = ir.async_get(hass)
    return frozenset(
        issue.issue_id
        for issue in registry.issues.values()
        if issue.domain == DOMAIN
    )


class TimescaledbHealthSensor(SensorEntity):
    """Overall health rollup: ok / degraded / error.

    error: any error-level issue active, or either worker not alive.
    degraded: any degraded-level issue active (workers alive, DB reachable).
    ok: no active issues and both workers alive.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Health"
    _attr_icon = "mdi:database-check"
    _attr_scan_interval = SCAN_INTERVAL_FAST

    def __init__(self, hass: HomeAssistant, entry: "TimescaledbRecorderConfigEntry") -> None:
        self._hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_health"
        self._attr_device_info = _device_info(entry)

    async def async_update(self) -> None:
        """Poll issue_registry and worker liveness to compute health state."""
        active = _all_domain_issue_ids(self._hass)
        data = self._entry.runtime_data
        self._attr_native_value = self._compute_state(active, data)

    @staticmethod
    def _compute_state(active_issue_ids: frozenset[str], data) -> str:
        states_alive = data.states_worker is not None and data.states_worker.is_alive()
        meta_alive = data.meta_worker is not None and data.meta_worker.is_alive()
        if (active_issue_ids & _ERROR_ISSUES) or not states_alive or not meta_alive:
            return "error"
        if active_issue_ids & _DEGRADED_ISSUES:
            return "degraded"
        return "ok"


class TimescaledbDbStatusSensor(SensorEntity):
    """Database connectivity status: connected / disconnected / stalled."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "DB Status"
    _attr_icon = "mdi:database-sync"
    _attr_scan_interval = SCAN_INTERVAL_FAST

    def __init__(self, hass: HomeAssistant, entry: "TimescaledbRecorderConfigEntry") -> None:
        self._hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_db_status"
        self._attr_device_info = _device_info(entry)

    async def async_update(self) -> None:
        """Poll issue_registry to determine database connectivity status."""
        active = _all_domain_issue_ids(self._hass)
        if "states_worker_stalled" in active:
            self._attr_native_value = "stalled"
        elif "db_unreachable" in active:
            self._attr_native_value = "disconnected"
        else:
            self._attr_native_value = "connected"


class TimescaledbQueueDepthSensor(SensorEntity):
    """Peak live queue depth since the last poll interval.

    Uses get_and_reset_peak() which resets the baseline to the current
    queue depth after each read, so the value reflects the worst-case
    depth within the last 10 s rather than an absolute high-water mark.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Queue Depth Peak"
    _attr_icon = "mdi:buffer"
    _attr_native_unit_of_measurement = "events"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_scan_interval = SCAN_INTERVAL_METRICS

    def __init__(self, entry: "TimescaledbRecorderConfigEntry") -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_queue_depth_peak"
        self._attr_device_info = _device_info(entry)

    async def async_update(self) -> None:
        """Read and reset peak queue depth."""
        self._attr_native_value = self._entry.runtime_data.live_queue.get_and_reset_peak()


class TimescaledbEventsDroppedSensor(SensorEntity):
    """Delta of dropped events since the last poll interval.

    Reads the monotonically-increasing _total_dropped counter and reports
    the delta from the previous reading. Stays at 0 during normal operation.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Events Dropped"
    _attr_icon = "mdi:database-remove"
    _attr_native_unit_of_measurement = "events"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_scan_interval = SCAN_INTERVAL_METRICS

    def __init__(self, entry: "TimescaledbRecorderConfigEntry") -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_events_dropped"
        self._attr_device_info = _device_info(entry)
        # Previous reading of _total_dropped — delta computed on each update.
        self._prev_total_dropped: int = 0

    async def async_update(self) -> None:
        """Compute and report delta drop count since last poll."""
        current = self._entry.runtime_data.live_queue._total_dropped
        delta = current - self._prev_total_dropped
        self._prev_total_dropped = current
        self._attr_native_value = delta


class TimescaledbMetaQueueDepthSensor(SensorEntity):
    """Current number of items in the persistent metadata queue."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Meta Queue Depth"
    _attr_icon = "mdi:database-clock"
    _attr_native_unit_of_measurement = "items"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_scan_interval = SCAN_INTERVAL_META

    def __init__(self, entry: "TimescaledbRecorderConfigEntry") -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_meta_queue_depth"
        self._attr_device_info = _device_info(entry)

    async def async_update(self) -> None:
        """Read current metadata queue depth via __len__."""
        self._attr_native_value = len(self._entry.runtime_data.meta_queue)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: "TimescaledbRecorderConfigEntry",
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Register all sensor entities for this config entry."""
    async_add_entities([
        TimescaledbHealthSensor(hass, entry),
        TimescaledbDbStatusSensor(hass, entry),
        TimescaledbQueueDepthSensor(entry),
        TimescaledbEventsDroppedSensor(entry),
        TimescaledbMetaQueueDepthSensor(entry),
    ])
