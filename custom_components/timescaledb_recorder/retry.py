"""Generic retry-until-success decorator used by both worker threads.

Catches every Exception. Transient errors (see is_transient) are retried
forever (D-07-d: recovery from a long outage is waiting it out). Any other
error means the input itself is bad: a wrapper given an on_poison hook gives up
on it after POISON_MAX_ATTEMPTS consecutive such failures and hands it to the
hook, so one poison item cannot wedge a queue (issue #20). Without the hook
every error is retried forever — right for calls with no item to drop (schema
setup, reads). Shutdown-aware via stop_event (D-07-e, D-13-c).
"""
from __future__ import annotations

import functools
import logging
import threading
import time
from typing import Any, Callable

import psycopg

from .const import POISON_MAX_ATTEMPTS, STALL_THRESHOLD

_LOGGER = logging.getLogger(__name__)

# D-07-b: exponential-ish backoff schedule, capped at the last value forever.
_BACKOFF_SCHEDULE: tuple[int, ...] = (1, 5, 10, 30, 60)

# Server-side errors that say "this database will not take writes right now",
# not "this input is bad". psycopg maps them outside OperationalError, but
# dropping data because of a failover or a revoked grant would turn a fixable
# outage into permanent loss.
_TRANSIENT_DB_ERRORS: tuple[type[Exception], ...] = (
    psycopg.errors.ReadOnlySqlTransaction,
    psycopg.errors.InsufficientPrivilege,
)


def is_transient(exc: BaseException) -> bool:
    """True if retrying the same input can succeed once the environment recovers.

    OperationalError covers connection loss, server shutdown, resource
    exhaustion, query cancellation and every TransactionRollback (deadlock,
    serialization failure); InterfaceError and OSError cover client-side
    connection trouble. ProgramLimitExceeded is an OperationalError in psycopg
    but SQLSTATE class 54 is about the statement (e.g. an index row too large),
    so the same input fails forever.
    """
    if isinstance(exc, psycopg.errors.ProgramLimitExceeded):
        return False
    return isinstance(
        exc,
        (psycopg.OperationalError, psycopg.InterfaceError, OSError, *_TRANSIENT_DB_ERRORS),
    )


def retry_until_success(
    *,
    stop_event: threading.Event,
    on_transient: Callable[[], None] | None = None,
    notify_stall: Callable[[int], None] | None = None,
    on_recovery: Callable[[], None] | None = None,
    on_sustained_fail: Callable[[], None] | None = None,
    on_poison: Callable[..., Any] | None = None,
    sustained_fail_seconds: float = 300.0,
    backoff_schedule: tuple[int, ...] = _BACKOFF_SCHEDULE,
    stall_threshold: int = STALL_THRESHOLD,
    poison_max_attempts: int = POISON_MAX_ATTEMPTS,
) -> Callable:
    """Retry decorator for thread-worker DB calls.

    IMPORTANT: This decorator uses threading.Event.wait(backoff) which is a
    SYNCHRONOUS blocking call. It is intended for use in dedicated OS threads
    or executor jobs (async_add_executor_job) ONLY. Never call a wrapped
    function directly from the HA event loop — it will block the loop.
    (Cross-AI review 2026-04-23, concern HIGH-3.)

    All timing uses time.monotonic() to be immune to wall-clock jumps from NTP
    adjustments, DST, or VM migration. (Cross-AI review 2026-04-23, concern MEDIUM-7.)

    Args:
        stop_event: shared threading.Event. stop_event.wait(backoff) used for
            the sleep — returns True on shutdown; decorator exits early.
        on_transient: optional hook called AFTER each caught exception and
            BEFORE the backoff sleep. Intended for reset_db_connection (D-07-g).
            Exceptions raised by the hook itself are logged and swallowed.
            Pass None (the default) for read paths that do not own a connection
            (e.g. wrapped calls through the recorder pool).
        notify_stall: optional callable invoked once with the current attempt
            count when attempts first reach stall_threshold. Not called again
            until after a successful call resets the notify-armed state.
        on_recovery: fired exactly once after a successful call that immediately
            follows a stall. Closure flag `stalled: bool` arms on stall threshold
            crossing, clears after on_recovery fires.
            Exceptions raised by the hook are logged and swallowed.
        on_sustained_fail: fired exactly once per failure streak when
            time.monotonic() - first_fail_ts >= sustained_fail_seconds. Closure
            flag `sustained_notified: bool` guards duplicate invocations; both
            timer and flag reset on success.
            Exceptions raised by the hook are logged and swallowed.
        on_poison: optional ``on_poison(exc, *args, **kwargs)`` called with the
            last exception and the wrapped call's arguments once
            poison_max_attempts consecutive non-transient failures accumulate.
            Its return value becomes the wrapper's. A transient failure resets
            the count. None (the default) retries every error forever.
            Unlike the other hooks, exceptions it raises propagate: it is the
            result path, not a notification.
        sustained_fail_seconds: threshold in seconds for on_sustained_fail
            (default 300.0 = DB_UNREACHABLE_THRESHOLD_SECONDS).
        backoff_schedule: override of the default (1, 5, 10, 30, 60) schedule.
            Indexing saturates at the last element.
        stall_threshold: override of the default STALL_THRESHOLD (5) attempt
            notify threshold.
        poison_max_attempts: override of POISON_MAX_ATTEMPTS.

    All hooks except on_poison are best-effort: exceptions inside them are
    logged and swallowed.

    Behaviour:
        - Returns the wrapped function's return value on success.
        - Returns on_poison's return value when giving up on a poison input.
          Giving up ends the failure streak like a success does: on_recovery
          fires if the streak had stalled, since the worker is moving again.
        - Returns None if stop_event is set during a backoff wait (D-07-e).
    """

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            attempts = 0
            notified = False          # stall-notified flag; re-armed after success
            stalled = False           # arms on_recovery; set True at stall threshold, False after on_recovery fires
            first_fail_ts: float | None = None   # monotonic timestamp of first failure in current streak
            sustained_notified = False  # guards on_sustained_fail from firing twice in same streak
            poison_attempts = 0       # consecutive non-transient failures
            while True:
                try:
                    result = fn(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 — classified below
                    attempts += 1
                    poison_attempts = 0 if is_transient(exc) else poison_attempts + 1
                    now_mono = time.monotonic()
                    if first_fail_ts is None:
                        first_fail_ts = now_mono
                    # D-07-g: drop stale connection so caller reconnects lazily.
                    if on_transient is not None:
                        try:
                            on_transient()
                        except Exception:  # noqa: BLE001
                            _LOGGER.exception(
                                "on_transient hook raised in retry_until_success; "
                                "continuing",
                            )
                    if on_poison is not None and poison_attempts >= poison_max_attempts:
                        _LOGGER.error(
                            "%s failed %d times with a non-transient error; giving up "
                            "on this input: %r",
                            fn.__qualname__, poison_attempts, exc,
                        )
                        if stalled and on_recovery is not None:
                            try:
                                on_recovery()
                            except Exception:  # noqa: BLE001
                                _LOGGER.exception("on_recovery hook raised; continuing")
                        return on_poison(exc, *args, **kwargs)
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
                        stalled = True  # arm on_recovery for the next success
                    # D-11: sustained-fail notification — fires once when cumulative
                    # fail duration exceeds the threshold (e.g. DB unreachable >5 min).
                    if (
                        not sustained_notified
                        and on_sustained_fail is not None
                        and first_fail_ts is not None
                        and now_mono - first_fail_ts >= sustained_fail_seconds
                    ):
                        try:
                            on_sustained_fail()
                        except Exception:  # noqa: BLE001
                            _LOGGER.exception(
                                "on_sustained_fail hook raised; continuing",
                            )
                        sustained_notified = True
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
                # Success path: fire on_recovery if we were stalled, then reset all
                # closure state so a future stall cycle starts fresh.
                if stalled and on_recovery is not None:
                    try:
                        on_recovery()
                    except Exception:  # noqa: BLE001
                        _LOGGER.exception("on_recovery hook raised; continuing")
                stalled = False
                notified = False
                sustained_notified = False
                first_fail_ts = None
                attempts = 0
                return result
        return wrapper
    return decorator
