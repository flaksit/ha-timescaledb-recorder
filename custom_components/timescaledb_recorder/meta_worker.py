"""TimescaledbMetaRecorderThread: dedicated thread owning metadata (SCD2) writes.

D-15-a: one thread per data type. This thread consumes plain dicts from a
PersistentQueue and dispatches via the Phase 1 SCD2 helpers on the syncer.

Item-dict schema (D-15-c; produced by plan 09 syncer updates):
    {
        "registry": "entity" | "device" | "area" | "label",
        "action":   "create" | "update" | "remove" | "snapshot",
                                       # "snapshot": startup registry read; same
                                       # reconcile semantics as create/update.
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

import json
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
    DEADLETTER_REASON_POISON,
    METADATA_DEADLETTER_INSERT_SQL,
    SCD2_CLOSE_AREA_SQL,
    SCD2_CLOSE_DEVICE_SQL,
    SCD2_CLOSE_ENTITY_SQL,
    SCD2_CLOSE_LABEL_SQL,
    SCD2_ANY_VERSION_SQL,
    SCD2_OPEN_VERSION_AT_SQL,
    SCD2_OPEN_VERSION_SQL,
    SCD2_SNAPSHOT_AREA_SQL,
    SCD2_SNAPSHOT_DEVICE_SQL,
    SCD2_SNAPSHOT_ENTITY_SQL,
    SCD2_SNAPSHOT_LABEL_SQL,
)
from .db import DbCallTracker, connect
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

# (close, guarded insert) per registry.
_SCD2_SQL = {
    "entity": (SCD2_CLOSE_ENTITY_SQL, SCD2_SNAPSHOT_ENTITY_SQL),
    "device": (SCD2_CLOSE_DEVICE_SQL, SCD2_SNAPSHOT_DEVICE_SQL),
    "area": (SCD2_CLOSE_AREA_SQL, SCD2_SNAPSHOT_AREA_SQL),
    "label": (SCD2_CLOSE_LABEL_SQL, SCD2_SNAPSHOT_LABEL_SQL),
}

_RECONCILE_ACTIONS = frozenset({"create", "update", "snapshot"})

# How often a schema wait re-checks stop_event (it cannot wait on both events).
_SCHEMA_WAIT_POLL_S = 1.0


class TimescaledbMetaRecorderThread(threading.Thread):
    """Daemon thread draining PersistentQueue and dispatching SCD2 writes."""

    def __init__(
        self,
        hass: HomeAssistant,
        dsn: str,
        meta_queue: "PersistentQueue",
        registry_listener: "RegistryListener",
        stop_event: threading.Event,
        schema_ready: threading.Event | None = None,
    ) -> None:
        super().__init__(daemon=True, name="timescaledb_meta_worker")
        self._hass = hass
        self._dsn = dsn
        self._meta_queue = meta_queue
        self._registry_listener = registry_listener
        self._stop_event = stop_event
        # Set by the states worker once the schema exists. None: no wait.
        self._schema_ready = schema_ready
        self._conn: psycopg.Connection | None = None
        # Watchdog-readable hang detection (issue #14).
        self.db_calls = DbCallTracker()

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
        # Items given up on after repeated non-transient errors (issue #20).
        # Same durability story as integrity_drops.
        self.poison_drops: int = 0

        # retry_until_success is applied to the bound method at __init__ time so
        # on_transient / notify_stall can reference self. D-07 wiring.
        # Phase 3: extended with on_recovery and on_sustained_fail (D-03, D-11).
        # on_poison dead-letters an item that keeps failing non-transiently; it
        # returns None, so run() task_done()s it like a success (issue #20).
        # No circuit breaker as in the states worker: the startup snapshot
        # rewrites every live registry entry, so an environment fault that
        # dead-letters items costs at most intermediate versions and removals.
        self._write_item = retry_until_success(
            stop_event=stop_event,
            on_transient=self.reset_db_connection,
            notify_stall=self._stall_hook,
            on_recovery=self._recovery_hook,
            on_sustained_fail=self._sustained_fail_hook,
            on_poison=self._drop_poison_item,
        )(self.db_calls.track(self._write_item_raw))

    # ------------------------------------------------------------------
    # Connection management (D-07-g) — identical shape to states_worker.
    # ------------------------------------------------------------------

    def get_db_connection(self) -> psycopg.Connection:
        """Return the current connection, opening one lazily via db.connect."""
        if self._conn is None:
            self._conn = connect(self._dsn)
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
        if not self._wait_for_schema():
            return
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

    def _wait_for_schema(self) -> bool:
        """Block until the states worker has created the schema; False on shutdown.

        On a fresh install the tables, metadata_deadletter included, do not
        exist until then, and every item would fail as poison.
        """
        if self._schema_ready is None or self._schema_ready.is_set():
            return True
        self._last_op = "wait_for_schema"
        _LOGGER.debug("%s: waiting for the states worker to set up the schema", self.name)
        while not self._schema_ready.wait(timeout=_SCHEMA_WAIT_POLL_S):
            if self._stop_event.is_set():
                return False
        return not self._stop_event.is_set()

    # ------------------------------------------------------------------
    # Dispatch (D-05-c)
    # ------------------------------------------------------------------

    def _write_item_raw(self, item: dict) -> None:
        """Dispatch one item, dropping items the SCD2 invariant refuses.

        Raises on any other error. Called as self._write_item (the
        retry-wrapped version) from run(): transient errors are retried, and an
        item that keeps failing otherwise ends up in _drop_poison_item.

        Integrity violations are caught here rather than left to that generic
        path: they are known to be permanent, so retrying them only delays the
        queue by the poison backoff, and they keep their own dead-letter reason
        and counter. Dropping it keeps ingestion alive; the constraint has
        already done its job by refusing the write.

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

    def _drop_poison_item(self, exc: Exception, item: dict) -> None:
        """retry_until_success on_poison hook: give up on an item that cannot be written."""
        self.poison_drops += 1
        _LOGGER.error(
            "%s: metadata write kept failing with a non-transient error; dropping "
            "the item to keep the queue moving (total dropped: %d). Item: %r",
            self.name, self.poison_drops, item, exc_info=exc,
        )
        self._dead_letter(item, DEADLETTER_REASON_POISON, repr(exc))

    def _dead_letter(self, item: dict, reason: str, error: str) -> None:
        """Record an item the worker is about to stop trying to write.

        Best-effort by construction: it runs on a connection that has just
        raised, so the insert gets its own transaction and its own failure
        handling. If even this fails the log line above is all that is left —
        which is where this whole path started, so it says so loudly rather
        than pretending the item was captured.
        """
        try:
            with self.db_calls.call("dead_letter"):
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
        if registry not in _SCD2_SQL:
            _LOGGER.warning("Unknown registry type in metadata item: %s", registry)
            return
        registry_id = item["registry_id"]
        old_id = item.get("old_id")
        params = self._rehydrate_params(registry, item.get("params"))
        close_ts = self._close_timestamp(registry, params, item.get("enqueued_at"))

        if action == "remove":
            self._process_remove(registry, registry_id, close_ts)
        elif action == "update" and old_id is not None:
            self._process_rename(registry, registry_id, old_id, params, close_ts)
        elif action in _RECONCILE_ACTIONS:
            self._process_reconcile(registry, registry_id, params, close_ts)
        else:
            _LOGGER.warning("Unknown action in metadata item: %s", action)

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
        conn = self.get_db_connection()
        with conn.cursor(row_factory=psycopg.rows.dict_row) as dict_cur:
            return self._row_changed(registry, dict_cur, registry_id, params)

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
    # SCD2 writes (D-05-c). One shape for all four registries; only entities
    # can be renamed, so only entity items ever carry an old_id.
    # ------------------------------------------------------------------

    def _row_changed(self, registry: str, dict_cur, registry_id: str, params: tuple) -> bool:
        """The registry listener's change detection — the one definition of "same version"."""
        comparators = {
            "entity": self._registry_listener._entity_row_changed,
            "device": self._registry_listener._device_row_changed,
            "area": self._registry_listener._area_row_changed,
            "label": self._registry_listener._label_row_changed,
        }
        return comparators[registry](dict_cur, registry_id, params)

    def _process_remove(self, registry: str, registry_id: str, close_ts: datetime) -> None:
        """Close the open row; nothing replaces it (the registry entry is gone)."""
        close_sql, _insert_sql = _SCD2_SQL[registry]
        conn = self.get_db_connection()
        with conn.cursor() as cur:
            cur.execute(close_sql, (close_ts, registry_id, close_ts))
            self._note_removal_skipped(cur, registry, registry_id, close_ts)

    def _process_rename(self, registry: str, registry_id: str, old_id: str,
                        params: tuple, close_ts: datetime) -> None:
        """Close the old id and insert under the new one, atomically.

        Always stamped at event time, never created_at: the registry entry
        predates the new id.
        """
        close_sql, insert_sql = _SCD2_SQL[registry]
        conn = self.get_db_connection()
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(close_sql, (close_ts, old_id, close_ts))
            cur.execute(insert_sql, (*params, params[0]))
            self._note_version_skipped(cur, registry, registry_id, close_ts, params)

    def _process_reconcile(self, registry: str, registry_id: str,
                           params: tuple, close_ts: datetime) -> None:
        """Make the open version match `params`: create, update and snapshot.

        No open row → insert; open row equal → nothing; differs → close+insert at
        the item's valid_from. Each is therefore safe whether or not an open row
        exists, which is what lets the listener enqueue from startup on and the
        snapshot repair drift instead of only filling gaps (issue #19).

        The change-detection read runs inside the transaction so it and the
        close+insert see one snapshot.
        """
        close_sql, insert_sql = _SCD2_SQL[registry]
        conn = self.get_db_connection()
        with conn.transaction(), conn.cursor() as cur:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as dict_cur:
                if not self._row_changed(registry, dict_cur, registry_id, params):
                    return
            cur.execute(close_sql, (close_ts, registry_id, close_ts))
            if not cur.rowcount:
                # Nothing closed: possibly the id's first version ever.
                params = self._first_version_params(cur, registry, registry_id, params)
            cur.execute(insert_sql, (*params, params[0]))
            self._note_version_skipped(cur, registry, registry_id, close_ts, params)

    def _first_version_params(self, cur, registry: str, registry_id: str,
                              params: tuple) -> tuple:
        """Start an id's first version at its registry entry's created_at.

        The event, and even more so the startup snapshot, arrives after HA has
        written the entity's first states; a first version stamped then leaves
        those states without metadata (issue #19). Only for an id with no
        version at all: once one exists, earlier history is already described,
        and a gap after a removal is real.

        Never later than the item's own valid_from. created_at is read from the
        `extra` blob (last param), so items persisted by older versions get it too.
        """
        idx = _VALID_FROM_INDEX[registry]
        created_at = _created_at(params[-1])
        if created_at is None or created_at >= params[idx]:
            return params
        args = (registry_id, params[1]) if registry == "entity" else (registry_id,)
        cur.execute(SCD2_ANY_VERSION_SQL[registry], args)
        if cur.fetchone() is not None:
            return params
        return (*params[:idx], created_at, *params[idx + 1:])


def _created_at(extra: object) -> datetime | None:
    """Registry entry's created_at from the serialized extra blob, if usable.

    HA's storage migrations set created_at to the Unix epoch for entries that
    predate the field. That means "unknown", not 1970, so it is ignored.
    """
    try:
        data = json.loads(extra) if isinstance(extra, str) else extra
        raw = data.get("created_at") if isinstance(data, dict) else None
        ts = datetime.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        return None
    if ts is None or ts.tzinfo is None or ts.timestamp() <= 0:
        return None
    return ts

