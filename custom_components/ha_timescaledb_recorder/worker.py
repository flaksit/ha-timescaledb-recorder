"""DbWorker: dedicated OS thread that owns all psycopg3 DB writes for ha_timescaledb_recorder."""
import logging
import queue
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import psycopg
import psycopg.rows
from psycopg.types.json import Jsonb

from homeassistant.core import HomeAssistant

from .const import (
    INSERT_SQL,
    SCD2_CLOSE_ENTITY_SQL, SCD2_CLOSE_DEVICE_SQL,
    SCD2_CLOSE_AREA_SQL, SCD2_CLOSE_LABEL_SQL,
    SCD2_SNAPSHOT_ENTITY_SQL, SCD2_SNAPSHOT_DEVICE_SQL,
    SCD2_SNAPSHOT_AREA_SQL, SCD2_SNAPSHOT_LABEL_SQL,
    SCD2_INSERT_ENTITY_SQL, SCD2_INSERT_DEVICE_SQL,
    SCD2_INSERT_AREA_SQL, SCD2_INSERT_LABEL_SQL,
    SELECT_ENTITY_CURRENT_SQL, SELECT_DEVICE_CURRENT_SQL,
    SELECT_AREA_CURRENT_SQL, SELECT_LABEL_CURRENT_SQL,
)
from .schema import sync_setup_schema

if TYPE_CHECKING:
    from .syncer import MetadataSyncer

_LOGGER = logging.getLogger(__name__)

# Singleton sentinel enqueued by stop() to signal the worker thread to exit cleanly.
# Identity-checked with `is` — never compared by value.
_STOP = object()


@dataclass(frozen=True, slots=True)
class StateRow:
    """Immutable state change payload enqueued from the HA event loop.

    frozen=True: immutable after enqueue — safe to read from worker thread.
    slots=True: reduces per-object memory for high-volume state events (Python 3.10+).
    attributes is kept as a plain dict here; Jsonb() wrapping happens in DbWorker._flush()
    to keep the @callback enqueue site simple and allocation-free.
    """

    entity_id: str
    state: str
    attributes: dict
    last_updated: datetime
    last_changed: datetime


@dataclass(frozen=True, slots=True)
class MetaCommand:
    """Immutable registry change payload enqueued from the HA event loop.

    registry: one of "entity", "device", "area", "label"
    action:   one of "create", "update", "remove"
    registry_id: entity_id / device_id / area_id / label_id (the primary key)
    old_id:   only set on entity renames (old_entity_id); None otherwise
    params:   pre-extracted positional tuple for the SCD2 SQL; None on "remove" actions
              (registry entry is already gone at remove time — D-08)
    """

    registry: str
    action: str
    registry_id: str
    old_id: str | None
    params: tuple | None


class DbWorker:
    """Dedicated OS thread that owns all psycopg3 DB writes.

    Isolation model: all DB I/O is confined to the worker thread. The HA event
    loop never blocks on DB calls. Producers (StateIngester, MetadataSyncer) place
    immutable payloads on the queue from @callback handlers; the worker drains it.

    Thread-safety note: any HA coroutine called from this thread must use
    hass.add_job(coro) (fire-and-forget) — never hass.async_* directly (not
    thread-safe from a non-event-loop thread, per WORK-05 / D-18). Phase 1
    does not call any HA async APIs from the worker thread, but this constraint
    is documented here for future phases.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        dsn: str,
        chunk_interval_days: int,
        compress_after_hours: int,
        syncer: "MetadataSyncer",
    ) -> None:
        self._hass = hass
        self._dsn = dsn
        self._chunk_interval_days = chunk_interval_days
        self._compress_after_hours = compress_after_hours
        self._syncer = syncer
        # queue.Queue (not SimpleQueue) — get(timeout=) needed for 5-second flush interval (BUF-03).
        # SimpleQueue lacks get(timeout=) support and would require a separate threading.Event.
        self._queue: queue.Queue = queue.Queue()
        # daemon=True: if HA exits without async_unload_entry (e.g. SIGKILL), thread dies immediately.
        # In-memory buffer is lost in this case — accepted trade-off. Clean shutdown via async_stop().
        self._thread = threading.Thread(
            target=self.run, daemon=True, name="ha_timescaledb_worker"
        )
        self._conn: psycopg.Connection | None = None

    @property
    def queue(self) -> queue.Queue:
        """Shared queue. Ingester and syncer enqueue payloads here."""
        return self._queue

    def start(self) -> None:
        """Start the worker thread. Returns immediately — all DB work runs in the thread."""
        self._thread.start()

    def run(self) -> None:
        """Worker thread entry point. Owns the full DB lifecycle.

        D-03: DB unreachable at startup → log warning, skip schema, enter flush loop.
        Events buffer in-memory until the next flush cycle; Phase 2 adds reconnect/retry.
        Any HA async API calls from this thread must use hass.add_job(coro) (fire-and-forget)
        — never hass.async_* directly (not thread-safe) per WORK-05 / D-18.
        """
        try:
            self._conn = psycopg.connect(self._dsn, autocommit=True)
        except psycopg.OperationalError as err:
            # D-03: DB unreachable at startup — log warning and continue into flush loop.
            # Events will buffer in the queue; schema + writes attempted on next flush cycle.
            # Worker thread must NOT die here — that would prevent recovery when DB comes back.
            _LOGGER.warning(
                "TimescaleDB unreachable at worker startup: %s — buffering events in memory", err
            )
        else:
            self._setup_schema()

        buffer: list[StateRow] = []
        while True:
            try:
                item = self._queue.get(timeout=5.0)
            except queue.Empty:
                # BUF-03: 5-second flush interval — queue timeout IS the flush timer.
                # Capture count before clear — len(buffer) after clear always yields 0
                # and gives no useful backpressure signal.
                if buffer and self._conn is not None:
                    flushed_count = len(buffer)
                    self._flush(buffer)
                    buffer.clear()
                else:
                    flushed_count = 0
                _LOGGER.debug(
                    "Flush tick — flushed=%d queue_depth=%d",
                    flushed_count,
                    self._queue.qsize(),
                )
                continue

            if item is _STOP:
                # Graceful shutdown: flush any remaining buffered states before exiting.
                if buffer and self._conn is not None:
                    self._flush(buffer)
                break
            elif isinstance(item, StateRow):
                buffer.append(item)
            elif isinstance(item, MetaCommand):
                # Flush pending state rows before processing meta — preserves ordering.
                # Example: entity rename must not overtake the last state for that entity_id.
                if buffer and self._conn is not None:
                    self._flush(buffer)
                    buffer.clear()
                if self._conn is not None:
                    # Per-item exception boundary: one bad MetaCommand must not kill the thread.
                    # Bad commands are logged and skipped; processing continues with the next item.
                    try:
                        self._process_meta_command(item)
                    except Exception as exc:  # noqa: BLE001
                        _LOGGER.error(
                            "Unhandled error processing %s %s command (registry_id=%s); "
                            "command skipped: %s",
                            item.registry,
                            item.action,
                            item.registry_id,
                            exc,
                        )

        if self._conn is not None:
            self._conn.close()

    def _setup_schema(self) -> None:
        """Run idempotent DDL via sync_setup_schema. Called once at worker startup."""
        try:
            sync_setup_schema(
                self._conn,
                chunk_interval_days=self._chunk_interval_days,
                compress_after_hours=self._compress_after_hours,
            )
        except psycopg.OperationalError as err:
            # D-03: schema setup failed — log and continue; retry deferred to Phase 2.
            _LOGGER.warning("Schema setup failed (DB may be unavailable): %s", err)

    def _flush(self, buffer: list[StateRow]) -> None:
        """Batch-insert StateRow items using executemany.

        Wraps dict attributes in Jsonb() per WORK-04 — psycopg3 raises TypeError
        if a raw dict is passed to a JSONB column.
        list[str] labels adapt to TEXT[] automatically (no wrapping needed).
        Logs queue depth so operators can detect if the worker is falling behind.
        """
        rows = [
            (
                row.entity_id,
                row.state,
                Jsonb(row.attributes),  # WORK-04: required wrapper for JSONB column
                row.last_updated,
                row.last_changed,
            )
            for row in buffer
        ]
        try:
            with self._conn.cursor() as cur:
                cur.executemany(INSERT_SQL, rows)
            _LOGGER.debug(
                "Flushed %d state rows (queue_depth=%d)", len(rows), self._queue.qsize()
            )
        except psycopg.OperationalError:
            # Phase 2 adds retry + re-queue; Phase 1 logs and drops to avoid infinite growth.
            _LOGGER.warning(
                "Connection error during flush; %d rows dropped (Phase 2 adds retry)", len(rows)
            )
        except psycopg.DatabaseError as err:
            _LOGGER.error("DB error during flush; %d rows dropped: %s", len(rows), err)

    def _process_meta_command(self, cmd: MetaCommand) -> None:
        """Execute SCD2 write for a registry change. Called from worker thread.

        Raises psycopg errors on DB failure — the caller (run()) catches these
        to log and continue processing subsequent commands.
        """
        now = datetime.now(timezone.utc)
        registry = cmd.registry

        if registry == "entity":
            self._process_entity_command(cmd, now)
        elif registry == "device":
            self._process_device_command(cmd, now)
        elif registry == "area":
            self._process_area_command(cmd, now)
        elif registry == "label":
            self._process_label_command(cmd, now)
        else:
            _LOGGER.warning("Unknown registry type in MetaCommand: %s", registry)

    def _process_entity_command(self, cmd: MetaCommand, now: datetime) -> None:
        """Execute SCD2 write for an entity registry change.

        "create": snapshot SQL with idempotent WHERE NOT EXISTS guard. The snapshot
        SQL has entity_id twice (once in SELECT, once in the subquery) — pass
        (*cmd.params, cmd.params[0]) so both %s slots receive the entity_id.

        "remove": close the open row; no new row inserted (entry already gone — D-08).

        "update" rename: close old entity_id row and insert new row atomically.
        Wrapped in transaction() so a failed insert rolls back the close, preventing
        a "closed with no successor" inconsistency (T-03-08).

        "update" field change: read current row with dict_row cursor, call syncer
        change-detection helper, only close+insert if something actually changed.
        The close+insert pair is also wrapped in transaction() for atomicity.
        """
        with self._conn.cursor() as cur:
            if cmd.action == "create":
                # params[0] = entity_id; must appear twice for the WHERE NOT EXISTS subquery.
                cur.execute(SCD2_SNAPSHOT_ENTITY_SQL, (*cmd.params, cmd.params[0]))
            elif cmd.action == "remove":
                cur.execute(SCD2_CLOSE_ENTITY_SQL, (now, cmd.registry_id))
            elif cmd.action == "update":
                if cmd.old_id is not None:
                    # Rename: close old entity_id, insert new row atomically.
                    with self._conn.transaction():
                        cur.execute(SCD2_CLOSE_ENTITY_SQL, (now, cmd.old_id))
                        cur.execute(SCD2_INSERT_ENTITY_SQL, (*cmd.params,))
                else:
                    # Field change: check if anything actually changed before writing.
                    with self._conn.cursor(row_factory=psycopg.rows.dict_row) as dict_cur:
                        changed = self._syncer._entity_row_changed(
                            dict_cur, cmd.registry_id, cmd.params
                        )
                    if changed:
                        with self._conn.transaction():
                            cur.execute(SCD2_CLOSE_ENTITY_SQL, (now, cmd.registry_id))
                            cur.execute(SCD2_INSERT_ENTITY_SQL, (*cmd.params,))

    def _process_device_command(self, cmd: MetaCommand, now: datetime) -> None:
        """Execute SCD2 write for a device registry change.

        "create": snapshot SQL with idempotent WHERE NOT EXISTS guard. params[0] = device_id;
        must appear twice for the WHERE NOT EXISTS subquery.

        "remove": close the open row.

        "update": check for changes via syncer helper; close+insert atomically if changed.
        """
        with self._conn.cursor() as cur:
            if cmd.action == "create":
                cur.execute(SCD2_SNAPSHOT_DEVICE_SQL, (*cmd.params, cmd.params[0]))
            elif cmd.action == "remove":
                cur.execute(SCD2_CLOSE_DEVICE_SQL, (now, cmd.registry_id))
            elif cmd.action == "update":
                with self._conn.cursor(row_factory=psycopg.rows.dict_row) as dict_cur:
                    changed = self._syncer._device_row_changed(
                        dict_cur, cmd.registry_id, cmd.params
                    )
                if changed:
                    with self._conn.transaction():
                        cur.execute(SCD2_CLOSE_DEVICE_SQL, (now, cmd.registry_id))
                        cur.execute(SCD2_INSERT_DEVICE_SQL, (*cmd.params,))

    def _process_area_command(self, cmd: MetaCommand, now: datetime) -> None:
        """Execute SCD2 write for an area registry change.

        "create": snapshot SQL with idempotent WHERE NOT EXISTS guard. params[0] = area_id;
        must appear twice for the WHERE NOT EXISTS subquery.

        "remove": close the open row.

        "update": check for changes via syncer helper; close+insert atomically if changed.
        """
        with self._conn.cursor() as cur:
            if cmd.action == "create":
                cur.execute(SCD2_SNAPSHOT_AREA_SQL, (*cmd.params, cmd.params[0]))
            elif cmd.action == "remove":
                cur.execute(SCD2_CLOSE_AREA_SQL, (now, cmd.registry_id))
            elif cmd.action == "update":
                with self._conn.cursor(row_factory=psycopg.rows.dict_row) as dict_cur:
                    changed = self._syncer._area_row_changed(
                        dict_cur, cmd.registry_id, cmd.params
                    )
                if changed:
                    with self._conn.transaction():
                        cur.execute(SCD2_CLOSE_AREA_SQL, (now, cmd.registry_id))
                        cur.execute(SCD2_INSERT_AREA_SQL, (*cmd.params,))

    def _process_label_command(self, cmd: MetaCommand, now: datetime) -> None:
        """Execute SCD2 write for a label registry change.

        "create": snapshot SQL with idempotent WHERE NOT EXISTS guard. params[0] = label_id;
        must appear twice for the WHERE NOT EXISTS subquery.

        "remove": close the open row.

        "update": check for changes via syncer helper; close+insert atomically if changed.
        """
        with self._conn.cursor() as cur:
            if cmd.action == "create":
                cur.execute(SCD2_SNAPSHOT_LABEL_SQL, (*cmd.params, cmd.params[0]))
            elif cmd.action == "remove":
                cur.execute(SCD2_CLOSE_LABEL_SQL, (now, cmd.registry_id))
            elif cmd.action == "update":
                with self._conn.cursor(row_factory=psycopg.rows.dict_row) as dict_cur:
                    changed = self._syncer._label_row_changed(
                        dict_cur, cmd.registry_id, cmd.params
                    )
                if changed:
                    with self._conn.transaction():
                        cur.execute(SCD2_CLOSE_LABEL_SQL, (now, cmd.registry_id))
                        cur.execute(SCD2_INSERT_LABEL_SQL, (*cmd.params,))

    def stop(self) -> None:
        """Enqueue the stop sentinel. Thread-safe: called from the event loop."""
        self._queue.put_nowait(_STOP)

    async def async_stop(self) -> None:
        """Graceful shutdown for use in async_unload_entry.

        Puts _STOP sentinel, then awaits thread join via executor job with timeout=30s.
        After join, checks if thread is still alive — logs critical warning if so
        (Phase 3 adds watchdog restart; Phase 1 must at least not hang indefinitely).

        MUST NOT use run_coroutine_threadsafe().result() here —
        deadlocks if the event loop is stopping (WATCH-02 / RESEARCH pitfall).
        """
        self.stop()
        await self._hass.async_add_executor_job(self._thread.join, 30)
        if self._thread.is_alive():
            _LOGGER.critical(
                "DbWorker thread did not exit within 30s after shutdown signal — "
                "thread may be stuck in a DB call. Phase 3 watchdog will address this."
            )
