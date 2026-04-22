"""TimescaledbMetaRecorderThread: dedicated thread owning metadata (SCD2) writes.

D-15-a: one thread per data type. This thread consumes plain dicts from a
PersistentQueue and dispatches via the Phase 1 SCD2 helpers on the syncer.

Item-dict schema (D-15-c; produced by plan 09 syncer updates):
    {
        "registry": "entity" | "device" | "area" | "label",
        "action":   "create" | "update" | "remove",
        "registry_id": str,            # entity_id | device_id | area_id | label_id
        "old_id": str | None,          # only non-None for entity renames
        "params": list | None,         # JSON-safe; None on remove actions.
                                       # Datetimes serialized as ISO strings;
                                       # this worker rehydrates via fromisoformat
                                       # on the valid_from slot (index is
                                       # registry-specific — see _rehydrate_params).
        "enqueued_at": str,            # ISO utc; informational only
    }
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import psycopg
import psycopg.rows

from homeassistant.core import HomeAssistant

from .const import (
    SCD2_CLOSE_AREA_SQL,
    SCD2_CLOSE_DEVICE_SQL,
    SCD2_CLOSE_ENTITY_SQL,
    SCD2_CLOSE_LABEL_SQL,
    SCD2_INSERT_AREA_SQL,
    SCD2_INSERT_DEVICE_SQL,
    SCD2_INSERT_ENTITY_SQL,
    SCD2_INSERT_LABEL_SQL,
    SCD2_SNAPSHOT_AREA_SQL,
    SCD2_SNAPSHOT_DEVICE_SQL,
    SCD2_SNAPSHOT_ENTITY_SQL,
    SCD2_SNAPSHOT_LABEL_SQL,
)
from .retry import retry_until_success

if TYPE_CHECKING:
    from .persistent_queue import PersistentQueue
    from .syncer import MetadataSyncer

_LOGGER = logging.getLogger(__name__)

# valid_from (ISO-string) slot indices per registry. Must match the
# _extract_*_params tuples emitted by syncer.py (Phase 1 unchanged).
# entity: $12 (0-indexed 11)
# device: $7  (0-indexed 6)
# area:   $3  (0-indexed 2)
# label:  $4  (0-indexed 3)
_VALID_FROM_INDEX = {
    "entity": 11,
    "device": 6,
    "area": 2,
    "label": 3,
}


class TimescaledbMetaRecorderThread(threading.Thread):
    """Daemon thread draining PersistentQueue and dispatching SCD2 writes."""

    def __init__(
        self,
        hass: HomeAssistant,
        dsn: str,
        meta_queue: "PersistentQueue",
        syncer: "MetadataSyncer",
        stop_event: threading.Event,
    ) -> None:
        super().__init__(daemon=True, name="ha_timescaledb_meta_worker")
        self._hass = hass
        self._dsn = dsn
        self._meta_queue = meta_queue
        self._syncer = syncer
        self._stop_event = stop_event
        self._conn: psycopg.Connection | None = None

        # retry_until_success is applied to the bound method at __init__ time so
        # on_transient / notify_stall can reference self. D-07 wiring.
        self._write_item = retry_until_success(
            stop_event=stop_event,
            on_transient=self.reset_db_connection,
            notify_stall=self._notify_stall,
        )(self._write_item_raw)

    # ------------------------------------------------------------------
    # Connection management (D-07-g) — identical shape to states_worker.
    # ------------------------------------------------------------------

    def get_db_connection(self) -> psycopg.Connection:
        """Return the current connection, opening a new one lazily if needed."""
        if self._conn is None:
            self._conn = psycopg.connect(self._dsn, autocommit=True)
        return self._conn

    def reset_db_connection(self) -> None:
        """Close and discard the current connection so get_db_connection reconnects.

        Called by retry_until_success on_transient hook after each DB failure
        (D-07-g). Swallows close errors — the connection may already be dead.
        """
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
        self._conn = None

    def _notify_stall(self, attempts: int) -> None:
        """Fire a persistent_notification when the retry loop has stalled.

        Uses hass.add_job (fire-and-forget) because this is called from the
        worker thread — direct async calls are not safe here (D-07-f, WATCH-02).
        """
        _LOGGER.warning(
            "meta worker stalled after %d attempts — firing persistent_notification",
            attempts,
        )
        from homeassistant.components import persistent_notification

        self._hass.add_job(
            persistent_notification.async_create,
            self._hass,
            f"TimescaleDB meta worker has failed {attempts} times in a row. "
            "The integration will keep retrying. Restart HA if the issue persists.",
            "TimescaleDB Recorder",
            "ha_timescaledb_recorder_meta_stalled",
        )

    # ------------------------------------------------------------------
    # Main loop (D-05-b, D-05-d)
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Drain PersistentQueue until shutdown; close connection on exit."""
        while not self._stop_event.is_set():
            item = self._meta_queue.get()  # blocks on Condition; None on wake
            if item is None or self._stop_event.is_set():
                # D-05-d: wake_consumer() from async_unload_entry unblocks get()
                # returning None. Also re-check stop_event for the sentinel-None case.
                break
            # retry-wrapped; returns None if interrupted by shutdown (D-07-e).
            result = self._write_item(item)
            if self._stop_event.is_set() and result is None:
                # Shutdown interrupted the retry loop — do NOT call task_done().
                # Item stays on disk for replay on next startup (D-03-h).
                break
            self._meta_queue.task_done()
        # Clean up connection regardless of how we exited the loop.
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # Dispatch (D-05-c)
    # ------------------------------------------------------------------

    def _write_item_raw(self, item: dict) -> None:
        """Dispatch one item to the appropriate SCD2 path.

        Raises on any DB error — retry_until_success handles transient failure.
        Called as self._write_item (the retry-wrapped version) from run().
        """
        registry = item["registry"]
        action = item["action"]
        registry_id = item["registry_id"]
        old_id = item.get("old_id")
        params = self._rehydrate_params(registry, item.get("params"))
        now = datetime.now(timezone.utc)

        if registry == "entity":
            self._process_entity(action, registry_id, old_id, params, now)
        elif registry == "device":
            self._process_device(action, registry_id, params, now)
        elif registry == "area":
            self._process_area(action, registry_id, params, now)
        elif registry == "label":
            self._process_label(action, registry_id, params, now)
        else:
            _LOGGER.warning("Unknown registry type in metadata item: %s", registry)

    def _rehydrate_params(self, registry: str, params: list | None) -> tuple | None:
        """Convert JSON-safe params list back to a tuple with datetime in the
        valid_from slot. Returns None if params is None (remove actions).

        The valid_from slot is serialized as an ISO-format string by the syncer
        (D-15-c). We rehydrate it via fromisoformat before passing to SQL so
        psycopg3 sees a proper datetime object, not a bare string.
        """
        if params is None:
            return None
        idx = _VALID_FROM_INDEX[registry]
        rehydrated = list(params)
        vf = rehydrated[idx]
        if isinstance(vf, str):
            rehydrated[idx] = datetime.fromisoformat(vf)
        return tuple(rehydrated)

    # ------------------------------------------------------------------
    # Per-registry dispatch — entity (D-05-c). Port Phase 1 worker.py:256-294
    # verbatim with s/cmd.*/params|registry_id|old_id/g.
    # ------------------------------------------------------------------

    def _process_entity(
        self,
        action: str,
        registry_id: str,
        old_id: str | None,
        params: tuple | None,
        now: datetime,
    ) -> None:
        """Execute SCD2 write for an entity registry change.

        "create": snapshot SQL with idempotent WHERE NOT EXISTS guard.
        "remove": close the open row; no new row (entry already gone).
        "update" rename (old_id is not None): close old entity_id + insert new, atomically.
        "update" field change (old_id is None): change-detection gate, then close+insert.
        """
        conn = self.get_db_connection()
        with conn.cursor() as cur:
            if action == "create":
                # params[0] = entity_id; appears twice per SCD2_SNAPSHOT_ENTITY_SQL.
                cur.execute(SCD2_SNAPSHOT_ENTITY_SQL, (*params, params[0]))
            elif action == "remove":
                cur.execute(SCD2_CLOSE_ENTITY_SQL, (now, registry_id))
            elif action == "update":
                if old_id is not None:
                    # Rename path — atomic close+insert.
                    with conn.transaction():
                        cur.execute(SCD2_CLOSE_ENTITY_SQL, (now, old_id))
                        cur.execute(SCD2_INSERT_ENTITY_SQL, (*params,))
                else:
                    # Field-change path — change-detection gate via syncer helper.
                    with conn.cursor(row_factory=psycopg.rows.dict_row) as dict_cur:
                        changed = self._syncer._entity_row_changed(
                            dict_cur, registry_id, params
                        )
                    if changed:
                        with conn.transaction():
                            cur.execute(SCD2_CLOSE_ENTITY_SQL, (now, registry_id))
                            cur.execute(SCD2_INSERT_ENTITY_SQL, (*params,))

    # ------------------------------------------------------------------
    # Device/area/label dispatches ADDED in Task 2.
    # ------------------------------------------------------------------

    def _process_device(
        self,
        action: str,
        registry_id: str,
        params: tuple | None,
        now: datetime,
    ) -> None:
        raise NotImplementedError("Implemented in task 2")

    def _process_area(
        self,
        action: str,
        registry_id: str,
        params: tuple | None,
        now: datetime,
    ) -> None:
        raise NotImplementedError("Implemented in task 2")

    def _process_label(
        self,
        action: str,
        registry_id: str,
        params: tuple | None,
        now: datetime,
    ) -> None:
        raise NotImplementedError("Implemented in task 2")
