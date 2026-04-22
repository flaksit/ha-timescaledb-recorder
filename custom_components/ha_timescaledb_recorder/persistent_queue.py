"""PersistentQueue: file-backed FIFO with JSON-lines on-disk format.

Thread model:
- Producers (syncer @callback on event loop) call put_async() -> blocking put()
  in the default executor.
- Consumer (meta_worker thread) calls get() -> task_done() only after DB success.
- Shutdown (event loop) calls wake_consumer() to unblock a blocked get().

Crash safety (D-03-h): if the consumer crashes between get() and task_done(),
the front line is still on disk and the next process startup replays it. The
SCD2 write handler is idempotent (change-detection helpers no-op on unchanged
data), so replay is safe.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)


# Module-level sentinel distinguishing "no in-flight" from "in-flight is None/{}".
_NO_IN_FLIGHT = object()


class PersistentQueue:
    """File-backed FIFO. JSON-lines, single lock, Condition-based blocking get.

    Call order for consumer: get() -> process -> task_done(). Never call
    task_done() without a successful preceding get() — the implementation
    trusts caller discipline.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        # Item returned by get() but not yet task_done'd. On crash, the item is
        # still on disk (we never removed it); on clean task_done(), we rewrite
        # the file without the front line and clear this field.
        self._in_flight: Any = _NO_IN_FLIGHT

    # ------------------------------------------------------------------
    # Producer paths (D-03-b, D-03-c)
    # ------------------------------------------------------------------

    def put(self, item: dict) -> None:
        """Blocking producer path: append JSON line, fsync, notify consumer.

        Called from any thread. Holds _lock for the duration of the append.
        """
        line = json.dumps(item, default=str) + "\n"
        with self._cond:
            # Open in append mode so concurrent producers serialized on _lock
            # always see a consistent file. fsync ensures the append survives
            # OS crash (D-03-b).
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            self._cond.notify_all()

    async def put_async(self, item: dict) -> None:
        """Async producer path for event-loop callers (syncer @callbacks).

        D-03-c: offloads blocking file I/O to the default executor so the event
        loop does not stall on fsync.
        """
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.put, item)

    # ------------------------------------------------------------------
    # Consumer paths (D-03-d, D-03-e, D-03-f, D-03-g)
    # ------------------------------------------------------------------

    def get(self) -> dict | None:
        """Block until an item is available; return the front item WITHOUT
        removing it from disk.

        Returns None when woken by wake_consumer() with no item available —
        caller (meta_worker.run) must check its stop_event and exit if set.
        """
        with self._cond:
            while True:
                if self._in_flight is not _NO_IN_FLIGHT:
                    return self._in_flight  # unacked item from prior get()
                first = self._read_first_line_locked()
                if first is not None:
                    self._in_flight = json.loads(first)
                    return self._in_flight
                # File empty — wait for producer notify_all or wake_consumer.
                self._cond.wait()
                # On spurious wake OR wake_consumer with still-empty file,
                # return None to let caller re-check shutdown flag.
                if self._in_flight is _NO_IN_FLIGHT:
                    first = self._read_first_line_locked()
                    if first is None:
                        return None
                    # Fall through to the top of the loop — parse on next iteration.

    def task_done(self) -> None:
        """Atomically rewrite the file without the front line. Call ONLY after
        the DB write for the in-flight item succeeded.

        D-03-e: tempfile + os.replace is atomic on POSIX; partial rewrite is
        never visible to other processes.
        """
        with self._cond:
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            # Read remaining (all lines except first) and write to tmp.
            with open(self._path, "r", encoding="utf-8") as src, \
                 open(tmp, "w", encoding="utf-8") as dst:
                src.readline()  # skip the front line (= the in-flight item)
                for remaining in src:
                    dst.write(remaining)
                dst.flush()
                os.fsync(dst.fileno())
            os.replace(tmp, self._path)  # atomic swap
            self._in_flight = _NO_IN_FLIGHT

    async def join(self) -> None:
        """Block until the file is empty AND no item is in-flight (D-03-f).

        Used at startup (D-12 step 4) to drain any items persisted before the
        most recent shutdown before new events begin arriving.
        """
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._join_blocking)

    def _join_blocking(self) -> None:
        with self._cond:
            while (
                self._in_flight is not _NO_IN_FLIGHT
                or self._read_first_line_locked() is not None
            ):
                # Short wait so we re-check consumer progress periodically.
                self._cond.wait(timeout=0.5)

    def wake_consumer(self) -> None:
        """Notify all waiters on _cond. D-03-g: used at shutdown to unblock a
        blocked get(). Caller (meta_worker.run) sees its stop_event set on the
        next loop iteration and exits.
        """
        with self._cond:
            self._cond.notify_all()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _read_first_line_locked(self) -> str | None:
        """Return the first line of the file (without newline) or None if
        file does not exist or is empty. Caller MUST hold _lock.
        """
        if not self._path.exists():
            return None
        with open(self._path, "r", encoding="utf-8") as f:
            line = f.readline()
        # Strip trailing newline so json.loads gets clean input.
        return line.rstrip("\n") if line else None
