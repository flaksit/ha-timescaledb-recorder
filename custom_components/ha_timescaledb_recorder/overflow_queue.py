"""OverflowQueue: queue.Queue subclass with drop-newest-on-full semantics.

D-02: put_nowait never raises queue.Full. On full, sets overflowed=True and
drops the item silently. @callback producers cannot tolerate exceptions.
D-11: single warning log on first drop per outage; counter-only thereafter.
"""
from __future__ import annotations

import logging
import queue
import threading

_LOGGER = logging.getLogger(__name__)


class OverflowQueue(queue.Queue):
    """FIFO queue whose put_nowait drops the newest item on full instead of raising.

    Consumers:
    - StateIngester (event-loop @callback): calls put_nowait on every state
      change event; never blocks, never raises.
    - TimescaledbStateRecorderThread (worker thread): reads .overflowed to
      decide whether to enter MODE_BACKFILL (D-04-c).
    - backfill_orchestrator (event loop): calls clear_and_reset_overflow()
      at the start of each backfill cycle (D-02-d).

    Thread-safety: inherits queue.Queue's internal mutex for the deque; adds
    a second mutex (_overflow_lock) pairing the "enqueue attempt failed →
    set flag → drop" sequence with clear_and_reset_overflow()'s drain.
    """

    def __init__(self, maxsize: int) -> None:
        super().__init__(maxsize=maxsize)
        # overflowed is read without a lock from the worker thread. Attribute
        # reads/writes are atomic under the GIL; D-02-b makes the write path
        # monotonic (False→True until clear_and_reset_overflow resets it), so
        # lockless read gives an eventually-consistent signal sufficient for
        # the worker's mode-transition check.
        self.overflowed: bool = False
        self._overflow_lock = threading.Lock()
        # Monotonic drop counter since last reset. Consumed by orchestrator
        # in clear_and_reset_overflow's return value (D-11-c).
        self._dropped: int = 0

    def put_nowait(self, item) -> None:  # type: ignore[override]
        """Non-blocking enqueue. Drops the item silently on full.

        Never raises queue.Full. First drop per outage logs a single warning
        (D-11-a); subsequent drops increment the counter only (D-11-b).
        """
        try:
            super().put(item, block=False)
        except queue.Full:
            with self._overflow_lock:
                if not self.overflowed:
                    _LOGGER.warning(
                        "live_queue full (%d) — dropping events until DB recovery",
                        self.maxsize,
                    )
                self.overflowed = True
                self._dropped += 1

    def clear_and_reset_overflow(self) -> int:
        """Drain internal deque under mutex, reset flag, return drop-count snapshot.

        D-02-d: exclusively called by the backfill orchestrator on the event loop
        at the start of a backfill cycle. Returns the number of events dropped
        since the last reset so the caller can log D-11-c recovery line.
        """
        with self._overflow_lock:
            with self.mutex:  # queue.Queue's internal mutex
                self.queue.clear()
                self.unfinished_tasks = 0
                self.not_full.notify_all()
            dropped = self._dropped
            self._dropped = 0
            self.overflowed = False
            return dropped
