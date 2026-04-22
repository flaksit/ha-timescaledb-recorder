"""TimescaledbStateRecorderThread: dedicated thread owning the states psycopg3 connection.

Three-mode state machine (D-04):
- MODE_INIT: startup. Triggers a MODE_BACKFILL transition once.
- MODE_BACKFILL: drain backfill_queue until BACKFILL_DONE sentinel. During
  this mode live events continue to pile into live_queue (bounded; overflow
  triggers another backfill cycle on return).
- MODE_LIVE: drain live_queue with adaptive FLUSH_INTERVAL timeout; flushes
  when buffer >= BATCH_FLUSH_SIZE OR last_flush > FLUSH_INTERVAL.

D-04-e fixes a Phase 1 latent bug: queue.get(timeout=5) on a busy HA never
raises queue.Empty (events arrive faster than the timeout), so the flush
never fired except on MetaCommand arrival. Adaptive timeout math guarantees
flush at or below FLUSH_INTERVAL cadence regardless of event pressure.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING

import psycopg
from psycopg.types.json import Jsonb

from homeassistant.core import HomeAssistant

from .backfill import BACKFILL_DONE
from .const import (
    BATCH_FLUSH_SIZE,
    FLUSH_INTERVAL,
    INSERT_CHUNK_SIZE,
    INSERT_SQL,
    SELECT_OPEN_ENTITIES_SQL,
    SELECT_WATERMARK_SQL,
)
from .retry import retry_until_success
from .schema import sync_setup_schema
from .worker import StateRow

if TYPE_CHECKING:
    import asyncio

    from .overflow_queue import OverflowQueue

_LOGGER = logging.getLogger(__name__)

# State-machine modes (D-04-b). String-valued for log readability.
MODE_INIT = "init"
MODE_BACKFILL = "backfill"
MODE_LIVE = "live"


class TimescaledbStateRecorderThread(threading.Thread):
    """Daemon OS thread owning all state-row psycopg3 writes.

    D-15-a: one thread per data type. This class handles only StateRow; meta
    events flow through meta_worker.TimescaledbMetaRecorderThread.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        dsn: str,
        live_queue: "OverflowQueue",
        backfill_queue: queue.Queue,
        backfill_request: "asyncio.Event",
        stop_event: threading.Event,
        *,
        chunk_interval_days: int,
        compress_after_hours: int,
    ) -> None:
        super().__init__(daemon=True, name="ha_timescaledb_states_worker")
        self._hass = hass
        self._dsn = dsn
        self._live_queue = live_queue
        self._backfill_queue = backfill_queue
        self._backfill_request = backfill_request
        self._stop_event = stop_event
        self._chunk_interval_days = chunk_interval_days
        self._compress_after_hours = compress_after_hours
        self._conn: psycopg.Connection | None = None

        # D-07: wrap _insert_chunk_raw at __init__ so retry hooks bind to `self`.
        # Wrapping at class-body time would fix `self` too early.
        self._insert_chunk = retry_until_success(
            stop_event=stop_event,
            on_transient=self.reset_db_connection,
            notify_stall=self._notify_stall,
        )(self._insert_chunk_raw)

    # ------------------------------------------------------------------
    # Connection management (D-07-g)
    # ------------------------------------------------------------------

    def get_db_connection(self) -> psycopg.Connection:
        """Lazily open the psycopg3 connection. Reconnect handled by the
        retry decorator via reset_db_connection → next call reopens."""
        if self._conn is None:
            self._conn = psycopg.connect(self._dsn, autocommit=True)
        return self._conn

    def reset_db_connection(self) -> None:
        """Drop the current connection handle so get_db_connection reconnects."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
        self._conn = None

    def _notify_stall(self, attempts: int) -> None:
        """Retry-decorator hook. Fires a persistent_notification via the event
        loop (hass.add_job is the thread-safe bridge per worker.py comment).
        """
        _LOGGER.warning(
            "states worker stalled after %d attempts — firing persistent_notification",
            attempts,
        )
        # Import lazily to avoid module-load-time HA import cost and keep
        # notifications orthogonal from the class's test surface.
        from homeassistant.components import persistent_notification

        self._hass.add_job(
            persistent_notification.async_create,
            self._hass,
            f"TimescaleDB states worker has failed {attempts} times in a row. "
            "The integration will keep retrying. Restart HA if the issue persists.",
            "TimescaleDB Recorder",
            "ha_timescaledb_recorder_states_stalled",
        )

    # ------------------------------------------------------------------
    # Main loop (D-04)
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Thread entry point."""
        # Ensure schema (idempotent; D-15-d carries Phase 1 behaviour). Retry
        # via decorator would be overkill here — startup race with DB outage
        # is handled by the main loop (continues without schema; next flush
        # cycle retries writes, which will fail until DDL succeeds on
        # reconnect). Log-and-continue preserves WATCH-02 non-blocking shape.
        try:
            conn = self.get_db_connection()
            sync_setup_schema(
                conn,
                chunk_interval_days=self._chunk_interval_days,
                compress_after_hours=self._compress_after_hours,
            )
        except Exception as err:  # noqa: BLE001 — broad by design
            _LOGGER.warning(
                "Schema setup failed at states worker startup (will retry on next "
                "flush): %s", err,
            )
            self.reset_db_connection()

        mode = MODE_INIT
        buffer: list[StateRow] = []
        last_flush = time.monotonic()

        while not self._stop_event.is_set():
            # D-04-c: init/overflow → request backfill, switch to MODE_BACKFILL.
            if mode == MODE_INIT or (mode == MODE_LIVE and self._live_queue.overflowed):
                if buffer:
                    self._flush(buffer)
                    buffer.clear()
                # asyncio.Event.set is NOT thread-safe — cross the boundary.
                self._hass.loop.call_soon_threadsafe(self._backfill_request.set)
                mode = MODE_BACKFILL
                last_flush = time.monotonic()
                continue

            if mode == MODE_BACKFILL:
                try:
                    item = self._backfill_queue.get(timeout=5.0)
                except queue.Empty:
                    continue  # D-04-f: timeout allows stop_event check
                if item is BACKFILL_DONE:
                    if buffer:
                        self._flush(buffer)
                        buffer.clear()
                    mode = MODE_LIVE
                    last_flush = time.monotonic()
                    continue
                # item is dict[entity_id, list[HA State]] (D-04-d)
                rows = self._slice_to_rows(item)
                buffer.extend(rows)
                self._flush(buffer)
                buffer.clear()
                continue

            # MODE_LIVE: adaptive-timeout flushing (D-04-e fix)
            now = time.monotonic()
            remaining = max(0.1, FLUSH_INTERVAL - (now - last_flush))
            try:
                item = self._live_queue.get(timeout=remaining)
            except queue.Empty:
                if buffer:
                    self._flush(buffer)
                    buffer.clear()
                last_flush = time.monotonic()
                continue
            if isinstance(item, StateRow):
                buffer.append(item)
            if (
                len(buffer) >= BATCH_FLUSH_SIZE
                or time.monotonic() - last_flush >= FLUSH_INTERVAL
            ):
                self._flush(buffer)
                buffer.clear()
                last_flush = time.monotonic()

        # Shutdown: one final flush (best-effort), then close connection.
        if buffer and self._conn is not None:
            try:
                self._flush(buffer)
            except Exception:  # noqa: BLE001
                _LOGGER.warning("Final flush failed at shutdown; %d rows lost", len(buffer))
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # Flush / chunk (D-06)
    # ------------------------------------------------------------------

    def _flush(self, rows: list[StateRow]) -> None:
        """Chunk the buffer into INSERT_CHUNK_SIZE slices; retry per chunk (D-06-a/b)."""
        for i in range(0, len(rows), INSERT_CHUNK_SIZE):
            self._insert_chunk(rows[i : i + INSERT_CHUNK_SIZE])

    def _insert_chunk_raw(self, chunk: list[StateRow]) -> None:
        """Raw insert. Wrapped by retry_until_success in __init__ (D-06-b).

        INSERT_SQL carries ON CONFLICT (last_updated, entity_id) DO NOTHING
        per D-06-d (requires D-09-a unique index).
        """
        params = [
            (r.entity_id, r.state, Jsonb(r.attributes), r.last_updated, r.last_changed)
            for r in chunk
        ]
        conn = self.get_db_connection()
        with conn.cursor() as cur:
            cur.executemany(INSERT_SQL, params)

    def _slice_to_rows(self, slice_dict: dict) -> list[StateRow]:
        """Merge per-entity lists, sort by last_updated, convert to StateRow (D-04-d)."""
        all_states: list = []
        for states in slice_dict.values():
            all_states.extend(states)
        all_states.sort(key=lambda s: s.last_updated)
        return [StateRow.from_ha_state(s) for s in all_states]

    # ------------------------------------------------------------------
    # Orchestrator-facing reads (D-08-d, D-08-f)
    # ------------------------------------------------------------------

    def read_watermark(self) -> datetime | None:
        """Return MAX(last_updated) from ha_states or None if empty.

        Called from the event loop via hass.async_add_executor_job while the
        orchestrator is triggering a backfill — this runs in the default
        executor, not in the worker thread. A side-effecting read through the
        worker's connection is acceptable because the worker's loop waits on
        queue.get() with a 5s timeout and psycopg3 connections allow reads
        from a different thread sequentially (no concurrent cursor use).
        """
        conn = self.get_db_connection()
        with conn.cursor() as cur:
            cur.execute(SELECT_WATERMARK_SQL)
            row = cur.fetchone()
        return row[0] if row and row[0] is not None else None

    def read_open_entities(self) -> set[str]:
        """Return entity_ids with open rows (valid_to IS NULL) in dim_entities."""
        conn = self.get_db_connection()
        with conn.cursor() as cur:
            cur.execute(SELECT_OPEN_ENTITIES_SQL)
            return {row[0] for row in cur.fetchall()}
