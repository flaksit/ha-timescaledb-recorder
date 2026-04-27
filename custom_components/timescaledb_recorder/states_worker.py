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

import json
import logging
import queue
import threading
import time
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING

import psycopg
from psycopg.types.json import Jsonb

from homeassistant.core import HomeAssistant

from .backfill import BACKFILL_DONE
from .const import (
    BATCH_FLUSH_SIZE,
    FLUSH_INTERVAL,
    INSERT_CHUNK_SIZE,
    INSERT_LIVE_SQL,
    INSERT_SQL,
    SELECT_ALL_KNOWN_ENTITIES_SQL,
    SELECT_WATERMARK_SQL,
)
from .issues import (
    clear_db_unreachable_issue,
    clear_states_worker_stalled_issue,
    create_db_unreachable_issue,
    create_states_worker_stalled_issue,
)
from .retry import retry_until_success
from .schema import sync_setup_schema
from .worker import StateRow

if TYPE_CHECKING:
    import asyncio

    from .overflow_queue import OverflowQueue

_LOGGER = logging.getLogger(__name__)


def _json_default(obj: object) -> str:
    # HA attributes may contain datetime/date objects not handled by stdlib json.
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return str(obj)


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
        super().__init__(daemon=True, name="timescaledb_states_worker")
        self._hass = hass
        self._dsn = dsn
        self._live_queue = live_queue
        self._backfill_queue = backfill_queue
        self._backfill_request = backfill_request
        self._stop_event = stop_event
        self._chunk_interval_days = chunk_interval_days
        self._compress_after_hours = compress_after_hours
        self._conn: psycopg.Connection | None = None

        # D-06-b: watchdog-readable post-mortem context. Initialized to safe defaults
        # so watchdog can always read these attributes even if run() never executes.
        # Populated by outer try/except in run() when an unhandled exception escapes
        # the main loop.
        self._last_exception: Exception | None = None
        self._last_context: dict = {
            "at": None,
            "mode": "init",
            "retry_attempt": None,
            "last_op": "unknown",
        }
        # Updated at stable checkpoints (mode transitions, pre-op) so the watchdog
        # notification carries meaningful last_op even after thread exit.
        self._last_op: str = "unknown"
        self._last_mode: str = "init"
        self._last_retry_attempt: int | None = None

        # D-03 / D-07 wiring: retry_until_success with full Phase 3 hook set.
        # Wrapping at class-body time would fix `self` too early; __init__ ensures
        # all hooks bind to this specific instance.
        self._insert_chunk = retry_until_success(
            stop_event=stop_event,
            on_transient=self.reset_db_connection,
            notify_stall=self._stall_hook,
            on_recovery=self._recovery_hook,
            on_sustained_fail=self._sustained_fail_hook,
        )(self._insert_chunk_raw)

        # D-03-c: watermark read uses same retry shape as writes (owns a connection,
        # so on_transient resets it). No stall/recovery hooks — a persistently
        # failing watermark read will block backfill; orchestrator done_callback
        # eventually surfaces the failure as a watchdog-recovery notification.
        # Runs in async_add_executor_job or states_worker OS thread — never on the
        # event loop directly (satisfies Plan 03 HIGH-3 constraint).
        self.read_watermark = retry_until_success(
            stop_event=stop_event,
            on_transient=self.reset_db_connection,
        )(self._read_watermark_raw)

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

    # ------------------------------------------------------------------
    # Phase 3 hook methods (D-02, D-03, D-11)
    # ------------------------------------------------------------------

    def _stall_hook(self, attempts: int) -> None:
        """Retry-decorator stall hook.

        D-02 / D-07-f: on STALL_THRESHOLD consecutive failures, fire BOTH:
          - persistent_notification (kept from Phase 2 for continuity)
          - states_worker_stalled repair issue (Phase 3 add)
        Both bridged via hass.add_job (thread-safe).
        """
        _LOGGER.warning(
            "states worker stalled after %d attempts — notifying + raising repair issue",
            attempts,
        )
        # Import lazily to avoid module-load-time HA import cost and keep
        # notifications orthogonal from the class's test surface.
        from homeassistant.components import persistent_notification

        # Keep the Phase 2 notification (unchanged body).
        self._hass.add_job(
            persistent_notification.async_create,
            self._hass,
            f"TimescaleDB states worker has failed {attempts} times in a row. "
            "The integration will keep retrying. Restart HA if the issue persists.",
            "TimescaleDB Recorder",
            "timescaledb_recorder_states_stalled",
        )
        # New Phase 3 repair issue — auto-clears on recovery.
        self._hass.add_job(create_states_worker_stalled_issue, self._hass)

    def _recovery_hook(self) -> None:
        """Retry-decorator recovery hook.

        D-02-c / D-03-a: fires exactly once on first success after a stall.
        Clears BOTH repair issues that may have been raised during the streak:
          - states_worker_stalled (always raised on stall — D-02)
          - db_unreachable (raised if streak also exceeded 300s — D-11)
        Cleared unconditionally — ir.async_delete_issue is a no-op if the
        issue was not present.
        """
        _LOGGER.info(
            "states worker recovered after stall — clearing repair issues",
        )
        self._hass.add_job(clear_states_worker_stalled_issue, self._hass)
        self._hass.add_job(clear_db_unreachable_issue, self._hass)

    def _sustained_fail_hook(self) -> None:
        """Retry-decorator sustained-fail hook.

        D-11: fires once when cumulative fail duration crosses
        DB_UNREACHABLE_THRESHOLD_SECONDS (300s default). Raises the
        db_unreachable repair issue; cleared by _recovery_hook on next success.
        """
        _LOGGER.warning(
            "states worker: DB unreachable for > threshold — raising repair issue",
        )
        self._hass.add_job(create_db_unreachable_issue, self._hass)

    # ------------------------------------------------------------------
    # Main loop (D-04)
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Thread entry point.

        Outer try/except catches unhandled bugs (D-06-a). The inner loop
        handles expected exceptions (queue.Empty, stop_event via continue).
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
            # Capture context HERE (in except) before finally runs.
            # If finally also raises, _last_exception already holds the
            # original fault — it is not overwritten.
            self._last_exception = err
            self._last_context = {
                "at": datetime.now(timezone.utc).isoformat(),
                "mode": self._last_mode,
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
        # Return naturally — watchdog polls is_alive() on next tick.

    def _run_main_loop(self) -> None:
        """Inner main loop extracted from run() so the outer try/except/finally
        in run() can wrap it cleanly (MEDIUM-8 pattern)."""
        # Ensure schema (idempotent; D-15-d carries Phase 1 behaviour). Retry
        # via decorator would be overkill here — startup race with DB outage
        # is handled by the main loop (continues without schema; next flush
        # cycle retries writes, which will fail until DDL succeeds on
        # reconnect). Log-and-continue preserves WATCH-02 non-blocking shape.
        self._last_op = "schema_setup"
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
        self._last_mode = mode
        buffer: list[StateRow] = []
        last_flush = time.monotonic()

        while not self._stop_event.is_set():
            # D-04-c: init/overflow → request backfill, switch to MODE_BACKFILL.
            if mode == MODE_INIT or (mode == MODE_LIVE and self._live_queue.overflowed):
                if buffer:
                    self._last_op = "flush_pre_backfill"
                    self._flush(buffer)
                    buffer.clear()
                # asyncio.Event.set is NOT thread-safe — cross the boundary.
                self._hass.loop.call_soon_threadsafe(self._backfill_request.set)
                mode = MODE_BACKFILL
                self._last_mode = mode
                last_flush = time.monotonic()
                continue

            if mode == MODE_BACKFILL:
                try:
                    item = self._backfill_queue.get(timeout=5.0)
                except queue.Empty:
                    continue  # D-04-f: timeout allows stop_event check
                if item is BACKFILL_DONE:
                    if buffer:
                        self._last_op = "flush_backfill_done"
                        self._flush(buffer)
                        buffer.clear()
                    mode = MODE_LIVE
                    self._last_mode = mode
                    last_flush = time.monotonic()
                    continue
                # item is dict[entity_id, list[HA State]] (D-04-d)
                rows = self._slice_to_rows(item)
                buffer.extend(rows)
                self._last_op = "flush_backfill_chunk"
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
                    self._last_op = "flush_live_timeout"
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
                self._last_op = "flush_live_threshold"
                self._flush(buffer)
                buffer.clear()
                last_flush = time.monotonic()

        # Shutdown: one final flush (best-effort).
        # Connection is NOT closed here — the finally block in run() handles
        # teardown unconditionally (MEDIUM-8: single teardown path).
        if buffer and self._conn is not None:
            try:
                self._last_op = "flush_shutdown"
                self._flush(buffer)
            except Exception:  # noqa: BLE001
                _LOGGER.warning("Final flush failed at shutdown; %d rows lost", len(buffer))

    # ------------------------------------------------------------------
    # Flush / chunk (D-06)
    # ------------------------------------------------------------------

    def _flush(self, rows: list[StateRow]) -> None:
        """Chunk the buffer into INSERT_CHUNK_SIZE slices; retry per chunk (D-06-a/b)."""
        for i in range(0, len(rows), INSERT_CHUNK_SIZE):
            self._insert_chunk(rows[i : i + INSERT_CHUNK_SIZE])

    def _insert_chunk_raw(self, chunk: list[StateRow]) -> None:
        """Raw insert. Wrapped by retry_until_success in __init__ (D-06-b).

        Live mode uses INSERT_LIVE_SQL (DO UPDATE) — live state_changed events
        carry full HA state-machine attributes which may be richer than what the
        HA SQLite recorder stored for the same (entity_id, last_updated).
        Backfill mode uses INSERT_SQL (DO NOTHING) — never overwrites live rows.
        """
        sql = INSERT_LIVE_SQL if self._last_mode == MODE_LIVE else INSERT_SQL
        params = [
            (r.entity_id, r.state, Jsonb(r.attributes, dumps=lambda v: json.dumps(v, default=_json_default)), r.last_updated, r.last_changed)
            for r in chunk
        ]
        conn = self.get_db_connection()
        with conn.cursor() as cur:
            cur.executemany(sql, params)

    def _slice_to_rows(self, slice_dict: dict) -> list[StateRow]:
        """Merge per-entity lists, sort by last_updated, convert to StateRow (D-04-d)."""
        all_states: list = []
        for states in slice_dict.values():
            all_states.extend(states)
        all_states.sort(key=lambda s: s.last_updated)
        return [StateRow.from_state(s) for s in all_states]

    # ------------------------------------------------------------------
    # Orchestrator-facing reads (D-08-d, D-08-f)
    # ------------------------------------------------------------------

    def _read_watermark_raw(self) -> datetime | None:
        """Raw watermark read — wrapped by retry_until_success in __init__ (D-03-c).

        Called from the event loop via hass.async_add_executor_job while the
        orchestrator is triggering a backfill. Reads through the worker's
        psycopg3 connection; on_transient=reset_db_connection ensures a dead
        connection is replaced before the next retry.

        NOTE: This runs inside async_add_executor_job (thread pool) when called
        by the orchestrator, and directly in the states_worker OS thread when
        called internally. Both contexts are off the event loop — satisfying the
        retry decorator's synchronous-blocking constraint. (Plan 03 HIGH-3.)
        """
        conn = self.get_db_connection()
        with conn.cursor() as cur:
            cur.execute(SELECT_WATERMARK_SQL)
            row = cur.fetchone()
        return row[0] if row and row[0] is not None else None

    def read_all_known_entities(self) -> set[str]:
        """Return every entity_id ever seen: entities ∪ states.

        No valid_to filter on entities: removed entities still need
        backfilling for the period before removal.
        states uses GROUP BY (not DISTINCT) to force index-based grouping
        before the UNION deduplication step — plain UNION causes a full scan.
        """
        conn = self.get_db_connection()
        with conn.cursor() as cur:
            cur.execute(SELECT_ALL_KNOWN_ENTITIES_SQL)
            return {row[0] for row in cur.fetchall()}
