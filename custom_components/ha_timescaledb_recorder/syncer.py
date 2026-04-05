"""MetadataSyncer: SCD2 dimension table writer for HA registry metadata."""
import json
import logging
from datetime import datetime, timezone
from typing import Callable

import asyncpg
import attrs

from homeassistant.core import HomeAssistant, Event, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import label_registry as lr
from homeassistant.helpers.entity_registry import EVENT_ENTITY_REGISTRY_UPDATED
from homeassistant.helpers.device_registry import EVENT_DEVICE_REGISTRY_UPDATED
from homeassistant.helpers.area_registry import EVENT_AREA_REGISTRY_UPDATED
from homeassistant.helpers.label_registry import EVENT_LABEL_REGISTRY_UPDATED

from .const import (
    SCD2_CLOSE_ENTITY_SQL,
    SCD2_CLOSE_DEVICE_SQL,
    SCD2_CLOSE_AREA_SQL,
    SCD2_CLOSE_LABEL_SQL,
    SCD2_SNAPSHOT_ENTITY_SQL,
    SCD2_SNAPSHOT_DEVICE_SQL,
    SCD2_SNAPSHOT_AREA_SQL,
    SCD2_SNAPSHOT_LABEL_SQL,
    SCD2_INSERT_ENTITY_SQL,
    SCD2_INSERT_DEVICE_SQL,
    SCD2_INSERT_AREA_SQL,
    SCD2_INSERT_LABEL_SQL,
)

_LOGGER = logging.getLogger(__name__)

# Fields explicitly typed on each dimension table — excluded from the extra JSONB column
# to avoid duplication between typed columns and the catch-all extra blob.
_ENTITY_TYPED_KEYS = frozenset({
    "entity_id", "id", "name", "original_name", "domain", "platform",
    "device_id", "area_id", "labels", "device_class", "unit_of_measurement",
    "disabled_by",
})
_DEVICE_TYPED_KEYS = frozenset({
    "id", "name", "manufacturer", "model", "area_id", "labels",
})
_AREA_TYPED_KEYS = frozenset({"id", "name"})
_LABEL_TYPED_KEYS = frozenset({"label_id", "name", "color"})


def _build_extra(entry, exclude_keys: frozenset) -> str:
    """Serialize non-typed registry entry fields to a JSONB string.

    Tries attrs.asdict() first (HA EntityEntry/DeviceEntry use @attr.s), then
    dataclasses.fields() for __slots__ dataclasses (AreaEntry, LabelEntry), then
    falls back to __dict__ as a last resort.  The default=str encoder handles
    HA-specific types (sets, enums, datetime, nested dataclasses).
    """
    try:
        raw = attrs.asdict(entry)
    except attrs.exceptions.NotAnAttrsClassError:
        try:
            import dataclasses
            raw = {f.name: getattr(entry, f.name) for f in dataclasses.fields(entry)}
        except TypeError:
            raw = vars(entry)
    # Also strip underscore-prefixed keys — HA uses these for internal caches
    # (e.g. _cache on EntityEntry) that change without meaningful metadata changes
    # and would produce spurious SCD2 rows if included.
    filtered = {
        k: v for k, v in raw.items()
        if k not in exclude_keys and not k.startswith("_")
    }
    return json.dumps(filtered, default=str)


# Fields present in extra that change on every HA internal registry write without
# reflecting a meaningful metadata change — excluded from SCD2 change detection
# but kept in extra for auditability.
_EXTRA_COMPARE_IGNORE = frozenset({"modified_at"})


def _extra_changed(stored, new_json: str) -> bool:
    """Return True if extra JSONB changed in a way that warrants a new SCD2 row.

    asyncpg returns JSONB columns as strings, so `stored` may be a str or dict.
    Ignores fields in _EXTRA_COMPARE_IGNORE (e.g. modified_at) which HA updates
    on every internal write regardless of whether user-visible metadata changed.
    """
    stored_dict = json.loads(stored) if isinstance(stored, str) else (stored or {})
    new_dict = json.loads(new_json)
    return (
        {k: v for k, v in stored_dict.items() if k not in _EXTRA_COMPARE_IGNORE}
        != {k: v for k, v in new_dict.items() if k not in _EXTRA_COMPARE_IGNORE}
    )


class MetadataSyncer:
    """Capture HA registry metadata into PostgreSQL dimension tables with SCD2 tracking.

    Lifecycle mirrors StateIngester:
    - async_start(): take initial full snapshot + subscribe to four registry events
    - async_stop(): cancel all event subscriptions (no buffer to flush)

    All four registries (entity, device, area, label) are handled.  Incremental
    updates use a close-and-insert SCD2 pattern keyed on the registry-level ID
    (entity_id, device_id, area_id, label_id).
    """

    def __init__(self, hass: HomeAssistant, pool: asyncpg.Pool) -> None:
        self._hass = hass
        self._pool = pool
        self._cancel_listeners: list[Callable] = []
        # Registry references — set in async_start(), used in event handlers
        self._entity_reg = None
        self._device_reg = None
        self._area_reg = None
        self._label_reg = None

    async def async_start(self) -> None:
        """Take initial snapshot and subscribe to registry update events."""
        self._entity_reg = er.async_get(self._hass)
        self._device_reg = dr.async_get(self._hass)
        self._area_reg = ar.async_get(self._hass)
        self._label_reg = lr.async_get(self._hass)

        await self._async_snapshot()

        self._cancel_listeners.append(
            self._hass.bus.async_listen(
                EVENT_ENTITY_REGISTRY_UPDATED, self._handle_entity_registry_updated
            )
        )
        self._cancel_listeners.append(
            self._hass.bus.async_listen(
                EVENT_DEVICE_REGISTRY_UPDATED, self._handle_device_registry_updated
            )
        )
        self._cancel_listeners.append(
            self._hass.bus.async_listen(
                EVENT_AREA_REGISTRY_UPDATED, self._handle_area_registry_updated
            )
        )
        self._cancel_listeners.append(
            self._hass.bus.async_listen(
                EVENT_LABEL_REGISTRY_UPDATED, self._handle_label_registry_updated
            )
        )

    async def _async_snapshot(self) -> None:
        """Write the initial full snapshot of all four registries.

        Uses WHERE NOT EXISTS inserts so re-running on HA restart is a no-op
        for entries that already have an open row (Pitfall 3).
        All rows in a single snapshot share the same valid_from timestamp.
        """
        now = datetime.now(timezone.utc)

        async with self._pool.acquire() as conn:
            for entry in self._entity_reg.entities.values():
                params = self._extract_entity_params(entry, now)
                await conn.execute(SCD2_SNAPSHOT_ENTITY_SQL, *params)

            for entry in self._device_reg.devices.values():
                params = self._extract_device_params(entry, now)
                await conn.execute(SCD2_SNAPSHOT_DEVICE_SQL, *params)

            for entry in self._area_reg.async_list_areas():
                params = self._extract_area_params(entry, now)
                await conn.execute(SCD2_SNAPSHOT_AREA_SQL, *params)

            for entry in self._label_reg.async_list_labels():
                params = self._extract_label_params(entry, now)
                await conn.execute(SCD2_SNAPSHOT_LABEL_SQL, *params)

    # ------------------------------------------------------------------
    # Field extraction helpers
    # ------------------------------------------------------------------

    def _extract_entity_params(self, entry, valid_from: datetime) -> tuple:
        """Extract positional parameters for entity SQL (snapshot + insert).

        Parameter order matches SCD2_SNAPSHOT_ENTITY_SQL / SCD2_INSERT_ENTITY_SQL:
        $1=entity_id, $2=ha_entity_uuid, $3=name, $4=domain, $5=platform,
        $6=device_id, $7=area_id, $8=labels, $9=device_class,
        $10=unit_of_measurement, $11=disabled_by, $12=valid_from, $13=extra
        """
        name = entry.name if entry.name is not None else entry.original_name
        domain = entry.entity_id.split(".")[0]
        # Convert set to list — asyncpg requires list for TEXT[] (Pitfall 5)
        labels = list(entry.labels)
        disabled_by = entry.disabled_by.value if entry.disabled_by is not None else None
        extra = _build_extra(entry, _ENTITY_TYPED_KEYS)
        return (
            entry.entity_id,
            entry.id,
            name,
            domain,
            entry.platform,
            entry.device_id,
            entry.area_id,
            labels,
            entry.device_class,
            entry.unit_of_measurement,
            disabled_by,
            valid_from,
            extra,
        )

    def _extract_device_params(self, entry, valid_from: datetime) -> tuple:
        """Extract positional parameters for device SQL.

        $1=device_id, $2=name, $3=manufacturer, $4=model,
        $5=area_id, $6=labels, $7=valid_from, $8=extra
        """
        labels = list(entry.labels)
        extra = _build_extra(entry, _DEVICE_TYPED_KEYS)
        return (
            entry.id,
            entry.name,
            entry.manufacturer,
            entry.model,
            entry.area_id,
            labels,
            valid_from,
            extra,
        )

    def _extract_area_params(self, entry, valid_from: datetime) -> tuple:
        """Extract positional parameters for area SQL.

        $1=area_id, $2=name, $3=valid_from, $4=extra
        """
        extra = _build_extra(entry, _AREA_TYPED_KEYS)
        return (entry.id, entry.name, valid_from, extra)

    def _extract_label_params(self, entry, valid_from: datetime) -> tuple:
        """Extract positional parameters for label SQL.

        $1=label_id, $2=name, $3=color, $4=valid_from, $5=extra
        """
        extra = _build_extra(entry, _LABEL_TYPED_KEYS)
        return (entry.label_id, entry.name, entry.color, valid_from, extra)

    # ------------------------------------------------------------------
    # Change detection helpers
    # ------------------------------------------------------------------

    async def _entity_row_changed(self, conn, entity_id: str, new_params: tuple) -> bool:
        """Return True if the current open row differs from new_params.

        Fetches typed columns from the current open row and compares field-by-field.
        A no-op event (e.g. HA invalidating _cache) returns False — skip SCD2 write.
        new_params indices: 0=entity_id, 1=ha_entity_uuid, 2=name, 3=domain,
        4=platform, 5=device_id, 6=area_id, 7=labels, 8=device_class,
        9=unit_of_measurement, 10=disabled_by, 11=valid_from, 12=extra
        """
        row = await conn.fetchrow(
            "SELECT name, platform, device_id, area_id, labels, device_class,"
            " unit_of_measurement, disabled_by, extra"
            " FROM dim_entities WHERE entity_id = $1 AND valid_to IS NULL",
            entity_id,
        )
        if row is None:
            return True
        return (
            row["name"] != new_params[2]
            or row["platform"] != new_params[4]
            or row["device_id"] != new_params[5]
            or row["area_id"] != new_params[6]
            or sorted(row["labels"] or []) != sorted(new_params[7] or [])
            or row["device_class"] != new_params[8]
            or row["unit_of_measurement"] != new_params[9]
            or row["disabled_by"] != new_params[10]
            or _extra_changed(row["extra"], new_params[12])
        )

    async def _device_row_changed(self, conn, device_id: str, new_params: tuple) -> bool:
        """Return True if the current open device row differs from new_params.

        new_params indices: 0=device_id, 1=name, 2=manufacturer, 3=model,
        4=area_id, 5=labels, 6=valid_from, 7=extra
        """
        row = await conn.fetchrow(
            "SELECT name, manufacturer, model, area_id, labels, extra"
            " FROM dim_devices WHERE device_id = $1 AND valid_to IS NULL",
            device_id,
        )
        if row is None:
            return True
        return (
            row["name"] != new_params[1]
            or row["manufacturer"] != new_params[2]
            or row["model"] != new_params[3]
            or row["area_id"] != new_params[4]
            or sorted(row["labels"] or []) != sorted(new_params[5] or [])
            or _extra_changed(row["extra"], new_params[7])
        )

    async def _area_row_changed(self, conn, area_id: str, new_params: tuple) -> bool:
        """Return True if the current open area row differs from new_params.

        new_params indices: 0=area_id, 1=name, 2=valid_from, 3=extra
        """
        row = await conn.fetchrow(
            "SELECT name, extra FROM dim_areas WHERE area_id = $1 AND valid_to IS NULL",
            area_id,
        )
        if row is None:
            return True
        return (
            row["name"] != new_params[1]
            or _extra_changed(row["extra"], new_params[3])
        )

    async def _label_row_changed(self, conn, label_id: str, new_params: tuple) -> bool:
        """Return True if the current open label row differs from new_params.

        new_params indices: 0=label_id, 1=name, 2=color, 3=valid_from, 4=extra
        """
        row = await conn.fetchrow(
            "SELECT name, color, extra FROM dim_labels WHERE label_id = $1 AND valid_to IS NULL",
            label_id,
        )
        if row is None:
            return True
        return (
            row["name"] != new_params[1]
            or row["color"] != new_params[2]
            or _extra_changed(row["extra"], new_params[4])
        )

    # ------------------------------------------------------------------
    # Entity registry event handling
    # ------------------------------------------------------------------

    @callback
    def _handle_entity_registry_updated(self, event: Event) -> None:
        """Synchronous callback — schedules async processing on the event loop."""
        action = event.data["action"]
        entity_id = event.data["entity_id"]
        # old_entity_id is present only on renames (Pitfall 2)
        old_entity_id = event.data.get("old_entity_id")
        self._hass.async_create_task(
            self._async_process_entity_event(action, entity_id, old_entity_id)
        )

    async def _async_process_entity_event(
        self, action: str, entity_id: str, old_entity_id: str | None
    ) -> None:
        """Apply SCD2 write for an entity registry event."""
        now = datetime.now(timezone.utc)
        try:
            async with self._pool.acquire() as conn:
                if action == "create":
                    entry = self._entity_reg.async_get(entity_id)
                    params = self._extract_entity_params(entry, now)
                    await conn.execute(SCD2_INSERT_ENTITY_SQL, *params)

                elif action == "remove":
                    # Do NOT fetch from registry — entry is already gone (Pitfall 4)
                    await conn.execute(SCD2_CLOSE_ENTITY_SQL, now, entity_id)

                elif action == "update":
                    entry = self._entity_reg.async_get(entity_id)
                    params = self._extract_entity_params(entry, now)
                    if old_entity_id is not None:
                        # Rename: always write — old and new entity_id are different rows (D-09)
                        await conn.execute(SCD2_CLOSE_ENTITY_SQL, now, old_entity_id)
                        await conn.execute(SCD2_INSERT_ENTITY_SQL, *params)
                    elif await self._entity_row_changed(conn, entity_id, params):
                        # Field change: close current row, insert updated row
                        await conn.execute(SCD2_CLOSE_ENTITY_SQL, now, entity_id)
                        await conn.execute(SCD2_INSERT_ENTITY_SQL, *params)
                    else:
                        _LOGGER.debug(
                            "Skipping SCD2 write for entity %s — no meaningful field change",
                            entity_id,
                        )

        except (asyncpg.PostgresConnectionError, OSError):
            self._pool.expire_connections()
            _LOGGER.warning(
                "Connection error processing entity registry event for %s", entity_id
            )

    # ------------------------------------------------------------------
    # Device registry event handling
    # ------------------------------------------------------------------

    @callback
    def _handle_device_registry_updated(self, event: Event) -> None:
        """Synchronous callback — schedules async processing on the event loop."""
        action = event.data["action"]
        device_id = event.data["device_id"]
        self._hass.async_create_task(
            self._async_process_device_event(action, device_id)
        )

    async def _async_process_device_event(self, action: str, device_id: str) -> None:
        """Apply SCD2 write for a device registry event."""
        now = datetime.now(timezone.utc)
        try:
            async with self._pool.acquire() as conn:
                if action == "create":
                    entry = self._device_reg.async_get(device_id)
                    params = self._extract_device_params(entry, now)
                    await conn.execute(SCD2_INSERT_DEVICE_SQL, *params)

                elif action == "remove":
                    await conn.execute(SCD2_CLOSE_DEVICE_SQL, now, device_id)

                elif action == "update":
                    entry = self._device_reg.async_get(device_id)
                    params = self._extract_device_params(entry, now)
                    if await self._device_row_changed(conn, device_id, params):
                        await conn.execute(SCD2_CLOSE_DEVICE_SQL, now, device_id)
                        await conn.execute(SCD2_INSERT_DEVICE_SQL, *params)
                    else:
                        _LOGGER.debug(
                            "Skipping SCD2 write for device %s — no meaningful field change",
                            device_id,
                        )

        except (asyncpg.PostgresConnectionError, OSError):
            self._pool.expire_connections()
            _LOGGER.warning(
                "Connection error processing device registry event for %s", device_id
            )

    # ------------------------------------------------------------------
    # Area registry event handling
    # ------------------------------------------------------------------

    @callback
    def _handle_area_registry_updated(self, event: Event) -> None:
        """Synchronous callback — schedules async processing on the event loop."""
        action = event.data["action"]
        area_id = event.data.get("area_id")
        self._hass.async_create_task(
            self._async_process_area_event(action, area_id)
        )

    async def _async_process_area_event(self, action: str, area_id: str | None) -> None:
        """Apply SCD2 write for an area registry event.

        Reorder events carry area_id=None (Pitfall 6) and are a no-op —
        they only reorder the UI list, no data change worth tracking.
        """
        if action == "reorder":
            return

        now = datetime.now(timezone.utc)
        try:
            async with self._pool.acquire() as conn:
                if action == "create":
                    entry = self._area_reg.async_get_area(area_id)
                    params = self._extract_area_params(entry, now)
                    await conn.execute(SCD2_INSERT_AREA_SQL, *params)

                elif action == "remove":
                    await conn.execute(SCD2_CLOSE_AREA_SQL, now, area_id)

                elif action == "update":
                    entry = self._area_reg.async_get_area(area_id)
                    params = self._extract_area_params(entry, now)
                    if await self._area_row_changed(conn, area_id, params):
                        await conn.execute(SCD2_CLOSE_AREA_SQL, now, area_id)
                        await conn.execute(SCD2_INSERT_AREA_SQL, *params)
                    else:
                        _LOGGER.debug(
                            "Skipping SCD2 write for area %s — no meaningful field change",
                            area_id,
                        )

        except (asyncpg.PostgresConnectionError, OSError):
            self._pool.expire_connections()
            _LOGGER.warning(
                "Connection error processing area registry event for %s", area_id
            )

    # ------------------------------------------------------------------
    # Label registry event handling
    # ------------------------------------------------------------------

    @callback
    def _handle_label_registry_updated(self, event: Event) -> None:
        """Synchronous callback — schedules async processing on the event loop."""
        action = event.data["action"]
        label_id = event.data["label_id"]
        self._hass.async_create_task(
            self._async_process_label_event(action, label_id)
        )

    async def _async_process_label_event(self, action: str, label_id: str) -> None:
        """Apply SCD2 write for a label registry event."""
        now = datetime.now(timezone.utc)
        try:
            async with self._pool.acquire() as conn:
                if action == "create":
                    entry = self._label_reg.async_get_label(label_id)
                    params = self._extract_label_params(entry, now)
                    await conn.execute(SCD2_INSERT_LABEL_SQL, *params)

                elif action == "remove":
                    await conn.execute(SCD2_CLOSE_LABEL_SQL, now, label_id)

                elif action == "update":
                    entry = self._label_reg.async_get_label(label_id)
                    params = self._extract_label_params(entry, now)
                    if await self._label_row_changed(conn, label_id, params):
                        await conn.execute(SCD2_CLOSE_LABEL_SQL, now, label_id)
                        await conn.execute(SCD2_INSERT_LABEL_SQL, *params)
                    else:
                        _LOGGER.debug(
                            "Skipping SCD2 write for label %s — no meaningful field change",
                            label_id,
                        )

        except (asyncpg.PostgresConnectionError, OSError):
            self._pool.expire_connections()
            _LOGGER.warning(
                "Connection error processing label registry event for %s", label_id
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_stop(self) -> None:
        """Cancel all registry event subscriptions.

        Unlike StateIngester, MetadataSyncer has no write buffer — events are
        processed immediately, so no final flush is needed on shutdown.
        """
        for cancel in self._cancel_listeners:
            cancel()
        self._cancel_listeners.clear()
