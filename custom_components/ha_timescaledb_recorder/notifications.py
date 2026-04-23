"""Unified observability bridge for watchdog recovery and backfill gaps.

D-07: notify_watchdog_recovery is the single entry point every recovery
path uses to emit a structured persistent_notification + full-traceback
_LOGGER.error. Called on the event loop directly (from watchdog_loop and
orchestrator done_callback) or bridged via hass.add_job(notify_..., hass,
component, exc, context) from worker threads.

Traceback capture uses TracebackException.from_exception (see _format_traceback)
rather than passing exc_info to _LOGGER.error, because this helper is always
called OUTSIDE an active except block (the exception was captured earlier,
stored on a dead thread's _last_exception or retrieved from task.exception()).
The logging exc_info parameter is unreliable in that context; explicit
formatting is not. (Cross-AI review 2026-04-23 — concern HIGH-4.)

D-08-b: notify_backfill_gap fires a human-readable notification when
orchestrator detects a recorder-retention overrun (HA sqlite purged the
window before TimescaleDB recovered).
"""
from __future__ import annotations

import logging
import traceback

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


def _format_traceback(exc: BaseException | None) -> str:
    """Format an exception's traceback reliably outside an active except.

    Returns the full multi-line traceback as a single string ending with
    the exception class and message. Safe to call with exc=None (returns
    "(no traceback)"). Safe to call with an exception that has no
    __traceback__ set (returns just the class+message line).
    """
    if exc is None:
        return "(no traceback)"
    try:
        tb_exc = traceback.TracebackException.from_exception(exc)
        parts = list(tb_exc.format())
        return "".join(parts) if parts else f"{exc.__class__.__name__}: {exc}"
    except Exception:  # noqa: BLE001 — formatting itself must never raise
        return f"{exc.__class__.__name__}: {exc} (traceback formatting failed)"


def notify_watchdog_recovery(
    hass: HomeAssistant,
    component: str,
    exc: BaseException | None,
    context: dict | None = None,
) -> None:
    """Fire a persistent_notification + full-traceback log for a watchdog-
    driven recovery event. Safe to call directly on the event loop; from a
    worker thread use hass.add_job(notify_watchdog_recovery, hass, component,
    exc, context) — all positional args are positional-compatible (D-07-d).

    Traceback is formatted explicitly (not via the logging exc_info parameter)
    because the caller context is outside an active except block.
    Cross-AI review HIGH-4.
    """
    ctx = context or {}

    # Format the traceback ONCE; use for both log and notification body.
    full_tb_text = _format_traceback(exc)

    # Log the full traceback at ERROR level. We build the formatted message
    # explicitly so no part of the traceback is dropped even though we're
    # outside an active except block.
    _LOGGER.error(
        "%s restarted after unhandled exception: %s\n%s",
        component,
        exc,
        full_tb_text,
    )

    # Abridged traceback for the notification body: last 10 lines of the
    # full formatted text, so the Repairs-UI notification stays readable.
    if exc is not None:
        tb_lines = full_tb_text.splitlines(keepends=True)
        tb_abridged = "".join(tb_lines[-10:]) if tb_lines else "(no traceback)"
    else:
        tb_abridged = "(no traceback)"

    # Standard context keys (D-07-c).
    at = ctx.get("at", "unknown")
    mode = ctx.get("mode")
    retry_attempt = ctx.get("retry_attempt")
    last_op = ctx.get("last_op")

    if exc is not None:
        exc_line = f"**Exception:** `{exc.__class__.__name__}: {exc}`"
    else:
        exc_line = "**Exception:** (none)"

    lines = [
        f"**Component:** {component}",
        exc_line,
        f"**At:** {at}",
        "**Context:**",
        f"- mode: {mode}",
        f"- retry_attempt: {retry_attempt}",
        f"- last_op: {last_op}",
    ]
    # Additional context keys beyond the standard four.
    for k, v in ctx.items():
        if k not in {"at", "mode", "retry_attempt", "last_op"}:
            lines.append(f"- {k}: {v}")
    lines += [
        "",
        "**Traceback (last 10 parts):**",
        f"```\n{tb_abridged}```",
        "",
        "See `home-assistant.log` for the full traceback.",
    ]
    body = "\n".join(lines)

    persistent_notification.async_create(
        hass,
        message=body,
        title=f"TimescaleDB recorder: {component} restarted",
        notification_id=f"timescaledb_recorder.watchdog_{component}",
    )


def notify_backfill_gap(
    hass: HomeAssistant,
    reason: str,
    details: dict | None = None,
) -> None:
    """Fire a persistent_notification for recorder-retention overrun (D-08).

    `reason` is informational (only "recorder_retention" emitted today).
    `details` supplies `window_start`, `window_end`, `duration_minutes`
    strings/ints used in the message body.
    """
    d = details or {}
    window_start = d.get("window_start", "unknown")
    window_end = d.get("window_end", "unknown")
    duration_minutes = d.get("duration_minutes", "unknown")

    body = (
        f"Backfill could not recover events in the range "
        f"**{window_start} — {window_end}** ({duration_minutes} minutes) "
        f"because they were purged from Home Assistant's recorder before "
        f"the TimescaleDB database became reachable.\n\n"
        f"This typically happens when `recorder.purge_keep_days` is shorter "
        f"than the TimescaleDB outage duration. Consider increasing "
        f"`recorder.purge_keep_days` to at least match your expected "
        f"worst-case outage duration.\n\n"
        f"Reason: `{reason}`"
    )

    _LOGGER.warning(
        "Backfill gap detected (%s): window_start=%s window_end=%s duration=%s min",
        reason,
        window_start,
        window_end,
        duration_minutes,
    )

    persistent_notification.async_create(
        hass,
        message=body,
        title="TimescaleDB recorder: backfill gap detected",
        notification_id="timescaledb_recorder.backfill_gap",
    )
