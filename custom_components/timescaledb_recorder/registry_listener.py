"""RegistryListener: thin HA registry event relay that enqueues JSON-safe dicts for MetaWorker."""
import asyncio
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
    DOMAIN,
    SELECT_ENTITY_CURRENT_SQL,
    SELECT_DEVICE_CURRENT_SQL,
    SELECT_AREA_CURRENT_SQL,
    SELECT_LABEL_CURRENT_SQL,
)
from .persistent_queue import PersistentQueue

_LOGGER = logging.getLogger(__name__)

# Backoff bounds for the drain loop. Short enough that a transient disk error
# costs one registry change no visible delay; capped so a persistent failure
# does not spin.
_DRAIN_RETRY_MIN_S = 1.0
_DRAIN_RETRY_MAX_S = 30.0

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


class RegistryListener:
    """Thin HA registry event relay that enqueues JSON-safe dicts for MetaWorker.

    Lifecycle:
    - async_start(): register event listeners in DISCARD mode — events are dropped
      until enable() is called. This allows subscription before the registry backfill
      completes without risking out-of-order SCD2 writes.
    - enable(): flip from DISCARD to LIVE — called by _async_meta_init after the
      persistent queue drain and initial registry backfill complete.
    - async_stop(): cancel all event subscriptions.

    Design boundary (D-08): registry param extraction MUST happen in @callback context
    (event loop). DB writes and change-detection reads run in the meta worker thread.

    DISCARD mode rationale: the initial registry backfill (_async_initial_registry_backfill)
    snapshots the current registry state via SCD2_SNAPSHOT (WHERE NOT EXISTS, idempotent).
    Any registry change event that fires during the drain+backfill window would be
    processed before the snapshot row exists, causing the SCD2 close/insert to target
    a non-existent row. DISCARD mode eliminates this ordering hazard. The backfill
    captures the registry state at snapshot time; changes after enable() flow normally.

    Event ordering (issue #17): handlers append to an in-memory buffer synchronously
    and a single drain task moves the buffer to the PersistentQueue. Each handler
    previously did hass.async_create_task(queue.put_async(item)), and put_async
    offloads the append with run_in_executor(None, ...) onto the default
    multi-threaded executor — so concurrent appends raced for the queue lock and
    landed in arbitrary order. valid_from is stamped here at event time, so an
    out-of-order append made the worker apply versions in the wrong sequence and
    corrupted the SCD2 intervals. A list append in a @callback cannot be reordered
    (it never awaits), and one drainer means one writer, so queue order is now
    event order. Draining through put_many_async also keeps the single-fsync
    property that issue #11 introduced.
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
        # DISCARD mode: events are dropped until enable() is called. Prevents
        # out-of-order SCD2 writes during the initial drain+backfill window.
        self._enabled: bool = False
        # Ordered hand-off to the meta queue (issue #17). Handlers append here
        # synchronously; _drain_loop is the only reader and the only writer to
        # the PersistentQueue, so event order survives all the way to the worker.
        self._buffer: list[dict] = []
        self._buffer_ready = asyncio.Event()
        self._drain_task: asyncio.Task | None = None

    def bind_meta_queue(self, q: PersistentQueue) -> None:
        """Wire the PersistentQueue after construction. Used by __init__.py
        when the queue must exist before the listener is constructed but is
        still allowed to be assigned post-hoc for test flexibility.
        """
        self._meta_queue = q

    def enable(self) -> None:
        """Flip from DISCARD to LIVE mode. Called by _async_meta_init after
        persistent queue drain and initial registry backfill complete.
        """
        self._enabled = True

    @callback
    def _enqueue(self, item: dict) -> None:
        """Append one item to the ordered buffer and wake the drain task.

        Must stay synchronous. The moment this awaits, two events can interleave
        and the buffer stops recording event order — which is the whole point of
        it existing (see the ordering note in the class docstring).
        """
        self._buffer.append(item)
        self._buffer_ready.set()

    async def _drain_loop(self) -> None:
        """Move buffered items to the PersistentQueue, preserving order.

        Sole writer to the queue from this listener. Takes the whole buffer each
        pass so a burst becomes one put_many_async — one fsync — rather than one
        per event.

        This loop must outlive its own errors. It is the only path from the event
        callbacks to disk, so if it exits, every later registry change piles up in
        memory unpersisted while nothing reports a problem. A failed flush puts the
        batch back and retries with a bounded backoff rather than propagating.
        """
        delay = _DRAIN_RETRY_MIN_S
        while True:
            try:
                await self._buffer_ready.wait()
                await self._flush_buffer()
                delay = _DRAIN_RETRY_MIN_S
            except asyncio.CancelledError:
                # async_stop cancels us; it flushes whatever is left afterwards.
                raise
            except Exception:  # noqa: BLE001
                # _flush_buffer has already restored the batch and logged.
                await asyncio.sleep(delay)
                delay = min(delay * 2, _DRAIN_RETRY_MAX_S)

    async def _flush_buffer(self) -> None:
        """Hand the current buffer contents to the queue in one batch."""
        # Swap before awaiting: put_many_async yields, and events arriving during
        # that window must land in the next batch, not be dropped with this one.
        batch = self._buffer
        self._buffer = []
        self._buffer_ready.clear()
        if not batch:
            return
        try:
            await self._meta_queue.put_many_async(batch)
        except BaseException:
            # BaseException, not Exception: a CancelledError raised while awaiting
            # the executor would otherwise strand this batch in a local variable,
            # belonging to neither the buffer nor the queue. Put it back in front
            # so ordering holds and async_stop's final flush can still see it.
            self._buffer = batch + self._buffer
            self._buffer_ready.set()
            _LOGGER.exception(
                "Failed to enqueue %d registry item(s); retrying", len(batch))
            raise

    async def async_start(self) -> None:
        """Cache registry references and register event listeners in DISCARD mode.

        Listeners are registered immediately so subscription starts as early as
        possible, but events are silently dropped until enable() is called.
        This matches the startup flow described in RegistryListener class docstring.
        """
        self._entity_reg = er.async_get(self._hass)
        self._device_reg = dr.async_get(self._hass)
        self._area_reg = ar.async_get(self._hass)
        self._label_reg = lr.async_get(self._hass)

        # Start the drainer before subscribing, so no event can be buffered
        # without something running to move it to the queue.
        self._drain_task = self._hass.async_create_background_task(
            self._drain_loop(), f"{DOMAIN}_registry_drain"
        )

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
    # Change detection helpers (sync, called from meta worker thread)
    # ------------------------------------------------------------------

    def _entity_row_changed(
        self, cur: psycopg.Cursor, entity_id: str, new_params: tuple
    ) -> bool:
        """Return True if the current open entity row differs from new_params.

        Receives a dict_row cursor from meta worker — row["name"] access is valid.
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
        if not self._enabled:
            return
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
        self._enqueue(item)

    # ------------------------------------------------------------------
    # Device registry event handling
    # ------------------------------------------------------------------

    @callback
    def _handle_device_registry_updated(self, event: Event) -> None:
        """Extract device registry params and enqueue to PersistentQueue."""
        if not self._enabled:
            return
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
        self._enqueue(item)

    # ------------------------------------------------------------------
    # Area registry event handling
    # ------------------------------------------------------------------

    @callback
    def _handle_area_registry_updated(self, event: Event) -> None:
        """Extract area registry params and enqueue to PersistentQueue.

        Reorder events (action="reorder") are skipped — these only affect UI ordering,
        contain no data change, and have area_id=None which would produce a corrupt item.
        """
        if not self._enabled:
            return
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
        self._enqueue(item)

    # ------------------------------------------------------------------
    # Label registry event handling
    # ------------------------------------------------------------------

    @callback
    def _handle_label_registry_updated(self, event: Event) -> None:
        """Extract label registry params and enqueue to PersistentQueue."""
        if not self._enabled:
            return
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
        self._enqueue(item)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_stop(self) -> None:
        """Cancel subscriptions, then flush anything still buffered.

        Order matters: unsubscribe first so no new events arrive, then stop the
        drainer, then flush what is left. Skipping the final flush would silently
        drop registry changes that arrived in the last drain interval, leaving
        permanent gaps in SCD2 history.
        """
        for cancel in self._cancel_listeners:
            cancel()
        self._cancel_listeners.clear()

        if self._drain_task is not None:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass
            self._drain_task = None

        try:
            await self._flush_buffer()
        except Exception:  # noqa: BLE001
            # Already logged in _flush_buffer. Unload must not fail on this —
            # the items stay in the buffer and are lost with the listener, which
            # is strictly better than blocking HA shutdown.
            _LOGGER.error("Dropped %d buffered registry item(s) on shutdown", len(self._buffer))
