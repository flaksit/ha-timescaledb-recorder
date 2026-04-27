"""Unit tests for notifications.py — unified observability bridge.

Tests cover:
  - notify_watchdog_recovery: basic path, traceback logging via caplog,
    exc=None path, extra context keys, exception-without-traceback path
  - notify_backfill_gap: basic path, missing details path
  - Both functions are plain def (not async, not @callback)

Design note: persistent_notification.async_create is patched at the module
level (custom_components.timescaledb_recorder.notifications.persistent_notification)
so the real HA component is never invoked.  hass is a MagicMock because these
helpers only pass it through to async_create.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

import pytest

from custom_components.timescaledb_recorder.notifications import (
    notify_backfill_gap,
    notify_watchdog_recovery,
)

_PATCH_TARGET = "custom_components.timescaledb_recorder.notifications.persistent_notification"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_real_exc() -> Exception:
    """Return an exception that has a real __traceback__ set (raised + caught).

    This simulates the call-site context where the exception was captured on a
    now-dead worker thread and is later passed to the watchdog helper outside
    an active except block.
    """
    try:
        raise RuntimeError("worker exploded")
    except RuntimeError as exc:
        return exc


# ---------------------------------------------------------------------------
# notify_watchdog_recovery
# ---------------------------------------------------------------------------

def test_notify_watchdog_recovery_basic():
    """Happy-path: full context dict, real exception — asserts all required
    message-body markers and correct title / notification_id."""
    hass = MagicMock()
    exc = _make_real_exc()
    ctx = {
        "at": "flush",
        "mode": "normal",
        "retry_attempt": 3,
        "last_op": "INSERT",
    }

    with patch(_PATCH_TARGET) as mock_pn:
        notify_watchdog_recovery(hass, "states_worker", exc, ctx)

    mock_pn.async_create.assert_called_once()
    call_kwargs = mock_pn.async_create.call_args.kwargs
    assert call_kwargs["title"] == "TimescaleDB recorder: states_worker restarted"
    assert call_kwargs["notification_id"] == "timescaledb_recorder.watchdog_states_worker"

    body = call_kwargs["message"]
    assert "**Component:** states_worker" in body
    assert "**Exception:**" in body
    assert "**At:**" in body
    assert "**Context:**" in body
    assert "- mode:" in body
    assert "- retry_attempt:" in body
    assert "- last_op:" in body
    assert "**Traceback (last 10 parts):**" in body


def test_notify_watchdog_recovery_logs_full_traceback(caplog):
    """The log record's formatted message must contain both the exception
    class+message AND a substring from the traceback (e.g. a function name
    from the call stack).

    We do NOT assert record.exc_info because the implementation uses explicit
    traceback formatting rather than exc_info=, per cross-AI review HIGH-4.
    """
    hass = MagicMock()
    exc = _make_real_exc()

    import logging

    with patch(_PATCH_TARGET):
        with caplog.at_level(logging.ERROR, logger="custom_components.timescaledb_recorder.notifications"):
            notify_watchdog_recovery(hass, "states_worker", exc)

    assert len(caplog.records) >= 1
    record = caplog.records[0]
    msg = record.getMessage()

    # Must contain the component/exception context
    assert "states_worker" in msg
    assert "RuntimeError" in msg or "worker exploded" in msg

    # Must contain at least one line from the captured traceback — this
    # confirms TracebackException.from_exception actually embedded the stack.
    assert "Traceback (most recent call last)" in msg or "_make_real_exc" in msg or "test_notifications" in msg


def test_notify_watchdog_recovery_none_exc():
    """When exc=None, body must contain '**Exception:** (none)' and
    '(no traceback)' — the helper should not crash."""
    hass = MagicMock()

    with patch(_PATCH_TARGET) as mock_pn:
        result = notify_watchdog_recovery(hass, "meta_worker", None, {"at": "startup"})

    assert result is None
    body = mock_pn.async_create.call_args.kwargs["message"]
    assert "**Exception:** (none)" in body
    assert "(no traceback)" in body


def test_notify_watchdog_recovery_extra_context_keys():
    """Extra context keys beyond the standard four must appear as
    '- <key>: <value>' lines in the body."""
    hass = MagicMock()
    exc = _make_real_exc()
    ctx = {
        "at": "backfill_slice",
        "mode": "backfill",
        "retry_attempt": 1,
        "last_op": "SELECT",
        "slice_start": "2026-04-20T00:00:00",
        "slice_end": "2026-04-20T01:00:00",
        "entity_count": 42,
    }

    with patch(_PATCH_TARGET) as mock_pn:
        notify_watchdog_recovery(hass, "orchestrator", exc, ctx)

    body = mock_pn.async_create.call_args.kwargs["message"]
    assert "- slice_start:" in body
    assert "- slice_end:" in body
    assert "- entity_count:" in body


def test_notify_watchdog_recovery_exception_without_traceback():
    """Construct an exception without raising it, so __traceback__ is None.

    The helper must not raise; the body should still contain the exception
    class and message (from the class+message fallback line).
    """
    hass = MagicMock()
    exc = RuntimeError("no tb")  # never raised — __traceback__ is None
    assert exc.__traceback__ is None

    with patch(_PATCH_TARGET) as mock_pn:
        # Must not raise
        notify_watchdog_recovery(hass, "states_worker", exc)

    body = mock_pn.async_create.call_args.kwargs["message"]
    # Body must at least contain the exception information
    assert "RuntimeError" in body or "no tb" in body


def test_notify_watchdog_recovery_returns_none():
    """Return value is None (plain helper, not a coroutine)."""
    hass = MagicMock()
    with patch(_PATCH_TARGET):
        result = notify_watchdog_recovery(hass, "states_worker", None)
    assert result is None


# ---------------------------------------------------------------------------
# notify_backfill_gap
# ---------------------------------------------------------------------------

def test_notify_backfill_gap_basic():
    """Happy-path: full details dict — asserts title, notification_id, and
    body contains window bounds and duration."""
    hass = MagicMock()
    details = {
        "window_start": "2026-04-20T00:00:00",
        "window_end": "2026-04-20T00:42:00",
        "duration_minutes": 42,
    }

    with patch(_PATCH_TARGET) as mock_pn:
        result = notify_backfill_gap(hass, reason="recorder_retention", details=details)

    assert result is None
    mock_pn.async_create.assert_called_once()
    call_kwargs = mock_pn.async_create.call_args.kwargs
    assert call_kwargs["title"] == "TimescaleDB recorder: backfill gap detected"
    assert call_kwargs["notification_id"] == "timescaledb_recorder.backfill_gap"

    body = call_kwargs["message"]
    assert "2026-04-20T00:00:00" in body
    assert "2026-04-20T00:42:00" in body
    assert "42 minutes" in body
    # purge_keep_days guidance must be present
    assert "purge_keep_days" in body or "recorder.purge_keep_days" in body


def test_notify_backfill_gap_missing_details():
    """When details=None, the body must contain 'unknown' placeholders and
    not crash."""
    hass = MagicMock()

    with patch(_PATCH_TARGET) as mock_pn:
        notify_backfill_gap(hass, reason="recorder_retention", details=None)

    body = mock_pn.async_create.call_args.kwargs["message"]
    assert "unknown" in body


def test_both_are_plain_def_not_coroutine():
    """Both helpers must be plain def — not async def and not @callback — so
    they can be scheduled via hass.add_job(fn, *args) from a worker thread
    without needing loop-safe wrappers.

    inspect.iscoroutinefunction checks for async def; we also verify there is
    no ha_callback attribute (set by @callback decorator).
    """
    assert not inspect.iscoroutinefunction(notify_watchdog_recovery)
    assert not inspect.iscoroutinefunction(notify_backfill_gap)
    # @callback sets a ha_callback attribute on the decorated function
    assert not getattr(notify_watchdog_recovery, "ha_callback", False)
    assert not getattr(notify_backfill_gap, "ha_callback", False)
