"""RegistryListener: thin HA registry event relay that enqueues JSON-safe dicts for MetaWorker."""
import asyncio
import json
import logging
from datetime import datetime, timezone
from itertools import chain
from typing import Any, Callable

import attrs
import psycopg
import psycopg.rows

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
from .canonical import canonical, canonical_labels
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

# compat_aliases: HA keeps it only to write the pre-1.22 `aliases` storage key;
# `aliases` carries the same data and more.
_ENTITY_EXTRA_EXCLUDED_KEYS = _ENTITY_TYPED_KEYS | {"compat_aliases"}


def _build_extra(entry, exclude_keys: frozenset[str]) -> str:
    """Serialize non-typed registry entry fields to a JSONB string.

    Tries attrs.asdict() first (HA EntityEntry/DeviceEntry use @attr.s), then
    dataclasses.fields() for __slots__ dataclasses (AreaEntry, LabelEntry), then
    falls back to __dict__ as a last resort. Sets become sorted lists;
    _json_default handles the remaining HA types (enums, datetime, nested
    dataclasses).
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
    return json.dumps(canonical(filtered), default=_json_default)


def _json_default(value: object) -> object:
    """JSON form of a value json cannot encode natively.

    COMPUTED_NAME (an alias entry meaning "the entity's computed name") becomes
    null, as in HA's own storage and websocket API; str() would store its repr.
    """
    return None if value is er.COMPUTED_NAME else str(value)


# Fields present in extra that change on every HA internal registry write without
# reflecting a meaningful metadata change — excluded from SCD2 change detection
# but kept in extra for auditability.
_EXTRA_COMPARE_IGNORE = frozenset({"modified_at"})


def _extra_changed(stored, new_json: str) -> bool:
    """Return True if extra JSONB changed in a way that warrants a new SCD2 row.

    `stored` may be a str or dict[str, Any] (DB drivers may return JSONB columns as either).
    Ignores fields in _EXTRA_COMPARE_IGNORE (e.g. modified_at) which HA updates
    on every internal write regardless of whether user-visible metadata changed.
    Stored rows are canonical (migrations.canonicalize_dimension_rows), so the
    parsed objects compare directly.
    """
    stored_dict = json.loads(stored) if isinstance(stored, str) else (stored or {})
    new_dict = json.loads(new_json)
    return _without_ignored(stored_dict) != _without_ignored(new_dict)


def _without_ignored(extra: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in extra.items() if k not in _EXTRA_COMPARE_IGNORE}


def _to_json_safe(params: tuple[Any, ...] | list[Any] | None) -> list[Any] | None:
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
    - async_start(): register event listeners; every event is enqueued from here on.
    - enqueue_snapshot(): called once HA has started, to append the startup
      registry snapshot and removal sweeps to the same ordered stream.
    - async_stop(): cancel all event subscriptions and flush.

    Design boundary (D-08): registry param extraction MUST happen in @callback context
    (event loop). DB writes and change-detection reads run in the meta worker thread.

    Startup (issue #19): there is no window in which events are dropped. Every
    item the worker applies reconciles — insert when no open row exists, nothing
    when the open row already matches, close+insert when it differs — and a
    remove with no open row closes nothing. So an event processed before the
    snapshot is safe, and the snapshot repairs whatever drifted while HA was not
    running. A change made while HA was down is therefore stamped with the
    snapshot time, not the time it happened. So is a removal while HA was down:
    the sweep after the snapshot closes every open row whose id the registry no
    longer has.

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
        self._cancel_listeners: list[Callable[[], None]] = []
        # Registry references — assigned in async_start(), used in event handlers.
        # Declared without a None default so handlers need no narrowing; nothing
        # reads them before async_start().
        self._entity_reg: er.EntityRegistry
        self._device_reg: dr.DeviceRegistry
        self._area_reg: ar.AreaRegistry
        self._label_reg: lr.LabelRegistry
        # Ordered hand-off to the meta queue (issue #17). Handlers append here
        # synchronously; _drain_loop is the only reader and the only writer to
        # the PersistentQueue, so event order survives all the way to the worker.
        self._buffer: list[dict[str, Any]] = []
        self._buffer_ready = asyncio.Event()
        self._drain_task: asyncio.Task[None] | None = None
        # (append task, its batch) while an enqueue is in flight, so a cancelled
        # flush can be settled rather than guessed at.
        self._inflight: tuple[asyncio.Task[None], list[dict[str, Any]]] | None = None

    @property
    def _queue(self) -> PersistentQueue:
        if self._meta_queue is None:
            raise AssertionError("meta queue used before bind_meta_queue()")
        return self._meta_queue

    def bind_meta_queue(self, q: PersistentQueue) -> None:
        """Wire the PersistentQueue after construction. Used by __init__.py
        when the queue must exist before the listener is constructed but is
        still allowed to be assigned post-hoc for test flexibility.
        """
        self._meta_queue = q

    @callback
    def enqueue_snapshot(self) -> None:
        """Append one "snapshot" item per registry entry, then one "sweep" per registry.

        Read, stamped and buffered in one synchronous step, so every event item
        already buffered is stamped earlier and every later one later: the
        worker sees them in timestamp order. Going straight to the queue instead
        would race the drain task and could put an earlier event behind the
        snapshot, where the ordering guard would refuse it as out of order.

        A sweep carries the registry's live ids; the worker closes every open
        row outside them, i.e. entries removed while HA was not running.

        Area → label → entity → device: referenced rows land before referencing ones.
        """
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        sources = (
            ("area", self._area_reg.async_list_areas(), self._extract_area_params,
             lambda e: e.id),
            ("label", self._label_reg.async_list_labels(), self._extract_label_params,
             lambda e: e.label_id),
            ("entity", self._entity_reg.entities.values(), self._extract_entity_params,
             lambda e: e.entity_id),
            # Child devices are a separate collection, but share device ids and
            # events with main devices; leaving them out would sweep them closed.
            ("device", chain(self._device_reg.devices, self._device_reg.child_devices),
             self._extract_device_params, lambda e: e.id),
        )
        items: list[dict[str, Any]] = []
        sweeps: list[dict[str, Any]] = []
        for registry, entries, extract, key in sources:
            entries = list(entries)
            items.extend(
                {
                    "registry": registry,
                    "action": "snapshot",
                    "registry_id": key(entry),
                    "old_id": None,
                    "params": _to_json_safe(extract(entry, now)),
                    "enqueued_at": now_iso,
                }
                for entry in entries
            )
            sweeps.append({
                "registry": registry,
                "action": "sweep",
                "registry_id": None,
                "old_id": None,
                "params": None,
                "live_ids": sorted({key(entry) for entry in entries}),
                "enqueued_at": now_iso,
            })
        # One buffer append → one put_many_async → one fsync (issue #11).
        self._buffer.extend(items + sweeps)
        self._buffer_ready.set()

    @callback
    def _enqueue(self, item: dict[str, Any]) -> None:
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

        This loop must outlive every ordinary error. It is the only path from the
        event callbacks to disk, so if it exits, every later registry change piles
        up in memory unpersisted while nothing reports a problem. A failed flush
        puts the batch back and retries with a bounded backoff rather than
        propagating.

        The exceptions to that are the exceptions that are not `Exception`:
        cancellation, KeyboardInterrupt, SystemExit. Those say the interpreter or
        the event loop is going away, and retrying in a backoff loop would fight
        the shutdown rather than survive it. _flush_buffer catches `BaseException`
        so it can restore the batch before any of them propagate, then re-raises;
        the batch is safe in _buffer and async_stop's final flush is what gets it
        to disk.
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

        # Shielded, and tracked: cancelling the awaiter does not stop an append
        # already running in the executor. Requeuing on cancellation would then
        # append the same items twice. Instead the in-flight append is recorded
        # so async_stop can await its real outcome and requeue only if it failed.
        append = asyncio.ensure_future(self._queue.put_many_async(batch))
        self._inflight = (append, batch)
        try:
            await asyncio.shield(append)
        except asyncio.CancelledError:
            # Normal shutdown, not a failure — async_stop settles this batch.
            raise
        except BaseException:
            # Clear before requeuing: this batch is now back in _buffer, and
            # leaving it recorded as in-flight would let _settle_inflight requeue
            # the same items a second time if the drainer is cancelled during its
            # backoff, so the final flush would write both copies.
            self._inflight = None
            self._requeue(batch)
            _LOGGER.exception(
                "Failed to enqueue %d registry item(s); retrying", len(batch))
            raise
        else:
            self._inflight = None

    def _requeue(self, batch: list[dict[str, Any]]) -> None:
        """Put an unpersisted batch back at the front, preserving order."""
        self._buffer = batch + self._buffer
        self._buffer_ready.set()

    async def _settle_inflight(self) -> None:
        """Await an append left running by a cancelled flush.

        Only the append itself can say whether the items reached disk, so the
        decision to requeue waits for it. Without this, shutdown either loses the
        batch or writes it twice.
        """
        inflight = self._inflight
        self._inflight = None
        if inflight is None:
            return
        append, batch = inflight
        try:
            await append
        except Exception:  # noqa: BLE001
            self._inflight = None
            self._requeue(batch)
            _LOGGER.exception(
                "Enqueue of %d registry item(s) failed during shutdown", len(batch))

    async def async_start(self) -> None:
        """Cache registry references and register event listeners."""
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

    def _extract_entity_params(self, entry, valid_from: datetime) -> tuple[Any, ...]:
        """Extract positional parameters for entity SQL (snapshot + insert).

        Parameter order matches SCD2_SNAPSHOT_ENTITY_SQL / SCD2_INSERT_ENTITY_SQL:
        $1=entity_id, $2=ha_entity_uuid, $3=name, $4=domain, $5=platform,
        $6=device_id, $7=area_id, $8=labels, $9=device_class,
        $10=unit_of_measurement, $11=disabled_by, $12=valid_from, $13=extra
        """
        name = entry.name if entry.name is not None else entry.original_name
        domain = entry.entity_id.split(".")[0]
        # psycopg3 needs a list for TEXT[]; sorted so the stored array does not
        # depend on PYTHONHASHSEED (the repair SQL compares arrays element-wise).
        labels = canonical_labels(list(entry.labels))
        disabled_by = entry.disabled_by.value if entry.disabled_by is not None else None
        extra = _build_extra(entry, _ENTITY_EXTRA_EXCLUDED_KEYS)
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

    def _extract_device_params(self, entry, valid_from: datetime) -> tuple[Any, ...]:
        """Extract positional parameters for device SQL.

        $1=device_id, $2=name, $3=manufacturer, $4=model,
        $5=area_id, $6=labels, $7=valid_from, $8=extra

        A ChildDeviceEntry has no manufacturer/model; reading them anyway goes
        through HA's deprecation shim, which logs a warning per access.
        """
        labels = canonical_labels(list(entry.labels))
        extra = _build_extra(entry, _DEVICE_TYPED_KEYS)
        is_child = isinstance(entry, dr.ChildDeviceEntry)
        return (
            entry.id,
            entry.name,
            None if is_child else entry.manufacturer,
            None if is_child else entry.model,
            entry.area_id,
            labels,
            valid_from,
            extra,
        )

    def _extract_area_params(self, entry, valid_from: datetime) -> tuple[Any, ...]:
        """Extract positional parameters for area SQL.

        $1=area_id, $2=name, $3=valid_from, $4=extra
        """
        extra = _build_extra(entry, _AREA_TYPED_KEYS)
        return (entry.id, entry.name, valid_from, extra)

    def _extract_label_params(self, entry, valid_from: datetime) -> tuple[Any, ...]:
        """Extract positional parameters for label SQL.

        $1=label_id, $2=name, $3=color, $4=valid_from, $5=extra
        """
        extra = _build_extra(entry, _LABEL_TYPED_KEYS)
        return (entry.label_id, entry.name, entry.color, valid_from, extra)

    # ------------------------------------------------------------------
    # Change detection helpers (sync, called from meta worker thread)
    # ------------------------------------------------------------------

    def _entity_row_changed(
        self, cur: psycopg.Cursor[psycopg.rows.DictRow], entity_id: str, new_params: tuple[Any, ...]
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
        self, cur: psycopg.Cursor[psycopg.rows.DictRow], device_id: str, new_params: tuple[Any, ...]
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
        self, cur: psycopg.Cursor[psycopg.rows.DictRow], area_id: str, new_params: tuple[Any, ...]
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
        self, cur: psycopg.Cursor[psycopg.rows.DictRow], label_id: str, new_params: tuple[Any, ...]
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
    def _handle_entity_registry_updated(self, event: Event[er.EventEntityRegistryUpdatedData]) -> None:
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
        self._enqueue(item)

    # ------------------------------------------------------------------
    # Device registry event handling
    # ------------------------------------------------------------------

    @callback
    def _handle_device_registry_updated(self, event: Event[dr.EventDeviceRegistryUpdatedData]) -> None:
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
        self._enqueue(item)

    # ------------------------------------------------------------------
    # Area registry event handling
    # ------------------------------------------------------------------

    @callback
    def _handle_area_registry_updated(self, event: Event[ar.EventAreaRegistryUpdatedData]) -> None:
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
        if area_id is None:
            raise AssertionError(f"area registry {action} event without area_id")

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
    def _handle_label_registry_updated(self, event: Event[lr.EventLabelRegistryUpdatedData]) -> None:
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

        # Cancelling the drainer does not stop an append already running in the
        # executor. Settle it first: requeue only if it genuinely failed, so the
        # final flush below cannot write the same items a second time.
        await self._settle_inflight()

        try:
            await self._flush_buffer()
        except Exception:  # noqa: BLE001
            # Already logged in _flush_buffer, which put the batch back in
            # _buffer. Try once more synchronously before giving up: the async
            # path failed in the executor, and by now the drainer is gone and
            # nothing else can touch the buffer, so a direct append is safe and
            # a blocking write is affordable on a shutdown path. Only if this
            # also fails is the change genuinely lost.
            self._flush_buffer_blocking()

    def _flush_buffer_blocking(self) -> None:
        """Last-resort synchronous append, for shutdown only.

        Unload must not fail on this — losing the items is bad, blocking HA
        shutdown is worse — so the failure is reported and swallowed.
        """
        batch = self._buffer
        self._buffer = []
        if not batch:
            return
        try:
            self._queue.put_many(batch)
        except Exception:  # noqa: BLE001
            self._buffer = batch
            _LOGGER.exception(
                "Dropped %d buffered registry item(s) on shutdown; the metadata "
                "queue could not be written even synchronously", len(batch),
            )
        else:
            _LOGGER.warning(
                "Persisted %d buffered registry item(s) synchronously after the "
                "shutdown flush failed", len(batch),
            )
