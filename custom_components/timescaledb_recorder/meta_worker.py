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
        "enqueued_at": str,            # ISO utc, stamped in the event callback.
                                       # Load-bearing for "remove": with no
                                       # params there is no valid_from, so this
                                       # is the close time (see
                                       # _close_timestamp). Informational for
                                       # every other action.
    }
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import psycopg
import psycopg.rows
from psycopg.types.json import Jsonb

from homeassistant.core import HomeAssistant

from .const import (
    DEADLETTER_REASON_INTEGRITY,
    DEADLETTER_REASON_OUT_OF_ORDER,
    METADATA_DEADLETTER_INSERT_SQL,
    SCD2_CLOSE_AREA_SQL,
    SCD2_CLOSE_DEVICE_SQL,
    SCD2_CLOSE_ENTITY_SQL,
    SCD2_CLOSE_LABEL_SQL,
    SCD2_OPEN_VERSION_AT_SQL,
    SCD2_OPEN_VERSION_SQL,
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
    create_metadata_dropped_issue,
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
        # Count of items dropped because they violated the SCD2 invariant. Any
        # non-zero value means metadata history has a hole and warrants a look.
        # In-process only, and reset by a restart — metadata_deadletter is the
        # durable record; these are the cheap in-memory view of it.
        self.integrity_drops: int = 0
        # Count of changes that landed out of order and were therefore skipped
        # rather than spliced into history — updates whose close+insert matched
        # nothing, and removals the ordering guard refused. Should stay 0: the
        # registry listener guarantees queue order. Non-zero means that guarantee
        # broke. Replays of an already-applied item also write nothing, but they
        # lose no history and are excluded — see _note_version_skipped.
        self.out_of_order_skips: int = 0

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
        """Dispatch one item, dropping items the SCD2 invariant refuses.

        Raises on any DB error — retry_until_success handles transient failure.
        Called as self._write_item (the retry-wrapped version) from run().

        Integrity violations are the exception: they are not transient, so
        retrying replays the same conflicting row forever. retry_until_success
        never gives up, so one bad item would wedge every later metadata write
        and silently stop the dimension tables from tracking anything. Dropping
        it keeps ingestion alive; the constraint has already done its job by
        refusing the write.

        Dropping is not forgetting. The item goes to the dead-letter table
        first, so the change survives the restart that erases the counter and
        the log rotation that erases the traceback, and a repair issue puts it
        in front of the operator. A drop nobody can see is the failure mode that
        made issue #17 last for months.
        """
        try:
            self._dispatch_item(item)
        except (psycopg.errors.ExclusionViolation, psycopg.errors.UniqueViolation) as err:
            self.integrity_drops += 1
            _LOGGER.error(
                "%s: SCD2 invariant rejected a metadata write; dropping the item to "
                "keep the queue moving (total dropped: %d). Item: %r",
                self.name, self.integrity_drops, item, exc_info=True,
            )
            self._dead_letter(item, DEADLETTER_REASON_INTEGRITY, str(err))

    def _dead_letter(self, item: dict, reason: str, error: str) -> None:
        """Record an item the worker is about to stop trying to write.

        Best-effort by construction: it runs on a connection that has just
        raised, so the insert gets its own transaction and its own failure
        handling. If even this fails the log line above is all that is left —
        which is where this whole path started, so it says so loudly rather
        than pretending the item was captured.
        """
        try:
            conn = self.get_db_connection()
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(
                    METADATA_DEADLETTER_INSERT_SQL,
                    (reason, item.get("registry"), item.get("registry_id"),
                     error, Jsonb(item)),
                )
        except Exception:  # noqa: BLE001 — the caller has already given up on this item
            _LOGGER.error(
                "%s: could not dead-letter the dropped item; it now exists only in "
                "this log. Item: %r", self.name, item, exc_info=True,
            )
        self._hass.add_job(create_metadata_dropped_issue, self._hass)

    def _dispatch_item(self, item: dict) -> None:
        """Route one item to the per-registry SCD2 path."""
        registry = item["registry"]
        action = item["action"]
        registry_id = item["registry_id"]
        old_id = item.get("old_id")
        params = self._rehydrate_params(registry, item.get("params"))
        close_ts = self._close_timestamp(registry, params, item.get("enqueued_at"))

        if registry == "entity":
            self._process_entity(action, registry_id, old_id, params, close_ts)
        elif registry == "device":
            self._process_device(action, registry_id, params, close_ts)
        elif registry == "area":
            self._process_area(action, registry_id, params, close_ts)
        elif registry == "label":
            self._process_label(action, registry_id, params, close_ts)
        else:
            _LOGGER.warning("Unknown registry type in metadata item: %s", registry)

    def _note_version_skipped(self, cur, registry: str, registry_id: str,
                          close_ts: datetime, params: tuple[object, ...] | None) -> None:
        """Warn when a close+insert pair lost a registry change.

        The close carries a `valid_from < new_valid_from` guard and the insert is
        guarded on there being no open row, so an update that arrives after a
        newer version already landed matches neither and is skipped. That is the
        right trade — splicing it in blind is how the intervals got corrupted —
        but a silent skip loses a registry change, and silence is exactly what let
        issue #17 run undetected. Surface it instead.

        A replay writes nothing for the opposite reason: the item was already
        applied, and the guards are what make replaying it a no-op. Replays are
        routine — task_done() runs after the write, so any shutdown that lands
        mid-item leaves it on disk for next startup — so counting them would make
        `out_of_order_skips` non-zero after the first unclean restart and destroy
        its value as a signal.

        Telling the two apart needs both halves of the question. The open row
        must start at exactly this item's valid_from AND carry this item's
        payload. The timestamp alone is not enough: two changes stamped in the
        same microsecond produce a second item whose close and insert both match
        nothing, and an open row does start at its valid_from — so a timestamp-only
        probe calls a genuinely lost change a replay and does not even count it.
        The payload comparison closes that hole, and costs one indexed read on a
        path that only runs when nothing was written.
        """
        if cur.rowcount:
            return
        cur.execute(SCD2_OPEN_VERSION_AT_SQL[registry], (registry_id, close_ts))
        if cur.fetchone() is not None and not self._open_row_differs(
            registry, registry_id, params
        ):
            _LOGGER.debug(
                "%s: %s %s was already applied; replay was a no-op",
                self.name, registry, registry_id,
            )
            return
        self._note_change_lost(registry, registry_id, close_ts,
                               "arrived after a newer version")

    def _open_row_differs(self, registry: str, registry_id: str,
                          params: tuple[object, ...] | None) -> bool:
        """Does the currently-open row describe something other than `params`?

        Delegates to the registry listener's change-detection helpers so the
        comparison is the same one that decides whether a change is worth
        writing at all. Anything else would let the two disagree about what
        "the same version" means.
        """
        if params is None:
            # No payload to compare against, so nothing can prove this is a
            # replay. Treat it as a real skip: over-reporting a lost change is
            # recoverable, under-reporting one is what issue #17 was.
            return True
        comparators = {
            "entity": self._registry_listener._entity_row_changed,
            "device": self._registry_listener._device_row_changed,
            "area": self._registry_listener._area_row_changed,
            "label": self._registry_listener._label_row_changed,
        }
        conn = self.get_db_connection()
        with conn.cursor(row_factory=psycopg.rows.dict_row) as dict_cur:
            return comparators[registry](dict_cur, registry_id, params)

    def _note_removal_skipped(self, cur, registry: str, registry_id: str,
                              close_ts: datetime) -> None:
        """Warn when a "remove" failed to close anything.

        A removal has no replacement row, so it has no insert to fall back on
        and nothing downstream notices that it did nothing. Two cases reach
        here and they are opposites: no open row at all means the removal was
        already applied and this is a replay, while an open row that survived
        means the `valid_from < close_ts` guard refused it — the version starts
        at or after the moment the entity was removed, so the removal is lost
        and the dimension will go on claiming the thing still exists.
        """
        if cur.rowcount:
            return
        cur.execute(SCD2_OPEN_VERSION_SQL[registry], (registry_id,))
        row = cur.fetchone()
        if row is None:
            _LOGGER.debug(
                "%s: %s %s was already closed; replay was a no-op",
                self.name, registry, registry_id,
            )
            return
        self._note_change_lost(
            registry, registry_id, close_ts,
            f"open version starts at {row[0]}, at or after the removal",
        )

    def _note_change_lost(self, registry: str, registry_id: str,
                          close_ts: datetime, why: str) -> None:
        """Count, log and dead-letter a registry change the guards refused.

        Same treatment as an integrity drop: both lose a real change, so both
        must be equally findable afterwards.
        """
        self.out_of_order_skips += 1
        _LOGGER.error(
            "%s: %s %s was skipped to keep the SCD2 intervals consistent — %s "
            "(total skipped: %d). Queue ordering should make this impossible. "
            "Change was stamped %s.",
            self.name, registry, registry_id, why, self.out_of_order_skips, close_ts,
        )
        self._dead_letter(
            {"registry": registry, "registry_id": registry_id,
             "close_ts": close_ts.isoformat(), "why": why},
            DEADLETTER_REASON_OUT_OF_ORDER, why,
        )

    @staticmethod
    def _close_timestamp(
        registry: str, params: tuple | None, enqueued_at: str | None = None
    ) -> datetime:
        """Timestamp used to expire the outgoing version — always event time.

        For a close+insert pair this MUST be the incoming row's valid_from, so the
        two intervals abut exactly and the exclusion constraint's '[)' bounds are
        satisfied. Reading a fresh clock here instead — which is what this worker
        used to do — puts the close after the successor's start and produces one
        overlapping interval per metadata change (issue #17).

        "remove" carries no params, but it does carry `enqueued_at` from the
        callback, and that is when the entity actually disappeared. Dequeue time
        breaks as soon as the queue is delayed: with the database unreachable from
        10:00 to 12:00, a removal at 10:00 followed by a re-creation at 10:05 would
        close the old version at 12:00, which then overlaps the re-created version.
        The constraint rejects that insert and the entity stays marked removed.

        Falls back to the current clock only for an item with no usable event time.
        """
        if params is not None:
            return params[_VALID_FROM_INDEX[registry]]
        if enqueued_at:
            try:
                return datetime.fromisoformat(enqueued_at)
            except (TypeError, ValueError):
                _LOGGER.warning(
                    "%s: unparseable enqueued_at %r; closing at the current time",
                    registry, enqueued_at,
                )
        return datetime.now(timezone.utc)

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
        close_ts: datetime,
    ) -> None:
        """Execute SCD2 write for an entity registry change.

        "create": snapshot SQL with idempotent WHERE NOT EXISTS guard.
        "remove": close the open row; no new row (entry already gone).
        "update" rename (old_id is not None): close old entity_id + insert new, atomically.
        "update" field change (old_id is None): change-detection gate, then close+insert.

        Both update paths close with the incoming valid_from and insert through the
        guarded snapshot statement, which together make a replayed item a no-op.
        """
        conn = self.get_db_connection()
        with conn.cursor() as cur:
            if action == "create":
                # params[0] = entity_id; appears twice per SCD2_SNAPSHOT_ENTITY_SQL.
                cur.execute(SCD2_SNAPSHOT_ENTITY_SQL, (*params, params[0]))
            elif action == "remove":
                cur.execute(SCD2_CLOSE_ENTITY_SQL, (close_ts, registry_id, close_ts))
                self._note_removal_skipped(cur, "entity", registry_id, close_ts)
            elif action == "update":
                if old_id is not None:
                    # Rename path — atomic close of the old id + insert under the new.
                    with conn.transaction():
                        cur.execute(SCD2_CLOSE_ENTITY_SQL, (close_ts, old_id, close_ts))
                        cur.execute(SCD2_SNAPSHOT_ENTITY_SQL, (*params, params[0]))
                        self._note_version_skipped(cur, "entity", registry_id, close_ts, params)
                else:
                    # Field-change path. The change-detection read runs inside the
                    # transaction so it and the close+insert see one snapshot.
                    with conn.transaction():
                        with conn.cursor(row_factory=psycopg.rows.dict_row) as dict_cur:
                            changed = self._registry_listener._entity_row_changed(
                                dict_cur, registry_id, params
                            )
                        if changed:
                            cur.execute(
                                SCD2_CLOSE_ENTITY_SQL, (close_ts, registry_id, close_ts)
                            )
                            cur.execute(SCD2_SNAPSHOT_ENTITY_SQL, (*params, params[0]))
                            self._note_version_skipped(cur, "entity", registry_id, close_ts, params)

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
        close_ts: datetime,
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
                cur.execute(SCD2_CLOSE_DEVICE_SQL, (close_ts, registry_id, close_ts))
                self._note_removal_skipped(cur, "device", registry_id, close_ts)
            elif action == "update":
                with conn.transaction():
                    with conn.cursor(row_factory=psycopg.rows.dict_row) as dict_cur:
                        changed = self._registry_listener._device_row_changed(
                            dict_cur, registry_id, params
                        )
                    if changed:
                        cur.execute(
                            SCD2_CLOSE_DEVICE_SQL, (close_ts, registry_id, close_ts)
                        )
                        cur.execute(SCD2_SNAPSHOT_DEVICE_SQL, (*params, params[0]))
                        self._note_version_skipped(cur, "device", registry_id, close_ts, params)

    def _process_area(
        self,
        action: str,
        registry_id: str,
        params: tuple | None,
        close_ts: datetime,
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
                cur.execute(SCD2_CLOSE_AREA_SQL, (close_ts, registry_id, close_ts))
                self._note_removal_skipped(cur, "area", registry_id, close_ts)
            elif action == "update":
                with conn.transaction():
                    with conn.cursor(row_factory=psycopg.rows.dict_row) as dict_cur:
                        changed = self._registry_listener._area_row_changed(
                            dict_cur, registry_id, params
                        )
                    if changed:
                        cur.execute(
                            SCD2_CLOSE_AREA_SQL, (close_ts, registry_id, close_ts)
                        )
                        cur.execute(SCD2_SNAPSHOT_AREA_SQL, (*params, params[0]))
                        self._note_version_skipped(cur, "area", registry_id, close_ts, params)

    def _process_label(
        self,
        action: str,
        registry_id: str,
        params: tuple | None,
        close_ts: datetime,
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
                cur.execute(SCD2_CLOSE_LABEL_SQL, (close_ts, registry_id, close_ts))
                self._note_removal_skipped(cur, "label", registry_id, close_ts)
            elif action == "update":
                with conn.transaction():
                    with conn.cursor(row_factory=psycopg.rows.dict_row) as dict_cur:
                        changed = self._registry_listener._label_row_changed(
                            dict_cur, registry_id, params
                        )
                    if changed:
                        cur.execute(
                            SCD2_CLOSE_LABEL_SQL, (close_ts, registry_id, close_ts)
                        )
                        cur.execute(SCD2_SNAPSHOT_LABEL_SQL, (*params, params[0]))
                        self._note_version_skipped(cur, "label", registry_id, close_ts, params)
