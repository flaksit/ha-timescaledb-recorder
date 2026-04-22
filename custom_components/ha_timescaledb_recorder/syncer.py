"""MetadataSyncer: thin HA registry event relay that enqueues JSON-safe dicts for MetaWorker."""
import json
import logging
from datetime import datetime, timezone
from typing import Callable

import attrs
import psycopg

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
    SELECT_ENTITY_CURRENT_SQL,
    SELECT_DEVICE_CURRENT_SQL,
    SELECT_AREA_CURRENT_SQL,
    SELECT_LABEL_CURRENT_SQL,
)
from .persistent_queue import PersistentQueue

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

    `stored` may be a str or dict (DB drivers may return JSONB columns as either).
    Ignores fields in _EXTRA_COMPARE_IGNORE (e.g. modified_at) which HA updates
    on every internal write regardless of whether user-visible metadata changed.
    """
    stored_dict = json.loads(stored) if isinstance(stored, str) else (stored or {})
    new_dict = json.loads(new_json)
    return (
        {k: v for k, v in stored_dict.items() if k not in _EXTRA_COMPARE_IGNORE}
        != {k: v for k, v in new_dict.items() if k not in _EXTRA_COMPARE_IGNORE}
    )


def _to_json_safe(params: tuple | list | None) -> list | None:
    """Convert params tuple to a JSON-serializable list.

    datetime → isoformat() string (meta_worker._rehydrate_params reverses this).
    All other types (str, int, float, list[str], None) pass through unchanged.
    """
    if params is None:
        return None
    return [
        v.isoformat() if isinstance(v, datetime) else v
        for v in params
    ]


class MetadataSyncer:
    """Thin HA registry event relay that enqueues JSON-safe dicts for MetaWorker.

    Lifecycle:
    - async_start(): snapshot all four registries into the queue, then register event listeners
    - async_stop(): cancel all event subscriptions (no buffer to flush — queue is owned by MetaWorker)

    Design boundary (D-08): registry param extraction MUST happen in @callback context (event loop).
    DB writes and change-detection reads run in the meta worker thread.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        meta_queue: PersistentQueue | None = None,
    ) -> None:
        self._hass = hass
        self._meta_queue: PersistentQueue | None = meta_queue
        self._cancel_listeners: list[Callable] = []
        # Registry references — set in async_start(), used in event handlers
        self._entity_reg = None
        self._device_reg = None
        self._area_reg = None
        self._label_reg = None

    def bind_meta_queue(self, q: PersistentQueue) -> None:
        """Wire the PersistentQueue after construction. Used by __init__.py
        when the queue must exist before the syncer is constructed but is
        still allowed to be assigned post-hoc for test flexibility.
        """
        self._meta_queue = q

    async def async_start(self) -> None:
        """Iterate all four registries in the event loop and enqueue snapshot items to PersistentQueue.

        D-04: snapshot items are enqueued BEFORE registering event listeners to ensure
        no registry events are missed and initial snapshot data is queued for the worker.

        Accepted race window (D-04 design decision): there is a small window between the
        snapshot iteration and listener registration where a registry change event could
        be missed. This is an accepted risk per D-04 — the snapshot + listener ordering
        minimises the window but does not eliminate it entirely. Phase 1 accepts this;
        Phase 3 (backfill) can close any gaps via periodic reconciliation if needed.
        """
        self._entity_reg = er.async_get(self._hass)
        self._device_reg = dr.async_get(self._hass)
        self._area_reg = ar.async_get(self._hass)
        self._label_reg = lr.async_get(self._hass)

        now = datetime.now(timezone.utc)

        for entry in self._entity_reg.entities.values():
            params = self._extract_entity_params(entry, now)
            item = {
                "registry": "entity",
                "action": "create",
                "registry_id": entry.entity_id,
                "old_id": None,
                "params": _to_json_safe(params),
                "enqueued_at": now.isoformat(),
            }
            self._hass.async_create_task(self._meta_queue.put_async(item))

        for entry in self._device_reg.devices.values():
            params = self._extract_device_params(entry, now)
            item = {
                "registry": "device",
                "action": "create",
                "registry_id": entry.id,
                "old_id": None,
                "params": _to_json_safe(params),
                "enqueued_at": now.isoformat(),
            }
            self._hass.async_create_task(self._meta_queue.put_async(item))

        for entry in self._area_reg.async_list_areas():
            params = self._extract_area_params(entry, now)
            item = {
                "registry": "area",
                "action": "create",
                "registry_id": entry.id,
                "old_id": None,
                "params": _to_json_safe(params),
                "enqueued_at": now.isoformat(),
            }
            self._hass.async_create_task(self._meta_queue.put_async(item))

        for entry in self._label_reg.async_list_labels():
            params = self._extract_label_params(entry, now)
            item = {
                "registry": "label",
                "action": "create",
                "registry_id": entry.label_id,
                "old_id": None,
                "params": _to_json_safe(params),
                "enqueued_at": now.isoformat(),
            }
            self._hass.async_create_task(self._meta_queue.put_async(item))

        # Register listeners AFTER enqueuing snapshot (D-04: no missed events)
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
        # Convert set to list — psycopg3 requires list for TEXT[] columns
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
    # Change detection helpers (sync, called from worker thread via DbWorker)
    # ------------------------------------------------------------------

    def _entity_row_changed(
        self, cur: psycopg.Cursor, entity_id: str, new_params: tuple
    ) -> bool:
        """Return True if the current open entity row differs from new_params.

        Receives a dict_row cursor from DbWorker — row["name"] access is valid.
        new_params indices: 0=entity_id, 1=ha_entity_uuid, 2=name, 3=domain,
        4=platform, 5=device_id, 6=area_id, 7=labels, 8=device_class,
        9=unit_of_measurement, 10=disabled_by, 11=valid_from, 12=extra
        """
        cur.execute(SELECT_ENTITY_CURRENT_SQL, (entity_id,))
        row = cur.fetchone()
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

    def _device_row_changed(
        self, cur: psycopg.Cursor, device_id: str, new_params: tuple
    ) -> bool:
        """Return True if the current open device row differs from new_params.

        new_params indices: 0=device_id, 1=name, 2=manufacturer, 3=model,
        4=area_id, 5=labels, 6=valid_from, 7=extra
        """
        cur.execute(SELECT_DEVICE_CURRENT_SQL, (device_id,))
        row = cur.fetchone()
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

    def _area_row_changed(
        self, cur: psycopg.Cursor, area_id: str, new_params: tuple
    ) -> bool:
        """Return True if the current open area row differs from new_params.

        new_params indices: 0=area_id, 1=name, 2=valid_from, 3=extra
        """
        cur.execute(SELECT_AREA_CURRENT_SQL, (area_id,))
        row = cur.fetchone()
        if row is None:
            return True
        return (
            row["name"] != new_params[1]
            or _extra_changed(row["extra"], new_params[3])
        )

    def _label_row_changed(
        self, cur: psycopg.Cursor, label_id: str, new_params: tuple
    ) -> bool:
        """Return True if the current open label row differs from new_params.

        new_params indices: 0=label_id, 1=name, 2=color, 3=valid_from, 4=extra
        """
        cur.execute(SELECT_LABEL_CURRENT_SQL, (label_id,))
        row = cur.fetchone()
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
        """Extract entity registry params and enqueue to PersistentQueue (D-08: extract in event loop)."""
        action = event.data["action"]
        entity_id = event.data["entity_id"]
        old_entity_id = event.data.get("old_entity_id")

        if action == "remove":
            # Registry entry is already gone at this point — do NOT call async_get.
            params = None
        else:
            entry = self._entity_reg.async_get(entity_id)
            if entry is None:
                # Race: event fired just before entry was deleted; skip to avoid AttributeError
                # in _extract_entity_params. The SCD2 history gap is acceptable (D-04).
                _LOGGER.warning(
                    "Entity %s not found in registry during %s event; skipping",
                    entity_id, action,
                )
                return
            params = self._extract_entity_params(entry, datetime.now(timezone.utc))

        item = {
            "registry": "entity",
            "action": action,
            "registry_id": entity_id,
            "old_id": old_entity_id,
            "params": _to_json_safe(params),
            "enqueued_at": datetime.now(timezone.utc).isoformat(),
        }
        self._hass.async_create_task(self._meta_queue.put_async(item))

    # ------------------------------------------------------------------
    # Device registry event handling
    # ------------------------------------------------------------------

    @callback
    def _handle_device_registry_updated(self, event: Event) -> None:
        """Extract device registry params and enqueue to PersistentQueue."""
        action = event.data["action"]
        device_id = event.data["device_id"]

        if action == "remove":
            params = None
        else:
            entry = self._device_reg.async_get(device_id)
            if entry is None:
                # Race: event fired just before entry was deleted; skip to avoid AttributeError
                # in _extract_device_params. The SCD2 history gap is acceptable (D-04).
                _LOGGER.warning(
                    "Device %s not found in registry during %s event; skipping",
                    device_id, action,
                )
                return
            params = self._extract_device_params(entry, datetime.now(timezone.utc))

        item = {
            "registry": "device",
            "action": action,
            "registry_id": device_id,
            "old_id": None,
            "params": _to_json_safe(params),
            "enqueued_at": datetime.now(timezone.utc).isoformat(),
        }
        self._hass.async_create_task(self._meta_queue.put_async(item))

    # ------------------------------------------------------------------
    # Area registry event handling
    # ------------------------------------------------------------------

    @callback
    def _handle_area_registry_updated(self, event: Event) -> None:
        """Extract area registry params and enqueue to PersistentQueue.

        Reorder events (action="reorder") are skipped — these only affect UI ordering,
        contain no data change, and have area_id=None which would produce a corrupt item.
        """
        action = event.data["action"]
        if action == "reorder":
            return
        # Bracket access (not .get()) — reorder is already filtered above, so all remaining
        # actions (create, update, remove) must carry area_id. KeyError here is intentional:
        # it surfaces unexpected event shapes immediately rather than silently propagating None,
        # which would cause NULL = NULL in SQL (matches zero rows) and hide the bug.
        area_id = event.data["area_id"]

        if action == "remove":
            params = None
        else:
            entry = self._area_reg.async_get_area(area_id)
            if entry is None:
                # Race: event fired just before entry was deleted; skip to avoid AttributeError
                # in _extract_area_params. The SCD2 history gap is acceptable (D-04).
                _LOGGER.warning(
                    "Area %s not found in registry during %s event; skipping",
                    area_id, action,
                )
                return
            params = self._extract_area_params(entry, datetime.now(timezone.utc))

        item = {
            "registry": "area",
            "action": action,
            "registry_id": area_id,
            "old_id": None,
            "params": _to_json_safe(params),
            "enqueued_at": datetime.now(timezone.utc).isoformat(),
        }
        self._hass.async_create_task(self._meta_queue.put_async(item))

    # ------------------------------------------------------------------
    # Label registry event handling
    # ------------------------------------------------------------------

    @callback
    def _handle_label_registry_updated(self, event: Event) -> None:
        """Extract label registry params and enqueue to PersistentQueue."""
        action = event.data["action"]
        label_id = event.data["label_id"]

        if action == "remove":
            params = None
        else:
            entry = self._label_reg.async_get_label(label_id)
            if entry is None:
                # Race: event fired just before entry was deleted; skip to avoid AttributeError
                # in _extract_label_params. The SCD2 history gap is acceptable (D-04).
                _LOGGER.warning(
                    "Label %s not found in registry during %s event; skipping",
                    label_id, action,
                )
                return
            params = self._extract_label_params(entry, datetime.now(timezone.utc))

        item = {
            "registry": "label",
            "action": action,
            "registry_id": label_id,
            "old_id": None,
            "params": _to_json_safe(params),
            "enqueued_at": datetime.now(timezone.utc).isoformat(),
        }
        self._hass.async_create_task(self._meta_queue.put_async(item))

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
