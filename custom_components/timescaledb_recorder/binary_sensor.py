"""Diagnostic binary sensor entities for the TimescaleDB Recorder integration.

Three push-updated BinarySensorEntity subclasses:
- TimescaledbBufferOverflowSensor  — PROBLEM class, on when live queue is overflowing
- TimescaledbStatesWorkerAliveSensor — CONNECTIVITY class, on when states worker is alive
- TimescaledbMetaWorkerAliveSensor   — CONNECTIVITY class, on when meta worker is alive

All subscribe to HA dispatcher signals in async_added_to_hass and unsubscribe
on entity removal (via the unsubscribe callable returned by async_dispatcher_connect).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_OVERFLOW_CHANGE, SIGNAL_WORKER_STATE_CHANGE

if TYPE_CHECKING:
    from . import TimescaledbRecorderConfigEntry


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    """Return shared DeviceInfo grouping all integration entities under one device."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="TimescaleDB Recorder",
        entry_type=DeviceEntryType.SERVICE,
    )


class TimescaledbBufferOverflowSensor(BinarySensorEntity):
    """Buffer overflow indicator: on when the live queue is dropping events.

    Push-updated via SIGNAL_OVERFLOW_CHANGE dispatched by _overflow_watcher
    in __init__.py on both False→True and True→False transitions so the sensor
    reflects the current state without relying on polling.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Buffer Overflow"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:database-alert"

    def __init__(self, entry: "TimescaledbRecorderConfigEntry") -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_buffer_overflow"
        self._attr_device_info = _device_info(entry)

    async def async_added_to_hass(self) -> None:
        """Subscribe to overflow signal and set initial state."""
        # Set initial state before subscribing so the entity is not stuck in
        # 'unknown' until the next dispatch (runtime_data is guaranteed to be
        # set by the time platforms are set up — async_forward_entry_setups
        # runs after entry.runtime_data = data in async_setup_entry).
        self._attr_is_on = self._entry.runtime_data.live_queue.overflowed

        @callback
        def _handle_overflow_change() -> None:
            self._attr_is_on = self._entry.runtime_data.live_queue.overflowed
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_OVERFLOW_CHANGE, _handle_overflow_change,
            )
        )

    @property
    def is_on(self) -> bool | None:
        return self._attr_is_on


class TimescaledbStatesWorkerAliveSensor(BinarySensorEntity):
    """States worker liveness: on when the worker thread is running.

    Push-updated via SIGNAL_WORKER_STATE_CHANGE dispatched by the watchdog
    before (dead) and after (respawned) _watchdog_respawn executes.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "States Worker"
    # BinarySensorDeviceClass.RUNNING does not exist — CONNECTIVITY is the
    # closest match (on = connected/alive, off = disconnected/dead).
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_icon = "mdi:database-arrow-up"

    def __init__(self, entry: "TimescaledbRecorderConfigEntry") -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_states_worker_alive"
        self._attr_device_info = _device_info(entry)

    async def async_added_to_hass(self) -> None:
        """Subscribe to worker state signal and set initial state."""
        # Guard for None: states_worker is None between async_setup_entry init
        # and the first spawn_states_worker call. In practice the platforms are
        # set up after runtime_data is assigned, so states_worker will be non-None.
        data = self._entry.runtime_data
        self._attr_is_on = (
            data.states_worker is not None and data.states_worker.is_alive()
        )

        @callback
        def _handle_worker_change() -> None:
            d = self._entry.runtime_data
            self._attr_is_on = (
                d.states_worker is not None and d.states_worker.is_alive()
            )
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_WORKER_STATE_CHANGE, _handle_worker_change,
            )
        )

    @property
    def is_on(self) -> bool | None:
        return self._attr_is_on


class TimescaledbMetaWorkerAliveSensor(BinarySensorEntity):
    """Metadata worker liveness: on when the worker thread is running.

    Push-updated via SIGNAL_WORKER_STATE_CHANGE, same signal as states worker.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Metadata Worker"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_icon = "mdi:database-cog"

    def __init__(self, entry: "TimescaledbRecorderConfigEntry") -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_meta_worker_alive"
        self._attr_device_info = _device_info(entry)

    async def async_added_to_hass(self) -> None:
        """Subscribe to worker state signal and set initial state."""
        data = self._entry.runtime_data
        self._attr_is_on = (
            data.meta_worker is not None and data.meta_worker.is_alive()
        )

        @callback
        def _handle_worker_change() -> None:
            d = self._entry.runtime_data
            self._attr_is_on = (
                d.meta_worker is not None and d.meta_worker.is_alive()
            )
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_WORKER_STATE_CHANGE, _handle_worker_change,
            )
        )

    @property
    def is_on(self) -> bool | None:
        return self._attr_is_on


async def async_setup_entry(
    hass: HomeAssistant,
    entry: "TimescaledbRecorderConfigEntry",
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Register all binary sensor entities for this config entry."""
    async_add_entities([
        TimescaledbBufferOverflowSensor(entry),
        TimescaledbStatesWorkerAliveSensor(entry),
        TimescaledbMetaWorkerAliveSensor(entry),
    ])
