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
from .issues import (
    clear_db_unreachable_issue,
    clear_meta_worker_stalled_issue,
    create_db_unreachable_issue,
    create_meta_worker_stalled_issue,
)
from .retry import retry_until_success

if TYPE_CHECKING:
    from .persistent_queue import PersistentQueue
    from .registry_listener import RegistryListener

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
        registry_listener: "RegistryListener",
        stop_event: threading.Event,
    ) -> None:
        super().__init__(daemon=True, name="timescaledb_meta_worker")
        self._hass = hass
        self._dsn = dsn
        self._meta_queue = meta_queue
        self._registry_listener = registry_listener
        self._stop_event = stop_event
        self._conn: psycopg.Connection | None = None

        # D-06-b: watchdog-readable post-mortem context. Safe defaults in __init__
        # so watchdog can read these even if run() never executes. (MEDIUM-8)
        # meta_worker has no mode state machine, so mode is always None.
        self._last_exception: Exception | None = None
        self._last_context: dict = {
            "at": None,
            "mode": None,       # meta_worker has no mode state machine
            "retry_attempt": None,
            "last_op": "unknown",
        }
        # Updated before each major operation so watchdog context is meaningful.
        self._last_op: str = "unknown"
        self._last_retry_attempt: int | None = None

        # retry_until_success is applied to the bound method at __init__ time so
        # on_transient / notify_stall can reference self. D-07 wiring.
        # Phase 3: extended with on_recovery and on_sustained_fail (D-03, D-11).
        self._write_item = retry_until_success(
            stop_event=stop_event,
            on_transient=self.reset_db_connection,
            notify_stall=self._stall_hook,
            on_recovery=self._recovery_hook,
            on_sustained_fail=self._sustained_fail_hook,
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

    # ------------------------------------------------------------------
    # Phase 3 hook methods (D-02, D-03, D-11)
    # ------------------------------------------------------------------

    def _stall_hook(self, attempts: int) -> None:
        """Retry-decorator stall hook.

        D-02 / D-07-f: on STALL_THRESHOLD consecutive failures, fire BOTH:
          - persistent_notification (kept from Phase 2 for continuity)
          - meta_worker_stalled repair issue (Phase 3 add)
        Both bridged via hass.add_job (thread-safe).
        """
        _LOGGER.warning(
            "meta worker stalled after %d attempts — notifying + raising repair issue",
            attempts,
        )
        # Import lazily to avoid module-load-time HA import cost.
        from homeassistant.components import persistent_notification

        # Keep the Phase 2 notification (unchanged body).
        self._hass.add_job(
            persistent_notification.async_create,
            self._hass,
            f"TimescaleDB meta worker has failed {attempts} times in a row. "
            "The integration will keep retrying. Restart HA if the issue persists.",
            "TimescaleDB Recorder",
            "timescaledb_recorder_meta_stalled",
        )
        # New Phase 3 repair issue — auto-clears on recovery.
        self._hass.add_job(create_meta_worker_stalled_issue, self._hass)

    def _recovery_hook(self) -> None:
        """Retry-decorator recovery hook.

        D-02-c / D-03-a: fires exactly once on first success after a stall.
        Clears BOTH repair issues that may have been raised during the streak:
          - meta_worker_stalled (always raised on stall — D-02)
          - db_unreachable (raised if streak also exceeded 300s — D-11)
        Cleared unconditionally — ir.async_delete_issue is a no-op if the
        issue was not present.
        """
        _LOGGER.info("meta worker recovered after stall — clearing repair issues")
        self._hass.add_job(clear_meta_worker_stalled_issue, self._hass)
        self._hass.add_job(clear_db_unreachable_issue, self._hass)

    def _sustained_fail_hook(self) -> None:
        """Retry-decorator sustained-fail hook.

        D-11: fires once when cumulative fail duration crosses
        DB_UNREACHABLE_THRESHOLD_SECONDS (300s default). Raises the
        db_unreachable repair issue; cleared by _recovery_hook on next success.
        """
        _LOGGER.warning(
            "meta worker: DB unreachable for > threshold — raising repair issue",
        )
        self._hass.add_job(create_db_unreachable_issue, self._hass)

    # ------------------------------------------------------------------
    # Main loop (D-05-b, D-05-d)
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Thread entry point.

        Outer try/except catches unhandled bugs (D-06-a). The inner loop
        handles expected exceptions (stop_event sentinel via break).
        Only exceptions outside the retry scope escape to the outer handler.

        The finally block closes the DB connection regardless of success or
        failure. Crucially, failure capture (_last_exception assignment) happens
        in the except block BEFORE finally runs, so a teardown error cannot
        overwrite the original fault. (Cross-AI review 2026-04-23, MEDIUM-8.)
        """
        try:
            self._run_main_loop()
        except Exception as err:  # noqa: BLE001 — last-resort guard (D-06-a)
            _LOGGER.error(
                "%s died with unhandled exception", self.name, exc_info=True,
            )
            # Capture BEFORE finally — teardown errors must not overwrite this.
            self._last_exception = err
            self._last_context = {
                "at": datetime.now(timezone.utc).isoformat(),
                "mode": None,  # meta_worker has no mode machine
                "retry_attempt": self._last_retry_attempt,
                "last_op": self._last_op,
            }
        finally:
            # Connection teardown — runs regardless of success or failure.
            # Errors here are caught and logged, NOT re-raised (D-06 principle).
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001
                    _LOGGER.debug(
                        "%s: error closing connection during teardown", self.name
                    )

    def _run_main_loop(self) -> None:
        """Inner main loop extracted from run() so the outer try/except/finally
        in run() can wrap it cleanly (MEDIUM-8 pattern)."""
        while not self._stop_event.is_set():
            item = self._meta_queue.get()  # blocks on Condition; None on wake
            if item is None or self._stop_event.is_set():
                # D-05-d: wake_consumer() from async_unload_entry unblocks get()
                # returning None. Also re-check stop_event for the sentinel-None case.
                break
            # Update last_op before the retried operation so watchdog context
            # captures the activity even after thread exit (D-06-b).
            self._last_op = "write_item"
            # retry-wrapped; returns None if interrupted by shutdown (D-07-e).
            result = self._write_item(item)
            if self._stop_event.is_set() and result is None:
                # Shutdown interrupted the retry loop — do NOT call task_done().
                # Item stays on disk for replay on next startup (D-03-h).
                break
            self._meta_queue.task_done()
        # Connection is NOT closed here — the finally block in run() handles
        # teardown unconditionally (MEDIUM-8: single teardown path).

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
                        changed = self._registry_listener._entity_row_changed(
                            dict_cur, registry_id, params
                        )
                    if changed:
                        with conn.transaction():
                            cur.execute(SCD2_CLOSE_ENTITY_SQL, (now, registry_id))
                            cur.execute(SCD2_INSERT_ENTITY_SQL, (*params,))

    # ------------------------------------------------------------------
    # Per-registry dispatch — device/area/label (D-05-c). Mechanical copies
    # of _process_entity without the rename path (devices, areas, and labels
    # cannot be renamed in HA — old_id is always None for these registries).
    # ------------------------------------------------------------------

    def _process_device(
        self,
        action: str,
        registry_id: str,
        params: tuple | None,
        now: datetime,
    ) -> None:
        """Execute SCD2 write for a device registry change.

        "create": idempotent snapshot SQL (WHERE NOT EXISTS guard).
        "remove": close the open row.
        "update": change-detection gate via syncer helper; close+insert if changed.
        No rename path — devices cannot be renamed in HA.
        """
        conn = self.get_db_connection()
        with conn.cursor() as cur:
            if action == "create":
                # params[0] = device_id; appears twice per SCD2_SNAPSHOT_DEVICE_SQL.
                cur.execute(SCD2_SNAPSHOT_DEVICE_SQL, (*params, params[0]))
            elif action == "remove":
                cur.execute(SCD2_CLOSE_DEVICE_SQL, (now, registry_id))
            elif action == "update":
                with conn.cursor(row_factory=psycopg.rows.dict_row) as dict_cur:
                    changed = self._registry_listener._device_row_changed(
                        dict_cur, registry_id, params
                    )
                if changed:
                    with conn.transaction():
                        cur.execute(SCD2_CLOSE_DEVICE_SQL, (now, registry_id))
                        cur.execute(SCD2_INSERT_DEVICE_SQL, (*params,))

    def _process_area(
        self,
        action: str,
        registry_id: str,
        params: tuple | None,
        now: datetime,
    ) -> None:
        """Execute SCD2 write for an area registry change.

        "create": idempotent snapshot SQL (WHERE NOT EXISTS guard).
        "remove": close the open row.
        "update": change-detection gate via syncer helper; close+insert if changed.
        No rename path — areas cannot be renamed by entity_id in HA.
        """
        conn = self.get_db_connection()
        with conn.cursor() as cur:
            if action == "create":
                # params[0] = area_id; appears twice per SCD2_SNAPSHOT_AREA_SQL.
                cur.execute(SCD2_SNAPSHOT_AREA_SQL, (*params, params[0]))
            elif action == "remove":
                cur.execute(SCD2_CLOSE_AREA_SQL, (now, registry_id))
            elif action == "update":
                with conn.cursor(row_factory=psycopg.rows.dict_row) as dict_cur:
                    changed = self._registry_listener._area_row_changed(
                        dict_cur, registry_id, params
                    )
                if changed:
                    with conn.transaction():
                        cur.execute(SCD2_CLOSE_AREA_SQL, (now, registry_id))
                        cur.execute(SCD2_INSERT_AREA_SQL, (*params,))

    def _process_label(
        self,
        action: str,
        registry_id: str,
        params: tuple | None,
        now: datetime,
    ) -> None:
        """Execute SCD2 write for a label registry change.

        "create": idempotent snapshot SQL (WHERE NOT EXISTS guard).
        "remove": close the open row.
        "update": change-detection gate via syncer helper; close+insert if changed.
        No rename path — labels cannot be renamed by label_id in HA.
        """
        conn = self.get_db_connection()
        with conn.cursor() as cur:
            if action == "create":
                # params[0] = label_id; appears twice per SCD2_SNAPSHOT_LABEL_SQL.
                cur.execute(SCD2_SNAPSHOT_LABEL_SQL, (*params, params[0]))
            elif action == "remove":
                cur.execute(SCD2_CLOSE_LABEL_SQL, (now, registry_id))
            elif action == "update":
                with conn.cursor(row_factory=psycopg.rows.dict_row) as dict_cur:
                    changed = self._registry_listener._label_row_changed(
                        dict_cur, registry_id, params
                    )
                if changed:
                    with conn.transaction():
                        cur.execute(SCD2_CLOSE_LABEL_SQL, (now, registry_id))
                        cur.execute(SCD2_INSERT_LABEL_SQL, (*params,))
