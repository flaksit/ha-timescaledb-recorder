"""Generic retry-until-success decorator used by both worker threads.

One decorator fits all (D-07-c): catches bare Exception, no error-class
branching. Persistent failure stalls the decorated call forever (D-07-d
accepted tradeoff; recovery is HA restart). Shutdown-aware via stop_event
(D-07-e, D-13-c).
"""
from __future__ import annotations

import functools
import logging
import threading
from typing import Callable

_LOGGER = logging.getLogger(__name__)

# D-07-b: exponential-ish backoff schedule, capped at the last value forever.
_BACKOFF_SCHEDULE: tuple[int, ...] = (1, 5, 10, 30, 60)

# D-07-f (Claude's Discretion): after this many consecutive failures,
# notify_stall fires once. Re-armed after the next successful call.
_STALL_NOTIFY_THRESHOLD: int = 5


def retry_until_success(
    *,
    stop_event: threading.Event,
    on_transient: Callable[[], None] | None = None,
    notify_stall: Callable[[int], None] | None = None,
    backoff_schedule: tuple[int, ...] = _BACKOFF_SCHEDULE,
    stall_threshold: int = _STALL_NOTIFY_THRESHOLD,
) -> Callable:
    """Return a decorator that retries the wrapped function forever on any
    Exception, with capped exponential backoff and shutdown-aware sleep.

    Args:
        stop_event: shared threading.Event. stop_event.wait(backoff) used for
            the sleep — returns True on shutdown; decorator exits early.
        on_transient: optional hook called AFTER each caught exception and
            BEFORE the backoff sleep. Intended for reset_db_connection (D-07-g).
            Exceptions raised by the hook itself are logged and swallowed.
        notify_stall: optional callable invoked once with the current attempt
            count when attempts first reach stall_threshold. Not called again
            until after a successful call resets the notify-armed state.
        backoff_schedule: override of the default (1, 5, 10, 30, 60) schedule.
            Indexing saturates at the last element.
        stall_threshold: override of the default 5-attempt notify threshold.

    Behaviour:
        - Broad exception catch per D-07-c. No error-class branching.
        - Returns the wrapped function's return value on success.
        - Returns None if stop_event is set during a backoff wait (D-07-e).
    """

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            attempts = 0
            notified = False
            while True:
                try:
                    result = fn(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 — D-07-c: broad by design
                    attempts += 1
                    # D-07-g: drop stale connection so caller reconnects lazily.
                    if on_transient is not None:
                        try:
                            on_transient()
                        except Exception:  # noqa: BLE001
                            _LOGGER.exception(
                                "on_transient hook raised in retry_until_success; "
                                "continuing",
                            )
                    # D-07-f: stall notification on first crossing of threshold.
                    if not notified and attempts >= stall_threshold:
                        if notify_stall is not None:
                            try:
                                notify_stall(attempts)
                            except Exception:  # noqa: BLE001
                                _LOGGER.exception(
                                    "notify_stall hook raised; continuing",
                                )
                        notified = True
                    idx = min(attempts - 1, len(backoff_schedule) - 1)
                    backoff = backoff_schedule[idx]
                    _LOGGER.warning(
                        "%s failed (attempt %d); retrying in %ds: %s",
                        fn.__qualname__, attempts, backoff, exc,
                    )
                    # D-07-e / D-13-c: interruptible sleep.
                    if stop_event.wait(backoff):
                        _LOGGER.info(
                            "%s retry interrupted by shutdown after %d attempts",
                            fn.__qualname__, attempts,
                        )
                        return None
                    continue
                # Success path: reset notify-armed state so a future stall notifies again.
                if notified:
                    notified = False
                    attempts = 0
                return result
        return wrapper
    return decorator
